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
python3 -m pip install -q -r requirements.txt -t build \
  --platform manylinux2014_aarch64 --implementation cp --python-version 3.14 --only-binary=:all:
cp src/*.py build/

aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null || \
  aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
    --create-bucket-configuration LocationConstraint="$REGION" >/dev/null

aws cloudformation package --region "$REGION" --template-file template.yaml \
  --s3-bucket "$BUCKET" --output-template-file build/packaged.yaml >/dev/null

aws cloudformation deploy --region "$REGION" --stack-name "$STACK" \
  --template-file build/packaged.yaml --capabilities CAPABILITY_IAM \
  ${1:+--parameter-overrides "$@"}
