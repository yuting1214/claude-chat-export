#!/usr/bin/env python3
"""One-time, fully-local extraction of the claude.ai sessionKey (macOS).

Reads the Claude desktop app's encrypted cookie and decrypts it on-device
using the app's Keychain key. Nothing is sent anywhere. The result is written
to a gitignored `.env` next to this script.

Idempotent by design: if `.env` already has a key, this exits WITHOUT touching
the Keychain — so you are prompted for your login/Keychain password at most
ONCE. Use --force to re-extract (e.g. after the session expires).

    python3 src/auth.py            # extract once -> .env
    python3 src/auth.py --force     # re-extract a fresh key
    python3 src/auth.py --print     # print masked, don't write

When macOS shows the Keychain prompt, click **Always Allow** so future
re-extractions never prompt again.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)            # repo root (parent of src/)
ENV_PATH = os.path.join(ROOT, ".env")   # .env lives at the repo root
COOKIES = os.path.expanduser(
    "~/Library/Application Support/Claude/Cookies"
)
KEYCHAIN_SERVICE = "Claude Safe Storage"
KEY_PREFIX = "sk-ant-sid"  # current claude.ai sessionKey format (sid01/sid02/…)


def _existing_key() -> str | None:
    if os.environ.get("CLAUDE_SESSION_KEY"):
        return os.environ["CLAUDE_SESSION_KEY"]
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f:
                if line.startswith("CLAUDE_SESSION_KEY="):
                    return line.split("=", 1)[1].strip()
    return None


def _mask(key: str) -> str:
    return f"{key[:13]}…{key[-4:]} (len {len(key)})"


def _read_encrypted_cookie() -> bytes:
    if not os.path.exists(COOKIES):
        sys.exit(
            f"[cookies] not found: {COOKIES}\n"
            "Is the Claude desktop app installed and have you signed in?"
        )
    # immutable=1 → safe read even while the app holds the DB open.
    uri = f"file:{COOKIES}?immutable=1"
    con = sqlite3.connect(uri, uri=True)
    try:
        row = con.execute(
            "SELECT encrypted_value FROM cookies "
            "WHERE name='sessionKey' AND host_key LIKE '%claude.ai%' "
            "ORDER BY length(encrypted_value) DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    if not row or not row[0]:
        sys.exit(
            "[cookies] no sessionKey cookie found. Open the Claude desktop app "
            "and make sure you are logged in, then retry."
        )
    return row[0]


def _keychain_password() -> str:
    """Fetch the app's Safe Storage key from Keychain (ONE prompt, max)."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-ws", KEYCHAIN_SERVICE],
            capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError:
        sys.exit(
            f"[keychain] could not read '{KEYCHAIN_SERVICE}'. If a password "
            "prompt appeared, click Allow (ideally 'Always Allow') and retry."
        )
    pw = out.stdout.strip()
    if not pw:
        sys.exit("[keychain] empty Safe Storage key.")
    return pw


def _decrypt(encrypted: bytes, password: str) -> str:
    # Chromium 'v10' scheme: AES-128-CBC, PBKDF2-HMAC-SHA1(salt, 1003), IV=16x' '.
    if encrypted[:3] != b"v10":
        sys.exit("[decrypt] unexpected cookie scheme (expected v10).")
    key = hashlib.pbkdf2_hmac("sha1", password.encode(), b"saltysalt", 1003, 16)
    proc = subprocess.run(
        ["openssl", "enc", "-d", "-aes-128-cbc",
         "-K", key.hex(), "-iv", (b" " * 16).hex(), "-nopad"],
        input=encrypted[3:], capture_output=True,
    )
    pt = proc.stdout
    if pt and 1 <= pt[-1] <= 16:        # strip PKCS7 padding
        pt = pt[: -pt[-1]]
    # Newer Chromium prepends a 32-byte SHA-256 domain hash; locate the token.
    text = pt.decode("utf-8", "replace")
    idx = text.find(KEY_PREFIX)
    if idx < 0:
        sys.exit(
            "[decrypt] decryption succeeded but no sessionKey token found. "
            "The cookie format may have changed."
        )
    return text[idx:].strip()


def _write_env(key: str) -> None:
    with open(ENV_PATH, "w") as f:
        f.write(f"CLAUDE_SESSION_KEY={key}\n")
    os.chmod(ENV_PATH, 0o600)  # owner-only


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract claude.ai sessionKey (macOS).")
    ap.add_argument("--force", action="store_true", help="re-extract even if .env exists")
    ap.add_argument("--print", dest="show", action="store_true",
                    help="print masked key without writing .env")
    args = ap.parse_args()

    if sys.platform != "darwin":
        sys.exit(
            "[platform] auto-extraction is macOS-only. On other systems, copy "
            "the sessionKey cookie from your browser's DevTools and set "
            "CLAUDE_SESSION_KEY yourself (see CLAUDE.md)."
        )

    if not args.force:
        existing = _existing_key()
        if existing:
            print(f"[ok] session key already configured: {_mask(existing)}")
            print("     Nothing to do (no Keychain prompt). Use --force to refresh.")
            return

    key = _decrypt(_read_encrypted_cookie(), _keychain_password())
    if not key.startswith(KEY_PREFIX):
        sys.exit("[verify] extracted value does not look like a sessionKey.")

    if args.show:
        print(f"[ok] extracted (not written): {_mask(key)}")
        return
    _write_env(key)
    print(f"[ok] wrote {os.path.relpath(ENV_PATH, ROOT)} (chmod 600): {_mask(key)}")
    print("     You can now run: python3 src/export.py --list")


if __name__ == "__main__":
    main()
