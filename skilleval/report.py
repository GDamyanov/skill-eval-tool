"""Aggregation and console reporting of evaluation results."""

from __future__ import annotations

from pathlib import Path
from statistics import mean

from .core import RunResult

try:
    from tabulate import tabulate
except ImportError:  # pragma: no cover
    tabulate = None


def _pct(vals: list[bool]) -> str:
    return f"{sum(vals) / len(vals) * 100:.0f}%" if vals else "n/a"


# USD per 1M tokens, used to compute a cache-independent "true" cost so that
# variants are comparable regardless of prompt-cache warm/cold state.
UNCACHED_PRICE = {"input": 3.00, "output": 15.00}


def uncached_cost(r: RunResult) -> float:
    """Cost as if NO prompt caching applied: every input token billed at the
    full input rate. This removes the ordering/warm-cache bias from `cost_usd`
    so control vs skill can be compared fairly."""
    total_in = r.input_tokens + r.cache_read_tokens + r.cache_write_tokens
    return (total_in * UNCACHED_PRICE["input"]
            + r.output_tokens * UNCACHED_PRICE["output"]) / 1_000_000


def summarize(results: list[RunResult]) -> tuple[list[str], list[list]]:
    # Collect the union of check names in stable order.
    check_names: list[str] = []
    for r in results:
        for name in r.checks:
            if name not in check_names:
                check_names.append(name)

    # Collect the union of post-check names in stable order.
    post_check_names: list[str] = []
    for r in results:
        for name in r.post_checks:
            if name not in post_check_names:
                post_check_names.append(name)

    # Collect the union of rule names in stable order.
    rule_names: list[str] = []
    for r in results:
        for name in r.rule_checks:
            if name not in rule_names:
                rule_names.append(name)

    headers = ["variant", "n", "avg in-tok", "avg out-tok", "avg cost $", "avg s"]
    headers += [f"{n} pass" for n in check_names]
    headers += ["skill verified"]
    headers += [f"rule: {n}" for n in rule_names]
    headers += [f"post: {n}" for n in post_check_names]
    headers += ["judge /10"]

    rows: list[list] = []
    for variant in ("control", "skill"):
        subset = [r for r in results if r.variant == variant and not r.error]
        if not subset:
            continue
        total_in = [r.input_tokens + r.cache_read_tokens + r.cache_write_tokens for r in subset]
        row = [
            variant, len(subset),
            round(mean(total_in)),
            round(mean([r.output_tokens for r in subset])),
            f"{mean([uncached_cost(r) for r in subset]):.5f}",
            f"{mean([r.latency_s for r in subset]):.1f}",
        ]
        for name in check_names:
            row.append(_pct([r.checks[name] for r in subset if name in r.checks]))
        verified = [r.skill_verified for r in subset if r.skill_verified is not None]
        row.append(_pct(verified) if verified else "n/a")
        for name in rule_names:
            row.append(_pct([r.rule_checks[name] for r in subset if name in r.rule_checks]))
        for name in post_check_names:
            row.append(_pct([r.post_checks[name] for r in subset if name in r.post_checks]))
        jv = [r.judge_score for r in subset if r.judge_score is not None]
        row.append(f"{mean(jv):.1f}" if jv else "n/a")
        rows.append(row)
    return headers, rows


def print_report(results: list[RunResult]) -> None:
    headers, rows = summarize(results)
    print("\n=== Skill effectiveness summary ===")
    if tabulate:
        print(tabulate(rows, headers=headers, tablefmt="github"))
    else:
        print("\t".join(headers))
        for r in rows:
            print("\t".join(str(c) for c in r))

    by = {r[0]: r for r in rows}
    if "control" in by and "skill" in by:
        c, s = by["control"], by["skill"]
        print("\n=== Delta (skill − control) ===")
        print(f"  output tokens : {s[3] - c[3]:+d}")
        print(f"  cost / task   : {float(s[4]) - float(c[4]):+.5f} USD (uncached)")
        print(f"  latency       : {float(s[5]) - float(c[5]):+.1f} s")
        for name in headers[6:]:
            if name in ("skill verified", "judge /10"):
                continue
            if name.startswith("post: "):
                continue
            i = headers.index(name)
            ci, si = c[i], s[i]
            if ci != "n/a" and si != "n/a":
                print(f"  {name:<30} : {ci} -> {si}")
        sv = s[headers.index("skill verified")]
        if sv != "n/a":
            print(f"  skill verified               : {sv}")
        if c[-1] != "n/a" and s[-1] != "n/a":
            print(f"  judge /10                    : {c[-1]} -> {s[-1]}")

    # Console friction summary
    all_friction: list[str] = []
    for r in results:
        for f in r.friction:
            if f not in all_friction:
                all_friction.append(f)
    if all_friction:
        print("\n=== Agent friction signals ===")
        for f in all_friction:
            print(f"  {f}")


