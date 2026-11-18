#!/usr/bin/env bash
# Creates a real user for the web app. Cognito emails a temporary password; on first
# sign-in the hosted UI asks for a new password and shows a QR code to enrol MFA.
#
#   ./scripts/create-user.sh you@yourdomain.com approvers     # groups: requesters | approvers | admins
set -euo pipefail
EMAIL="${1:?usage: create-user.sh <email> [requesters|approvers|admins]}"
GROUP="${2:-requesters}"
STACK="${STACK:-request-hub}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"

POOL=$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue | [0]" --output text)
APP=$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='AppUrl'].OutputValue | [0]" --output text)

aws cognito-idp admin-create-user --region "$REGION" --user-pool-id "$POOL" --username "$EMAIL" \
  --user-attributes Name=email,Value="$EMAIL" Name=email_verified,Value=true \
  --desired-delivery-mediums EMAIL >/dev/null
aws cognito-idp admin-add-user-to-group --region "$REGION" --user-pool-id "$POOL" --username "$EMAIL" --group-name "$GROUP"

echo "Created $EMAIL in '$GROUP'. Cognito has emailed a temporary password (check spam)."
echo "Sign in at: $APP"
