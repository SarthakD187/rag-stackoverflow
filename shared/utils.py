"""Shared utility helpers for RAG Lambdas."""

from __future__ import annotations

import base64
import json
import logging
import math
from typing import Any, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)


def parse_event(event: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Parse Lambda event payload from direct invoke or HTTP API v2.

    Supports direct dictionary payloads and API Gateway events where request data
    is encoded in ``event['body']`` and optionally base64 encoded.

    Args:
        event: Raw Lambda event dictionary.

    Returns:
        Parsed request payload. Returns an empty dictionary when parsing fails.
    """
    if not isinstance(event, dict):
        return {}

    if "body" not in event:
        return event

    body = event.get("body")
    if event.get("isBase64Encoded") and isinstance(body, str):
        try:
            body = base64.b64decode(body).decode("utf-8", "replace")
        except Exception:
            logger.exception("Failed to decode base64 event body")
            return {}

    if isinstance(body, str):
        try:
            parsed = json.loads(body)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            logger.exception("Failed to parse JSON event body")
            return {}

    return body if isinstance(body, dict) else {}


def make_response(status_code: int, body: Mapping[str, Any]) -> dict[str, Any]:
    """Create a structured Lambda HTTP response payload."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "OPTIONS,POST",
        },
        "body": json.dumps(body),
    }


def validate_question(payload: Mapping[str, Any]) -> str:
    """Validate and normalize the required question field.

    Args:
        payload: Parsed request payload containing ``question``.

    Returns:
        Non-empty question text.

    Raises:
        ValueError: If question is missing or empty.
    """
    question = payload.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("'question' is required and must be a non-empty string.")
    return question.strip()


def parse_k(payload: Mapping[str, Any], default: int = 3) -> int:
    """Validate and normalize top-k retrieval parameter.

    Args:
        payload: Parsed request payload possibly containing ``k``.
        default: Value used when ``k`` is omitted.

    Returns:
        Integer k constrained to [1, 20].

    Raises:
        ValueError: If k is not an integer in [1, 20].
    """
    raw_k = payload.get("k", default)
    try:
        k = int(raw_k)
    except (TypeError, ValueError) as exc:
        raise ValueError("'k' must be an integer between 1 and 20.") from exc

    if k < 1 or k > 20:
        raise ValueError("'k' must be between 1 and 20.")
    return k


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Compute cosine similarity between two numeric vectors.

    Args:
        a: First vector-like sequence of numbers.
        b: Second vector-like sequence of numbers.

    Returns:
        Cosine similarity as a float in [-1.0, 1.0]. Returns 0.0 when either
        vector has zero magnitude or when no comparable dimensions are present.
    """
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0

    for ai, bi in zip(a, b):
        a_val = float(ai)
        b_val = float(bi)
        dot += a_val * b_val
        norm_a += a_val * a_val
        norm_b += b_val * b_val

    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def embed_text(br_client: Any, model_id: str, text: str) -> list[float]:
    """Create an embedding vector using an Amazon Bedrock embedding model.

    Args:
        br_client: Boto3 bedrock-runtime client.
        model_id: Bedrock model ID (for example ``amazon.titan-embed-text-v1``).
        text: Plain text input sent in ``{"inputText": text}`` format.

    Returns:
        Embedding vector returned by Bedrock response payload key ``embedding``.

    Raises:
        ValueError: If embedding field is missing or invalid.
    """
    payload = json.dumps({"inputText": text}).encode("utf-8")
    response = br_client.invoke_model(
        modelId=model_id,
        contentType="application/json",
        accept="application/json",
        body=payload,
    )
    parsed = json.loads(response["body"].read())
    embedding = parsed.get("embedding")
    if not isinstance(embedding, list):
        raise ValueError("Bedrock embedding response did not contain a valid 'embedding' array.")
    return [float(v) for v in embedding]


def chunk_text(
    text: str,
    chunk_chars: int = 800,
    overlap: int = 120,
    max_chunks: Optional[int] = None,
) -> list[str]:
    """Split text into overlapping character-based chunks.

    Uses a sliding window over characters with fixed overlap. Each chunk spans up
    to ``chunk_chars`` characters. The next chunk starts ``overlap`` characters
    before the previous chunk end. Empty/whitespace-only chunks are omitted.

    Args:
        text: Input text to split.
        chunk_chars: Maximum characters per chunk.
        overlap: Character overlap between adjacent chunks.
        max_chunks: Optional hard cap on number of chunks returned.

    Returns:
        Ordered list of non-empty chunk strings.

    Raises:
        ValueError: If ``chunk_chars`` is invalid or overlap is out of range.
    """
    if chunk_chars <= 0:
        raise ValueError("chunk_chars must be > 0")
    if overlap < 0 or overlap >= chunk_chars:
        raise ValueError("overlap must be >= 0 and < chunk_chars")

    chunks: list[str] = []
    start = 0
    text_len = len(text)

    while start < text_len:
        if max_chunks is not None and len(chunks) >= max_chunks:
            break

        end = min(text_len, start + chunk_chars)
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)

        if end >= text_len:
            break

        start = end - overlap

    return chunks
