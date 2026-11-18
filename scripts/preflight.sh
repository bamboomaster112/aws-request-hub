#!/usr/bin/env bash
# Read-only permission probe for the services Request Hub uses. Run it first in a
# fresh playground session: it creates nothing and takes about 20 seconds.
set -uo pipefail
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION"

probe() { # label, command...
  local label="$1"; shift
  if out=$("$@" 2>&1 >/dev/null); then
    printf '  \033[32mOK\033[0m      %s\n' "$label"
  else
    reason=$(echo "$out" | grep -oE '(AccessDenied[A-Za-z]*|UnauthorizedOperation|explicit deny|not authorized)' | head -1)
    printf '  \033[31mDENIED\033[0m  %s  %s\n' "$label" "${reason:-$(echo "$out" | tail -1 | cut -c1-110)}"
  fi
}

echo "Account: $(aws sts get-caller-identity --query Account --output text)   Region: $REGION"
echo "Tools:   $(aws --version 2>&1 | cut -d' ' -f1)  $(sam --version 2>/dev/null || echo 'sam: not installed')  $(python3 --version)"
python3 -c 'import boto3' 2>/dev/null && echo "         boto3 available" || echo "         boto3 missing: pip3 install --user boto3"
echo
probe "CloudFormation"            aws cloudformation list-stacks --max-items 1
probe "IAM (list roles)"          aws iam list-roles --max-items 1
probe "KMS"                       aws kms list-keys --limit 1
probe "DynamoDB"                  aws dynamodb list-tables --max-items 1
probe "S3"                        aws s3api list-buckets
probe "SSM Parameter Store"       aws ssm describe-parameters --max-results 1
probe "Secrets Manager"           aws secretsmanager list-secrets --max-results 1
probe "Cognito user pools"        aws cognito-idp list-user-pools --max-results 1
probe "API Gateway (REST)"        aws apigateway get-rest-apis --limit 1
probe "API Gateway account"       aws apigateway get-account
probe "WAF (regional)"            aws wafv2 list-web-acls --scope REGIONAL --limit 1
probe "Lambda"                    aws lambda list-functions --max-items 1
probe "Step Functions"            aws stepfunctions list-state-machines --max-results 1
probe "EventBridge"               aws events list-event-buses --limit 1
probe "EventBridge Pipes"         aws pipes list-pipes --limit 1
probe "SQS"                       aws sqs list-queues --max-results 1
probe "SNS"                       aws sns list-topics
probe "Kinesis Data Firehose"     aws firehose list-delivery-streams --limit 1
probe "CloudWatch alarms"         aws cloudwatch describe-alarms --max-records 1
probe "CloudWatch Logs"           aws logs describe-log-groups --limit 1
probe "X-Ray"                     aws xray get-sampling-rules
probe "CodeBuild"                 aws codebuild list-projects
probe "CodePipeline"              aws codepipeline list-pipelines --max-items 1
probe "AWS Config"                aws configservice describe-configuration-recorders
probe "CloudTrail"                aws cloudtrail describe-trails
echo
echo "A list call succeeding does not prove create permissions, but a DENIED here means that service will fail."
echo "If EventBridge Pipes is DENIED, deploy with: USE_PIPES=false ./scripts/deploy.sh"
