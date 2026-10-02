# skilleval

Measure whether a Claude Code skill (`SKILL.md`) actually improves agent output quality, and by how much it changes token usage and cost. Fully config-driven — nothing is hard-coded for a specific skill or repository.

---

## How it works

The core mode runs every task **twice**, with the agent working inside the real repository — reading files, following conventions, writing output to disk — exactly like a developer would:

| Variant | Skill in context |
|---------|-----------------|
| `control` | no skill |
| `skill` | `SKILL.md` + reference files injected into system prompt |

Everything else is identical. Any measurable difference is attributable to the skill. The agent has full tool access and operates in the real repo, so the evaluation reflects how the skill actually behaves in practice.

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
# Full report (recommended) — runs all 5 evaluation dimensions
skilleval -c examples/ui5-cypress/config.agentic.yaml --full

# Plain A/B — tokens and cost only
skilleval -c config.yaml

# A/B with LLM judge + shell quality gates
skilleval -c config.yaml --judge --check
```

---

## Global flags

These flags apply to **every** command:

| Flag | Default | Description |
|------|---------|-------------|
| `--config / -c` | _(required)_ | Path to the config YAML or JSON file. |
| `--backend` | from config | Override backend: `claude-cli` or `anthropic-sdk`. |
| `--model` | from config | Override model (e.g. `claude-haiku-4-5-20251001`). |
| `--out` | `results/<timestamp>-<mode>/` | Write all output files to this directory instead of the default. |
| `--parallel N` | `1` | Run N agents simultaneously, each in its own isolated git worktree. |

---

## Commands

### `--full` — full synthesis report _(recommended)_

Runs all five evaluation dimensions in order and writes a single `full_report.md` with a weighted health score, token/cost breakdown, per-command results table, and LLM-synthesized top findings. Also writes an `improvement_prompt.md` generated from all sub-reports combined.

```bash
skilleval -c config.yaml --full

# With parallel agents and multiple plugin-eval runs
skilleval -c config.yaml --full --parallel 3 --plugin-eval-runs 2

# Disable phase cache (always re-run everything)
skilleval -c config.yaml --full --no-cache

# Override plugin-eval cache TTL
skilleval -c config.yaml --full --cache-ttl 120
```

**What runs (in order):**

| Phase | What it does |
|-------|-------------|
| 1. Best-practices audit | Scores SKILL.md against Anthropic's 21 official guidelines |
| 2. Skill review | LLM rubric review: structure, content, clarity, anti-patterns |
| 3. Skill verify | Probes whether the skill actually loads into the model's context |
| 4. A/B eval | 1 run per task, with LLM judge + rule compliance checks |
| 5. Plugin eval | `claude plugin eval` against your `evals/` cases |

**Health score** is a weighted average across all dimensions:

| Dimension | Weight |
|-----------|--------|
| Best-practices audit | 2× |
| Skill review | 2× |
| A/B judge score | 2× |
| Plugin eval pass rate | 2× |
| Rule compliance | 1× |
| Skill verify | 1× |

**Additional flags:**

| Flag | Default | Description |
|------|---------|-------------|
| `--plugin-eval-runs N` | `1` | Runs per eval case in phase 5. |
| `--no-cache` | off | Re-run all phases even if a cached result exists. |
| `--cache-ttl MINS` | `60` | Max age (minutes) of a cached plugin-eval result. Other phases are re-run only when SKILL.md changes. |

**Output directory:** `results/<timestamp>-full/`

**Files written:**
- `full_report.md` — health score, all dimension scores, top 3 findings
- `best_practices_audit.md` — phase 1 detail
- `skill_review.md` — phase 2 detail
- `report.md` — phase 4 A/B detail
- `plugin_eval_report.md` — phase 5 detail
- `raw_results.jsonl` — all A/B run records
- `post_check_logs/` — post-check execution logs
- `improvement_prompt.md` — ready-to-use prompt to improve SKILL.md

---

### A/B evaluation _(default command)_

Runs every task with and without the skill, measures tokens, cost, latency, and optionally quality. After each run, **post-checks** verify the agent's changes against the real repo (e.g. running the Cypress test file the agent edited). The target file is detected automatically from the agent's trace.

```bash
# Tokens and cost only
skilleval -c config.yaml

