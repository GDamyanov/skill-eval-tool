"""Generate a ready-to-use improvement prompt from an evaluation report."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .backends import Backend
from .config import Config
from .utils import load_skill_text  # noqa: F401

if TYPE_CHECKING:
    from .full_report import FullReport


def _skill_file_paths(cfg: Config) -> list[Path]:
    """Return the resolved absolute paths of all skill files."""
    base = cfg.resolve(cfg.skill_base_dir)
    files: list[Path] = []
    for pattern in cfg.skill_files:
        p = Path(pattern)
        if p.is_absolute():
            files.extend(sorted(Path(p.anchor).glob(str(p.relative_to(p.anchor)))))
        else:
            files.extend(sorted(base.glob(pattern)))
    seen: set[Path] = set()
    unique: list[Path] = []
    for f in files:
        if f.is_file() and f not in seen:
            seen.add(f)
            unique.append(f)
    return unique


_AGENT_PROMPT_TEMPLATE = """\
You are a senior skill author. Your task: analyse the evaluation reports for the \
Claude Code skill named "{skill_name}" and write a precise, actionable improvement prompt.

Step 1 — Read each of the following report files carefully (use your Read tool):
{report_file_list}

While reading, extract ALL of the following (write nothing yet — just collect):
  a) plugin_eval_report: every grader criterion that failed in any run; every friction signal \
(repeated reads, high turn counts, strategy pivots, ignored skill guidance)
  b) skill_review: every Critical, Major, and Minor issue listed under "## Issues"; \
every item in "## Prioritised Recommendations"
  c) best_practices_audit: every rule listed under "## Failures" — including structural ones \
like bp-concise, bp-name-gerund, bp-under-500-lines. These are failures and MUST produce instructions.

Step 2 — Read the current skill files (use your Read tool):
{skill_file_list}

Step 2.5 — Before writing, count the items in your collected list per category and verify:
  ✓ plugin eval: N grader failures collected (one per distinct failing criterion)
  ✓ skill_review: N Critical + N Major + N Minor collected (must match the Issues section exactly)
  ✓ best_practices_audit: N failures collected — count the items under "## Failures" \
and confirm your list has the same count. Every failure listed there, regardless of its rule ID \
or category, MUST appear as a separate item. Do not merge, skip, or summarise any of them.

Step 3 — Write the improvement prompt in this exact order:
  1. Plugin eval grader failures (one instruction each, most frequent failures first)
  2. Every audit failure from "## Failures" — one concrete instruction per failure, \
in the order they appear in the audit file. For each:
     - Name the rule and what it requires
     - Name the specific SKILL.md file/section to change
     - Give a concrete example of the fix where applicable
  3. skill_review Critical issues (if any)
  4. skill_review Major issues
  5. skill_review Minor issues and friction signals

Additional rules:
- Each instruction must name the specific skill file and section to edit, \
and include a short correct example where relevant.
- Do NOT mention reports, evaluation runs, scores, pass rates, or any metric names. \
Write as if a knowledgeable colleague reviewed the skill directly and found these gaps.
- Do NOT add a preamble, introduction, section headers, rationale section, or closing summary.
  Start immediately with the first instruction. Output ONLY the instructions, nothing else.\
"""


_FALLBACK_SYSTEM = """\
You are a senior skill author reviewing a Claude Code skill for correctness and effectiveness.
You have been given evaluation findings first, then the full skill text. Read the findings
carefully before reading the skill — every finding must produce at least one concrete instruction.

Your task: write a precise, actionable edit prompt that the skill author will paste directly
into Claude Code to fix the skill files.

Rules:
- Cover ALL findings from ALL sections of the evaluation data: audit rule failures,
  review issues (critical, major, and minor), and agent behaviour problems.
  Do not silently skip any finding, even minor ones.
- Write one concrete instruction per weakness. Each instruction must name the specific skill
  file and section to edit, and include a short correct code or text example where relevant.
- Order by impact: criteria that fail every run come first, then major issues, then minor.
- Do NOT mention reports, evaluation runs, scores, pass rates, or any metric names.
  Write as if a knowledgeable colleague reviewed the skill directly and found these gaps.
- Do NOT add a preamble, introduction, section headers, rationale section, or closing summary.
  Start immediately with the first instruction. Output ONLY the instructions, nothing else.\
"""

_FALLBACK_TEMPLATE = """\
TASK: Improve the Claude Code skill named "{skill_name}" based on the findings below.

EVALUATION FINDINGS:
{reports_text}

---

