"""Model backends. Each returns a normalized dict of text + usage + cost.

Two backends are provided:
  * claude-cli    — drives the local `claude` CLI (inherits proxy/auth config)
  * anthropic-sdk — uses the Anthropic Python SDK (needs ANTHROPIC_API_KEY)

Both expose the same `complete(...)` signature so the evaluator is agnostic.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass


@dataclass
class Completion:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""


class Backend:
    def complete(self, prompt: str, system: str, *, model: str | None,
                 no_tools: bool, timeout: int = 600,
                 cwd: str | None = None, agentic: bool = False,
                 skip_permissions: bool = True,
                 plugin_dir: str | None = None) -> Completion:
        raise NotImplementedError

    def available(self) -> bool:
        return True

# --------------------------------------------------------------------------- #
# Claude CLI
# --------------------------------------------------------------------------- #

class ClaudeCLIBackend(Backend):
    def __init__(self, binary: str = "claude"):
        self.binary = binary

    def available(self) -> bool:
        try:
            subprocess.run([self.binary, "--version"], capture_output=True, timeout=15)
            return True
        except Exception:  # noqa: BLE001
            return False

    def complete(self, prompt: str, system: str, *, model: str | None,
                 no_tools: bool, timeout: int = 600,
                 cwd: str | None = None, agentic: bool = False,
                 skip_permissions: bool = True,
                 plugin_dir: str | None = None) -> Completion:
        cmd = [self.binary, "-p", prompt, "--output-format", "json"]
        if model:
            cmd += ["--model", model]
        if agentic:
            if skip_permissions:
                cmd += ["--dangerously-skip-permissions"]
        elif no_tools:
            cmd += ["--tools", ""]
        if system:
            cmd += ["--append-system-prompt", system]
        if plugin_dir:
            cmd += ["--plugin-dir", plugin_dir]

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              cwd=cwd)
        # The CLI emits a JSON result object even on non-zero exit (e.g. max turns).
        line = next((l for l in reversed(proc.stdout.splitlines()) if l.strip()), "")
        if not line:
            raise RuntimeError(
                f"claude exited {proc.returncode}, no JSON output: "
                f"{proc.stderr.strip()[:400]}"
            )
        data = json.loads(line)
        u = data.get("usage", {})
        return Completion(
            text=data.get("result", ""),
            input_tokens=u.get("input_tokens", 0),
            output_tokens=u.get("output_tokens", 0),
            cache_read_tokens=u.get("cache_read_input_tokens", 0),
            cache_write_tokens=u.get("cache_creation_input_tokens", 0),
            cost_usd=round(float(data.get("total_cost_usd", 0.0)), 6),
            model=next(iter(data.get("modelUsage", {})), "") or (model or ""),
        )


# --------------------------------------------------------------------------- #
# Anthropic SDK
# --------------------------------------------------------------------------- #

# USD per 1M tokens. Override per model as pricing changes.
SDK_PRICES = {
    "default": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
}


class AnthropicSDKBackend(Backend):
    def __init__(self, default_model: str = "claude-sonnet-4-5"):
        try:
            from anthropic import Anthropic
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("anthropic not installed. `pip install anthropic`.") from exc
        self._client = Anthropic()
        self.default_model = default_model

    def available(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY"))

    def _cost(self, model: str, u) -> float:
        p = SDK_PRICES.get(model, SDK_PRICES["default"])
        cr = getattr(u, "cache_read_input_tokens", 0) or 0
        cw = getattr(u, "cache_creation_input_tokens", 0) or 0
        return (u.input_tokens * p["input"] + u.output_tokens * p["output"]
                + cr * p["cache_read"] + cw * p["cache_write"]) / 1_000_000

    def complete(self, prompt: str, system: str, *, model: str | None,
                 no_tools: bool, timeout: int = 600,
                 cwd: str | None = None, agentic: bool = False,
                 skip_permissions: bool = True,
                 plugin_dir: str | None = None) -> Completion:
        if plugin_dir:
            raise NotImplementedError(
                "plugin_dir (skill-as-tool) is not supported by AnthropicSDKBackend — "
                "use backend: claude-cli instead."
            )
        mdl = model or self.default_model
        blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}] if system else []
        resp = self._client.messages.create(
            model=mdl,
            max_tokens=4096,
            temperature=0,
            system=blocks,
            messages=[{"role": "user", "content": prompt}],
        )
        u = resp.usage
        return Completion(
            text="".join(b.text for b in resp.content if b.type == "text"),
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            cost_usd=round(self._cost(mdl, u), 6),
            model=mdl,
        )


def make_backend(name: str) -> Backend:
    if name == "claude-cli":
        return ClaudeCLIBackend()
    if name == "anthropic-sdk":
        return AnthropicSDKBackend()
    raise ValueError(f"Unknown backend: {name!r}. Use 'claude-cli' or 'anthropic-sdk'.")
