# skilleval

Evaluate whether a Claude Code skill (`SKILL.md`) is well-written, correctly loaded, and actually improves agent output quality. Fully config-driven — nothing is hard-coded for a specific skill or repository.

---

## How it works

`skilleval` runs four complementary evaluation dimensions against a skill:

| Dimension | What it measures |
|-----------|-----------------|
| **Audit** | Static quality score against Anthropic's 21 best-practices guidelines |
| **Review** | LLM rubric review: structure, content, clarity, anti-patterns |
| **Verify** | Probes whether the skill actually loads into the model's context |
| **Plugin eval** | `claude plugin eval` against your `evals/` cases with graders |

Run them individually or all at once with `--full`, which synthesizes a weighted health score and generates an improvement prompt.

---

## Install

```bash
git clone https://github.com/your-org/skill-eval
cd skill-eval && pip install -e ".[all]"
```

Extras: `[yaml]` for YAML configs · `[table]` for pretty console output · `[sdk]` for the Anthropic API backend · `[all]` installs everything.

---

## Backends

| Backend | Requires | Notes |
|---------|----------|-------|
| `claude-cli` _(default)_ | `claude` CLI on `PATH` | Inherits your existing auth/proxy — no API key needed. Supports skills as tools (`plugin_dir`). |
| `anthropic-sdk` | `ANTHROPIC_API_KEY` env var | Direct API; pricing in `backends.py`. Does **not** support `plugin_dir` / skill-as-tool. |

---

## Quick start

```bash
# Full report (recommended) — runs all 4 evaluation dimensions
skilleval -c examples/ui5-knowledge-base/config.agentic.yaml --full

# Individual dimensions
skilleval -c config.yaml --audit
skilleval -c config.yaml --review
skilleval -c config.yaml --verify
skilleval -c config.yaml --claude-skill-eval
```

---

## Global flags

| Flag | Default | Description |
|------|---------|-------------|
| `--config / -c` | _(required)_ | Path to the config YAML or JSON file. |
| `--backend` | from config | Override backend: `claude-cli` or `anthropic-sdk`. |
| `--model` | from config | Override model (e.g. `claude-haiku-4-5-20251001`). |
| `--out` | `results/<timestamp>-<mode>/` | Write all output files here instead of the default. |
| `--parallel N` | `1` | Run N plugin eval cases in parallel. |

---

## Commands

### `--full` — full synthesis report _(recommended)_

Runs all four evaluation dimensions in order and writes a single `full_report.md` with a weighted health score, per-dimension scores, and LLM-synthesized top findings. Also writes an `improvement_prompt.md` generated from all sub-reports combined.

```bash
skilleval -c config.yaml --full

# With parallel plugin-eval agents and multiple runs per case
skilleval -c config.yaml --full --parallel 3 --plugin-eval-runs 2

# Disable phase cache (always re-run everything)
skilleval -c config.yaml --full --no-cache

# Override plugin-eval cache TTL
skilleval -c config.yaml --full --cache-ttl 120
```

**What runs (in order):**

| Phase | What it does |
|-------|-------------|
| 1/4. Best-practices audit | Scores SKILL.md against Anthropic's 21 official guidelines |
| 2/4. Skill review | LLM rubric review: structure, content, clarity, anti-patterns |
| 3/4. Skill verify | Probes whether the skill actually loads into the model's context |
| 4/4. Plugin eval | `claude plugin eval` against your `evals/` cases |

**Health score** is a weighted average:

| Dimension | Weight |
|-----------|--------|
| Best-practices audit | 2× |
| Skill review | 2× |
| Plugin eval pass rate | 2× |
| Skill verify | 1× |

**Additional flags:**

| Flag | Default | Description |
|------|---------|-------------|
| `--plugin-eval-runs N` | `1` | Runs per eval case in phase 4. |
| `--no-cache` | off | Re-run all phases even if a cached result exists. |
| `--cache-ttl MINS` | `60` | Max age (minutes) of a cached plugin-eval result. Audit/review/verify are re-run only when SKILL.md changes. |

**Output directory:** `results/<timestamp>-full/`

**Files written:**
- `full_report.md` — health score, all dimension scores, top 3 actionable findings
- `best_practices_audit.md` — phase 1 detail
- `skill_review.md` — phase 2 detail
- `plugin_eval_report.md` — phase 4 detail
- `post_check_logs/` — post-check execution logs
- `improvement_prompt.md` — ready-to-use prompt to improve SKILL.md

---

### `--audit` — best-practices audit

