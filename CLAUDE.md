# claude-chat-export — agent instructions

You are running inside a freshly cloned `claude-chat-export` repo. The user wants
to **export their claude.ai chat history** into readable Markdown + JSON under
`conversations/`. Drive the whole flow. Follow the onboarding gate below **in
order** — it is designed so the user is prompted for their Keychain password
**at most once**.

## Repo layout

```
claude-chat-export/
├── CLAUDE.md            # this file — how to run everything
├── README.md            # human-facing docs
├── src/                 # all scripts (run from the repo ROOT)
│   ├── auth.py          # one-time local sessionKey extraction (macOS)
│   ├── export.py        # the exporter (pure stdlib, no install)
│   └── regenerate.py    # rebuild binary deliverables (.docx/.pdf/…)
├── .env                 # (gitignored) holds CLAUDE_SESSION_KEY after auth
├── conversations/       # (gitignored) export output, one folder per chat
└── .regen-cache/        # (gitignored) regenerate.py's node/python deps
```

**Always run scripts from the repo root** (e.g. `python src/export.py`), so the
scripts find `.env`, write to `conversations/`, and cache deps in `.regen-cache/`.

## Tools

- `src/auth.py` — one-time, fully-local sessionKey extraction (macOS).
- `src/export.py` — the exporter (pure Python 3.8+ stdlib, no install).
- `src/regenerate.py` — rebuild binary deliverables from captured builder scripts.

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
python src/auth.py
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

`src/export.py` auto-loads `.env`, so no need to export the variable.

```bash
python src/export.py --list                  # preview — show the COUNT
python src/export.py                          # export ALL -> conversations/
python src/export.py --limit 5                # newest 5
python src/export.py --conversation <uuid>    # one
python src/export.py --format md              # md only (default md,json)
```

Flow: run `--list` first, report the count, confirm scope, then export.

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
  `conversation.json` with the builder script that produced them; src/regenerate.py
  rebuilds the real file (next section).

## Step 4 — regenerate binary deliverables (optional)

If any conversation shows **binary deliverables**, rebuild the real files by
re-running their builder scripts locally:

```bash
python src/regenerate.py                 # rebuild all in conversations/
python src/regenerate.py --conversation <folder>
```

- Needs **Node** (for `.js` builders) and/or **Python** (`.py` builders) plus
  network to install the libraries each builder imports (cached in
  `.regen-cache/`, gitignored).
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

Everything in `conversations/` is the user's **personal chat data** (including
downloaded inputs and regenerated docs) and is gitignored. Never `git add` it,
never share it, without explicit instruction.

## Troubleshooting

- **401/403** → key expired. Run `python src/auth.py --force`.
- **Empty `--list`** → wrong org auto-picked; pass `--org <uuid>` (find orgs by
  GETting `/api/organizations` with the same cookie).
- **Keychain prompt won't go away** → user clicked "Allow" (once) instead of
  "Always Allow"; that's fine, it only matters on re-extraction.
