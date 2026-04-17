"""Lambda handler for retrieval + answer synthesis with Bedrock text model."""

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
TEXT_MODEL = os.getenv("TEXT_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")

s3 = boto3.client("s3", region_name=REGION, config=Config(signature_version="s3v4"))
br = boto3.client(
    "bedrock-runtime",
    region_name=REGION,
    config=Config(retries={"max_attempts": 8, "mode": "standard"}),
)


def _stream_chunks(bucket: str, key: str) -> Iterator[tuple[str, list[float]]]:
    """Yield valid `(text, vector)` tuples from a JSONL index in S3."""
    obj = s3.get_object(Bucket=bucket, Key=key)
    for raw in obj["Body"].iter_lines():
        if not raw:
            continue
        row = json.loads(raw)
        text = row.get("text") or ""
        vector = row.get("vector") or []
        if text and vector:
            yield text, [float(v) for v in vector]


def _retrieve(question: str, k: int) -> tuple[list[str], list[float]]:
    """Retrieve top-k contexts by cosine similarity against question embedding."""
    question_vector = embed_text(br_client=br, model_id=EMBED_MODEL, text=question)
    scored: list[tuple[float, str]] = []
    seen_text: set[str] = set()

    for text, vector in _stream_chunks(INDEX_BUCKET, INDEX_KEY):
        if text in seen_text:
            continue
        seen_text.add(text)
        score = cosine_similarity(question_vector, vector)
        scored.append((score, text))

    scored.sort(key=lambda item: item[0], reverse=True)
    top = scored[:k]
    return [text for _, text in top], [float(score) for score, _ in top]


def _answer_with_claude(question: str, contexts: list[str]) -> str:
    bullets = "\n".join(f"- {context}" for context in contexts)
    prompt = f"""You are a concise technical assistant. Use only the context.

Context:
{bullets}

Question: {question}
Answer in 3-6 sentences. If context is insufficient, say so explicitly."""
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 512,
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
    }
    response = br.invoke_model(
        modelId=TEXT_MODEL,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(body).encode("utf-8"),
    )
    parsed = json.loads(response["body"].read())
    return "".join(part.get("text", "") for part in parsed.get("content", []))


def _answer_with_titan(question: str, contexts: list[str]) -> str:
    bullets = "\n".join(f"- {context}" for context in contexts)
    input_text = f"""Use only the context to answer.

Context:
{bullets}

Question: {question}
Answer in 3-6 sentences. If context is insufficient, say so."""
    body = {
        "inputText": input_text,
        "textGenerationConfig": {
            "maxTokenCount": 512,
            "temperature": 0.2,
            "topP": 0.9,
            "stopSequences": [],
        },
    }
    response = br.invoke_model(
        modelId=TEXT_MODEL,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(body).encode("utf-8"),
    )
    parsed = json.loads(response["body"].read())
    results = parsed.get("results", [])
    return results[0].get("outputText", "").strip() if results else ""


def _synthesize(question: str, contexts: list[str]) -> str:
    """Synthesize final answer from retrieved contexts using configured text model."""
    if not contexts:
        return "I couldn't find anything relevant in the knowledge base."

    if TEXT_MODEL.startswith("anthropic."):
        return _answer_with_claude(question, contexts)
    if TEXT_MODEL.startswith("amazon.titan-text"):
        return _answer_with_titan(question, contexts)

    logger.error("Unsupported text model id: %s", TEXT_MODEL)
    return "Configured text model is unsupported in this Lambda."


def lambda_handler(event: dict[str, Any] | None = None, _ctx: Any = None) -> dict[str, Any]:
    """Return top-k contexts and synthesized answer."""
    payload = parse_event(event or {})

    try:
        question = validate_question(payload)
        k = parse_k(payload, default=3)
    except ValueError as exc:
        return make_response(400, {"error": str(exc)})

    try:
        contexts, scores = _retrieve(question=question, k=k)
        answer = _synthesize(question=question, contexts=contexts)
        return make_response(
            200,
            {"question": question, "k": k, "contexts": contexts, "scores": scores, "answer": answer},
        )
    except Exception:
        logger.exception("ANSWER_ERR")
        return make_response(500, {"error": "Internal server error while generating answer."})
