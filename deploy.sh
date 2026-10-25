#!/usr/bin/env bash
# Build the Lambda package and deploy the stack.
# Usage: ./deploy.sh [ParamName=value ...]   e.g. ./deploy.sh UseAI=false
set -euo pipefail
cd "$(dirname "$0")"

REGION=${AWS_REGION:-us-east-2}
STACK=nfl-pickem
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET=nfl-pickem-artifacts-$ACCOUNT-$REGION

rm -rf build && mkdir build
# Linux/arm64 wheels for the Lambda runtime, regardless of the local machine.
# --no-compile keeps .pyc files (which embed mtimes) out of the zip, so identical code packages to an identical key.
python3 -m pip install -q --no-compile --require-hashes -r requirements.txt -t build \
  --platform manylinux2014_aarch64 --implementation cp --python-version 3.14 --only-binary=:all:
# The Lambda runtime already ships boto3/botocore; don't bundle a second copy.
rm -rf build/{boto3,botocore,s3transfer,jmespath}{,-*.dist-info}
cp src/*.py build/

aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null || \
  aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
    --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
# The bucket lives outside the stack, so it needs its own tag (also fixes buckets created before tagging).
aws s3api put-bucket-tagging --bucket "$BUCKET" \
  --tagging 'TagSet=[{Key=project,Value=nfl-pickem}]'

aws cloudformation package --region "$REGION" --template-file template.yaml \
  --s3-bucket "$BUCKET" --output-template-file build/packaged.yaml >/dev/null

aws cloudformation deploy --region "$REGION" --stack-name "$STACK" \
  --template-file build/packaged.yaml --capabilities CAPABILITY_IAM \
  --tags project=nfl-pickem --no-fail-on-empty-changeset \
  ${1:+--parameter-overrides "$@"}

# Keep only the zip the stack now points at; older ones are never needed again.
KEY=$(awk '/S3Key:/ {print $2; exit}' build/packaged.yaml)
for OLD in $(aws s3api list-objects-v2 --bucket "$BUCKET" --query 'Contents[].Key' --output text); do
  [ "$OLD" = "$KEY" ] || [ "$OLD" = None ] || aws s3 rm --quiet "s3://$BUCKET/$OLD"
done