# With LLM judge (requires judge_rubric in config)
skilleval -c config.yaml --judge

# With shell quality gates (requires checks in config)
skilleval -c config.yaml --check

# With per-rule compliance check
skilleval -c config.yaml --check-rules

# All of the above
skilleval -c config.yaml --judge --check --check-rules

# Custom tasks file
skilleval -c config.yaml --tasks my-tasks.jsonl

# 4 agents in parallel
skilleval -c config.yaml --parallel 4
```

**Flags:**

| Flag | Default | Description |
|------|---------|-------------|
| `--judge` | off | Score each artifact 1–10 with a blind LLM judge. Requires `judge_rubric` in config. |
| `--check` | off | Run `checks` commands from config on each artifact. |
| `--check-rules` | off | Extract rules from SKILL.md with an LLM, then verify each artifact against them. |
| `--tasks` | from config | Override `tasks_file` for this run. |

**Output directory:** `results/<timestamp>-ab/` (or `-ab-judge` / `-ab-check`)

**Files written:**
- `raw_results.jsonl` — one record per run: tokens, cost, latency, checks, judge score, rule checks, friction signals
- `report.md` — summary table, deltas, per-task breakdown
- `artifacts/` — every generated artifact (`<id>.<variant>.<ext>`)
- `post_check_logs/` — per-run post-check execution logs
- `improvement_prompt.md` — prompt to improve SKILL.md based on findings

**Console output:**

```
=== Skill effectiveness summary ===
| variant | n | avg in-tok | avg out-tok | avg cost $ | avg s | skill verified | judge /10 |
|---------|---|------------|-------------|------------|-------|----------------|-----------|
| control | 3 |     228180 |        1061 |    0.70046 |  63.3 | n/a            |       7.5 |
| skill   | 3 |     287003 |        1520 |    0.88381 |  55.5 | 100%           |       5.5 |

=== Delta (skill − control) ===
  output tokens : +459
  cost / task   : +0.18335 USD
  latency       : -7.8 s
  judge /10     : 7.5 -> 5.5
```

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

The model used for `--review` defaults to `model` from config (or the backend default). Set `reviewer_model` in config to use a different model for review.

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

# Run cases in parallel (delegates to claude plugin eval's --concurrency flag)
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

### `--check-rules`

Extracts every concrete rule from SKILL.md using an LLM, then checks each generated artifact for per-rule compliance. Always combined with the A/B run.

```bash
skilleval -c config.yaml --check-rules
```

Rule names and pass/fail results are stored per-run in `raw_results.jsonl` under the `rule_checks` field and appear in `report.md`.

---

## Parallel execution

Use `--parallel N` to run N agents simultaneously.

```bash
# 4 control+skill agents running at the same time
skilleval -c config.yaml --parallel 4
```

- **A/B eval**: creates N git worktrees (capped at `tasks × 2`, since that is the total run count). Each worktree is assigned to exactly one running agent at a time via an internal queue — two agents never share a worktree concurrently. Worktrees are torn down when all runs finish.
- **`--claude-skill-eval`**: the sandbox is already isolated per case by `claude plugin eval`; `--parallel N` is passed through as `--concurrency N`. No worktrees needed.
- **`--full`**: applies to both A/B (phase 4) and plugin eval (phase 5).
- Results are sorted back into the original serial order before writing `raw_results.jsonl`, so reports are deterministic.

Requirement: the repository pointed to by `repo_root` must be a git repository for worktrees to be created. If it is not, parallel runs fall back to running sequentially.

---

## Post-checks

Post-checks are shell commands defined in `post_checks` (config) that run automatically after **every** A/B run and after `--claude-skill-eval`. They verify that the agent's actual changes compile, pass tests, or satisfy other real-world quality gates.

### Target file detection

The target file (the file the agent edited) is detected from the agent's trace using three strategies, tried in order:

1. **Structured response** — agent ends its response with `FILE: <path>` followed by the complete file content (patched into the case prompt automatically when `post_checks` are configured).
2. **Write/Edit tool call** — last `Write` or `Edit` tool call in the trace.
3. **Read heuristic** — most-read `.cy.` file in the trace that matches a **pre-existing** repo file.

Only files that **existed before the agent ran** are accepted as targets. Files the agent created from scratch are rejected — this prevents the agent from inventing a test file with a different name than intended.

If no target is resolved and the post-check command contains `{target_basename}`, the post-check is **skipped** (logged as `skipped: no target file resolved`).

### Cleanup

After post-checks run, all agent changes are reverted: tracked files are restored with `git checkout --`, untracked files created by the agent are deleted. The repository is always left in its original state.

### Configuration

```yaml
post_checks:
  - name: cypress
    command: ["yarn", "test:cypress:single", "cypress/specs/{target_basename}"]
    cwd: "packages/main"
    timeout: 1200
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Label shown in the report and used as the log file suffix. |
| `command` | yes | Command as an argv list. Supports `{target_basename}` (filename only), `{target_file}` (repo-relative path), and any task field placeholder. |
| `cwd` | no | Working directory, relative to `repo_root`. Defaults to `.`. Supports task field placeholders. |
| `timeout` | no | Seconds before the command is killed. Default: `600`. |

