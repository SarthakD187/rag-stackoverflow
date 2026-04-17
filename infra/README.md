# CDK Infra

CDK app that deploys:

- `IndexBucket` S3 bucket
- `IngestFn`, `QueryFn`, `AnswerFn` Lambda functions
- HTTP API with `POST /query` and `POST /answer`
- Optional nightly EventBridge rule for re-indexing (context `enableNightlyIngest`)

Useful commands:

- `npm run build`
- `npm run test`
- `npx cdk synth`
- `npx cdk deploy`
- `npx cdk diff`
