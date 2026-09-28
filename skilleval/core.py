"""Core evaluation logic: run each task with and without the skill, measure."""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path

from .backends import Backend, Completion
from .config import Config, CheckCommand
from .verify import run_verify
from .trace_analysis import analyse_trace, TraceAnalysis


# --------------------------------------------------------------------------- #
# Result record
# --------------------------------------------------------------------------- #

@dataclass
class RunResult:
    task_id: str
    variant: str            # "control" | "skill"
    repeat: int
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    latency_s: float = 0.0
    cost_usd: float = 0.0
    checks: dict[str, bool] = field(default_factory=dict)   # check name -> passed
    judge_score: float | None = None
    judge_rationale: str = ""
    skill_verified: bool | None = None   # None = not checked, True/False = probe result
    rule_checks: dict[str, bool] = field(default_factory=dict)  # rule name -> passed
    friction: list[str] = field(default_factory=list)   # friction point messages from trace
    artifact_path: str = ""
    error: str = ""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def load_skill_text(cfg: Config) -> str:
    base = cfg.resolve(cfg.skill_base_dir)
    files: list[Path] = []
    for pattern in cfg.skill_files:
        p = Path(pattern)
        if p.is_absolute():
            files.extend(sorted(Path(p.anchor).glob(str(p.relative_to(p.anchor)))))
        else:
            files.extend(sorted(base.glob(pattern)))
    seen, unique = set(), []
    for f in files:
        if f.is_file() and f not in seen:
            seen.add(f)
            unique.append(f)
    parts = [f"# ===== {f.name} =====\n{f.read_text()}" for f in unique]
    if not parts:
        raise RuntimeError(f"No skill files matched {cfg.skill_files} under {base}")
    return "\n\n".join(parts)


def extract_artifact(text: str, mode: str) -> str:
    if mode == "raw":
        return text.strip()
    m = re.search(r"```[a-zA-Z0-9]*\s*\n(.*?)```", text, re.DOTALL)
    return (m.group(1) if m else text).strip()


def _find_latest_session_jsonl(cwd: str, after_ts: float) -> Path | None:
    """Find the most recently modified session JSONL written by the claude CLI.

    The CLI writes session files to ~/.claude/projects/<slug>/*.jsonl where
    <slug> is derived from the working directory. We look for any JSONL modified
    after `after_ts` (epoch seconds) across all project slugs.
    """
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        return None
    candidates: list[Path] = []
    for f in projects_dir.rglob("*.jsonl"):
        try:
            if f.stat().st_mtime > after_ts:
                candidates.append(f)
        except OSError:
            pass
    if not candidates:
        return None
    return max(candidates, key=lambda f: f.stat().st_mtime)


def _fmt(template: str, task: dict, extra: dict | None = None) -> str:
    values = {"task_id": task.get("id", ""), **{k: str(v) for k, v in task.items()}}
    if extra:
        values.update(extra)
    try:
        return template.format(**values)
    except (KeyError, IndexError):
        return template


# --------------------------------------------------------------------------- #
# Agentic (realistic developer) run
# --------------------------------------------------------------------------- #

