# query/handler.py
# Reads vectors from s3://INDEX_BUCKET/INDEX_PREFIX/chunks.jsonl
# Computes cosine similarity (pure Python) against Titan embeddings.
# Works for both Lambda invoke and API Gateway v2 (HTTP API).

import os, json, math, base64, typing as T
import boto3
from botocore.config import Config

REGION        = os.environ.get("AWS_REGION", "us-east-1")
INDEX_BUCKET  = os.environ["INDEX_BUCKET"]
INDEX_PREFIX  = os.environ.get("INDEX_PREFIX", "rag-index")
INDEX_KEY     = f"{INDEX_PREFIX}/chunks.jsonl"
EMBED_MODEL   = os.getenv("EMBED_MODEL_ID", "amazon.titan-embed-text-v1")

s3 = boto3.client("s3", region_name=REGION, config=Config(signature_version="s3v4"))
br = boto3.client("bedrock-runtime", region_name=REGION, config=Config(retries={'max_attempts': 8, 'mode': 'standard'}))

# ——— Helpers ———

def _parse_event(event: dict) -> dict:
    """Accept both direct invoke (dict) and HTTP API (body string, maybe base64)."""
    if not isinstance(event, dict):
        return {}
    if "body" in event:
        body = event["body"]
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode("utf-8", "replace")
        try:
            return json.loads(body) if isinstance(body, str) else (body or {})
        except Exception:
            return {}
    return event

def _embed(text: str) -> T.List[float]:
    payload = json.dumps({"inputText": text}).encode("utf-8")
    r = br.invoke_model(
        modelId=EMBED_MODEL,
        contentType="application/json",
        accept="application/json",
        body=payload,
    )
    return json.loads(r["body"].read())["embedding"]

def _cosine(a: T.List[float], b: T.List[float]) -> float:
    # Robust to zeros/short arrays
    s = 0.0; na = 0.0; nb = 0.0
    ln = min(len(a), len(b))
    for i in range(ln):
        ai = float(a[i]); bi = float(b[i])
        s += ai * bi; na += ai * ai; nb += bi * bi
    if na == 0.0 or nb == 0.0:
        return 0.0
    return s / (math.sqrt(na) * math.sqrt(nb))

def _stream_chunks(bucket: str, key: str):
    obj = s3.get_object(Bucket=bucket, Key=key)
    # iterate line by line to support large files
    for raw in obj["Body"].iter_lines():
        if not raw:
            continue
        j = json.loads(raw)
        text = j.get("text") or ""
        vec  = j.get("vector") or []
        path = j.get("path") or "unknown"
        if text and vec:
            yield (text, vec, path)

def _rank(question: str, k: int) -> T.Tuple[T.List[str], T.List[float]]:
    q_vec = _embed(question)
    scored: T.List[T.Tuple[float, str]] = []
    seen = set()
    for text, vec, path in _stream_chunks(INDEX_BUCKET, INDEX_KEY):
        # skip exact dupes of text
        if text in seen:
            continue
        seen.add(text)
        score = _cosine(q_vec, vec)
        scored.append((score, text))
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:k]
    return [t for _, t in top], [float(s) for s, _ in top]

def _resp(body: dict, code: int = 200):
    return {
        "statusCode": code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "OPTIONS,POST",
        },
        "body": json.dumps(body),
    }

# ——— Handler ———

def lambda_handler(event=None, _ctx=None):
    payload = _parse_event(event or {})
    q = payload.get("question") or "What does this project do?"
    k = int(payload.get("k", 4))
    try:
        contexts, scores = _rank(q, k)
        return _resp({"question": q, "k": k, "contexts": contexts, "scores": scores}, 200)
    except Exception as e:
        return _resp({"error": str(e)}, 500)

if __name__ == "__main__":
    print(lambda_handler({"question": "What does this project do?"}))
