# claude-chat-export

Export your [claude.ai](https://claude.ai) chat history into **readable Markdown**
and **normalized JSON** — turning your conversations into shareable, portable
knowledge files.

Built to be driven by an agent: clone the repo, open it in
[Claude Code](https://claude.com/claude-code), and let the agent run the export
for you (see [`CLAUDE.md`](./CLAUDE.md)). It also works as a plain CLI.

## Quick start

No installation — Python 3.8+ standard library only.

```bash
# 1. One-time auth (macOS): extract your sessionKey locally from the desktop app.
#    macOS will prompt for your password ONCE — click "Always Allow".
python3 src/auth.py        # writes a gitignored .env

# 2. Preview, then export (.env is auto-loaded)
python3 src/export.py --list       # see what's there
python3 src/export.py              # export everything -> conversations/
```

> **Not on macOS / no desktop app?** Copy the `sessionKey` cookie from
> claude.ai DevTools (Application → Cookies) and save it yourself:
> `printf 'CLAUDE_SESSION_KEY=%s\n' '<key>' > .env`

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
  **builder script** that generated each, and [`src/regenerate.py`](#regenerate)
  re-runs it locally to recreate the real file.

<a name="regenerate"></a>
### Regenerating binary deliverables

```bash
python3 src/regenerate.py                       # rebuild all
python3 src/regenerate.py --conversation <dir>  # just one
```

Needs Node (`.js` builders) and/or Python (`.py` builders) + network to install
the libraries each builder imports (cached in `.regen-cache/`). Unbuildable
items are left as `<name>.UNAVAILABLE.txt` notes — nothing is lost.

## How it works

The claude.ai desktop app is an Electron wrapper around the web app — your
conversations live on Anthropic's servers, not in a local file. This tool reads
them through claude.ai's web API using your logged-in `sessionKey` cookie.

## Privacy & caveats

- **Your exported chats (`conversations/`) are personal** and gitignored. Never commit them.
- The `sessionKey` is a live credential — keep it out of git and shell history.
- This uses claude.ai's **internal, undocumented** API. It may change without
  notice. For a fully supported alternative, use **Settings → Privacy → Export
  data** on claude.ai (arrives by email as a JSON dump).
