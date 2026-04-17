"""Lambda handler for building the S3 JSONL vector index."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import time
from typing import Any, Iterable

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from shared.utils import chunk_text, embed_text, make_response

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

REGION = os.environ["AWS_REGION"]
BUCKET = os.environ["INDEX_BUCKET"]
SEED_PREFIX = os.getenv("SEED_PREFIX", "seed")
INDEX_PREFIX = os.getenv("INDEX_PREFIX", "rag-index")
EMBED_MODEL = os.getenv("EMBED_MODEL_ID", "amazon.titan-embed-text-v1")
EMBED_SLEEP_SECS = float(os.getenv("EMBED_SLEEP_SECS", "0.05"))
INDEX_KEY = f"{INDEX_PREFIX}/chunks.jsonl"
DEFAULT_LIMIT_FILES = 50
MAX_CHUNKS_PER_FILE = 200

s3 = boto3.client("s3", region_name=REGION)
br = boto3.client(
    "bedrock-runtime",
    region_name=REGION,
    config=Config(connect_timeout=3, read_timeout=10, retries={"max_attempts": 2}),
)

EXCLUDE_SUBSTR = (
    "/.git/",
    "/.venv/",
    "/venv/",
    "/node_modules/",
    "/dist/",
    "/build/",
    "/__pycache__/",
    "/.next/",
    "/.turbo/",
    "/.cache/",
    "/site-packages/",
    "/cdk.out/",
    "/.serverless/",
    "/.terraform/",
)
EXCLUDE_FILENAMES = (
    "LICENSE",
    "LICENSE.txt",
    "COPYING",
    "NOTICE",
    "CHANGES",
    "CHANGELOG",
    "CODE_OF_CONDUCT.md",
)


def _want_key(key: str) -> bool:
    if not key.startswith(f"{SEED_PREFIX}/"):
        return False
    if any(part in key for part in EXCLUDE_SUBSTR):
        return False
    filename = key.rsplit("/", 1)[-1]
    if filename in EXCLUDE_FILENAMES:
        return False
    return key.endswith((".md", ".mdx", ".txt"))


def list_seed_keys(limit: int) -> list[str]:
    """List ingestible seed document keys under the configured seed prefix."""
    keys: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=f"{SEED_PREFIX}/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if _want_key(key):
                keys.append(key)
                if len(keys) >= limit:
                    return keys
    return keys


def get_text(key: str) -> str:
    """Read UTF-8 seed text from S3 object key."""
    body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    return body.decode("utf-8", errors="ignore")


def _norm_text(text: str) -> str:
    return " ".join(text.split()).lower()


def embed(text: str) -> list[float]:
    """Generate an embedding for a text chunk via configured Bedrock embedding model."""
    for attempt in (1, 2):
        try:
            vector = embed_text(br_client=br, model_id=EMBED_MODEL, text=text)
            if EMBED_SLEEP_SECS > 0:
                time.sleep(EMBED_SLEEP_SECS)
            return vector
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code == "ThrottlingException" and attempt == 1:
                time.sleep(0.25)
                continue
            raise

    raise RuntimeError("Failed to embed chunk after retries")


def write_jsonl(rows: Iterable[dict[str, Any]], key: str) -> None:
    """Write rows as JSONL to S3."""
    buffer = io.BytesIO()
    for row in rows:
        buffer.write(json.dumps(row, ensure_ascii=False).encode("utf-8"))
        buffer.write(b"\n")
    buffer.seek(0)
    s3.put_object(Bucket=BUCKET, Key=key, Body=buffer.getvalue())


def delete_prefix(prefix: str) -> None:
    """Delete all objects under an S3 prefix."""
    paginator = s3.get_paginator("list_objects_v2")
    batch: list[dict[str, str]] = []
    target_prefix = prefix if prefix.endswith("/") else f"{prefix}/"

    for page in paginator.paginate(Bucket=BUCKET, Prefix=target_prefix):
        for obj in page.get("Contents", []):
            batch.append({"Key": obj["Key"]})
            if len(batch) == 1000:
                s3.delete_objects(Bucket=BUCKET, Delete={"Objects": batch})
                batch = []

    if batch:
        s3.delete_objects(Bucket=BUCKET, Delete={"Objects": batch})


def lambda_handler(event: dict[str, Any] | None = None, _ctx: Any = None) -> dict[str, Any]:
    """Build or refresh the vector index from seed documents."""
    payload = event or {}

    try:
        dry_run = bool(payload.get("dry_run"))
        truncate = bool(payload.get("truncate"))
        limit = int(payload.get("limit", DEFAULT_LIMIT_FILES))
        if limit < 1:
            return make_response(400, {"error": "'limit' must be a positive integer."})
    except (TypeError, ValueError):
        return make_response(400, {"error": "'limit' must be a positive integer."})

    logger.info(
        "INGEST_START bucket=%s seed_prefix=%s limit=%s truncate=%s dry_run=%s",
        BUCKET,
        SEED_PREFIX,
        limit,
        truncate,
        dry_run,
    )

    if truncate:
        try:
            delete_prefix(INDEX_PREFIX)
            logger.info("TRUNCATE_OK prefix=%s", INDEX_PREFIX)
        except Exception:
            logger.exception("TRUNCATE_ERR prefix=%s", INDEX_PREFIX)
            return make_response(500, {"error": "Failed to truncate existing index prefix."})

    keys = list_seed_keys(limit=limit)
    logger.info("SEED_KEYS count=%s", len(keys))

    if not keys:
        return make_response(200, {"message": "No seeds found", "indexed": 0})

    rows: list[dict[str, Any]] = []
    seen_norm_text: set[str] = set()

    for key in keys:
        try:
            text = get_text(key)
        except Exception:
            logger.exception("READ_ERR key=%s", key)
            continue

        chunks = chunk_text(text=text, chunk_chars=800, overlap=120, max_chunks=MAX_CHUNKS_PER_FILE)
        for chunk_id, chunk_value in enumerate(chunks):
            normalized = _norm_text(chunk_value)
            if normalized in seen_norm_text:
                continue
            seen_norm_text.add(normalized)

            try:
                vector = [0.0] * 1536 if dry_run else embed(chunk_value)
            except Exception:
                logger.exception("EMBED_ERR key=%s chunk_id=%s", key, chunk_id)
                continue

            uid = hashlib.sha1(f"{key}|{chunk_id}|{normalized}".encode("utf-8")).hexdigest()
            rows.append(
                {
                    "uid": uid,
                    "path": key,
                    "chunk_id": chunk_id,
                    "text": chunk_value,
                    "vector": vector,
                }
            )

    try:
        write_jsonl(rows, INDEX_KEY)
    except Exception:
        logger.exception("WRITE_ERR key=%s", INDEX_KEY)
        return make_response(500, {"error": "Failed to write index to S3."})

    output_uri = f"s3://{BUCKET}/{INDEX_KEY}"
    logger.info("BULK_DONE rows=%s out=%s", len(rows), output_uri)
    return make_response(200, {"indexed": len(rows), "out": output_uri})
