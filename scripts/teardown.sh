#!/usr/bin/env bash
# Removes everything Request Hub created, in reverse order. The playground wipes the
# account when the session ends anyway, but this is the habit to keep in a real account.
set -uo pipefail
STACK="${STACK:-request-hub}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION"

empty_bucket() {
  local bucket="$1"
  [[ -z "$bucket" || "$bucket" == "None" ]] && return
  aws s3api head-bucket --bucket "$bucket" 2>/dev/null || return
  echo "  emptying s3://$bucket"
  aws s3 rm "s3://$bucket" --recursive --only-show-errors
  python3 - "$bucket" <<'EOF'
import sys, boto3
bucket = boto3.resource("s3").Bucket(sys.argv[1])
bucket.object_versions.delete()
EOF
}

out() {
  aws cloudformation describe-stacks --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue | [0]" --output text 2>/dev/null
}

delete_stack() {
  if aws cloudformation describe-stacks --stack-name "$1" >/dev/null 2>&1; then
    echo "Deleting stack $1"
    aws cloudformation delete-stack --stack-name "$1"
    aws cloudformation wait stack-delete-complete --stack-name "$1" && echo "  deleted"
  fi
}

empty_bucket "$(out "${STACK}-governance" TrailBucketName)"
empty_bucket "$(out "${STACK}-governance" ConfigBucketName)"
delete_stack "${STACK}-governance"

empty_bucket "$(out "${STACK}-cicd" SourceBucketName)"
empty_bucket "$(out "${STACK}-cicd" ArtifactBucketName)"
delete_stack "${STACK}-cicd"

empty_bucket "$(out "$STACK" WebBucketName)"
empty_bucket "$(out "$STACK" AttachmentsBucketName)"
empty_bucket "$(out "$STACK" AuditBucketName)"
aws secretsmanager delete-secret --secret-id "${STACK}/test-users" --force-delete-without-recovery >/dev/null 2>&1 \
  && echo "Deleted secret ${STACK}/test-users"
delete_stack "$STACK"
echo "Teardown complete. (The KMS key is scheduled for deletion after its 7-day waiting period.)"
