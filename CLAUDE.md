# claude-chat-export — agent instructions

You are running inside a freshly cloned `claude-chat-export` repo. It exports
Claude history into readable Markdown + JSON, with **two** exporters:

- **chat** (`src/chat/`) — claude.ai web/desktop chat history → `conversations/`.
  Needs a sessionKey (one-time Keychain extraction on macOS). This is the rest of
  this file — drive the onboarding gate below **in order** (it prompts for the
  Keychain password **at most once**).
- **code** (`src/code/`) — Claude Code (CLI) session history → `code-sessions/`.
  Reads local `~/.claude/projects/*.jsonl`; **no auth, no network**. See
  [§ Claude Code session export](#claude-code-session-export-srccode).

The repo is also a Claude Code **plugin** (`claude-export`) exposing these as the
`chat` and `code` skills — see [§ Plugin](#plugin).

## Repo layout

```
claude-chat-export/
├── CLAUDE.md            # this file — how to run everything
├── README.md            # human-facing docs
├── .claude-plugin/      # plugin + marketplace manifests (claude-export)
├── skills/              # plugin skills: chat/ and code/ (SKILL.md each)
├── src/                 # all scripts (run from the repo ROOT)
│   ├── chat/            # claude.ai web exporter
│   │   ├── auth.py      # one-time local sessionKey extraction (macOS)
│   │   ├── export.py    # the exporter (pure stdlib, no install)
│   │   └── regenerate.py # rebuild binary deliverables (.docx/.pdf/…)
│   ├── code/            # Claude Code (CLI) session exporter
│   │   └── export.py    # pure stdlib, reads ~/.claude/projects
│   └── common/          # shared helpers (slugify, …)
├── .env                 # (gitignored) holds CLAUDE_SESSION_KEY after auth
├── conversations/       # (gitignored) chat export output, one folder per chat
├── code-sessions/       # (gitignored) code export output, one folder per session
└── .regen-cache/        # (gitignored) regenerate.py's node/python deps
```

**Always run scripts from the repo root** (e.g. `python3 src/chat/export.py`), so the
scripts find `.env`, write to `conversations/`, and cache deps in `.regen-cache/`.

## Tools

- `src/chat/auth.py` — one-time, fully-local sessionKey extraction (macOS).
- `src/chat/export.py` — the claude.ai exporter (pure Python 3.8+ stdlib, no install).
- `src/chat/regenerate.py` — rebuild binary deliverables from captured builder scripts.
- `src/code/export.py` — the Claude Code session exporter (pure stdlib, no auth).

## Onboarding gate (do this first, every session)

**Step 0 — is a key already configured? If yes, SKIP onboarding.**
A `.env` file (gitignored) or a `CLAUDE_SESSION_KEY` env var means you're ready —
do **not** run extraction again, do **not** prompt the Keychain. Check quietly:

```bash
test -f .env && echo "configured" || echo "needs onboarding"
```

If `configured`, go straight to **Exporting**.

**Step 1 — extract once (only if not configured).**
On macOS, the key lives in the Claude desktop app's encrypted cookie. Extract it
locally — nothing is sent anywhere:

```bash
python3 src/chat/auth.py
```

- macOS will show **one** Keychain prompt. Tell the user up front:
  > A macOS prompt will ask for your login password to read the Claude app's
  > Keychain key. Click **Always Allow** so it never asks again.
- The key is written to `.env` (chmod 600, gitignored). Re-running later does
  **not** prompt again (it's idempotent). Use `--force` only to refresh an
  expired key.

**Step 1 fallback — not on macOS, or no desktop app.**
Ask the user to copy the cookie manually, then write it to `.env` yourself:
> claude.ai → DevTools (⌥⌘I) → **Application** → **Cookies** →
> `https://claude.ai` → copy the **`sessionKey`** value (`sk-ant-sid…`).

```bash
printf 'CLAUDE_SESSION_KEY=%s\n' "<pasted-key>" > .env && chmod 600 .env
```

Never echo the key back, never commit it, never paste it into chat output.

## Exporting

`src/chat/export.py` auto-loads `.env`, so no need to export the variable.

```bash
python3 src/chat/export.py --list                  # preview + sync status (new/changed/ok)
python3 src/chat/export.py                          # incremental sync -> conversations/
python3 src/chat/export.py --limit 5                # cap this run to 5 (newest of work set)
python3 src/chat/export.py --conversation <uuid>    # one
python3 src/chat/export.py --full                   # re-export everything, ignore manifest
python3 src/chat/export.py --format md              # md only (default md,json)
```

Default end-to-end flow:
1. `--list` first (shows **new / changed / unchanged**); report the counts.
2. Confirm scope, then export.
3. **If the export reports any binary deliverables, automatically run
   `python3 src/chat/regenerate.py`** (see Step 4) — don't ask first.
4. Give one final summary (exported counts + regenerated counts).

(`python3` — `python` may not be on PATH on macOS.)

## Incremental sync (default behavior)

The export is **incremental** — it does NOT re-fetch everything each run:

- A `conversations/manifest.json` (keyed by conversation `uuid`) records each
  chat's `updated_at` and export folder.
- Each run lists all conversations (one cheap call) and fetches only the ones
  that are **new** (unseen uuid) or **changed** (`updated_at` newer than recorded).
  Unchanged chats are skipped — this is the whole point, so don't use `--full`
  unless the user wants a complete rebuild.
- Existing chats keep their folder (stable even if the title changed).
- Conversations deleted on claude.ai are **kept locally** and marked
  `"archived": true` in the manifest (the export is an archive, not a mirror).
- The run prints `N new, M updated, K unchanged`. "0 new, 0 updated" means
  everything is already up to date.

`manifest.json` lives under `conversations/` (gitignored) — it holds chat names,
so never commit it.

## Output layout (one folder per conversation)

```
conversations/<conversation-slug>/
├── conversation.md          # readable transcript
├── conversation.json        # normalized data (+ artifact/input manifests)
├── artifacts/               # files Claude generated (code, docs, …)
└── inputs/                  # files the USER uploaded (images, docs) for context
```

- **Text artifacts** (code, .md, .html, classic artifacts) are saved directly.
- **User inputs** (images/attachments) are downloaded into `inputs/` — these add
  context for future reuse.
- **Binary deliverables** (.docx/.pdf/.xlsx/.pptx) can't be pulled from the API
  (they live only in the ephemeral sandbox). The exporter records them in
  `conversation.json` with the builder script that produced them; src/chat/regenerate.py
  rebuilds the real file (next section).

## Step 4 — regenerate binary deliverables (run automatically)

If the export reports any **binary deliverables** (e.g. "N binary"), **just run
the regenerator** — do NOT stop to ask the user first. Rebuilding the real
`.docx/.pdf/.xlsx/.pptx` is the expected completion of the export:

```bash
python3 src/chat/regenerate.py                 # rebuild all in conversations/
python3 src/chat/regenerate.py --conversation <folder>   # or a single folder
```

Default behavior, in order:
1. Whenever a sync exports conversations that have binary deliverables, run
   `python3 src/chat/regenerate.py` right after, by default.
2. Report the result (`Regenerated X, failed Y`) as part of the export summary.
3. Only pause to ask the user if it **can't** proceed — i.e. Node/Python is
   missing, or there's no network to install builder libraries.

Notes:
- Needs **Node** (for `.js` builders) and/or **Python** (`.py` builders) plus
  network to install the libraries each builder imports (cached in
  `.regen-cache/`, gitignored). The first run installs deps (slow); later runs
  reuse the cache (fast).
- Anything it can't rebuild is left as a `<name>.UNAVAILABLE.txt` note beside the
  builder — nothing is lost; rerun once the runtime/network is available.

## Rate-limit safety (important — don't get blocked)

The exporter is deliberately gentle and you should keep it that way:

- It waits **~1s (+jitter) between every conversation** by default.
- On `429` / `5xx` it honors `Retry-After` and backs off exponentially (cap 60s).
- For a large history (hundreds of chats), **do not lower `--delay`**. If you
  ever see repeated `[rate]` lines, *raise* it (e.g. `--delay 2`), don't retry
  in a tight loop. Never parallelize requests.

## Guard the output

Everything in `conversations/` (and `code-sessions/`) is the user's **personal
data** (including downloaded inputs and regenerated docs) and is gitignored.
Never `git add` it, never share it, without explicit instruction.

## Troubleshooting

- **401/403** → key expired. Run `python3 src/chat/auth.py --force`.
- **Empty `--list`** → wrong org auto-picked; pass `--org <uuid>` (find orgs by
  GETting `/api/organizations` with the same cookie).
- **Keychain prompt won't go away** → user clicked "Allow" (once) instead of
  "Always Allow"; that's fine, it only matters on re-extraction.

## Claude Code session export (src/code)

Separate from the claude.ai export above. Claude Code stores every CLI session
locally as a JSONL transcript under `~/.claude/projects/<project-hash>/<id>.jsonl`.
`src/code/export.py` reads those directly — **no auth, no network, no Keychain,
no rate limits** — and renders one folder per session, grouped by project.

```bash
python3 src/code/export.py --list                  # preview + sync status (new/changed/ok)
python3 src/code/export.py                          # incremental sync -> code-sessions/
python3 src/code/export.py --limit 5               # newest 5 sessions
python3 src/code/export.py --project <substr>      # only projects matching a substring
python3 src/code/export.py --session <id>          # one session
python3 src/code/export.py --full                  # re-render everything
python3 src/code/export.py --no-thinking           # omit assistant thinking blocks
python3 src/code/export.py --include-sidechains    # also fold inline sidechain records
```

Flow: `--list` first (reports **new / changed / unchanged**), confirm scope
(all? `--project` a repo? `--limit N`?), then export. Incremental via a
`code-sessions/manifest.json` keyed by `sessionId` (re-renders only sessions whose
file size/mtime changed — the signature includes the session's subagent files).
Output layout:
`code-sessions/<project-slug>/<date>__<sid8>__<title-slug>/{session.md,session.json}`.
Sessions deleted from `~/.claude/projects` are kept and flagged `"archived": true`.

**Spawned subagents** (Task/Agent runs in `<session>/subagents/agent-*.jsonl`) are
**folded inline** at their spawn point (recursively) as collapsible `<details>`
blocks, with an "Unlinked subagents" fallback so none are lost; they're also in
`session.json`'s `subagents` array. After changing the renderer, re-run with
`--full` to regenerate existing exports.

## Plugin

This repo is an installable Claude Code plugin named **`claude-export`** (its own
single-plugin marketplace via `.claude-plugin/marketplace.json`), exposing two
skills:

- `chat` → `/claude-export:chat` (the claude.ai export, `src/chat/`)
- `code` → `/claude-export:code` (the Claude Code export, `src/code/`)

Install: `/plugin marketplace add yuting1214/claude-chat-export` then
`/plugin install claude-export@claude-export`. Skill paths anchor to
`${CLAUDE_PLUGIN_ROOT}`; validate the plugin with `claude plugin validate .`.
When editing a `src/` script, keep `skills/*/SKILL.md` and this file in sync.
