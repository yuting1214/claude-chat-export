#!/usr/bin/env python3
"""Regenerate binary deliverables (.docx/.pdf/.xlsx/.pptx) from their builders.

Claude's code-execution sandbox produced these files by RUNNING a builder
script (e.g. build_note1.js). The binary itself isn't retrievable from the API,
but the builder script is captured in the export — so we re-run it locally to
recreate the real file.

Requires Node (for .js builders) and/or Python (for .py builders), plus network
to install the libraries each builder imports (cached in .regen-cache/).

    python3 src/regenerate.py                 # scan ./conversations and rebuild all
    python3 src/regenerate.py --dir DIR       # scan a different export dir
    python3 src/regenerate.py --conversation <folder>

Anything that can't be rebuilt (missing runtime, offline, build error) is left
as a `<name>.UNAVAILABLE.txt` note beside its builder — nothing is lost.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                  # repo root (parent of src/)
CACHE = os.path.join(ROOT, ".regen-cache")    # dep cache at the repo root

_NODE_BUILTINS = {
    "fs", "path", "os", "util", "crypto", "stream", "events", "http", "https",
    "url", "zlib", "child_process", "buffer", "assert", "process", "readline",
    "querystring", "string_decoder", "tty", "net", "dns", "tls", "module",
}
# python import name -> pip package name (when they differ)
_PY_PKG = {
    "docx": "python-docx", "pptx": "python-pptx", "PIL": "Pillow",
    "yaml": "PyYAML", "bs4": "beautifulsoup4", "fitz": "PyMuPDF",
}
_PY_STDLIB = {
    "os", "sys", "json", "re", "io", "csv", "math", "datetime", "pathlib",
    "subprocess", "tempfile", "zipfile", "shutil", "collections", "itertools",
    "functools", "typing", "random", "string", "textwrap", "base64",
}

_NODE_SHIM = r"""
const fs = require("fs"), path = require("path");
const OUT = process.env.ART_OUT;
const TARGETS = (process.env.ART_TARGETS || "").split(",").filter(Boolean);
const SCRATCH = process.env.ART_SCRATCH;
function redir(p) {
  const base = path.basename(String(p));
  const dir = TARGETS.includes(base) ? OUT : SCRATCH;
  fs.mkdirSync(dir, { recursive: true });
  return path.join(dir, base);
}
const ws = fs.writeFileSync;
fs.writeFileSync = (p, d, ...r) => ws(redir(p), d, ...r);
const wf = fs.writeFile;
fs.writeFile = (p, ...r) => wf(redir(p), ...r);
require(process.argv[2]);
"""

_PY_SHIM = r"""
import builtins, os, sys, runpy
OUT = os.environ["ART_OUT"]
TARGETS = set(filter(None, os.environ.get("ART_TARGETS", "").split(",")))
SCRATCH = os.environ["ART_SCRATCH"]
_open = builtins.open
_SANDBOX = ("/home/claude", "/mnt/user-data", "/mnt/", "/tmp/outputs", "/root/")
def _redir(file, *a, **k):
    p = str(file)
    if any(p.startswith(x) for x in _SANDBOX) or os.path.basename(p) in TARGETS:
        base = os.path.basename(p)
        dest = OUT if base in TARGETS else SCRATCH
        os.makedirs(dest, exist_ok=True)
        p = os.path.join(dest, base)
    return _open(p, *a, **k)
