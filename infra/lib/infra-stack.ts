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

    // 🔐 Encryption policy (must exist before collection)
    const enc = new oss.CfnSecurityPolicy(this, "EncryptionPolicy", {
      name: "rag-encryption",
      type: "encryption",
      policy: JSON.stringify({
        Rules: [
          {
            ResourceType: "collection",
            Resource: [`collection/${collectionName}`],
          },
        ],
        AWSOwnedKey: true,
      }),
    });

    // 🌐 Network policy (public HTTPS for dev) — wildcard collection + dashboard
    const net = new oss.CfnSecurityPolicy(this, "NetworkPolicy", {
      name: "rag-network",
      type: "network",
      policy: JSON.stringify([
        {
          Description: "Public access for dev (collection + dashboards)",
          Rules: [
            { ResourceType: "collection", Resource: ["collection/*"] },
            { ResourceType: "dashboard", Resource: ["dashboard/*"] },
          ],
          AllowFromPublic: true,
        },
      ]),
    });

    // 🧺 Collection
    const collection = new oss.CfnCollection(this, "VectorCollection", {
      name: collectionName,
      type: "VECTORSEARCH",
    });
    collection.addDependency(enc);
    collection.addDependency(net);

    // 🧰 Ingest Lambda
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
        OS_ENDPOINT: collection.attrCollectionEndpoint,  // AOSS data endpoint
        EMBED_MODEL_ID: "amazon.titan-embed-text-v1",
        // AWS_REGION is provided by Lambda automatically
      },
    });

    // 🔎 Query Lambda
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

    /** 👮 Data-access policy — DEV: full access (wildcards + IAM + STS + account root) */
    const account = cdk.Stack.of(this).account;
    const partition = cdk.Stack.of(this).partition;

    const iamPrincipals = [ingestFn.role!.roleArn, queryFn.role!.roleArn];
    const stsPrincipals = [
      `arn:${partition}:sts::${account}:assumed-role/${ingestFn.role!.roleName}/*`,
      `arn:${partition}:sts::${account}:assumed-role/${queryFn.role!.roleName}/*`,
    ];
    const accountRoot = `arn:${partition}:iam::${account}:root`;

    const dataPolicy = new oss.CfnAccessPolicy(this, "VectorPolicy", {
      name: "rag-access",
      type: "data",
      policy: JSON.stringify([
        {
          Rules: [
            { ResourceType: "index", Resource: ["index/*/*"], Permission: ["aoss:*"] },
            { ResourceType: "collection", Resource: ["collection/*"], Permission: ["aoss:*"] },
          ],
          Principal: [...iamPrincipals, ...stsPrincipals, accountRoot],
          Description: "Dev: full data access from Lambdas/account to AOSS",
        },
      ]),
    });
    dataPolicy.node.addDependency(ingestFn);
    dataPolicy.node.addDependency(queryFn);
    dataPolicy.node.addDependency(collection);

    // ⏰ Schedule daily ingest (03:00 UTC)
    new events.Rule(this, "DailyIngestSchedule", {
      schedule: events.Schedule.cron({ minute: "0", hour: "3" }),
      targets: [new targets.LambdaFunction(ingestFn)],
    });

    // 🔐 Bedrock + (optional) AOSS IAM actions — not strictly required for data-plane, but fine
    const bedrockPerms = new iam.PolicyStatement({
      actions: ["bedrock:InvokeModel"],
      resources: ["*"],
    });
    ingestFn.addToRolePolicy(bedrockPerms);
    queryFn.addToRolePolicy(bedrockPerms);

    const osPerms = new iam.PolicyStatement({
      actions: ["aoss:CreateIndex", "aoss:WriteDocument", "aoss:ReadDocument", "aoss:DescribeIndex"],
      resources: ["*"],
    });
    ingestFn.addToRolePolicy(osPerms);
    queryFn.addToRolePolicy(osPerms);

    new cdk.CfnOutput(this, "OpenSearchCollectionEndpoint", {
      value: collection.attrCollectionEndpoint,
    });
  }
}
