# ingest/handler.py
import os, json, time, typing as T
import boto3
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth
from opensearchpy.exceptions import TransportError

REGION = os.environ.get("AWS_REGION", "us-east-1")
HOST = os.environ["OS_ENDPOINT"].replace("https://", "").replace("http://", "")
INDEX = os.environ["OS_INDEX"]
MODEL_ID = os.getenv("EMBED_MODEL_ID", "amazon.titan-embed-text-v1")

bedrock = boto3.client("bedrock-runtime", region_name=REGION)

def embed(text: str) -> T.List[float]:
    body = json.dumps({"inputText": text}).encode("utf-8")
    resp = bedrock.invoke_model(
        modelId=MODEL_ID, contentType="application/json", accept="application/json", body=body
    )
    return json.loads(resp["body"].read())["embedding"]

def connect() -> OpenSearch:
    session = boto3.Session()
    auth = AWSV4SignerAuth(session.get_credentials(), REGION, service="aoss")
    return OpenSearch(
        hosts=[{"host": HOST, "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
    )

def ensure_index(client: OpenSearch):
    """Create k-NN index; ignore 'already exists'; then wait until it’s visible."""
    dim = len(embed("dimension probe"))
    mapping = {
        "settings": {"index": {"knn": True}},
        "mappings": {
            "properties": {
                "text": {"type": "text"},
                "vector": {
                    "type": "knn_vector",
                    "dimension": dim,
                    "method": {"name": "hnsw", "space_type": "l2", "engine": "faiss"},
                },
            }
        },
    }
    try:
        resp = client.indices.create(index=INDEX, body=mapping)
        print("CREATE_INDEX_RESP", resp)
    except TransportError as e:
        # 400 → already exists; anything else: bubble up
        print("CREATE_INDEX_ERROR", getattr(e, "status_code", None), getattr(e, "info", None))
        if getattr(e, "status_code", None) not in (400,):
            raise

    # Wait until HEAD /{index} returns true (AOSS can be eventually consistent)
    for attempt in range(20):  # ~10s max
        try:
            if client.indices.exists(index=INDEX):
                print("INDEX_READY", attempt)
                return
        except Exception as ex:
            print("INDEX_EXISTS_CHECK_ERR", repr(ex))
        time.sleep(0.5)
    raise RuntimeError("Index did not become ready in time")

def bulk_index(client: OpenSearch, docs: T.List[str]):
    actions = []
    for t in docs:
        v = embed(t)
        # AOSS will auto-generate IDs; don't send _id
        actions.append({"index": {"_index": INDEX}})
        actions.append({"text": t, "vector": v})
    body = "\n".join(json.dumps(a) for a in actions) + "\n"

    # ✅ No refresh param for AOSS (not supported)
    resp = client.bulk(body=body)

    if resp.get("errors"):
        bad = [it for it in resp.get("items", []) if list(it.values())[0].get("error")]
        print("BULK_FIRST_ERRORS", json.dumps(bad[:3]))
    return resp

def lambda_handler(event=None, _ctx=None):
    print("CALLER", boto3.client("sts").get_caller_identity())
    docs = [
        "Hello world: first RAG document.",
        "This project indexes text into OpenSearch Serverless using Titan embeddings.",
        "Ask a question; we retrieve the most relevant chunks with k-NN search.",
    ]
    client = connect()
    ensure_index(client)
    result = bulk_index(client, docs)
    return {
        "statusCode": 200,
        "body": json.dumps({"bulk_errors": result.get("errors", False), "items": len(result.get("items", []))}),
    }

if __name__ == "__main__":
    print(lambda_handler())
