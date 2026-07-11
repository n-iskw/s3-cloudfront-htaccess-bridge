# Integration Guide (Existing S3 + CloudFront Environments) / 既存環境への組み込みガイド

## Languages

- [日本語](#日本語)
- [English](#english)

## 日本語

### 前提

このガイドは、以下がすべて既存で用意されている状態を前提にします。

- サイトコンテンツを配信している S3 バケット
- そのバケットをオリジンとする CloudFront Distribution

このプロジェクトが追加するのは、リダイレクト・Basic 認証メンテナンスモード・DirectoryIndex を実現するための KVS・Lambda・CloudFront Functions の関数・S3 イベント通知だけです。既存の S3 バケット・CloudFront Distribution 自体は新規作成しません。既存バケットの設定（暗号化・バージョニング・パブリックアクセスブロック等）や、既存 Distribution の他の設定（他の Cache Behavior、オリジン設定等）も変更しません。

### 既存IaC製品へ統合する場合

既存環境がCDK、TerraformなどのIaCで管理されている場合、`infra/integration-addon.yaml`を別スタックとしてそのままデプロイすることは推奨しません。このテンプレートは、既存のS3通知設定を置換するカスタムリソースを含むためです。また、同じCache Behaviorの`viewer-request`には複数のCloudFront Functionsの関数を関連付けられません。

その場合は、このリポジトリを仕様・ロジックのリファレンスとして利用し、次を既存IaCへ移植してください。

- `lambda/htaccess_bridge.py`と、その約21MBの依存関係を含むLambdaパッケージ
- CloudFront KeyValueStoreとLambdaのIAM権限
- `.htaccess`と`.htpasswd`のS3イベント通知（既存IaCのbucket notification APIで追加）
- `cloudfront-function/handler.js`の処理（既存の`viewer-request` Function生成ロジックへ統合）

`infra/integration-addon.yaml`は、既存のS3通知や`viewer-request` Functionがない単純な環境での検証・導入例として位置付けます。

新規に S3 バケットと CloudFront Distribution を両方作る場合は `infra/standalone.yaml` を使います。既存環境への組み込みでは、下記の `infra/integration-addon.yaml` を使います。

### 使うテンプレート: `infra/integration-addon.yaml`

`infra/standalone.yaml` から、新規 S3 バケット・新規 CloudFront Distribution の作成部分を除いたテンプレートです。作成するリソースと、作成しないリソースは以下の通りです。

| リソース | このテンプレートで作成するか |
|---|---|
| CloudFront KeyValueStore | 作成する |
| Lambda 関数・実行ロール | 作成する |
| S3 バケット通知設定（カスタムリソース） | 作成する（既存バケットに追加） |
| CloudFront Functions の関数 | 作成する（コード本体のみ。既存 Distribution への関連付けは別途手動） |
| S3 バケット本体 | **作成しない**（既存バケットを使う） |
| CloudFront Distribution 本体 | **作成しない**（既存 Distribution を使う） |
| Lambda デプロイ用アーティファクトバケット | **作成しない**（ZIPを直接アップロード） |

### 手順

以下は `us-east-1` リージョンでの実行例です。CloudFront KeyValueStore・CloudFront Functions の関数は `us-east-1` でのみ作成できます（[公式ドキュメント](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/cloudfront-limits.html)）。既存の S3 バケット・Distribution が他リージョンにあっても、この点は変わりません。

このテンプレートはLambdaアーティファクトバケットを使いません。`infra/integration-addon.yaml`のLambda関数はプレースホルダーコード（`Code.ZipFile`インライン）で作成し、実際のコードは`aws lambda update-function-code --zip-file`でローカルのzipを直接アップロードします（S3を経由しません）。

#### 1. テンプレートを検証してデプロイする

```bash
cd s3-cloudfront-htaccess-bridge

aws cloudformation validate-template \
  --template-body file://infra/integration-addon.yaml \
  --region us-east-1

aws cloudformation create-stack \
  --stack-name htaccess-bridge-integration \
  --template-body file://infra/integration-addon.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1 \
  --parameters \
      ParameterKey=ContentBucketName,ParameterValue=YOUR-EXISTING-CONTENT-BUCKET

aws cloudformation wait stack-create-complete \
  --stack-name htaccess-bridge-integration \
  --region us-east-1
```

Basic 認証（メンテナンスモード）の認証情報は、コンテンツと一緒に `.htpasswd`（`htpasswd -s` の `{SHA}` 形式）としてアップロードします。

この時点では、Lambda関数はプレースホルダーコード（呼ばれるとエラーを返すだけ）で作成されています。まだ`.htaccess`をアップロードしても正しく動作しません。

#### 2. Lambdaデプロイパッケージをビルドし、実コードを直接デプロイする

CloudFront KeyValueStore API は Signature Version 4A（SigV4A）認証を要求するため、`botocore[crt]`（AWS Common Runtime のネイティブバイナリを含む拡張）を明示的にインストールして zip に同梱する必要があります（[公式ドキュメント](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/kvs-with-functions-kvp.html)）。

```bash
./scripts/build-lambda.sh
```

`--platform`/`--python-version` は、テンプレート（`Runtime: python3.13`）と一致させています。ローカル環境（特に macOS の Apple Silicon）とアーキテクチャが異なる場合、このクロスプラットフォームビルドは必須です。`awscrt` を含む結果、zip サイズは実測で約21MBでした（AWS Lambdaの直接アップロード上限50MB以内。実機確認済み）。

```bash
aws lambda update-function-code \
  --function-name htaccess-bridge-fn \
  --zip-file fileb://htaccess_bridge.zip \
  --region us-east-1
```

この関数名は`ProjectName`パラメータ（デフォルト`htaccess-bridge`）+`-fn`です。カスタムの`ProjectName`を指定した場合は関数名もそれに合わせて変わります（手順3のOutputsで`LambdaFunctionName`として確認できます）。

50MBを超える依存関係を持つ場合（今回は該当しない）は、S3経由（`--s3-bucket`/`--s3-key`）でのデプロイが必要になります。

#### 3. スタックの出力を取得する

```bash
aws cloudformation describe-stacks \
  --stack-name htaccess-bridge-integration \
  --region us-east-1 \
  --query 'Stacks[0].Outputs'
```

`CloudFrontFunctionArn` の値を控えます（次のステップで使います）。

#### 4. 既存の CloudFront Distribution に CloudFront Functions の関数を関連付ける

このテンプレートは既存 Distribution 自体を変更しません。以下の手順で手動（または自分の IaC）で関連付けます。

```bash
# 既存 Distribution の現在の設定と ETag を取得
aws cloudfront get-distribution-config \
  --id YOUR-EXISTING-DISTRIBUTION-ID \
  > dist-config.json

# dist-config.json の "ETag" を控える
cat dist-config.json | python3 -c "import json,sys; print(json.load(sys.stdin)['ETag'])"
```

`dist-config.json` の `DistributionConfig.DefaultCacheBehavior`（または対象の `CacheBehaviors` エントリ）に、以下を追加します。既存の `CacheBehavior` に既に別の `viewer-request` 関数が設定されている場合、1つの Cache Behavior には `viewer-request` イベントの関数を1つしか関連付けられないため（[公式ドキュメント](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/cloudfront-limits.html) の `Quotas on CloudFront Functions`）、既存の関数ロジックと統合する必要があります。

```json
"FunctionAssociations": {
  "Quantity": 1,
  "Items": [
    {
      "EventType": "viewer-request",
      "FunctionARN": "手順3で取得した CloudFrontFunctionArn の値"
    }
  ]
}
```

編集後、`DistributionConfig` 部分だけを取り出して更新します（`get-distribution-config` のレスポンスは `{"ETag": ..., "DistributionConfig": {...}}` という構造のため、`update-distribution` には `DistributionConfig` 部分のみを渡します）。

```bash
python3 -c "import json; d=json.load(open('dist-config.json')); json.dump(d['DistributionConfig'], open('dist-config-only.json','w'))"

aws cloudfront update-distribution \
  --id YOUR-EXISTING-DISTRIBUTION-ID \
  --distribution-config file://dist-config-only.json \
  --if-match "手順4で取得した ETag の値"
```

#### 5. `.htaccess` をアップロードして自動トリガーを確認する

```bash
aws s3 cp examples/.htaccess s3://YOUR-EXISTING-CONTENT-BUCKET/.htaccess
```

`aws lambda invoke` で手動起動して確認するのは、本番相当の動作確認になりません（S3 イベント通知経由の自動起動を検証する必要があります。詳細は `README.md` の実機検証結果を参照）。CloudWatch Logs で Lambda が自動的に起動したことを確認します。

```bash
aws logs describe-log-streams \
  --log-group-name /aws/lambda/htaccess-bridge-fn \
  --order-by LastEventTime \
  --descending \
  --max-items 1 \
  --region us-east-1
```

直近のログストリームが、S3 アップロード後の時刻で自動生成されていれば、S3 イベント通知経由で Lambda が起動しています。

KVS の更新はエッジロケーションへの伝播に最大 60 秒程度かかります。実機での動作確認は 60〜90 秒待ってから行ってください（詳細は `README.md` の「注意事項」を参照）。

```bash
sleep 90
curl -I https://YOUR-EXISTING-DISTRIBUTION-DOMAIN/.htaccess
# 期待結果: 403 Forbidden
```

### 既存の通知設定との統合

既存の（サイトコンテンツ）バケットに既に S3 イベント通知（Lambda・SNS・SQS のいずれか）が設定されている場合、`s3:PutBucketNotificationConfiguration` は既存の設定全体を1回の呼び出しで置き換えます。追記ではなく上書きです。事前に確認してください。

```bash
aws s3api get-bucket-notification-configuration --bucket YOUR-EXISTING-CONTENT-BUCKET
```

結果が空（`{}`）でない場合、`infra/integration-addon.yaml` の `BucketNotificationFunction`（カスタムリソース Lambda）をそのままデプロイすると、既存の通知設定が失われます。この場合は、`BucketNotificationFunction` のコード（`s3.put_bucket_notification_configuration` を呼ぶ箇所）を、`s3:GetBucketNotificationConfiguration` で既存設定を取得してから `LambdaFunctionConfigurations` に追記するよう改修してからデプロイしてください。現在の実装は、バケットに他の通知設定がない前提です。

### 最小構成（DirectoryIndex のみ、リダイレクト・Basic 認証を使わない場合）

`.htaccess` を一切アップロードしない運用であれば、Lambda・S3 イベント通知・アーティファクトバケットは不要です。CloudFront Functions の関数と KVS（空の状態）だけを追加すれば、`resolveIndexDocument()` のロジック（`.htaccess` の有無と無関係に常時動作）だけが機能します。この場合、`infra/integration-addon.yaml` から `HtaccessBridgeFunction` / `ContentBucketLambdaPermission` / `BucketNotification*` リソースを削除したテンプレートを別途作成してください。

### 削除する場合

```bash
aws cloudformation delete-stack \
  --stack-name htaccess-bridge-integration \
  --region us-east-1
```

このスタックの削除前に、手順4で追加した `FunctionAssociations` を既存 Distribution から先に取り除いてください（`CloudFront Functions の関数` リソースが削除された後も Distribution 側に参照が残っていると、次回のデプロイやトラブルシューティングで混乱の元になります）。

---

## English

### Prerequisites

This guide assumes both of the following already exist:

- An S3 bucket serving your site content
- A CloudFront Distribution using that bucket as its origin

This project only adds the KVS, Lambda, the function in CloudFront Functions, and S3 event notification needed for redirects, Basic-auth maintenance mode, and DirectoryIndex. It does not create your S3 bucket or CloudFront Distribution. It does not modify your existing bucket settings (encryption, versioning, public access block, etc.) or other parts of your existing Distribution (other cache behaviors, origins, etc.).

### Integrating into an existing IaC-managed product

If the existing environment is managed by CDK, Terraform, or another IaC system, do not deploy `infra/integration-addon.yaml` unchanged as a separate stack. Its custom resource replaces the bucket's S3 notification configuration, and a cache behavior cannot associate multiple functions in CloudFront Functions with the same `viewer-request` event.

Use this repository as a specification and logic reference, and port the following into the existing IaC:

- `lambda/htaccess_bridge.py` and its approximately 21 MB Lambda package with dependencies
- The CloudFront KeyValueStore and Lambda IAM permissions
- `.htaccess` and `.htpasswd` S3 event notifications, added through the existing IaC's bucket notification API
- The logic from `cloudfront-function/handler.js`, merged into the existing `viewer-request` function generator

Treat `infra/integration-addon.yaml` as a validation and deployment example for simple environments that have no existing S3 notifications or `viewer-request` function.

Use `infra/standalone.yaml` to create both a new S3 bucket and a new CloudFront Distribution. For an existing environment, use `infra/integration-addon.yaml` below instead.

### Template to use: `infra/integration-addon.yaml`

This is `infra/standalone.yaml` with the new-S3-bucket and new-CloudFront-Distribution portions removed.

| Resource | Created by this template? |
|---|---|
| CloudFront KeyValueStore | Yes |
| Lambda function + execution role | Yes (placeholder code; real code deployed via CLI, see step 2) |
| S3 bucket notification (custom resource) | Yes (added to your existing bucket) |
| the function in CloudFront Functions | Yes (code only; you attach it to your existing Distribution manually) |
| S3 bucket itself | **No** (use your existing bucket) |
| CloudFront Distribution itself | **No** (use your existing Distribution) |
| Lambda deployment artifact bucket | **No** (not needed -- code is deployed directly via `update-function-code --zip-file`) |

### Steps

The examples below use `us-east-1`. CloudFront KeyValueStore and CloudFront Functions can only be created in `us-east-1` ([official documentation](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/cloudfront-limits.html)), regardless of which region your existing bucket/Distribution use.

This template does not use a Lambda artifact bucket. The Lambda function in `infra/integration-addon.yaml` is created with placeholder code (an inline `Code.ZipFile`), and the real code is deployed afterward with `aws lambda update-function-code --zip-file`, uploading your local zip directly (no S3 involved).

#### 1. Validate and deploy the template

```bash
cd s3-cloudfront-htaccess-bridge

aws cloudformation validate-template \
  --template-body file://infra/integration-addon.yaml \
  --region us-east-1

aws cloudformation create-stack \
  --stack-name htaccess-bridge-integration \
  --template-body file://infra/integration-addon.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1 \
  --parameters \
      ParameterKey=ContentBucketName,ParameterValue=YOUR-EXISTING-CONTENT-BUCKET

aws cloudformation wait stack-create-complete \
  --stack-name htaccess-bridge-integration \
  --region us-east-1
```

Basic auth credentials are uploaded with the content as `.htpasswd` in the `{SHA}` format produced by `htpasswd -s`.

At this point the Lambda function exists with placeholder code only (it raises an error if invoked). Uploading a `.htaccess` file will not work correctly yet.

#### 2. Build the Lambda deployment package and deploy the real code directly

The CloudFront KeyValueStore API requires SigV4A authentication, so you need `botocore[crt]` (which bundles the AWS Common Runtime native binary) packaged into the zip ([official documentation](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/kvs-with-functions-kvp.html)).

```bash
./scripts/build-lambda.sh
```

`--platform`/`--python-version` match the template's `Runtime: python3.13`. This cross-platform build is required if your local machine has a different architecture (e.g. Apple Silicon macOS). The resulting zip was measured at about 21MB (well within the 50MB limit for direct Lambda uploads -- verified against real infrastructure).

```bash
aws lambda update-function-code \
  --function-name htaccess-bridge-fn \
  --zip-file fileb://htaccess_bridge.zip \
  --region us-east-1
```

The function name is `ProjectName` (default `htaccess-bridge`) + `-fn`. If you used a custom `ProjectName`, the function name changes accordingly (check the `LambdaFunctionName` stack output in step 3).

If your dependencies ever exceed 50MB (not the case here), you would need to deploy via S3 (`--s3-bucket`/`--s3-key`) instead.

#### 3. Retrieve stack outputs

```bash
aws cloudformation describe-stacks \
  --stack-name htaccess-bridge-integration \
  --region us-east-1 \
  --query 'Stacks[0].Outputs'
```

Note the `CloudFrontFunctionArn` value for the next step.

#### 4. Attach the function in CloudFront Functions to your existing Distribution

This template does not modify your existing Distribution. Attach it manually (or via your own IaC) as follows.

```bash
# Fetch the current config and ETag
aws cloudfront get-distribution-config \
  --id YOUR-EXISTING-DISTRIBUTION-ID \
  > dist-config.json

cat dist-config.json | python3 -c "import json,sys; print(json.load(sys.stdin)['ETag'])"
```

Add the following to `DistributionConfig.DefaultCacheBehavior` (or the target `CacheBehaviors` entry) in `dist-config.json`. If your existing cache behavior already has a different `viewer-request` function attached, a single cache behavior can only have one function per event type ("Quotas on CloudFront Functions" in the [official documentation](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/cloudfront-limits.html)), so you need to merge the logic.

```json
"FunctionAssociations": {
  "Quantity": 1,
  "Items": [
    {
      "EventType": "viewer-request",
      "FunctionARN": "the CloudFrontFunctionArn value from step 3"
    }
  ]
}
```

After editing, extract just the `DistributionConfig` portion (the `get-distribution-config` response has the shape `{"ETag": ..., "DistributionConfig": {...}}`, and `update-distribution` only accepts the `DistributionConfig` part).

```bash
python3 -c "import json; d=json.load(open('dist-config.json')); json.dump(d['DistributionConfig'], open('dist-config-only.json','w'))"

aws cloudfront update-distribution \
  --id YOUR-EXISTING-DISTRIBUTION-ID \
  --distribution-config file://dist-config-only.json \
  --if-match "the ETag value from the previous step"
```

#### 5. Upload a `.htaccess` and verify the auto-trigger

```bash
aws s3 cp examples/.htaccess s3://YOUR-EXISTING-CONTENT-BUCKET/.htaccess
```

Manually invoking with `aws lambda invoke` does not verify production-equivalent behavior -- you need to confirm the S3-event-notification auto-trigger (see the real-device verification results in `README.md`). Check CloudWatch Logs to confirm Lambda ran automatically.

```bash
aws logs describe-log-streams \
  --log-group-name /aws/lambda/htaccess-bridge-fn \
  --order-by LastEventTime \
  --descending \
  --max-items 1 \
  --region us-east-1
```

If the most recent log stream was created right after your S3 upload, Lambda was triggered by the S3 event notification.

KVS updates take up to about 60 seconds to propagate to edge locations. Wait 60-90 seconds before verifying behavior (see "Notes" in `README.md`).

```bash
sleep 90
curl -I https://YOUR-EXISTING-DISTRIBUTION-DOMAIN/.htaccess
# expected: 403 Forbidden
```

### Integrating with existing notification configurations

If your existing (site content) bucket already has an S3 event notification configured (Lambda, SNS, or SQS), `s3:PutBucketNotificationConfiguration` replaces the entire existing configuration in a single call -- it does not append. Check first.

```bash
aws s3api get-bucket-notification-configuration --bucket YOUR-EXISTING-CONTENT-BUCKET
```

If the result is not empty (`{}`), deploying `infra/integration-addon.yaml`'s `BucketNotificationFunction` (custom resource Lambda) as-is will destroy the existing configuration. In that case, modify `BucketNotificationFunction`'s code (where it calls `s3.put_bucket_notification_configuration`) to first fetch the existing configuration via `s3:GetBucketNotificationConfiguration` and append to its `LambdaFunctionConfigurations` list before deploying. The current implementation assumes the bucket has no other notification configuration.

### Minimal setup (DirectoryIndex only, no redirects or Basic auth)

If you never upload any `.htaccess` file, the Lambda, S3 event notification, and artifact bucket are not required. Just add the function in CloudFront Functions and KVS (even empty) -- only the `resolveIndexDocument()` logic (which runs regardless of whether `.htaccess` exists) will be active. In this case, create a separate template that removes the `HtaccessBridgeFunction` / `ContentBucketLambdaPermission` / `BucketNotification*` resources from `infra/integration-addon.yaml`.

### Tearing down

```bash
aws cloudformation delete-stack \
  --stack-name htaccess-bridge-integration \
  --region us-east-1
```

Before deleting this stack, remove the `FunctionAssociations` entry you added in step 4 from your existing Distribution (leaving a reference to a deleted function in CloudFront Functions on your Distribution will cause confusion in future deployments or troubleshooting).
