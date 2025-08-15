Internal Stack Overflow (RAG)

Retrieval-augmented Q&A over your codebase using Amazon Bedrock and a simple S3-backed vector index (no OpenSearch required).

Architecture: Git/Docs → Ingest Lambda → S3 JSONL index → Query Lambda (cosine search) → Answer Lambda (retrieval + LLM).

Status: MVP works end-to-end (ingest, retrieve, synthesize).
Next up: Minimal web UI and polish.

✨ What it does

Indexes repo docs into a vector index with Titan Embeddings

Retrieves top-k matching chunks via cosine similarity (pure Python) over an S3-hosted JSONL

Synthesizes an answer with a Bedrock text model (Claude 3 Haiku or Titan Text Lite)

Returns { answer, contexts, scores } as JSON via API Gateway

🏗️ Architecture
flowchart LR
  subgraph AWS [AWS]
    D1[Docs / READMEs / Notes (s3://$BUCKET/seed/...)]
    L1[IngestFn (Lambda)]
    S3[(S3: rag-index/chunks.jsonl)]
    Q1[QueryFn (Lambda)]
    A1[AnswerFn (Lambda)]
    B1[Bedrock Embeddings]
    B2[Bedrock Text Model]

    D1 --> L1
    L1 -->|chunk + embed| B1
    B1 -->|vectors| L1
    L1 -->|write JSONL| S3
    Q1 -->|embed question| B1
    Q1 -->|stream + cosine top-k| S3
    Q1 -->|contexts| A1
    A1 -->|prompt with contexts| B2
    B2 -->|final answer| A1
  end

📂 Repo structure
.
├─ ingest/          # Lambda: build index (Titan embeddings -> S3 JSONL)
│  └─ handler.py
├─ query/           # Lambda: retrieval-only (cosine over S3 JSONL)
│  └─ handler.py
├─ answer/          # Lambda: retrieval + LLM synthesis (Bedrock text model)
│  └─ handler.py
├─ infra/           # AWS CDK (TypeScript)
│  ├─ bin/infra.ts
│  └─ lib/infra-stack.ts
└─ README.md

✅ Prerequisites

AWS account with CLI configured: aws sts get-caller-identity

Node.js 20+, npm, Docker (for CDK bundling)

Python 3.11 (for local Lambda work)

CDK bootstrapped once per account/region:

npx aws-cdk@latest bootstrap


Bedrock model access in us-east-1:

Embeddings: amazon.titan-embed-text-v1 (or amazon.titan-embed-text-v2:0)

Text model: anthropic.claude-3-haiku-20240307-v1:0 (or amazon.titan-text-lite-v1)

Tip: To skip the Anthropic form, set TEXT_MODEL_ID=amazon.titan-text-lite-v1 in the CDK stack.

If you hit a CDK CLI/library mismatch, use npx aws-cdk@latest ... to run the latest CLI.

🚀 Deploy
# from repo root
cd infra
npm i
npm run build
npx aws-cdk@latest deploy --require-approval never


The stack creates:

S3 bucket for vectors: s3://$BUCKET

Lambdas: IngestFn, QueryFn, AnswerFn

HTTP API (API Gateway) exposing POST /query and POST /answer

(Optional) Nightly re-index EventBridge rule (03:00 UTC)

Outputs include the API URL and the bucket name.

📥 Populate seed docs & build the index

Put your docs into s3://$BUCKET/seed/... and run the ingest:

# Example: sync a subset of your repo (adjust includes/excludes)
aws s3 sync . "s3://$BUCKET/seed/repo" \
  --exclude "*" \
  --include "README.md" \
  --include "docs/**/*.md" --include "docs/**/*.mdx" \
  --include "**/*.md" --include "**/*.mdx" --include "**/*.txt" \
  --exclude ".git/*" --exclude ".venv/*" --exclude "node_modules/*" --exclude "cdk.out/*"

# Build a fresh index (truncate deletes rag-index/* before writing)
aws lambda invoke \
  --function-name "$INGEST_FN" \
  --payload '{"truncate":true,"limit":5000}' \
  --cli-binary-format raw-in-base64-out \
  /tmp/ing.json >/dev/null && python3 -m json.tool /tmp/ing.json

# Inspect the first few rows
aws s3 cp "s3://$BUCKET/rag-index/chunks.jsonl" - | head -n 3

🔬 Smoke tests
1) Retrieval only
curl -sS -X POST "$API/query" \
  -H 'Content-Type: application/json' \
  -d '{"question":"Describe the architecture, components and data flow of this project.","k":6}' \
  | python3 -m json.tool

2) Full answer
curl -sS -X POST "$API/answer" \
  -H 'Content-Type: application/json' \
  -d '{"question":"Describe the architecture, components and data flow of this project.","k":6}' \
  | python3 -m json.tool

⚙️ Configuration

Most settings are provided to the Lambdas as environment variables via CDK:

Var	Default	Notes
INDEX_BUCKET	(stack output)	S3 bucket for the index
INDEX_PREFIX	rag-index	JSONL written to ${PREFIX}/chunks.jsonl
SEED_PREFIX	seed	Where seed docs live in the bucket
EMBED_MODEL_ID	amazon.titan-embed-text-v1	Or amazon.titan-embed-text-v2:0
TEXT_MODEL_ID	anthropic.claude-3-haiku-20240307-v1:0	Or amazon.titan-text-lite-v1
AWS_REGION	us-east-1 (from Lambda env)	Region for Bedrock + S3
🧼 Keep the index clean (avoid old junk + duplicates)

Excludes common build/dep folders during ingest (.git/, node_modules/, cdk.out/, etc.).

De-dupes identical chunk texts across files in each run.

Truncate mode: pass {"truncate": true} to IngestFn to delete old rag-index/* before writing.

Chunking: default ~800 chars with ~120 overlap (tune in ingest/handler.py).

🧪 Ingesting real docs

Point aws s3 sync at your repos, docs, ADRs, run ingest, then query/answer.
If answers mention old components (e.g., OpenSearch), update your docs—answers mirror your corpus.

🔐 Security notes (dev vs prod)

Bucket is private; Lambdas get least-priv S3 read/write as needed.

Bedrock IAM policy currently allows bedrock:InvokeModel on * (dev). Narrow to specific model ARNs for prod.

Enable CORS on the API if you’ll call it from a browser app (limit to your origin domains).

Consider VPC endpoints / IAM conditions if stricter isolation is required.

💸 Costs

S3 storage (single JSONL file), Lambda (ingest/query/answer), Bedrock inference.

Keep datasets modest; gzip the JSONL if it grows; turn off nightly ingest if idle.

🧹 Cleanup
cd infra
npx aws-cdk@latest destroy

🛣️ Roadmap

Minimal web UI (one-page chat) that shows contexts + scores

Observability: latency + token metrics

Tighten IAM & networking

Tune nightly re-ingest (currently 03:00 UTC) and add incremental updates

🧾 License

MIT (or your preferred license)