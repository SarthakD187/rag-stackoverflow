// infra/lib/infra-stack.ts
import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as path from "path";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambdaPython from "@aws-cdk/aws-lambda-python-alpha";
import * as apigwv2 from "@aws-cdk/aws-apigatewayv2-alpha";
import * as integrations from "@aws-cdk/aws-apigatewayv2-integrations-alpha";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";

export class InfraStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // S3 bucket to store the vector index
    const indexBucket = new s3.Bucket(this, "IndexBucket", {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      removalPolicy: cdk.RemovalPolicy.DESTROY, // dev
      autoDeleteObjects: true, // dev
    });

    // Single source of truth for env vars used by all Lambdas.
    // NOTE: INDEX_KEY pins everyone to the SAME file.
    const commonEnv = {
      INDEX_BUCKET: indexBucket.bucketName,
      INDEX_PREFIX: "rag-index",
      INDEX_KEY: "rag-index/chunks.jsonl",
      SEED_PREFIX: "seed",
      EMBED_MODEL_ID: "amazon.titan-embed-text-v1",
      TEXT_MODEL_ID: "anthropic.claude-3-haiku-20240307-v1:0",
      // Small sleep lets Bedrock breathe under bursty loads; adjust if needed.
      EMBED_SLEEP_SECS: "0.05",
    };

    // Force pip to use wheels only (avoids compiling numpy in bundling image)
    const wheelOnly = { environment: { PIP_ONLY_BINARY: ":all:" } };

    // Ingest: reads raw files from s3://bucket/seed/, writes index to rag-index/
    const ingestFn = new lambdaPython.PythonFunction(this, "IngestFn", {
      runtime: cdk.aws_lambda.Runtime.PYTHON_3_11,
      entry: path.join(__dirname, "../../ingest"),
      index: "handler.py",
      handler: "lambda_handler",
      memorySize: 1024,
      timeout: cdk.Duration.minutes(5),
      environment: commonEnv,
      bundling: wheelOnly,
    });

    // Query: reads vectors/meta from S3 (same INDEX_KEY as AnswerFn)
    const queryFn = new lambdaPython.PythonFunction(this, "QueryFn", {
      runtime: cdk.aws_lambda.Runtime.PYTHON_3_11,
      entry: path.join(__dirname, "../../query"),
      index: "handler.py",
      handler: "lambda_handler",
      memorySize: 512,
      timeout: cdk.Duration.seconds(20),
      environment: commonEnv,
      bundling: wheelOnly,
    });

    // Answer: retrieval + LLM synthesis (same INDEX_KEY as QueryFn)
    const answerFn = new lambdaPython.PythonFunction(this, "AnswerFn", {
      runtime: cdk.aws_lambda.Runtime.PYTHON_3_11,
      entry: path.join(__dirname, "../../answer"),
      index: "handler.py",
      handler: "lambda_handler",
      memorySize: 512,
      timeout: cdk.Duration.seconds(25), // within API GW 29s limit
      environment: commonEnv,
      bundling: wheelOnly,
    });

    // IAM: S3 access
    indexBucket.grantReadWrite(ingestFn);  // put vectors/meta + read seed
    indexBucket.grantRead(queryFn);        // read vectors/meta
    indexBucket.grantRead(answerFn);       // read vectors/meta

    // IAM: Bedrock (include streaming for AnswerFn/QueryFn if used)
    const bedrockPerms = new iam.PolicyStatement({
      actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
      resources: ["*"], // (optional) narrow to model ARNs later
    });
    [ingestFn, queryFn, answerFn].forEach(fn => fn.addToRolePolicy(bedrockPerms));

    // HTTP API with CORS enabled (dev-friendly; tighten origins for prod)
    const api = new apigwv2.HttpApi(this, "Api", {
      apiName: "rag-api",
      corsPreflight: {
        allowOrigins: ["*"], // e.g., replace with ["http://localhost:5173", "https://yourdomain.com"] for prod
        allowMethods: [apigwv2.CorsHttpMethod.ANY],
        allowHeaders: ["content-type"],
      },
    });

    api.addRoutes({
      path: "/query",
      methods: [apigwv2.HttpMethod.POST],
      integration: new integrations.HttpLambdaIntegration("QueryInt", queryFn),
    });
    api.addRoutes({
      path: "/answer",
      methods: [apigwv2.HttpMethod.POST],
      integration: new integrations.HttpLambdaIntegration("AnswerInt", answerFn),
    });

    // Nightly clean re-index (03:00 UTC) with truncate=true to avoid stale junk
    new events.Rule(this, "NightlyIngest", {
      schedule: events.Schedule.cron({ minute: "0", hour: "3" }),
      targets: [
        new targets.LambdaFunction(ingestFn, {
          event: events.RuleTargetInput.fromObject({
            truncate: true,
            limit: 2000, // adjust to your corpus size
          }),
        }),
      ],
    });

    // Outputs
    new cdk.CfnOutput(this, "ApiUrl", { value: api.apiEndpoint });
    new cdk.CfnOutput(this, "QueryUrl", { value: `${api.apiEndpoint}/query` });
    new cdk.CfnOutput(this, "AnswerUrl", { value: `${api.apiEndpoint}/answer` });
    new cdk.CfnOutput(this, "IndexBucketName", { value: indexBucket.bucketName });
  }
}
