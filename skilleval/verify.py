"""Skill context verification: probe the model to confirm the skill is injected."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .backends import Backend
from .config import Config

PROBE_TOKEN = "SKILLEVAL_VERIFY_OK"

PROBE_PROMPT = (
    "You have skill instructions in your system prompt. "
    f"To confirm you received them, reply with exactly this token and nothing else: {PROBE_TOKEN}"
)


def _load_skill_text(cfg: Config) -> str:
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


@dataclass
class VerifyResult:
    skill_name: str
    verified: bool
    probe_response: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""
    error: str = ""


def run_verify(cfg: Config, backend: Backend) -> VerifyResult:
    skill_text = _load_skill_text(cfg)
    system = f"{cfg.skill_preamble}{skill_text}"
    result = VerifyResult(skill_name=cfg.name, verified=False)
    try:
        c = backend.complete(
            PROBE_PROMPT,
            system,
            model=cfg.model,
            no_tools=True,
            timeout=60,
        )
        result.probe_response = c.text.strip()
        result.verified = PROBE_TOKEN in result.probe_response
        result.input_tokens = c.input_tokens
        result.output_tokens = c.output_tokens
        result.cost_usd = c.cost_usd
        result.model = c.model
    except Exception as exc:  # noqa: BLE001
        result.error = str(exc)
    return result


def print_verify_summary(result: VerifyResult) -> None:
    print("\n=== Skill context verification ===")
    if result.error:
        print(f"ERROR: {result.error}")
        return
    status = "PASS" if result.verified else "FAIL"
    print(f"Status : {status}")
    print(f"Model  : {result.model}")
    print(f"Tokens : {result.input_tokens} in / {result.output_tokens} out  (${result.cost_usd:.5f})")
    if not result.verified:
        print(f"Response: {result.probe_response[:200]!r}")
