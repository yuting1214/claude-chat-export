#!/usr/bin/env python3
"""Export Claude Code (CLI) session history into readable Markdown + JSON.

Claude Code stores every session locally as a JSONL transcript under
`~/.claude/projects/<project-hash>/<session-id>.jsonl`. This reads those files
directly — no auth, no network, no rate limits — and renders one folder per
session, grouped by project. Pure Python 3.8+ standard library.

Usage:
    python3 src/code/export.py --list                 # list sessions + sync status
    python3 src/code/export.py                          # incremental export -> code-sessions/
    python3 src/code/export.py --limit 5               # newest 5 sessions
    python3 src/code/export.py --project agent-lab     # only projects matching a substring
    python3 src/code/export.py --session <id>          # one session
    python3 src/code/export.py --full                  # re-render everything
    python3 src/code/export.py --no-thinking           # omit assistant thinking blocks
    python3 src/code/export.py --include-sidechains    # include subagent threads
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

# Make the shared src/common package importable when run as a plain script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # -> src/
from common.text import slugify  # noqa: E402

DEFAULT_SRC = os.path.expanduser("~/.claude/projects")
MANIFEST_NAME = "manifest.json"
MAX_RESULT_CHARS = 4000      # truncate long tool outputs in markdown
MAX_INPUT_CHARS = 2000       # truncate long tool inputs in markdown


# --- parsing ---------------------------------------------------------------

def _read_jsonl(path: str) -> list:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _last(rows: list, key: str):
    """Last non-empty value of a top-level key across rows."""
    val = None
    for r in rows:
        v = r.get(key)
        if v:
            val = v
    return val


def _result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        out = []
        for p in c:
            if isinstance(p, dict):
                if p.get("type") == "text":
                    out.append(p.get("text", ""))
                elif p.get("type") == "image":
                    out.append("[image]")
            elif isinstance(p, str):
                out.append(p)
        return "\n".join(out)
    return ""


def _parse_message(rec: dict) -> dict | None:
    """Normalize one user/assistant record into a flat message dict."""
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None
    role = msg.get("role")
    if role not in ("user", "assistant"):
        return None

    text_parts, thinking_parts, tools, results = [], [], [], []
    content = msg.get("content")
    if isinstance(content, str):
        text_parts.append(content)
    elif isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                text_parts.append(b.get("text", ""))
            elif bt == "thinking":
                thinking_parts.append(b.get("thinking", ""))
            elif bt == "tool_use":
                tools.append({"name": b.get("name", "?"), "input": b.get("input")})
            elif bt == "tool_result":
                results.append({"text": _result_text(b), "is_error": bool(b.get("is_error"))})
            elif bt == "image":
                text_parts.append("[image]")

    # When a Task/Agent tool returns, the result record carries the spawned
    # subagent's id under toolUseResult.agentId — the splice point for folding
    # that subagent's transcript inline.
    tur = rec.get("toolUseResult")
    spawn = tur.get("agentId") if isinstance(tur, dict) else None

    return {
        "role": role,
        "timestamp": rec.get("timestamp"),
        "text": "\n".join(t for t in text_parts if t).strip(),
        "thinking": "\n\n".join(t for t in thinking_parts if t).strip(),
        "tools": tools,
        "results": results,
        "is_sidechain": bool(rec.get("isSidechain")),
        "spawn_agent_id": spawn,
    }


def parse_session(path: str, include_sidechains: bool) -> dict:
    rows = _read_jsonl(path)
    session_id = _last(rows, "sessionId") or os.path.splitext(os.path.basename(path))[0]
    cwd = _last(rows, "cwd") or ""
    title = _last(rows, "customTitle") or _last(rows, "slug") or ""

    messages = []
    timestamps = []
    for r in rows:
        if r.get("type") not in ("user", "assistant"):
            continue
        if r.get("isMeta"):
            continue
        if r.get("isSidechain") and not include_sidechains:
            continue
        m = _parse_message(r)
        if not m:
            continue
        # Drop empty turns (no text, no thinking, no tools, no results).
        if not (m["text"] or m["thinking"] or m["tools"] or m["results"]):
            continue
        messages.append(m)
        if m["timestamp"]:
            timestamps.append(m["timestamp"])

    if not title:
        # Fall back to the first real user prompt.
        for m in messages:
            if m["role"] == "user" and m["text"] and not m["text"].startswith("<"):
                title = m["text"][:80]
                break
    if not title:
        title = session_id[:8]

    return {
        "session_id": session_id,
        "project_cwd": cwd,
        "git_branch": _last(rows, "gitBranch") or "",
        "version": _last(rows, "version") or "",
        "agent_name": _last(rows, "agentName") or "",
        "agent_type": _last(rows, "attributionAgent") or "",
        "title": title,
        "created_at": timestamps[0] if timestamps else None,
        "updated_at": timestamps[-1] if timestamps else None,
        "message_count": len(messages),
        "messages": messages,
    }


# --- rendering -------------------------------------------------------------

def _truncate(s: str, n: int) -> str:
    s = s or ""
    if len(s) <= n:
        return s
    return s[:n] + f"\n… [truncated {len(s) - n} chars]"


def _fmt_ts(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except (ValueError, AttributeError):
        return ts


def _emit_results(m: dict, lines: list) -> None:
    for r in m["results"]:
        tag = "⚠️ Tool error" if r["is_error"] else "↳ Tool result"
        lines.append(f"> **{tag}**\n")
        lines.append("```\n" + _truncate(r["text"], MAX_RESULT_CHARS) + "\n```\n")


def _fold_subagent(aid: str, subs: dict, include_thinking: bool,
                   rendered: set, lines: list, depth: int) -> None:
    """Splice a spawned subagent's transcript inline as a collapsible block."""
    if aid in rendered or depth > 8:
        return
    rendered.add(aid)
    sub = subs.get(aid)
    if not sub:
        return
    atype = sub.get("agent_type") or sub.get("agent_name") or "subagent"
    lines.append(
        f'<details><summary>🧵 <b>Subagent: {atype}</b> '
        f'<code>{aid[:8]}</code> — {sub["message_count"]} msgs</summary>\n'
    )
    _render_messages(sub["messages"], include_thinking, subs, rendered, lines, depth + 1)
    lines.append("\n</details>\n")


