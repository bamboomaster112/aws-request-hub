#!/usr/bin/env bash
# Brands the classic hosted sign-in page with the Request Hub logo and colours.
# deploy.sh runs it when LoginStyle is classic, which it picks when the account blocks
# managed login branding (the KodeKloud playground denies cognito-idp:CreateManagedLoginBranding).
# The classic page only accepts a fixed set of CSS classes: see web/brand/hosted-ui.css.
set -euo pipefail

STACK="${STACK:-request-hub}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

out() {
  aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue | [0]" --output text
}

POOL=$(out UserPoolId)
CLIENT=$(out WebClientId)
CSS=$(tr '\n' ' ' < "$ROOT/web/brand/hosted-ui.css")
aws cognito-idp set-ui-customization --region "$REGION" --user-pool-id "$POOL" --client-id "$CLIENT" \
  --css "$CSS" --image-file "fileb://$ROOT/web/brand/logo.png" >/dev/null
echo "Sign-in page branded: Request Hub logo and the app's colours (classic hosted UI)."
