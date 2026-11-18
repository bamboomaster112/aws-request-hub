#!/usr/bin/env bash
# Uploads the single-page app to the web bucket and writes its runtime config.json
# from the core stack outputs. Used by deploy.sh and by the pipeline's Deploy stage.
set -euo pipefail

STACK="${STACK:-request-hub}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

out() {
  aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue | [0]" --output text
}

WEB_BUCKET=$(out WebBucketName)
APP_URL=$(out AppUrl)
API_BASE=$(out ApiBaseUrl)
CLIENT_ID=$(out WebClientId)
USER_POOL_ID=$(out UserPoolId)
COGNITO_DOMAIN=$(out CognitoDomain)

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
cat > "$TMP/config.json" <<EOF
{
  "apiBase": "$API_BASE",
  "region": "$REGION",
  "userPoolId": "$USER_POOL_ID",
  "clientId": "$CLIENT_ID",
  "cognitoDomain": "$COGNITO_DOMAIN",
  "redirectUri": "$APP_URL"
}
EOF

put() { # file key content-type cache-control
  aws s3 cp "$1" "s3://$WEB_BUCKET/$2" --region "$REGION" --content-type "$3" --cache-control "$4" --only-show-errors
}

put "$ROOT/web/index.html" index.html "text/html; charset=utf-8" "no-cache"
put "$ROOT/web/assets/app.js" assets/app.js "text/javascript; charset=utf-8" "no-cache"
put "$ROOT/web/assets/styles.css" assets/styles.css "text/css; charset=utf-8" "no-cache"
put "$TMP/config.json" config.json "application/json" "no-store"

echo "Web app published to s3://$WEB_BUCKET"
echo "Open: $APP_URL"
