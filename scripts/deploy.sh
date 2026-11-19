#!/usr/bin/env bash
# One-command deploy for Request Hub. Run from AWS CloudShell inside the playground.
#
#   ./scripts/deploy.sh                                  # core stack + web app + CI/CD pipeline
#   NOTIFICATION_EMAIL=you@example.com ./scripts/deploy.sh
#   USE_PIPES=false ./scripts/deploy.sh                  # Lambda CDC publisher instead of EventBridge Pipes
#   WITH_GOVERNANCE=true ./scripts/deploy.sh             # also CloudTrail + AWS Config rules
#   LOGIN_STYLE=default ./scripts/deploy.sh              # sign-in page: custom (app look), default (Cognito's) or classic
#                                                        # (classic is branded by scripts/brand-sign-in.sh)
#
set -euo pipefail

STACK="${STACK:-request-hub}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION" STACK
NOTIFICATION_EMAIL="${NOTIFICATION_EMAIL:-}"
USE_PIPES="${USE_PIPES:-true}"
API_LOGGING="${API_LOGGING:-true}"
SLA_SECONDS="${SLA_SECONDS:-180}"
WITH_PIPELINE="${WITH_PIPELINE:-true}"
WITH_GOVERNANCE="${WITH_GOVERNANCE:-false}"
LOGIN_STYLE="${LOGIN_STYLE:-custom}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
started=$(date +%s)

step "Identity and region"
aws sts get-caller-identity --query '[Account, Arn]' --output text
echo "Region: $REGION   Stack: $STACK"

# Some services that the playground page lists are blocked by an organisation-level
# service control policy. Detect that up front and switch to the built-in fallbacks.
denied() { local err; err=$("$@" 2>&1 >/dev/null); [[ "$err" == *AccessDenied* ]]; }
ENABLE_WAF="${ENABLE_WAF:-auto}"
if [[ "$ENABLE_WAF" == "auto" ]]; then
  if denied aws wafv2 list-web-acls --scope REGIONAL --limit 1; then
    ENABLE_WAF=false; echo "AWS WAF is blocked in this account: deploying without the web ACL (API Gateway throttling still applies)."
  else
    ENABLE_WAF=true
  fi
fi
# A made-up pool and style ID: an allowed caller gets "not found", a blocked one gets AccessDenied.
if [[ "$LOGIN_STYLE" != "classic" ]] && denied aws cognito-idp describe-managed-login-branding \
    --user-pool-id "${REGION}_AAAAAAAAA" --managed-login-branding-id 00000000-0000-0000-0000-000000000000; then
  LOGIN_STYLE=classic; echo "Managed login branding is blocked in this account: keeping the classic hosted sign-in page."
fi
if [[ "$USE_PIPES" == "true" ]] && denied aws pipes list-pipes --limit 1; then
  USE_PIPES=false; echo "EventBridge Pipes is blocked in this account: using the Lambda CDC publisher instead."
fi

# The playground denies secretsmanager:GetRandomPassword, so generate the downstream API key here.
DOWNSTREAM_API_KEY="${DOWNSTREAM_API_KEY:-$(openssl rand -hex 24)}"