**Log files:** `post_check_logs/<id>.<variant>.<check>.log` (A/B) or `post_check_logs/<case>.<check>.log` (plugin eval).

---

## Configuration reference

All paths in the config are resolved **relative to the config file's directory**.

### Minimal config

```yaml
name: my-skill

skill_base_dir: "./my-skill"
skill_files:
  - "SKILL.md"

tasks_file: "tasks.jsonl"
backend: claude-cli

agent_cwd: "."
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
                                   # system-prompt block for the "skill" variant.

# ─── Prompts ─────────────────────────────────────────────────────────────────
system_base: >                     # System prompt sent to BOTH variants.
  You are a developer in this repository. Complete the assigned task.

skill_preamble: "Follow these skill instructions strictly:\n\n"
                                   # Prepended to the skill text when it is
                                   # injected. Default shown above.

# ─── Tasks ───────────────────────────────────────────────────────────────────
tasks_file: "tasks.jsonl"          # One JSON object per line (see below).

# ─── Artifact extraction ─────────────────────────────────────────────────────
extract: codeblock                 # How to extract the artifact from the
                                   # model's reply:
                                   #   "codeblock" — first fenced code block
                                   #   "raw"       — entire reply
artifact_ext: "txt"                # File extension for saved artifacts.

# ─── Execution ───────────────────────────────────────────────────────────────
repo_root: "../my-repo"            # Base directory for agent_cwd, check paths,
                                   # and write_to paths. Must be a git repo for
                                   # --parallel to create worktrees.
backend: claude-cli                # "claude-cli" | "anthropic-sdk"
model: null                        # Override model (e.g. "claude-haiku-4-5-20251001").
                                   # Null = use the backend's default.

# ─── Agentic mode ────────────────────────────────────────────────────────────
# The agent runs inside the real repository with full tool access, exactly like
# a developer. This is the only supported mode for A/B eval.

agent_cwd: "packages/{package}"   # Working directory for the agent. Relative
                                   # to repo_root. Supports task field
                                   # placeholders (e.g. {package}, {id}).

agent_instructions: >              # Extra text appended to every task prompt.
  Complete the assigned task by    # Use it to set behavioural expectations:
  making the necessary changes     # explore first, follow conventions, etc.
  directly in the repository. Do
  not add commentary — just do
  the work on disk.

agent_skip_permissions: true       # Skip interactive permission prompts.
                                   # Required for headless/unattended eval.

# agent_write_to is intentionally omitted here. When absent, the agent decides
# which file to edit and the target is detected from the trace automatically.
# Set it only when you want to force the agent to create a specific new file:
#
# agent_write_to: "packages/{package}/cypress/specs/{id}.cy.tsx"

# ─── Quality gates (--check) ─────────────────────────────────────────────────
# Run only when --check is passed. The artifact is written to write_to before
# the command runs, then the original file is restored.
checks:
  - name: typescript
    command: ["yarn", "ts"]
    cwd: "."
    timeout: 600
    write_to: "packages/{package}/cypress/specs/{id}.cy.tsx"
                                   # Where to write the artifact before running
                                   # the command. Relative to repo_root.
                                   # Supports task field placeholders.

  - name: cypress
    command: ["yarn", "test:cypress:single", "cypress/specs/{target_basename}"]
    cwd: "packages/{package}"
    timeout: 1200
    # write_to omitted — target is detected from trace automatically.

# ─── Post-checks (automatic after every A/B and plugin eval run) ─────────────
# Run automatically — no flag needed. Target file detected from agent trace.
# {target_basename} is replaced with the filename the agent actually edited.
# The post-check is skipped if no target file is detected.
post_checks:
  - name: cypress
    command: ["yarn", "test:cypress:single", "cypress/specs/{target_basename}"]
    cwd: "packages/main"
    timeout: 1200

# ─── LLM judge (--judge) ─────────────────────────────────────────────────────
# Use {skill} as a placeholder — it is replaced at runtime with the current
# skill text so the judge always scores against the actual SKILL.md.
judge_rubric: |
  You are a strict senior reviewer. Score the output from 1 to 10 based on
  how well it follows the rules in the skill below.

  {skill}

  Respond with ONLY: {"score": <1-10>, "rationale": "<one sentence>"}

# ─── Reviewer ────────────────────────────────────────────────────────────────
reviewer_model: null               # Model for --review. Falls back to `model`
                                   # or the backend default if null.

# ─── Plugin eval ─────────────────────────────────────────────────────────────
plugin_evals_dir: "evals"          # Directory containing eval cases for
                                   # --claude-skill-eval and --full (phase 5).
                                   # Relative to this config file.
```

