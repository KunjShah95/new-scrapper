#!/usr/bin/env python3
"""Set the R2 secret access key without pasting it into a chat window.

Prompts with hidden input, validates the shape, writes it into cloudflare/.env
and immediately proves the credentials work against the bucket.

Usage
-----
    python cloudflare/set_secret.py
    python cloudflare/set_secret.py --env-file cloudflare/.env
    python cloudflare/set_secret.py --test-only
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "cloudflare"))

from r2_sync import load_env, make_client  # noqa: E402


def write_secret(env_file: Path, secret: str) -> None:
    """Replace R2_SECRET_ACCESS_KEY in place, leaving every other line alone."""
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    out, replaced = [], False
    for line in lines:
        if line.strip().startswith("R2_SECRET_ACCESS_KEY="):
            out.append(f"R2_SECRET_ACCESS_KEY={secret}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"R2_SECRET_ACCESS_KEY={secret}")
    env_file.write_text("\n".join(out) + "\n", encoding="utf-8")


def check() -> bool:
    load_env(Path(os.environ["_R2_ENV_FILE"]))
    try:
        client = make_client()
        page = client.list_objects_v2(Bucket=os.environ["R2_BUCKET"], MaxKeys=1)
        print(f"  credentials work — bucket '{os.environ['R2_BUCKET']}' is reachable "
              f"({page.get('KeyCount', 0)} object(s) in the first page)")
        return True
    except Exception as exc:
        print(f"  still failing: {type(exc).__name__}: {str(exc).splitlines()[0][:160]}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=str(REPO / "cloudflare" / ".env"))
    parser.add_argument("--test-only", action="store_true",
                        help="do not prompt, just re-test what is already configured")
    args = parser.parse_args()

    env_file = Path(args.env_file)
    os.environ["_R2_ENV_FILE"] = str(env_file)

    if not env_file.exists():
        print(f"missing {env_file} — copy cloudflare/.env.example first")
        return 2

    if not args.test_only:
        # getpass opens the console directly on Windows and blocks when input is
        # piped, so fall back to stdin when there is no terminal attached.
        if sys.stdin is not None and not sys.stdin.isatty():
            secret = sys.stdin.readline().strip()
            print("R2 secret access key (read from stdin)")
        else:
            print("R2 secret access key (input is hidden, paste and press Enter):")
            secret = getpass.getpass("  secret: ").strip()

        if not secret:
            print("nothing entered")
            return 2
        if not re.fullmatch(r"[0-9a-fA-F]{64}", secret):
            print(f"  rejected: expected 64 hex characters, got {len(secret)}"
                  f"{' (non-hex characters present)' if len(secret) == 64 else ''}")
            print("  copy it again from R2 -> Manage R2 API Tokens; a short or long")
            print("  value is almost always a paste that lost or gained characters.")
            return 2

        write_secret(env_file, secret)
        print(f"  written to {env_file}")
        # Make sure the freshly written value is what the test reads.
        os.environ.pop("R2_SECRET_ACCESS_KEY", None)

    print("testing ...")
    return 0 if check() else 1


if __name__ == "__main__":
    sys.exit(main())