Scores SKILL.md against Anthropic's official best-practices guidelines: 21 rules across 6 categories (Core principles, Skill structure, Content guidelines, Workflows, Evaluation, Scripts).

```bash
skilleval -c config.yaml --audit
```

**Output directory:** `results/<timestamp>-audit/`

**Files written:** `best_practices_audit.md`, `improvement_prompt.md`

**Console output:**

```
=== Best-practices audit: my-skill ===
Score: 83.3%  (15/18 rules passed)
Failures:
  ❌ [Core principles] SKILL.md is concise
     SKILL.md contains exhaustive inline examples that bloat the main file.
  ❌ [Skill structure] SKILL.md body is under 500 lines
     SKILL.md exceeds 500 lines.
```

---

### `--review` — skill quality review

Sends the skill files to the model with a structured rubric and gets a quality report with an overall rating and categorised issues.

```bash
skilleval -c config.yaml --review
```

**Output directory:** `results/<timestamp>-review/`

**Files written:** `skill_review.md`, `improvement_prompt.md`

Ratings: **Pass** · **Needs Improvement** · **Needs Major Revision**

Issues are categorised as Critical / Major / Minor and cover: structure, description quality, content quality, progressive disclosure, workflow clarity, and anti-patterns.

Set `reviewer_model` in config to use a different model for review (falls back to `model` or the backend default).

---

### `--verify` — skill context verification

Probes the model with the skill in its context and confirms the skill was actually injected. Useful in CI to catch misconfigured `skill_base_dir` or `skill_files` paths before a full eval.

```bash
skilleval -c config.yaml --verify
```

Exits `0` if verified, `1` if not. No report file is written.

---

### `--claude-skill-eval` — plugin eval integration

Runs `claude plugin eval` against the eval cases in `evals/` next to the config. Writes a structured report with per-case scores, grader results, token/cost metrics, friction analysis, and post-check verification results.

```bash
skilleval -c config.yaml --claude-skill-eval

# More runs per case for stable scores
skilleval -c config.yaml --claude-skill-eval --plugin-eval-runs 3

# Run cases in parallel
skilleval -c config.yaml --claude-skill-eval --parallel 3
```

**Flags:**

| Flag | Default | Description |
|------|---------|-------------|
| `--plugin-eval-runs N` | `1` | Runs per eval case. More runs = more stable scores. |
| `--parallel N` | `1` | Passed as `--concurrency N` to `claude plugin eval`. |

**Output directory:** `results/<timestamp>-plugin-eval/`

**Files written:** `plugin_eval_report.md`, `post_check_logs/`, `improvement_prompt.md`