---

## Tasks file

One JSON object per line. `id` and `prompt` are required. Any extra keys (like `package`) become placeholders you can reference in `agent_cwd`, `checks`, and `post_checks` command paths using `{key}`.

```jsonl
{"id": "button-click", "package": "main", "prompt": "Write a Cypress component test for the Button component that tests a basic click interaction."}
{"id": "input-change", "package": "main", "prompt": "Write a Cypress component test for the Input component that tests the live-change event."}
```

If `id` is missing, the tool assigns `task-1`, `task-2`, etc. If `prompt` is missing, the task is rejected with an error.

---

## Eval case structure (`--claude-skill-eval`)

Place eval cases under `evals/<case-name>/` next to your config file.

```
evals/
  uses-attribute-selectors/
    case.yaml
    scaffold.sh        ← runs before the agent to set up the sandbox
  uses-real-events/
    case.yaml
    scaffold.sh
```

### `case.yaml`

```yaml
schema_version: "1.0"
name: uses-attribute-selectors

context:
  scaffold_script: scaffold.sh    # Path relative to this case directory.

execution:
  max_turns: 10
  timeout_seconds: 900
  allowed_tools: [Skill, Read, Glob, Grep, Write, Agent]
  prompt: |
    Write a Cypress component test for a ui5-checkbox component that
    toggles the checkbox and verifies the checked state changes.

graders:
  - name: attribute-selectors
    type: llm
    criteria: |
      PASS if the generated test uses attribute selectors like [ui5-checkbox].
      FAIL if it uses bare tag selectors like cy.get("ui5-checkbox").

  - name: skill-fired
    type: tool_used
    tool: Skill
    input_match: '"skill"\s*:\s*"(?:[\w-]+:)?cypress-test-writer"'
```

