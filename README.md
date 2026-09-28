# skilleval

Measure whether an AI-agent _skill_ (a `SKILL.md` file) actually improves output quality, and by how much it changes token usage and cost.

---

## How it works

The core mode runs every task **twice**, with the agent working inside the real repository — reading files, following conventions, writing output to disk — exactly like a developer would:

| Variant | Skill in system prompt |
|---------|------------------------|
| `control` | ❌ no |
| `skill` | ✅ yes (`SKILL.md` + any reference files) |

Everything else is identical. Each pair is repeated N times because model output varies. Any measurable difference is attributable to the skill.

The tool is **fully config-driven** — nothing is hard-coded for a specific skill or repository.

---

## Install

```bash
# from source
git clone https://github.com/your-org/skill-eval
cd skill-eval && pip install -e ".[all]"
```

Extras: `[yaml]` for YAML configs, `[table]` for the pretty summary, `[sdk]` for the Anthropic API backend. `[all]` installs everything.

### Backends

| Backend | Needs | Notes |
|---------|-------|-------|
| `claude-cli` (default) | `claude` CLI on PATH | Inherits your existing auth/proxy — no API key needed. |
| `anthropic-sdk` | `ANTHROPIC_API_KEY` env var | Direct API; pricing set in `backends.py`. |

---

## Commands

All commands share these flags:

| Flag | Description |
|------|-------------|
| `--config / -c` | Path to config YAML/JSON (required) |
| `--backend` | Override backend: `claude-cli` or `anthropic-sdk` |
| `--model` | Override model from config |
| `--out` | Output directory (default: `results/<timestamp>-<mode>/` next to the config) |

### `--full` — full synthesis report _(recommended starting point)_

Runs all five evaluation dimensions in sequence and writes a single synthesised `full_report.md` with a health score, token/cost metrics, per-command results table, and LLM-generated top findings.

```bash
skilleval -c examples/ui5-cypress/config.agentic.yaml --full
```

Output directory: `results/<timestamp>-full/`

Runs in order: audit → review → verify → A/B eval (judge + check-rules) → plugin eval.
Each sub-command also writes its own report to the same directory.

---

### A/B evaluation (default)

Runs every task with and without the skill, compares tokens, cost, latency, and optionally quality.

```bash
# Basic A/B — tokens and cost only
skilleval -c config.yaml --repeats 3

# With LLM judge (requires judge_rubric in config)
skilleval -c config.yaml --repeats 3 --judge

# With shell quality gates (requires checks in config)
skilleval -c config.yaml --repeats 3 --check

# With per-rule compliance check
skilleval -c config.yaml --repeats 3 --check-rules

# All of the above
skilleval -c config.yaml --repeats 3 --check --judge --check-rules
```

Output directory: `results/<timestamp>-ab/` (or `-ab-judge` / `-ab-check`)

Files written:
- `raw_results.jsonl` — one record per run (tokens, cost, latency, checks, judge score, rule checks)
- `report.md` — summary table, delta, per-task breakdown, friction signals
- `artifacts/` — every generated artifact

Console output:

```
=== Skill effectiveness summary ===
| variant | n | avg in-tok | avg out-tok | avg cost $ | avg s | skill verified | judge /10 |
|---------|---|------------|-------------|------------|-------|----------------|-----------|
| control | 6 |     228180 |        1061 |    0.70046 |  63.3 | n/a            |       7.5 |
| skill   | 6 |     287003 |        1520 |    0.88381 |  55.5 | 100%           |       5.5 |

=== Delta (skill − control) ===
  output tokens : +459
  cost / task   : +0.18335 USD
  latency       : -7.8 s
  judge /10     : 7.5 -> 5.5
```

---

### `--audit` — best-practices audit

Scores the skill against Anthropic's [official best-practices guidelines](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices) (21 rules across 6 categories).

```bash
skilleval -c config.yaml --audit
```

Output directory: `results/<timestamp>-audit/`

Files written: `best_practices_audit.md`

Console output:

