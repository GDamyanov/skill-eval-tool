"""Best-practices audit: score a configured skill against Anthropic's guidelines."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .backends import Backend
from .config import Config
from .utils import load_skill_text


# ---------------------------------------------------------------------------
# Rules extracted from https://platform.claude.com/docs/en/agents-and-tools/
#   agent-skills/best-practices
# ---------------------------------------------------------------------------

BEST_PRACTICES_RULES: list[dict] = [
    # Core principles
    {
        "id": "bp-concise",
        "category": "Core principles",
        "title": "SKILL.md is concise",
        "description": (
            "The skill should be as short as possible. Every token must justify "
            "its cost. It should not explain things Claude already knows "
            "(e.g., language fundamentals, generic library usage). "
            "A good skill is ~50–300 tokens in the main body."
        ),
    },
    {
        "id": "bp-no-verbosity",
        "category": "Core principles",
        "title": "No unnecessary verbosity",
        "description": (
            "The skill avoids wordy introductions, repeating the same point "
            "multiple times, or explaining obvious background knowledge. "
            "Only context Claude genuinely doesn't have should be included."
        ),
    },
    {
        "id": "bp-freedom-match",
        "category": "Core principles",
        "title": "Degree of freedom matches task fragility",
        "description": (
            "High-freedom tasks (many valid approaches) use text instructions. "
            "Fragile, error-prone tasks (e.g., DB migrations) use exact scripts "
            "and low-freedom instructions. The skill should not over-constrain "
            "flexible tasks or under-constrain fragile ones."
        ),
    },
    # Frontmatter / naming
    {
        "id": "bp-name-format",
        "category": "Skill structure",
        "title": "name field follows spec",
        "description": (
            "The YAML frontmatter `name` must be lowercase letters, numbers, "
            "and hyphens only, max 64 characters, no XML tags, no reserved words "
            "('anthropic', 'claude')."
        ),
    },
    {
        "id": "bp-name-gerund",
        "category": "Skill structure",
        "title": "Name uses gerund or action-oriented form",
        "description": (
            "The skill name should use gerund form (verb + -ing, e.g. "
            "'processing-pdfs') or a clear action phrase. Vague names like "
            "'helper', 'utils', 'tools', 'data', 'files' should be avoided."
        ),
    },
    {
        "id": "bp-description-third-person",
        "category": "Skill structure",
        "title": "description is written in third person",
        "description": (
            "The `description` field must be in third person. "
            "Avoid 'I can help you...' or 'You can use this to...'. "
            "Good: 'Processes Excel files and generates reports'."
        ),
    },
    {
        "id": "bp-description-specific",
        "category": "Skill structure",
        "title": "description is specific and includes trigger context",
        "description": (
            "The description should include both WHAT the skill does AND WHEN "
            "to use it. It must be specific enough for Claude to select this "
            "skill over 100+ others. Vague descriptions like 'Helps with "
            "documents' are insufficient."
        ),
    },
    {
        "id": "bp-description-length",
        "category": "Skill structure",
        "title": "description is within 1,024 characters",
        "description": "The description field must not exceed 1,024 characters.",
    },
    # File structure
    {
        "id": "bp-under-500-lines",
        "category": "Skill structure",
        "title": "SKILL.md body is under 500 lines",
        "description": (
            "The main SKILL.md body should be under 500 lines. Excess content "
            "should be split into separate reference files using progressive "
            "disclosure patterns."
        ),
    },
    {
        "id": "bp-progressive-disclosure",
        "category": "Skill structure",
        "title": "Progressive disclosure for large content",
        "description": (
            "If the skill has substantial reference material, it should be split "
            "into separate files (e.g., FORMS.md, reference.md) that are linked "
            "from SKILL.md. These files should be loaded only when needed."
        ),
    },
    {
        "id": "bp-references-one-level",
        "category": "Skill structure",
        "title": "References are at most one level deep",
        "description": (
            "All reference files should link directly from SKILL.md — not from "
            "other reference files. Deeply nested references cause Claude to only "
            "partially read content."
        ),
    },
    # Content guidelines
    {
        "id": "bp-no-time-sensitive",
        "category": "Content guidelines",
        "title": "No time-sensitive information",
        "description": (
            "The skill should not include date-based conditions like 'before "
            "August 2025, use X; after, use Y'. Instead, use versioned 'old "
            "patterns' sections or simply reference the current approach."
        ),
    },
    {
        "id": "bp-consistent-terminology",
        "category": "Content guidelines",
        "title": "Consistent terminology",
        "description": (
            "The skill should use one term consistently for each concept. "
            "Mixing synonyms (endpoint/URL/path, field/box/element) makes "
            "it harder for Claude to parse instructions reliably."
        ),
    },
    {
        "id": "bp-no-unix-paths",
        "category": "Content guidelines",
        "title": "Uses forward slashes (no Windows-style paths)",
        "description": (
            "All file paths in the skill must use forward slashes, not backslashes, "
            "to ensure cross-platform compatibility."
        ),
    },
    {
        "id": "bp-no-too-many-options",
        "category": "Content guidelines",
        "title": "Provides a default, not a menu of options",
        "description": (
            "The skill should recommend a single approach as the default, with "
            "an optional escape hatch for edge cases. It should not present "
            "multiple equal alternatives without a recommendation."
        ),
    },
    # Workflows
    {
        "id": "bp-workflow-steps",
        "category": "Workflows",
        "title": "Complex tasks broken into numbered steps",
        "description": (
            "For multi-step tasks, the skill should provide a clear numbered "
            "workflow. Optionally, a checklist Claude can copy and check off "
            "is recommended for complex operations."
        ),
    },
    {
        "id": "bp-feedback-loops",
        "category": "Workflows",
        "title": "Validation / feedback loops included for quality-critical tasks",
        "description": (
            "If the skill involves quality-critical output (code generation, "
            "document editing), it should include a validate-fix-repeat loop. "
            "Scripts or reference documents serve as the validator."
        ),
    },
    # Evaluation
    {
        "id": "bp-eval-first",
        "category": "Evaluation",
        "title": "Skill was built with evaluations in mind",
        "description": (
            "The skill content and structure suggest it was tested against real "
            "scenarios. Evidence: concrete examples, specifics about what succeeds "
            "or fails, rules derived from observed agent behavior rather than "
            "hypothetical requirements."
        ),
    },
    # Scripts (conditional — only applies when the skill includes code)
    {
        "id": "bp-scripts-handle-errors",
        "category": "Scripts",
        "title": "Scripts handle errors explicitly (if applicable)",
        "description": (
            "If the skill includes utility scripts, they should handle error "
            "conditions (FileNotFoundError, PermissionError) rather than letting "
            "Python raise unhandled exceptions. Does not apply to skills with no "
            "script content."
        ),
    },
    {
        "id": "bp-no-voodoo-constants",
        "category": "Scripts",
        "title": "No unexplained magic constants (if applicable)",
        "description": (
            "If the skill includes scripts with numeric constants (timeouts, "
            "retries), each constant should have a comment explaining why it has "
            "that value. Does not apply to skills with no script content."
        ),
    },
    {
        "id": "bp-mcp-fully-qualified",
        "category": "Scripts",
        "title": "MCP tool references are fully qualified (if applicable)",
        "description": (
            "If the skill references MCP tools, they must use the format "
            "'ServerName:tool_name' to avoid 'tool not found' errors. "
            "Does not apply to skills that don't use MCP."
        ),
    },
]

BEST_PRACTICES_URL = (
    "https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices"
)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class RuleResult:
    rule_id: str
    category: str
    title: str
    status: str   # "pass" | "fail" | "n/a"
    rationale: str = ""


@dataclass
class AuditResult:
    skill_name: str
    skill_files: list[str] = field(default_factory=list)
    rules: list[RuleResult] = field(default_factory=list)
    overall_score: float = 0.0   # 0-100 (n/a rules excluded from denominator)
    error: str = ""


# ---------------------------------------------------------------------------
# Scoring prompt
# ---------------------------------------------------------------------------

_AUDIT_SYSTEM = """\
You are an expert reviewer of Claude Agent Skills (SKILL.md files). Your job is
to evaluate a skill against a set of best-practice rules from Anthropic's
official documentation.

