"""Integration with `claude plugin eval`.

Runs the CLI's built-in plugin evaluator against the configured skill
directory, using the eval cases stored inside this repo under
<config_dir>/evals/, and converts the JSON result into a Markdown report.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean

from .config import Config, CheckCommand
from .core import _write_log
from .trace_analysis import analyse_trace, render_friction_section, TraceAnalysis, extract_written_content, extract_target_file, extract_structured_response


@dataclass
class PluginEvalResult:
    skill_name: str
    raw: dict = field(default_factory=dict)
    report_text: str = ""
    overall_score: float = 0.0
    overall_pass_rate: float = 0.0
    cases_total: int = 0
    cases_passed: int = 0
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    claude_version: str = ""
    ablation: str = ""
    traces: dict = field(default_factory=dict)   # case_name -> {"with": [...], "without": [...]}
    post_checks: dict = field(default_factory=dict)  # case_name -> {check_name: bool}
    error: str = ""


# --------------------------------------------------------------------------- #
# Post-check helpers
# --------------------------------------------------------------------------- #

def _fmt(template: str, values: dict) -> str:
    try:
        return template.format(**values)
    except (KeyError, IndexError):
        return template


def _run_post_checks_for_case(
    cfg: Config, case_name: str, artifact_text: str, out_dir: Path | None = None,
    target_file: str | None = None,
) -> dict[str, bool]:
    repo_root = cfg.repo_root_path
    ctx = {"id": case_name, "case_name": case_name}
    results: dict[str, bool] = {}

    log_dir: Path | None = None
    if out_dir:
        log_dir = out_dir / "post_check_logs"
        log_dir.mkdir(parents=True, exist_ok=True)

    written: list[tuple[Path, str | None]] = []  # (path, original_content)
    try:
        for chk in cfg.post_checks:
            if chk.write_to:
                # Prefer target_file detected from trace; fall back to write_to template
                if target_file:
                    dest = repo_root / target_file
                else:
                    dest = repo_root / _fmt(chk.write_to, ctx)
                dest.parent.mkdir(parents=True, exist_ok=True)
                original = dest.read_text() if dest.exists() else None
                # Merge: if the target exists and the artifact doesn't already contain
                # the original content, append the artifact to the original.
                # This handles the case where the agent returned only new code without
                # reading and merging the existing file.
                if original and original.strip() not in artifact_text:
                    merged = original.rstrip() + "\n\n" + artifact_text.lstrip()
                else:
                    merged = artifact_text
                dest.write_text(merged)
                written.append((dest, original))
            cwd = repo_root / _fmt(chk.cwd, ctx)
            try:
                # Expose target_file name (without path) for command placeholders.
                # If no target_file was detected, fall back to {case_name}.cy.tsx so
                # commands using {target_basename} still produce a usable value.
                if target_file:
                    ctx_cmd = {**ctx, "target_file": target_file,
                               "target_basename": Path(target_file).name}
                else:
                    ctx_cmd = {**ctx, "target_file": _fmt(chk.write_to or "", ctx),
                               "target_basename": f"{case_name}.cy.tsx"}
                command = [_fmt(arg, ctx_cmd) for arg in chk.command]
                proc = subprocess.run(
                    command, cwd=cwd, capture_output=True, text=True,
                    timeout=chk.timeout,
                )
                results[chk.name] = proc.returncode == 0
                if log_dir:
                    log_name = f"{case_name}.{chk.name}.log"
                    _write_log(log_dir / log_name, command, cwd, proc.returncode, proc.stdout, proc.stderr)
            except subprocess.TimeoutExpired:
                results[chk.name] = False
    finally:
        for dest, original in written:
            if original is not None:
                dest.write_text(original)
            else:
                dest.unlink(missing_ok=True)
    return results


_POST_CHECK_PROMPT_SUFFIX = """\
At the end of your response, output the result in this exact format:
FILE: <relative/path/to/file>
```
<complete file content here>
```
Use the actual file path you are targeting (relative to the repo root)."""


def _patch_case_prompts(evals_dir: Path) -> None:
    """Append the structured-response suffix to every case.yaml prompt under evals_dir."""
    import re as _re
    suffix = _POST_CHECK_PROMPT_SUFFIX
    for case_yaml in evals_dir.rglob("case.yaml"):
        try:
            text = case_yaml.read_text()
            def _append(m: "_re.Match") -> str:
                block = m.group(0).rstrip()
                return block + "\n" + "\n".join("    " + line for line in suffix.splitlines()) + "\n"
            patched = _re.sub(
                r"( {2}prompt: \|(?:\n(?:    [^\n]*))+)",
                _append,
                text,
            )
            if patched != text:
                case_yaml.write_text(patched)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

def run_plugin_eval(cfg: Config, evals_dir: Path, runs: int = 1,
                    out_dir: Path | None = None) -> PluginEvalResult:
    skill_dir = cfg.resolve(cfg.skill_base_dir)
    result = PluginEvalResult(skill_name=cfg.name)

    if not skill_dir.exists():
        result.error = f"Skill directory not found: {skill_dir}"
        return result

    if not evals_dir.exists():
        result.error = f"Evals directory not found: {evals_dir}"
        return result

    skill_dir_resolved = skill_dir.resolve()
    evals_dir_resolved = evals_dir.resolve()
    repo_root = cfg.repo_root_path.resolve()

    # Run claude plugin eval with CWD = repo_root so the agent sandbox starts
    # there and can read project files without symlinks or absolute paths.
    # The skill target must be relative to repo_root; --eval-dir relative to skill_dir.
    try:
        skill_rel = skill_dir_resolved.relative_to(repo_root)
    except ValueError:
        # skill_dir is outside repo_root — fall back to running from skill_dir
        skill_rel = Path(".")
        repo_root = skill_dir_resolved

    # --eval-dir must be relative to the skill dir (plugin root), but must NOT
    # be inside the plugin's skills/ directory.  Place the tmp copy at repo_root
    # level so it sits outside any skills/ tree.
    try:
        eval_dir_rel = evals_dir_resolved.relative_to(skill_dir_resolved)
        tmp_copy = None
    except ValueError:
        # Copy evals dir into the plugin root so --eval-dir can reference it.
        # scaffold.sh lives inside each case dir, so copying the evals dir is enough.
        tmp_copy = skill_dir_resolved / "skilleval-evals-tmp"
        if tmp_copy.exists():
            shutil.rmtree(tmp_copy)
        shutil.copytree(
            str(evals_dir_resolved), str(tmp_copy),
            ignore=shutil.ignore_patterns("_archived", "_archived/*"),
        )

        # --eval-dir is relative to skill_dir (the plugin root)
        eval_dir_rel = Path("skilleval-evals-tmp")

    # Patch case.yaml prompts in the temp copy to request structured FILE: responses
    if tmp_copy and cfg.post_checks:
        _patch_case_prompts(tmp_copy)

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp_path = tmp.name

    cmd = [
        "claude", "plugin", "eval", str(skill_rel),
        "--eval-dir", str(eval_dir_rel),
        "--json", tmp_path,
        "--no-publish",
        "--trust-plugin",
        "--scaffold",
        "--runs", str(runs),
        "--concurrency", "1",
        "--keep-temp",   # preserve trace files for friction analysis
    ]
    if cfg.model:
        cmd += ["--model", cfg.model]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=3600,
            cwd=str(repo_root),
        )
        raw_text = Path(tmp_path).read_text()
        if not raw_text.strip():
            result.error = (
                f"claude plugin eval produced no JSON output "
                f"(exit {proc.returncode}): {proc.stderr.strip()[:400]}"
            )
            return result

        data = json.loads(raw_text)
        result.raw = data
        agg = data.get("aggregates") or {}
        suite = data.get("suite") or {}
        result.overall_score = float(agg.get("overallScore", 0))
        result.overall_pass_rate = float(agg.get("overallPassRate", 0))
        result.cases_total = int(agg.get("casesTotal", 0))
        result.cases_passed = int(agg.get("casesPassed", 0))
        result.cost_usd = float(data.get("costUsd") or 0)
        result.duration_seconds = float(data.get("durationSeconds") or 0)
        result.claude_version = data.get("claudeVersion") or ""
        result.ablation = suite.get("ablation") or "none"

        # Collect trace analyses per case per arm for friction reporting
        result.traces = {}   # case_name -> {"with": [...], "without": [...]}
        for case in data.get("cases", []):
            cname = case.get("name", "")
            result.traces[cname] = {"with": [], "without": []}
            for arm_name, runs in (case.get("arms") or {}).items():
                for run in runs:
                    tp = run.get("tracePath")
                    if tp:
                        result.traces[cname][arm_name].append(analyse_trace(tp))

        # Run post-checks per case using the artifact from the "with" arm trace
        if cfg.post_checks:
            log_dir: Path | None = None
            if out_dir:
                log_dir = out_dir / "post_check_logs"
                log_dir.mkdir(parents=True, exist_ok=True)

            for case in data.get("cases", []):
                cname = case.get("name", "")
                with_analyses = result.traces.get(cname, {}).get("with", [])
                if not with_analyses:
                    if log_dir:
                        (log_dir / f"{cname}.skipped.log").write_text("skipped: no trace found for 'with' arm\n")
                    continue
                rep = max(with_analyses, key=lambda a: a.total_turns)

                # Strategy 1: structured FILE: response — gives both target and content directly
                target_file, artifact_text = extract_structured_response(rep.path)
                from_structured = artifact_text is not None
                # Discard target if the file doesn't exist in the repo (agent invented it)
                if target_file and not (cfg.repo_root_path / target_file).exists():
                    target_file = None

                # Strategy 2: Write tool call in trace
                if not artifact_text:
                    artifact_text = extract_written_content(rep.path)

                # Strategy 3: code block in final_text
                if not artifact_text and rep.final_text:
                    import re as _re
                    m = _re.search(r"```[a-zA-Z0-9]*\s*\n(.*?)```", rep.final_text, _re.DOTALL)
                    artifact_text = m.group(1).strip() if m else None

                if not artifact_text:
                    if log_dir:
                        (log_dir / f"{cname}.skipped.log").write_text(
                            f"skipped: no artifact extracted from trace\n"
                            f"final_text preview: {rep.final_text[:300] if rep.final_text else '(empty)'}\n"
                        )
                    continue

                # If not from structured response, detect target from trace heuristics
                if not target_file:
                    target_file = extract_target_file(rep.path, repo_root=cfg.repo_root_path)

                if log_dir:
                    tgt_info = target_file or "(none — will use write_to template)"
                    source = "structured" if from_structured else "heuristic"
                    (log_dir / f"{cname}.target.log").write_text(f"target_file: {tgt_info}\nsource: {source}\n")

                result.post_checks[cname] = _run_post_checks_for_case(
                    cfg, cname, artifact_text, out_dir, target_file=target_file
                )
    except subprocess.TimeoutExpired:
        result.error = "claude plugin eval timed out (3600 s)"
    except Exception as exc:  # noqa: BLE001
        result.error = str(exc)
    finally:
        Path(tmp_path).unlink(missing_ok=True)
        if tmp_copy and tmp_copy.exists():
            shutil.rmtree(tmp_copy, ignore_errors=True)

    return result


# --------------------------------------------------------------------------- #
# Report rendering
# --------------------------------------------------------------------------- #

def _pct(v: float) -> str:
    return f"{v * 100:.0f}%"


def _pass_icon(passed: bool) -> str:
    return "✓" if passed else "✗"


def _md_table(headers: list[str], rows: list[list]) -> str:
    line = "| " + " | ".join(str(h) for h in headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    body = "\n".join(
        "| " + " | ".join(str(c) for c in row) + " |" for row in rows
    )
    return "\n".join([line, sep, body])


def _render_report(result: PluginEvalResult) -> str:
    data = result.raw
    lines: list[str] = []

    # Header
    lines += [f"# Plugin eval report — {result.skill_name}", ""]
    lines += [
        f"_Claude: {result.claude_version} · "
        f"Duration: {result.duration_seconds:.0f}s · "
        f"Cost: ${result.cost_usd:.5f} · "
        f"Cases: {result.cases_passed}/{result.cases_total} passed_",
        "", "---", "",
    ]

    # Overall summary
    lines += ["## Overall", ""]

    # Collect model(s) from traces
    all_models: set[str] = set()
    for case_traces in getattr(result, "traces", {}).values():
        for analyses in case_traces.values():
            for a in analyses:
                if a.model:
                    all_models.add(a.model)
    model_str = ", ".join(sorted(all_models)) or result.claude_version or "unknown"

    overall_rows = [
        ["Model", model_str],
        ["Overall score", f"{result.overall_score:.2f}"],
        ["Pass rate", _pct(result.overall_pass_rate)],
        ["Cases passed", f"{result.cases_passed}/{result.cases_total}"],
    ]
    agg = data.get("aggregates") or {}
    if result.ablation == "with-without" and "meanDelta" in agg:
        overall_rows.append(["Mean delta (with − without)", f"{agg['meanDelta']:+.2f}"])
    lines += [_md_table(["metric", "value"], overall_rows), ""]

    # Case summary table
    cases = data.get("cases", [])
    if cases:
        lines += ["## Case results", ""]
        has_delta = result.ablation == "with-without"
        sum_headers = ["case", "score", "pass rate"]
        if has_delta:
            sum_headers.append("delta (with−without)")
        sum_headers.append("runs")
        sum_rows = []
        for case in cases:
            cagg = case.get("aggregates") or {}
            row = [
                case.get("name", ""),
                f"{cagg.get('score', 0):.2f}",
                _pct(cagg.get("passRate", 0)),
            ]
            if has_delta:
                delta = cagg.get("delta")
                row.append(f"{delta:+.2f}" if delta is not None else "n/a")
            row.append(str(case.get("runsPerCase", "?")))
            # Add avg tokens and cost per arm from traces
            cname = case.get("name", "")
            for arm_name in ("with", "without"):
                arm_analyses = (getattr(result, "traces", {}).get(cname) or {}).get(arm_name) or []
                if arm_analyses:
                    n = len(arm_analyses)
                    avg_in = sum(a.input_tokens for a in arm_analyses) // n
                    avg_out = sum(a.output_tokens for a in arm_analyses) // n
                    avg_cost = sum(a.cost_usd for a in arm_analyses) / n
                    row.append(f"in={avg_in:,} out={avg_out:,} ${avg_cost:.4f}")
                else:
                    row.append("—")
            sum_rows.append(row)
        has_traces = bool(getattr(result, "traces", {}))
        if has_traces:
            sum_headers += ["with: tokens/cost", "without: tokens/cost"]
        lines += [_md_table(sum_headers, sum_rows), ""]

    # Per-case detail
    traces = getattr(result, "traces", {})
    for case in cases:
        name = case.get("name", "")
        lines += [f"## Case: {name}", ""]

        prompt = (case.get("promptMarkdown") or "").strip()
        if prompt:
            lines += [f"**Prompt:** {prompt}", ""]

        graders = case.get("graders", [])
        if graders:
            grader_summary = ", ".join(
                f"{g['name']} ({g.get('type', '?')}, w={g.get('weight', 1)})"
                for g in graders
            )
            lines += [f"**Graders:** {grader_summary}", ""]

        arms = case.get("arms", {})

        for arm_name in ("with", "without"):
            arm_runs = arms.get(arm_name)
            if not arm_runs:
                continue
            label = "with-skill" if arm_name == "with" else "without-skill"
            scores = [r.get("score", 0) for r in arm_runs]
            avg_score = mean(scores) if scores else 0
            all_pass = all(r.get("passed", False) for r in arm_runs)
            pass_label = "PASS" if all_pass else "FAIL"
            lines += [f"### {label} arm (avg score: {avg_score:.2f} — {pass_label})", ""]

            # Aggregate grader results across runs
            grader_results: dict[str, list] = {}
            for run in arm_runs:
                for g in run.get("graders", []):
                    gname = g["name"]
                    if gname not in grader_results:
                        grader_results[gname] = []
                    grader_results[gname].append(g)

            if grader_results:
                g_rows = []
                for gname, runs_g in grader_results.items():
                    scored = [g for g in runs_g if g.get("scored", True)]
                    if scored:
                        pass_count = sum(1 for g in scored if g.get("passed", False))
                        icon = _pass_icon(pass_count == len(scored))
                        explanations = list({g.get("explanation", "") for g in scored if g.get("explanation")})
                        explanation = explanations[0] if explanations else ""
                    else:
                        icon = "—"
                        explanation = "not scored (with-only indicator)"
                    g_rows.append([gname, icon, explanation])
                lines += [_md_table(["grader", "passed", "explanation"], g_rows), ""]

            # Agent response — take final_text from the most detailed trace
            case_traces = traces.get(name, {})
            arm_analyses = case_traces.get(arm_name, [])
            if arm_analyses:
                rep = max(arm_analyses, key=lambda a: a.total_turns)

                # Token / cost summary across all runs for this arm
                total_in = sum(a.input_tokens for a in arm_analyses)
                total_out = sum(a.output_tokens for a in arm_analyses)
                total_think = sum(a.thinking_tokens for a in arm_analyses)
                total_cost = sum(a.cost_usd for a in arm_analyses)
                n = len(arm_analyses)
                token_parts = [
                    f"in={total_in // n:,}",
                    f"out={total_out // n:,}",
                ]
                if total_think:
                    token_parts.append(f"think={total_think // n:,}")
                token_parts.append(f"cost=${total_cost / n:.4f}")
                if n > 1:
                    token_parts.append(f"(avg of {n} runs, total ${total_cost:.4f})")
                arm_models = {a.model for a in arm_analyses if a.model}
                model_label = f" · model={', '.join(sorted(arm_models))}" if arm_models else ""
                lines += [f"_Tokens per run: {' · '.join(token_parts)}{model_label}_", ""]
                if arm_name == "with":
                    # Prefer structured FILE: response for cleaner report output
                    target_file, artifact_text = extract_structured_response(rep.path)
                    if target_file and artifact_text:
                        lines += [f"**Generated file:** `{target_file}`", ""]
                        lines += [f"```\n{artifact_text}\n```", ""]
                    elif rep.final_text:
                        lines += ["**Response:**", ""]
                        lines += [f"```\n{rep.final_text}\n```", ""]
                elif rep.final_text:
                    lines += ["**Response:**", ""]
                    lines += [f"```\n{rep.final_text}\n```", ""]

    # Friction analysis section
    if traces:
        friction_lines: list[str] = []
        for case in cases:
            cname = case.get("name", "")
            case_traces = traces.get(cname, {})
            with_analyses = case_traces.get("with", [])
            without_analyses = case_traces.get("without", [])
            block = render_friction_section(cname, with_analyses, without_analyses)
            friction_lines.extend(block)
        if friction_lines:
            lines += ["## Where the agent struggled", ""]
            lines += ["_Friction signals extracted from agent traces — repeated tool calls, "
                      "permission denials, max-turn hits, and high turn counts indicate "
                      "areas where the skill could be clearer or more directive._", ""]
            lines.extend(friction_lines)

    # Post-checks section
    post_checks = result.post_checks
    if post_checks:
        lines += ["## Post-check verification", ""]
        lines += ["_Shell commands run against the generated artifact to verify it works._", ""]
        pc_rows = []
        for cname, checks in post_checks.items():
            for chk_name, passed in checks.items():
                icon = "✓" if passed else "✗"
                pc_rows.append([cname, chk_name, icon])
        if pc_rows:
            lines += [_md_table(["case", "check", "result"], pc_rows), ""]

    return "\n".join(lines)


def write_plugin_eval_report(result: PluginEvalResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    report_text = _render_report(result) if not result.error else (
        f"# Plugin eval report — {result.skill_name}\n\n**ERROR:** {result.error}\n"
    )
    result.report_text = report_text
    path = out_dir / "plugin_eval_report.md"
    path.write_text(report_text)
    return path


# --------------------------------------------------------------------------- #
# Console summary
# --------------------------------------------------------------------------- #

def print_plugin_eval_summary(result: PluginEvalResult) -> None:
    print("\n=== claude plugin eval ===")
    if result.error:
        print(f"ERROR: {result.error}")
        return
    icon = "✓" if result.overall_pass_rate == 1.0 else ("~" if result.overall_pass_rate > 0 else "✗")
    print(f"Result : {icon}  score {result.overall_score:.2f}  |  "
          f"{_pct(result.overall_pass_rate)} pass rate  |  "
          f"{result.cases_passed}/{result.cases_total} cases passed")
    if result.ablation == "with-without":
        delta = result.raw.get("aggregates", {}).get("meanDelta")
        if delta is not None:
            print(f"Delta  : {delta:+.2f} (with − without skill)")
    print(f"Cost   : ${result.cost_usd:.5f}  |  {result.duration_seconds:.0f}s")
    for case in result.raw.get("cases") or []:
        cagg = case.get("aggregates") or {}
        passed = cagg.get("passRate", 0) == 1.0
        ci = "✓" if passed else "✗"
        print(f"  {ci} {case['name']:40s}  score {cagg.get('score', 0):.2f}")
