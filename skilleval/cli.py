"""Command-line entry point: `skilleval --config path/to/config.yaml`."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from .backends import make_backend
from .config import Config
from .reviewer import run_review, write_review_report, print_review_summary
from .verify import run_verify, print_verify_summary
from .plugin_eval import run_plugin_eval, write_plugin_eval_report, print_plugin_eval_summary
from .audit import run_audit, write_audit_report, print_audit_summary
from .full_report import run_full, write_full_report, print_full_summary
from .improve import generate_improvement_prompt, write_improvement_prompt


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="skilleval",
        description="Evaluate a Claude Code skill via plugin eval, review, audit, and verify.",
    )
    p.add_argument("--config", "-c", required=True,
                   help="Path to the evaluation config (.yaml or .json).")
    p.add_argument("--review", action="store_true",
                   help="Run the skill-reviewer rubric on the configured skill files and write a quality report.")
    p.add_argument("--verify", action="store_true",
                   help="Probe the model to confirm the skill is in its context (exits with code 1 if not).")
    p.add_argument("--claude-skill-eval", action="store_true",
                   help="Run claude plugin eval on the configured skill using the eval cases in "
                        "<config_dir>/evals/ and write a plugin_eval_report.md.")
    p.add_argument("--audit", action="store_true",
                   help="Score the configured skill against Anthropic's best-practices guidelines "
                        "and write a best_practices_audit.md report.")
    p.add_argument("--full", action="store_true",
                   help="Run all evaluation commands (audit, review, verify, plugin eval) "
                        "and write a synthesized full_report.md.")
    p.add_argument("--plugin-eval-runs", type=int, default=0,
                   help="Runs per eval case for --claude-skill-eval and --full. "
                        "Defaults to --parallel value (default: 1 if --parallel not set).")
    p.add_argument("--parallel", type=int, default=1, metavar="N",
                   help="Run N plugin eval cases in parallel (default: 1 = serial).")
    p.add_argument("--no-cache", action="store_true",
                   help="Disable incremental phase cache for --full (always re-run all phases).")
    p.add_argument("--cache-ttl", type=int, default=60, metavar="MINS",
                   help="Max age in minutes of a cached plugin-eval result before re-running (default: 60).")
    p.add_argument("--backend", default=None,
                   help="Override the backend from config: 'claude-cli' or 'anthropic-sdk'.")
    p.add_argument("--model", default=None, help="Override the model from config.")
    p.add_argument("--out", default=None,
                   help="Output directory (default: ./results/<timestamp> next to the config).")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    cfg = Config.load(args.config)
    if args.backend:
        cfg.backend = args.backend
    if args.model:
        cfg.model = args.model

    backend = make_backend(cfg.backend)
    if not backend.available():
        print(f"Backend '{cfg.backend}' is not available (missing CLI or API key).", file=sys.stderr)
        return 2

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if args.out:
        out_dir = Path(args.out)
    else:
        if args.full:
            run_label = "full"
        elif args.claude_skill_eval:
            run_label = "plugin-eval"
        elif args.audit:
            run_label = "audit"
        elif args.review:
            run_label = "review"
        elif args.verify:
            run_label = "verify"
        else:
            run_label = "eval"
        out_dir = cfg._config_dir / "results" / f"{stamp}-{run_label}"
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.full:
        evals_dir = (
            cfg.resolve(cfg.plugin_evals_dir) if cfg.plugin_evals_dir
            else cfg._config_dir / "evals"
        )
        result = run_full(
            cfg, backend,
            evals_dir=evals_dir,
            plugin_eval_runs=args.plugin_eval_runs or args.parallel or 1,
            out_dir=out_dir,
            use_cache=not args.no_cache,
            plugin_eval_cache_ttl_min=args.cache_ttl,
            parallel=args.parallel,
        )
        print_full_summary(result)
        report_path = write_full_report(result, out_dir)
        print(f"\nFull report: {report_path}")
        sub_reports = [out_dir / f for f in (
            "best_practices_audit.md", "skill_review.md", "plugin_eval_report.md"
        )]
        imp_path = write_improvement_prompt(generate_improvement_prompt(sub_reports, cfg, backend), out_dir)
        print(f"Improvement prompt: {imp_path}")
        return 1 if result.error else 0

    if args.audit:
        result = run_audit(cfg, backend)
        print_audit_summary(result)
        report_path = write_audit_report(result, out_dir)
        print(f"\nBest-practices audit: {report_path}")
        imp_path = write_improvement_prompt(generate_improvement_prompt([report_path], cfg, backend), out_dir)
        print(f"Improvement prompt: {imp_path}")
        return 1 if result.error else 0

    if args.review:
        result = run_review(cfg, backend)
        print_review_summary(result)
        report_path = write_review_report(result, out_dir)
        print(f"\nSkill review:  {report_path}")
        imp_path = write_improvement_prompt(generate_improvement_prompt([report_path], cfg, backend), out_dir)
        print(f"Improvement prompt: {imp_path}")
        return 1 if result.error else 0

    if args.verify:
        result = run_verify(cfg, backend)
        print_verify_summary(result)
        return 0 if result.verified else 1

    if args.claude_skill_eval:
        evals_dir = (
            cfg.resolve(cfg.plugin_evals_dir) if cfg.plugin_evals_dir
            else cfg._config_dir / "evals"
        )
        result = run_plugin_eval(cfg, evals_dir, runs=args.plugin_eval_runs or args.parallel or 1,
                                  out_dir=out_dir, concurrency=args.parallel)
        print_plugin_eval_summary(result)
        report_path = write_plugin_eval_report(result, out_dir)
        print(f"\nPlugin eval report: {report_path}")
        imp_path = write_improvement_prompt(generate_improvement_prompt([report_path], cfg, backend), out_dir)
        print(f"Improvement prompt: {imp_path}")
        return 1 if result.error else 0

    print("No action specified. Use --full, --audit, --review, --verify, or --claude-skill-eval.",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