def _render_messages(messages: list, include_thinking: bool, subs: dict,
                     rendered: set, lines: list, depth: int = 0) -> None:
    last_role = None
    for m in messages:
        # A user turn carrying only tool results -> an output block (the result
        # of the assistant's tools, not a new human turn). If that result is an
        # Agent/Task return, fold the spawned subagent's transcript in first.
        if m["role"] == "user" and not m["text"] and m["results"]:
            if m.get("spawn_agent_id"):
                _fold_subagent(m["spawn_agent_id"], subs, include_thinking, rendered, lines, depth)
            _emit_results(m, lines)
            last_role = None
            continue

        # Only print a role heading when the speaker actually changes, so a run
        # of assistant text/thinking/tool turns stays under one heading.
        if m["role"] != last_role:
            label = "🧑 User" if m["role"] == "user" else "🤖 Assistant"
            head = f"### {label}"
            ts = _fmt_ts(m["timestamp"])
            if ts:
                head += f" — {ts}"
            if m["is_sidechain"]:
                head += " · _sidechain_"
            lines.append(head + "\n")
            last_role = m["role"]

        if m["role"] == "assistant" and m["thinking"] and include_thinking:
            lines.append("<details><summary>💭 thinking</summary>\n")
            lines.append("```\n" + _truncate(m["thinking"], MAX_RESULT_CHARS) + "\n```\n")
            lines.append("</details>\n")

        if m["text"]:
            lines.append(m["text"] + "\n")

        for t in m["tools"]:
            try:
                inp = json.dumps(t["input"], ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                inp = str(t["input"])
            lines.append(f"🔧 **{t['name']}**\n")
            lines.append("```json\n" + _truncate(inp, MAX_INPUT_CHARS) + "\n```\n")

        # Results attached directly to an assistant turn (rare) or mixed turns.
        if m.get("spawn_agent_id"):
            _fold_subagent(m["spawn_agent_id"], subs, include_thinking, rendered, lines, depth)
        _emit_results(m, lines)

    return None


def to_markdown(s: dict, include_thinking: bool, subs: dict | None = None) -> str:
    subs = subs or {}
    lines = [f"# {s['title']}", ""]
    meta = [
        ("Project", s["project_cwd"]),
        ("Branch", s["git_branch"]),
        ("Session", s["session_id"]),
        ("Agent", s["agent_name"]),
        ("Version", s["version"]),
        ("Started", _fmt_ts(s["created_at"])),
        ("Updated", _fmt_ts(s["updated_at"])),
        ("Messages", str(s["message_count"])),
        ("Subagents", str(len(subs)) if subs else ""),
    ]
    for k, v in meta:
        if v:
            lines.append(f"- **{k}:** {v}")
    lines.append("\n---\n")

    rendered: set = set()
    _render_messages(s["messages"], include_thinking, subs, rendered, lines, depth=0)

    # Any subagent files we couldn't tie to a spawn point: append so nothing is
    # lost (keeps the export complete even if linkage detection misses one).
    leftover = [aid for aid in subs if aid not in rendered]
    if leftover:
        lines.append("\n---\n")
        lines.append(f"## Unlinked subagents ({len(leftover)})\n")
        for aid in leftover:
            _fold_subagent(aid, subs, include_thinking, rendered, lines, depth=0)

    return "\n".join(lines).rstrip() + "\n"


# --- discovery + sync ------------------------------------------------------

def discover_sessions(src: str) -> list:
    """Return [{session_id, file, mtime, size, project_dir}] newest-first."""
    out = []
    if not os.path.isdir(src):
        sys.exit(f"[src] not found: {src}\nIs Claude Code installed? Pass --src to override.")
    for proj in sorted(os.listdir(src)):
        pdir = os.path.join(src, proj)
        if not os.path.isdir(pdir):
            continue
        for fn in os.listdir(pdir):
            if not fn.endswith(".jsonl"):
                continue
            fp = os.path.join(pdir, fn)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            mtime, size = int(st.st_mtime), st.st_size
            # Fold any spawned-subagent files into the change signature so a
            # session re-renders when its subagents change, not just its own file.
            sdir = os.path.join(pdir, os.path.splitext(fn)[0], "subagents")
            if os.path.isdir(sdir):
                for sf in os.listdir(sdir):
                    if not sf.endswith(".jsonl"):
                        continue
                    try:
                        sst = os.stat(os.path.join(sdir, sf))
                    except OSError:
                        continue
                    mtime = max(mtime, int(sst.st_mtime))
                    size += sst.st_size
            out.append({
                "session_id": os.path.splitext(fn)[0],
                "file": fp,
                "mtime": mtime,
                "size": size,
                "project_dir": proj,
            })
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def load_manifest(out_dir: str) -> dict:
    path = os.path.join(out_dir, MANIFEST_NAME)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                m = json.load(f)
            m.setdefault("sessions", {})
            return m
        except (json.JSONDecodeError, OSError):
            pass
    return {"version": 1, "source": None, "last_sync": None, "sessions": {}}


def save_manifest(out_dir: str, manifest: dict) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, MANIFEST_NAME), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def classify(rec: dict, known: dict) -> str:
    prev = known.get(rec["session_id"])
    if not prev:
        return "new"
    if prev.get("size") != rec["size"] or prev.get("mtime") != rec["mtime"]:
        return "changed"
    return "unchanged"


