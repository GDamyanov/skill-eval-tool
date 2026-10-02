"""Generate a ready-to-use improvement prompt from an evaluation report."""

from __future__ import annotations

from pathlib import Path

from .backends import Backend
from .config import Config
from .core import load_skill_text


_IMPROVE_SYSTEM = """\
You are a skill improvement assistant. Given an evaluation report for a Claude Code skill
and the current skill text, generate a concrete, ready-to-use prompt that the skill author
can give to Claude Code to improve their SKILL.md based on the findings.

The prompt must:
- Reference the specific issues found (by name/section)
- Give Claude clear instructions on what to change and why
- Include the skill file path so Claude knows what to edit
- Be self-contained (include enough context that Claude can act without the report)
- Be actionable in a single Claude Code session

Return ONLY the prompt text — no preamble, no explanation, no markdown fences.\
"""

_IMPROVE_TEMPLATE = """\
EVALUATION REPORT:
{report_text}

CURRENT SKILL TEXT:
{skill_text}\
"""


def generate_improvement_prompt(
    report_text: str,
    cfg: Config,
    backend: Backend,
) -> str:
    skill_text = load_skill_text(cfg)
    prompt = _IMPROVE_TEMPLATE.format(report_text=report_text, skill_text=skill_text)
    model = cfg.reviewer_model or cfg.model
    try:
        c = backend.complete(prompt, _IMPROVE_SYSTEM, model=model, no_tools=True, timeout=120)
        return c.text.strip()
    except Exception as exc:  # noqa: BLE001
        return f"Could not generate improvement prompt: {exc}"


def write_improvement_prompt(prompt_text: str, out_dir: Path) -> Path:
    path = out_dir / "improvement_prompt.md"
    path.write_text(prompt_text + "\n")
    return path