**`case.yaml` fields:**

| Field | Description |
|-------|-------------|
| `schema_version` | Must be `"1.0"`. |
| `name` | Case identifier. Used in the report and log file names. |
| `context.scaffold_script` | Path to the scaffold script, **relative to this `case.yaml` file**. No `../` allowed. |
| `execution.max_turns` | Maximum agent turns before the run is cut off. |
| `execution.timeout_seconds` | Timeout for the entire agent run. |
| `execution.allowed_tools` | Tools the agent is allowed to use. Always include `Skill` to test skill injection. |
| `execution.prompt` | The task prompt sent to the agent. |
| `graders` | List of graders — see below. |

**Grader types:**

| Type | Fields | Description |
|------|--------|-------------|
| `llm` | `criteria` | An LLM grades the output against the criteria text. |
| `tool_used` | `tool`, `input_match` (optional regex) | Passes if the agent used the named tool, optionally with input matching the regex. |

### `scaffold.sh`

Runs before the agent inside the eval sandbox to set up required files (e.g. copy repo packages):

```bash
#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="/path/to/your/repo"
rsync -a --exclude='node_modules/' "$REPO_ROOT/packages/main/" packages/main/
```

All cases that need the same setup share an identical `scaffold.sh`. Each case keeps its own copy — this is intentional; `claude plugin eval` resolves the path relative to each `case.yaml`.

---

## Output directory naming

Every command writes results to `results/<timestamp>-<mode>/` next to the config file, unless `--out` is used.

| Command | Directory suffix |
|---------|-----------------|
| `--full` | `-full` |
| `--audit` | `-audit` |
| `--review` | `-review` |
| `--verify` | `-verify` |
| `--check-rules` | `-check-rules` |
| `--claude-skill-eval` | `-plugin-eval` |
| `--judge` (A/B) | `-ab-judge` |
| `--check` (A/B) | `-ab-check` |
| A/B only | `-ab` |
| `--out <path>` | uses the given path directly |

---

## Using it in another repository

1. Copy `examples/generic/` into (or next to) your target repo.
2. Edit `config.yaml`: set `skill_base_dir`, `skill_files`, `repo_root`, `agent_cwd`.
3. Fill `tasks.jsonl` with representative prompts (5+ tasks, varied).
4. (Optional) Add `post_checks`, a `judge_rubric`, and `evals/` cases.
5. Run:

```bash
skilleval -c config.yaml --full
```

No code changes needed — the tool is entirely driven by the config.

---

## Notes

- **Agentic mode only.** The agent always works inside the real repository — reads files, follows conventions, makes changes to disk. This is the only supported A/B mode because it reflects how the skill is actually used in practice.
- **Target file detection.** For post-checks and `--check`, the target file is detected from the agent's trace (structured response → Write/Edit tool call → most-read matching file). Only pre-existing files are accepted — invented files are rejected. See [Post-checks](#post-checks) for details.
- **Clean working tree.** After each run (A/B and plugin eval), all agent changes are reverted via `git checkout`. For parallel runs, each agent has its own worktree so changes never cross-contaminate.
- **Phase cache (`--full`).** Phases 1–3 (audit, review, verify) are cached by skill content hash and reused in subsequent `--full` runs unless `--no-cache` is passed. Phase 4 (A/B) always re-runs. Phase 5 (plugin eval) uses a time-based TTL (`--cache-ttl`, default 60 min).
- **Judge objectivity.** The `{skill}` placeholder in `judge_rubric` ensures the judge always scores against the current `SKILL.md` — it never drifts from the actual skill content.
- **Sample size.** Use ≥ 5 tasks for stable A/B numbers. With `--parallel N` you can run all tasks simultaneously to save wall-clock time.
- **`agent_write_to`.** Omitting this field (recommended) lets the agent decide which file to edit, with the target detected from the trace. Set it only when you need to force a specific new file path.

---

## License

MIT