CURRENT SKILL FILES (edit these):
{skill_text}\
"""


def _extract_actionable(path: Path) -> str:
    """Extract only the actionable sections from a report file.

    Strips verbose content (generated code blocks, positive aspects, full agent
    traces) so the LLM receives dense signal without noise.
    """
    if not path.exists():
        return ""
    text = path.read_text().strip()
    name = path.name

    if name == "plugin_eval_report.md":
        # Keep: overall table, case results, per-run detail, grader tables, friction signals.
        # Drop: the large generated code blocks (everything between ``` fences after "Generated file").
        import re
        # Remove generated file code blocks — they can be thousands of lines
        text = re.sub(
            r"\*\*Generated files?.*?\n```.*?```",
            "(generated code omitted)",
            text,
            flags=re.DOTALL,
        )
        # Also remove the without-skill full response block
        text = re.sub(
            r"\*\*Response:\*\*\s*\n```.*?```",
            "(agent response omitted)",
            text,
            flags=re.DOTALL,
        )
        return text

    if name == "skill_review.md":
        # Keep: Issues (Critical/Major/Minor) + Prioritised Recommendations.
        # Drop: Summary, Description Analysis, Content Quality, Positive Aspects, etc.
        import re
        sections = []
        # Extract Issues section
        m = re.search(r"(## Issues.*?)(?=## Positive Aspects|## Overall Rating|$)", text, re.DOTALL)
        if m:
            sections.append(m.group(1).strip())
        # Extract Overall Rating
        m = re.search(r"(## Overall Rating.*?)(?=## Prioritised|$)", text, re.DOTALL)
        if m:
            sections.append(m.group(1).strip())
        # Extract Prioritised Recommendations
        m = re.search(r"(## Prioritised Recommendations.*?)$", text, re.DOTALL)
        if m:
            sections.append(m.group(1).strip())
        return "\n\n".join(sections) if sections else text

    if name == "best_practices_audit.md":
        # Keep: Failures section only (drop Passes and Not applicable).
        import re
        m = re.search(r"(## Failures.*?)(?=## Passes|## Not applicable|$)", text, re.DOTALL)
        return m.group(1).strip() if m else text

    return text


def _collect_reports_text(report_paths: list[Path]) -> str:
    """Extract actionable sections from all report files and concatenate them.

    plugin_eval_report comes first (agent behaviour evidence), then audit and
    review last so they sit closest to the skill text (recency bias).
    """
    priority_last = {"best_practices_audit.md", "skill_review.md"}
    first = [p for p in report_paths if p.name not in priority_last]
    last = [p for p in report_paths if p.name in priority_last]
    parts = []
    for p in first + last:
        content = _extract_actionable(p)
        if content:
            parts.append(content)
    return "\n\n---\n\n".join(parts)


def generate_improvement_prompt(
    report_paths: list[Path],
    cfg: Config,
    backend: Backend,
    report: "FullReport | None" = None,
) -> str:
    from .backends import ClaudeCLIBackend
    model = cfg.reviewer_model or cfg.model

    if isinstance(backend, ClaudeCLIBackend):
        skill_files = _skill_file_paths(cfg)
        report_file_list = "\n".join(f"  {p}" for p in report_paths if p.exists())
        skill_file_list = "\n".join(f"  {p}" for p in skill_files)
        prompt = _AGENT_PROMPT_TEMPLATE.format(
            skill_name=cfg.name,
            report_file_list=report_file_list,
            skill_file_list=skill_file_list,
        )
        try:
            c = backend.complete(
                prompt, "",
                model=model,
                no_tools=False,
                agentic=True,
                skip_permissions=True,
                timeout=300,
            )
            return c.text.strip()
        except Exception as exc:  # noqa: BLE001
            return f"Could not generate improvement prompt: {exc}"

    # Fallback for SDK backend: pass report contents directly
    skill_text = load_skill_text(cfg)
    reports_text = _collect_reports_text(report_paths)
    prompt = _FALLBACK_TEMPLATE.format(
        skill_name=cfg.name,
        skill_text=skill_text,
        reports_text=reports_text,
    )
    try:
        c = backend.complete(prompt, _FALLBACK_SYSTEM, model=model, no_tools=True, timeout=180)
        return c.text.strip()
    except Exception as exc:  # noqa: BLE001
        return f"Could not generate improvement prompt: {exc}"


def write_improvement_prompt(prompt_text: str, out_dir: Path) -> Path:
    path = out_dir / "improvement_prompt.md"
    path.write_text(prompt_text + "\n")
    return path
