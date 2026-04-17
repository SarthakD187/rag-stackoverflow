"""Lambda handler for retrieval-only cosine similarity over S3 JSONL index."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Iterator

import boto3
from botocore.config import Config

from shared.utils import (
    cosine_similarity,
    embed_text,
    make_response,
    parse_event,
    parse_k,
    validate_question,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

REGION = os.environ["AWS_REGION"]
INDEX_BUCKET = os.environ["INDEX_BUCKET"]
INDEX_PREFIX = os.environ.get("INDEX_PREFIX", "rag-index")
INDEX_KEY = f"{INDEX_PREFIX}/chunks.jsonl"
EMBED_MODEL = os.getenv("EMBED_MODEL_ID", "amazon.titan-embed-text-v1")

s3 = boto3.client("s3", region_name=REGION, config=Config(signature_version="s3v4"))
br = boto3.client(
    "bedrock-runtime",
    region_name=REGION,
    config=Config(retries={"max_attempts": 8, "mode": "standard"}),
)


def _stream_chunks(bucket: str, key: str) -> Iterator[tuple[str, list[float], str]]:
    """Yield valid `(text, vector, path)` tuples from a JSONL index in S3."""
    obj = s3.get_object(Bucket=bucket, Key=key)
    for raw in obj["Body"].iter_lines():
        if not raw:
            continue
        row = json.loads(raw)
        text = row.get("text") or ""
        vector = row.get("vector") or []
        path = row.get("path") or "unknown"
        if text and vector:
            yield text, [float(v) for v in vector], path


def _rank(question: str, k: int) -> tuple[list[str], list[float]]:
    """Rank indexed chunks by cosine similarity to question embedding."""
    question_vector = embed_text(br_client=br, model_id=EMBED_MODEL, text=question)
    scored: list[tuple[float, str]] = []
    seen_text: set[str] = set()

    for text, vector, _path in _stream_chunks(INDEX_BUCKET, INDEX_KEY):
        if text in seen_text:
            continue
        seen_text.add(text)
        score = cosine_similarity(question_vector, vector)
        scored.append((score, text))

    scored.sort(key=lambda item: item[0], reverse=True)
    top = scored[:k]
    return [text for _, text in top], [float(score) for score, _ in top]


def lambda_handler(event: dict[str, Any] | None = None, _ctx: Any = None) -> dict[str, Any]:
    """Return top-k retrieval contexts and similarity scores."""
    payload = parse_event(event or {})

    try:
        question = validate_question(payload)
        k = parse_k(payload, default=3)
    except ValueError as exc:
        return make_response(400, {"error": str(exc)})

    try:
        contexts, scores = _rank(question=question, k=k)
        return make_response(200, {"question": question, "k": k, "contexts": contexts, "scores": scores})
    except Exception:
        logger.exception("QUERY_ERR")
        return make_response(500, {"error": "Internal server error while retrieving contexts."})
