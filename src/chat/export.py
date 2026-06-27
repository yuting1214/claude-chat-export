#!/usr/bin/env python3
"""Export claude.ai chat history into readable Markdown + normalized JSON.

Zero dependencies — Python 3.8+ standard library only.

Auth: pass your claude.ai `sessionKey` cookie via --session-key or the
CLAUDE_SESSION_KEY environment variable. The org is auto-discovered.

Usage:
    python3 src/export.py --list                  # list conversations only
    python3 src/export.py                          # export all -> conversations/
    python3 src/export.py --limit 5                # export newest 5
    python3 src/export.py --conversation <uuid>    # export one
    python3 src/export.py --format md              # md only (default: md,json)

See CLAUDE.md for how an agent should obtain the session key and run this.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# Make the shared src/common package importable when run as a plain script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # -> src/
from common.text import slugify  # noqa: E402

BASE = "https://claude.ai"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# --- Rate-limit safety -----------------------------------------------------
# Conservative defaults so a large history (hundreds of chats) never trips a
# block. Tunable via --delay. We also honor the server's Retry-After header
# and back off exponentially on 429/5xx.
DEFAULT_DELAY = 1.0      # seconds between conversation fetches
MAX_RETRIES = 6          # per request, on transient errors
MAX_BACKOFF = 60.0       # cap for exponential backoff


def _sleep_polite(base: float) -> None:
    """Pace requests with a little jitter so calls aren't perfectly regular."""
    time.sleep(base + random.uniform(0, 0.4))


# --------------------------------------------------------------------------- #
# HTTP                                                                         #
# --------------------------------------------------------------------------- #
def _get(path: str, session_key: str, retries: int = MAX_RETRIES) -> object:
    """GET a claude.ai API path and return parsed JSON, with safe backoff."""
    url = path if path.startswith("http") else BASE + path
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    req.add_header("Referer", BASE + "/")
    req.add_header("Origin", BASE)
    req.add_header("anthropic-client-platform", "web_claude_ai")
    req.add_header("Cookie", f"sessionKey={session_key}")

    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                sys.exit(
                    f"\n[auth] {e.code} from claude.ai — the session key is "
                    "missing, wrong, or expired.\nRe-run src/auth.py "
                    "or pass a fresh key via --session-key / CLAUDE_SESSION_KEY.\n"
                )
            if e.code in (429, 500, 502, 503, 529):  # rate limited / transient
                retry_after = e.headers.get("Retry-After") if e.headers else None
                if retry_after and retry_after.isdigit():
                    wait = float(retry_after)
                else:
                    wait = min(MAX_BACKOFF, 2.0 ** (attempt + 1))
                sys.stderr.write(
                    f"[rate] {e.code} — backing off {wait:.0f}s "
                    f"(attempt {attempt + 1}/{retries})\n"
                )
                time.sleep(wait + random.uniform(0, 0.5))
                last_err = e
                continue
            last_err = e
        except urllib.error.URLError as e:
            last_err = e
            time.sleep(min(MAX_BACKOFF, 1.5 * (attempt + 1)))
    sys.exit(f"[http] giving up on {url}: {last_err}")


