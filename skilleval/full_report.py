"""Full synthesis report: runs all evaluation commands and produces a single report."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from .backends import Backend
from .config import Config
from .audit import run_audit, write_audit_report, AuditResult, RuleResult
from .reviewer import run_review, write_review_report, ReviewResult
from .verify import run_verify, VerifyResult
from .core import evaluate, RunResult
from .report import write_markdown
from .plugin_eval import run_plugin_eval, write_plugin_eval_report, PluginEvalResult
from .cache import PhaseCache, find_latest_run, skill_hash as compute_skill_hash


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class FullReport:
    skill_name: str
    audit: Optional[AuditResult] = None
    review: Optional[ReviewResult] = None
    verify: Optional[VerifyResult] = None
    ab_results: list[RunResult] = field(default_factory=list)
    plugin_eval: Optional[PluginEvalResult] = None
    health_score: float = 0.0
    health_dimensions: list[dict] = field(default_factory=list)
    top_findings: list[dict] = field(default_factory=list)   # {title, action}
    error: str = ""


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_full(
    cfg: Config,
    backend: Backend,
    evals_dir: Optional[Path],
    plugin_eval_runs: int,
    out_dir: Path,
    progress=print,
    use_cache: bool = True,
    plugin_eval_cache_ttl_min: int = 60,
    parallel: int = 1,
) -> FullReport:
    report = FullReport(skill_name=cfg.name)

    # --- incremental cache setup ---
    shash = compute_skill_hash(cfg)
    cache = PhaseCache(shash)
    if use_cache:
        prior_dir = find_latest_run(cfg._config_dir)
        if prior_dir and prior_dir != out_dir:
            cache = PhaseCache.load_from(prior_dir, shash)

    progress("\n[1/5] Best-practices audit …")
    if use_cache and cache.has("audit"):
        progress("  (cached)")
        report.audit = _restore_audit(cfg.name, cache.load("audit"))
        write_audit_report(report.audit, out_dir)
    else:
        try:
            report.audit = run_audit(cfg, backend)
            write_audit_report(report.audit, out_dir)
            if report.audit.error:
                progress(f"  ! audit error: {report.audit.error}")
            else:
                progress(f"  score: {report.audit.overall_score}%")
                cache.save("audit", report.audit)
        except Exception as exc:
            progress(f"  ! audit failed: {exc}")
            report.audit = AuditResult(skill_name=cfg.name, error=str(exc))

    progress("\n[2/5] Skill review …")
    if use_cache and cache.has("review"):
        progress("  (cached)")
        report.review = _restore_review(cfg.name, cache.load("review"))
        write_review_report(report.review, out_dir)
    else:
        try:
            report.review = run_review(cfg, backend)
            write_review_report(report.review, out_dir)
            if report.review.error:
                progress(f"  ! review error: {report.review.error}")
            else:
                progress(f"  rating: {report.review.rating}")
                cache.save("review", report.review)
        except Exception as exc:
            progress(f"  ! review failed: {exc}")
            report.review = ReviewResult(skill_name=cfg.name, report_text="", error=str(exc))

    progress("\n[3/5] Skill verify …")
    if use_cache and cache.has("verify") and cache.load("verify").get("verified"):
        progress("  (cached — verified)")
        report.verify = _restore_verify(cfg.name, cache.load("verify"))
    else:
        try:
            report.verify = run_verify(cfg, backend)
            status = "verified" if report.verify.verified else "NOT verified"
            progress(f"  {status}")
            if report.verify.verified:
                cache.save("verify", report.verify)
        except Exception as exc:
            progress(f"  ! verify failed: {exc}")
            report.verify = VerifyResult(skill_name=cfg.name, verified=False, error=str(exc))

    progress("\n[4/5] A/B evaluation (judge + check-rules) …")
    try:
        report.ab_results = evaluate(
            cfg, backend,
            do_checks=False,
            do_judge=True,
            do_check_rules=True,
            out_dir=out_dir,
            parallel=parallel,
            progress=progress,
        )
        write_markdown(report.ab_results, out_dir / "report.md", name=cfg.name)
    except Exception as exc:
        progress(f"  ! A/B eval failed: {exc}")

    progress("\n[5/5] Plugin eval …")
    if evals_dir and evals_dir.exists():
        # Check whether a cached plugin_eval result is still fresh enough.
        pe_cached: Optional[PluginEvalResult] = None
        if use_cache and cache.has("plugin_eval"):
            age_min = cache.age_seconds() / 60
            if age_min <= plugin_eval_cache_ttl_min:
                progress(f"  (cached — {age_min:.0f} min old, ttl {plugin_eval_cache_ttl_min} min)")
                pe_cached = _restore_plugin_eval(cfg.name, cache.load("plugin_eval"))
            else:
                progress(f"  (cache stale — {age_min:.0f} min old, re-running)")
        try:
            if pe_cached is not None:
                report.plugin_eval = pe_cached
                write_plugin_eval_report(report.plugin_eval, out_dir)
            else:
                report.plugin_eval = run_plugin_eval(cfg, evals_dir, runs=plugin_eval_runs,
                                                      out_dir=out_dir, concurrency=parallel)
                write_plugin_eval_report(report.plugin_eval, out_dir)
                if report.plugin_eval.error:
                    progress(f"  ! plugin eval error: {report.plugin_eval.error}")
                else:
                    progress(
                        f"  {report.plugin_eval.cases_passed}/{report.plugin_eval.cases_total} "
                        f"cases passed  ({report.plugin_eval.overall_pass_rate * 100:.0f}%)"
                    )
                    cache.save("plugin_eval", report.plugin_eval)
        except Exception as exc:
            progress(f"  ! plugin eval failed: {exc}")
            report.plugin_eval = PluginEvalResult(skill_name=cfg.name, raw={},
                                                   report_text="", error=str(exc))
    else:
        progress("  skipped (no evals/ directory found)")

    _compute_health(report)
    _compute_top_findings(cfg, backend, report)

    if use_cache:
        cache.flush(out_dir)

    return report


# ---------------------------------------------------------------------------
# Cache restore helpers
# ---------------------------------------------------------------------------

def _restore_audit(skill_name: str, data: dict) -> AuditResult:
    rules = [RuleResult(**r) for r in data.get("rules", [])]
    return AuditResult(
        skill_name=skill_name,
        skill_files=data.get("skill_files", []),
        rules=rules,
        overall_score=data.get("overall_score", 0.0),
        error=data.get("error", ""),
    )


def _restore_review(skill_name: str, data: dict) -> ReviewResult:
    return ReviewResult(
        skill_name=skill_name,
        report_text=data.get("report_text", ""),
        rating=data.get("rating", ""),
        input_tokens=data.get("input_tokens", 0),
        output_tokens=data.get("output_tokens", 0),
        cost_usd=data.get("cost_usd", 0.0),
        model=data.get("model", ""),
        error=data.get("error", ""),
        issues=data.get("issues", {}),
    )


def _restore_verify(skill_name: str, data: dict) -> VerifyResult:
    return VerifyResult(
        skill_name=skill_name,
        verified=data.get("verified", False),
        probe_response=data.get("probe_response", ""),
        input_tokens=data.get("input_tokens", 0),
        output_tokens=data.get("output_tokens", 0),
        cost_usd=data.get("cost_usd", 0.0),
        model=data.get("model", ""),
        error=data.get("error", ""),
    )


def _restore_plugin_eval(skill_name: str, data: dict) -> PluginEvalResult:
    return PluginEvalResult(
        skill_name=skill_name,
        raw=data.get("raw", {}),
        report_text=data.get("report_text", ""),
        overall_score=data.get("overall_score", 0.0),
        overall_pass_rate=data.get("overall_pass_rate", 0.0),
        cases_total=data.get("cases_total", 0),
        cases_passed=data.get("cases_passed", 0),
        cost_usd=data.get("cost_usd", 0.0),
        duration_seconds=data.get("duration_seconds", 0.0),
        claude_version=data.get("claude_version", ""),
        ablation=data.get("ablation", ""),
        traces=data.get("traces", {}),
        error=data.get("error", ""),
    )


# ---------------------------------------------------------------------------
# Health score
# ---------------------------------------------------------------------------

_WEIGHTS = {
    "audit":        2,
    "review":       2,
    "judge":        2,
    "rule_checks":  1,
    "plugin_eval":  2,
    "verify":       1,
}

def _compute_health(report: FullReport) -> None:
    dims: list[dict] = []

    if report.audit and not report.audit.error:
        dims.append({"key": "audit", "label": "Best-practices audit",
                     "score": report.audit.overall_score,
                     "weight": _WEIGHTS["audit"],
                     "detail": f"{report.audit.overall_score}%"})

    if report.review and not report.review.error:
        rating_scores = {
            "Pass": 100.0,
            "Needs Improvement": 55.0,
            "Needs Major Revision": 20.0,
        }
        score = rating_scores.get(report.review.rating, 50.0)
        dims.append({"key": "review", "label": "Skill review",
                     "score": score,
                     "weight": _WEIGHTS["review"],
                     "detail": report.review.rating or "n/a"})

    if report.verify:
        score = 100.0 if report.verify.verified else 0.0
        dims.append({"key": "verify", "label": "Skill verify",
                     "score": score,
                     "weight": _WEIGHTS["verify"],
                     "detail": "verified" if report.verify.verified else "not verified"})

    skill_runs = [r for r in report.ab_results if r.variant == "skill" and not r.error]
    if skill_runs:
        scored = [r for r in skill_runs if r.judge_score is not None]
        if scored:
            avg_judge = sum(r.judge_score for r in scored) / len(scored)  # type: ignore[arg-type]
            dims.append({"key": "judge", "label": "A/B judge score (skill)",
                         "score": avg_judge * 10,
                         "weight": _WEIGHTS["judge"],
                         "detail": f"{avg_judge:.1f}/10"})

        rule_vals = []
        for r in skill_runs:
            rule_vals.extend(r.rule_checks.values())
        if rule_vals:
            pass_rate = sum(rule_vals) / len(rule_vals) * 100
            dims.append({"key": "rule_checks", "label": "Rule compliance (skill)",
                         "score": pass_rate,
                         "weight": _WEIGHTS["rule_checks"],
                         "detail": f"{pass_rate:.0f}% rules passed"})

    if report.plugin_eval and not report.plugin_eval.error and report.plugin_eval.cases_total:
        score = report.plugin_eval.overall_pass_rate * 100
        dims.append({"key": "plugin_eval", "label": "Plugin eval pass rate",
                     "score": score,
                     "weight": _WEIGHTS["plugin_eval"],
                     "detail": (
                         f"{report.plugin_eval.cases_passed}/"
                         f"{report.plugin_eval.cases_total} cases passed"
                     )})

    report.health_dimensions = dims
    if not dims:
        report.health_score = 0.0
        return
    total_weight = sum(d["weight"] for d in dims)
    weighted_sum = sum(d["score"] * d["weight"] for d in dims)
    report.health_score = round(weighted_sum / total_weight, 1)


# ---------------------------------------------------------------------------
# LLM synthesis of top findings
# ---------------------------------------------------------------------------

_SYNTHESIS_SYSTEM = """\
You are a skill quality analyst. Given evaluation results from multiple dimensions,
identify the top 3 most impactful actionable improvements.