Requires eval cases in `evals/<case-name>/case.yaml` format — see [Eval case structure](#eval-case-structure) below.

---

## Post-checks

Post-checks are shell commands defined in `post_checks` (config) that run automatically after each `--claude-skill-eval` case. They verify that the agent's actual changes compile, pass tests, or satisfy other real-world quality gates.

```yaml
post_checks:
  - name: lint
    command: ["yarn", "lint"]
    cwd: "."
    timeout: 300
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Label shown in the report and used as the log file suffix. |
| `command` | yes | Command as an argv list. |
| `cwd` | no | Working directory, relative to `repo_root`. Defaults to `.`. |
| `timeout` | no | Seconds before the command is killed. Default: `300`. |

**Log files:** `post_check_logs/<case>.<check>.log`

---

## Configuration reference

All paths in the config are resolved **relative to the config file's directory**.

### Minimal config

```yaml
name: my-skill

skill_base_dir: "./my-skill"
skill_files:
  - "SKILL.md"

backend: claude-cli
agent_skip_permissions: true
```

### Full config reference

```yaml
# ─── Identity ────────────────────────────────────────────────────────────────
name: my-skill                     # Used in report headings and file names.

# ─── Skill ───────────────────────────────────────────────────────────────────
skill_base_dir: "../my-skill"      # Directory that contains the skill files.
                                   # Relative to this config file.
skill_files:                       # Files that make up the skill. Globs are
  - "SKILL.md"                     # supported. Multiple files are concatenated
  - "references/*.md"              # in sorted order and injected as a single
                                   # system-prompt block.

skill_preamble: "Follow these skill instructions strictly:\n\n"
                                   # Prepended to the skill text when injected.

# ─── Execution ───────────────────────────────────────────────────────────────
repo_root: "../my-repo"            # Base directory for post-check commands.
backend: claude-cli                # "claude-cli" | "anthropic-sdk"
model: null                        # Override model (e.g. "claude-haiku-4-5-20251001").
agent_skip_permissions: true       # Skip interactive permission prompts.

# ─── Reviewer ────────────────────────────────────────────────────────────────
reviewer_model: null               # Model for --review. Falls back to `model`
                                   # or the backend default if null.

# ─── Plugin eval ─────────────────────────────────────────────────────────────
plugin_evals_dir: "evals"          # Directory containing eval cases for
                                   # --claude-skill-eval and --full.
                                   # Relative to this config file.

# ─── Post-checks (run after each plugin eval case) ───────────────────────────
post_checks:
  - name: lint
    command: ["yarn", "lint"]
    cwd: "."
    timeout: 300
```

---

## Eval case structure (`--claude-skill-eval`)

Place eval cases under `evals/<case-name>/` next to your config file.

```
evals/
  create-gantt-chart/
    case.yaml
    scaffold.sh        ← runs before the agent to set up the sandbox
```

### `case.yaml`

```yaml
schema_version: "1.0"
name: create-gantt-chart

context:
  scaffold_script: scaffold.sh    # Path relative to this case directory.

execution:
  max_turns: 30
  timeout_seconds: 1800
  allowed_tools: [Skill, Read, Glob, Grep, Write, Edit, Agent]
  prompt: |
    Invoke the ui5-knowledge-base skill before doing any work. Then scaffold
    and implement a new ui5-gantt-chart component following all project conventions.

graders:
  - name: skill-fired
    type: tool_used
    tool: Skill
    input_match: '"skill"\s*:\s*"(?:[\w-]+:)?ui5-knowledge-base"'

  - name: enum-as-template-literal
    type: llm
    criteria: |
      PASS if enum property types are declared as template literal types
      (e.g. `${GanttChartViewMode}`) with a type-only import and a string
      literal default. FAIL otherwise.
```

**`case.yaml` fields:**

| Field | Description |
|-------|-------------|
| `schema_version` | Must be `"1.0"`. |
| `name` | Case identifier. Used in the report and log file names. |
| `context.scaffold_script` | Path to the scaffold script, relative to this `case.yaml`. No `../` allowed. |
| `execution.max_turns` | Maximum agent turns before the run is cut off. |
| `execution.timeout_seconds` | Timeout for the entire agent run. |
| `execution.allowed_tools` | Tools the agent may use. Always include `Skill` to test skill injection. |
| `execution.prompt` | The task prompt sent to the agent. |
| `graders` | List of graders — see below. |

**Grader types:**

| Type | Fields | Description |
|------|--------|-------------|
| `llm` | `criteria` | An LLM grades the output against the criteria text. |
| `tool_used` | `tool`, `input_match` (optional regex) | Passes if the agent used the named tool, optionally with input matching the regex. |

### `scaffold.sh`

Runs before the agent inside the eval sandbox to set up required files:

```bash
#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="/path/to/your/repo"
rsync -a --exclude='node_modules/' "$REPO_ROOT/packages/main/" packages/main/
```

---

## Output directory naming

Every command writes to `results/<timestamp>-<mode>/` next to the config, unless `--out` is used.

| Command | Directory suffix |
|---------|-----------------|
| `--full` | `-full` |
| `--audit` | `-audit` |
| `--review` | `-review` |
| `--verify` | `-verify` |
| `--claude-skill-eval` | `-plugin-eval` |
| `--out <path>` | uses the given path directly |

---

## Using it in another repository

1. Copy `examples/ui5-knowledge-base/` (or `examples/ui5-cypress/`) as a starting point.
2. Edit `config.agentic.yaml`: set `skill_base_dir`, `skill_files`, `repo_root`.
3. Add eval cases under `evals/<case-name>/case.yaml` with graders that target your skill's rules.
4. (Optional) Add `post_checks` for quality gates that run after each case.
5. Run:

```bash
skilleval -c config.agentic.yaml --full --parallel 3
```

No code changes needed — the tool is entirely driven by the config.

---

## Notes

- **Phase cache (`--full`).** Audit, review, and verify are cached by skill content hash and reused in subsequent `--full` runs unless `--no-cache` is passed. Plugin eval uses a time-based TTL (`--cache-ttl`, default 60 min).
- **Parallel plugin eval.** `--parallel N` is passed through as `--concurrency N` to `claude plugin eval`. The sandbox is already isolated per case, so no worktrees are needed.
- **Reviewer model.** Set `reviewer_model` in config to use a cheaper/faster model for `--review` while keeping a stronger model for plugin eval.

---

## License

MIT
