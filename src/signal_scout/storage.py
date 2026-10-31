"""Content-addressed blob storage on S3 (MinIO in dev).

Raw bodies and extracted text are stored gzipped under keys derived from their SHA-256, so the same
content is uploaded once no matter how many feeds syndicate it, and Postgres only needs the key:

    raw/<yyyy>/<mm>/<dd>/<sha256>.html.gz   (the date is the UTC fetch date)
    text/<sha256>.txt.gz                    (sha256 of the extracted text = signals.content_hash)
"""

from __future__ import annotations

import gzip
import hashlib
from datetime import datetime
from typing import Any, Protocol

import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .config import Settings


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def raw_key(sha256: str, fetched_at: datetime) -> str:
    """S3 key for a raw body. Partitioned by fetch date so old raw data is easy to expire."""
    return f"raw/{fetched_at:%Y/%m/%d}/{sha256}.html.gz"


def text_key(sha256: str) -> str:
    """S3 key for extracted text. Not date-partitioned: one object per distinct text."""
    return f"text/{sha256}.txt.gz"


def gzip_bytes(data: bytes) -> bytes:
    """Gzip with a fixed mtime, so identical input always produces identical bytes."""
    return gzip.compress(data, mtime=0)


class BlobStore(Protocol):
    """What the pipeline needs from object storage; lets tests swap in an in-memory store."""

    async def put_if_absent(self, key: str, data: bytes, content_type: str) -> bool: ...

    async def get(self, key: str) -> bytes: ...


class S3BlobStore:
    """BlobStore on S3 / MinIO via aioboto3. Objects are written gzipped; `get` returns decompressed bytes."""

    def __init__(self, settings: Settings) -> None:
        self._session = aioboto3.Session()
        self._bucket = settings.s3_bucket
        self._client_kwargs: dict[str, Any] = {
            "endpoint_url": settings.s3_endpoint_url,
            "region_name": settings.s3_region,
            "aws_access_key_id": settings.s3_access_key_id.get_secret_value() or None,
            "aws_secret_access_key": settings.s3_secret_access_key.get_secret_value() or None,
            "config": Config(retries={"max_attempts": 3, "mode": "standard"}, s3={"addressing_style": "path"}),
        }

    async def put_if_absent(self, key: str, data: bytes, content_type: str) -> bool:
        """Upload gzipped `data` unless `key` already exists. Returns True if it uploaded."""
        async with self._session.client("s3", **self._client_kwargs) as s3:
            try:
                await s3.head_object(Bucket=self._bucket, Key=key)
                return False
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") not in ("404", "NoSuchKey", "NotFound"):
                    raise
            await s3.put_object(
                Bucket=self._bucket, Key=key, Body=gzip_bytes(data), ContentType=content_type, ContentEncoding="gzip"
            )
            return True

    async def get(self, key: str) -> bytes:
        async with self._session.client("s3", **self._client_kwargs) as s3:
            obj = await s3.get_object(Bucket=self._bucket, Key=key)
            body: bytes = await obj["Body"].read()
        return gzip.decompress(body)

    async def presign_get(self, key: str, expires_s: int = 900) -> str:
        """A time-limited GET URL for one object, so API clients can fetch raw bodies without S3 credentials."""
        async with self._session.client("s3", **self._client_kwargs) as s3:
            url: str = await s3.generate_presigned_url(
                "get_object", Params={"Bucket": self._bucket, "Key": key}, ExpiresIn=expires_s
            )
        return url

    async def ping(self) -> None:
        """Raise unless the bucket is reachable with our credentials (used by the readiness probe)."""
        async with self._session.client("s3", **self._client_kwargs) as s3:
            await s3.head_bucket(Bucket=self._bucket)

    async def ensure_bucket(self) -> None:
        """Create the bucket if missing (compose's init container normally does this)."""
        async with self._session.client("s3", **self._client_kwargs) as s3:
            try:
                await s3.head_bucket(Bucket=self._bucket)
            except ClientError:
                await s3.create_bucket(Bucket=self._bucket)


class MemoryBlobStore:
    """In-memory BlobStore for unit tests and dry runs."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts = 0

    async def put_if_absent(self, key: str, data: bytes, content_type: str) -> bool:
        if key in self.objects:
            return False
        self.objects[key] = gzip_bytes(data)
        self.puts += 1
        return True

    async def get(self, key: str) -> bytes:
        return gzip.decompress(self.objects[key])

    async def presign_get(self, key: str, expires_s: int = 900) -> str:
        return f"memory://{key}?expires={expires_s}"

    async def ping(self) -> None:
        return None
