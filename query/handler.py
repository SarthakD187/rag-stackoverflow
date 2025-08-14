# query/handler.py
import os, json, typing as T, boto3
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth
from opensearchpy.exceptions import TransportError

REGION = os.environ.get("AWS_REGION", "us-east-1")
HOST = os.environ["OS_ENDPOINT"].replace("https://", "").replace("http://", "")
INDEX = os.environ["OS_INDEX"]
MODEL_ID = os.getenv("EMBED_MODEL_ID", "amazon.titan-embed-text-v1")

bedrock = boto3.client("bedrock-runtime", region_name=REGION)

def embed(text: str) -> T.List[float]:
    body = json.dumps({"inputText": text}).encode("utf-8")
    resp = bedrock.invoke_model(modelId=MODEL_ID, contentType="application/json", accept="application/json", body=body)
    return json.loads(resp["body"].read())["embedding"]

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
    body = {
        "size": k,
        "_source": ["text"],
        "query": {"knn": {"field": "vector", "query_vector": q_vec, "k": k, "num_candidates": max(100, 5*k)}},
    }
    try:
        res = client.search(index=INDEX, body=body)
        return [h.get("_source", {}).get("text", "") for h in res.get("hits", {}).get("hits", [])]
    except TransportError as e:
        print("SEARCH_ERROR", getattr(e, "status_code", None), getattr(e, "info", None))
        raise

def lambda_handler(event=None, _ctx=None):
    if not event: event = {}
    q = event.get("question") or "Hello world"
    k = int(event.get("k", 3))
    q_vec = embed(q)
    client = connect()
    texts = knn_search(client, q_vec, k=k)
    return {"statusCode": 200, "body": json.dumps({"question": q, "k": k, "results": texts})}
