"""Incremental phase cache for --full and --claude-skill-eval.

Caches the results of static analysis phases (audit, review, verify, plugin_eval)
keyed on a SHA-256 hash of the skill file contents.  A/B evaluation is never
cached because it is the empirical measurement users want fresh.

Cache file: <out_dir>/.cache.json
Schema:
  {
    "skill_hash": "<hex>",
    "written_at": <epoch float>,
    "phases": {
      "audit":       { ...AuditResult fields ... },
      "review":      { ...ReviewResult fields ... },
      "verify":      { ...VerifyResult fields ... },
      "plugin_eval": { ...PluginEvalResult fields ... }
    }
  }
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import Config


# ---------------------------------------------------------------------------
# Skill hash
# ---------------------------------------------------------------------------

def skill_hash(cfg: Config) -> str:
    """Return a SHA-256 hex digest of all skill file contents (sorted by path)."""
    base = cfg.resolve(cfg.skill_base_dir)
    paths: list[Path] = []
    for pattern in cfg.skill_files:
        p = Path(pattern)
        if p.is_absolute():
            paths.extend(sorted(Path(p.anchor).glob(str(p.relative_to(p.anchor)))))
        else:
            paths.extend(sorted(base.glob(pattern)))

    seen: set[Path] = set()
    unique: list[Path] = []
    for f in paths:
        if f.is_file() and f not in seen:
            seen.add(f)
            unique.append(f)

    h = hashlib.sha256()
    for f in sorted(unique):
        h.update(f.read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Find most recent prior run directory
# ---------------------------------------------------------------------------

def find_latest_run(config_dir: Path) -> Path | None:
    """Return the most recently created results/<timestamp>-*/ directory, or None."""
    results_dir = config_dir / "results"
    if not results_dir.is_dir():
        return None
    candidates = [d for d in results_dir.iterdir() if d.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda d: d.name)


# ---------------------------------------------------------------------------
# PhaseCache
# ---------------------------------------------------------------------------

_CACHE_FILE = ".cache.json"


class PhaseCache:
    """Read/write phase results keyed by skill hash.

    Instantiate with the *current* run's out_dir and the skill hash computed
    before the run starts.  Call load_from(prior_dir) to pull an existing cache
    from a previous run directory.
    """

    def __init__(self, current_hash: str) -> None:
        self._hash = current_hash
        self._phases: dict[str, Any] = {}
        self._written_at: float = 0.0
        self._loaded = False

    # -- loading ---------------------------------------------------------------

    @classmethod
    def load_from(cls, prior_dir: Path, current_hash: str) -> "PhaseCache":
        """Try to load a cache from a prior run directory.

        Returns a populated PhaseCache if the hash matches, otherwise an empty one.
        """
        cache = cls(current_hash)
        cache_file = prior_dir / _CACHE_FILE
        if not cache_file.exists():
            return cache
        try:
            data = json.loads(cache_file.read_text())
        except Exception:
            return cache
        if data.get("skill_hash") != current_hash:
            return cache
        cache._phases = data.get("phases", {})
        cache._written_at = float(data.get("written_at", 0))
        cache._loaded = True
        return cache

    # -- querying --------------------------------------------------------------

    def has(self, phase: str) -> bool:
        return phase in self._phases

    def age_seconds(self) -> float:
        """Seconds since the cache was written (0 if unknown)."""
        if not self._written_at:
            return 0.0
        return time.time() - self._written_at

    def load(self, phase: str) -> dict:
        return self._phases.get(phase, {})

    # -- saving ----------------------------------------------------------------

    def save(self, phase: str, data: Any) -> None:
        if hasattr(data, "__dataclass_fields__"):
            data = asdict(data)
        self._phases[phase] = data

    def flush(self, out_dir: Path) -> None:
        """Write the current cache state to out_dir/.cache.json."""
        out_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "skill_hash": self._hash,
            "written_at": time.time(),
            "phases": self._phases,
        }
        (out_dir / _CACHE_FILE).write_text(json.dumps(payload, indent=2))