For each rule you will return one of:
  "pass"  — the skill clearly satisfies this rule
  "fail"  — the skill clearly violates this rule
  "n/a"   — the rule is explicitly marked conditional and does not apply
             to this skill (e.g., a script-quality rule for a skill with no
             scripts)

Return ONLY a JSON array — no prose, no markdown fences:
[
  {"rule_id": "bp-concise", "status": "pass", "rationale": "<one sentence>"},
  ...
]
Provide a rationale for every entry, especially failures.
"""


def run_audit(cfg: Config, backend: Backend) -> AuditResult:
    skill_text = load_skill_text(cfg)

    # Collect skill file names for the report
    from pathlib import Path as _Path
    base = cfg.resolve(cfg.skill_base_dir)
    skill_file_names: list[str] = []
    for pattern in cfg.skill_files:
        p = _Path(pattern)
        if p.is_absolute():
            matches = sorted(_Path(p.anchor).glob(str(p.relative_to(p.anchor))))
        else:
            matches = sorted(base.glob(pattern))
        for f in matches:
            if f.is_file():
                skill_file_names.append(str(f.relative_to(base)))

    result = AuditResult(
        skill_name=cfg.name,
        skill_files=skill_file_names,
    )

    rules_json = json.dumps(
        [{"rule_id": r["id"], "title": r["title"], "description": r["description"]}
         for r in BEST_PRACTICES_RULES],
        indent=2,
    )
    prompt = (
        f"SKILL CONTENT:\n\n{skill_text}\n\n"
        f"RULES TO EVALUATE:\n\n{rules_json}"
    )

    try:
        c = backend.complete(
            prompt, _AUDIT_SYSTEM,
            model=cfg.model, no_tools=True, timeout=120,
        )
        m = re.search(r"\[.*\]", c.text, re.DOTALL)
        parsed: list[dict] = json.loads(m.group(0) if m else c.text)
    except Exception as exc:
        result.error = str(exc)
        return result

    rule_map = {r["id"]: r for r in BEST_PRACTICES_RULES}
    rule_results: list[RuleResult] = []
    for item in parsed:
        rid = item.get("rule_id", "")
        meta = rule_map.get(rid, {})
        rule_results.append(RuleResult(
            rule_id=rid,
            category=meta.get("category", ""),
            title=meta.get("title", rid),
            status=item.get("status", "fail"),
            rationale=item.get("rationale", ""),
        ))

    result.rules = rule_results

    applicable = [r for r in rule_results if r.status != "n/a"]
    passed = [r for r in applicable if r.status == "pass"]
    result.overall_score = (
        round(len(passed) / len(applicable) * 100, 1) if applicable else 0.0
    )
    return result


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------

def write_audit_report(result: AuditResult, out_dir: Path) -> Path:
    lines: list[str] = []
    lines.append(f"# Best-practices audit — {result.skill_name}\n")
    lines.append(
        f"> Reference: [{BEST_PRACTICES_URL}]({BEST_PRACTICES_URL})\n"
    )
    lines.append(f"**Skill files:** {', '.join(result.skill_files) or 'n/a'}\n")

    if result.error:
        lines.append(f"\n**Error:** {result.error}\n")
        path = out_dir / "best_practices_audit.md"
        path.write_text("\n".join(lines))
        return path

    # Overall score
    applicable = [r for r in result.rules if r.status != "n/a"]
    passed = [r for r in applicable if r.status == "pass"]
    failed = [r for r in applicable if r.status == "fail"]
    na = [r for r in result.rules if r.status == "n/a"]

    lines.append(f"## Overall score: {result.overall_score}%")
    lines.append(
        f"**{len(passed)} passed** / {len(applicable)} applicable rules "
        f"({len(failed)} failed, {len(na)} not applicable)\n"
    )

    # Quick summary table
    lines.append("## Summary table\n")
    lines.append("| # | Category | Rule | Status |")
    lines.append("|---|----------|------|--------|")
    for i, r in enumerate(result.rules, 1):
        icon = {"pass": "✅", "fail": "❌", "n/a": "—"}.get(r.status, r.status)
        lines.append(f"| {i} | {r.category} | {r.title} | {icon} |")
    lines.append("")

    # Failures section
    if failed:
        lines.append("## Failures — what to fix\n")
        for r in failed:
            lines.append(f"### ❌ {r.title}")
            lines.append(f"**Rule:** `{r.rule_id}`  |  **Category:** {r.category}")
            # Full rule description
            meta = next((bp for bp in BEST_PRACTICES_RULES if bp["id"] == r.rule_id), {})
            if meta.get("description"):
                lines.append(f"\n**Guideline:** {meta['description']}")
            lines.append(f"\n**Finding:** {r.rationale}\n")

    # Passes section
    if passed:
        lines.append("## Passes\n")
        for r in passed:
            lines.append(f"- ✅ **{r.title}** — {r.rationale}")
        lines.append("")

    # N/A section
    if na:
        lines.append("## Not applicable\n")
        for r in na:
            lines.append(f"- — **{r.title}** — {r.rationale}")
        lines.append("")

    path = out_dir / "best_practices_audit.md"
    path.write_text("\n".join(lines))
    return path


def print_audit_summary(result: AuditResult) -> None:
    if result.error:
        print(f"Audit error: {result.error}")
        return
    applicable = [r for r in result.rules if r.status != "n/a"]
    passed = [r for r in applicable if r.status == "pass"]
    failed = [r for r in applicable if r.status == "fail"]
    print(f"\n=== Best-practices audit: {result.skill_name} ===")
    print(f"Score: {result.overall_score}%  ({len(passed)}/{len(applicable)} rules passed)")
    if failed:
        print("Failures:")
        for r in failed:
            print(f"  ❌ [{r.category}] {r.title}")
            print(f"     {r.rationale}")
