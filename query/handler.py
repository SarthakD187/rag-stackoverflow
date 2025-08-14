# query/handler.py
# Retrieve top-k docs from OpenSearch Serverless using Titan embeddings.

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

def knn_search(client: OpenSearch, q_vec: T.List[float], k: int = 3):
    """
    Standard k-NN query for a knn_vector field named 'vector'.
    """
    body = {
        "size": k,
        "_source": ["text"],
        "query": {
            "knn": {
                "vector": {  # field name in your mapping
                    "vector": q_vec,
                    "k": k
                }
            }
        },
    }
    try:
        res = client.search(index=INDEX, body=body)
        hits = res.get("hits", {}).get("hits", [])
        return [h.get("_source", {}).get("text", "") for h in hits]
    except TransportError as e:
        # If you see a 400 here, print the body so we can adjust quickly
        print("SEARCH_ERROR", getattr(e, "status_code", None), getattr(e, "info", None))
        raise

def lambda_handler(event=None, _ctx=None):
    """
    event:
      {
        "question": "What does this project do?",
        "k": 3
      }
    """
    print("CALLER", boto3.client("sts").get_caller_identity())

    if not event:
        event = {}
    question = event.get("question") or "Hello world"
    k = int(event.get("k", 3))

    q_vec = embed(question)
    client = connect()
    texts = knn_search(client, q_vec, k=k)

    # Simple, direct return of retrieved texts (no LLM synthesis yet)
    return {
        "statusCode": 200,
        "body": json.dumps({
            "question": question,
            "k": k,
            "results": texts
        }),
    }

if __name__ == "__main__":
    print(lambda_handler({"question": "What does this project do?", "k": 3}))