```
=== Best-practices audit: my-skill ===
Score: 83.3%  (15/18 rules passed)
Failures:
  ❌ [Core principles] SKILL.md is concise
     SKILL.md contains exhaustive inline examples that bloat the main file.
  ❌ [Skill structure] SKILL.md body is under 500 lines
     SKILL.md exceeds 500 lines.
```

The report groups rules by category (Core principles, Skill structure, Content guidelines, Workflows, Evaluation, Scripts), shows a summary table, and gives the full guideline text + finding for every failure.

---

### `--review` — skill quality review

Sends the skill files to the model with a structured rubric and gets a quality report with rating and issue breakdown.

```bash
skilleval -c config.yaml --review
```

Output directory: `results/<timestamp>-review/`

Files written: `skill_review.md`

Ratings: **Pass** / **Needs Improvement** / **Needs Major Revision**

Issues are categorised as Critical / Major / Minor and cover: structure, description quality, content quality, progressive disclosure, workflow clarity, and anti-patterns.

---

### `--verify` — skill context verification

Probes the model with the skill in its system prompt to confirm the skill was actually injected into context.

```bash
skilleval -c config.yaml --verify
```

Exits with code `0` if verified, `1` if not. Useful in CI to confirm the skill loads correctly.

---

### `--claude-skill-eval` — plugin eval integration

Runs `claude plugin eval` against the eval cases in `evals/` next to the config and writes a structured report.

```bash
skilleval -c config.yaml --claude-skill-eval

# More runs per case for stable scores
skilleval -c config.yaml --claude-skill-eval --plugin-eval-runs 3

# Custom evals directory
skilleval -c config.yaml --claude-skill-eval --evals-dir path/to/evals
```

Output directory: `results/<timestamp>-plugin-eval/`

Files written: `plugin_eval_report.md`

