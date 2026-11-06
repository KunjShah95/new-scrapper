#!/usr/bin/env python3
"""Mirror the scraper's ``output/`` tree into Cloudflare R2.

Destination layout is routed by file type, not mirrored blindly:

    output/29742_SINDUR_VIENNA/files/ORIGINAL + SUPPL DEED.pdf
      -> main_pdfs/ircms/gujrera/29742_SINDUR_VIENNA/files/ORIGINAL + SUPPL DEED.pdf

    output/29742_SINDUR_VIENNA/project_bundle.json
      -> scraper/json/gujrera/29742_SINDUR_VIENNA/project_bundle.json

Binary documents (PDF, images, zips) go under ``R2_PDF_PREFIX``; everything
textual (JSON, Markdown, CSV, GeoJSON) goes under ``R2_JSON_PREFIX``.

``raw/`` folders are **never** uploaded -- they are verbatim API responses kept
only for local auditing. Pass ``--include-raw`` to override.

Every object is recorded in a state file, so an interrupted 135 GB run resumes
where it stopped instead of re-sending everything.

Usage
-----
    python cloudflare/r2_sync.py --dry-run            # show destinations
    python cloudflare/r2_sync.py                      # upload (resumable)
    python cloudflare/r2_sync.py --verify             # bucket vs disk
    python cloudflare/r2_sync.py --only 29742_SINDUR_VIENNA
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import mimetypes
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

try:
    import boto3
    from boto3.s3.transfer import TransferConfig
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:  # pragma: no cover
    sys.exit("boto3 is required: pip install -r cloudflare/requirements-cloudflare.txt")

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "output"

# Routing: binary documents vs textual records.
BINARY_EXT = {".pdf", ".jpg", ".jpeg", ".png", ".jfif", ".webp", ".gif", ".zip", ".tif", ".tiff"}
TEXT_EXT = {".json", ".md", ".csv", ".geojson", ".txt", ".html", ".xml"}

EXTRA_TYPES = {
    ".geojson": "application/geo+json",
    ".md": "text/markdown; charset=utf-8",
    ".json": "application/json",
    ".csv": "text/csv; charset=utf-8",
    ".jfif": "image/jpeg",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}

CHUNK = 16 * 1024 * 1024
MULTIPART_AFTER = 64 * 1024 * 1024
FLUSH_EVERY = 50


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} TiB"


def load_env(path: Path) -> None:
    """Minimal .env reader so the script needs no extra dependency."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def endpoint_url() -> str:
    if os.environ.get("R2_ENDPOINT"):
        return os.environ["R2_ENDPOINT"].rstrip("/")
    return f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"


def content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in EXTRA_TYPES:
        return EXTRA_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def prefix_for(suffix: str) -> str:
    if suffix.lower() in BINARY_EXT:
        root = os.environ.get("R2_PDF_PREFIX", "main_pdfs/ircms")
    else:
        root = os.environ.get("R2_JSON_PREFIX", "scraper/json")
    return root.strip("/")


def destination(path: Path) -> str:
    """R2 object key for a file inside output/."""
    rel = path.relative_to(SOURCE).as_posix()
    root = prefix_for(path.suffix)
    return f"{root}/{rel}" if root else rel


def is_raw(rel: str) -> bool:
    """True when the file sits inside any folder named `raw`."""
    return "raw" in rel.split("/")[:-1]


def matches(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, pattern) for pattern in patterns)


def make_client():
    # botocore >= 1.36 attaches flexible checksums that R2 rejects. These env
    # vars are the documented workaround and, unlike the matching Config
    # options, they exist on every botocore version.
    os.environ.setdefault("AWS_REQUEST_CHECKSUM_CALCULATION", "when_required")
    os.environ.setdefault("AWS_RESPONSE_CHECKSUM_VALIDATION", "when_required")
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url(),
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(
            retries={"max_attempts": 10, "mode": "standard"},
            max_pool_connections=64,
        ),
    )


def transfer_config() -> TransferConfig:
    return TransferConfig(
        multipart_threshold=MULTIPART_AFTER,
        multipart_chunksize=CHUNK,
        max_concurrency=4,
        use_threads=True,
    )


