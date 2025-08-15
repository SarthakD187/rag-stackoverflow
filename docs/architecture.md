# Project Architecture

- **Storage:** S3 bucket holds the vector index at `s3://$BUCKET/rag-index/chunks.jsonl`.
- **Ingest Lambda:** reads files from `s3://$BUCKET/seed/…`, chunks text (~500–800 chars), calls **Bedrock Titan Embeddings** (`amazon.titan-embed-text-v1`) and writes JSONL lines `{path, chunk_id, text, vector}`.
- **Query Lambda:** embeds the user question with Titan, streams the JSONL from S3, computes **cosine similarity** in pure Python, returns the top-k chunks.
- **Answer Lambda:** calls **Bedrock (Claude 3 Haiku)** with the top-k chunks to synthesize a concise answer.
- **API:** API Gateway HTTP API exposes `/query` and `/answer`.
- **Cost control:** No OpenSearch; vectors live in S3; Lambdas do stateless cosine search.
