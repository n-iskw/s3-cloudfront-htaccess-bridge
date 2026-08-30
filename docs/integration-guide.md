# Integration Guide / 既存環境への導入

## Languages

- [日本語](#日本語)
- [English](#english)

## 日本語

既存のS3バケットとCloudFront Distributionを前提とします。`infra/bridge-resources.yaml`は次だけを作成し、既存のS3通知設定とDistributionは変更しません。

- CloudFront KeyValueStore
- 設定同期とコンテンツmetadata更新を行うLambda、実行ロール、ログ
- CloudFront Functionsのviewer-request関数
- 既存S3からLambdaを呼び出す権限

S3からLambdaへの直接通知は同一リージョン内でのみ設定できます。この単一スタックは、既存コンテンツバケットと同じリージョンにデプロイしてください。CloudFront KeyValueStoreとCloudFront Functionsはグローバルサービスですが、スタック内のLambdaはデプロイ先リージョンに作成されます。以下では、既存バケットのリージョンを`AWS_REGION`へ設定します。

### 1. リソースを作成

CloudFront Functionは、読みやすい `cloudfront-function/handler.js` をminifyしてからテンプレートへ埋め込みます。初回または `handler.js` 更新時に生成してください。10 KBを超える場合はこのコマンドが失敗します。

```bash
npm ci
npm run build:cloudfront-function
```

```bash
AWS_REGION=YOUR-CONTENT-BUCKET-REGION

aws cloudformation deploy \
  --stack-name htaccess-bridge \
  --template-file infra/bridge-resources.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "$AWS_REGION" \
  --parameter-overrides \
    ContentBucketName=YOUR-CONTENT-BUCKET \
    DefaultCacheControl='public, max-age=60'
```

`DefaultCacheControl`は、アップロード時に`Cache-Control`が指定されていない通常コンテンツへ追加する既定値です。明示済みの値は上書きしません。

### 2. Lambdaコードを配置

テンプレート内のLambdaはプレースホルダーです。KeyValueStore APIのSigV4A認証に必要な`botocore[crt]`を含むため、ビルド後のZIPは約21MBになります。実コードの配置が完了するまで設定ファイルをアップロードしないでください。

```bash
./scripts/build-lambda.sh

FUNCTION_NAME=$(aws cloudformation describe-stacks \
  --stack-name htaccess-bridge \
  --region "$AWS_REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`LambdaFunctionName`].OutputValue' \
  --output text)

aws lambda update-function-code \
  --function-name "$FUNCTION_NAME" \
  --zip-file fileb://htaccess_bridge.zip \
  --region "$AWS_REGION"
```

作成されたリソースはOutputsで確認できます。

```bash
aws cloudformation describe-stacks \
  --stack-name htaccess-bridge \
  --region "$AWS_REGION" \
  --query 'Stacks[0].Outputs'
```

### 3. AWS CLIで既存環境へ接続

まずスタックOutputsの`LambdaFunctionArn`と`CloudFrontFunctionArn`を確認し、値を設定します。

```bash
LAMBDA_ARN=YOUR_LAMBDA_FUNCTION_ARN
FUNCTION_ARN=YOUR_CLOUDFRONT_FUNCTION_ARN
```

S3通知の現在値を取得し、既存項目を残したまま次の5項目を`LambdaFunctionConfigurations`へ追加します。`put-bucket-notification-configuration`は設定全体を置換するため、空の設定から作り直さないでください。

```text
全key       -> s3:ObjectCreated:Put / Post / CompleteMultipartUpload
.htaccess  -> s3:ObjectCreated:Copy / s3:ObjectRemoved:*
.htpasswd  -> s3:ObjectCreated:Copy / s3:ObjectRemoved:*
```

通常コンテンツのmetadata更新は同じkeyへの`CopyObject`で行われます。そのCopyが再びLambdaを起動し続けないよう、全key側には`ObjectCreated:Copy`を含めません。`.htaccess`／`.htpasswd`のCopyだけを別のsuffix通知で扱います。全keyの`ObjectCreated:*`とsuffix付き`ObjectCreated:*`を併用するとS3の通知条件が重複するため、この構成へ置き換えてください。既存の別通知にも同じevent typeで重複するprefix／suffixがある場合は、適用前に1つの通知先へ集約するかSNS/SQS/EventBridgeによるfan-outを設計してください。

```bash
aws s3api get-bucket-notification-configuration \
  --bucket YOUR-CONTENT-BUCKET > notification.json

LAMBDA_ARN="$LAMBDA_ARN" python3 - <<'PY'
import json, os
p = "notification.json"
d = json.load(open(p))
items = d.setdefault("LambdaFunctionConfigurations", [])
managed_ids = {"htaccess-bridge-content-created"}
for suffix in (".htaccess", ".htpasswd"):
    managed_ids.update({
        f"htaccess-bridge-{suffix[1:]}-created",
        f"htaccess-bridge-{suffix[1:]}-copy",
        f"htaccess-bridge-{suffix[1:]}-removed",
    })
items[:] = [item for item in items if item.get("Id") not in managed_ids]
planned = [{
    "Id": "htaccess-bridge-content-created",
    "LambdaFunctionArn": os.environ["LAMBDA_ARN"],
    "Events": [
        "s3:ObjectCreated:Put",
        "s3:ObjectCreated:Post",
        "s3:ObjectCreated:CompleteMultipartUpload",
    ],
}]
for suffix in (".htaccess", ".htpasswd"):
    rules = {"Key": {"FilterRules": [{"Name": "suffix", "Value": suffix}]}}
    planned.extend([
        {
            "Id": f"htaccess-bridge-{suffix[1:]}-copy",
            "LambdaFunctionArn": os.environ["LAMBDA_ARN"],
            "Events": ["s3:ObjectCreated:Copy"],
            "Filter": rules,
        },
        {
            "Id": f"htaccess-bridge-{suffix[1:]}-removed",
            "LambdaFunctionArn": os.environ["LAMBDA_ARN"],
            "Events": ["s3:ObjectRemoved:*"],
            "Filter": rules,
        },
    ])

def event_overlaps(left, right):
    left = left.split(":")
    right = right.split(":")
    return (
        len(left) == 3
        and len(right) == 3
        and left[:2] == right[:2]
        and (left[2] == "*" or right[2] == "*" or left[2] == right[2])
    )

def key_filter(item):
    values = {"prefix": "", "suffix": ""}
    rules = item.get("Filter", {}).get("Key", {}).get("FilterRules", [])
    for rule in rules:
        name = rule.get("Name", "").lower()
        if name in values:
            values[name] = rule.get("Value", "")
    return values["prefix"], values["suffix"]

def filter_overlaps(left, right):
    left_prefix, left_suffix = key_filter(left)
    right_prefix, right_suffix = key_filter(right)
    return (
        (left_prefix.startswith(right_prefix) or right_prefix.startswith(left_prefix))
        and (left_suffix.endswith(right_suffix) or right_suffix.endswith(left_suffix))
    )

conflicts = []
for section in ("LambdaFunctionConfigurations", "QueueConfigurations", "TopicConfigurations"):
    for existing in d.get(section, []):
        for candidate in planned:
            if filter_overlaps(existing, candidate) and any(
                event_overlaps(old, new)
                for old in existing.get("Events", [])
                for new in candidate["Events"]
            ):
                conflicts.append(f"{section}:{existing.get('Id', '<no-id>')}")
if conflicts:
    raise SystemExit(
        "overlapping S3 notifications: " + ", ".join(sorted(set(conflicts)))
        + "; consolidate routing or use SNS/SQS/EventBridge fan-out"
    )

items.extend(planned)
json.dump(d, open(p, "w"), indent=2)
PY

aws s3api put-bucket-notification-configuration \
  --bucket YOUR-CONTENT-BUCKET \
  --notification-configuration file://notification.json
```

通常コンテンツのアップロード通知を受けると、Lambdaは最新version／ETagを確認し、`Cache-Control`が未指定の場合だけ同じkeyへcopyして`DefaultCacheControl`を付けます。S3通知は非同期なので、アップロード完了からmetadata更新完了まで短い時間差があります。通常コンテンツの`ObjectCreated:Copy`は自己copyの再起動を避けるため意図的に自動付与の対象外です。Copyベースのデプロイではcopy時に`Cache-Control`を指定してください。このmetadata更新自体は実際の`ObjectCreated:Copy` eventを生成するため、別のS3通知、EventBridge、replication、監査処理ではbridgeによるcopyを許容または除外してください。更新済みmetadataは次で確認できます。

```bash
aws s3api head-object \
  --bucket YOUR-CONTENT-BUCKET \
  --key path/to/file.html \
  --query CacheControl
```

CloudFrontのmanaged cache policy `CachingOptimized`は、minimum TTL 1秒、default TTL 86,400秒、maximum TTL 31,536,000秒です。default TTLはoriginが`Cache-Control`／`Expires`を返さない場合だけ使われ、originの`max-age=60`はminimum／maximumの範囲内なので60秒が選ばれます。明示値を保持する場合もpolicyの範囲が優先されるため、`no-cache`／`no-store`／`private`でも最低1秒はCloudFrontにcacheされます。ただし、これは「アップロードから厳密に60秒後に必ず新しい内容へ切り替わる」という保証ではありません。Lambdaの非同期処理時間も影響し、metadata更新前に既に保存されたレスポンスは、その時点で選ばれたTTLを維持します。初回導入時は既存TTLの満了を待つか、必要なkeyだけ一度invalidateしてください。導入前から存在するオブジェクトには自動適用されないため、必要なら再アップロードまたは別途一括metadata更新を行ってください。詳細は[AWSのmanaged cache policy仕様](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/using-managed-cache-policies.html)を参照してください。

運用上の制約は次の通りです。

- アップロード時に明示された`Cache-Control`は保持します。
- eventのversion／ETagと最新HEADを照合し、copyにもETag条件を付けます。検出できたstale eventとHTTP 412は処理済みとして終了し、retry可能なHTTP 409はLambdaの非同期retryへ渡します。ただしETagはmetadataを表しません。同一内容でmetadataだけが異なる並行アップロードとの競合を完全には検出できないため、同じkeyを並行更新しない運用を推奨します。
- 単一`CopyObject`を使うため、5 GiBはAPI上の上限で、それを超えるオブジェクトは対象外です。5 GiB以下でもLambdaのtimeoutは30秒なので、大きなオブジェクトのcopy完了は保証しません。大容量ファイルはアップロード時に`Cache-Control`を付けてください。
- 同じkeyへのcopyではACLが`private`へリセットされます。S3 Object OwnershipのBucket owner enforcedとCloudFront OAC／OAIを前提とします。オブジェクトACLによる公開を維持する構成では、そのACLを安全に復元する処理を追加するまで有効化しないでください。
- customer managed keyによるSSE-KMSオブジェクトでは、対象KMS keyに対する`kms:Decrypt`と`kms:GenerateDataKey`をLambda roleへ別途許可してください。汎用テンプレートはkey ARNを特定できないため、この権限を含めません。
- S3 Object Lockのretention／legal holdがHEADで確認できるオブジェクトは自動的にskipします。SSE-Cはcustomer-provided encryption keyをcopy requestへ渡せないため対象外です。いずれもアップロード時に`Cache-Control`を指定してください。
- S3 Object Annotationsは`AnnotationDirective=EXCLUDE`で除外するため、このmetadata更新では保持しません。Object Annotationsはchecksumとは別機能で、checksumはS3の通常のCopyObject動作で保持されます。Object Annotationsが必須のオブジェクトでは利用しないでください。
- バージョニング有効バケットでは、同じkeyへのcopyが完全な新規versionを1つ作成し、非現行version分のストレージ料金も発生します。更新頻度と復旧要件に合わせてnoncurrent versionのLifecycleを設定してください。バージョニング停止中は現在のnull versionを置換し、version IDによる競合防止がないためETag条件によるbest-effort動作になります。

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
  --region "$AWS_REGION"
```

## English

This project targets an existing S3 bucket and CloudFront Distribution. `infra/bridge-resources.yaml` creates only:

- CloudFront KeyValueStore
- Lambda for config sync and content metadata updates, its execution role, and logs
- A viewer-request function in CloudFront Functions
- Permission for the existing S3 bucket to invoke Lambda

It does not modify the existing bucket notification configuration or Distribution.

Direct S3-to-Lambda notifications require the bucket and function to be in the same Region. Deploy this single stack in the existing content bucket's Region. CloudFront KeyValueStore and CloudFront Functions are global services, while the Lambda in this stack is created in the stack Region. The commands below use `AWS_REGION` for the existing bucket's Region.

### 1. Create bridge resources

The template embeds a minified version of the readable `cloudfront-function/handler.js`. Run this before the first deployment and whenever `handler.js` changes. The command fails if the generated source reaches the 10 KB limit.

```bash
npm ci
npm run build:cloudfront-function
```

```bash
AWS_REGION=YOUR-CONTENT-BUCKET-REGION

aws cloudformation deploy \
  --stack-name htaccess-bridge \
  --template-file infra/bridge-resources.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "$AWS_REGION" \
  --parameter-overrides \
    ContentBucketName=YOUR-CONTENT-BUCKET \
    DefaultCacheControl='public, max-age=60'
```

`DefaultCacheControl` is added to ordinary uploaded content that has no `Cache-Control` metadata. An explicitly supplied value is preserved.

### 2. Deploy the Lambda code

The template initially creates placeholder Lambda code. The deployment ZIP is approximately 21 MB because it includes `botocore[crt]` for SigV4A access to the KeyValueStore API. Do not upload configuration files until the real code is deployed.

```bash
./scripts/build-lambda.sh

FUNCTION_NAME=$(aws cloudformation describe-stacks \
  --stack-name htaccess-bridge \
  --region "$AWS_REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`LambdaFunctionName`].OutputValue' \
  --output text)

aws lambda update-function-code \
  --function-name "$FUNCTION_NAME" \
  --zip-file fileb://htaccess_bridge.zip \
  --region "$AWS_REGION"
```

Inspect the created resource identifiers in the stack outputs:

```bash
aws cloudformation describe-stacks \
  --stack-name htaccess-bridge \
  --region "$AWS_REGION" \
  --query 'Stacks[0].Outputs'
```

### 3. Connect the existing environment with AWS CLI

Copy `LambdaFunctionArn` and `CloudFrontFunctionArn` from the stack outputs:

```bash
LAMBDA_ARN=YOUR_LAMBDA_FUNCTION_ARN
FUNCTION_ARN=YOUR_CLOUDFRONT_FUNCTION_ARN
```

Retrieve the current S3 notification configuration and merge the following five entries into `LambdaFunctionConfigurations`. `put-bucket-notification-configuration` replaces the complete configuration, so preserve every existing entry.

```text
all keys   -> s3:ObjectCreated:Put / Post / CompleteMultipartUpload
.htaccess  -> s3:ObjectCreated:Copy / s3:ObjectRemoved:*
.htpasswd  -> s3:ObjectCreated:Copy / s3:ObjectRemoved:*
```

Content metadata is updated with `CopyObject` to the same key. The all-key notification deliberately excludes `ObjectCreated:Copy`, preventing that copy from repeatedly invoking Lambda. Copy events for `.htaccess` and `.htpasswd` use separate suffix notifications. Do not combine an all-key `ObjectCreated:*` rule with suffix `ObjectCreated:*` rules because S3 treats those filters as overlapping. If another existing notification has an overlapping prefix or suffix for the same event types, consolidate the routing or design fan-out through SNS, SQS, or EventBridge before applying this configuration.

```bash
aws s3api get-bucket-notification-configuration \
  --bucket YOUR-CONTENT-BUCKET > notification.json

LAMBDA_ARN="$LAMBDA_ARN" python3 - <<'PY'
import json, os
p = "notification.json"
d = json.load(open(p))
items = d.setdefault("LambdaFunctionConfigurations", [])
managed_ids = {"htaccess-bridge-content-created"}
for suffix in (".htaccess", ".htpasswd"):
    managed_ids.update({
        f"htaccess-bridge-{suffix[1:]}-created",
        f"htaccess-bridge-{suffix[1:]}-copy",
        f"htaccess-bridge-{suffix[1:]}-removed",
    })
items[:] = [item for item in items if item.get("Id") not in managed_ids]
planned = [{
    "Id": "htaccess-bridge-content-created",
    "LambdaFunctionArn": os.environ["LAMBDA_ARN"],
    "Events": [
        "s3:ObjectCreated:Put",
        "s3:ObjectCreated:Post",
        "s3:ObjectCreated:CompleteMultipartUpload",
    ],
}]
for suffix in (".htaccess", ".htpasswd"):
    rules = {"Key": {"FilterRules": [{"Name": "suffix", "Value": suffix}]}}
    planned.extend([
        {
            "Id": f"htaccess-bridge-{suffix[1:]}-copy",
            "LambdaFunctionArn": os.environ["LAMBDA_ARN"],
            "Events": ["s3:ObjectCreated:Copy"],
            "Filter": rules,
        },
        {
            "Id": f"htaccess-bridge-{suffix[1:]}-removed",
            "LambdaFunctionArn": os.environ["LAMBDA_ARN"],
            "Events": ["s3:ObjectRemoved:*"],
            "Filter": rules,
        },
    ])

def event_overlaps(left, right):
    left = left.split(":")
    right = right.split(":")
    return (
        len(left) == 3
        and len(right) == 3
        and left[:2] == right[:2]
        and (left[2] == "*" or right[2] == "*" or left[2] == right[2])
    )

def key_filter(item):
    values = {"prefix": "", "suffix": ""}
    rules = item.get("Filter", {}).get("Key", {}).get("FilterRules", [])
    for rule in rules:
        name = rule.get("Name", "").lower()
        if name in values:
            values[name] = rule.get("Value", "")
    return values["prefix"], values["suffix"]

def filter_overlaps(left, right):
    left_prefix, left_suffix = key_filter(left)
    right_prefix, right_suffix = key_filter(right)
    return (
        (left_prefix.startswith(right_prefix) or right_prefix.startswith(left_prefix))
        and (left_suffix.endswith(right_suffix) or right_suffix.endswith(left_suffix))
    )

conflicts = []
for section in ("LambdaFunctionConfigurations", "QueueConfigurations", "TopicConfigurations"):
    for existing in d.get(section, []):
        for candidate in planned:
            if filter_overlaps(existing, candidate) and any(
                event_overlaps(old, new)
                for old in existing.get("Events", [])
                for new in candidate["Events"]
            ):
                conflicts.append(f"{section}:{existing.get('Id', '<no-id>')}")
if conflicts:
    raise SystemExit(
        "overlapping S3 notifications: " + ", ".join(sorted(set(conflicts)))
        + "; consolidate routing or use SNS/SQS/EventBridge fan-out"
    )

items.extend(planned)
json.dump(d, open(p, "w"), indent=2)
PY

aws s3api put-bucket-notification-configuration \
  --bucket YOUR-CONTENT-BUCKET \
  --notification-configuration file://notification.json
```

On an ordinary content upload, Lambda verifies the latest version and ETag. If `Cache-Control` is absent, it copies the object to the same key with `DefaultCacheControl`. S3 invokes Lambda asynchronously, so there is a short interval between upload completion and metadata update completion. `ObjectCreated:Copy` for ordinary content is deliberately outside automatic normalization to prevent the same-key copy from invoking itself; copy-based deployment tools must set `Cache-Control` on their copy request. The metadata update itself still emits a real `ObjectCreated:Copy` event, so other S3 notifications, EventBridge rules, replication, and audit consumers must tolerate or exclude the bridge copy. Verify the resulting metadata with:

```bash
aws s3api head-object \
  --bucket YOUR-CONTENT-BUCKET \
  --key path/to/file.html \
  --query CacheControl
```

The CloudFront managed cache policy `CachingOptimized` has a 1-second minimum TTL, an 86,400-second default TTL, and a 31,536,000-second maximum TTL. The default applies only when the origin sends neither `Cache-Control` nor `Expires`; origin `max-age=60` falls within those bounds, so CloudFront selects 60 seconds. Policy bounds still apply to preserved explicit values, so even `no-cache`, `no-store`, or `private` is cached by CloudFront for at least one second. This still does not guarantee that new content becomes visible exactly 60 seconds after upload. Lambda runs asynchronously, and a response cached before the metadata update keeps the TTL selected when it was cached. On initial rollout, wait for the previous TTL or invalidate only the affected keys once. Existing objects do not receive the value until they are re-uploaded or handled by a separate metadata backfill. See the [AWS managed cache policy reference](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/using-managed-cache-policies.html).

Operational constraints:

- An uploader-supplied `Cache-Control` value is preserved.
- The event version/ETag is compared with the latest HEAD and the copy has an ETag condition. Detectably stale events and HTTP 412 responses finish successfully; retryable HTTP 409 responses are raised for Lambda's asynchronous retry. An ETag does not represent metadata, so a concurrent upload of identical content with different metadata cannot always be detected. Avoid concurrent writes to the same key.
- A single `CopyObject` has a 5 GiB API limit, so larger objects are unsupported. The Lambda timeout remains 30 seconds, so completion is not guaranteed for large objects even below that API limit. Set `Cache-Control` during upload for large files.
- Copying to the same key resets the ACL to `private`. The supported deployment assumes S3 Object Ownership set to Bucket owner enforced and CloudFront OAC/OAI access. Do not enable this workflow for legacy object-ACL publication until it is adapted to restore the intended ACL safely.
- Objects encrypted with an SSE-KMS customer managed key require `kms:Decrypt` and `kms:GenerateDataKey` on that key for the Lambda role. The generic template cannot identify the key ARN and does not grant those permissions.
- Objects with S3 Object Lock retention/legal-hold settings visible in HEAD are skipped automatically. SSE-C is unsupported because the copy request cannot provide the customer-provided encryption key. Set `Cache-Control` during upload for both cases.
- S3 Object Annotations are deliberately excluded with `AnnotationDirective=EXCLUDE` and are not preserved by this metadata update. Object Annotations are distinct from checksums; checksums are preserved by the normal S3 CopyObject behavior. Do not use this workflow for objects that require Object Annotations.
- In a versioned bucket, copying to the same key creates one additional full object version and incurs storage for the noncurrent version. Configure a noncurrent-version Lifecycle rule that matches the update rate and recovery requirements. With Versioning suspended, the copy replaces the current null version and concurrency protection is best-effort through ETag conditions because there is no version-ID guard.

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
  --region "$AWS_REGION"
```
