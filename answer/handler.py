# answer/handler.py
# Retrieval-augmented answer: fetch top-k docs from AOSS, then call a Bedrock text model.
# Works with API Gateway proxy events (CORS) and direct Lambda test events.

import os, json, typing as T, boto3
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth

REGION       = os.environ.get("AWS_REGION", "us-east-1")
HOST         = os.environ["OS_ENDPOINT"].replace("https://","").replace("http://","")
INDEX        = os.environ["OS_INDEX"]
EMBED_MODEL  = os.getenv("EMBED_MODEL_ID", "amazon.titan-embed-text-v1")
TEXT_MODEL   = os.getenv("TEXT_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")

br = boto3.client("bedrock-runtime", region_name=REGION)

# ---------- helpers: API Gateway parsing + CORS ----------
def _parse_event(event: T.Any) -> dict:
    """Accept either direct events or API Gateway proxy events."""
    if isinstance(event, dict) and "body" in event:
        try:
            return json.loads(event["body"] or "{}")
        except Exception:
            return {}
    return event if isinstance(event, dict) else {}

def _resp(payload: dict, status: int = 200) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",  # dev-friendly; tighten in prod
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "OPTIONS,POST",
        },
        "body": json.dumps(payload),
    }

# ---------- embeddings + search ----------
def embed(text: str) -> T.List[float]:
    body = json.dumps({"inputText": text}).encode("utf-8")
    r = br.invoke_model(modelId=EMBED_MODEL, contentType="application/json", accept="application/json", body=body)
    return json.loads(r["body"].read())["embedding"]

def aoss() -> OpenSearch:
    s = boto3.Session()
    auth = AWSV4SignerAuth(s.get_credentials(), REGION, service="aoss")
    return OpenSearch(
        hosts=[{"host": HOST, "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
    )

def retrieve(question: str, k: int = 4) -> T.List[T.Tuple[str, float]]:
    """Return list of (text, score) from AOSS."""
    vec = embed(question)
    body = {"size": k, "_source": ["text"], "query": {"knn": {"vector": {"vector": vec, "k": k}}}}
    res = aoss().search(index=INDEX, body=body)
    seen, out = set(), []
    for h in res.get("hits", {}).get("hits", []):
        t = h.get("_source", {}).get("text", "")
        if t and t not in seen:
            seen.add(t)
            out.append((t, float(h.get("_score", 0.0))))
    return out

# ---------- LLM synthesis ----------
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
    r = br.invoke_model(
        modelId=TEXT_MODEL,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(body).encode(),
    )
    j = json.loads(r["body"].read())
    return "".join(part.get("text", "") for part in j.get("content", []))

def _answer_with_titan(question: str, ctx: T.List[str]) -> str:
    bullets = "\n".join(f"- {c}" for c in ctx)
    input_text = f"""Use only the context to answer.

Context:
{bullets}

Question: {question}
Answer in 3–6 sentences. If context is insufficient, say so."""
    body = {
        "inputText": input_text,
        "textGenerationConfig": {
            "maxTokenCount": 512,
            "temperature": 0.2,
            "topP": 0.9,
            "stopSequences": [],
        },
    }
    r = br.invoke_model(
        modelId=TEXT_MODEL,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(body).encode(),
    )
    j = json.loads(r["body"].read())
    results = j.get("results", [])
    return results[0].get("outputText", "").strip() if results else ""

def synthesize(question: str, contexts: T.List[str]) -> str:
    if not contexts:
        return "I couldn't find anything relevant in the knowledge base."
    if TEXT_MODEL.startswith("anthropic."):
        return _answer_with_claude(question, contexts)
    elif TEXT_MODEL.startswith("amazon.titan-text"):
        return _answer_with_titan(question, contexts)
    else:
        return "Configured TEXT_MODEL_ID is unsupported in this Lambda."

# ---------- Lambda entry ----------
def lambda_handler(event=None, _ctx=None):
    # Handle CORS preflight from API Gateway
    if isinstance(event, dict) and event.get("httpMethod") == "OPTIONS":
        return _resp({"ok": True})

    data = _parse_event(event or {})
    q = (data.get("question") or "").strip()
    k = int(data.get("k") or 4)
    if not q:
        return _resp({"error": "Missing 'question'."}, 400)

    try:
        pairs = retrieve(q, k)                # [(text, score), ...]
        ctx_texts = [t for t, _ in pairs]
        scores    = [s for _, s in pairs]
        ans = synthesize(q, ctx_texts)
        return _resp({"question": q, "contexts": ctx_texts, "scores": scores, "answer": ans})
    except Exception as e:
        # Minimal error surface (don’t leak internals)
        return _resp({"error": "Answer failed", "detail": str(e)}, 500)

if __name__ == "__main__":
    print(lambda_handler({"question": "What does this project do?", "k": 3}))
