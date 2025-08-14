Internal Stack Overflow (RAG)

Retrieval-augmented Q&A over my codebase using Amazon Bedrock + OpenSearch Serverless (Vector Search).
Architecture: Git/Docs → Ingest Lambda → AOSS vectors → Answer Lambda (retrieval + LLM).

Status: MVP working end-to-end (ingest, retrieve, synthesize).
Next up: API Gateway + simple web UI.

✨ What it does

Indexes repo docs into a vector index using Titan Embeddings

Retrieves top-k matches from OpenSearch Serverless (AOSS)

Synthesizes an answer with a Bedrock text model (Claude 3 Haiku or Titan Text)

Returns answer + contexts (+scores) as JSON

🏗️ Architecture
flowchart LR
    subgraph AWS [AWS]
        D1[Docs / READMEs / Notes]
        L1[IngestFn (Lambda)]
        A1[(AOSS Vector Collection)]
        L2[AnswerFn (Lambda)]
        B1[Bedrock Embeddings]
        B2[Bedrock Text Model]

        D1 --> L1
        L1 -->|embed| B1
        B1 -->|vectors| A1
        L2 -->|kNN search| A1
        L2 -->|prompt with contexts| B2
        B2 -->|final answer| L2
    end

📂 Repo structure
.
├─ ingest/          # Lambda: build index (Titan embeddings -> AOSS)
│  └─ handler.py
├─ query/           # (dev util) Lambda: raw kNN search
│  └─ handler.py
├─ answer/          # Lambda: retrieval + LLM synthesis
│  └─ handler.py
├─ infra/           # AWS CDK (TypeScript)
│  ├─ bin/infra.ts
│  └─ lib/infra-stack.ts
└─ README.md

✅ Prerequisites

AWS account with CLI configured (aws sts get-caller-identity)

Node.js 20+, npm, Docker (CDK bundles Python Lambdas in a container)

Python 3.11 (for local work on Lambdas)

CDK bootstrapped in your account/region (once):
npx aws-cdk@latest bootstrap

Bedrock model access (Region: us-east-1):

Embeddings: Titan Embeddings G1 – Text (amazon.titan-embed-text-v1) or v2 (amazon.titan-embed-text-v2:0)

Text model: Claude 3 Haiku (anthropic.claude-3-haiku-20240307-v1:0) or Titan Text Lite (amazon.titan-text-lite-v1)

tip: If you’d rather skip the Anthropic form, set TEXT_MODEL_ID=amazon.titan-text-lite-v1 in the CDK stack.

🚀 Deploy
# from repo root:
cd infra
npm i
npm run build
npx aws-cdk@latest deploy --require-approval never


The stack creates:

AOSS vector collection (rag-vectors) and index (docs)

IngestFn, QueryFn (dev), AnswerFn

Data/Network/Encryption policies for AOSS

🔬 Smoke tests
1) Ingest demo docs

Run the IngestFn Lambda once (Console → Lambda → IngestFn → Test → empty {} event).
Expected body:

{ "bulk_errors": false, "items": 3 }

2) Ask a question

Run AnswerFn with:

{ "question": "What does this project do?", "k": 3 }


Example response:

{
  "question": "What does this project do?",
  "contexts": ["This project indexes text into OpenSearch Serverless using Titan embeddings."],
  "answer": "This project indexes text into OpenSearch Serverless using Titan embeddings..."
}

(CLI alternative)
aws lambda list-functions --query 'Functions[?contains(FunctionName, `InfraStack-`)].FunctionName'
aws lambda invoke --function-name <AnswerFnName> \
  --payload '{"question":"What does this project do?","k":3}' out.json
cat out.json

⚙️ Configuration

Most settings are set via CDK env vars on the Lambdas:

OS_ENDPOINT – AOSS collection endpoint

OS_INDEX=docs

OS_COLLECTION=rag-vectors

EMBED_MODEL_ID=amazon.titan-embed-text-v1 (or amazon.titan-embed-text-v2:0)

TEXT_MODEL_ID=anthropic.claude-3-haiku-20240307-v1:0 (or amazon.titan-text-lite-v1)

🧪 Ingesting real docs

Right now the ingest uses a tiny placeholder corpus.
Next step: read local files or a Git repo (e.g., docs/, README.md, ADRs), chunk them, embed, and index.

🔐 Security notes (dev vs prod)

Network policy currently allows public HTTPS to the collection (dev convenience).
For prod: restrict by VPC or specific principals.

AOSS permissions are broad (aoss:*) for iteration.
For prod: reduce to the exact API calls needed.

Remove the account root principal from the AOSS data policy once verified.

💸 Costs

AOSS Serverless collection, Lambda executions, Bedrock inference.
Keep datasets small and shut down when idle.

Cleanup

cd infra
npx aws-cdk@latest destroy

🛣️ Roadmap

 API Gateway → POST /answer → AnswerFn (CORS on)

 Minimal web UI (one-page chat) that shows citations + scores

 Real corpus ingestion (repos/docs) with chunking

 Observability: latency + token metrics

 Tighten IAM & network policies

 Add embeddings re-ingest schedule (already daily @ 03:00 UTC — tune as needed)

🧾 License

MIT (or your preferred license)