def subagent_map(session_file: str, include_sidechains: bool) -> dict:
    """Parse a session's spawned subagents -> {agentId: parsed transcript}.

    They live in a sibling `<session-id>/subagents/agent-<id>.jsonl` directory.
    """
    base = session_file[:-6] if session_file.endswith(".jsonl") else session_file
    sdir = os.path.join(base, "subagents")
    out = {}
    if os.path.isdir(sdir):
        for fn in sorted(os.listdir(sdir)):
            if fn.startswith("agent-") and fn.endswith(".jsonl"):
                aid = fn[len("agent-"):-len(".jsonl")]
                # Subagent transcripts ARE the sidechain content — always include.
                out[aid] = parse_session(os.path.join(sdir, fn), include_sidechains=True)
    return out


def _project_slug(cwd: str, project_dir: str) -> str:
    base = os.path.basename(cwd.rstrip("/")) if cwd else ""
    return slugify(base) if base else slugify(project_dir)


def _session_folder(s: dict) -> str:
    date = (s["created_at"] or "")[:10] or "undated"
    return f"{date}__{s['session_id'][:8]}__{slugify(s['title'])}"


# --- main ------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> None:
    p = argparse.ArgumentParser(description="Export Claude Code session history.")
    p.add_argument("--src", default=DEFAULT_SRC, help=f"source dir (default: {DEFAULT_SRC})")
    p.add_argument("--out", default="code-sessions", help="output dir (default: code-sessions)")
    p.add_argument("--list", action="store_true", help="list sessions + sync status, then exit")
    p.add_argument("--limit", type=int, help="cap to N newest sessions")
    p.add_argument("--project", help="only sessions whose project path matches this substring")
    p.add_argument("--session", help="export a single session by id")
    p.add_argument("--full", action="store_true", help="re-render everything, ignoring the manifest")
    p.add_argument("--format", default="md,json", help="md | json | md,json (default md,json)")
    p.add_argument("--no-thinking", action="store_true", help="omit assistant thinking blocks")
    p.add_argument("--include-sidechains", action="store_true", help="include subagent threads")
    args = p.parse_args()

    formats = {x.strip() for x in args.format.split(",") if x.strip()}
    sessions = discover_sessions(args.src)
    if args.session:
        sessions = [r for r in sessions if r["session_id"] == args.session]
    if args.project:
        sessions = [r for r in sessions if args.project.lower() in r["project_dir"].lower()]

    manifest = load_manifest(args.out)
    known = manifest["sessions"]

    # Classify, then pick the work set.
    work = []
    for rec in sessions:
        state = "new" if args.full else classify(rec, known)
        work.append((state, rec))

    if args.list:
        new = sum(1 for s, _ in work if s == "new")
        chg = sum(1 for s, _ in work if s == "changed")
        unc = sum(1 for s, _ in work if s == "unchanged")
        for state, rec in work:
            # Cheap title peek without a full parse: reuse manifest where possible.
            title = known.get(rec["session_id"], {}).get("title", "")
            date = datetime.fromtimestamp(rec["mtime"]).strftime("%Y-%m-%d")
            tag = {"new": "[new]", "changed": "[changed]", "unchanged": "[ok]"}[state]
            label = title or rec["project_dir"]
            print(f"{tag:<10} {date}  {rec['project_dir'][:40]:<40}  {label}")
        print(f"\n{len(work)} total — {new} new, {chg} changed, {unc} unchanged.")
        return

    todo = [(s, r) for (s, r) in work if s != "unchanged"]
    if args.limit:
        todo = todo[:args.limit]

    # Reserve folders already owned by the manifest so slugs stay stable.
    taken = {v.get("folder") for v in known.values() if v.get("folder")}
    done = 0
    for i, (state, rec) in enumerate(todo, 1):
        s = parse_session(rec["file"], args.include_sidechains)
        subs = subagent_map(rec["file"], args.include_sidechains)
        proj = _project_slug(s["project_cwd"], rec["project_dir"])
        folder = known.get(s["session_id"], {}).get("folder")
        if not folder:
            base = os.path.join(proj, _session_folder(s))
            folder = base
            n = 2
            while folder in taken:
                folder = f"{base}-{n}"
                n += 1
            taken.add(folder)
        conv_dir = os.path.join(args.out, folder)
        os.makedirs(conv_dir, exist_ok=True)

        if "md" in formats:
            with open(os.path.join(conv_dir, "session.md"), "w", encoding="utf-8") as f:
                f.write(to_markdown(s, include_thinking=not args.no_thinking, subs=subs))
        if "json" in formats:
            s_json = dict(s)
            s_json["subagents"] = [
                {"agent_id": aid, "agent_type": d.get("agent_type"),
                 "message_count": d["message_count"], "messages": d["messages"]}
                for aid, d in subs.items()
            ]
            with open(os.path.join(conv_dir, "session.json"), "w", encoding="utf-8") as f:
                json.dump(s_json, f, ensure_ascii=False, indent=2)

        known[s["session_id"]] = {
            "title": s["title"],
            "project": s["project_cwd"],
            "folder": folder,
            "file": rec["file"],
            "mtime": rec["mtime"],
            "size": rec["size"],
            "messages": s["message_count"],
            "updated_at": s["updated_at"],
            "archived": False,
        }
        done += 1
        extra = f", {len(subs)} subagents" if subs else ""
        print(f"[{i}/{len(todo)}] {state:<9} {proj}/{os.path.basename(folder)} "
              f"({s['message_count']} msgs{extra})")

    # Mark sessions that vanished from the source (keep them locally).
    live = {r["session_id"] for r in sessions}
    archived = 0
    for sid, info in known.items():
        if sid not in live and not info.get("archived"):
            info["archived"] = True
            archived += 1

    manifest["source"] = args.src
    manifest["last_sync"] = _now_iso()
    save_manifest(args.out, manifest)

    skipped = len(work) - len(todo)
    extra = f", {archived} archived" if archived else ""
    print(f"\nSync complete: {done} exported, {skipped} unchanged/skipped{extra}. "
          f"{len(known)} total tracked -> {args.out}/")


if __name__ == "__main__":
    main()
