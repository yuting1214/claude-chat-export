---
name: chat
description: "Export the user's claude.ai (web/desktop) chat history into readable Markdown + JSON. Use when the user wants to back up, archive, download, sync, fetch recent, or export their Claude.ai chats/conversations, or refresh their local chat archive. For Claude Code CLI session history, use the 'code' skill instead. macOS-first (auto-extracts the sessionKey once via Keychain); manual cookie fallback elsewhere."
---

# claude-export: chat

Export claude.ai chat history into readable Markdown + JSON, one folder per
conversation. The export is **incremental** (a uuid-keyed manifest fetches only
new/changed chats) and **gentle** (rate-limited, never parallel). Drive the
whole flow; the onboarding gate is designed to prompt for the Keychain password
**at most once**.

## Where things live (self-contained under the plugin)

The scripts ship inside this plugin at `${CLAUDE_PLUGIN_ROOT}/src/chat/` and run with
`python3` (not `python` — it's often missing on macOS PATH). They anchor `.env`
and `.regen-cache/` to the plugin root automatically. Output is cwd-relative by
default, so **always pass `--out`/`--dir` with the absolute path below** so the
archive + manifest stay in one stable place regardless of the user's cwd.

Set these once per session and reuse them (the fallback keeps the skill working
if it's ever run from the cloned repo directly, where `CLAUDE_PLUGIN_ROOT` is
unset):

```bash
ROOT="${CLAUDE_PLUGIN_ROOT:-$PWD}"
OUT="$ROOT/conversations"
```

```
$ROOT/
├── src/chat/             # auth.py · export.py · regenerate.py (bundled)
├── .env                  # (created by auth) CLAUDE_SESSION_KEY, chmod 600
├── conversations/        # export output, one folder per chat (+ manifest.json)
└── .regen-cache/         # regenerate.py's node/python deps
```

## Step 0 — is a key already configured? If yes, SKIP onboarding.

A `.env` in the plugin root (or a `CLAUDE_SESSION_KEY` env var) means you're
ready. Do **not** re-extract, do **not** prompt the Keychain. Check quietly:

```bash
test -f "$ROOT/.env" && echo configured || echo "needs onboarding"
```

If `configured`, go straight to **Exporting**.

## Step 1 — extract once (only if not configured, macOS)

Tell the user up front, then run it:

> A macOS prompt will ask for your login password to read the Claude desktop
> app's Keychain key. Click **Always Allow** so it never asks again. Nothing is
> sent anywhere — extraction is fully local.

```bash
python3 "$ROOT/src/chat/auth.py"
```

Writes `.env` (chmod 600) in the plugin root. Idempotent — re-running won't
prompt again. Use `--force` only to refresh an expired key. Never echo the key,
never commit it, never paste it into chat.

### Step 1 fallback — not on macOS, or no desktop app

Ask the user to copy the cookie manually, then write it yourself:

> claude.ai → DevTools (⌥⌘I) → **Application** → **Cookies** →
> `https://claude.ai` → copy the **`sessionKey`** value (`sk-ant-sid…`).

```bash
printf 'CLAUDE_SESSION_KEY=%s\n' "<pasted-key>" > "$ROOT/.env" && chmod 600 "$ROOT/.env"
```

## Exporting

`export.py` auto-loads `.env` from the plugin root.

```bash
python3 "$ROOT/src/chat/export.py" --list --out "$OUT"                 # preview + sync status
python3 "$ROOT/src/chat/export.py" --out "$OUT"                        # incremental sync
python3 "$ROOT/src/chat/export.py" --out "$OUT" --limit 5              # cap to 5 newest
python3 "$ROOT/src/chat/export.py" --out "$OUT" --conversation <uuid>  # one chat
python3 "$ROOT/src/chat/export.py" --out "$OUT" --full                 # rebuild everything
python3 "$ROOT/src/chat/export.py" --out "$OUT" --format md            # md only (default md,json)
```

**Default end-to-end flow:**
1. Run `--list` first; report the counts (**new / changed / unchanged**).
2. Confirm scope with the user (e.g. "all new?", or honor a number they gave).
   If they named a count ("5 recent"), the list is newest-first — use `--limit N`.
3. **If the export reports any binary deliverables ("N binary"), automatically
   run the regenerator** (next section) — don't ask first.
4. Give one final summary (exported counts + regenerated counts).

## Regenerate binary deliverables (run automatically)

`.docx/.pdf/.xlsx/.pptx` can't be pulled from the API (they live only in the
ephemeral sandbox); the exporter records their builder script instead. When a
sync reports binary deliverables, rebuild the real files right after — by
default, without asking:

```bash
python3 "$ROOT/src/chat/regenerate.py" --dir "$OUT"
python3 "$ROOT/src/chat/regenerate.py" --dir "$OUT" --conversation <folder>   # one
```

- Report the result (`Regenerated X, failed Y`) in the summary.
- Needs **Node** (`.js` builders) and/or **Python** (`.py` builders) + network to
  install builder libs (cached in `.regen-cache/`; first run slow, later fast).
- Only pause to ask if it **can't** proceed (Node/Python missing, or no network).
  Anything unrebuildable is left as `<name>.UNAVAILABLE.txt` — nothing is lost.

## Incremental sync (default behavior)

- `conversations/manifest.json` (keyed by `uuid`) records each chat's
  `updated_at` + folder. Each run lists all chats (one cheap call) and fetches
  only **new** (unseen uuid) or **changed** (newer `updated_at`) ones.
- Existing chats keep their folder even if the title changed.
- Chats deleted on claude.ai are kept locally and marked `"archived": true` (this
  is an archive, not a mirror).
- `0 new, 0 updated` means everything is up to date. Don't use `--full` unless the
  user wants a complete rebuild.

## Output layout (one folder per conversation)

```
conversations/<slug>/
├── conversation.md          # readable transcript
├── conversation.json        # normalized data (+ artifact/input manifests)
├── artifacts/               # files Claude generated (code, docs, …)
└── inputs/                  # files the USER uploaded (images, docs)
```

## Rate-limit safety (don't get blocked)

- Waits ~1s (+jitter) between chats; honors `Retry-After` and backs off on
  429/5xx (cap 60s).
- For large histories (hundreds), **do not lower `--delay`**. If you see repeated
  `[rate]` lines, *raise* it (`--delay 2`); never retry in a tight loop or
  parallelize.

## Guard the output

Everything in `conversations/` is the user's **personal chat data** (downloaded
inputs + regenerated docs included). Never `git add` it, never share it, without
explicit instruction.

## Troubleshooting

- **401/403** → key expired: `python3 "$ROOT/src/chat/auth.py" --force`.
- **Empty `--list`** → wrong org auto-picked: pass `--org <uuid>` (find orgs by
  GETting `/api/organizations` with the same cookie).
- **Keychain prompt won't go away** → user clicked "Allow" once instead of
  "Always Allow"; harmless, only matters on re-extraction.
