#!/usr/bin/env python3
"""Asks IAM's policy simulator whether the current user may perform every action
the Request Hub stacks need, and prints only the ones that are NOT allowed.

The simulator evaluates identity policies (where the playground's restrictions
live), not service control policies, so WAF and EventBridge Pipes are checked
separately by deploy.sh.
"""
import sys

import boto3

ACTIONS = {
    "events": ["CreateEventBus", "PutRule", "PutTargets", "TagResource", "CreateArchive", "PutEvents",
               "DeleteRule", "RemoveTargets", "DescribeRule", "DeleteArchive"],
    "lambda": ["CreateFunction", "TagResource", "CreateEventSourceMapping", "AddPermission", "GetFunction",
               "DeleteFunction", "UpdateFunctionCode", "UpdateFunctionConfiguration"],
    "states": ["CreateStateMachine", "TagResource", "DeleteStateMachine", "DescribeStateMachine", "UpdateStateMachine"],
    "apigateway": ["POST", "PUT", "PATCH", "GET", "DELETE"],
    "cognito-idp": ["CreateUserPool", "CreateUserPoolDomain", "CreateUserPoolClient", "CreateGroup", "TagResource",
                    "AdminCreateUser", "AdminSetUserPassword", "AdminAddUserToGroup", "AdminInitiateAuth",
                    "AdminRespondToAuthChallenge", "AdminDeleteUser", "DeleteUserPool", "DeleteUserPoolDomain"],
    "dynamodb": ["CreateTable", "TagResource", "UpdateContinuousBackups", "DescribeTable", "DeleteTable", "GetItem"],
    "s3": ["CreateBucket", "PutBucketPolicy", "PutEncryptionConfiguration", "PutBucketCORS",
           "PutLifecycleConfiguration", "PutBucketOwnershipControls", "PutBucketPublicAccessBlock",
           "PutBucketNotification", "PutBucketVersioning", "PutBucketTagging", "DeleteBucket"],
    "sqs": ["CreateQueue", "SetQueueAttributes", "TagQueue", "DeleteQueue"],
    "sns": ["CreateTopic", "SetTopicAttributes", "Subscribe", "TagResource", "DeleteTopic"],
    "firehose": ["CreateDeliveryStream", "TagDeliveryStream", "DeleteDeliveryStream"],
    "kms": ["CreateKey", "PutKeyPolicy", "EnableKeyRotation", "TagResource", "CreateGrant", "DescribeKey"],
    "secretsmanager": ["CreateSecret", "TagResource", "DeleteSecret", "GetSecretValue"],
    "ssm": ["PutParameter", "AddTagsToResource", "DeleteParameter"],
    "logs": ["CreateLogGroup", "PutRetentionPolicy", "TagResource", "TagLogGroup", "CreateLogDelivery",
             "PutResourcePolicy", "PutQueryDefinition", "AssociateKmsKey", "DeleteLogGroup"],
    "cloudwatch": ["PutMetricAlarm", "PutDashboard", "TagResource", "DeleteAlarms"],
    "iam": ["CreateRole", "CreatePolicy", "AttachRolePolicy", "TagRole", "TagPolicy", "PutRolePolicy", "DeleteRole"],
    "codebuild": ["CreateProject", "StartBuild"],
    "codepipeline": ["CreatePipeline", "TagResource", "StartPipelineExecution"],
    "config": ["PutConfigRule", "PutConfigurationRecorder", "PutDeliveryChannel", "StartConfigurationRecorder"],
    "cloudtrail": ["CreateTrail", "StartLogging", "AddTags"],
}


def main():
    arn = boto3.client("sts").get_caller_identity()["Arn"]
    iam = boto3.client("iam")
    names = [f"{svc}:{a}" for svc, acts in ACTIONS.items() for a in acts]
    denied = []
    for start in range(0, len(names), 40):
        resp = iam.simulate_principal_policy(PolicySourceArn=arn, ActionNames=names[start:start + 40])
        for r in resp["EvaluationResults"]:
            if r["EvalDecision"] != "allowed":
                denied.append((r["EvalActionName"], r["EvalDecision"]))
    print(f"Checked {len(names)} actions for {arn.split('/')[-1]}")
    if not denied:
        print("All allowed.")
    for name, decision in denied:
        print(f"  {decision:<14} {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