def _download_bytes(path: str, session_key: str):
    """GET raw bytes (for user-uploaded files). Returns (data, content_type) or None."""
    url = path if path.startswith("http") else BASE + path
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "*/*")
    req.add_header("Referer", BASE + "/")
    req.add_header("Cookie", f"sessionKey={session_key}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read(), resp.headers.get("content-type", "")
    except (urllib.error.HTTPError, urllib.error.URLError):
        return None


# --------------------------------------------------------------------------- #
# claude.ai API                                                               #
# --------------------------------------------------------------------------- #
def discover_org(session_key: str) -> str:
    orgs = _get("/api/organizations", session_key)
    if not isinstance(orgs, list) or not orgs:
        sys.exit("[org] no organizations returned for this session key.")
    # Prefer an org that can actually chat.
    for o in orgs:
        caps = o.get("capabilities") or []
        if "chat" in caps:
            return o["uuid"]
    return orgs[0]["uuid"]


def list_conversations(session_key: str, org: str) -> list:
    convos = _get(f"/api/organizations/{org}/chat_conversations", session_key)
    if not isinstance(convos, list):
        sys.exit("[list] unexpected response listing conversations.")
    return convos


def fetch_conversation(session_key: str, org: str, uuid: str) -> dict:
    q = "?tree=True&rendering_mode=messages&render_all_tools=true"
    return _get(
        f"/api/organizations/{org}/chat_conversations/{uuid}{q}", session_key
    )


# --------------------------------------------------------------------------- #
# Normalization                                                                #
# --------------------------------------------------------------------------- #
def _text_from_message(msg: dict) -> str:
    """Extract human-readable text from a message's content blocks."""
    content = msg.get("content")
    if isinstance(content, list):
        parts = []
        for block in content:
            btype = block.get("type")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "thinking":
                # Keep thinking but clearly demarcated.
                think = block.get("thinking", "")
                if think:
                    parts.append(f"<thinking>\n{think}\n</thinking>")
            elif btype == "tool_use":
                name = block.get("name", "tool")
                parts.append(f"[tool_use: {name}]")
            elif btype == "tool_result":
                parts.append("[tool_result]")
        text = "\n\n".join(p for p in parts if p)
        if text:
            return text
    # Fallback to the legacy flat `text` field.
    return msg.get("text", "") or ""


# --------------------------------------------------------------------------- #
# Artifact / generated-file extraction                                         #
# --------------------------------------------------------------------------- #
# Two mechanisms produce shareable files in a Claude conversation:
#   1. Code-execution agent — create_file / str_replace / present_files tools.
#   2. Classic artifacts     — the `artifacts` tool (text/html, markdown, …).
# We replay create/edit operations to reconstruct each file's final content.

_SANDBOX_PREFIXES = (
    "/home/claude/", "/home/user/", "/mnt/user-data/outputs/",
    "/mnt/user-data/", "/mnt/", "/tmp/", "/root/", "/workspace/",
)
_LANG_EXT = {
    "python": ".py", "javascript": ".js", "typescript": ".ts", "jsx": ".jsx",
    "tsx": ".tsx", "bash": ".sh", "shell": ".sh", "sh": ".sh", "json": ".json",
    "yaml": ".yaml", "html": ".html", "css": ".css", "sql": ".sql", "go": ".go",
    "rust": ".rs", "java": ".java", "c": ".c", "cpp": ".cpp",
}
_TYPE_EXT = {
    "text/markdown": ".md", "text/html": ".html", "image/svg+xml": ".svg",
    "application/vnd.ant.mermaid": ".mmd", "application/vnd.ant.react": ".jsx",
}
# Binary deliverables we can't reconstruct from text — src/regenerate.py rebuilds.
_REGEN_EXTS = {".docx", ".pdf", ".xlsx", ".pptx"}


def _safe_relpath(path: str) -> str:
    """Strip sandbox prefixes and neutralize traversal for a safe rel path."""
    p = (path or "").replace("\\", "/")
    for pre in _SANDBOX_PREFIXES:
        if p.startswith(pre):
            p = p[len(pre):]
            break
    parts = [seg for seg in p.split("/") if seg not in ("", ".", "..")]
    return "/".join(parts) or "file"


def _artifact_filename(art: dict, taken: set) -> str:
    atype = art.get("type")
    ext = _TYPE_EXT.get(atype) or _LANG_EXT.get((art.get("language") or "").lower())
    if not ext:
        ext = ".txt" if atype == "application/vnd.ant.code" else ".txt"
    name = slugify(art.get("title") or "artifact") + ext
    i = 2
    while name in taken:
        name = f"{slugify(art.get('title') or 'artifact')}-{i}{ext}"
        i += 1
    taken.add(name)
    return name


def extract_artifacts(convo: dict) -> dict:
    """Return {'files': [...], 'binaries': [...]}.

    'files' are text artifacts we can reconstruct directly (code, markdown,
    classic artifacts). 'binaries' are presented deliverables we CANNOT pull as
    text (.docx/.pdf/.xlsx/.pptx) — generated by a builder script in the sandbox;
    src/regenerate.py re-runs that builder locally to recreate the real file.
    """
    files: dict = {}        # relpath -> content (code-exec)
    classic: dict = {}      # id -> {content,type,title,language}
    order: list = []        # preserve first-seen order: (kind, key)
    presented: set = set()
    presented_order: list = []

    for m in convo.get("chat_messages", []):
        for b in (m.get("content") or []):
            if b.get("type") != "tool_use":
                continue
            name, inp = b.get("name"), (b.get("input") or {})
            if name == "create_file" and inp.get("path"):
                rp = _safe_relpath(inp["path"])
                files[rp] = inp.get("file_text", "")
                if ("file", rp) not in order:
                    order.append(("file", rp))
            elif name == "str_replace" and inp.get("path"):
                rp = _safe_relpath(inp["path"])
                if rp in files:
                    files[rp] = files[rp].replace(
                        inp.get("old_str", ""), inp.get("new_str", ""), 1
                    )
            elif name == "present_files":
                for fp in inp.get("filepaths", []):
                    rp = _safe_relpath(fp)
                    presented.add(rp)
                    if rp not in presented_order:
                        presented_order.append(rp)
            elif name == "artifacts":
                cmd = inp.get("command")
                aid = inp.get("id") or inp.get("title") or "artifact"
                if cmd in ("create", "rewrite") or aid not in classic:
                    classic[aid] = {
                        "content": inp.get("content", ""),
                        "type": inp.get("type"),
                        "title": inp.get("title") or aid,
                        "language": inp.get("language"),
                    }
                    if ("classic", aid) not in order:
                        order.append(("classic", aid))
                elif cmd == "update" and aid in classic:
                    classic[aid]["content"] = classic[aid]["content"].replace(
                        inp.get("old_str", ""), inp.get("new_str", ""), 1
                    )

    out, taken = [], set()
    for kind, key in order:
        if kind == "file":
            out.append({
                "relpath": key, "content": files[key], "kind": "code_file",
                "presented": key in presented,
            })
        else:
            c = classic[key]
            out.append({
                "relpath": _artifact_filename(c, taken), "content": c["content"],
                "kind": "artifact", "type": c.get("type"),
                "title": c.get("title"), "presented": True,
            })

    # Binary deliverables (docx/pdf/xlsx/pptx) presented but not reconstructable
    # as text. Locate the builder script (a reconstructed file whose content
    # writes that output) so src/regenerate.py can re-run it.
    binaries = []
    for rp in presented_order:
        base = rp.split("/")[-1]
        ext = os.path.splitext(base)[1].lower()
        if ext not in _REGEN_EXTS or rp in files:
            continue
        builder = next(
            (fp for fp in files if base in files[fp]), None
        )
        binaries.append({
            "name": base,
            "ext": ext,
            "builder": builder,            # relpath of the script that writes it
            "kind": "binary",
        })
    return {"files": out, "binaries": binaries}


def extract_inputs(convo: dict) -> list:
    """Collect user-uploaded inputs (images, docs) from human messages.

    These provide context for future reuse. Images/files live in the file store
    (downloadable); attachments carry extracted text in the transcript itself.
    """
    inputs, taken = [], set()

    def _unique(name: str) -> str:
        name = os.path.basename(name or "input").replace("/", "_") or "input"
        cand, stem_ext = name, os.path.splitext(name)
        i = 2
        while cand in taken:
            cand = f"{stem_ext[0]}-{i}{stem_ext[1]}"
            i += 1
        taken.add(cand)
        return cand

    for m in convo.get("chat_messages", []):
        if m.get("sender") != "human":
            continue
        for a in (m.get("attachments") or []):
            name = a.get("file_name") or ""
            if not name:  # attachments often have no filename
                aid = (a.get("id") or "")[:8]
                ftype = a.get("file_type") or "txt"
                name = f"attachment-{aid}.{ftype}" if aid else f"attachment.{ftype}"
            inputs.append({
                "source": "attachment", "file_name": name,
                "save_as": _unique(name), "file_uuid": a.get("file_uuid"),
                "file_kind": "document", "download_url": None,
                "extracted_content": a.get("extracted_content") or "",
            })
        for f in (m.get("files") or m.get("files_v2") or []):
            name = f.get("file_name") or "file"
            inputs.append({
                "source": "file", "file_name": name,
                "save_as": _unique(name),
                "file_uuid": f.get("file_uuid") or f.get("uuid"),
                "file_kind": f.get("file_kind"),
                "download_url": f.get("preview_url") or f.get("thumbnail_url"),
                "extracted_content": f.get("extracted_content") or "",
            })
    return inputs


def normalize(convo: dict, arts: dict, inputs: list) -> dict:
    msgs = convo.get("chat_messages") or convo.get("messages") or []
    out_msgs = []
    for m in msgs:
        attachments = [
            a.get("file_name") or a.get("name") or "attachment"
            for a in (m.get("attachments") or [])
        ]
        files = [
            f.get("file_name") or f.get("name") or "file"
            for f in (m.get("files") or m.get("files_v2") or [])
        ]
        out_msgs.append(
            {
                "role": m.get("sender", "unknown"),
                "created_at": m.get("created_at"),
                "text": _text_from_message(m),
                "attachments": attachments + files,
            }
        )
    uuid = convo.get("uuid", "")
    return {
        "uuid": uuid,
        "name": convo.get("name") or "(untitled)",
        "created_at": convo.get("created_at"),
        "updated_at": convo.get("updated_at"),
        "url": f"{BASE}/chat/{uuid}",
        "message_count": len(out_msgs),
        "artifacts": [
            {
                "file": f"artifacts/{a['relpath']}",
                "kind": a["kind"],
                "title": a.get("title"),
                "type": a.get("type"),
                "presented": a.get("presented", False),
                "size": len(a["content"]),
            }
            for a in arts["files"]
        ],
        "binary_artifacts": [
            {
                "file": f"artifacts/{b['name']}",
                "kind": "binary",
                "builder": f"artifacts/{b['builder']}" if b["builder"] else None,
                "needs_regen": True,
            }
            for b in arts["binaries"]
        ],
        "inputs": [
            {
                "file": f"inputs/{i['save_as']}",
                "source": i["source"],
                "kind": i.get("file_kind"),
                "original_name": i["file_name"],
                "downloaded": i.get("downloaded", False),
            }
            for i in inputs
        ],
        "messages": out_msgs,
    }


# --------------------------------------------------------------------------- #
# Rendering                                                                    #
# --------------------------------------------------------------------------- #
_ROLE_LABEL = {"human": "🧑 Human", "assistant": "🤖 Assistant"}


def to_markdown(c: dict) -> str:
    lines = [
        f"# {c['name']}",
        "",
        f"- **Created:** {c.get('created_at') or '—'}",
        f"- **Updated:** {c.get('updated_at') or '—'}",
        f"- **Messages:** {c['message_count']}",
        f"- **URL:** {c['url']}",
    ]
    if c.get("artifacts"):
        lines.append(f"- **Artifacts:** {len(c['artifacts'])}")
        for a in c["artifacts"]:
            star = " ⭐" if a.get("presented") else ""
            label = a.get("title") or a["file"].split("/")[-1]
            lines.append(f"  - [`{a['file']}`](./{a['file']}) — {label}{star}")
    if c.get("binary_artifacts"):
        lines.append(f"- **Binary deliverables:** {len(c['binary_artifacts'])} "
                     "(run `src/regenerate.py` to rebuild)")
        for b in c["binary_artifacts"]:
            lines.append(f"  - `{b['file']}` ⭐ — regenerate from "
                         f"`{b['builder'] or '?'}`")
    if c.get("inputs"):
        got = [i for i in c["inputs"] if i.get("downloaded")]
        if got:
            lines.append(f"- **User inputs:** {len(got)}")
            for i in got:
                lines.append(f"  - [`{i['file']}`](./{i['file']}) — "
                             f"{i['original_name']} ({i['source']})")
    lines += ["", "---", ""]
    for m in c["messages"]:
        label = _ROLE_LABEL.get(m["role"], m["role"])
        when = m.get("created_at") or ""
        head = f"### {label}" + (f" · {when}" if when else "")
        lines.append(head)
        if m["attachments"]:
            lines.append(f"*attachments: {', '.join(m['attachments'])}*")
        lines.append("")
        lines.append(m["text"] or "*(empty)*")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #
def load_env_file() -> None:
    """Load CLAUDE_SESSION_KEY from a sibling .env if not already in the env.

    Keeps the secret out of shell history and lets the one-time extraction
    persist, so the keychain is never touched again on later runs.
    """
    if os.environ.get("CLAUDE_SESSION_KEY"):
        return
    # .env lives at the repo/plugin root (two levels up from src/chat/).
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env_path = os.path.join(root, ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("CLAUDE_SESSION_KEY=") and "=" in line:
                os.environ.setdefault("CLAUDE_SESSION_KEY", line.split("=", 1)[1])


_CTYPE_EXT = {
    "image/webp": ".webp", "image/png": ".png", "image/jpeg": ".jpg",
    "image/gif": ".gif", "application/pdf": ".pdf",
}


def _retype(name: str, ctype: str) -> str:
    ext = _CTYPE_EXT.get((ctype or "").split(";")[0].strip())
    if ext and not name.lower().endswith(ext):
        return os.path.splitext(name)[0] + ext
    return name


def _write_inputs(inputs: list, conv_dir: str, session_key: str) -> None:
    """Download user-uploaded files / save extracted text into inputs/.

    Mutates each input's save_as/downloaded to reflect what landed on disk.
    """
    inputs_dir = os.path.join(conv_dir, "inputs")
    for inp in inputs:
        data = None
        if inp["source"] == "file" and inp.get("download_url"):
            got = _download_bytes(inp["download_url"], session_key)
            if got:
                data, ctype = got
                inp["save_as"] = _retype(inp["save_as"], ctype)
        if data is not None:
            os.makedirs(inputs_dir, exist_ok=True)
            with open(os.path.join(inputs_dir, inp["save_as"]), "wb") as f:
                f.write(data)
            inp["downloaded"] = True
        elif inp.get("extracted_content"):
            os.makedirs(inputs_dir, exist_ok=True)
            inp["save_as"] = inp["save_as"] + ".extracted.txt"
            with open(os.path.join(inputs_dir, inp["save_as"]), "w",
                      encoding="utf-8") as f:
                f.write(inp["extracted_content"])
            inp["downloaded"] = True
        else:
            inp["downloaded"] = False


# --------------------------------------------------------------------------- #
# Incremental sync — manifest keyed by conversation uuid                        #
# --------------------------------------------------------------------------- #
MANIFEST_NAME = "manifest.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_manifest(out_dir: str) -> dict:
    path = os.path.join(out_dir, MANIFEST_NAME)
    if os.path.exists(path):
        try:
            with open(path) as f:
                m = json.load(f)
            m.setdefault("conversations", {})
            return m
        except (json.JSONDecodeError, OSError):
            pass
    return {"version": 1, "org": None, "last_sync": None, "conversations": {}}


def save_manifest(out_dir: str, manifest: dict) -> None:
    with open(os.path.join(out_dir, MANIFEST_NAME), "w") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def _needs_export(summary: dict, known: dict, full: bool) -> str:
    """Classify a conversation vs the manifest: new | changed | unchanged."""
    entry = known.get(summary["uuid"])
    if entry is None:
        return "new"
    if full:
        return "changed"
    server_ts = summary.get("updated_at") or ""
    return "changed" if server_ts > (entry.get("updated_at") or "") else "unchanged"


def main() -> None:
    load_env_file()
    p = argparse.ArgumentParser(description="Export claude.ai chat history.")
    p.add_argument("--session-key", default=os.environ.get("CLAUDE_SESSION_KEY"))
    p.add_argument("--org", help="org uuid (auto-discovered if omitted)")
    p.add_argument("--out", default="conversations",
                   help="output directory (default: conversations)")
    p.add_argument("--format", default="md,json", help="md, json, or md,json")
    p.add_argument("--limit", type=int, help="export only the newest N")
    p.add_argument("--conversation", help="export a single conversation uuid")
    p.add_argument("--list", action="store_true",
                   help="list conversations + sync status, then exit")
    p.add_argument("--full", action="store_true",
                   help="re-export everything, ignoring the manifest (no skip)")
    p.add_argument(
        "--delay", type=float, default=DEFAULT_DELAY,
        help=f"seconds between conversation fetches (default: {DEFAULT_DELAY})",
    )
    args = p.parse_args()

    if not args.session_key:
        sys.exit(
            "No session key. Pass --session-key or set CLAUDE_SESSION_KEY.\n"
            "See CLAUDE.md for how to obtain it."
        )

    formats = {f.strip() for f in args.format.split(",") if f.strip()}
    org = args.org or discover_org(args.session_key)

    all_summaries = list_conversations(args.session_key, org)
    all_summaries.sort(key=lambda c: c.get("updated_at") or "", reverse=True)
    present_uuids = {s["uuid"] for s in all_summaries}

    os.makedirs(args.out, exist_ok=True)
    manifest = load_manifest(args.out)
    known = manifest["conversations"]

    # Scope: a single conversation, or the whole list.
    summaries = all_summaries
    if args.conversation:
        summaries = [c for c in all_summaries if c.get("uuid") == args.conversation]
        if not summaries:
            sys.exit(f"[find] conversation {args.conversation} not found.")

    if args.list:
        counts = {"new": 0, "changed": 0, "unchanged": 0}
        tags = {"new": "[new]    ", "changed": "[changed]", "unchanged": "[ok]     "}
        for s in summaries:
            st = _needs_export(s, known, args.full)
            counts[st] += 1
            print(f"{tags[st]} {(s.get('updated_at') or '')[:10]}  {s.get('name')}")
        print(f"\n{len(summaries)} total — {counts['new']} new, "
              f"{counts['changed']} changed, {counts['unchanged']} unchanged.")
        return

    # Work set = new + changed (or everything with --full), newest first.
    candidates = [s for s in summaries
                  if _needs_export(s, known, args.full) in ("new", "changed")]
    unchanged = len(summaries) - len(candidates)
    work = candidates[: args.limit] if args.limit else candidates
    deferred = len(candidates) - len(work)

    # Reserve every folder the manifest already owns, so new slugs never clash.
    used_folders = {e["folder"] for e in known.values() if e.get("folder")}

    new_ct = changed_ct = 0
    for i, summary in enumerate(work, 1):
        uuid = summary["uuid"]
        entry = known.get(uuid)
        status = "changed" if entry else "new"
        full = fetch_conversation(args.session_key, org, uuid)
        arts = extract_artifacts(full)
        inputs = extract_inputs(full)

        # Reuse the existing folder for known chats (stable even if renamed);
        # otherwise pick a fresh slug, -N on collision.
        if entry and entry.get("folder"):
            base = entry["folder"]
        else:
            base, n = slugify(summary.get("name") or full.get("name") or "untitled"), 2
            while base in used_folders:
                base = f"{slugify(full.get('name') or 'untitled')}-{n}"
                n += 1
            used_folders.add(base)
        conv_dir = os.path.join(args.out, base)
        os.makedirs(conv_dir, exist_ok=True)

        _write_inputs(inputs, conv_dir, args.session_key)
        c = normalize(full, arts, inputs)

        n_art, n_bin = len(arts["files"]), len(arts["binaries"])
        n_in = sum(1 for x in inputs if x.get("downloaded"))
        extra = "".join([
            f", {n_art} artifact(s)" if n_art else "",
            f", {n_bin} binary" if n_bin else "",
            f", {n_in} input(s)" if n_in else "",
        ])
        print(f"[{i}/{len(work)}] {status:7} {c['name']} "
              f"({c['message_count']} msgs{extra})")

        if "json" in formats:
            with open(os.path.join(conv_dir, "conversation.json"), "w") as f:
                json.dump(c, f, ensure_ascii=False, indent=2)
        if "md" in formats:
            with open(os.path.join(conv_dir, "conversation.md"), "w") as f:
                f.write(to_markdown(c))

        for a in arts["files"]:
            dest = os.path.join(conv_dir, "artifacts", a["relpath"])
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(a["content"])

        known[uuid] = {
            "uuid": uuid,
            "name": c["name"],
            "folder": base,
            "updated_at": summary.get("updated_at"),
            "exported_at": _now_iso(),
            "message_count": c["message_count"],
            "artifact_count": n_art,
            "binary_count": n_bin,
            "input_count": n_in,
            "archived": False,
        }
        new_ct += status == "new"
        changed_ct += status == "changed"
        if i < len(work):
            _sleep_polite(args.delay)  # pace requests to stay under rate limits

    # Mark conversations no longer on the server as archived (kept locally).
    archived_ct = 0
    for uuid, entry in known.items():
        gone = uuid not in present_uuids
        if gone and not entry.get("archived"):
            archived_ct += 1
        entry["archived"] = gone

    # Keep the manifest's conversations ordered newest-first so it doubles as a
    # human-browseable index — no separate index.json needed.
    manifest["conversations"] = dict(sorted(
        known.items(),
        key=lambda kv: kv[1].get("updated_at") or "", reverse=True,
    ))
    manifest["org"] = org
    manifest["last_sync"] = _now_iso()
    save_manifest(args.out, manifest)

    print(f"\nSync complete: {new_ct} new, {changed_ct} updated, "
          f"{unchanged} unchanged"
          + (f", {deferred} deferred (--limit)" if deferred else "")
          + (f", {archived_ct} newly-archived" if archived_ct else "")
          + f". {len(known)} total tracked -> {args.out}/")


if __name__ == "__main__":
    main()