def run_task(cfg: Config, backend: Backend, skill_text: str, task: dict,
             variant: str, repeat: int, out_dir: Path) -> RunResult:
    """Run the model as an agent inside the repository, exactly like a developer.

    The agent has tools enabled, works in the repo (so it can read existing
    tests/components and follow real conventions) and writes the spec file at
    its real path. We read that file back as the artifact, then remove it so the
    working tree stays clean.
    """
    res = RunResult(task_id=task["id"], variant=variant, repeat=repeat)
    repo_root = cfg.repo_root_path

    no_write = bool(task.get("no_write"))
    target_rel = _fmt(cfg.agent_write_to, task) if cfg.agent_write_to and not no_write else None
    target_abs = (repo_root / target_rel) if target_rel else None
    work_dir = repo_root / _fmt(cfg.agent_cwd, task)

    system = cfg.system_base
    plugin_dir: str | None = None
    if variant == "skill":
        if cfg.skill_base_dir and cfg.backend == "claude-cli":
            # Load the skill via --plugin-dir so the agent invokes it as a tool,
            # matching the real Claude Code usage pattern.
            plugin_dir = str(cfg.resolve(cfg.skill_base_dir))
        else:
            # Fallback: inject skill into system prompt (SDK backend or no skill_base_dir).
            system = f"{cfg.system_base}\n\n{cfg.skill_preamble}{skill_text}"

    prompt = task["prompt"].strip()
    if target_rel:
        prompt += (
            f"\n\nCreate the file at this exact path (relative to the repository "
            f"root): {target_rel}"
        )
    if cfg.agent_instructions:
        prompt += f"\n\n{cfg.agent_instructions}"

    pre_existing = bool(target_abs and target_abs.exists())
    prior_text = target_abs.read_text() if pre_existing else None
    if target_abs and target_abs.exists() and not pre_existing:
        target_abs.unlink(missing_ok=True)

    try:
        t0 = time.time()
        c: Completion = backend.complete(
            prompt, system, model=cfg.model, no_tools=False,
            timeout=1200, cwd=str(work_dir), agentic=True,
            skip_permissions=cfg.agent_skip_permissions,
            plugin_dir=plugin_dir,
        )
        res.latency_s = round(time.time() - t0, 2)
        res.input_tokens = c.input_tokens
        res.output_tokens = c.output_tokens
        res.cache_read_tokens = c.cache_read_tokens
        res.cache_write_tokens = c.cache_write_tokens
        res.cost_usd = c.cost_usd
        res.model = c.model

        # Trace analysis — find the session JSONL written during this run
        session_jsonl = _find_latest_session_jsonl(str(work_dir), t0)
        if session_jsonl:
            ta = analyse_trace(session_jsonl)
            res.friction = [f"[{fp.kind}] {fp.message}" for fp in ta.friction]

        if target_abs and target_abs.exists():
            artifact = target_abs.read_text()
        else:
            artifact = extract_artifact(c.text, cfg.extract)

        if not artifact:
            res.error = "empty artifact (agent produced no output)"
        else:
            art_dir = out_dir / "artifacts"
            art_dir.mkdir(parents=True, exist_ok=True)
            art_file = art_dir / f"{task['id']}.{variant}.{repeat}.{cfg.artifact_ext}"
            art_file.write_text(artifact)
            res.artifact_path = str(art_file)
    except Exception as exc:  # noqa: BLE001
        res.error = str(exc)
    finally:
        if target_abs:
            if prior_text is not None:
                target_abs.write_text(prior_text)
            else:
                target_abs.unlink(missing_ok=True)
    return res


# --------------------------------------------------------------------------- #
# Quality checks
# --------------------------------------------------------------------------- #

def run_checks(cfg: Config, task: dict, res: RunResult) -> None:
    if res.error or not res.artifact_path or not cfg.checks:
        return
    repo_root = cfg.repo_root_path
    artifact = Path(res.artifact_path).read_text()

    written: list[Path] = []
    try:
        for chk in cfg.checks:
            if chk.write_to:
                dest = repo_root / _fmt(chk.write_to, task)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(artifact)
                written.append(dest)
            cwd = repo_root / _fmt(chk.cwd, task)
            try:
                command = [_fmt(arg, task) for arg in chk.command]
                proc = subprocess.run(
                    command, cwd=cwd, capture_output=True, text=True,
                    timeout=chk.timeout,
                )
                res.checks[chk.name] = proc.returncode == 0
            except subprocess.TimeoutExpired:
                res.checks[chk.name] = False
                res.error = (res.error + f" | {chk.name} timeout").strip(" |")
    finally:
        for w in written:
            w.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# LLM-as-a-judge
# --------------------------------------------------------------------------- #

def run_judge(cfg: Config, backend: Backend, task: dict, res: RunResult,
              skill_text: str = "") -> None:
    if not cfg.judge_rubric or res.error or not res.artifact_path:
        return
    rubric = cfg.judge_rubric.replace("{skill}", skill_text) if skill_text else cfg.judge_rubric
    artifact = Path(res.artifact_path).read_text()
    prompt = (
        f"TASK:\n{task['prompt']}\n\n"
        f"GENERATED OUTPUT:\n```\n{artifact}\n```\n\nScore it per the rubric."
    )
    try:
        c = backend.complete(prompt, rubric, model=cfg.model,
                             no_tools=True, timeout=300)
        m = re.search(r"\{.*\}", c.text, re.DOTALL)
        parsed = json.loads(m.group(0) if m else c.text)
        res.judge_score = float(parsed.get("score"))
        res.judge_rationale = str(parsed.get("rationale", ""))[:300]
    except Exception as exc:  # noqa: BLE001
        res.judge_rationale = f"judge error: {exc}"


