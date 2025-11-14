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

🔐 Security & Production Hardening

⚠️ **Current state: MVP/Development** - Several hardening steps required before production use.

### Required for Production (P0)

**Authentication & Authorization**
- [ ] Add API Gateway authorizer (AWS IAM, Cognito, or Lambda authorizer)
- [ ] Implement request signing for Lambda-to-Lambda calls if needed
- [ ] Consider AWS WAF for additional protection

**Rate Limiting & Abuse Prevention**
- [ ] Enable API Gateway throttling (per-client quotas)
- [ ] Add input validation bounds:
  - Max `k` parameter (e.g., 1-20 results)
  - Max question length (e.g., 500 chars)
  - Max files per ingest run
- [ ] Implement request size limits

**CORS Hardening**
- [ ] Replace `allowOrigins: ["*"]` with specific domain(s) in `infra/lib/infra-stack.ts:93`
- [ ] Restrict `allowHeaders` and `allowMethods` to minimum required

**IAM Tightening**
- [ ] Narrow Bedrock policy from `resources: ["*"]` to specific model ARNs:
  ```typescript
  resources: [
    `arn:aws:bedrock:${this.region}::foundation-model/amazon.titan-embed-text-v1`,
    `arn:aws:bedrock:${this.region}::foundation-model/anthropic.claude-3-haiku-20240307-v1:0`,
  ]
  ```
- [ ] Add S3 bucket policies with explicit deny rules for unauthorized access
- [ ] Enable S3 bucket versioning and MFA delete for index protection

**Error Handling**
- [ ] Sanitize error messages - never expose stack traces or internal paths to clients
- [ ] Classify errors (400 for client errors, 500 for server errors, 429 for throttling)
- [ ] Log detailed errors internally but return generic messages externally

### Recommended for Production (P1)

**Observability**
- [ ] Implement structured logging (use AWS Lambda Powertools)
- [ ] Add CloudWatch metrics:
  - Request latency per endpoint
  - Bedrock API call duration/throttling
  - Token usage and costs
  - Index size and chunk count
- [ ] Set up CloudWatch alarms for errors, throttling, high latency
- [ ] Add request correlation IDs for tracing query → answer flows
- [ ] Create CloudWatch dashboard for operational visibility

**Configuration Management**
- [ ] Move environment variables to AWS Systems Manager Parameter Store
- [ ] Separate dev/staging/prod configurations
- [ ] Validate required config at Lambda cold start
- [ ] Use AWS Secrets Manager for any API keys (if integrating external services)

**Networking**
- [ ] Deploy Lambdas in VPC if accessing private resources
- [ ] Use VPC endpoints for S3 and Bedrock to avoid internet egress
- [ ] Enable VPC Flow Logs for network monitoring

**Data Protection**
- [ ] Enable S3 bucket encryption at rest (already using S3_MANAGED, consider KMS)
- [ ] Implement S3 access logging
- [ ] Add S3 lifecycle policies for old index versions
- [ ] Consider S3 Object Lock for compliance requirements

**Resilience**
- [ ] Implement circuit breakers for Bedrock API calls
- [ ] Add retry logic with exponential backoff (partially done in ingest)
- [ ] Set up Lambda reserved concurrency to prevent runaway costs
- [ ] Create dead letter queues for failed async invocations

### Nice-to-Have (P2)

**Code Quality**
- [ ] Add unit and integration tests (current test suite is empty)
- [ ] Extract duplicate code (`_embed()`, `_cosine()`, `_parse_event()`) to shared module
- [ ] Add comprehensive docstrings with parameter types and exceptions
- [ ] Clean up unused dependencies in requirements.lock

**Documentation**
- [ ] Create OpenAPI/Swagger spec for API endpoints
- [ ] Write operational runbook (monitoring, troubleshooting, rollback procedures)
- [ ] Document error codes and meanings
- [ ] Add performance tuning guide
- [ ] Create cost estimation calculator

**Performance**
- [ ] Benchmark query latency at various index sizes
- [ ] Consider caching for frequent queries (ElastiCache or API Gateway caching)
- [ ] Optimize S3 streaming for large indexes (use pagination tokens)
- [ ] Compress JSONL with gzip if index exceeds 100MB
- [ ] Consider Lambda SnapStart for faster cold starts

**Advanced Security**
- [ ] Enable AWS Config rules for compliance monitoring
- [ ] Implement AWS CloudTrail for audit logging
- [ ] Use AWS GuardDuty for threat detection
- [ ] Consider field-level encryption for sensitive documents
- [ ] Add data classification tags to S3 objects

### Current Security Posture

✅ **Already implemented:**
- Private S3 bucket (BlockPublicAccess enabled)
- Least-privilege IAM for Lambda S3 access
- Encryption at rest (S3 managed keys)
- HTTPS-only API endpoints

⚠️ **Dev-only settings (change for prod):**
- CORS: `allowOrigins: ["*"]` → restrict to your domains
- Bedrock IAM: `resources: ["*"]` → specific model ARNs
- No authentication on API endpoints
- No rate limiting
- RemovalPolicy: DESTROY (good for dev, risky for prod data)
- Generic error messages expose internal details

💸 Costs

S3 storage (single JSONL file), Lambda (ingest/query/answer), Bedrock inference.

Keep datasets modest; gzip the JSONL if it grows; turn off nightly ingest if idle.

🧹 Cleanup
cd infra
npx aws-cdk@latest destroy

🔧 Additional Polish Needed

Beyond security hardening, several areas could benefit from improvement:

**Error Handling**
- Add proper error classification (distinguish client vs server errors)
- Implement structured error responses with error codes
- Add retry logic with exponential backoff for all Bedrock calls
- Validate input parameters (k bounds, question length, file limits)

**Observability**
- Replace `print()` with structured logging (AWS Lambda Powertools recommended)
- Add CloudWatch metrics for latency, costs, and performance
- Implement request tracing with correlation IDs
- Create operational dashboards and alarms

**Code Quality**
- Add unit and integration tests (test suite is currently empty)
- Extract duplicate code across handlers to shared utilities
- Add type hints and docstrings consistently
- Remove unused dependencies from requirements.lock (langchain, opensearch-py, etc.)

**Configuration**
- Centralize magic numbers (chunk size, overlap, timeouts) to single config
- Move env vars to Parameter Store for easier updates
- Add config validation at Lambda startup
- Support multiple environments (dev/staging/prod)

**Documentation**
- Create OpenAPI spec for `/query` and `/answer` endpoints
- Write operational runbook (monitoring, troubleshooting, rollback)
- Document error codes and recovery procedures
- Add architecture decision records (ADRs)

**Performance**
- Benchmark query latency at scale (1K, 10K, 100K chunks)
- Add caching for frequently asked questions
- Optimize S3 streaming for large indexes
- Consider Lambda SnapStart for cold start reduction

🛣️ Roadmap

Minimal web UI enhancements (show contexts + scores, multi-turn chat)

Incremental index updates (avoid full re-index for new docs)

Multi-region deployment pattern

Support for additional embedding models (Cohere, Voyage AI)

🧾 License

MIT (or your preferred license)