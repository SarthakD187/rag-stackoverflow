# answer/handler.py
# Uses the same S3 index retrieval as query/, then synthesizes an answer with Claude 3 Haiku
# (or Titan Text if TEXT_MODEL_ID starts with "amazon.titan-text").

import os, json, math, base64, typing as T
import boto3
from botocore.config import Config

REGION        = os.environ.get("AWS_REGION", "us-east-1")
INDEX_BUCKET  = os.environ["INDEX_BUCKET"]
INDEX_PREFIX  = os.environ.get("INDEX_PREFIX", "rag-index")
INDEX_KEY     = f"{INDEX_PREFIX}/chunks.jsonl"
EMBED_MODEL   = os.getenv("EMBED_MODEL_ID", "amazon.titan-embed-text-v1")
TEXT_MODEL    = os.getenv("TEXT_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")

s3 = boto3.client("s3", region_name=REGION, config=Config(signature_version="s3v4"))
br = boto3.client("bedrock-runtime", region_name=REGION, config=Config(retries={'max_attempts': 8, 'mode': 'standard'}))

# ——— HTTP parsing ———
def _parse_event(event: dict) -> dict:
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

# ——— Embedding & index streaming ———
def _embed(text: str) -> T.List[float]:
    payload = json.dumps({"inputText": text}).encode("utf-8")
    r = br.invoke_model(modelId=EMBED_MODEL, contentType="application/json", accept="application/json", body=payload)
    return json.loads(r["body"].read())["embedding"]

def _cosine(a: T.List[float], b: T.List[float]) -> float:
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
    for raw in obj["Body"].iter_lines():
        if not raw:
            continue
        j = json.loads(raw)
        text = j.get("text") or ""
        vec  = j.get("vector") or []
        if text and vec:
            yield text, vec

def _retrieve(question: str, k: int) -> T.Tuple[T.List[str], T.List[float]]:
    q_vec = _embed(question)
    scored: T.List[T.Tuple[float, str]] = []
    seen = set()
    for text, vec in _stream_chunks(INDEX_BUCKET, INDEX_KEY):
        if text in seen:
            continue
        seen.add(text)
        s = _cosine(q_vec, vec)
        scored.append((s, text))
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:k]
    return [t for _, t in top], [float(s) for s, _ in top]

# ——— Synthesis (Claude or Titan) ———
def _answer_with_claude(question: str, ctx: T.List[str]) -> str:
    bullets = "\n".join(f"- {c}" for c in ctx)
    prompt = f"""You are a concise technical assistant. Use only the context.

Context:
{bullets}

Question: {question}
Answer in 3–6 sentences. If context is insufficient, say so explicitly."""
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 512,
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
    }
    r = br.invoke_model(modelId=TEXT_MODEL, contentType="application/json", accept="application/json", body=json.dumps(body).encode())
    j = json.loads(r["body"].read())
    return "".join(part.get("text", "") for part in j.get("content", []))

def _answer_with_titan(question: str, ctx: T.List[str]) -> str:
    bullets = "\n".join(f"- {c}" for c in ctx)
    input_text = f"""Use only the context to answer.

Context:
{bullets}

Question: {question}
Answer in 3–6 sentences. If context is insufficient, say so."""
    body = {"inputText": input_text, "textGenerationConfig": {"maxTokenCount": 512, "temperature": 0.2, "topP": 0.9, "stopSequences": []}}
    r = br.invoke_model(modelId=TEXT_MODEL, contentType="application/json", accept="application/json", body=json.dumps(body).encode())
    j = json.loads(r["body"].read())
    res = j.get("results", [])
    return res[0].get("outputText", "").strip() if res else ""

def _synthesize(question: str, contexts: T.List[str]) -> str:
    if not contexts:
        return "I couldn't find anything relevant in the knowledge base."
    if TEXT_MODEL.startswith("anthropic."):
        return _answer_with_claude(question, contexts)
    if TEXT_MODEL.startswith("amazon.titan-text"):
        return _answer_with_titan(question, contexts)
    return "Configured TEXT_MODEL_ID is unsupported in this Lambda."

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
        ctx, scores = _retrieve(q, k)
        ans = _synthesize(q, ctx)
        return _resp({"question": q, "k": k, "contexts": ctx, "scores": scores, "answer": ans}, 200)
    except Exception as e:
        return _resp({"error": str(e)}, 500)

if __name__ == "__main__":
    print(lambda_handler({"question": "What does this project do?", "k": 4}))
