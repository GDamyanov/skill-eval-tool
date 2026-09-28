"""Skill quality review using the skill-reviewer rubric.

Loads the skill files (same mechanism as the A/B evaluator), sends them to
the model with the reviewer prompt, and writes a structured Markdown report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .backends import Backend
from .config import Config
from .core import load_skill_text


REVIEWER_SYSTEM = """\
You are an expert Claude Code skill reviewer. Your job is to evaluate a skill
definition (one or more SKILL.md / reference files) and produce a structured
quality report.

Evaluate the skill on these criteria:

1. STRUCTURE — frontmatter present with `name` and `description` fields (YAML
   between `---` delimiters).

2. DESCRIPTION QUALITY — the most critical field:
   - Written in third person ("This skill should be used when …")
   - Contains specific trigger phrases a user would naturally say
   - Concrete scenarios, not vague generalities
   - 50–500 characters long

3. CONTENT QUALITY:
   - Body word count is 1 000–3 000 words
   - Imperative / infinitive writing style ("To do X, do Y")
   - Clear sections with logical flow
   - Concrete, actionable guidance

4. PROGRESSIVE DISCLOSURE:
   - Core SKILL.md contains only essential information
   - Detail moved to references/ and examples/ directories
   - SKILL.md references those resources explicitly

5. ISSUES — categorise as:
   - **Critical**: major flaw that breaks usability
   - **Major**: significant improvement needed
   - **Minor**: polish / refinement

Respond in this exact Markdown structure (keep the headings verbatim):

## Summary
<one-paragraph overview of what the skill does and its overall quality>

Word count: <N> words
Description length: <N> characters

## Description Analysis
<analysis of the `description` field with specific fixes if needed>

## Content Quality
<assessment of writing style, structure, completeness>

## Progressive Disclosure
<assessment of file organisation — what is present and what is missing>

## Issues

### Critical
<bulleted list, or "None">

### Major
<bulleted list, or "None">

### Minor
<bulleted list, or "None">

## Positive Aspects
<bulleted list of things done well>

## Overall Rating
**<Pass | Needs Improvement | Needs Major Revision>**

<one sentence justification>

## Prioritised Recommendations
<numbered action list, most impactful first>
"""

REVIEWER_PROMPT_TEMPLATE = """\
Review the following skill definition and produce a quality report.

<skill>
{skill_text}
</skill>
"""


@dataclass
class ReviewResult:
    skill_name: str
    report_text: str
    rating: str = ""          # "Pass" | "Needs Improvement" | "Needs Major Revision"
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""
    error: str = ""
    issues: dict[str, list[str]] = field(default_factory=dict)


def _parse_rating(text: str) -> str:
    m = re.search(
        r"\*\*(Pass|Needs Improvement|Needs Major Revision)\*\*",
        text,
    )
    return m.group(1) if m else ""


def _parse_issues(text: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for level in ("Critical", "Major", "Minor"):
        # Stop at the next ## or ### heading to avoid bleeding into other sections
        pattern = rf"### {level}\s*\n(.*?)(?=^##|\Z)"
        m = re.search(pattern, text, re.DOTALL | re.MULTILINE)
        if not m:
            continue
        block = m.group(1).strip()
        if not block or block.lower() == "none":
            result[level.lower()] = []
        else:
            items = [
                re.sub(r"\*\*", "", line.lstrip("- •*")).strip()
                for line in block.splitlines()
                if line.strip() and line.strip().startswith("-")
            ]
            result[level.lower()] = [i for i in items if i]
    return result


def run_review(cfg: Config, backend: Backend) -> ReviewResult:
    skill_text = load_skill_text(cfg)
    prompt = REVIEWER_PROMPT_TEMPLATE.format(skill_text=skill_text)

    model = cfg.reviewer_model or cfg.model
    result = ReviewResult(skill_name=cfg.name, report_text="")

    try:
        c = backend.complete(
            prompt,
            REVIEWER_SYSTEM,
            model=model,
            no_tools=True,
            timeout=300,
        )
        result.report_text = c.text.strip()
        result.rating = _parse_rating(result.report_text)
        result.issues = _parse_issues(result.report_text)
        result.input_tokens = c.input_tokens
        result.output_tokens = c.output_tokens
        result.cost_usd = c.cost_usd
        result.model = c.model
    except Exception as exc:  # noqa: BLE001
        result.error = str(exc)

    return result


def write_review_report(result: ReviewResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "skill_review.md"

    header = f"# Skill review — {result.skill_name}\n\n"
    meta = (
        f"_Model: {result.model} · "
        f"Tokens: {result.input_tokens} in / {result.output_tokens} out · "
        f"Cost: ${result.cost_usd:.5f}_\n\n"
        "---\n\n"
    )
    path.write_text(header + meta + result.report_text + "\n")
    return path


def print_review_summary(result: ReviewResult) -> None:
    print("\n=== Skill review ===")
    if result.error:
        print(f"ERROR: {result.error}")
        return

    rating_symbol = {"Pass": "✓", "Needs Improvement": "~", "Needs Major Revision": "✗"}.get(
        result.rating, "?"
    )
    print(f"Rating : {rating_symbol} {result.rating or '(unknown)'}")

    for level in ("critical", "major", "minor"):
        items = result.issues.get(level, [])
        if items:
            print(f"\n{level.capitalize()} issues ({len(items)}):")
            for item in items:
                print(f"  - {item}")