# --------------------------------------------------------------------------- #
# state file -- makes a 135 GB job restartable
# --------------------------------------------------------------------------- #
class State:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.done: dict[str, dict] = {}
        self._pending = 0
        if path.exists():
            try:
                self.done = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                print(f"! state file {path.name} unreadable, starting fresh")
                self.done = {}

    def is_current(self, key: str, path: Path) -> bool:
        record = self.done.get(key)
        if not record:
            return False
        stat = path.stat()
        return record.get("size") == stat.st_size and record.get("mtime") == int(stat.st_mtime)

    def record(self, key: str, path: Path) -> None:
        stat = path.stat()
        with self.lock:
            self.done[key] = {"size": stat.st_size, "mtime": int(stat.st_mtime)}
            self._pending += 1
            if self._pending >= FLUSH_EVERY:
                self._flush_locked()

    def _flush_locked(self) -> None:
        self._pending = 0
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.done, separators=(",", ":")), encoding="utf-8")
        tmp.replace(self.path)

    def flush(self) -> None:
        with self.lock:
            self._flush_locked()


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def collect(only: list[str], exclude: list[str], include_raw: bool) -> list[tuple[Path, str]]:
    if not SOURCE.exists():
        sys.exit(f"no such folder: {SOURCE}")
    pairs: list[tuple[Path, str]] = []
    for path in SOURCE.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(SOURCE).as_posix()
        if only and not any(rel == o or rel.startswith(o.rstrip("/") + "/") for o in only):
            continue
        if not include_raw and is_raw(rel):
            continue
        if exclude and matches(rel, exclude):
            continue
        pairs.append((path, destination(path)))
    pairs.sort(key=lambda pair: pair[1])
    return pairs


def prune_local(args, pairs, state) -> None:
    """Delete local copies of files that are confirmed in R2."""
    if not getattr(args, "prune", False):
        return
    pruned = 0
    freed = 0
    for path, key in pairs:
        try:
            if not state.is_current(key, path):
                continue
        except OSError:
            continue
        if not (getattr(args, "prune_all", False) or path.suffix.lower() in BINARY_EXT):
            continue
        try:
            size = path.stat().st_size
            path.unlink()
            pruned += 1
            freed += size
        except OSError:
            pass
    state.flush()
    scope = "all file types" if getattr(args, "prune_all", False) else "documents only"
    print(f"\npruned {pruned:,} local file(s), {human(freed)} freed ({scope})")


def cmd_upload(args, client, state: State) -> int:
    pairs = collect(args.only, args.exclude, args.include_raw)
    if not pairs:
        sys.exit("nothing matched")

    todo = [(p, k) for p, k in pairs if not state.is_current(k, p)]
    total_bytes = sum(p.stat().st_size for p, _ in todo)
    by_prefix: dict[str, list[int]] = {}
    for path, key in pairs:
        bucket = key.split("/")[0] if "/" in key else "(root)"
        slot = by_prefix.setdefault(bucket, [0, 0])
        slot[0] += 1
        slot[1] += path.stat().st_size

    print(f"source  : {SOURCE}")
    print(f"endpoint: {endpoint_url()}")
    print(f"bucket  : {os.environ['R2_BUCKET']}")
    print("routing :")
    for name, (count, size) in sorted(by_prefix.items()):
        print(f"          {name:<14} {count:>7,} files  {human(size)}")
    print(f"matched : {len(pairs):,} files")
    print(f"pending : {len(todo):,} files, {human(total_bytes)}")
    if args.include_raw:
        print("! --include-raw set: raw API responses WILL be uploaded")
    else:
        print("excluding: raw/ folders (verbatim API cache)")
    if not args.dry_run and state.done:
        print(f"resuming: {len(state.done):,} already uploaded")

    if args.dry_run:
        for path, key in todo[:15]:
            print(f"  {key}  ({human(path.stat().st_size)})")
        if len(todo) > 15:
            print(f"  ... and {len(todo) - 15:,} more")
        return 0
    if not todo:
        print("nothing to do -- everything is current")
        prune_local(args, pairs, state)
        return 0

    cfg = transfer_config()
    bucket = os.environ["R2_BUCKET"]
    lock = threading.Lock()
    progress = {"files": 0, "bytes": 0, "failed": 0}
    started = time.monotonic()

    def one(path: Path, key: str) -> tuple[str, str | None]:
        try:
            client.upload_file(
                str(path), bucket, key,
                ExtraArgs={"ContentType": content_type(path)},
                Config=cfg,
            )
            return key, None
        except (BotoCoreError, ClientError, OSError) as exc:
            return key, f"{type(exc).__name__}: {exc}"

    print("\nuploading ... (Ctrl-C is safe; rerun to resume)\n")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(one, p, k): (p, k) for p, k in todo}
        for finished, future in enumerate(as_completed(futures), start=1):
            path, key = futures[future]
            key, error = future.result()
            if error:
                with lock:
                    progress["failed"] += 1
                print(f"  FAILED {key}\n         {error}")
                continue
            state.record(key, path)
            with lock:
                progress["files"] += 1
                progress["bytes"] += path.stat().st_size
                done_bytes, done_files = progress["bytes"], progress["files"]
            if done_files % 25 == 0 or finished == len(todo):
                elapsed = max(time.monotonic() - started, 1e-6)
                rate = done_bytes / elapsed
                remaining = (total_bytes - done_bytes) / rate if rate else 0
                pct = done_bytes / total_bytes * 100 if total_bytes else 100.0
                eta = (datetime.now() + timedelta(seconds=remaining)).strftime("%H:%M:%S")
                print(f"  {done_files:,}/{len(todo):,} files  {pct:5.1f}%  "
                      f"{human(done_bytes)} / {human(total_bytes)}  {human(rate)}/s  ETA {eta}"
                      + (f"  ({progress['failed']} failed)" if progress["failed"] else ""))
    state.flush()

    if progress["failed"]:
        print(f"\n{progress['failed']} object(s) failed -- rerun to retry only those")
        return 1

    prune_local(args, pairs, state)

    print(f"\ndone: {progress['files']:,} objects, {human(progress['bytes'])} "
          f"in {time.monotonic() - started:,.0f}s")
    return 0


