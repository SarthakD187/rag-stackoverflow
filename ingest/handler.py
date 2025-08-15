# ingest/handler.py
import os, json, typing as T, io, hashlib, time
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

REGION       = os.environ.get("AWS_REGION", "us-east-1")
BUCKET       = os.environ["INDEX_BUCKET"]
SEED_PREFIX  = os.getenv("SEED_PREFIX", "seed")
INDEX_PREFIX = os.getenv("INDEX_PREFIX", "rag-index")
EMBED_MODEL  = os.getenv("EMBED_MODEL_ID", "amazon.titan-embed-text-v1")

# pacing (seconds) between embedding calls to avoid throttling
EMBED_SLEEP_SECS = float(os.getenv("EMBED_SLEEP_SECS", "0.05"))

# write target
INDEX_KEY    = f"{INDEX_PREFIX}/chunks.jsonl"

# caps (safe defaults; override via event)
DEFAULT_LIMIT_FILES   = 50     # max files to consider per run
MAX_CHUNKS_PER_FILE   = 200    # safety cap to avoid huge single files

s3 = boto3.client("s3", region_name=REGION)
br = boto3.client(
    "bedrock-runtime",
    region_name=REGION,
    config=Config(connect_timeout=3, read_timeout=10, retries={"max_attempts": 2}),
)

# Skip common junk dirs so old build artifacts don't pollute the index
EXCLUDE_SUBSTR = (
    "/.git/", "/.venv/", "/venv/", "/node_modules/", "/dist/", "/build/",
    "/__pycache__/", "/.next/", "/.turbo/", "/.cache/", "/site-packages/",
    "/cdk.out/", "/.serverless/", "/.terraform/",
)
EXCLUDE_FILENAMES = ("LICENSE", "LICENSE.txt", "COPYING", "NOTICE", "CHANGES", "CHANGELOG", "CODE_OF_CONDUCT.md")

def _want_key(key: str) -> bool:
    if not key.startswith(SEED_PREFIX + "/"):
        return False
    if any(part in key for part in EXCLUDE_SUBSTR):
        return False
    fname = key.rsplit("/", 1)[-1]
    if fname in EXCLUDE_FILENAMES:
        return False
    return key.endswith((".md", ".mdx", ".txt"))

def list_seed_keys(limit: int) -> T.List[str]:
    keys: T.List[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=SEED_PREFIX + "/"):
        for obj in page.get("Contents", []):
            k = obj["Key"]
            if _want_key(k):
                keys.append(k)
                if len(keys) >= limit:
                    return keys
    return keys

def get_text(key: str) -> str:
    body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    return body.decode("utf-8", errors="ignore")

def _norm_text(t: str) -> str:
    # collapse whitespace + lowercase for robust de-dup
    return " ".join(t.split()).lower()

def embed(text: str) -> T.List[float]:
    payload = {"inputText": text}
    for attempt in (1, 2):  # targeted retry for throttling
        try:
            resp = br.invoke_model(
                modelId=EMBED_MODEL,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(payload).encode("utf-8"),
            )
            # gentle pacing to avoid ThrottlingException
            if EMBED_SLEEP_SECS > 0:
                time.sleep(EMBED_SLEEP_SECS)
            return json.loads(resp["body"].read())["embedding"]
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code == "ThrottlingException" and attempt == 1:
                time.sleep(0.25)
                continue
            raise

def chunk(text: str, chunk_chars=800, overlap=120) -> T.List[str]:
    chunks, i, n = [], 0, len(text)
    while i < n and len(chunks) < MAX_CHUNKS_PER_FILE:
        j = min(n, i + chunk_chars)
        c = text[i:j].strip()
        if c:
            chunks.append(c)
        i = j - overlap if j < n else j
    return chunks

def write_jsonl(rows: T.Iterable[dict], key: str):
    buf = io.BytesIO()
    for r in rows:
        buf.write(json.dumps(r, ensure_ascii=False).encode("utf-8"))
        buf.write(b"\n")
    buf.seek(0)
    s3.put_object(Bucket=BUCKET, Key=key, Body=buf.getvalue())

def delete_prefix(prefix: str):
    """Delete all objects under a prefix (safe for dev; beware in prod)."""
    paginator = s3.get_paginator("list_objects_v2")
    batch = []
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix if prefix.endswith("/") else prefix + "/"):
        for obj in page.get("Contents", []):
            batch.append({"Key": obj["Key"]})
            if len(batch) == 1000:
                s3.delete_objects(Bucket=BUCKET, Delete={"Objects": batch})
                batch = []
    if batch:
        s3.delete_objects(Bucket=BUCKET, Delete={"Objects": batch})

def lambda_handler(event=None, _ctx=None):
    event     = event or {}
    dry_run   = bool(event.get("dry_run"))
    truncate  = bool(event.get("truncate"))
    limit     = int(event.get("limit", DEFAULT_LIMIT_FILES))

    print("INGEST_START", {"bucket": BUCKET, "seed_prefix": SEED_PREFIX, "dry_run": dry_run,
                          "limit_files": limit, "truncate": truncate})

    # Optional: clear old index to avoid stale junk across runs
    if truncate:
        try:
            delete_prefix(INDEX_PREFIX)  # wipes rag-index/*
            print("TRUNCATE_OK", INDEX_PREFIX)
        except Exception as e:
            print("TRUNCATE_ERR", repr(e))

    keys = list_seed_keys(limit=limit)
    print("SEED_KEYS", {"count": len(keys)})

    if not keys:
        return {"statusCode": 200, "body": json.dumps({"message": "No seeds found", "indexed": 0})}

    rows: T.List[dict] = []
    seen_norm_text: set[str] = set()  # de-dupe identical chunks in this run

    for key in keys:
        try:
            text = get_text(key)
        except Exception as e:
            print("READ_ERR", {"key": key, "err": repr(e)})
            continue

        for i, ch in enumerate(chunk(text)):
            norm = _norm_text(ch)
            if norm in seen_norm_text:
                continue
            seen_norm_text.add(norm)

            if dry_run:
                vec = [0.0] * 1536  # skip Bedrock when dry-running
            else:
                vec = embed(ch)

            uid = hashlib.sha1(f"{key}|{i}|{norm}".encode("utf-8")).hexdigest()
            rows.append({
                "uid": uid,
                "path": key,
                "chunk_id": i,
                "text": ch,
                "vector": vec,
            })

    # Write a single, clean JSONL (overwrites any old file)
    write_jsonl(rows, INDEX_KEY)
    out_uri = f"s3://{BUCKET}/{INDEX_KEY}"
    print("BULK_DONE", {"rows": len(rows), "out": out_uri})

    return {"statusCode": 200, "body": json.dumps({"indexed": len(rows), "out": out_uri})}

if __name__ == "__main__":
    print(lambda_handler({"dry_run": True, "truncate": True}))