Requires eval cases in `evals/<case-name>/case.yaml` format (see [Eval case structure](#eval-case-structure) below).

---

### `--check-rules`

Extracts all rules from the skill with an LLM call, then checks each generated artifact for per-rule compliance. Combined with the default A/B run.

```bash
skilleval -c config.yaml --repeats 2 --check-rules
```

Results are stored per-run in `raw_results.jsonl` under the `rule_checks` field.

---

## Configuration

A config is a YAML (or JSON) file. All paths resolve relative to the config file's directory.

### Minimal config

```yaml
name: my-skill

skill_base_dir: "./my-skill-dir"   # directory containing SKILL.md
skill_files:
  - "SKILL.md"

tasks_file: "tasks.jsonl"
backend: claude-cli

# Agentic mode — required fields
agent_cwd: "."
agent_write_to: "output/{id}.txt"
agent_skip_permissions: true
```

### Full config reference

```yaml
name: my-skill

# --- Skill ------------------------------------------------------------------
skill_base_dir: "../my-skill"      # relative to this config file
skill_files:                       # globs allowed; files are concatenated
  - "SKILL.md"
  - "references/*.md"

# --- Prompts ----------------------------------------------------------------
system_base: >                     # framing for BOTH variants
  You are an expert assistant. Return only the requested artifact.

skill_preamble: "Follow these skill instructions strictly:\n\n"

# --- Tasks ------------------------------------------------------------------
tasks_file: "tasks.jsonl"          # one JSON object per line

# --- Artifact extraction ----------------------------------------------------
extract: codeblock                 # "codeblock" | "raw"
artifact_ext: "txt"                # extension for saved artifacts

# --- Execution --------------------------------------------------------------
repo_root: "../my-repo"            # base for checks and write_to paths
backend: claude-cli                # "claude-cli" | "anthropic-sdk"
model: null                        # override model (e.g. "claude-opus-5")

# --- Agentic mode -----------------------------------------------------------
# For realistic developer-style evaluation where the agent has full tool access
# and writes files to disk (like a real developer would).
agent_cwd: "packages/{package}"    # CWD for the agent (relative to repo_root)
agent_write_to: "packages/{package}/cypress/specs/{id}.cy.tsx"
agent_instructions: >              # appended to the task prompt
  Complete the assigned task by writing the spec file at the given path.
  Do not add commentary — just do the work on disk.
agent_skip_permissions: true       # headless eval — no interactive approval

# --- Quality gates (--check) ------------------------------------------------
checks:
  - name: typescript
    command: ["yarn", "ts"]
    cwd: "."
    timeout: 600
    write_to: "packages/{package}/cypress/specs/{id}.cy.tsx"

  - name: cypress
    command: ["yarn", "test:cypress:single", "cypress/specs/{id}.cy.tsx"]
    cwd: "packages/{package}"
    timeout: 1200
    write_to: "packages/{package}/cypress/specs/{id}.cy.tsx"

# --- LLM judge (--judge) ----------------------------------------------------
# Use {skill} as a placeholder — it is replaced at runtime with the actual
# skill text so the judge always scores against the current SKILL.md rules.
judge_rubric: |
  You are a strict senior reviewer. Score the output from 1 to 10 based on
  how well it follows the rules in the skill below.

  {skill}

  Respond with ONLY: {"score": <1-10>, "rationale": "<one sentence>"}
```

### Tasks file

One JSON object per line. `id` and `prompt` are required. Any extra keys become
placeholders usable in check paths and `agent_write_to`:

```jsonl
{"id": "button-click", "package": "ui5-button", "prompt": "Write a Cypress component test for the Button component that tests a basic click interaction."}
{"id": "input-change", "package": "ui5-input",  "prompt": "Write a Cypress component test for the Input component that tests the live-change event."}
```

---

## Eval case structure (`--claude-skill-eval`)

Place eval cases under `evals/<case-name>/` next to your config file.

```
evals/
  uses-attribute-selectors/
    case.yaml
    scaffold.sh       ← runs before the agent; sets up the repo sandbox
  async-api-pattern/
    case.yaml
    scaffold.sh
```

### `case.yaml`

```yaml
schema_version: "1.0"
name: uses-attribute-selectors

context:
  scaffold_script: scaffold.sh    # path relative to this case directory

execution:
  max_turns: 10
  timeout_seconds: 900
  allowed_tools: [Skill, Read, Glob, Grep, Write, Agent]
  prompt: |
    Write a Cypress component test for the Button component.
    Create the file at: packages/ui5-button/cypress/specs/Button.cy.tsx
```

### `scaffold.sh`

Runs before the agent in the eval sandbox to set up required files (e.g. copy repo packages):

```bash
#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="/path/to/your/repo"
rsync -a --exclude='node_modules/' "$REPO_ROOT/packages/" packages/
```

---

## Output directory naming

Every command writes results to `results/<timestamp>-<mode>/` next to the config file:

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
| default A/B | `-ab` |
| `--out <path>` | uses the given path directly |

---

## Using it in another repository

1. Copy `examples/generic/` into (or next to) your target repo.
2. Set `skill_base_dir` / `skill_files` to your skill.
3. Fill `tasks.jsonl` with representative prompts.
4. (Optional) Add `checks`, a `judge_rubric`, and `evals/` cases.
5. Run the full report:

```bash
skilleval -c config.yaml --full
```

No code changes needed — the tool is fully driven by the config.

---

## Notes

- **Agentic mode:** the agent always has full tool access and works inside the real repository — reads files, follows conventions, writes artifacts to disk. This is the only supported mode for A/B eval because it reflects how the skill is actually used in practice.
- **Judge objectivity:** the `{skill}` placeholder in `judge_rubric` ensures the judge always scores against the current SKILL.md rules — it never drifts from the actual skill content.
- **Sample size:** use ≥ 3 repeats and ≥ 5 tasks for stable A/B numbers.
- **Clean working tree:** quality gate checks write temp files into the repo and remove them after — run on a clean working tree.
- **Scaffold and `case.yaml`:** the `scaffold_script` path must be relative to its `case.yaml` file (no `../`). Schema version must be `"1.0"`.

---

## License

MIT
