#!/usr/bin/env bash
# Invocations per Request Hub Lambda function over the last N minutes (default 60).
# The KodeKloud playground removes a function invoked more than 300 times in an hour,
# so check this between long live test runs.
#   ./scripts/lambda-usage.sh        ./scripts/lambda-usage.sh 30
set -euo pipefail
MINUTES="${1:-60}"
STACK="${STACK:-request-hub}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
start=$(date -u -d "-${MINUTES} min" +%Y-%m-%dT%H:%M:%SZ)
end=$(date -u +%Y-%m-%dT%H:%M:%SZ)
printf '%-55s %s\n' "Function (last ${MINUTES} min)" "Invocations"
for fn in $(aws lambda list-functions --region "$REGION" --query "Functions[?starts_with(FunctionName, '${STACK}')].FunctionName" --output text); do
  n=$(aws cloudwatch get-metric-statistics --region "$REGION" --namespace AWS/Lambda --metric-name Invocations \
      --dimensions Name=FunctionName,Value="$fn" --start-time "$start" --end-time "$end" --period 60 --statistics Sum \
      --query 'sum(Datapoints[].Sum)' --output text)
  [[ "$n" == "None" || -z "$n" ]] && n=0
  flag=""; (( ${n%.*} >= 240 )) && flag="  <- close to the 300-an-hour limit"
  printf '%-55s %s%s\n' "$fn" "${n%.*}" "$flag"
done
