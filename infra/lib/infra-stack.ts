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

    const allowedOrigin = this.node.tryGetContext("frontendOrigin") ?? "*";
    const enableNightlyIngest = this.node.tryGetContext("enableNightlyIngest") !== "false";
    const embedModelId = this.node.tryGetContext("embedModelId") ?? "amazon.titan-embed-text-v1";
    const textModelId =
      this.node.tryGetContext("textModelId") ?? "anthropic.claude-3-haiku-20240307-v1:0";

    const indexBucket = new s3.Bucket(this, "IndexBucket", {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    const commonEnv = {
      AWS_REGION: cdk.Stack.of(this).region,
      INDEX_BUCKET: indexBucket.bucketName,
      INDEX_PREFIX: "rag-index",
      SEED_PREFIX: "seed",
      EMBED_MODEL_ID: embedModelId,
      TEXT_MODEL_ID: textModelId,
      EMBED_SLEEP_SECS: "0.05",
    };

    const bundlingWithShared = {
      environment: { PIP_ONLY_BINARY: ":all:" },
      commandHooks: {
        beforeInstall(): string[] {
          return [];
        },
        beforeBundling(): string[] {
          return [];
        },
        afterBundling(inputDir: string, outputDir: string): string[] {
          return [
            `mkdir -p ${outputDir}/shared`,
            `cp -R ${path.posix.join(inputDir, "..", "shared", ".")} ${outputDir}/shared`,
          ];
        },
      },
    };

    const ingestFn = new lambdaPython.PythonFunction(this, "IngestFn", {
      runtime: cdk.aws_lambda.Runtime.PYTHON_3_11,
      entry: path.join(__dirname, "../../ingest"),
      index: "handler.py",
      handler: "lambda_handler",
      memorySize: 1024,
      timeout: cdk.Duration.minutes(5),
      environment: commonEnv,
      bundling: bundlingWithShared,
    });

    const queryFn = new lambdaPython.PythonFunction(this, "QueryFn", {
      runtime: cdk.aws_lambda.Runtime.PYTHON_3_11,
      entry: path.join(__dirname, "../../query"),
      index: "handler.py",
      handler: "lambda_handler",
      memorySize: 512,
      timeout: cdk.Duration.seconds(20),
      environment: commonEnv,
      bundling: bundlingWithShared,
    });

    const answerFn = new lambdaPython.PythonFunction(this, "AnswerFn", {
      runtime: cdk.aws_lambda.Runtime.PYTHON_3_11,
      entry: path.join(__dirname, "../../answer"),
      index: "handler.py",
      handler: "lambda_handler",
      memorySize: 512,
      timeout: cdk.Duration.seconds(25),
      environment: commonEnv,
      bundling: bundlingWithShared,
    });

    indexBucket.grantReadWrite(ingestFn);
    indexBucket.grantRead(queryFn);
    indexBucket.grantRead(answerFn);

    const embedModelArn = cdk.Stack.of(this).formatArn({
      service: "bedrock",
      region: cdk.Stack.of(this).region,
      account: "",
      resource: "foundation-model",
      resourceName: embedModelId,
      arnFormat: cdk.ArnFormat.SLASH_RESOURCE_NAME,
    });
    const textModelArn = cdk.Stack.of(this).formatArn({
      service: "bedrock",
      region: cdk.Stack.of(this).region,
      account: "",
      resource: "foundation-model",
      resourceName: textModelId,
      arnFormat: cdk.ArnFormat.SLASH_RESOURCE_NAME,
    });

    const bedrockPerms = new iam.PolicyStatement({
      actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
      resources: [embedModelArn, textModelArn],
    });
    [ingestFn, queryFn, answerFn].forEach((fn) => fn.addToRolePolicy(bedrockPerms));

    const api = new apigwv2.HttpApi(this, "Api", {
      apiName: "rag-api",
      corsPreflight: {
        allowOrigins: [allowedOrigin],
        allowMethods: [apigwv2.CorsHttpMethod.POST, apigwv2.CorsHttpMethod.OPTIONS],
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

    if (enableNightlyIngest) {
      new events.Rule(this, "NightlyIngest", {
        schedule: events.Schedule.cron({ minute: "0", hour: "3" }),
        targets: [
          new targets.LambdaFunction(ingestFn, {
            event: events.RuleTargetInput.fromObject({
              truncate: true,
              limit: 2000,
            }),
          }),
        ],
      });
    }

    new cdk.CfnOutput(this, "ApiUrl", { value: api.apiEndpoint });
    new cdk.CfnOutput(this, "QueryUrl", { value: `${api.apiEndpoint}/query` });
    new cdk.CfnOutput(this, "AnswerUrl", { value: `${api.apiEndpoint}/answer` });
    new cdk.CfnOutput(this, "IndexBucketName", { value: indexBucket.bucketName });
  }
}
