"""
Storage sinks — where the bytes end up.

Two implementations, one interface:

  * `LocalSink`  mirrors the key layout under a directory. Used for development,
    for verifying the delta engine without credentials, and as the fallback when
    R2 is not configured.
  * `R2Sink`     uploads through Cloudflare's S3-compatible API with boto3.

The key layout is identical either way, so switching sinks never changes what a
consumer sees:

    blobs/<ab>/<cd>/<sha256>          document bytes, content-addressed, immutable
    projects/<pid>/raw.json.gz        every endpoint payload for one project
    projects/<pid>/report.md
    projects/<pid>/summary.json
    projects/<pid>/documents_manifest.json
    catalogue/projects.json
    catalogue/documents.jsonl.gz
    changes/<date>.md | .json | .jsonl
    runs/<run>.json

Blobs carry no file extension and no filename: the key *is* the content, which
is what makes deduplication across projects free and re-uploads idempotent.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("gujrera.sink")

IMMUTABLE = "public, max-age=31536000, immutable"
REFRESH = "public, max-age=300"
VOLATILE = "public, max-age=60"

CONTENT_TYPES = {
    "json": "application/json; charset=utf-8",
    "jsonl": "application/x-ndjson",
    "gz": "application/gzip",
    "md": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
}


def blob_key(sha256: str) -> str:
    """Two levels of fan-out: R2 listings and dashboards both cope better."""
    return f"blobs/{sha256[:2]}/{sha256[2:4]}/{sha256}"


def _content_type_for(key: str) -> str:
    name = key.rsplit("/", 1)[-1]
    if "." not in name:
        return "application/octet-stream"
    ext = name.rsplit(".", 1)[-1].lower()
    if ext in CONTENT_TYPES:
        return CONTENT_TYPES[ext]
    return "application/octet-stream"


class Sink:
    """Interface. Everything the pipeline is allowed to do with storage."""

    name = "sink"
    public_base: Optional[str] = None

    def put_bytes(self, key: str, data: bytes, content_type: Optional[str] = None,
                  cache_control: str = REFRESH) -> str:
        raise NotImplementedError

    def put_file(self, key: str, path: Path, content_type: Optional[str] = None,
                 cache_control: str = REFRESH) -> str:
        raise NotImplementedError

    def put_json(self, key: str, obj: Any, cache_control: str = REFRESH) -> str:
        return self.put_bytes(
            key,
            json.dumps(obj, ensure_ascii=False, indent=2, default=str).encode("utf-8"),
            "application/json; charset=utf-8",
            cache_control,
        )

    def put_json_gz(self, key: str, obj: Any, cache_control: str = REFRESH) -> str:
        return self.put_bytes(
            key,
            gzip.compress(
                json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"), compresslevel=6
            ),
            "application/gzip",
            cache_control,
        )

    def put_jsonl_gz(self, key: str, rows, cache_control: str = REFRESH) -> str:
        body = "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows)
        return self.put_bytes(
            key, gzip.compress(body.encode("utf-8"), compresslevel=6), "application/gzip",
            cache_control,
        )

    def exists(self, key: str) -> bool:
        raise NotImplementedError

    def url_for(self, key: str) -> Optional[str]:
        return f"{self.public_base.rstrip('/')}/{key}" if self.public_base else None


class LocalSink(Sink):
    """Writes into a local directory with the same key layout as R2."""

    name = "local"

    def __init__(self, root: Path, public_base: Optional[str] = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.public_base = public_base

    def _target(self, key: str) -> Path:
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def put_bytes(self, key: str, data: bytes, content_type: Optional[str] = None,
                  cache_control: str = REFRESH) -> str:
        target = self._target(key)
        target.write_bytes(data)
        return key

    def put_file(self, key: str, path: Path, content_type: Optional[str] = None,
                 cache_control: str = REFRESH) -> str:
        target = self._target(key)
        shutil.copyfile(path, target)
        return key

    def exists(self, key: str) -> bool:
        return (self.root / key).exists()

    def url_for(self, key: str) -> Optional[str]:
        if self.public_base:
            return super().url_for(key)
        return str(self.root / key)


class R2Sink(Sink):
    """Cloudflare R2 through the S3-compatible API.

    Credentials come from the environment unless passed explicitly:

        R2_ACCOUNT_ID  R2_ACCESS_KEY_ID  R2_SECRET_ACCESS_KEY  R2_BUCKET
        R2_PUBLIC_BASE   e.g. https://files.example.com   (custom domain)

    R2 has no egress fees and is strongly consistent on read-after-write, so
    uploads here need no cache-busting dance: a blob is readable the moment the
    PUT returns.
    """

    name = "r2"

    def __init__(self, bucket: str, account_id: Optional[str], access_key_id: str,
                 secret_access_key: str, public_base: Optional[str] = None,
                 max_pool_connections: int = 32, endpoint: Optional[str] = None,
                 pdf_prefix: Optional[str] = None, json_prefix: Optional[str] = None):
        try:
            import boto3
            from boto3.s3.transfer import TransferConfig
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError(
                "boto3 is required for the R2 sink:  pip install boto3"
            ) from exc

        from botocore.config import Config

        # An explicit endpoint wins; otherwise derive it from the account id.
        if endpoint:
            self.endpoint = endpoint.rstrip("/")
        elif account_id:
            self.endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
        else:
            raise RuntimeError("R2 sink needs R2_ENDPOINT or R2_ACCOUNT_ID")

        self.bucket = bucket
        self.public_base = public_base
        self.pdf_prefix = (pdf_prefix or "").strip("/")
        self.json_prefix = (json_prefix or "").strip("/")

        # Newer botocore adds x-amz-checksum-* headers by default and R2 rejects
        # some of them. The env vars are the documented workaround and exist on
        # every botocore version, unlike the matching Config keywords.
        os.environ.setdefault("AWS_REQUEST_CHECKSUM_CALCULATION", "when_required")
        os.environ.setdefault("AWS_RESPONSE_CHECKSUM_VALIDATION", "when_required")

        self.client = boto3.client(
            "s3",
            endpoint_url=self.endpoint,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",  # required by the SDK, ignored by R2
            config=Config(
                max_pool_connections=max_pool_connections,
                retries={"max_attempts": 5, "mode": "standard"},
            ),
        )
        # Small objects (reports, manifests) never need multipart; the large
        # ones are the occasional multi-hundred-MB zip.
        self._transfer = TransferConfig(
            multipart_threshold=16 * 1024 * 1024,
            multipart_chunksize=16 * 1024 * 1024,
            max_concurrency=4,
            use_threads=True,
        )

    def _route(self, key: str) -> str:
        """Place an internal key under its public prefix.

        Document bytes (`blobs/...`) are PDFs and images, so they go under the
        PDF prefix; everything else is a textual record and goes under the JSON
        prefix. Internal keys never change -- routing is a storage detail.

        Idempotent by construction: a key that already carries either prefix is
        returned untouched. Without this, routing a routed key would match the
        *other* prefix and bury a document under `scraper/json/...`.
        """
        # Longest first, in case one prefix happens to be a prefix of the other.
        for base in sorted((self.pdf_prefix, self.json_prefix), key=len, reverse=True):
            if base and key.startswith(base + "/"):
                return key
        base = self.pdf_prefix if key.startswith("blobs/") else self.json_prefix
        return f"{base}/{key}" if base else key

    def url_for(self, key: str) -> Optional[str]:
        if not self.public_base:
            return None
        return f"{self.public_base.rstrip('/')}/{self._route(key)}"

    def put_bytes(self, key: str, data: bytes, content_type: Optional[str] = None,
                  cache_control: str = REFRESH) -> str:
        self.client.put_object(
            Bucket=self.bucket,
            Key=self._route(key),
            Body=data,
            ContentType=content_type or _content_type_for(key),
            CacheControl=cache_control,
        )
        return key

    def put_file(self, key: str, path: Path, content_type: Optional[str] = None,
                 cache_control: str = REFRESH) -> str:
        with open(path, "rb") as handle:
            self.client.upload_fileobj(
                handle,
                self.bucket,
                self._route(key),
                ExtraArgs={
                    "ContentType": content_type or _content_type_for(key),
                    "CacheControl": cache_control,
                },
                Config=self._transfer,
            )
        return key

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=self._route(key))
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def head(self, key: str) -> Optional[Dict[str, Any]]:
        from botocore.exceptions import ClientError

        try:
            return self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError:
            return None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


ENV_FILE = Path(__file__).resolve().parent.parent / "cloudflare" / ".env"


def _load_dotenv(path: Path = ENV_FILE) -> None:
    """Pick up cloudflare/.env without requiring python-dotenv.

    Existing environment variables always win, so a real deployment can still
    override the file.
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def make_sink(kind: str = "auto", root: Optional[Path] = None,
              public_base: Optional[str] = None) -> Sink:
    """Build a sink from an explicit kind, or from the environment when 'auto'."""
    _load_dotenv()
    env = os.environ
    kind = (kind or "auto").lower()

    # Either endpoint form counts as "R2 is configured".
    if kind == "auto":
        configured = env.get("R2_BUCKET") and (env.get("R2_ENDPOINT") or env.get("R2_ACCOUNT_ID"))
        kind = "r2" if configured else "local"

    if kind == "local":
        return LocalSink(root or Path("r2_mirror"), public_base=public_base or env.get("LOCAL_PUBLIC_BASE"))

    if kind == "r2":
        missing = [k for k in ("R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
                   if not env.get(k)]
        if not (env.get("R2_ENDPOINT") or env.get("R2_ACCOUNT_ID")):
            missing.append("R2_ENDPOINT (or R2_ACCOUNT_ID)")
        if missing:
            raise RuntimeError(f"R2 sink selected but missing env vars: {', '.join(missing)}")
        return R2Sink(
            bucket=env["R2_BUCKET"],
            account_id=env.get("R2_ACCOUNT_ID"),
            access_key_id=env["R2_ACCESS_KEY_ID"],
            secret_access_key=env["R2_SECRET_ACCESS_KEY"],
            public_base=public_base or env.get("R2_PUBLIC_BASE"),
            endpoint=env.get("R2_ENDPOINT"),
            pdf_prefix=env.get("R2_PDF_PREFIX"),
            json_prefix=env.get("R2_JSON_PREFIX"),
        )

    raise ValueError(f"Unknown sink '{kind}' (expected: auto, local, r2)")