Prioritize findings that appear across multiple dimensions or that have the highest
impact on skill effectiveness. Be concrete and specific — not "improve conciseness"
but "SKILL.md is 600+ lines; move the reference tables to WRITING-SPECS.md".

Return ONLY a JSON array — no prose, no markdown fences:
[
  {"title": "<short problem label>", "action": "<one sentence concrete action>"},
  {"title": "...", "action": "..."},
  {"title": "...", "action": "..."}
]
"""


def _compute_top_findings(cfg: Config, backend: Backend, report: FullReport) -> None:
    summary: dict = {
        "health_score": report.health_score,
        "dimensions": [
            {"label": d["label"], "score": d["score"], "detail": d["detail"]}
            for d in report.health_dimensions
        ],
    }

    if report.audit and not report.audit.error:
        summary["audit_failures"] = [
            {"rule": r.title, "rationale": r.rationale}
            for r in report.audit.rules if r.status == "fail"
        ]

    if report.review and not report.review.error:
        summary["review_issues"] = report.review.issues

    if report.verify and not report.verify.verified:
        summary["verify"] = "skill not verified in context"

    skill_runs = [r for r in report.ab_results if r.variant == "skill" and not r.error]
    if skill_runs:
        failing_rules: dict[str, int] = {}
        for r in skill_runs:
            for k, v in r.rule_checks.items():
                if not v:
                    failing_rules[k] = failing_rules.get(k, 0) + 1
        if failing_rules:
            summary["rule_check_failures"] = failing_rules
        friction_all = [f for r in skill_runs for f in r.friction]
        if friction_all:
            summary["friction_signals"] = friction_all[:10]

    if (report.plugin_eval and not report.plugin_eval.error
            and report.plugin_eval.cases_total):
        failed_cases = [
            c.get("name") or c.get("id", "unknown")
            for c in (report.plugin_eval.raw.get("cases") or [])
            if not c.get("passed")
        ]
        if failed_cases:
            summary["plugin_eval_failed_cases"] = failed_cases

    prompt = f"EVALUATION SUMMARY:\n{json.dumps(summary, indent=2)}"
    try:
        c = backend.complete(
            prompt, _SYNTHESIS_SYSTEM,
            model=cfg.model, no_tools=True, timeout=60,
        )
        m = re.search(r"\[.*\]", c.text, re.DOTALL)
        report.top_findings = json.loads(m.group(0) if m else c.text)
    except Exception:  # noqa: BLE001
        report.top_findings = []


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------

def write_full_report(report: FullReport, out_dir: Path) -> Path:
    lines: list[str] = []
    lines.append(f"# Full evaluation report — {report.skill_name}\n")

    # --- Health score ---
    score_bar = _score_bar(report.health_score)
    lines.append(f"## Health score: {report.health_score}%  {score_bar}\n")

    lines.append("| Dimension | Score | Detail | Weight |")
    lines.append("|-----------|-------|--------|--------|")
    for d in report.health_dimensions:
        icon = _score_icon(d["score"])
        lines.append(
            f"| {d['label']} | {icon} {d['score']:.0f}% | {d['detail']} | {d['weight']}× |"
        )
    lines.append("")

    # --- Token / cost metrics ---
    lines.append("## Token & cost metrics\n")
    _append_token_section(lines, report)

    # --- Per-command results table ---
    lines.append("## Per-command results\n")
    lines.append("| Command | Output | Status |")
    lines.append("|---------|--------|--------|")

    if report.audit:
        status = "❌ error" if report.audit.error else _score_icon(report.audit.overall_score)
        val = report.audit.error or f"{report.audit.overall_score}%"
        lines.append(f"| Best-practices audit | {val} | {status} |")

    if report.review:
        status = "❌ error" if report.review.error else _rating_icon(report.review.rating)
        val = report.review.error or report.review.rating or "n/a"
        lines.append(f"| Skill review | {val} | {status} |")

    if report.verify:
        status = "✅" if report.verify.verified else "❌"
        val = "verified" if report.verify.verified else "not verified"
        lines.append(f"| Skill verify | {val} | {status} |")

    skill_runs = [r for r in report.ab_results if r.variant == "skill" and not r.error]
    ctrl_runs  = [r for r in report.ab_results if r.variant == "control" and not r.error]
    if report.ab_results:
        scored_s = [r for r in skill_runs if r.judge_score is not None]
        scored_c = [r for r in ctrl_runs  if r.judge_score is not None]
        if scored_s and scored_c:
            avg_s = sum(r.judge_score for r in scored_s) / len(scored_s)  # type: ignore[arg-type]
            avg_c = sum(r.judge_score for r in scored_c) / len(scored_c)  # type: ignore[arg-type]
            delta = avg_s - avg_c
            delta_str = f"+{delta:.1f}" if delta >= 0 else f"{delta:.1f}"
            val = f"skill {avg_s:.1f}/10 vs control {avg_c:.1f}/10 (Δ{delta_str})"
            status = "✅" if delta >= 0 else "⚠️"
            lines.append(f"| A/B judge score | {val} | {status} |")

        if any(r.rule_checks for r in skill_runs):
            rule_vals = [v for r in skill_runs for v in r.rule_checks.values()]
            pct = sum(rule_vals) / len(rule_vals) * 100
            lines.append(
                f"| Rule compliance | {pct:.0f}% ({sum(rule_vals)}/{len(rule_vals)}) | "
                f"{_score_icon(pct)} |"
            )

    if report.plugin_eval:
        if report.plugin_eval.error:
            lines.append(f"| Plugin eval | {report.plugin_eval.error} | ❌ error |")
        else:
            pct = report.plugin_eval.overall_pass_rate * 100
            val = (
                f"{report.plugin_eval.cases_passed}/{report.plugin_eval.cases_total} "
                f"cases passed ({pct:.0f}%)"
            )
            lines.append(f"| Plugin eval | {val} | {_score_icon(pct)} |")
    else:
        lines.append("| Plugin eval | skipped (no evals/ dir) | — |")

    lines.append("")

    # --- Top actionable findings ---
    lines.append("## Top actionable findings\n")
    if report.top_findings:
        for i, f in enumerate(report.top_findings[:3], 1):
            lines.append(f"### {i}. {f.get('title', 'Finding')}")
            lines.append(f"{f.get('action', '')}\n")
    else:
        lines.append("_No findings synthesized._\n")

    # --- Sub-report links ---
    lines.append("## Sub-reports\n")
    sub_reports = [
        ("best_practices_audit.md", "Best-practices audit"),
        ("skill_review.md", "Skill review"),
        ("report.md", "A/B evaluation"),
        ("plugin_eval_report.md", "Plugin eval"),
    ]
    for filename, label in sub_reports:
        if (out_dir / filename).exists():
            lines.append(f"- [{label}]({filename})")
    lines.append("")

    path = out_dir / "full_report.md"
    path.write_text("\n".join(lines))
    return path


def print_full_summary(report: FullReport) -> None:
    print(f"\n{'='*60}")
    print(f"FULL REPORT — {report.skill_name}")
    print(f"Health score: {report.health_score}%  {_score_bar(report.health_score)}")
    print(f"{'='*60}")
    for d in report.health_dimensions:
        print(f"  {_score_icon(d['score'])} {d['label']}: {d['detail']}")
    if report.top_findings:
        print("\nTop findings:")
        for i, f in enumerate(report.top_findings[:3], 1):
            print(f"  {i}. {f.get('title')}: {f.get('action')}")


# ---------------------------------------------------------------------------
# Token/cost helpers
# ---------------------------------------------------------------------------

def _append_token_section(lines: list[str], report: FullReport) -> None:
    ctrl  = [r for r in report.ab_results if r.variant == "control" and not r.error]
    skill = [r for r in report.ab_results if r.variant == "skill"   and not r.error]

    # --- A/B token breakdown (control vs skill) ---
    if ctrl or skill:
        def avg(lst, attr):
            vals = [getattr(r, attr) for r in lst]
            return sum(vals) / len(vals) if vals else 0.0

        lines.append("### A/B eval — token usage (control vs skill)\n")
        lines.append("| Metric | Control | Skill | Delta |")
        lines.append("|--------|---------|-------|-------|")
        for label, attr in [
            ("Avg input tokens", "input_tokens"),
            ("Avg output tokens", "output_tokens"),
            ("Avg cached tokens", "cache_read_tokens"),
            ("Avg cost (USD)", "cost_usd"),
            ("Avg latency (s)", "latency_s"),
        ]:
            c_val = avg(ctrl, attr)
            s_val = avg(skill, attr)
            delta = s_val - c_val
            sign = "+" if delta >= 0 else ""
            if attr == "cost_usd":
                lines.append(
                    f"| {label} | ${c_val:.4f} | ${s_val:.4f} | {sign}{delta:.4f} |"
                )
            elif attr == "latency_s":
                lines.append(
                    f"| {label} | {c_val:.1f}s | {s_val:.1f}s | {sign}{delta:.1f}s |"
                )
            else:
                lines.append(
                    f"| {label} | {c_val:,.0f} | {s_val:,.0f} | {sign}{delta:,.0f} |"
                )
        lines.append("")

    # --- Plugin eval metrics ---
    if report.plugin_eval and not report.plugin_eval.error:
        pe = report.plugin_eval
        lines.append("### Plugin eval — metrics\n")
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        lines.append(f"| Cases passed | {pe.cases_passed}/{pe.cases_total} |")
        lines.append(f"| Overall pass rate | {pe.overall_pass_rate * 100:.0f}% |")
        lines.append(f"| Overall score | {pe.overall_score:.2f} |")
        if pe.ablation == "with-without":
            delta = (pe.raw.get("aggregates") or {}).get("meanDelta")
            if delta is not None:
                lines.append(f"| Mean delta (with − without) | {delta:+.2f} |")
        lines.append(f"| Duration | {pe.duration_seconds:.0f}s |")
        lines.append(f"| Cost (USD) | ${pe.cost_usd:.4f} |")
        # Per-case cost if available
        cases = pe.raw.get("cases") or []
        case_costs = [(c.get("name", "?"), c.get("costUsd", 0)) for c in cases if c.get("costUsd")]
        if case_costs:
            lines.append("")
            lines.append("**Per-case cost:**\n")
            lines.append("| Case | Cost (USD) |")
            lines.append("|------|-----------|")
            for name, cost in case_costs:
                lines.append(f"| {name} | ${cost:.4f} |")
        lines.append("")


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _score_icon(score: float) -> str:
    if score >= 80:
        return "✅"
    if score >= 50:
        return "⚠️"
    return "❌"


def _score_bar(score: float) -> str:
    filled = round(score / 10)
    return "█" * filled + "░" * (10 - filled)


def _rating_icon(rating: str) -> str:
    return {"Pass": "✅", "Needs Improvement": "⚠️", "Needs Major Revision": "❌"}.get(
        rating, "—"
    )
