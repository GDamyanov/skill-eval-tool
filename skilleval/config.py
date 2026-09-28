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
    """A shell command run as a quality gate on a generated artifact."""
    name: str                       # label shown in the report (e.g. "typescript", "tests")
    command: list[str]              # argv, e.g. ["yarn", "ts"]
    cwd: str = "."                  # relative to repo_root (supports {package} placeholder)
    timeout: int = 600
    # If the generated output must be written to a file before the command runs,
    # set write_to (path relative to repo_root, supports {task_id}/{package} etc).
    write_to: str | None = None


@dataclass
class Config:
    # --- identity ------------------------------------------------------------
    name: str = "skill-eval"

    # --- skill ---------------------------------------------------------------
    # Files that make up the skill. Globs are supported. They are concatenated
    # (in sorted order) and injected into the model's system prompt for the
    # "skill" variant only.
    skill_files: list[str] = field(default_factory=list)
    skill_base_dir: str = "."       # base dir the skill_files globs resolve against

    # --- prompts -------------------------------------------------------------
    system_base: str = (
        "You are an expert assistant. Answer the task precisely and return only "
        "what is asked, with no extra commentary."
    )
    # Optional text prepended to the skill content when injected.
    skill_preamble: str = "Follow these skill instructions strictly:\n\n"

    # --- tasks ---------------------------------------------------------------
    tasks_file: str = "tasks.jsonl"

    # --- extraction ----------------------------------------------------------
    # How to pull the artifact out of the model's text reply before saving /
    # quality-checking. "codeblock" grabs the first fenced code block; "raw"
    # keeps the whole reply.
    extract: str = "codeblock"      # "codeblock" | "raw"
    artifact_ext: str = "txt"       # file extension for saved artifacts

    # --- execution -----------------------------------------------------------
    repo_root: str = "."            # base dir for check commands & write_to paths
    backend: str = "claude-cli"     # "claude-cli" | "anthropic-sdk"
    model: str | None = None

    # --- agentic (realistic developer) mode ---------------------------------
    # Working directory the agent runs in (relative to repo_root; supports
    # placeholders like {package}). Defaults to repo_root when empty.
    agent_cwd: str = "."
    # Where the agent is told to create the artifact (relative to repo_root;
    # supports {id}/{package}). We read this file back as the artifact and then
    # remove it to keep the working tree clean.
    agent_write_to: str | None = None
    # Extra instructions appended to the task telling the agent how to behave
    # like a real developer (explore first, follow conventions, verify).
    agent_instructions: str = (
        "Work like a developer in this repository. First explore existing tests "
        "and the relevant component to learn the conventions, then write the file. "
        "Return nothing except doing the work on disk."
    )
    # Let the agent edit files without interactive approval (headless eval).
    agent_skip_permissions: bool = True

    # --- quality gates -------------------------------------------------------
    checks: list[CheckCommand] = field(default_factory=list)

    # --- judge ---------------------------------------------------------------
    judge_rubric: str | None = None  # if set, enables --judge scoring 1-10

    # --- reviewer ------------------------------------------------------------
    # Model used for --review. Falls back to `model` (or the backend default).
    reviewer_model: str | None = None

    # --- plugin eval ---------------------------------------------------------
    # Default evals directory for --claude-skill-eval and --full.
    # Relative to the config file's directory. Overridden by --evals-dir.
    plugin_evals_dir: str | None = None

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

        checks = [CheckCommand(**c) for c in data.pop("checks", [])]
        known = {f.name for f in cls.__dataclass_fields__.values()}
        data = {k: v for k, v in data.items() if k in known}
        cfg = cls(**data, checks=checks)  # type: ignore[arg-type]
        cfg._config_dir = cfg_path.parent
        return cfg
