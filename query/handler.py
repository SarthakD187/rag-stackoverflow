# query/handler.py
import os, json, typing as T, boto3, time
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth
from opensearchpy.exceptions import TransportError

REGION = os.environ.get("AWS_REGION", "us-east-1")
HOST = os.environ["OS_ENDPOINT"].replace("https://", "").replace("http://", "")
INDEX = os.environ["OS_INDEX"]
MODEL_ID = os.getenv("EMBED_MODEL_ID", "amazon.titan-embed-text-v1")

bedrock = boto3.client("bedrock-runtime", region_name=REGION)

def embed(text: str) -> T.List[float]:
    body = json.dumps({"inputText": text}).encode("utf-8")
    r = bedrock.invoke_model(modelId=MODEL_ID, contentType="application/json", accept="application/json", body=body)
    return json.loads(r["body"].read())["embedding"]

def connect() -> OpenSearch:
    s = boto3.Session()
    auth = AWSV4SignerAuth(s.get_credentials(), REGION, service="aoss")
    return OpenSearch(
        hosts=[{"host": HOST, "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
    )

def knn_search(client: OpenSearch, q_vec: T.List[float], k: int = 3):
    # AOSS-friendly bodies (we already know these shapes work)
    bodies = [
        {"size": k, "_source": ["text"], "query": {"knn": {"vector": {"vector": q_vec, "k": k}}}},
        {"size": k, "_source": ["text"], "query": {"knn": {"vector": {"values": q_vec, "k": k}}}},
    ]
    last_err = None
    for i, body in enumerate(bodies, 1):
        try:
            res = client.search(index=INDEX, body=body)
            hits = res.get("hits", {}).get("hits", [])
            # return text + score so we can dedupe and show confidence if needed
            return [{"text": h.get("_source", {}).get("text", ""), "score": h.get("_score")} for h in hits]
        except TransportError as e:
            print(f"SEARCH_SHAPE_{i}_ERROR", getattr(e, "status_code", None), getattr(e, "info", None))
            last_err = e
            if getattr(e, "status_code", None) != 400:
                break
    raise last_err or RuntimeError("Search failed")

def lambda_handler(event=None, _ctx=None):
    event = event or {}
    question = event.get("question") or "Hello world"
    k = int(event.get("k", 3))

    q_vec = embed(question)
    client = connect()
    raw = knn_search(client, q_vec, k=k)

    # ✅ Deduplicate by text while preserving order
    seen = set()
    uniq = []
    for h in raw:
        t = h["text"]
        if t and t not in seen:
            seen.add(t)
            uniq.append(h)

    return {
        "statusCode": 200,
        "body": json.dumps({
            "question": question,
            "k": k,
            "results": [h["text"] for h in uniq],
            "scores": [h["score"] for h in uniq],
        }),
    }
if __name__ == "__main__":
    print(lambda_handler({"question": "What does this project do?", "k": 3}))
