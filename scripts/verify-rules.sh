#!/usr/bin/env bash
# Verifies that every EventBridge rule in the core stack really exists, and rebuilds the
# workflow start rule if it is missing.
#
# Why: in the KodeKloud playground, CloudFormation hits a tagging permission error on
# AWS::Events::Rule and "retries without tags". In one run it then reported the start rule
# as CREATE_COMPLETE although the rule did not exist, so new requests never reached
# Step Functions. deploy.sh runs this as its last step.
set -uo pipefail
STACK="${STACK:-request-hub}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION"

physical() {
  aws cloudformation describe-stack-resource --stack-name "$STACK" --logical-resource-id "$1" \
    --query 'StackResourceDetail.PhysicalResourceId' --output text
}
output() {
  aws cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue | [0]" --output text
}

rebuild_start_rule() {
  local name="$1" sm role dlq
  sm=$(output StateMachineArn)
  role=$(aws iam get-role --role-name "$(physical EventsToWorkflowRole)" --query Role.Arn --output text)
  dlq=$(aws sqs get-queue-attributes --queue-url "$(output EventsDlqUrl)" --attribute-names QueueArn \
    --query Attributes.QueueArn --output text)
  aws events put-rule --name "$name" --state ENABLED \
    --description "Start the approval workflow for every new request" \
    --event-pattern '{"source":["request-hub.requests"],"detail-type":["Request Submitted"]}' >/dev/null
  aws events put-targets --rule "$name" --targets \
    "Id=approval-workflow,Arn=$sm,RoleArn=$role,DeadLetterConfig={Arn=$dlq},RetryPolicy={MaximumRetryAttempts=4,MaximumEventAgeInSeconds=3600}" \
    --query FailedEntryCount --output text >/dev/null
  echo "  rebuilt $name"
}

status=0
while read -r logical name; do
  [[ -z "$logical" ]] && continue
  name="${name##*|}"
  if aws events describe-rule --name "$name" >/dev/null 2>&1; then
    echo "  ok       $logical ($name)"
  else
    echo "  MISSING  $logical ($name)"
    if [[ "$logical" == "SubmittedRule" ]]; then
      rebuild_start_rule "$name"
    else
      status=1
    fi
  fi
done < <(aws cloudformation describe-stack-resources --stack-name "$STACK" \
  --query "StackResources[?ResourceType=='AWS::Events::Rule'].[LogicalResourceId,PhysicalResourceId]" --output text)

if (( status )); then
  echo "Some rules are missing. Redeploy, or recreate them from template.yaml."
fi
exit $status
