# answer/handler.py
# Retrieval-augmented answer: fetch top-k docs from AOSS, then call a Bedrock text model.
# Supports Anthropic Claude 3 (recommended) OR Titan Text based on TEXT_MODEL_ID.

import os, json, typing as T, boto3
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth

REGION       = os.environ.get("AWS_REGION", "us-east-1")
HOST         = os.environ["OS_ENDPOINT"].replace("https://","").replace("http://","")
INDEX        = os.environ["OS_INDEX"]
EMBED_MODEL  = os.getenv("EMBED_MODEL_ID", "amazon.titan-embed-text-v1")
TEXT_MODEL   = os.getenv("TEXT_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")

br = boto3.client("bedrock-runtime", region_name=REGION)

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

def retrieve(question: str, k: int = 4) -> T.List[str]:
    vec = embed(question)
    # AOSS-friendly kNN body (we used this shape in QueryFn)
    body = {"size": k, "_source": ["text"], "query": {"knn": {"vector": {"vector": vec, "k": k}}}}
    res = aoss().search(index=INDEX, body=body)
    # De-dupe texts while preserving order
    seen, out = set(), []
    for h in res.get("hits", {}).get("hits", []):
        t = h.get("_source", {}).get("text", "")
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out

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
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": prompt}]}
        ],
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
    # Titan Text responses: {"results":[{"outputText": "..."}]}
    results = j.get("results", [])
    return results[0].get("outputText", "").strip() if results else ""

def synthesize(question: str, contexts: T.List[str]) -> str:
    if not contexts:
        return "I couldn't find anything relevant in the knowledge base."
    # Route based on model family
    if TEXT_MODEL.startswith("anthropic."):
        return _answer_with_claude(question, contexts)
    elif TEXT_MODEL.startswith("amazon.titan-text"):
        return _answer_with_titan(question, contexts)
    else:
        return "Configured TEXT_MODEL_ID is unsupported in this Lambda."

def lambda_handler(event=None, _ctx=None):
    event = event or {}
    q = event.get("question") or "What does this project do?"
    k = int(event.get("k", 4))
    ctx = retrieve(q, k)
    ans = synthesize(q, ctx)
    return {
        "statusCode": 200,
        "body": json.dumps({"question": q, "contexts": ctx, "answer": ans}),
    }

if __name__ == "__main__":
    print(lambda_handler({"question": "What does this project do?"}))
