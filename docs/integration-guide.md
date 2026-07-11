# Integration Guide / 既存環境への導入

## Languages

- [日本語](#日本語)
- [English](#english)

## 日本語

既存のS3バケットとCloudFront Distributionを前提とします。`infra/bridge-resources.yaml`は次だけを作成し、既存のS3通知設定とDistributionは変更しません。

- CloudFront KeyValueStore
- 同期Lambda、実行ロール、ログ
- CloudFront Functionsのviewer-request関数
- 既存S3からLambdaを呼び出す権限

CloudFront KeyValueStoreとCloudFront Functionsのリソースは`us-east-1`に作成します。既存S3バケットが別リージョンでも、テンプレートのデプロイ先は`us-east-1`です。

### 1. リソースを作成

```bash
aws cloudformation deploy \
  --stack-name htaccess-bridge \
  --template-file infra/bridge-resources.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1 \
  --parameter-overrides ContentBucketName=YOUR-CONTENT-BUCKET
```

### 2. Lambdaコードを配置

テンプレート内のLambdaはプレースホルダーです。KeyValueStore APIのSigV4A認証に必要な`botocore[crt]`を含むため、ビルド後のZIPは約21MBになります。実コードの配置が完了するまで設定ファイルをアップロードしないでください。

```bash
./scripts/build-lambda.sh

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

作成されたリソースはOutputsで確認できます。

```bash
aws cloudformation describe-stacks \
  --stack-name htaccess-bridge \
  --region us-east-1 \
  --query 'Stacks[0].Outputs'
```

### 3. AWS CLIで既存環境へ接続

まずスタックOutputsの`LambdaFunctionArn`と`CloudFrontFunctionArn`を確認し、値を設定します。

```bash
LAMBDA_ARN=YOUR_LAMBDA_FUNCTION_ARN
FUNCTION_ARN=YOUR_CLOUDFRONT_FUNCTION_ARN
```

S3通知の現在値を取得し、既存項目を残したまま次の4項目を`LambdaFunctionConfigurations`へ追加します。`put-bucket-notification-configuration`は設定全体を置換するため、空の設定から作り直さないでください。

```text
.htaccess  -> s3:ObjectCreated:* / s3:ObjectRemoved:*
.htpasswd  -> s3:ObjectCreated:* / s3:ObjectRemoved:*
```

```bash
aws s3api get-bucket-notification-configuration \
  --bucket YOUR-CONTENT-BUCKET > notification.json

LAMBDA_ARN="$LAMBDA_ARN" python3 - <<'PY'
import json, os
p = "notification.json"
d = json.load(open(p))
items = d.setdefault("LambdaFunctionConfigurations", [])
for suffix in (".htaccess", ".htpasswd"):
    for action, event in (("created", "s3:ObjectCreated:*"), ("removed", "s3:ObjectRemoved:*")):
        item_id = f"htaccess-bridge-{suffix[1:]}-{action}"
        items[:] = [item for item in items if item.get("Id") != item_id]
        items.append({
            "Id": item_id,
            "LambdaFunctionArn": os.environ["LAMBDA_ARN"],
            "Events": [event],
            "Filter": {"Key": {"FilterRules": [{"Name": "suffix", "Value": suffix}]}}
        })
json.dump(d, open(p, "w"), indent=2)
PY

aws s3api put-bucket-notification-configuration \
  --bucket YOUR-CONTENT-BUCKET \
  --notification-configuration file://notification.json
```

CloudFront設定も現在値とETagを取得してから更新します。

```bash
aws cloudfront get-distribution-config \
  --id YOUR-DISTRIBUTION-ID > distribution.json
```

既存viewer-request関数がないことを確認してから、対象Behaviorの`FunctionAssociations`へ追加します。次はDefault Cache Behaviorの例です。

```bash
FUNCTION_ARN="$FUNCTION_ARN" python3 - <<'PY'
import json, os
d = json.load(open("distribution.json"))
b = d["DistributionConfig"]["DefaultCacheBehavior"]
a = b.setdefault("FunctionAssociations", {"Quantity": 0})
items = a.setdefault("Items", [])
if any(x["EventType"] == "viewer-request" for x in items):
    raise SystemExit("viewer-request function already exists; merge the bridge logic instead")
items.append({"EventType": "viewer-request", "FunctionARN": os.environ["FUNCTION_ARN"]})
a["Quantity"] = len(items)
json.dump(d["DistributionConfig"], open("distribution-config.json", "w"), indent=2)
open("distribution-etag.txt", "w").write(d["ETag"])
PY

aws cloudfront update-distribution \
  --id YOUR-DISTRIBUTION-ID \
  --distribution-config file://distribution-config.json \
  --if-match "$(cat distribution-etag.txt)"
```

同じBehaviorに既存のviewer-request関数がある場合、複数は関連付けできないためCLIで置換せず、`cloudfront-function/handler.js`の処理を既存の関数生成ロジックへ統合してください。

関連付ける関数ARNはOutputの`CloudFrontFunctionArn`です。統合する場合の処理順序は、保護パス拒否、Basic認証、リダイレクト、`DirectoryIndex`、既存の書き換え処理です。

既存環境をCDKやTerraformで管理している場合は、上記のリソースと関連付けを参考に同等の設定を既存IaCへ取り込んでください。

### 4. S3 Lifecycleで履歴の保持期間を設定

`_control-history/published/`と`_control-history/rejected/`には小さなJSONオブジェクトが蓄積されます。必要に応じて、バケットのバージョニング設定と監査要件に合うS3 Lifecycleルールを設定してください。バージョニングが有効な場合は、現行オブジェクトだけでなく非現行バージョンの保持期間も検討します。保持期間の例は[詳細仕様](reference.md#履歴)を参照してください。

### 5. 設定をアップロード

```bash
aws s3 cp examples/.htpasswd s3://YOUR-CONTENT-BUCKET/.htpasswd
aws s3 cp examples/.htaccess s3://YOUR-CONTENT-BUCKET/.htaccess
```

CloudWatch Logsと履歴でLambdaの自動起動を確認します。

```bash
aws s3 ls s3://YOUR-CONTENT-BUCKET/_control-history/published/
```

KVSの反映には時間がかかる場合があります。60〜90秒待ってからCloudFront経由で確認してください。

```bash
curl -I https://YOUR-DOMAIN/.htaccess
curl -I https://YOUR-DOMAIN/old/
curl -I -u preview:pass https://YOUR-DOMAIN/
```

期待値は順に`403`、リダイレクト、認証成功です。サンプルの認証情報は`preview` / `pass`なので、本番利用前に必ず変更してください。失敗した更新は`_control-history/rejected/`に記録され、直前の有効設定が維持されます。

### 6. 削除

先に既存S3のイベント通知とCloudFrontの関数関連付けを外してから、スタックを削除します。

```bash
aws cloudformation delete-stack \
  --stack-name htaccess-bridge \
  --region us-east-1
```

## English

This project targets an existing S3 bucket and CloudFront Distribution. `infra/bridge-resources.yaml` creates only:

- CloudFront KeyValueStore
- Sync Lambda, execution role, and logs
- A viewer-request function in CloudFront Functions
- Permission for the existing S3 bucket to invoke Lambda

It does not modify the existing bucket notification configuration or Distribution.

Create the CloudFront KeyValueStore and CloudFront Functions resources in `us-east-1`, even when the existing S3 bucket is in another region.

### 1. Create bridge resources

```bash
aws cloudformation deploy \
  --stack-name htaccess-bridge \
  --template-file infra/bridge-resources.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1 \
  --parameter-overrides ContentBucketName=YOUR-CONTENT-BUCKET
```

### 2. Deploy the Lambda code

The template initially creates placeholder Lambda code. The deployment ZIP is approximately 21 MB because it includes `botocore[crt]` for SigV4A access to the KeyValueStore API. Do not upload configuration files until the real code is deployed.

```bash
./scripts/build-lambda.sh

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

Inspect the created resource identifiers in the stack outputs:

```bash
aws cloudformation describe-stacks \
  --stack-name htaccess-bridge \
  --region us-east-1 \
  --query 'Stacks[0].Outputs'
```

### 3. Connect the existing environment with AWS CLI

Copy `LambdaFunctionArn` and `CloudFrontFunctionArn` from the stack outputs:

```bash
LAMBDA_ARN=YOUR_LAMBDA_FUNCTION_ARN
FUNCTION_ARN=YOUR_CLOUDFRONT_FUNCTION_ARN
```

Retrieve the current S3 notification configuration and merge the following four entries into `LambdaFunctionConfigurations`. `put-bucket-notification-configuration` replaces the complete configuration, so preserve every existing entry.

```text
.htaccess  -> s3:ObjectCreated:* / s3:ObjectRemoved:*
.htpasswd  -> s3:ObjectCreated:* / s3:ObjectRemoved:*
```

```bash
aws s3api get-bucket-notification-configuration \
  --bucket YOUR-CONTENT-BUCKET > notification.json

LAMBDA_ARN="$LAMBDA_ARN" python3 - <<'PY'
import json, os
p = "notification.json"
d = json.load(open(p))
items = d.setdefault("LambdaFunctionConfigurations", [])
for suffix in (".htaccess", ".htpasswd"):
    for action, event in (("created", "s3:ObjectCreated:*"), ("removed", "s3:ObjectRemoved:*")):
        item_id = f"htaccess-bridge-{suffix[1:]}-{action}"
        items[:] = [item for item in items if item.get("Id") != item_id]
        items.append({
            "Id": item_id,
            "LambdaFunctionArn": os.environ["LAMBDA_ARN"],
            "Events": [event],
            "Filter": {"Key": {"FilterRules": [{"Name": "suffix", "Value": suffix}]}}
        })
json.dump(d, open(p, "w"), indent=2)
PY

aws s3api put-bucket-notification-configuration \
  --bucket YOUR-CONTENT-BUCKET \
  --notification-configuration file://notification.json
```

Retrieve the current Distribution configuration and ETag before editing it:

```bash
aws cloudfront get-distribution-config \
  --id YOUR-DISTRIBUTION-ID > distribution.json
```

After confirming that no viewer-request function exists, add the bridge function. This example updates the default cache behavior:

```bash
FUNCTION_ARN="$FUNCTION_ARN" python3 - <<'PY'
import json, os
d = json.load(open("distribution.json"))
b = d["DistributionConfig"]["DefaultCacheBehavior"]
a = b.setdefault("FunctionAssociations", {"Quantity": 0})
items = a.setdefault("Items", [])
if any(x["EventType"] == "viewer-request" for x in items):
    raise SystemExit("viewer-request function already exists; merge the bridge logic instead")
items.append({"EventType": "viewer-request", "FunctionARN": os.environ["FUNCTION_ARN"]})
a["Quantity"] = len(items)
json.dump(d["DistributionConfig"], open("distribution-config.json", "w"), indent=2)
open("distribution-etag.txt", "w").write(d["ETag"])
PY

aws cloudfront update-distribution \
  --id YOUR-DISTRIBUTION-ID \
  --distribution-config file://distribution-config.json \
  --if-match "$(cat distribution-etag.txt)"
```

If the behavior already has a viewer-request function, do not replace it. Merge the logic from `cloudfront-function/handler.js` into the existing function generator because only one can be associated per behavior.

Use the `CloudFrontFunctionArn` stack output for a direct association. When merging, preserve this order: protected-path blocking, Basic auth, redirects, `DirectoryIndex`, then existing rewrite logic.

For environments managed by CDK or Terraform, use the resource and association steps above as the contract and implement the equivalent configuration in the existing IaC.

### 4. Configure history retention with S3 Lifecycle

Small JSON objects accumulate under `_control-history/published/` and `_control-history/rejected/`. If needed, configure S3 Lifecycle rules that match the bucket's versioning state and audit requirements. For a versioned bucket, consider retention for noncurrent versions as well as current objects. See the [reference](reference.md#history) for example retention periods.

### 5. Upload configuration

```bash
aws s3 cp examples/.htpasswd s3://YOUR-CONTENT-BUCKET/.htpasswd
aws s3 cp examples/.htaccess s3://YOUR-CONTENT-BUCKET/.htaccess
```

Confirm automatic invocation in CloudWatch Logs and published history:

```bash
aws s3 ls s3://YOUR-CONTENT-BUCKET/_control-history/published/
```

Wait 60–90 seconds for KVS propagation, then verify through CloudFront:

```bash
curl -I https://YOUR-DOMAIN/.htaccess
curl -I https://YOUR-DOMAIN/old/
curl -I -u preview:pass https://YOUR-DOMAIN/
```

Expect `403`, a redirect, and successful authentication respectively. Replace the example `preview` / `pass` credentials before production use. Rejected updates are recorded under `_control-history/rejected/` and do not replace the last valid configuration.

### 6. Remove

Remove the existing S3 event notifications and CloudFront function association before deleting the stack:

```bash
aws cloudformation delete-stack \
  --stack-name htaccess-bridge \
  --region us-east-1
```
