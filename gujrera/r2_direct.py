"""Send scraped files straight to Cloudflare R2, so they never pile up locally.

Enabled with the environment variable ``GUJRERA_DIRECT_R2=1`` (or ``--direct-r2``
on the CLI). When on, every downloaded document is uploaded to R2 immediately
after it is written and then deleted from disk.

Design rules
    * the R2 key is identical to the one ``cloudflare/r2_sync.py`` would produce,
      so a direct run and a later sync are interchangeable;
    * a file is only deleted after the upload is confirmed - a failed upload
      leaves the local copy in place, so nothing is ever lost;
    * any problem degrades to the old behaviour (keep the file) rather than
      failing the scrape.

Credentials come from ``cloudflare/.env`` (R2_ENDPOINT / keys / bucket /
prefixes), the same file the sync tool uses.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger("gujrera.r2_direct")

REPO = Path(__file__).resolve().parent.parent
ENV_FILE = REPO / "cloudflare" / ".env"

_client = None
_destination = None
_content_type = None
_failed = False


def enabled() -> bool:
    return os.environ.get("GUJRERA_DIRECT_R2", "").strip().lower() in ("1", "true", "yes", "on")


def _setup() -> bool:
    """Build the S3 client once. Returns False if anything is missing."""
    global _client, _destination, _content_type, _failed
    if _client is not None:
        return True
    if _failed:
        return False
    try:
        sys.path.insert(0, str(REPO / "cloudflare"))
        import r2_sync

        r2_sync.load_env(ENV_FILE)
        missing = [k for k in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
                   if not os.environ.get(k)]
        if not (os.environ.get("R2_ENDPOINT") or os.environ.get("R2_ACCOUNT_ID")):
            missing.append("R2_ENDPOINT")
        if missing:
            log.error("direct-to-R2 disabled, missing: %s", ", ".join(missing))
            _failed = True
            return False
        _client = r2_sync.make_client()
        _destination = r2_sync.destination
        _content_type = r2_sync.content_type
        log.info("direct-to-R2 enabled -> bucket %s", os.environ["R2_BUCKET"])
        return True
    except Exception as exc:  # noqa: BLE001 - never break a scrape over storage
        log.error("direct-to-R2 unavailable: %s", exc)
        _failed = True
        return False


def key_for(path: Path) -> Optional[str]:
    if not _setup():
        return None
    try:
        # r2_sync.destination() does path.relative_to(OUTPUT), which needs an
        # absolute path; callers may hand us a relative one.
        return _destination(path.resolve())
    except Exception:  # noqa: BLE001 - file outside output/
        return None


def offload(path: Path, delete_local: bool = True) -> Optional[str]:
    """Upload `path` to R2 and, on success, delete it. Returns the key or None."""
    if not enabled() or not path.exists():
        return None
    if not _setup():
        return None
    key = key_for(path)
    if not key:
        return None
    try:
        import boto3  # noqa: F401  (imported for clarity; client already built)

        _client.upload_file(
            str(path),
            os.environ["R2_BUCKET"],
            key,
            ExtraArgs={"ContentType": _content_type(path), "CacheControl": "public, max-age=3600"},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("R2 upload failed for %s (keeping local copy): %s", path.name, exc)
        return None

    if delete_local:
        try:
            path.unlink()
        except OSError as exc:
            log.warning("uploaded %s but could not delete local copy: %s", path.name, exc)
    return key
