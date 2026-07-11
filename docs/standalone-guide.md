# Standalone setup / 新規環境の構築

`infra/standalone.yaml` creates a content bucket, Lambda, CloudFront KeyValueStore, a function in CloudFront Functions, and a CloudFront Distribution in one stack. It does not create a Lambda artifact bucket.

`infra/standalone.yaml` は、コンテンツバケット、Lambda、CloudFront KeyValueStore、CloudFront Functions の関数、CloudFront Distributionを1スタックで作成します。Lambdaアーティファクトバケットは作成しません。

## Deploy

```sh
./scripts/build-lambda.sh

aws cloudformation create-stack \
  --stack-name htaccess-bridge \
  --template-body file://infra/standalone.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1

aws cloudformation wait stack-create-complete \
  --stack-name htaccess-bridge \
  --region us-east-1

FUNCTION_NAME=$(aws cloudformation describe-stacks \
  --stack-name htaccess-bridge \
  --region us-east-1 \
  --query 'Stacks[0].Outputs[?OutputKey==`LambdaFunctionName`].OutputValue' \
  --output text)

aws lambda update-function-code \
  --function-name "$FUNCTION_NAME" \
  --zip-file fileb://htaccess_bridge.zip \
  --region us-east-1
```

The stack initially creates the Lambda with placeholder code. Do not upload a `.htaccess` file until `update-function-code` succeeds.

スタック作成時のLambdaはプレースホルダーコードです。`update-function-code` が成功するまで `.htaccess` をアップロードしないでください。

Stack outputs include the content bucket name and CloudFront domain name.

スタックのOutputsから、コンテンツバケット名とCloudFrontドメイン名を確認できます。
