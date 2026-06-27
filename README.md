# claude-chat-export

Export your Claude history into **readable Markdown** and **normalized JSON** —
both your [claude.ai](https://claude.ai) **web chats** and your
[**Claude Code (CLI) sessions**](#claude-code-session-export) (with spawned
subagents folded in) — turning your conversations into shareable, portable
knowledge files.

Built to be driven by an agent: clone the repo, open it in
[Claude Code](https://claude.com/claude-code), and let the agent run the export
for you (see [`CLAUDE.md`](./CLAUDE.md)). It also works as a plain CLI.

It exports **two** kinds of history: your **claude.ai web chats** (below) and your
**[Claude Code (CLI) sessions](#claude-code-session-export)** — and ships as an
installable Claude Code [plugin](#use-as-a-claude-code-plugin).

## Quick start

No installation — Python 3.8+ standard library only.

```bash
# 1. One-time auth (macOS): extract your sessionKey locally from the desktop app.
#    macOS will prompt for your password ONCE — click "Always Allow".
python3 src/chat/auth.py        # writes a gitignored .env

# 2. Preview, then export (.env is auto-loaded)
python3 src/chat/export.py --list       # see what's there
python3 src/chat/export.py              # export everything -> conversations/
```

> **Not on macOS / no desktop app?** Copy the `sessionKey` cookie from
> claude.ai DevTools (Application → Cookies) and save it yourself:
> `printf 'CLAUDE_SESSION_KEY=%s\n' '<key>' > .env`

## Use as a Claude Code plugin

This repo is also a self-contained [Claude Code](https://claude.com/claude-code)
**plugin** named `claude-export` (it ships its own marketplace), so you can
install it once and run either exporter from any directory via a skill — no need
to keep the repo as your working dir.

```bash
# From inside Claude Code, point it at this repo as a marketplace…
/plugin marketplace add yuting1214/claude-chat-export      # GitHub
# …or add a local clone:
/plugin marketplace add /path/to/claude-chat-export

# then install the plugin:
/plugin install claude-export@claude-export
```

It provides **two skills** (also auto-invoked when you just ask, e.g. “export my
Claude chats” / “back up my Claude Code sessions”):

| Command | What it does |
| --- | --- |
| `/claude-export:chat` | Export claude.ai web chat history (this README) |
| `/claude-export:code` | Export Claude Code (CLI) session history — see below |

Bundled scripts and output live under the plugin's install directory
(`${CLAUDE_PLUGIN_ROOT}`), so it stays self-contained.

## Options

| Flag | Meaning |
| --- | --- |
| `--list` | List conversations + sync status (new/changed/unchanged) |
| `--limit N` | Cap this run to N conversations (newest of the work set) |
| `--full` | Re-export everything, ignoring the manifest |
| `--conversation <uuid>` | Export a single conversation |
| `--format md\|json\|md,json` | Output formats (default `md,json`) |
| `--out DIR` | Output directory (default `conversations/`) |
| `--org <uuid>` | Override org (auto-discovered by default) |
| `--session-key <key>` | Session key (or set `CLAUDE_SESSION_KEY` / `.env`) |
| `--delay <seconds>` | Pause between fetches (default `1.0`, raise if rate-limited) |

## Incremental sync

The export is **incremental** by default — re-running only fetches what changed:

- A `conversations/manifest.json` (keyed by conversation `uuid`) tracks each
  chat's `updated_at` and folder.
- Each run fetches only **new** chats and ones whose `updated_at` advanced;
  unchanged chats are skipped. The run reports `N new, M updated, K unchanged`.
- Renamed chats keep their folder; chats deleted on claude.ai are kept locally
  and flagged `"archived": true`.
- Use `--full` to force a complete re-export.

## Rate-limit safety

Designed not to get you blocked: ~1s + jitter between every conversation,
honors `Retry-After`, and exponential backoff (cap 60s) on `429`/`5xx`. For a
large history, **don't lower `--delay`** — raise it if you see `[rate]` warnings.
Never run parallel exports.

## Output

One folder per conversation:

```
conversations/
├── manifest.json               # sync state + index of all chats (keyed by uuid)
└── my-chat-about-rust/
    ├── conversation.md          # readable transcript
    ├── conversation.json        # normalized data + artifact/input manifests
    ├── artifacts/               # files Claude generated (code, docs)
    │   └── ...
    └── inputs/                  # files YOU uploaded (images, docs) — context
        └── ...
```

The normalized JSON schema per conversation:

```json
{
  "uuid": "...", "name": "...", "created_at": "...", "updated_at": "...",
  "url": "https://claude.ai/chat/...", "message_count": 12,
  "artifacts":        [ { "file": "artifacts/app.py", "presented": true } ],
  "binary_artifacts": [ { "file": "artifacts/report.docx", "builder": "artifacts/build.js", "needs_regen": true } ],
  "inputs":           [ { "file": "inputs/diagram.png", "source": "file", "downloaded": true } ],
  "messages": [
    { "role": "human", "created_at": "...", "text": "...", "attachments": [] }
  ]
}
```

### Artifacts, inputs & binary deliverables

- **Artifacts** — code/text files Claude produced (via `create_file` or classic
  artifacts) are reconstructed exactly, preserving directory structure.
- **Inputs** — images/files *you* uploaded are downloaded into `inputs/` for
  future reuse and context.
- **Binary deliverables** (`.docx/.pdf/.xlsx/.pptx`) live only in Claude's
  ephemeral sandbox and can't be pulled from the API. The export captures the
  **builder script** that generated each, and [`src/chat/regenerate.py`](#regenerate)
  re-runs it locally to recreate the real file.

<a name="regenerate"></a>
### Regenerating binary deliverables

```bash
python3 src/chat/regenerate.py                       # rebuild all
python3 src/chat/regenerate.py --conversation <dir>  # just one
```

Needs Node (`.js` builders) and/or Python (`.py` builders) + network to install
the libraries each builder imports (cached in `.regen-cache/`). Unbuildable
items are left as `<name>.UNAVAILABLE.txt` notes — nothing is lost.

## Claude Code session export

Everything above is the **claude.ai web** export. There's also a second exporter
for **Claude Code (CLI) session history** — `src/code/export.py`. Claude Code
already stores every session locally as a JSONL transcript under
`~/.claude/projects/<project-hash>/<id>.jsonl`, so this just reads those files:
**no auth, no network, no rate limits**.

```bash
python3 src/code/export.py --list                # preview + sync status
python3 src/code/export.py                        # incremental sync -> code-sessions/
python3 src/code/export.py --limit 5             # newest 5 sessions
python3 src/code/export.py --project agent-lab   # only projects matching a substring
python3 src/code/export.py --session <id>        # one session
python3 src/code/export.py --no-thinking         # omit assistant thinking blocks
```

It's **incremental** too (a `code-sessions/manifest.json` keyed by `sessionId`
re-renders only sessions whose file changed). Output is one folder per session,
grouped by project:

```
code-sessions/
├── manifest.json
└── <project-slug>/
    └── <date>__<sid8>__<title-slug>/
        ├── session.md          # readable transcript: turns, thinking, tool calls + results
        └── session.json        # normalized data
```

Assistant thinking renders in a collapsible block (`--no-thinking` to drop it);
tool calls show name + input, with results quoted below (long output truncated).
**Spawned subagents** (Task/Agent runs, stored separately by Claude Code) are
**folded inline** at their spawn point as collapsible blocks — recursively, so
nested subagents are captured too — and also listed in `session.json`. (Pass
`--include-sidechains` to additionally fold any inline sidechain records.)

## How it works

The claude.ai desktop app is an Electron wrapper around the web app — your
conversations live on Anthropic's servers, not in a local file. This tool reads
them through claude.ai's web API using your logged-in `sessionKey` cookie.

## Privacy & caveats

- **Your exported data (`conversations/`, `code-sessions/`) is personal** and gitignored. Never commit it.
- The `sessionKey` is a live credential — keep it out of git and shell history.
- This uses claude.ai's **internal, undocumented** API. It may change without
  notice. For a fully supported alternative, use **Settings → Privacy → Export
  data** on claude.ai (arrives by email as a JSON dump).
