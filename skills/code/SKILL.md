---
name: code
description: "Export the user's Claude Code (CLI) session history into readable Markdown + JSON. Use when the user wants to back up, archive, save, sync, or export their Claude Code sessions / coding transcripts / local agent history, or read past Claude Code conversations. Reads local ~/.claude/projects JSONL transcripts — no auth, no network. For claude.ai web chats, use the 'chat' skill instead."
---

# claude-export: code

Export Claude Code CLI session history into readable Markdown + JSON, one folder
per session, grouped by project. Claude Code already stores every session locally
as a JSONL transcript under `~/.claude/projects/<project-hash>/<session-id>.jsonl`
— this reads those directly. **No auth, no network, no rate limits, no Keychain**
— it's much simpler than the `chat` skill. The export is **incremental** (a
sessionId-keyed manifest re-renders only new/changed sessions).

## Where things live

The script ships at `${CLAUDE_PLUGIN_ROOT}/src/code/export.py` and runs with
`python3` (not `python` — often missing on macOS PATH). Output is cwd-relative by
default, so **always pass `--out` with the absolute path below** so the archive +
manifest stay in one stable place regardless of cwd.

```bash
ROOT="${CLAUDE_PLUGIN_ROOT:-$PWD}"
OUT="$ROOT/code-sessions"
```

```
$ROOT/
├── src/code/export.py    # the exporter (bundled)
└── code-sessions/        # output, one folder per session (+ manifest.json)
```

Source is `~/.claude/projects/` by default (override with `--src`).

## Exporting

```bash
python3 "$ROOT/src/code/export.py" --list --out "$OUT"              # preview + sync status
python3 "$ROOT/src/code/export.py" --out "$OUT"                     # incremental sync
python3 "$ROOT/src/code/export.py" --out "$OUT" --limit 5          # newest 5 sessions
python3 "$ROOT/src/code/export.py" --out "$OUT" --project <substr>  # filter by project path
python3 "$ROOT/src/code/export.py" --out "$OUT" --session <id>      # one session
python3 "$ROOT/src/code/export.py" --out "$OUT" --full              # re-render everything
python3 "$ROOT/src/code/export.py" --out "$OUT" --no-thinking       # omit assistant thinking
python3 "$ROOT/src/code/export.py" --out "$OUT" --include-sidechains # include subagent threads
```

**Default end-to-end flow:**
1. Run `--list` first; report the counts (**new / changed / unchanged**). The list
   is newest-first and grouped by project.
2. Confirm scope (all? a project? newest N?), then export — use `--project` to
   scope to one repo, or `--limit N` for the newest few.
3. Give one final summary (exported counts + where they landed).

## Incremental sync (default behavior)

- `code-sessions/manifest.json` (keyed by `sessionId`) records each session's
  file size + mtime and its output folder. Each run re-renders only sessions that
  are **new** or **changed** (file grew / mtime advanced); unchanged are skipped.
- Sessions keep their folder even if the title changes.
- Sessions deleted from `~/.claude/projects` are kept locally and marked
  `"archived": true` (this is an archive, not a mirror).

## Output layout (one folder per session, grouped by project)

```
code-sessions/
├── manifest.json
└── <project-slug>/
    └── <date>__<sid8>__<title-slug>/
        ├── session.md          # readable transcript (turns, thinking, tool calls + results)
        └── session.json        # normalized data
```

- **Assistant thinking** is rendered in a collapsible `<details>` block (omit with
  `--no-thinking`).
- **Tool calls** show the tool name + JSON input; **tool results** follow as
  quoted output (long inputs/outputs are truncated with a note).
- **Spawned subagents** (Task/Agent runs, stored separately under
  `<session>/subagents/agent-*.jsonl`) are **folded inline** at their spawn point
  as collapsible `<details>` blocks, recursively (nested subagents included). Any
  whose spawn point can't be located are appended under an "Unlinked subagents"
  section so nothing is lost. `session.json` also carries a `subagents` array.
  The session's change signature includes its subagent files, so it re-renders
  when they change.
- `--include-sidechains` additionally folds in any *inline* `isSidechain` records
  found within a transcript (separate from the spawned-subagent files above).

## Notes

- Sessions can be large (thousands of events); rendering is still fast since it's
  all local. No rate-limit concerns.
- Everything in `code-sessions/` is the user's **personal history** — never
  `git add` it or share it without explicit instruction.
