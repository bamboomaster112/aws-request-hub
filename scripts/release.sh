#!/usr/bin/env bash
# Packages the repository and drops it in the pipeline's source bucket. S3 sends an
# "Object Created" event to EventBridge, which starts the pipeline (no polling).
#
# Guard rail: the playground deletes a pipeline that runs more than 5 times in an hour,
# so this script refuses to release when 4 runs already happened in the last hour.
set -euo pipefail
STACK="${STACK:-request-hub}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PIPELINE="${STACK}-pipeline"

SOURCE_BUCKET=$(aws cloudformation describe-stacks --stack-name "${STACK}-cicd" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='SourceBucketName'].OutputValue | [0]" --output text)

since=$(date -u -d '-60 minutes' +%Y-%m-%dT%H:%M:%SZ)
recent=$(aws codepipeline list-pipeline-executions --pipeline-name "$PIPELINE" --region "$REGION" --max-items 20 \
  --query "length(pipelineExecutionSummaries[?startTime>='$since'])" --output text 2>/dev/null || echo 0)
if [[ "${recent:-0}" =~ ^[0-9]+$ ]] && (( recent >= 4 )); then
  echo "Refusing to release: $PIPELINE already ran $recent times in the last hour (playground limit is 5)."
  exit 1
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
cd "$ROOT"
if git rev-parse --git-dir >/dev/null 2>&1; then
  git archive --format=zip -o "$TMP/request-hub.zip" HEAD
  echo "Packaged commit $(git rev-parse --short HEAD)"
else
  zip -qr "$TMP/request-hub.zip" . -x '.git/*' '.aws-sam/*'
fi
aws s3 cp "$TMP/request-hub.zip" "s3://$SOURCE_BUCKET/request-hub.zip" --region "$REGION" --only-show-errors
echo "Uploaded. EventBridge will start $PIPELINE in a few seconds:"
echo "https://${REGION}.console.aws.amazon.com/codesuite/codepipeline/pipelines/${PIPELINE}/view?region=${REGION}"