builtins.open = _redir
runpy.run_path(sys.argv[1], run_name="__main__")
"""


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


# --------------------------------------------------------------------------- #
# Dependency detection                                                         #
# --------------------------------------------------------------------------- #
def node_packages(src: str) -> list:
    pkgs = set()
    for m in re.findall(r"""require\(\s*['"]([^'"]+)['"]\s*\)""", src):
        pkgs.add(m)
    for m in re.findall(r"""from\s+['"]([^'"]+)['"]""", src):
        pkgs.add(m)
    out = set()
    for p in pkgs:
        if p.startswith(".") or p.startswith("/"):
            continue
        base = p[1:].split("/")[0] if p.startswith("@") else p.split("/")[0]
        scoped = "/".join(p.split("/")[:2]) if p.startswith("@") else base
        if base in _NODE_BUILTINS:
            continue
        out.add(scoped)
    return sorted(out)


def py_packages(src: str) -> list:
    mods = set()
    for m in re.findall(r"^\s*import\s+([a-zA-Z0-9_]+)", src, re.M):
        mods.add(m)
    for m in re.findall(r"^\s*from\s+([a-zA-Z0-9_]+)", src, re.M):
        mods.add(m)
    out = set()
    for m in mods:
        if m in _PY_STDLIB:
            continue
        out.add(_PY_PKG.get(m, m))
    return sorted(out)


# --------------------------------------------------------------------------- #
# Runtimes                                                                     #
# --------------------------------------------------------------------------- #
def ensure_node_deps(pkgs: list) -> str | None:
    """Install npm packages into the cache; return NODE_PATH or None on failure."""
    if not shutil.which("npm"):
        return None
    nm = os.path.join(CACHE, "node_modules")
    os.makedirs(CACHE, exist_ok=True)
    missing = [p for p in pkgs if not os.path.isdir(os.path.join(nm, *p.split("/")))]
    if missing:
        if not os.path.exists(os.path.join(CACHE, "package.json")):
            _run(["npm", "init", "-y"], cwd=CACHE)
        r = _run(["npm", "install", "--no-audit", "--no-fund", *missing], cwd=CACHE)
        if r.returncode != 0:
            sys.stderr.write(f"  [npm] install failed: {r.stderr.strip()[:200]}\n")
            return None
    return nm


def ensure_py_venv(pkgs: list) -> str | None:
    """Create a venv and install pip packages; return python exe or None."""
    venv = os.path.join(CACHE, "venv")
    py = os.path.join(venv, "bin", "python")
    if not os.path.exists(py):
        r = _run([sys.executable, "-m", "venv", venv])
        if r.returncode != 0:
            return None
    if pkgs:
        r = _run([py, "-m", "pip", "install", "-q", *pkgs])
        if r.returncode != 0:
            sys.stderr.write(f"  [pip] install failed: {r.stderr.strip()[:200]}\n")
            return None
    return py


def _shim_path(name: str, content: str) -> str:
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, name)
    with open(path, "w") as f:
        f.write(content)
    return path


# --------------------------------------------------------------------------- #
# Build one binary                                                             #
# --------------------------------------------------------------------------- #
def regenerate(conv_dir: str, builder_rel: str, targets: list) -> bool:
    # Absolute paths: node's require()/the shim resolve relative to the shim dir.
    builder = os.path.abspath(os.path.join(conv_dir, builder_rel))
    if not os.path.exists(builder):
        return False
    src = open(builder, encoding="utf-8", errors="replace").read()
    out_dir = os.path.abspath(os.path.join(conv_dir, "artifacts"))
    os.makedirs(out_dir, exist_ok=True)
    scratch = tempfile.mkdtemp(prefix="regen-")
    env = {
        **os.environ, "ART_OUT": out_dir,
        "ART_TARGETS": ",".join(targets), "ART_SCRATCH": scratch,
    }
    try:
        if builder.endswith((".js", ".mjs", ".cjs")):
            if not shutil.which("node"):
                sys.stderr.write("  [node] not installed — skipping\n")
                return False
            node_path = ensure_node_deps(node_packages(src))
            if node_path is None:
                return False
            env["NODE_PATH"] = node_path
            shim = _shim_path("runner.js", _NODE_SHIM)
            r = _run(["node", shim, builder], env=env)
        elif builder.endswith(".py"):
            py = ensure_py_venv(py_packages(src))
            if py is None:
                return False
            shim = _shim_path("runner.py", _PY_SHIM)
            r = _run([py, shim, builder], env=env)
        else:
            return False
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    if r.returncode != 0:
        sys.stderr.write(f"  [build] {builder_rel} failed: {r.stderr.strip()[:200]}\n")
        return False
    return all(os.path.exists(os.path.join(out_dir, t)) for t in targets)


def write_unavailable(conv_dir: str, name: str, builder_rel: str, reason: str):
    note = os.path.join(conv_dir, "artifacts", name + ".UNAVAILABLE.txt")
    os.makedirs(os.path.dirname(note), exist_ok=True)
    with open(note, "w") as f:
        f.write(
            f"'{name}' could not be regenerated locally.\n"
            f"Reason: {reason}\n"
            f"Builder script: {builder_rel}\n"
            "Re-run `python3 src/regenerate.py` once the runtime/network is available.\n"
        )


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="Regenerate binary deliverables.")
    ap.add_argument("--dir", default="conversations", help="export dir to scan")
    ap.add_argument("--conversation", help="only this conversation folder")
    args = ap.parse_args()

    if not os.path.isdir(args.dir):
        sys.exit(f"[dir] not found: {args.dir}")

    folders = (
        [args.conversation] if args.conversation
        else sorted(os.listdir(args.dir))
    )
    ok = fail = skipped = 0
    for folder in folders:
        conv_dir = os.path.join(args.dir, folder)
        meta_path = os.path.join(conv_dir, "conversation.json")
        if not os.path.exists(meta_path):
            continue
        meta = json.load(open(meta_path))
        binaries = meta.get("binary_artifacts") or []
        if not binaries:
            continue

        # Group expected outputs by builder so each builder runs once.
        by_builder: dict = {}
        for b in binaries:
            name = os.path.basename(b["file"])
            if os.path.exists(os.path.join(conv_dir, b["file"])):
                skipped += 1  # already built
                continue
            builder_rel = (b.get("builder") or "").replace("artifacts/", "artifacts/")
            if not builder_rel:
                write_unavailable(conv_dir, name, "?", "no builder script found")
                fail += 1
                continue
            by_builder.setdefault(builder_rel, []).append(name)

        for builder_rel, targets in by_builder.items():
            print(f"[{folder}] building {', '.join(targets)} ← {builder_rel}")
            if regenerate(conv_dir, builder_rel, targets):
                ok += len(targets)
            else:
                for t in targets:
                    write_unavailable(conv_dir, t, builder_rel,
                                      "runtime/deps unavailable or build error")
                fail += len(targets)

    print(f"\nRegenerated {ok}, failed {fail}, already-built {skipped}.")


if __name__ == "__main__":
    main()
