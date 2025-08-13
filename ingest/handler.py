# ingest/handler.py
# Index demo docs into OpenSearch Serverless using Amazon Titan embeddings.
# Env vars provided by the CDK stack:
#   OS_ENDPOINT   -> e.g. https://xxxxxxxxxx.region.aoss.amazonaws.com
#   OS_INDEX      -> e.g. "docs"
#   OS_COLLECTION -> e.g. "rag-vectors" (not used directly here)
#   EMBED_MODEL_ID -> e.g. "amazon.titan-embed-text-v1" (G1 Text) or "amazon.titan-embed-text-v2:0"

import os, json, typing as T
import boto3
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth

REGION = os.environ.get("AWS_REGION", "us-east-1")
HOST = os.environ["OS_ENDPOINT"].replace("https://", "").replace("http://", "")
INDEX = os.environ["OS_INDEX"]
MODEL_ID = os.getenv("EMBED_MODEL_ID", "amazon.titan-embed-text-v1")  # default to G1 Text

bedrock = boto3.client("bedrock-runtime", region_name=REGION)

def embed(text: str) -> T.List[float]:
    """Call Bedrock embeddings model and return a float vector."""
    body = json.dumps({"inputText": text}).encode("utf-8")
    resp = bedrock.invoke_model(
        modelId=MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=body,
    )
    payload = json.loads(resp["body"].read())
    return payload["embedding"]

def connect() -> OpenSearch:
    """SigV4-authenticated OpenSearch Serverless client (service='aoss')."""
    session = boto3.Session()
    creds = session.get_credentials()
    auth = AWSV4SignerAuth(creds, REGION, service="aoss")
    return OpenSearch(
        hosts=[{"host": HOST, "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
    )

def ensure_index(client: OpenSearch):
    """
    Create a KNN index if it doesn't exist yet.
    Dimension is derived from the model by doing one tiny embed call,
    so this works for both Titan G1 (v1) and Titan v2.
    """
    if client.indices.exists(index=INDEX):
        return
    dim = len(embed("dimension probe"))  # 1st call: figure out vector size
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
    client.indices.create(index=INDEX, body=mapping)

def bulk_index(client: OpenSearch, docs: T.List[str]):
    """NDJSON bulk index: [{index:..}, {doc}, ...]."""
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
    # Replace these with your real corpus soon.
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
        "body": json.dumps(
            {"bulk_errors": result.get("errors", False), "items": len(result.get("items", []))}
        ),
    }

if __name__ == "__main__":
    print(lambda_handler())
