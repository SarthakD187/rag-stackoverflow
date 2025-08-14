# ingest/handler.py
# Index demo docs into OpenSearch Serverless using Amazon Titan embeddings.
# Env vars:
#   OS_ENDPOINT, OS_INDEX, OS_COLLECTION, EMBED_MODEL_ID
import os, json, typing as T
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
        modelId=MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=body,
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
    """
    Idempotent index creation (no pre-check). Works even if exists.
    We probe the embedding dimension once for a correct knn_vector mapping.
    """
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
        client.indices.create(index=INDEX, body=mapping)
    except TransportError as e:
        # 400 is "resource_already_exists_exception" — ignore it
        if getattr(e, "status_code", None) not in (400,):
            raise

def bulk_index(client: OpenSearch, docs: T.List[str]):
    actions = []
    for i, t in enumerate(docs):
        v = embed(t)
        actions.append({"index": {"_index": INDEX, "_id": str(i)}})
        actions.append({"text": t, "vector": v})
    body = "\n".join(json.dumps(a) for a in actions) + "\n"
    resp = client.bulk(body=body)
    client.indices.refresh(index=INDEX)
    return resp

def lambda_handler(event=None, _ctx=None):
    # 🔎 Log the caller so we can match it to policy principals
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
