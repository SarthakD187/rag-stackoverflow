import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as path from "path";

import * as lambdaPython from "@aws-cdk/aws-lambda-python-alpha";
import * as oss from "aws-cdk-lib/aws-opensearchserverless";
import * as iam from "aws-cdk-lib/aws-iam";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";

export class InfraStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const collectionName = "rag-vectors";

    const enc = new oss.CfnSecurityPolicy(this, "EncryptionPolicy", {
      name: "rag-encryption",
      type: "encryption",
      policy: JSON.stringify({
        Rules: [{ ResourceType: "collection", Resource: [`collection/${collectionName}`] }],
        AWSOwnedKey: true,
      }),
    });

    const net = new oss.CfnSecurityPolicy(this, "NetworkPolicy", {
      name: "rag-network",
      type: "network",
      policy: JSON.stringify([
        {
          Description: "Public access for dev",
          Rules: [{ ResourceType: "collection", Resource: [`collection/${collectionName}`] }],
          AllowFromPublic: true,
        },
      ]),
    });

    const collection = new oss.CfnCollection(this, "VectorCollection", {
      name: collectionName,
      type: "VECTORSEARCH",
    });
    collection.addDependency(enc);
    collection.addDependency(net);

    const ingestFn = new lambdaPython.PythonFunction(this, "IngestFn", {
      runtime: cdk.aws_lambda.Runtime.PYTHON_3_11,
      entry: path.join(__dirname, "../../ingest"),
      index: "handler.py",
      handler: "lambda_handler",
      memorySize: 512,
      timeout: cdk.Duration.seconds(60),
      environment: {
        OS_COLLECTION: collectionName,
        OS_INDEX: "docs",
        OS_ENDPOINT: collection.attrCollectionEndpoint,
        EMBED_MODEL_ID: "amazon.titan-embed-text-v1",
      },
    });

    const queryFn = new lambdaPython.PythonFunction(this, "QueryFn", {
      runtime: cdk.aws_lambda.Runtime.PYTHON_3_11,
      entry: path.join(__dirname, "../../query"),
      index: "handler.py",
      handler: "lambda_handler",
      memorySize: 512,
      timeout: cdk.Duration.seconds(20),
      environment: {
        OS_COLLECTION: collectionName,
        OS_INDEX: "docs",
        OS_ENDPOINT: collection.attrCollectionEndpoint,
        EMBED_MODEL_ID: "amazon.titan-embed-text-v1",
      },
    });

    const answerFn = new lambdaPython.PythonFunction(this, "AnswerFn", {
      runtime: cdk.aws_lambda.Runtime.PYTHON_3_11,
      entry: path.join(__dirname, "../../answer"),
      index: "handler.py",
      handler: "lambda_handler",
      memorySize: 512,
      timeout: cdk.Duration.seconds(20),
      environment: {
        OS_COLLECTION: collectionName,
        OS_INDEX: "docs",
        OS_ENDPOINT: collection.attrCollectionEndpoint,
        EMBED_MODEL_ID: "amazon.titan-embed-text-v1",
        TEXT_MODEL_ID: "anthropic.claude-3-haiku-20240307-v1:0",
      },
    });

    /** Data-access policy (dev): full access for Lambdas + account root */
    const account   = cdk.Stack.of(this).account;
    const partition = cdk.Stack.of(this).partition;

    const iamPrincipals = [
      ingestFn.role!.roleArn,
      queryFn.role!.roleArn,
      answerFn.role!.roleArn,
    ];
    const stsPrincipals = [
      `arn:${partition}:sts::${account}:assumed-role/${ingestFn.role!.roleName}/*`,
      `arn:${partition}:sts::${account}:assumed-role/${queryFn.role!.roleName}/*`,
      `arn:${partition}:sts::${account}:assumed-role/${answerFn.role!.roleName}/*`,
    ];

    const dataPolicy = new oss.CfnAccessPolicy(this, "VectorPolicy", {
      name: "rag-access",
      type: "data",
      policy: JSON.stringify([
        {
          Rules: [
            { ResourceType: "index", Resource: [`index/${collectionName}/*`], Permission: ["aoss:*"] },
            { ResourceType: "collection", Resource: [`collection/${collectionName}`], Permission: ["aoss:*"] },
          ],
          Principal: [...iamPrincipals, ...stsPrincipals],
          Description: "Dev full access from Lambdas to collection & indexes",
        },
      ]),
    });
    dataPolicy.node.addDependency(ingestFn);
    dataPolicy.node.addDependency(queryFn);
    dataPolicy.node.addDependency(answerFn);
    dataPolicy.node.addDependency(collection);

    new events.Rule(this, "DailyIngestSchedule", {
      schedule: events.Schedule.cron({ minute: "0", hour: "3" }),
      targets: [new targets.LambdaFunction(ingestFn)],
    });

    // 🔐 Permissions
    const bedrockPerms = new iam.PolicyStatement({
      actions: ["bedrock:InvokeModel"],
      resources: ["*"], // tighten later
    });
    [ingestFn, queryFn, answerFn].forEach(fn => fn.addToRolePolicy(bedrockPerms));

    // ✅ Add API-level permission for AOSS
    const osPerms = new iam.PolicyStatement({
      actions: [
        "aoss:APIAccessAll",   // <— this is the key addition
        "aoss:CreateIndex",
        "aoss:WriteDocument",
        "aoss:ReadDocument",
        "aoss:DescribeIndex",
      ],
      resources: ["*"], // tighten later
    });
    [ingestFn, queryFn, answerFn].forEach(fn => fn.addToRolePolicy(osPerms));

    new cdk.CfnOutput(this, "OpenSearchCollectionEndpoint", {
      value: collection.attrCollectionEndpoint,
    });
  }
}
