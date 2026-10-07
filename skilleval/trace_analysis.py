"""Trace analysis: parse a claude CLI trace.jsonl and surface detailed friction points.

Captures per-turn detail: thinking snapshots, tool inputs/outputs, strategy
pivots, subagent launches, permission denials, loops, and the final output.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #

@dataclass
class ToolCall:
    turn: int
    name: str
    input_summary: str    # truncated JSON of the input
    result_summary: str   # truncated result text
    denied: bool = False


@dataclass
class TurnSnapshot:
    turn: int
    thinking: str = ""          # first thinking block (truncated)
    text: str = ""              # first assistant text (truncated)
    tool_calls: list[ToolCall] = field(default_factory=list)
    is_pivot: bool = False      # agent changed strategy this turn
    subagents_launched: int = 0


@dataclass
class FrictionPoint:
    kind: str    # "max_turns"|"permission_denied"|"tool_loop"|"empty_repo"|"error"|"high_turns"|"skill_not_fired"|"strategy_pivot"|"repeated_reads"|"skill_ignored"
    message: str
    turn: int = 0


@dataclass
class TraceAnalysis:
    path: str
    total_turns: int = 0
    tool_calls: list[str] = field(default_factory=list)   # ordered tool names
    turns: list[TurnSnapshot] = field(default_factory=list)
    friction: list[FrictionPoint] = field(default_factory=list)
    skill_fired: bool = False
    skill_name: str = ""
    final_text: str = ""
    stop_reason: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    first_write_turn: int = 0      # turn when agent first wrote/edited a file (0 = never)
    files_read: list[str] = field(default_factory=list)   # all file paths read, in order
    model: str = ""                # model id from modelUsage in the result event
    error: str = ""


# --------------------------------------------------------------------------- #
# Pivot detection keywords
# --------------------------------------------------------------------------- #

_PIVOT_PHRASES = [
    "directory is empty",
    "no files found",
    "from scratch",
    "write from scratch",
    "write the test directly",
    "write a standalone",
    "write a canonical",
    "i'll write",
    "let me write",
    "i need to write",
    "without referencing",
    "based on the skill",
    "based on my knowledge",
    "i don't have access",
    "can't read",
    "cannot read",
    "permission denied",
    "not relevant here",
]


def _is_pivot(text: str) -> bool:
    low = text.lower()
    return any(phrase in low for phrase in _PIVOT_PHRASES)


def _tool_input_summary(inp: dict) -> str:
    """Short summary of a tool input dict."""
    if not inp:
        return ""
    # For common tools, show the most relevant field
    if "pattern" in inp and "path" in inp:
        return f"pattern={inp['pattern']!r} path={inp.get('path','')}"
    if "file_path" in inp:
        return f"file={inp['file_path']}"
    if "skill" in inp:
        return f"skill={inp['skill']!r}"
    if "prompt" in inp:
        return inp["prompt"][:120]
    return json.dumps(inp)[:120]


def _tool_result_summary(content) -> str:
    """Short summary of a tool result content."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content[:200]
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text", "")[:120])
        return " ".join(p for p in parts if p)[:200]
    return str(content)[:200]


# --------------------------------------------------------------------------- #
# Main analyser
# --------------------------------------------------------------------------- #