def cmd_init(client) -> int:
    """Materialise the two destination folders so they appear in the dashboard."""
    bucket = os.environ["R2_BUCKET"]
    roots = [p.strip("/") for p in (os.environ.get("R2_PDF_PREFIX"), os.environ.get("R2_JSON_PREFIX")) if p]
    if not roots:
        print("neither R2_PDF_PREFIX nor R2_JSON_PREFIX is set")
        return 2
    for root in roots:
        marker = f"{root}/"
        try:
            client.put_object(Bucket=bucket, Key=marker, Body=b"",
                              ContentType="application/x-directory")
        except (BotoCoreError, ClientError) as exc:
            detail = str(exc).splitlines()[0][:200]
            print(f"  FAILED   {bucket}/{marker}")
            print(f"           {detail}")
            if "SignatureDoesNotMatch" in detail:
                print("\n  The secret access key is wrong. Fix it with:\n"
                      "      python cloudflare/set_secret.py")
            elif "NoSuchBucket" in detail:
                print(f"\n  Bucket '{bucket}' does not exist on this endpoint.")
            elif "AccessDenied" in detail or "Forbidden" in detail:
                print(f"\n  The token is valid but not scoped to '{bucket}'.")
            return 1
        print(f"  created  {bucket}/{marker}")
    print(f"\n{bucket} now contains "
          + " and ".join(f"'{r}/'" for r in roots))
    return 0


def cmd_verify(args, client) -> int:
    bucket = os.environ["R2_BUCKET"]
    roots = {p.strip("/") for p in (os.environ.get("R2_PDF_PREFIX"), os.environ.get("R2_JSON_PREFIX")) if p}
    print(f"listing {bucket} under {', '.join(sorted(roots)) or 'the whole bucket'} ...")
    remote: dict[str, int] = {}
    for root in sorted(roots) or [""]:
        kwargs = {"Bucket": bucket, "MaxKeys": 1000}
        if root:
            kwargs["Prefix"] = root + "/"
        while True:
            page = client.list_objects_v2(**kwargs)
            for item in page.get("Contents", []):
                remote[item["Key"]] = item["Size"]
            if not page.get("IsTruncated"):
                break
            kwargs["ContinuationToken"] = page["NextContinuationToken"]
    print(f"bucket holds {len(remote):,} objects under those prefixes, {human(sum(remote.values()))}")

    local = {k: p for p, k in collect(args.only, args.exclude, args.include_raw)}
    missing = [k for k in local if k not in remote]
    differing = [k for k in local if k in remote and remote[k] != local[k].stat().st_size]
    extra = [k for k in remote if k not in local]

    for label, keys in (("missing", missing), ("size mismatch", differing), ("only in bucket", extra)):
        print(f"{label:15}: {len(keys):,}")
        for key in keys[:10]:
            print(f"  {key}")
        if len(keys) > 10:
            print(f"  ... and {len(keys) - 10:,} more")
    return 0 if not (missing or differing) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Mirror output/ into Cloudflare R2")
    parser.add_argument("--env-file", default=str(REPO / "cloudflare" / ".env"))
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--only", nargs="*", default=[], metavar="PREFIX")
    parser.add_argument("--exclude", nargs="*", default=[], metavar="GLOB")
    parser.add_argument("--include-raw", action="store_true",
                        help="also upload raw/ API responses (off by default)")
    parser.add_argument("--state", default=str(REPO / "cloudflare" / "r2_state.json"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--init", action="store_true",
                        help="create the destination folders (zero-byte markers) and exit")
    parser.add_argument("--prune", action="store_true",
                        help="after uploading, delete local copies of documents that are "
                             "confirmed in R2 (metadata is kept unless --prune-all)")
    parser.add_argument("--prune-all", action="store_true",
                        help="with --prune, also delete local metadata (json/md/csv)")
    args = parser.parse_args()

    load_env(Path(args.env_file))
    required = ["R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"]
    if not os.environ.get("R2_ENDPOINT") and not os.environ.get("R2_ACCOUNT_ID"):
        required.append("R2_ENDPOINT (or R2_ACCOUNT_ID)")
    missing = [k for k in required if not os.environ.get(k.split(" ")[0])]
    if missing:
        sys.exit(f"missing env var(s): {', '.join(missing)}\n"
                 f"copy cloudflare/.env.example to cloudflare/.env and fill it in")

    client = make_client()
    if args.init:
        return cmd_init(client)
    if args.verify:
        return cmd_verify(args, client)
    return cmd_upload(args, client, State(Path(args.state)))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted -- state saved, rerun to resume")
        sys.exit(130)
