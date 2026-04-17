# Internal Stack Overflow (RAG)

Serverless RAG on AWS using Bedrock embeddings and an S3 JSONL vector index.

Pipeline:

`Docs (S3 seed/) -> IngestFn (chunk + embed) -> S3 JSONL index -> QueryFn (cosine top-k) -> AnswerFn (prompt + Bedrock text model) -> API Gateway response`

## Architecture

- `IngestFn` reads seed docs from `s3://$INDEX_BUCKET/$SEED_PREFIX/...`, chunks text, embeds with Titan, writes `s3://$INDEX_BUCKET/$INDEX_PREFIX/chunks.jsonl`.
- `QueryFn` validates input, embeds question, computes cosine similarity over JSONL vectors, returns top-k contexts + scores.
- `AnswerFn` reuses retrieval logic and synthesizes a grounded answer using Claude 3 Haiku or Titan Text Lite.

## Repository layout

```
.
├─ ingest/             # Ingest Lambda
├─ query/              # Retrieval Lambda
├─ answer/             # Retrieval + synthesis Lambda
├─ shared/             # Shared Python utils for all Lambdas
├─ infra/              # AWS CDK stack (TypeScript)
├─ web/                # Minimal frontend
└─ tests/              # pytest unit tests for shared utils
```

## Prerequisites

- AWS CLI configured (`aws sts get-caller-identity`)
- Node.js 20+, npm, Docker (for Lambda Python bundling)
- Python 3.11+
- CDK bootstrap done for deploy account/region
- Bedrock model access enabled in deploy region:
  - Embedding: `amazon.titan-embed-text-v1`
  - Text: `anthropic.claude-3-haiku-20240307-v1:0` or `amazon.titan-text-lite-v1`

## Deploy

```bash
cd infra
npm i
npm run build
npx aws-cdk@latest deploy --require-approval never \
  -c frontendOrigin=http://localhost:3000 \
  -c enableNightlyIngest=true
```

Optional context values:

- `frontendOrigin` (default `*`) controls API CORS allow origin.
- `enableNightlyIngest` (default `true`) controls EventBridge nightly re-index rule.
- `embedModelId` and `textModelId` override default Bedrock model IDs.

Stack outputs:

- `ApiUrl`
- `QueryUrl`
- `AnswerUrl`
- `IndexBucketName`

## Seed and index build

```bash
aws s3 sync . "s3://$BUCKET/seed/repo" \
  --exclude "*" \
  --include "README.md" \
  --include "docs/**/*.md" --include "docs/**/*.mdx" \
  --include "**/*.md" --include "**/*.txt" \
  --exclude ".git/*" --exclude ".venv/*" --exclude "node_modules/*" --exclude "cdk.out/*"

aws lambda invoke \
  --function-name "$INGEST_FN" \
  --payload '{"truncate":true,"limit":5000}' \
  --cli-binary-format raw-in-base64-out \
  /tmp/ing.json >/dev/null && python3 -m json.tool /tmp/ing.json
```

## API examples

```bash
curl -sS -X POST "$API/query" \
  -H 'Content-Type: application/json' \
  -d '{"question":"Describe the architecture and data flow of this project.","k":6}'

curl -sS -X POST "$API/answer" \
  -H 'Content-Type: application/json' \
  -d '{"question":"Describe the architecture and data flow of this project.","k":6}'
```

Request validation (`QueryFn` and `AnswerFn`):

- `question` is required and must be non-empty string.
- `k` defaults to `3` if omitted and must be integer in `[1, 20]`.

## Lambda environment variables

Set via CDK stack environment for all Lambdas:

- `AWS_REGION`
- `INDEX_BUCKET`
- `INDEX_PREFIX` (default `rag-index`)
- `SEED_PREFIX` (default `seed`)
- `EMBED_MODEL_ID`
- `TEXT_MODEL_ID`
- `EMBED_SLEEP_SECS`

## Web UI

- Edit `web/config.js` and set `window.__RAG_CONFIG__.API_URL`.
- Alternatively pass `?api=https://.../answer` in URL.
- UI shows API/network errors inline.

## Testing

```bash
python3 -m pytest tests/test_utils.py
```

Covers:

- chunking output count/content with overlap
- cosine similarity for identical and orthogonal vectors

## Security notes

- Bucket is private and encrypted (S3 managed keys).
- Bedrock IAM policy is scoped to configured embedding/text model ARNs.
- API is HTTPS-only.
- Add an authorizer for production access control.

## Cleanup

```bash
cd infra
npx aws-cdk@latest destroy
```