# --------------------------------------------------------------------------- #
# Rule extraction
# --------------------------------------------------------------------------- #

_EXTRACT_RULES_SYSTEM = """\
You are a skill analyst. Given a skill definition, extract every concrete rule
that a generated artifact must follow.

Return ONLY a JSON array — no prose, no markdown fences:
[{"name": "<short label, max 40 chars>", "description": "<what to verify in the artifact>"}]
"""

def extract_rules(cfg: Config, backend: Backend, skill_text: str) -> list[dict]:
    prompt = f"Extract all rules from this skill:\n\n{skill_text}"
    try:
        c = backend.complete(prompt, _EXTRACT_RULES_SYSTEM, model=cfg.model,
                             no_tools=True, timeout=120)
        m = re.search(r"\[.*\]", c.text, re.DOTALL)
        return json.loads(m.group(0) if m else c.text)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Rule extraction failed: {exc}") from exc


# --------------------------------------------------------------------------- #
# Rule compliance check
# --------------------------------------------------------------------------- #

_RULE_CHECK_SYSTEM = """\
You are a strict code reviewer. For each rule provided, check whether the artifact
follows it. Return ONLY a JSON object — no prose, no markdown fences:
{"<rule_name>": true, "<rule_name>": false, ...}
Use the exact rule names from the input.
"""

def run_rule_checks(cfg: Config, backend: Backend, res: RunResult,
                    rules: list[dict]) -> None:
    if res.error or not res.artifact_path or not rules:
        return
    artifact = Path(res.artifact_path).read_text()
    rules_text = json.dumps(rules, indent=2)
    prompt = f"RULES:\n{rules_text}\n\nARTIFACT:\n```\n{artifact}\n```"
    try:
        c = backend.complete(prompt, _RULE_CHECK_SYSTEM, model=cfg.model,
                             no_tools=True, timeout=120)
        m = re.search(r"\{.*\}", c.text, re.DOTALL)
        parsed = json.loads(m.group(0) if m else c.text)
        res.rule_checks = {k: bool(v) for k, v in parsed.items()}
    except Exception as exc:  # noqa: BLE001
        res.rule_checks = {"error": False}


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def load_tasks(cfg: Config) -> list[dict]:
    path = cfg.resolve(cfg.tasks_file)
    tasks = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    for i, t in enumerate(tasks):
        if "id" not in t:
            t["id"] = f"task-{i+1}"
        if "prompt" not in t:
            raise ValueError(f"Task {t['id']} is missing a 'prompt' field.")
    return tasks


def evaluate(cfg: Config, backend: Backend, *, repeats: int, do_checks: bool,
             do_judge: bool, do_check_rules: bool, out_dir: Path,
             progress=print) -> list[RunResult]:
    skill_text = load_skill_text(cfg)
    tasks = load_tasks(cfg)
    results: list[RunResult] = []

    rules: list[dict] = []
    if do_check_rules:
        progress("Extracting rules from skill …")
        rules = extract_rules(cfg, backend, skill_text)
        progress(f"  {len(rules)} rule(s) extracted: {[r['name'] for r in rules]}")

    for task in tasks:
        for variant in ("control", "skill"):
            for rep in range(1, repeats + 1):
                progress(f"[{task['id']}] {variant} run {rep}/{repeats} …")
                res = run_task(cfg, backend, skill_text, task, variant, rep, out_dir)
                if variant == "skill" and not res.error:
                    verify = run_verify(cfg, backend)
                    res.skill_verified = verify.verified
                    if not verify.verified:
                        progress(f"   ! skill verify FAIL (probe response: {verify.probe_response[:80]!r})")
                if do_checks:
                    run_checks(cfg, task, res)
                if do_judge:
                    run_judge(cfg, backend, task, res, skill_text)
                if do_check_rules and rules:
                    run_rule_checks(cfg, backend, res, rules)
                results.append(res)
                if res.error:
                    progress(f"   ! error: {res.error}")

    raw = out_dir / "raw_results.jsonl"
    with raw.open("w") as f:
        for r in results:
            f.write(json.dumps(asdict(r)) + "\n")
    return results
