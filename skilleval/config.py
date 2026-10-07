"""Configuration model for a skill evaluation.

Everything project-specific lives in a YAML/JSON config file so the tool itself
stays generic. See `examples/` for ready-to-use configs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml  # optional; only needed for .yaml configs
except ImportError:  # pragma: no cover
    yaml = None


@dataclass
class CheckCommand:
    """A shell command run as a post-check after each plugin eval case."""
    name: str                       # label shown in the report (e.g. "lint")
    command: list[str]              # argv, e.g. ["yarn", "lint"]
    cwd: str = "."                  # relative to repo_root
    timeout: int = 300
    write_to: str | None = None     # write artifact here before running (repo-root-relative)


@dataclass
class Config:
    # --- identity ------------------------------------------------------------
    name: str = "skill-eval"

    # --- skill ---------------------------------------------------------------
    # Files that make up the skill. Globs are supported. They are concatenated
    # (in sorted order) and injected into the model's system prompt.
    skill_files: list[str] = field(default_factory=list)
    skill_base_dir: str = "."       # base dir the skill_files globs resolve against

    # Optional text prepended to the skill content when injected.
    skill_preamble: str = "Follow these skill instructions strictly:\n\n"

    # --- execution -----------------------------------------------------------
    repo_root: str = "."            # base dir for post-check commands
    backend: str = "claude-cli"     # "claude-cli" | "anthropic-sdk"
    model: str | None = None
    agent_skip_permissions: bool = True

    # --- reviewer ------------------------------------------------------------
    # Model used for --review. Falls back to `model` (or the backend default).
    reviewer_model: str | None = None

    # --- plugin eval ---------------------------------------------------------
    # Default evals directory for --claude-skill-eval and --full.
    # Relative to the config file's directory. Overridden by --evals-dir.
    plugin_evals_dir: str | None = None

    # --- post-checks (run after each plugin eval case) -----------------------
    post_checks: list[CheckCommand] = field(default_factory=list)

    # --- resolved base path (set at load time) -------------------------------
    _config_dir: Path = field(default_factory=lambda: Path("."))

    # ------------------------------------------------------------------ paths
    def resolve(self, p: str) -> Path:
        """Resolve a path from the config relative to the config file's dir."""
        path = Path(p)
        return path if path.is_absolute() else (self._config_dir / path).resolve()

    @property
    def repo_root_path(self) -> Path:
        return self.resolve(self.repo_root)

    # --------------------------------------------------------------- loading
    @classmethod
    def load(cls, path: str | Path) -> "Config":
        cfg_path = Path(path).resolve()
        text = cfg_path.read_text()
        if cfg_path.suffix in (".yaml", ".yml"):
            if yaml is None:
                raise RuntimeError("PyYAML not installed. `pip install pyyaml` or use a .json config.")
            data: dict[str, Any] = yaml.safe_load(text) or {}
        else:
            data = json.loads(text)

        known = {f.name for f in cls.__dataclass_fields__.values()}
        post_checks = [CheckCommand(**c) for c in data.pop("post_checks", [])]
        data = {k: v for k, v in data.items() if k in known}
        cfg = cls(**data, post_checks=post_checks)  # type: ignore[arg-type]
        cfg._config_dir = cfg_path.parent
        return cfg
