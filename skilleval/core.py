"""Core evaluation logic: run each task with and without the skill, measure."""

from __future__ import annotations

import json
import queue
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict, field
from pathlib import Path

from .backends import Backend, Completion
from .config import Config, CheckCommand
from .verify import run_verify
from .trace_analysis import analyse_trace, TraceAnalysis, extract_structured_response, extract_target_file, extract_written_content


# --------------------------------------------------------------------------- #
# Result record
# --------------------------------------------------------------------------- #

@dataclass
class RunResult:
    task_id: str
    variant: str            # "control" | "skill"
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    latency_s: float = 0.0
    cost_usd: float = 0.0
    checks: dict[str, bool] = field(default_factory=dict)   # check name -> passed
    post_checks: dict[str, bool] = field(default_factory=dict)   # post-check name -> passed
    judge_score: float | None = None
    judge_rationale: str = ""
    skill_verified: bool | None = None   # None = not checked, True/False = probe result
    rule_checks: dict[str, bool] = field(default_factory=dict)  # rule name -> passed
    friction: list[str] = field(default_factory=list)   # friction point messages from trace
    artifact_path: str = ""
    original_artifact_path: str = ""  # content of target file before agent ran (for judge diff)
    agent_target_path: str = ""   # real file the agent wrote to (agent_write_to resolved)
    trace_path: str = ""          # session JSONL path for post-check target extraction
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
    <slug> is derived from the working directory by replacing '/' with '-'.
    We look only inside the project directory that corresponds to `cwd` so
    that parallel runs on different worktrees never steal each other's traces.
    """
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        return None

    # Derive the slug the CLI would use for this cwd (replace / with -)
    slug = cwd.replace("/", "-")
    project_dir = projects_dir / slug

    if project_dir.exists():
        # Fast path: look only in the exact project directory for this cwd
        candidates = [
            f for f in project_dir.glob("*.jsonl")
            if f.stat().st_mtime > after_ts
        ]
    else:
        # Fallback: scan all projects (single-machine, no worktrees)
        candidates = [
            f for f in projects_dir.rglob("*.jsonl")
            if f.stat().st_mtime > after_ts
        ]
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
             variant: str, out_dir: Path) -> tuple["RunResult", dict]:
    """Run the model as an agent inside the repository, exactly like a developer.

    Returns (RunResult, snapshot) where snapshot is a dict of {Path: original_text|None}
    for every file the agent may have modified. Call restore_snapshot(snapshot) after
    post-checks to leave the working tree clean.
    """
    res = RunResult(task_id=task["id"], variant=variant)
    repo_root = cfg.repo_root_path

    no_write = bool(task.get("no_write"))
    # agent_write_to can be overridden per-task (tasks.jsonl field takes priority)
    write_to_template = task.get("agent_write_to") or cfg.agent_write_to
    target_rel = _fmt(write_to_template, task) if write_to_template and not no_write else None
    target_abs = (repo_root / target_rel) if target_rel else None
    work_dir = repo_root / _fmt(cfg.agent_cwd, task)

    # Snapshot tracked files under agent_cwd before the agent runs.
    # Used by run_post_checks to distinguish pre-existing files from new ones.
    # restore_snapshot uses git checkout/clean so only the pre_existing set matters.
    snapshot: dict[Path, str | None] = {}
    # Save original content of the target file (if it exists) so the judge can
    # diff against it and score only the new code, not the pre-existing content.
    original_target_content: str | None = None
    if cfg.post_checks and not no_write:
        try:
            ls = subprocess.run(
                ["git", "ls-files"], cwd=str(work_dir),
                capture_output=True, text=True,
            )
            for rel in ls.stdout.splitlines():
                abs_f = work_dir / rel
                snapshot[abs_f] = ""  # non-None = pre-existing; content not needed for restore
        except Exception:
            pass
        if target_abs and target_abs not in snapshot:
            snapshot[target_abs] = target_abs.read_text() if target_abs.exists() else None
    # Capture original content for judge diff regardless of post_checks config
    if target_abs and target_abs.exists():
        original_target_content = target_abs.read_text()

    system = cfg.system_base
    plugin_dir: str | None = None
    if variant == "skill":
        if cfg.skill_base_dir and cfg.backend == "claude-cli":
            plugin_dir = str(cfg.resolve(cfg.skill_base_dir))
        else:
            system = f"{cfg.system_base}\n\n{cfg.skill_preamble}{skill_text}"

    prompt = task["prompt"].strip()
    if target_rel:
        target_abs_check = repo_root / target_rel
        if target_abs_check.exists():
            prompt += (
                f"\n\nEdit the existing file at this path (relative to the repository "
                f"root): {target_rel}\nDo not create a new file."
            )
        else:
            prompt += (
                f"\n\nCreate the file at this exact path (relative to the repository "
                f"root): {target_rel}"
            )
    if cfg.agent_instructions:
        prompt += f"\n\n{cfg.agent_instructions}"
    if cfg.post_checks and not no_write:
        prompt += (
            "\n\nAt the very end of your response, output the file you edited or created "
            "in this exact format so automated checks can verify it:\n"
            "FILE: <path/to/file relative to repo root>\n"
            "```\n<complete file content>\n```"
        )

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
            res.trace_path = str(session_jsonl)

        if target_abs and target_abs.exists():
            artifact = target_abs.read_text()
        else:
            artifact = extract_artifact(c.text, cfg.extract)

        if not artifact:
            res.error = "empty artifact (agent produced no output)"
        else:
            art_dir = out_dir / "artifacts"
            art_dir.mkdir(parents=True, exist_ok=True)
            art_file = art_dir / f"{task['id']}.{variant}.{cfg.artifact_ext}"
            art_file.write_text(artifact)
            res.artifact_path = str(art_file)
            if original_target_content is not None:
                orig_file = art_dir / f"{task['id']}.{variant}.original.{cfg.artifact_ext}"
                orig_file.write_text(original_target_content)
                res.original_artifact_path = str(orig_file)
            if target_abs:
                res.agent_target_path = str(target_abs)
    except Exception as exc:  # noqa: BLE001
        res.error = str(exc)
    return res, snapshot


def restore_snapshot(snapshot: dict) -> None:
    """Restore the working tree to its pre-run state via git.

    Uses `git checkout -- .` to restore all tracked modifications and
    `git clean -fd` to remove any untracked files the agent created.
    Falls back to the in-memory snapshot for files outside the git repo.
    """
    if not snapshot:
        return

    # Find the git repo root from the first path in the snapshot
    repo_root: Path | None = None
    for p in snapshot:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=str(Path(p).parent), capture_output=True, text=True,
            )
            if result.returncode == 0:
                repo_root = Path(result.stdout.strip())
                break
        except Exception:
            pass

    if repo_root:
        try:
            # Restore all tracked modifications
            subprocess.run(
                ["git", "checkout", "--", "."],
                cwd=str(repo_root), capture_output=True,
            )
            # Remove all untracked files/dirs the agent created
            subprocess.run(
                ["git", "clean", "-fd"],
                cwd=str(repo_root), capture_output=True,
            )
        except Exception:
            pass
    else:
        # Fallback: use in-memory snapshot
        for path, original in snapshot.items():
            path = Path(path)
            if original is not None:
                path.write_text(original)
            else:
                path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Git worktree helpers for parallel isolation
# --------------------------------------------------------------------------- #

def _find_git_root(path: Path) -> Path | None:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(path), capture_output=True, text=True,
        )
        return Path(r.stdout.strip()) if r.returncode == 0 else None
    except Exception:
        return None


def _setup_worktree(git_root: Path, name: str) -> Path:
    """Create a git worktree at <git_root>/../.skilleval-worktrees/<name>."""
    worktrees_dir = git_root.parent / ".skilleval-worktrees"
    worktrees_dir.mkdir(exist_ok=True)
    wt_path = worktrees_dir / name
    if wt_path.exists():
        subprocess.run(["git", "worktree", "remove", "--force", str(wt_path)],
                       cwd=str(git_root), capture_output=True)
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(wt_path), "HEAD"],
        cwd=str(git_root), capture_output=True, check=True,
    )
    return wt_path


def _teardown_worktree(git_root: Path, wt_path: Path) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(wt_path)],
        cwd=str(git_root), capture_output=True,
    )


def _write_log(path: Path, command: list[str], cwd: Path, returncode: int,
               stdout: str, stderr: str) -> None:
    lines = [
        f"command: {' '.join(command)}",
        f"cwd:     {cwd}",
        f"exit:    {returncode}",
        "",
    ]
    if stdout.strip():
        lines += ["--- stdout ---", stdout.rstrip(), ""]
    if stderr.strip():
        lines += ["--- stderr ---", stderr.rstrip(), ""]
    path.write_text("\n".join(lines))


# --------------------------------------------------------------------------- #
# Quality checks
# --------------------------------------------------------------------------- #

def run_checks(cfg: Config, task: dict, res: RunResult, snapshot: dict | None = None) -> None:
    if res.error or not res.artifact_path or not cfg.checks:
        return
    repo_root = cfg.repo_root_path
    artifact = Path(res.artifact_path).read_text()

    pre_existing: set[Path] = {p for p, txt in (snapshot or {}).items() if txt is not None}

    # Resolve target from trace if available (same logic as run_post_checks)
    trace_target: str | None = None
    if res.trace_path:
        trace_target, trace_artifact = extract_structured_response(res.trace_path)
        if trace_target:
            candidate_abs = (repo_root / trace_target).resolve()
            if pre_existing and candidate_abs not in pre_existing:
                trace_target = None
        if not trace_artifact:
            trace_artifact = extract_written_content(res.trace_path)
        if not trace_target:
            trace_target = extract_target_file(res.trace_path, repo_root=repo_root,
                                               allowed_paths=pre_existing or None)
        if trace_artifact:
            artifact = trace_artifact

    written: list[tuple[Path, str | None]] = []
    try:
        for chk in cfg.checks:
            if chk.write_to or trace_target:
                dest = (repo_root / trace_target) if trace_target else repo_root / _fmt(chk.write_to, task)
                dest.parent.mkdir(parents=True, exist_ok=True)
                original = dest.read_text() if dest.exists() else None
                dest.write_text(artifact)
                written.append((dest, original))
            # Expose target_basename for command placeholders
            if trace_target:
                ctx = {**task, "target_basename": Path(trace_target).name,
                       "target_file": trace_target}
            else:
                ctx = task
            cwd = repo_root / _fmt(chk.cwd, ctx)
            try:
                command = [_fmt(arg, ctx) for arg in chk.command]
                proc = subprocess.run(
                    command, cwd=cwd, capture_output=True, text=True,
                    timeout=chk.timeout,
                )
                res.checks[chk.name] = proc.returncode == 0
            except subprocess.TimeoutExpired:
                res.checks[chk.name] = False
                res.error = (res.error + f" | {chk.name} timeout").strip(" |")
    finally:
        for dest, original in written:
            if original is not None:
                dest.write_text(original)
            else:
                dest.unlink(missing_ok=True)


def run_post_checks(cfg: Config, task: dict, res: RunResult, out_dir: Path | None = None,
                    snapshot: dict | None = None, worktree_root: "Path | None" = None) -> None:
    if res.error or not res.artifact_path or not cfg.post_checks:
        return
    repo_root = cfg.repo_root_path
    artifact = Path(res.artifact_path).read_text()

    # Normalise placeholders: {case_name} = {id} for compatibility with plugin-eval config
    task = {**task, "case_name": task.get("id", "")}

    # Files that existed before the agent ran (snapshot keys with non-None values).
    # Used to reject invented files the agent created from scratch.
    # When running in parallel the snapshot was taken inside a git worktree.
    # worktree_root lets us remap those paths to original repo_root so lookups work.
    raw_pre: set[Path] = {p for p, txt in (snapshot or {}).items() if txt is not None}
    pre_existing: set[Path] = set()
    wt_root: Path = worktree_root if worktree_root is not None else repo_root
    for p in raw_pre:
        if p.is_relative_to(repo_root):
            pre_existing.add(p)  # already under original repo_root
        elif wt_root != repo_root and p.is_relative_to(wt_root):
            # Remap worktree-relative path to original repo_root
            pre_existing.add((repo_root / p.relative_to(wt_root)).resolve())
        else:
            pre_existing.add(p)  # unknown origin — keep as-is

    log_dir: Path | None = None
    if out_dir:
        log_dir = out_dir / "post_check_logs" / "ab"
        log_dir.mkdir(parents=True, exist_ok=True)

    # Resolve target file from trace (same strategies as plugin_eval.py):
    # 1. Structured FILE: response  2. Write tool call  3. extract_target_file heuristic
    # Fall back to agent_target_path (agent_write_to template) only if trace gives nothing.
    # Only accept a candidate if it was pre-existing (agent edited it, not created it).
    trace_target: str | None = None
    if res.trace_path:
        trace_target, trace_artifact = extract_structured_response(res.trace_path)
        if trace_target:
            # Normalize absolute paths to repo-relative.
            # When running in parallel the agent works inside a git worktree
            # (.skilleval-worktrees/skilleval-worker-N/) so the FILE: response
            # often contains an absolute worktree path.  Convert it to a path
            # relative to repo_root so pre_existing lookup and dest resolution work.
            t_path = Path(trace_target)
            if t_path.is_absolute():
                try:
                    trace_target = str(t_path.relative_to(repo_root))
                except ValueError:
                    # Try using worktree_root to remap the path
                    if wt_root != repo_root:
                        try:
                            trace_target = str(t_path.relative_to(wt_root))
                        except ValueError:
                            trace_target = None  # can't resolve — discard
                    else:
                        trace_target = None
            candidate = (repo_root / trace_target).resolve() if trace_target else None
            if candidate and pre_existing and candidate not in pre_existing:
                trace_target = None  # agent invented this file — ignore it
        if not trace_artifact:
            trace_artifact = extract_written_content(res.trace_path)
        if not trace_target:
            trace_target = extract_target_file(res.trace_path, repo_root=repo_root,
                                               allowed_paths=pre_existing or None)
        if trace_artifact:
            artifact = trace_artifact
        if log_dir:
            src = "structured" if trace_target else "heuristic"
            tgt = trace_target or "(none — will use agent_write_to)"
            (log_dir / f"{task.get('id', 'task')}.{res.variant}.target.log").write_text(
                f"target_file: {tgt}\nsource: {src}\n"
            )

    if trace_target:
        dest_for_checks: Path | None = repo_root / trace_target
    elif res.agent_target_path:
        dest_for_checks = Path(res.agent_target_path)
    else:
        dest_for_checks = None

    written: list[tuple[Path, str | None]] = []  # (path, original_content)
    try:
        for chk in cfg.post_checks:
            if chk.write_to or dest_for_checks:
                dest = dest_for_checks if dest_for_checks else repo_root / _fmt(chk.write_to, task)
                dest.parent.mkdir(parents=True, exist_ok=True)
                original = dest.read_text() if dest.exists() else None
                # If the agent already wrote/edited this file on disk, use it directly.
                # Otherwise merge: append artifact to existing content if needed.
                if original and original == artifact:
                    pass  # agent already wrote exactly this — no-op, but track for restore
                elif original and original.strip() not in artifact:
                    merged = original.rstrip() + "\n\n" + artifact.lstrip()
                    dest.write_text(merged)
                else:
                    dest.write_text(artifact)
                written.append((dest, original))
            # Expose target_basename for command placeholders
            if dest_for_checks:
                target_basename = dest_for_checks.name
            elif chk.write_to:
                target_basename = Path(_fmt(chk.write_to, task)).name
            else:
                target_basename = ""
            # Skip if the command references {target_basename} but we have none
            if not target_basename and any("{target_basename}" in arg for arg in chk.command):
                if log_dir:
                    log_name = f"{task.get('id', 'task')}.{res.variant}.{chk.name}.log"
                    (log_dir / log_name).write_text("skipped: no target file resolved\n")
                continue
            ctx = {**task, "target_basename": target_basename}
            cwd = repo_root / _fmt(chk.cwd, task)
            try:
                command = [_fmt(arg, ctx) for arg in chk.command]
                proc = subprocess.run(
                    command, cwd=cwd, capture_output=True, text=True,
                    timeout=chk.timeout,
                )
                res.post_checks[chk.name] = proc.returncode == 0
                if log_dir:
                    log_name = f"{task.get('id', 'task')}.{res.variant}.{chk.name}.log"
                    _write_log(log_dir / log_name, command, cwd, proc.returncode, proc.stdout, proc.stderr)
            except subprocess.TimeoutExpired:
                res.post_checks[chk.name] = False
                res.error = (res.error + f" | {chk.name} post-check timeout").strip(" |")
    finally:
        for dest, original in written:
            if original is not None:
                dest.write_text(original)
            else:
                dest.unlink(missing_ok=True)



# --------------------------------------------------------------------------- #

def run_judge(cfg: Config, backend: Backend, task: dict, res: RunResult,
              skill_text: str = "") -> None:
    if not cfg.judge_rubric or res.error or not res.artifact_path:
        return
    rubric = cfg.judge_rubric.replace("{skill}", skill_text) if skill_text else cfg.judge_rubric
    artifact = Path(res.artifact_path).read_text()

    # If the agent edited an existing file, extract only the new code so the
    # judge scores the agent's contribution, not pre-existing tests.
    if res.original_artifact_path and Path(res.original_artifact_path).exists():
        original = Path(res.original_artifact_path).read_text()
        new_lines = [
            line for line in artifact.splitlines()
            if line not in original.splitlines()
        ]
        new_code = "\n".join(new_lines).strip()
        if new_code:
            artifact = new_code

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


def evaluate(cfg: Config, backend: Backend, *, do_checks: bool,
             do_judge: bool, do_check_rules: bool, out_dir: Path,
             parallel: int = 1, progress=print) -> list[RunResult]:
    skill_text = load_skill_text(cfg)
    tasks = load_tasks(cfg)
    results: list[RunResult] = []

    rules: list[dict] = []
    if do_check_rules:
        progress("Extracting rules from skill …")
        rules = extract_rules(cfg, backend, skill_text)
        progress(f"  {len(rules)} rule(s) extracted: {[r['name'] for r in rules]}")

    # One run per task per variant — no repeats
    runs: list[tuple[dict, str]] = [
        (task, variant)
        for task in tasks
        for variant in ("control", "skill")
    ]

    if parallel <= 1:
        for task, variant in runs:
            res = _run_one(cfg, backend, skill_text, task, variant,
                           out_dir, do_checks, do_judge, do_check_rules, rules, progress)
            results.append(res)
    else:
        # One git worktree per parallel slot. Each slot is owned by exactly one
        # running task at a time — slots are handed out via a queue so two tasks
        # never share a worktree concurrently.
        git_root = _find_git_root(cfg.repo_root_path)
        worktrees: list[Path] = []
        wt_queue: queue.Queue = queue.Queue()
        if git_root:
            n_wt = min(parallel, len(runs))
            progress(f"  Setting up {n_wt} git worktrees for parallel isolation …")
            for i in range(n_wt):
                wt = _setup_worktree(git_root, f"skilleval-worker-{i}")
                worktrees.append(wt)
                wt_queue.put(wt)
                progress(f"  worktree {i}: {wt}")

        def _run_with_wt(task: dict, variant: str) -> RunResult:
            if worktrees:
                wt_path = wt_queue.get()
                try:
                    run_cfg = _cfg_with_repo_root(cfg, wt_path)
                    slot = worktrees.index(wt_path)
                    return _run_one(run_cfg, backend, skill_text, task, variant,
                                    out_dir, do_checks, do_judge, do_check_rules, rules,
                                    lambda msg, s=slot: progress(f"  [worker-{s}] {msg}"),
                                    original_cfg=cfg)
                finally:
                    wt_queue.put(wt_path)
            else:
                return _run_one(cfg, backend, skill_text, task, variant,
                                out_dir, do_checks, do_judge, do_check_rules, rules, progress)

        try:
            with ThreadPoolExecutor(max_workers=parallel) as pool:
                futures = {
                    pool.submit(_run_with_wt, task, variant): (task["id"], variant)
                    for task, variant in runs
                }
                for f in as_completed(futures):
                    tid, variant = futures[f]
                    try:
                        res = f.result()
                        results.append(res)
                        progress(f"[{tid}] {variant} done")
                    except Exception as exc:
                        progress(f"[{tid}] {variant} FAILED: {exc}")
        finally:
            if git_root and worktrees:
                for wt in worktrees:
                    _teardown_worktree(git_root, wt)
                progress("  Worktrees cleaned up.")

    # Sort results to match the original serial order for deterministic reports
    order = {(task["id"], v): i for i, (task, v) in enumerate(runs)}
    results.sort(key=lambda res: order.get((res.task_id, res.variant), 9999))

    raw = out_dir / "raw_results.jsonl"
    with raw.open("w") as f:
        for r in results:
            f.write(json.dumps(asdict(r)) + "\n")
    return results


def _cfg_with_repo_root(cfg: Config, new_root: Path) -> Config:
    """Return a shallow copy of cfg with repo_root pointing to a worktree."""
    import copy
    c = copy.copy(cfg)
    # Store the absolute path so resolve() returns it directly
    c.repo_root = str(new_root)
    return c


def _run_one(cfg: Config, backend: Backend, skill_text: str,
             task: dict, variant: str, out_dir: Path,
             do_checks: bool, do_judge: bool, do_check_rules: bool,
             rules: list[dict], progress,
             original_cfg: "Config | None" = None) -> RunResult:
    progress(f"[{task['id']}] {variant} …")
    res, snapshot = run_task(cfg, backend, skill_text, task, variant, out_dir)
    # Copy the agent's trace JSONL into post_check_logs/ab/ for per-agent inspection
    if res.trace_path and out_dir:
        import shutil as _shutil
        log_dir = out_dir / "post_check_logs" / "ab"
        log_dir.mkdir(parents=True, exist_ok=True)
        dest = log_dir / f"{task.get('id','task')}.{variant}.trace.jsonl"
        try:
            _shutil.copy2(res.trace_path, dest)
        except Exception:
            pass
    if variant == "skill" and not res.error:
        verify = run_verify(cfg, backend)
        res.skill_verified = verify.verified
        if not verify.verified:
            progress(f"   ! skill verify FAIL (probe response: {verify.probe_response[:80]!r})")
    if do_checks:
        run_checks(cfg, task, res, snapshot=snapshot)
    if cfg.post_checks:
        # Post-checks must run against the original repo (with node_modules, full install),
        # not the bare worktree which only has git-tracked files.
        post_cfg = original_cfg if original_cfg is not None else cfg
        wt_root = cfg.repo_root_path if original_cfg is not None else None
        run_post_checks(post_cfg, task, res, out_dir, snapshot=snapshot, worktree_root=wt_root)
    restore_snapshot(snapshot)
    if do_judge:
        run_judge(cfg, backend, task, res, skill_text)
    if do_check_rules and rules:
        run_rule_checks(cfg, backend, res, rules)
    if res.error:
        progress(f"   ! error: {res.error}")
    return res