def _md_table(headers: list[str], rows: list[list]) -> str:
    line = "| " + " | ".join(headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    body = "\n".join("| " + " | ".join(str(c) for c in r) + " |" for r in rows)
    return "\n".join([line, sep, body])


def write_markdown(results: list[RunResult], path: Path, *, name: str = "skill") -> None:
    """Write a human-readable Markdown report focused on token usage."""
    headers, rows = summarize(results)
    lines: list[str] = [f"# Skill effectiveness report — {name}", ""]

    # Overall summary table
    lines += ["## Summary", "", _md_table(headers, rows), ""]

    # Token usage breakdown
    lines += ["## Token usage (with vs without skill)", ""]
    tok_headers = ["variant", "runs", "avg input", "avg cached", "avg output",
                   "avg total", "avg cost $ (uncached)", "skill verified"]
    tok_rows: list[list] = []
    totals: dict[str, dict] = {}
    for variant in ("control", "skill"):
        subset = [r for r in results if r.variant == variant and not r.error]
        if not subset:
            continue
        avg_in = mean([r.input_tokens for r in subset])
        avg_cache = mean([r.cache_read_tokens + r.cache_write_tokens for r in subset])
        avg_out = mean([r.output_tokens for r in subset])
        avg_total = avg_in + avg_cache + avg_out
        avg_cost_nc = mean([uncached_cost(r) for r in subset])
        totals[variant] = {"in": avg_in, "cache": avg_cache, "out": avg_out,
                           "total": avg_total, "cost_nc": avg_cost_nc}
        label = "without skill" if variant == "control" else "with skill"
        verified = [r.skill_verified for r in subset if r.skill_verified is not None]
        tok_rows.append([label, len(subset), round(avg_in), round(avg_cache),
                         round(avg_out), round(avg_total), f"{avg_cost_nc:.5f}",
                         _pct(verified) if verified else "n/a"])
    lines += [_md_table(tok_headers, tok_rows), ""]

    # Delta section
    if "control" in totals and "skill" in totals:
        c, s = totals["control"], totals["skill"]
        def d(k):  # noqa: E306
            return s[k] - c[k]
        def pct(k):  # noqa: E306
            return f"{(d(k) / c[k] * 100):+.0f}%" if c[k] else "n/a"
        lines += ["## Delta (skill − control)", ""]
        lines += [_md_table(
            ["metric", "without skill", "with skill", "delta", "change"],
            [
                ["input tokens", round(c["in"]), round(s["in"]), f"{round(d('in')):+d}", pct("in")],
                ["output tokens", round(c["out"]), round(s["out"]), f"{round(d('out')):+d}", pct("out")],
                ["total tokens", round(c["total"]), round(s["total"]), f"{round(d('total')):+d}", pct("total")],
                ["cost $ (uncached)", f"{c['cost_nc']:.5f}", f"{s['cost_nc']:.5f}", f"{d('cost_nc'):+.5f}", pct("cost_nc")],
            ],
        ), ""]

    # Per-task token detail
    lines += ["## Per-task token usage", ""]
    task_ids: list[str] = []
    for r in results:
        if r.task_id not in task_ids:
            task_ids.append(r.task_id)
    detail_headers = ["task", "variant", "input", "cached", "output", "cost $", "latency s"]
    detail_rows: list[list] = []
    for tid in task_ids:
        for variant in ("control", "skill"):
            subset = [r for r in results if r.task_id == tid and r.variant == variant and not r.error]
            if not subset:
                continue
            detail_rows.append([
                tid, variant,
                round(mean([r.input_tokens for r in subset])),
                round(mean([r.cache_read_tokens + r.cache_write_tokens for r in subset])),
                round(mean([r.output_tokens for r in subset])),
                f"{mean([r.cost_usd for r in subset]):.5f}",
                f"{mean([r.latency_s for r in subset]):.1f}",
            ])
    lines += [_md_table(detail_headers, detail_rows), ""]

    # Per-run detail (every individual agent run)
    lines += ["## Per-run detail", ""]
    run_headers = ["task", "variant", "input", "cached", "output", "cost $", "latency s", "judge /10", "error"]
    run_rows: list[list] = []
    for tid in task_ids:
        for variant in ("control", "skill"):
            for r in [x for x in results if x.task_id == tid and x.variant == variant]:
                run_rows.append([
                    tid, variant,
                    r.input_tokens,
                    r.cache_read_tokens + r.cache_write_tokens,
                    r.output_tokens,
                    f"{r.cost_usd:.5f}",
                    f"{r.latency_s:.1f}",
                    f"{r.judge_score:.1f}" if r.judge_score is not None else "n/a",
                    r.error or "—",
                ])
    lines += [_md_table(run_headers, run_rows), ""]
    friction_by_task: dict[str, dict[str, list[str]]] = {}
    for r in results:
        if r.friction:
            friction_by_task.setdefault(r.task_id, {}).setdefault(r.variant, [])
            friction_by_task[r.task_id][r.variant].extend(r.friction)

    if friction_by_task:
        lines += ["## Where the agent struggled", ""]
        lines += [
            "_Friction signals from agent traces — repeated tool calls, permission denials, "
            "and high turn counts indicate where the skill could be clearer._", ""
        ]
        for tid in task_ids:
            task_friction = friction_by_task.get(tid)
            if not task_friction:
                continue
            lines.append(f"#### {tid}")
            for variant in ("control", "skill"):
                signals = task_friction.get(variant)
                if signals:
                    unique = list(dict.fromkeys(signals))
                    lines.append(f"**{variant}:**")
                    for s in unique:
                        lines.append(f"  - {s}")
            lines.append("")

    path.write_text("\n".join(lines))