# On a redeploy, keep the email address the stack already has unless a new one is given.
if [[ -z "$NOTIFICATION_EMAIL" ]]; then
  NOTIFICATION_EMAIL=$(aws cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Parameters[?ParameterKey=='NotificationEmail'].ParameterValue | [0]" --output text 2>/dev/null || true)
  [[ "$NOTIFICATION_EMAIL" == "None" ]] && NOTIFICATION_EMAIL=""
fi

PARAMS=(
  "DownstreamApiKey=$DOWNSTREAM_API_KEY"
  "UseEventBridgePipes=$USE_PIPES"
  "EnableWaf=$ENABLE_WAF"
  "LoginStyle=$LOGIN_STYLE"
  "EnableApiGatewayAccountLogging=$API_LOGGING"
  "ApprovalSlaSeconds=$SLA_SECONDS"
  "EscalationSlaSeconds=$SLA_SECONDS"
)
# The SAM CLI rejects an empty override ("NotificationEmail="), so only pass it when set.
if [[ -n "$NOTIFICATION_EMAIL" ]]; then
  PARAMS+=("NotificationEmail=$NOTIFICATION_EMAIL")
fi

step "Deploying core stack ($STACK)"
# A first deploy that rolled back leaves a ROLLBACK_COMPLETE stack, which cannot be updated.
status=$(aws cloudformation describe-stacks --stack-name "$STACK" --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo NONE)
if [[ "$status" == "ROLLBACK_COMPLETE" || "$status" == "ROLLBACK_FAILED" || "$status" == "DELETE_FAILED" ]]; then
  echo "Removing the previous failed stack ($status) first..."
  aws cloudformation delete-stack --stack-name "$STACK"
  aws cloudformation wait stack-delete-complete --stack-name "$STACK" 2>/dev/null || {
    aws cloudformation delete-stack --stack-name "$STACK" --deletion-mode FORCE_DELETE_STACK
    aws cloudformation wait stack-delete-complete --stack-name "$STACK"
  }
fi
if command -v sam >/dev/null 2>&1; then
  sam deploy \
    --template-file template.yaml \
    --stack-name "$STACK" \
    --region "$REGION" \
    --resolve-s3 \
    --s3-prefix "$STACK" \
    --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
    --no-confirm-changeset \
    --no-fail-on-empty-changeset \
    --parameter-overrides "${PARAMS[@]}"
else
  echo "SAM CLI not found, falling back to aws cloudformation package/deploy"
  ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
  ARTIFACTS="${STACK}-artifacts-${ACCOUNT}-${REGION}"
  aws s3api head-bucket --bucket "$ARTIFACTS" 2>/dev/null || aws s3 mb "s3://$ARTIFACTS" --region "$REGION"
  aws cloudformation package --template-file template.yaml --s3-bucket "$ARTIFACTS" --s3-prefix "$STACK" \
    --output-template-file /tmp/packaged.yaml
  aws cloudformation deploy --template-file /tmp/packaged.yaml --stack-name "$STACK" \
    --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
    --no-fail-on-empty-changeset --parameter-overrides "${PARAMS[@]}"
fi

step "Refreshing the API deployment"
API_ID=$(aws cloudformation describe-stacks --stack-name "$STACK" --query "Stacks[0].Outputs[?OutputKey=='ApiId'].OutputValue | [0]" --output text)
aws apigateway create-deployment --rest-api-id "$API_ID" --stage-name v1 --description "deploy.sh $(date -u +%FT%TZ)" --query id --output text

step "Verifying EventBridge rules"
"$ROOT/scripts/verify-rules.sh" || true

step "Seeding forms, workflows and approver groups (never overwrites admin changes)"
SLA_SECONDS="$SLA_SECONDS" python3 "$ROOT/scripts/seed-config.py"

step "Publishing the web app"
"$ROOT/scripts/publish-web.sh"

if [[ "$LOGIN_STYLE" == "classic" ]]; then
  step "Branding the sign-in page"
  "$ROOT/scripts/brand-sign-in.sh" || echo "Could not brand the classic sign-in page; it keeps Cognito's default look."
fi

if [[ "$WITH_PIPELINE" == "true" ]]; then
  step "Deploying CI/CD stack (${STACK}-cicd)"
  aws cloudformation deploy \
    --template-file pipeline/cicd.yaml \
    --stack-name "${STACK}-cicd" \
    --capabilities CAPABILITY_IAM \
    --no-fail-on-empty-changeset \
    --parameter-overrides "CoreStackName=$STACK"
  echo "Pipeline ready. Run ./scripts/release.sh to push this code through it."
fi

if [[ "$WITH_GOVERNANCE" == "true" ]]; then
  step "Deploying governance stack (${STACK}-governance)"
  existing=$(aws configservice describe-configuration-recorders --query 'length(ConfigurationRecorders)' --output text 2>/dev/null || echo 0)
  create_recorder=true
  if [[ "$existing" != "0" ]]; then
    echo "An AWS Config recorder already exists in this account, so only the rules are deployed."
    create_recorder=false
  fi
  aws cloudformation deploy \
    --template-file governance/governance.yaml \
    --stack-name "${STACK}-governance" \
    --capabilities CAPABILITY_IAM \
    --no-fail-on-empty-changeset \
    --parameter-overrides "CoreStackName=$STACK" "CreateConfigRecorder=$create_recorder"
fi

step "Done in $(( $(date +%s) - started ))s"
aws cloudformation describe-stacks --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?contains(['AppUrl','UserPoolId','CognitoDomain','DashboardUrl','CdcMode'], OutputKey)].[OutputKey, OutputValue]" \
  --output table
cat <<'EOF'

Next:
  ./scripts/create-user.sh you@yourdomain.com admins   # your own admin login for the web app
  python3 scripts/seed-users.py      # requester -> approver -> two admins, with MFA, for the smoke and UI tests
  python3 scripts/smoke-test.py      # end-to-end checks through API Gateway, workflow and fulfilment
EOF
