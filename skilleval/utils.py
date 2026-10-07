"""Shared utilities used across skilleval modules."""

from __future__ import annotations

from pathlib import Path
from .config import Config


def load_skill_text(cfg: Config) -> str:
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


def write_log(path: Path, command: list[str], cwd: Path, returncode: int,
              stdout: str, stderr: str) -> None:
    lines = [
        f"command: {' '.join(command)}",
        f"cwd:     {cwd}",
        f"exit:    {returncode}",
        "",
    ]
    if stdout.strip():
        lines += ["--- stdout ---", stdout.rstrip(), ""]
    if stderr.strip():
        lines += ["--- stderr ---", stderr.rstrip(), ""]
    path.write_text("\n".join(lines))
