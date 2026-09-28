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

from .config import Config
from .trace_analysis import analyse_trace, render_friction_section, TraceAnalysis


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
    error: str = ""


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

def run_plugin_eval(cfg: Config, evals_dir: Path, runs: int = 1) -> PluginEvalResult:
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
    overall_rows = [
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
            sum_rows.append(row)
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
                if rep.final_text:
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