def analyse_trace(path: str | Path) -> TraceAnalysis:
    p = Path(path)
    result = TraceAnalysis(path=str(path))

    if not p.exists():
        result.error = f"Trace file not found: {path}"
        return result

    try:
        events = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    except Exception as exc:  # noqa: BLE001
        result.error = f"Failed to parse trace: {exc}"
        return result

    turn = 0
    tool_counts: dict[str, int] = {}
    # pending tool inputs: tool_use_id -> ToolCall (filled in once we see the result)
    pending_tools: dict[str, ToolCall] = {}
    current_snapshot: TurnSnapshot | None = None
    denied_tool_ids: set[str] = set()
    # For tracking reads: path -> list of turns it was read at
    file_read_turns: dict[str, list[int]] = {}
    # Turns after skill fired where agent read files (signals skill was ignored)
    skill_fired_turn: int = 0
    reads_after_skill: int = 0

    for event in events:
        t = event.get("type", "")
        st = event.get("subtype", "")

        # ------------------------------------------------------------------ user turn
        if t == "user":
            turn += 1
            if current_snapshot:
                result.turns.append(current_snapshot)
            current_snapshot = TurnSnapshot(turn=turn)

            # Tool results arrive in user messages
            for block in event.get("message", {}).get("content", []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    tid = block.get("tool_use_id", "")
                    res_text = _tool_result_summary(block.get("content"))
                    if tid in pending_tools:
                        pending_tools[tid].result_summary = res_text
                        if current_snapshot:
                            current_snapshot.tool_calls.append(pending_tools.pop(tid))

        # ------------------------------------------------------------------ assistant
        elif t == "assistant":
            snap = current_snapshot or TurnSnapshot(turn=turn)
            msg = event.get("message", {})

            # Collect token counts from assistant message usage
            usage = msg.get("usage", {})
            result.input_tokens += usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
            result.output_tokens += usage.get("output_tokens", 0)
            result.thinking_tokens += usage.get("output_tokens_details", {}).get("thinking_tokens", 0)

            for block in msg.get("content", []):
                if not isinstance(block, dict):
                    continue
                bt = block.get("type", "")

                if bt == "thinking":
                    thinking = block.get("thinking", "").strip()
                    if thinking and not snap.thinking:
                        snap.thinking = thinking[:300]

                elif bt == "text":
                    text = block.get("text", "").strip()
                    if text:
                        if not snap.text:
                            snap.text = text[:300]
                        # Prefer text blocks that contain code; fall back to last text
                        if "```" in text:
                            result.final_text = text
                        elif not result.final_text or "```" not in result.final_text:
                            result.final_text = text
                        if _is_pivot(text) or _is_pivot(snap.thinking):
                            snap.is_pivot = True

                elif bt == "tool_use":
                    name = block.get("name", "unknown")
                    tid = block.get("id", "")
                    inp = block.get("input", {})
                    inp_summary = _tool_input_summary(inp)

                    # Track skill fires
                    if name == "Skill":
                        result.skill_fired = True
                        result.skill_name = inp.get("skill", "")
                        skill_fired_turn = turn

                    # Track subagent launches
                    if name == "Agent":
                        snap.subagents_launched += 1

                    # Track file reads
                    if name == "Read":
                        fpath = inp.get("file_path", "")
                        if fpath:
                            result.files_read.append(fpath)
                            file_read_turns.setdefault(fpath, []).append(turn)
                            if skill_fired_turn > 0:
                                reads_after_skill += 1

                    # Track first write turn
                    if name in ("Write", "Edit", "NotebookEdit") and result.first_write_turn == 0:
                        result.first_write_turn = turn

                    tc = ToolCall(turn=turn, name=name, input_summary=inp_summary,
                                  result_summary="", denied=(tid in denied_tool_ids))
                    pending_tools[tid] = tc
                    result.tool_calls.append(name)
                    tool_counts[name] = tool_counts.get(name, 0) + 1

        # ------------------------------------------------------------------ system events
        elif t == "system":
            if st == "permission_denied":
                tool_name = event.get("tool_name", "?")
                tid = event.get("tool_use_id", "")
                denied_tool_ids.add(tid)
                if tid in pending_tools:
                    pending_tools[tid].denied = True
                result.friction.append(FrictionPoint(
                    kind="permission_denied",
                    message=f"'{tool_name}' denied at turn {turn}",
                    turn=turn,
                ))

        # ------------------------------------------------------------------ result
        elif t == "result":
            result.stop_reason = event.get("stop_reason", "")
            # Prefer result-level cost; fall back to accumulated assistant usage
            cost = event.get("total_cost_usd")
            if cost is not None:
                result.cost_usd = float(cost)
            usage = event.get("usage", {})
            if usage:
                result.input_tokens = (
                    usage.get("input_tokens", 0)
                    + usage.get("cache_read_input_tokens", 0)
                    + usage.get("cache_creation_input_tokens", 0)
                )
                result.output_tokens = usage.get("output_tokens", 0)
                result.thinking_tokens = usage.get("output_tokens_details", {}).get("thinking_tokens", 0)
            # Extract model id from modelUsage keys (e.g. {"claude-haiku-4-5-...": {...}})
            model_usage = event.get("modelUsage", {})
            if model_usage:
                result.model = next(iter(model_usage), "")
            if result.stop_reason == "error_max_turns":
                result.friction.append(FrictionPoint(
                    kind="max_turns",
                    message="Agent hit the max turn limit — task was never completed.",
                    turn=turn,
                ))
            elif result.stop_reason == "error":
                result.friction.append(FrictionPoint(
                    kind="error",
                    message=f"Agent exited with error: {event.get('subtype', '')}",
                    turn=turn,
                ))

    if current_snapshot:
        result.turns.append(current_snapshot)

    result.total_turns = turn

    # ------------------------------------------------------------------ post-pass analysis

    # Skill not fired
    if not result.skill_fired:
        result.friction.append(FrictionPoint(
            kind="skill_not_fired",
            message="Skill was never invoked — agent worked without skill guidance.",
            turn=0,
        ))

    # Skill fired but agent kept reading many files afterwards — possible skill gap
    if result.skill_fired and reads_after_skill >= 5:
        result.friction.append(FrictionPoint(
            kind="skill_ignored",
            message=f"Skill fired at turn {skill_fired_turn} but agent read {reads_after_skill} more files after — skill may not provide enough guidance.",
            turn=skill_fired_turn,
        ))

    # Repeated reads — same file read more than once
    for fpath, turns_list in file_read_turns.items():
        if len(turns_list) > 1:
            result.friction.append(FrictionPoint(
                kind="repeated_reads",
                message=f"'{Path(fpath).name}' read {len(turns_list)}x (turns {', '.join(str(t) for t in turns_list)}) — agent re-read instead of relying on skill.",
                turn=turns_list[0],
            ))

    # Late first write — agent took many turns before producing output
    if result.first_write_turn > 5:
        result.friction.append(FrictionPoint(
            kind="high_turns",
            message=f"First file write at turn {result.first_write_turn} — agent spent many turns exploring before acting.",
            turn=result.first_write_turn,
        ))

    # Empty repo discovery (agent found nothing and pivoted)
    pivot_turns = [s for s in result.turns if s.is_pivot]
    for pt in pivot_turns:
        result.friction.append(FrictionPoint(
            kind="strategy_pivot",
            message=f"Turn {pt.turn}: agent changed strategy — \"{pt.text[:120]}\"",
            turn=pt.turn,
        ))

    # Tool loops — same tool called ≥3 times consecutively
    streak_tool, streak_count = None, 0
    for tool in result.tool_calls:
        if tool == streak_tool:
            streak_count += 1
            if streak_count == 3:
                result.friction.append(FrictionPoint(
                    kind="tool_loop",
                    message=f"'{tool}' called {streak_count}+ times in a row — search loop.",
                    turn=0,
                ))
        else:
            streak_tool, streak_count = tool, 1

    # High turn count
    if result.total_turns > 8:
        result.friction.append(FrictionPoint(
            kind="high_turns",
            message=f"Agent used {result.total_turns} turns — high effort for this task.",
            turn=0,
        ))

    return result


# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #

def extract_target_file(
    path: str | Path,
    file_pattern: str = ".cy.",
    repo_root: str | Path | None = None,
    allowed_paths: set | None = None,
) -> str | None:
    """Return the relative path of the target file the agent worked on.

    Strategy (first match wins):
      1. ``FILE: packages/...`` marker in final_text (structured response format).
      2. File path comment inside the generated code block in final_text
         e.g. ``// packages/main/cypress/specs/Button.cy.tsx``
         (only trusted if the file exists in repo_root)
      3. Most-frequently Read file matching file_pattern from sandbox paths.
         Tie-breaking: prefer non-visuals/ paths, then non-variant names.

    If ``allowed_paths`` is given (set of absolute Path objects), only candidates
    whose resolved absolute path is in that set are returned.

    Sandbox paths (/private/tmp/e-XXXXX/home/cwd/...) are normalised to relative paths.
    Returns None if nothing is found.
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        events = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    except Exception:  # noqa: BLE001
        return None

    import re as _re
    from collections import Counter

    final_text = ""
    counts: Counter = Counter()
    write_candidates: list[str] = []  # file_path from Write/Edit calls matching file_pattern

    for event in events:
        if event.get("type") != "assistant":
            continue
        for block in event.get("message", {}).get("content", []):
            if not isinstance(block, dict):
                continue
            bt = block.get("type", "")
            if bt == "text":
                text = block.get("text", "")
                if "```" in text:
                    final_text = text
                elif not final_text:
                    final_text = text
            elif bt == "tool_use":
                tool_name = block.get("name", "")
                fp = block.get("input", {}).get("file_path", "")
                if tool_name == "Read" and file_pattern in fp:
                    # Sandbox path: /private/tmp/.../home/cwd/<rel>
                    m = _re.search(r"/home/cwd/(.+)", fp)
                    if m:
                        counts[m.group(1)] += 1
                    # Absolute path inside repo_root: strip repo_root prefix
                    elif repo_root:
                        try:
                            rel = str(Path(fp).relative_to(Path(repo_root)))
                            counts[rel] += 1
                        except ValueError:
                            pass
                elif tool_name in ("Write", "Edit") and file_pattern in fp:
                    # Track files the agent actually wrote — these are the most reliable
                    m = _re.search(r"/home/cwd/(.+)", fp)
                    if m:
                        write_candidates.append(m.group(1))
                    elif repo_root:
                        try:
                            rel = str(Path(fp).relative_to(Path(repo_root)))
                            write_candidates.append(rel)
                        except ValueError:
                            write_candidates.append(fp)  # keep as-is; may be absolute worktree path
                    else:
                        write_candidates.append(fp)

    def _normalize_candidate(raw: str) -> tuple[str | None, Path | None]:
        """Return (rel_path, abs_path) normalized to repo_root, or (None, None).

        Handles three cases:
        - Relative path → joined with repo_root
        - Absolute path under repo_root → made relative
        - Absolute worktree path (not under repo_root) → matched by filename
          against allowed_paths, or stripped to relative by matching the suffix
          that corresponds to a plausible repo-relative path (packages/...).
        """
        p_raw = Path(raw)
        if p_raw.is_absolute():
            if repo_root:
                try:
                    rel = str(p_raw.relative_to(Path(repo_root)))
                    return rel, (Path(repo_root) / rel).resolve()
                except ValueError:
                    # Absolute path not under repo_root — likely a worktree path.
                    # Walk up the path parts trying increasingly long suffixes
                    # until one resolves to an existing file under repo_root.
                    parts = p_raw.parts
                    for i in range(1, len(parts)):
                        rel = str(Path(*parts[i:]))
                        candidate = (Path(repo_root) / rel).resolve()
                        if candidate.exists():
                            return rel, candidate
                    # Fall back: match by filename against allowed_paths
                    if allowed_paths:
                        for allowed in allowed_paths:
                            if allowed.name == p_raw.name:
                                try:
                                    rel = str(allowed.relative_to(Path(repo_root)))
                                    return rel, allowed.resolve()
                                except ValueError:
                                    pass
                    # Last resort: use the longest suffix that looks plausible
                    # (i.e. doesn't start with a system dir like /usr /home /private)
                    _SYS = {"usr", "home", "private", "tmp", "var", "etc", "opt"}
                    for i in range(1, len(parts)):
                        if parts[i] not in _SYS:
                            rel = str(Path(*parts[i:]))
                            return rel, (Path(repo_root) / rel).resolve()
                    return None, None
            return None, None
        # relative path
        abs_p = (Path(repo_root) / raw).resolve() if repo_root else None
        return raw, abs_p

    # Strategy 0: file the agent actually wrote/edited — most reliable signal.
    # Write/Edit tool calls are direct evidence — skip the allowed_paths guard
    # (new files won't be in pre_existing) and the .exists() check (file may
    # have been cleaned up by restore_snapshot before we run).
    if write_candidates:
        rel, candidate_abs = _normalize_candidate(write_candidates[-1])
        if rel is not None:
            return rel

    # Strategy 1: explicit FILE: marker in structured response.
    # Only trust it if the file exists in the repo (guards against invented names).
    if final_text:
        m = _re.search(r"^FILE:\s*`?(\S+?)`?\s*$", final_text, _re.MULTILINE)
        if m:
            candidate = m.group(1)
            candidate_abs = (Path(repo_root) / candidate).resolve() if repo_root else None
            in_allowed = allowed_paths is None or (candidate_abs and candidate_abs in allowed_paths)
            if in_allowed and (repo_root is None or (Path(repo_root) / candidate).exists()):
                return candidate

    # Strategy 2: path comment inside the code block.
    # Match any relative or absolute path that looks like a file reference.
    # Only trust it if the file already exists in the repo (guards against invented names).
    if final_text:
        # Match: // some/path/File.ext  or  # some/path/File.ext  (with optional spaces)
        m = _re.search(r"(?://|#)\s*((?:\w[\w.-]*/)+[\w.-]+\.\w+)", final_text)
        if m:
            candidate = m.group(1)
            candidate_abs = (Path(repo_root) / candidate).resolve() if repo_root else None
            in_allowed = allowed_paths is None or (candidate_abs and candidate_abs in allowed_paths)
            if in_allowed and (repo_root is None or (Path(repo_root) / candidate).exists()):
                return candidate

    # Strategy 3: most-read .cy. file from trace
    if not counts:
        return None

    _VARIANT_RE = _re.compile(r"\.(mobile|a11y|rtl|ltr|dark|hcb)\.")

    def _score(rel_path: str) -> tuple:
        n = counts[rel_path]
        return (n, "/visuals/" not in rel_path, not bool(_VARIANT_RE.search(rel_path)))

    candidates = sorted(counts, key=_score, reverse=True)
    for candidate in candidates:
        candidate_abs = (Path(repo_root) / candidate).resolve() if repo_root else None
        in_allowed = allowed_paths is None or (candidate_abs and candidate_abs in allowed_paths)
        if in_allowed:
            return candidate
    return None


def extract_structured_response(path: str | Path) -> tuple[str | None, str | None]:
    """Parse ``FILE: ...`` structured response blocks from the agent's final text.

    When the agent outputs multiple files (one FILE: block each), all blocks are
    collected and concatenated so that graders see the full generated output.

    Returns ``(first_target_file, combined_content)`` where first_target_file is
    the path from the first FILE: block (used for logging / post-check targeting)
    and combined_content is the concatenation of every FILE: block separated by a
    comment header.  Both are ``None`` if no FILE: blocks are found.
    """
    import re as _re

    p = Path(path)
    if not p.exists():
        return None, None
    try:
        events = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    except Exception:  # noqa: BLE001
        return None, None

    final_text = ""
    for event in events:
        if event.get("type") != "assistant":
            continue
        for block in event.get("message", {}).get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if "```" in text:
                    final_text = text
                elif not final_text:
                    final_text = text

    if not final_text:
        return None, None

    matches = list(_re.finditer(
        r"^FILE:\s*`?(\S+?)`?\s*\n```[a-zA-Z0-9]*\s*\n(.*?)```",
        final_text,
        _re.MULTILINE | _re.DOTALL,
    ))
    if not matches:
        return None, None

    first_file = matches[0].group(1)
    parts: list[str] = []
    for m in matches:
        file_path = m.group(1)
        content = m.group(2).strip()
        parts.append(f"// FILE: {file_path}\n{content}")

    return first_file, "\n\n".join(parts)


def extract_written_content(
    path: str | Path,
    file_pattern: str | None = None,
    repo_root: str | Path | None = None,
) -> str | None:
    """Reconstruct the final content of every file the agent wrote or edited.

    Replays Write and Edit tool calls in trace order so that the result reflects
    the true final state of each file — including files that were created with
    Write and then modified with one or more Edit calls — without reading from
    disk (the worktree is typically gone by the time this runs).

    Path normalisation: file_path values in traces are absolute paths inside a
    worktree (e.g. ``/…/.skilleval-worktrees/skilleval-worker-1/packages/…``).
    When ``repo_root`` is supplied the worktree prefix is stripped by calling
    ``Path.relative_to(repo_root)``; otherwise the raw absolute path is kept as
    the key (content is still correct, the header is just less pretty).

    Returns a combined string with ``// FILE: <rel_path>`` section headers, one
    per file, in the order files were first seen.  Returns None if no Write or
    Edit calls are found.
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        events = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    except Exception:  # noqa: BLE001
        return None

    def _rel(fp: str) -> str:
        """Strip worktree / sandbox prefix to get a repo-relative path."""
        import re as _re
        p_fp = Path(fp)
        if repo_root:
            # Happy path: file is directly under repo_root.
            try:
                return str(p_fp.relative_to(Path(repo_root)))
            except ValueError:
                pass
            # Worktree path: find the longest suffix of p_fp that, when joined
            # onto repo_root, resolves to an existing file.
            parts = p_fp.parts
            for i in range(1, len(parts)):
                candidate = Path(*parts[i:])
                if (Path(repo_root) / candidate).exists():
                    return str(candidate)
        # Fallback: strip known worktree prefix patterns regardless of repo_root.
        # e.g. /…/.skilleval-worktrees/skilleval-worker-N/<rel>
        m = _re.search(r"\.skilleval-worktrees/[^/]+/(.+)", fp)
        if m:
            return m.group(1)
        # Legacy sandbox paths: /private/tmp/…/home/cwd/<rel>
        m = _re.search(r"/home/cwd/(.+)", fp)
        if m:
            return m.group(1)
        return fp

    file_order: list[str] = []
    file_content: dict[str, str] = {}

    for event in events:
        if event.get("type") != "assistant":
            continue
        for block in event.get("message", {}).get("content", []):
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name", "")
            if name not in ("Write", "Edit"):
                continue
            inp = block.get("input", {})
            raw_fp = inp.get("file_path", "")
            if not raw_fp:
                continue
            if file_pattern and file_pattern not in raw_fp:
                continue

            rel = _rel(raw_fp)

            if name == "Write":
                if rel not in file_content:
                    file_order.append(rel)
                file_content[rel] = inp.get("content", "")

            elif name == "Edit":
                old_str = inp.get("old_string", "")
                new_str = inp.get("new_string", "")
                replace_all = inp.get("replace_all", False)
                if rel not in file_content:
                    # Edit on a file with no prior Write — try disk as last resort.
                    disk = Path(raw_fp)
                    file_order.append(rel)
                    if disk.exists():
                        file_content[rel] = disk.read_text()
                    else:
                        # Worktree gone; record new_string as best effort.
                        file_content[rel] = new_str
                        continue
                current = file_content[rel]
                if replace_all:
                    file_content[rel] = current.replace(old_str, new_str)
                else:
                    file_content[rel] = current.replace(old_str, new_str, 1)

    if not file_content:
        return None

    parts = [f"// FILE: {fp}\n{file_content[fp]}" for fp in file_order]
    return "\n\n".join(parts)


def friction_summary(analyses: list[TraceAnalysis]) -> list[str]:
    """Unique friction messages across multiple traces."""
    seen: set[str] = set()
    lines: list[str] = []
    for a in analyses:
        for fp in a.friction:
            msg = f"[{fp.kind}] {fp.message}"
            if msg not in seen:
                seen.add(msg)
                lines.append(msg)
    return lines


def render_friction_section(
    case_name: str,
    with_analyses: list[TraceAnalysis],
    without_analyses: list[TraceAnalysis],
) -> list[str]:
    """Render a detailed Markdown friction section for one eval case."""
    lines: list[str] = []

    def _arm_block(label: str, analyses: list[TraceAnalysis]) -> list[str]:
        out: list[str] = []
        if not analyses:
            return out

        avg_turns = sum(a.total_turns for a in analyses) / len(analyses)
        all_friction = friction_summary(analyses)

        # Take the most detailed trace (most turns) as the representative
        rep = max(analyses, key=lambda a: a.total_turns)

        # Header line with tokens and cost
        token_detail = (
            f"in={rep.input_tokens:,} out={rep.output_tokens:,}"
            + (f" think={rep.thinking_tokens:,}" if rep.thinking_tokens else "")
        )
        first_write = f" · first write T{rep.first_write_turn}" if rep.first_write_turn else ""
        out.append(
            f"**{label}** — {avg_turns:.0f} turns · skill {'fired ✓' if rep.skill_fired else 'NOT fired ✗'}"
            f" · ${rep.cost_usd:.4f} · tokens: {token_detail}{first_write}"
        )

        # Tool sequence
        if rep.tool_calls:
            seq = rep.tool_calls[:15]
            tail = "…" if len(rep.tool_calls) > 15 else ""
            out.append(f"  Tool sequence: `{'` → `'.join(seq)}{tail}`")

        # Files read
        if rep.files_read:
            from collections import Counter
            counts = Counter(Path(f).name for f in rep.files_read)
            file_list = ", ".join(
                f"`{name}`{'×' + str(n) if n > 1 else ''}"
                for name, n in counts.most_common()
            )
            out.append(f"  Files read: {file_list}")

        # Per-turn detail for turns with interesting content
        interesting = [
            s for s in rep.turns
            if s.thinking or s.text or s.is_pivot or s.subagents_launched > 0
        ]
        if interesting:
            out.append("  Turn-by-turn highlights:")
            for snap in interesting:
                flags = []
                if snap.is_pivot:
                    flags.append("⚠ strategy pivot")
                if snap.subagents_launched:
                    flags.append(f"→ {snap.subagents_launched} subagent(s) launched")
                flag_str = f" [{', '.join(flags)}]" if flags else ""
                if snap.thinking:
                    out.append(f"    T{snap.turn}{flag_str} thinking: \"{snap.thinking[:180]}\"")
                if snap.text:
                    out.append(f"    T{snap.turn}{flag_str} said: \"{snap.text[:180]}\"")
                # Show denied tool calls
                for tc in snap.tool_calls:
                    if tc.denied:
                        out.append(f"    T{snap.turn} ✗ {tc.name}({tc.input_summary[:80]}) — DENIED")
                    elif tc.result_summary and "No files found" in tc.result_summary:
                        out.append(f"    T{snap.turn} ∅ {tc.name}({tc.input_summary[:80]}) → no results")

        # Friction signals
        if all_friction:
            out.append("  Friction signals:")
            for f in all_friction:
                out.append(f"    - {f}")
        else:
            out.append("  No friction signals.")

        return out

    with_block = _arm_block("with skill", with_analyses)
    without_block = _arm_block("without skill", without_analyses)

    if with_block or without_block:
        lines.append(f"#### {case_name}")
        lines.extend(with_block)
        if without_block:
            lines.append("")
            lines.extend(without_block)
        lines.append("")

    return lines
