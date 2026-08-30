# Reference / 詳細仕様

This document contains the full design, compatibility notes, operational details, and bilingual reference for the project.

この文書には、設計、互換仕様、運用上の注意、および日英の詳細資料を収録しています。

# S3 + CloudFront .htaccess Bridge

S3 + CloudFront static sites do not evaluate Apache `.htaccess` files. This project provides a reference implementation that accepts a small, safe `.htaccess` compatibility subset, validates it with AWS Lambda, publishes normalized rules to CloudFront KeyValueStore, and evaluates them with CloudFront Functions.

## Languages

- [日本語](#日本語)
- [English](#english)

## 日本語

### 概要

このリファレンス実装は、S3にアップロードされた `.htaccess` と `.htpasswd` をLambdaで検証・変換し、CloudFront KeyValueStoreに反映します。同じLambdaは、通常コンテンツに`Cache-Control`が指定されていない場合、設定済みの既定値をS3 object metadataへ非同期で追加します。CloudFront Functionsのviewer-request関数は、KVSの設定を使って以下を処理します。

```text
hidden file block
  -> Basic auth / IP bypass
  -> redirects
  -> index document routing
  -> S3 origin
```

想定する用途は、Apache から S3 + CloudFront へ移行した静的サイトで、コンテンツ制作者が従来に近い `.htaccess` ファイル操作で以下を扱えるようにすることです。

- パス単位のリダイレクト
- Basic 認証によるメンテナンスモード
- メンテナンス中の確認用 IP バイパス
- `.htaccess` に明示した場合のディレクトリアクセス時のデフォルトドキュメント配信（Apache の `DirectoryIndex` 相当。実在確認はできず常にリストの先頭を使う）

SPA（Single Page Application）のクライアントサイドルーティング用フォールバック（存在しないパスをすべて `index.html` に落とす動作。Apache の `RewriteCond %{REQUEST_FILENAME} !-f` や `FallbackResource` に相当）は対象外です。理由は「対応しない Apache 機能」節を参照してください。

### 構成

```text
S3 upload client
  |
  | upload content or update/delete .htaccess/.htpasswd
  v
S3 Event Notification
  |
  v
Lambda
  |-- .htaccess/.htpasswd -> validate -> CloudFront KeyValueStore
  '-- ordinary content without Cache-Control -> same-key metadata copy

viewer request -> CloudFront Functions (reads KeyValueStore) -> S3 origin
```

CloudFront FunctionsはリクエストごとにS3の設定ファイルを読みません。`.htaccess` または `.htpasswd` の更新時にLambdaがサイト設定を読み直し、1つの正規化済み設定としてKVSにpublishします。通常コンテンツのPut／Post／multipart upload完了時は、既存の`Cache-Control`を保持し、未指定の場合だけ同じkeyへの`CopyObject`で既定値を付けます。

### ファイル

- `lambda/htaccess_bridge.py`: S3 Event Lambda、コンテンツmetadata更新処理、`.htaccess`／`.htpasswd` パーサー
- `lambda/test_htaccess_bridge.py`: パーサーとバリデータのテスト
- `cloudfront-function/handler.js`: CloudFront Functions JavaScript runtime 2.0 サンプル
- `examples/.htaccess`: サポート対象構文のサンプル
- `examples/.htpasswd`: Basic認証情報のサンプル
- `docs/content-creator-guide.md`: コンテンツ制作者向けの短い利用ガイド
- `docs/integration-guide.md`: 既存の S3 + CloudFront 環境への組み込み手順（新規構築ではなく既存リソースに追加したい場合）
- `scripts/build-lambda.sh`: Linux x86_64 / Python 3.13 向け Lambda ZIP の再現可能なビルド
- `.github/workflows/ci.yml`: Python と CloudFront Functions の自動テスト

ローカルでのテスト:

```bash
python3 -m unittest discover -s lambda -p 'test_*.py' -v
node cloudfront-function/test_handler_logic.js
```

Lambda ZIP のビルド:

```bash
./scripts/build-lambda.sh
```

### クイックスタート

既存のS3バケット・CloudFront Distributionへの導入は[組み込みガイド](integration-guide.md)を参照してください。`infra/bridge-resources.yaml`は既存環境を変更せず、bridge専用のKVS、Lambda、CloudFront Functionを作成します。

新規構築の場合の手順:

1. `lambda/htaccess_bridge.py` を Lambda 関数としてデプロイします。
2. CloudFront KeyValueStore を作成し、ARN を `KVS_ARN` に設定します。
3. Basic 認証を使う場合は、`htpasswd -s` で `.htpasswd` を作成します。
4. S3 Event Notificationで通常コンテンツのPut／Post／multipart upload完了と、`.htaccess`／`.htpasswd` のCopy／削除イベントをLambdaに送ります。
5. 既存の index document routing 用 CloudFront Functions 関数コードに `cloudfront-function/handler.js` の処理順序を統合します。
6. 任意のS3アップロードクライアントで `examples/.htpasswd` と `examples/.htaccess` をS3バケットにアップロードします。
7. `_control-history/published/` に published JSON が作成されることを確認します。
8. CloudFront 経由で動作を確認します。

```text
/.htaccess       -> 403
/old/foo.html    -> 301 /new/foo.html
DirectoryIndex index.html がある場合: / -> /index.html origin request
maintenance ON   -> Basic auth, except allowed IPs
```

コンテンツ制作者向けの利用手順は [docs/content-creator-guide.md](content-creator-guide.md) を参照してください。

### サポートする `.htaccess` サブセット

```apache
AuthType Basic
AuthName "Maintenance"
AuthUserFile .htpasswd
Require valid-user
Require ip 203.0.113.10 198.51.100.0/24

Redirect 301 /old/ /new/
Redirect 302 /campaign-old/ /campaign/
RedirectPermanent /legacy/ /new/
RedirectTemp /tmp/ /maintenance/

RewriteEngine On
RewriteRule ^old/(.*)$ /new/$1 [R=301,L]
```

未対応ディレクティブは無視しません。検証エラーとして rejected にし、最後に成功した KVS 設定を維持します。

### 互換仕様

この実装は、メンテナンスモードとパスリダイレクトに必要な最小 subset だけを実装します。

#### 対応

| Apache directive / behavior | 対応 | 動作 |
| --- | --- | --- |
| `# comment` | 対応 | 無視 |
| 空行 | 対応 | 無視 |
| 複数 `.htaccess` | 対応 | ルートと下位の `.htaccess` を収集してフラット化 |
| ディレクトリスコープ | 対応 | `members/.htaccess` は `/members/` に適用 |
| `AuthType Basic` | 対応 | `Require valid-user` と組み合わせて Basic 認証を有効化 |
| `AuthName "..."` | 対応 | Basic 認証 realm として利用 |
| `Require valid-user` | 対応 | そのスコープを保護対象にする |
| `Require ip IPv4[/CIDR]` | 対応 | 単独指定時は一致IPだけ許可。Basic認証と併用時は一致IPが認証をバイパス |
| `Redirect 301 from to` | 対応 | prefix redirect |
| `Redirect 302 from to` | 対応 | prefix redirect |
| `Redirect 307 from to` | 対応 | prefix redirect |
| `Redirect 308 from to` | 対応 | prefix redirect |
| `RedirectPermanent from to` | 対応 | `Redirect 301` と同等 |
| `RedirectTemp from to` | 対応 | `Redirect 302` と同等 |
| `RewriteEngine On` | 限定対応 | 対応済み `RewriteRule` の前に必要 |
| `RewriteEngine Off` | 限定対応 | 許可。以後の `RewriteRule` は再度 `On` まで rejected |
| `RewriteRule pattern target [R=301,L]` | 限定対応 | redirect のみ |
| `RewriteRule pattern target [R=302,L]` | 限定対応 | redirect のみ |
| nested `RewriteRule` relative matching | 対応 | `.htaccess` が置かれたディレクトリからの相対パスで評価 |
| `DirectoryIndex local-url [local-url] ...` | 限定対応 | 複数ファイル名を優先順位付きで指定可能。ただし実在確認ができないため常にリストの最初の名前を使う（詳細は下記の注意事項を参照） |
| `DirectoryIndex disabled` | 限定対応 | そのスコープでは index 探索を行わない。親スコープの設定も適用しない |

#### 非対応

未対応機能が含まれる `.htaccess` は rejected になり、本番の KVS 設定は更新されません。

| Apache feature | 対応 | 理由 / 代替 |
| --- | --- | --- |
| `AuthType Digest` | 非対応 | nonce/challenge 検証が必要で軽量な CloudFront Functions 設計に合わない |
| `AuthUserFile .htpasswd` | 対応 | 同じS3ディレクトリの `.htpasswd` を参照 |
| `AuthDigestProvider`, `AuthDigestDomain`, `AuthDigestNonceLifetime` | 非対応 | Digest 認証は対象外 |
| `.htpasswd` `{SHA}` | 対応 | `htpasswd -s` で生成。bcrypt、Apache MD5、cryptは非対応 |
| `AuthGroupFile` | 非対応 | グループ認証は対象外 |
| `Require user ...` | 非対応 | メンテナンス用途の `Require valid-user` のみ対応 |
| `Require group ...` | 非対応 | グループ認証は対象外 |
| `Require ip` の IPv6 | 非対応 | 意図的な判断。詳細は下記「IPv6 の対応判断について」を参照 |
| `Order`, `Allow`, `Deny`, `Satisfy` | 非対応 | Apache 2.2 access-control 互換は対象外 |
| `RewriteCond` | 非対応 | Apache の実行文脈への依存が大きい。SPA フォールバック用途（`!-f`/`!-d` によるファイル存在判定）はこの実装では実現できない。詳細は「SPA フォールバックについて」を参照 |
| `RewriteBase` | 非対応 | Lambda が下位 `.htaccess` を正規化 |
| `FallbackResource` | 非対応 | `RewriteCond` と同じ理由でファイル存在判定に依存するため対応不可。「SPA フォールバックについて」を参照 |
| `R` と `L` 以外の `RewriteRule` flags | 非対応 | CloudFront Functions の処理を小さく保つ |
| `QSA`, `QSD`, `NE`, `NC`, `PT`, `END` | 非対応 | query / rewrite flag 互換は対象外 |
| `Header` | 非対応 | CloudFront Response Headers Policy を利用 |
| `ExpiresActive`, `ExpiresByType` | 非対応 | CloudFront cache policy または S3 metadata を利用 |
| `AddType`, `AddEncoding` | 非対応 | S3 object metadata または upload tooling を利用 |
| `Options` | 非対応 | Apache directory behavior は S3 には適用しない |
| `ErrorDocument` | 非対応 | CloudFront custom error responses を利用 |
| `Files`, `FilesMatch`, `Directory`, `IfModule` | 非対応 | Apache config context は対象外 |
| `SetEnv`, `SetEnvIf` | 非対応 | Apache environment variables は CloudFront には存在しない |

### IPv6 の対応判断について

Apache の `Require ip` は本来 IPv4・IPv6 の両方に対応しており（[公式ドキュメント](https://httpd.apache.org/docs/2.4/mod/mod_authz_host.html)）、IPv4 側も完全アドレス・部分アドレス（先頭 1〜3 バイトでのサブネット制限）・netmask ペア・CIDR・複数 IP の列記など複数の記法をサポートしています。

この実装では、そのうち IPv4 の CIDR 記法（`a.b.c.d/nn`、CIDR 省略時は `/32` として扱う）のみをサポートします。

- IPv4 の部分アドレス・netmask ペア等の複数記法: 対応しません。記法が増えるとパース・検証ロジックの分岐が増え、意図しない誤判定（バグ）を招くリスクが高まるため
- IPv6: 対応しません。IPv4 のビット演算ロジック（32bit 整数として扱う `ipv4ToInt`/`ipv4InCidr`）とは構造が異なり、128bit を安全に扱うための別ロジック一式が必要になります。既存の IPv4 専用ロジックと並行して保守する複雑さが、機能追加の利益に対して大きいと判断しました

IPv4/IPv6 を問わず柔軟な IP 制限が必要な場合は、CloudFront の Distribution 設定や AWS WAF の IP set を使う方法を検討してください。これらは `.htaccess` のパースとは独立した、AWS ネイティブな IP 制限の仕組みです。

### SPA フォールバックについて

SPA（React Router、Vue Router 等のクライアントサイドルーティングを使うアプリケーション）のフォールバックルーティング（存在しないパスをすべて `index.html` に落としてクライアント側にルーティングを委ねる動作）には対応しません。

Apache では `RewriteCond %{REQUEST_FILENAME} !-f` と `RewriteRule` の組み合わせ、または `FallbackResource` ディレクティブでこの動作を実現しますが、いずれもサーバー側でファイルシステムに実在するかどうかを判定する処理に依存します。この実装のアーキテクチャでは、この判定を再現できません。

`DirectoryIndex` も同様にファイル実在確認を前提とするディレクティブですが（Apache は複数の候補ファイルのうち実在する最初の1つを返す）、こちらは限定的に対応しています。候補が複数存在する状況（`RewriteCond`/`FallbackResource` が扱う「あらゆる存在しないパス」という無限の空間）と比べ、`DirectoryIndex` は「同じディレクトリ内の少数の候補ファイル名」という限定された空間であるため、実在確認をせず「常にリストの先頭を使う」という簡略化を行っても実用上の破綻が少ないという判断です。この簡略化の結果、`.htaccess` で指定した1番目の候補ファイルが実際に存在しない場合、404 になります（Apache のように2番目以降の候補へ自動フォールバックしません）。

この実装では `DirectoryIndex` は明示的なオプトインです。`.htaccess` に指定がなければ URI は書き換えられず、`DirectoryIndex disabled` を指定したスコープでは親スコープの設定も適用されません。

- Lambda（`htaccess_bridge.py`）は `.htaccess` の内容を静的に解析するだけで、コンテンツバケットのオブジェクト一覧とは無関係に動作します
- CloudFront Functions（`handler.js`）は毎リクエストで実行されますが、S3 オリジンへの事前フェッチができない軽量実行環境です（[CloudFront Functions の制約](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/cloudfront-function-restrictions.html)を参照）

CloudFront 側で SPA フォールバックを実現する一般的な方法は `CustomErrorResponses`（403/404 を 200 + `/index.html` に変換する設定）ですが、この実装では対応していません。理由は以下の通りです。

- デフォルトの OAI（Origin Access Identity）構成では `s3:ListBucket` 権限を持たないため、S3 オリジンは「本当にファイルが存在しない」場合も「アクセス権限がない」場合も同じ 403 を返します。この実装では意図的に `s3:ListBucket` を付与しない方針としているため、403 と 404 を区別できません
- `s3:ListBucket` を付与すれば 404 と 403 を区別できますが（[公式ドキュメント](https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_cloudfront_origins/README.html)参照）、バケット内のオブジェクトキー一覧が列挙可能になるリスクがあるため、この実装では採用していません
- `s3:ListBucket` を付与しない場合、403 を無条件に `index.html` に変換すると、認証・権限設定の誤りによる本来のアクセス拒否も `index.html` に隠れてしまい、障害の切り分けが困難になります

SPA を S3 + CloudFront でホストする場合は、`CustomErrorResponses` を含む別の CloudFront Distribution 構成を検討してください。この実装は Apache から移行した従来型の静的サイト（ディレクトリ構成がそのまま URL パスに対応するもの）を対象としています。

### Lambda 環境変数

- `RULES_KEY`: 監視対象の厳密な S3 key。未指定の場合、バケット内の全 `.htaccess` を対象にします。
- `RULES_SUFFIX`: `RULES_KEY` 未指定時に対象とする suffix。既定値は `.htaccess`。
- `HISTORY_PREFIX`: 履歴保存 prefix。既定値は `_control-history`。
- `KVS_ARN`: CloudFront KeyValueStore ARN。未指定の場合、サイト設定をKVSへpublishしません。通常コンテンツへの`Cache-Control`付与は引き続き実行します。
- `KVS_CONFIG_KEY`: KVS に保存する設定 key。既定値は `htaccess-config`。
- `ALLOWED_EXTERNAL_HOSTS`: 外部 redirect 先として許可する host のカンマ区切り allowlist。
- `DEFAULT_CACHE_CONTROL`: 未指定の通常コンテンツに付与する`Cache-Control`。既定値は`public, max-age=60`。

### Lambda 権限

Lambda execution role には概ね以下の権限が必要です。

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::YOUR_BUCKET"
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:GetObjectLegalHold",
        "s3:GetObjectRetention",
        "s3:GetObjectVersion",
        "s3:PutObject",
        "s3:PutObjectTagging"
      ],
      "Resource": [
        "arn:aws:s3:::YOUR_BUCKET/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": ["cloudfront-keyvaluestore:DescribeKeyValueStore", "cloudfront-keyvaluestore:UpdateKeys"],
      "Resource": "YOUR_KVS_ARN"
    }
  ]
}
```

### S3 Event

S3 Event Notificationで、通常コンテンツのPut／Post／multipart upload完了と、suffix `.htaccess`／`.htpasswd` のCopy／削除をLambdaに送ります。直接通知のLambdaはS3バケットと同じAWSリージョンに配置する必要があります。

推奨イベント:

```text
全key       -> s3:ObjectCreated:Put / Post / CompleteMultipartUpload
.htaccess  -> s3:ObjectCreated:Copy / s3:ObjectRemoved:*
.htpasswd  -> s3:ObjectCreated:Copy / s3:ObjectRemoved:*
```

全key側に`ObjectCreated:Copy`を含めないことで、metadata更新の同一key copyによる再起動loopを防ぎます。通常コンテンツをCopyで配置する仕組みは自動付与の対象外なので、copy requestに`Cache-Control`を指定してください。metadata更新自身は実際の`ObjectCreated:Copy` eventを発生させるため、別のS3通知、EventBridge、replication、監査処理では許容または除外が必要です。全keyとsuffixの`ObjectCreated:*`通知を併用すると条件が重複し、S3に拒否されます。既存通知との重複も含め、[組み込みガイド](integration-guide.md)の検査付き手順を使ってください。

`.htaccess` または `.htpasswd` が更新されるたびに、Lambdaはサイト設定を読み直してKVSにpublishします。最後の `.htaccess` が削除された場合は空設定をpublishします。Basic認証が有効なのに対応する `.htpasswd` がない場合はrejectedとなり、直前の有効設定を維持します。

通常コンテンツでは、明示済みの`Cache-Control`を保持し、未指定の場合だけ`DEFAULT_CACHE_CONTROL`を付けます。処理は非同期で、既定値`public, max-age=60`は厳密な60秒後の切り替えを保証しません。managed cache policy `CachingOptimized`のminimum／default／maximum TTLは1／86,400／31,536,000秒で、origin headerがない場合だけdefaultが使われます。`max-age=60`はその範囲内なので60秒が選ばれますが、明示した`no-cache`／`no-store`／`private`もminimum TTLにより最低1秒はCloudFrontにcacheされます。metadata更新前に保存済みのresponseは従来のTTLを維持するため、初回導入時は満了待ちまたは対象keyの一度限りのinvalidationが必要になる場合があります。

単一`CopyObject`のAPI上限は5 GiBで、Lambda timeoutは30秒のため、上限以下の大容量objectも完了を保証しません。copyはACLを`private`へリセットするためBucket owner enforced + OAC／OAIを前提とし、legacy ACL公開は対象外です。SSE-KMS customer managed keyではLambda roleへ`kms:Decrypt`と`kms:GenerateDataKey`等を別途許可してください。S3 Object Lockのretention／legal holdがHEADで確認できるオブジェクトはskipします。SSE-Cはcustomer-provided keyをcopy requestへ渡せないため対象外です。Object Annotationsはchecksumとは別機能であり、`AnnotationDirective=EXCLUDE`により保持しません。checksumはS3の通常のCopyObject動作で保持されます。バージョニング有効バケットでは完全な新規versionが追加されるため、非現行versionのストレージ料金とLifecycleを考慮してください。バージョニング停止中は現在のnull versionを置換し、version IDによる競合防止がないためETag条件によるbest-effort動作です。

Lambdaはeventのversion／ETagと最新HEADを照合し、copyにもETag条件を付けます。検出できたstale eventとHTTP 412は処理済みとして終了し、HTTP 409はLambdaの非同期retryへ渡します。ただしETagはmetadataを表さないため、同一内容でmetadataだけが異なる並行アップロードとの競合を完全には検出できません。同じkeyの並行更新は避けてください。

#### 履歴

成功履歴:

```text
_control-history/published/YYYYMMDDTHHMMSSZ-<source-hash>.json
```

失敗履歴:

```text
_control-history/rejected/YYYYMMDDTHHMMSSZ-<source-hash>.json
```

履歴は 1 つの巨大ファイルに追記せず、試行ごとに小さな JSON オブジェクトを作成します。必要に応じてS3 LifecycleとCloudWatch Logs retentionを設定してください。保持期間はバケットのバージョニング設定と監査要件に合わせ、バージョニングが有効な場合は非現行バージョンも考慮します。

例:

```text
_control-history/rejected/*   90日後に削除
_control-history/published/*  90日後に削除
Lambda log group              7日保持（テンプレートの初期値）
```

S3バージョニングが有効な場合、この例の現行オブジェクトの期限設定だけでは非現行バージョンは削除されません。非現行バージョンの保持期間も別途設定してください。

### 注意事項

- CloudFront Functions の関数を対象Cache Behaviorの `viewer-request` に関連付けてください。これにより `.htaccess`、`.htpasswd`、`_control-history` へのアクセスも403で拒否されます。
- Basic 認証を使う場合は、同じディレクトリの `.htpasswd` が必須です。未配置または不正な更新は rejected になり、直前の有効設定が維持されます。
- `Require ip` 単独指定時は、指定IP以外を403で拒否します。Basic認証と併用した場合は、指定IPが認証をバイパスします。
- KVSの反映には時間がかかる場合があります。設定更新後の確認は60〜90秒待ってください。
- 必要に応じて、履歴とLambdaログにLifecycle／retentionを設定してください。

## English

### Overview

This reference implementation validates and converts `.htaccess` and `.htpasswd` files uploaded to S3, then publishes normalized rules to CloudFront KeyValueStore. The same Lambda asynchronously adds the configured default to S3 object metadata when ordinary content has no `Cache-Control`. A viewer-request function in CloudFront Functions uses that KVS config to handle:

```text
hidden file block
  -> Basic auth / IP bypass
  -> redirects
  -> index document routing
  -> S3 origin
```

It is intended for static sites migrated from Apache to S3 + CloudFront where content creators still need a familiar `.htaccess`-style workflow for:

- path redirects
- Basic-auth maintenance mode
- IP bypass during maintenance review
- serving a default document on directory access when explicitly declared in `.htaccess` (equivalent to Apache's `DirectoryIndex`; existence is not checked so the first name is always used)

SPA (Single Page Application) client-side routing fallback (rewriting every non-existent path to `index.html` so the client-side router can handle it) is out of scope. See "About SPA fallback" for details.

### Architecture

```text
S3 upload client
  |
  | upload content or update/delete .htaccess/.htpasswd
  v
S3 Event Notification
  |
  v
Lambda
  |-- .htaccess/.htpasswd -> validate -> CloudFront KeyValueStore
  '-- ordinary content without Cache-Control -> same-key metadata copy

viewer request -> CloudFront Functions (reads KeyValueStore) -> S3 origin
```

CloudFront Functions does not read configuration files from S3 on each request. When either `.htaccess` or `.htpasswd` changes, Lambda reloads the site configuration and publishes one normalized config to KVS. For ordinary content Put, Post, and completed multipart uploads, it preserves an existing `Cache-Control` value and adds the default only when the value is absent by using `CopyObject` to the same key.

### Quick Start

Local tests:

```bash
python3 -m unittest discover -s lambda -p 'test_*.py' -v
node cloudfront-function/test_handler_logic.js
```

Build the Lambda deployment archive:

```bash
./scripts/build-lambda.sh
```

Follow the [integration guide](integration-guide.md) for an existing S3 bucket and CloudFront Distribution. `infra/bridge-resources.yaml` creates only the bridge KVS, Lambda, and CloudFront Function without modifying the existing environment.

Steps for building from scratch:

1. Deploy `lambda/htaccess_bridge.py` as a Lambda function.
2. Create a CloudFront KeyValueStore and set its ARN as `KVS_ARN`.
3. For Basic auth, create `.htpasswd` with `htpasswd -s`.
4. Configure S3 Event Notification for ordinary content Put/Post/completed multipart uploads and `.htaccess`/`.htpasswd` Copy/removal events.
5. Merge `cloudfront-function/handler.js` into the existing viewer-request CloudFront Functions code that performs index document routing.
6. Upload `examples/.htpasswd` and `examples/.htaccess` to the S3 bucket with any S3 upload client.
7. Confirm that Lambda writes a published JSON under `_control-history/published/`.
8. Confirm CloudFront behavior:

```text
/.htaccess       -> 403
/old/foo.html    -> 301 /new/foo.html
With DirectoryIndex index.html: / -> /index.html origin request
maintenance ON   -> Basic auth, except allowed IPs
```

For content creators, see [docs/content-creator-guide.md](content-creator-guide.md).

### Supported `.htaccess` Subset

```apache
AuthType Basic
AuthName "Maintenance"
Require valid-user
Require ip 203.0.113.10 198.51.100.0/24

Redirect 301 /old/ /new/
Redirect 302 /campaign-old/ /campaign/
RedirectPermanent /legacy/ /new/
RedirectTemp /tmp/ /maintenance/

RewriteEngine On
RewriteRule ^old/(.*)$ /new/$1 [R=301,L]
```

Unsupported directives fail validation. They are not ignored.

### Compatibility Specification

This bridge intentionally implements only the subset needed for maintenance mode and path redirects.

#### Supported

| Apache directive / behavior | Support | Behavior |
| --- | --- | --- |
| `# comment` | Yes | Ignored |
| blank line | Yes | Ignored |
| multiple `.htaccess` files | Yes | Root and nested `.htaccess` files are collected and flattened |
| directory scope | Yes | `members/.htaccess` applies to `/members/` |
| `AuthType Basic` | Yes | Enables Basic-auth maintenance when paired with `Require valid-user` |
| `AuthName "..."` | Yes | Used as Basic auth realm |
| `Require valid-user` | Yes | Marks that scope as protected |
| `Require ip IPv4[/CIDR]` | Yes | Allows only matching IPs when used alone; bypasses Basic auth for matching IPs when combined with Basic auth |
| `Redirect 301 from to` | Yes | Prefix redirect |
| `Redirect 302 from to` | Yes | Prefix redirect |
| `Redirect 307 from to` | Yes | Prefix redirect |
| `Redirect 308 from to` | Yes | Prefix redirect |
| `RedirectPermanent from to` | Yes | Equivalent to `Redirect 301` |
| `RedirectTemp from to` | Yes | Equivalent to `Redirect 302` |
| `RewriteEngine On` | Limited | Required before supported `RewriteRule` redirects |
| `RewriteEngine Off` | Limited | Accepted; following `RewriteRule` is rejected unless `On` appears again |
| `RewriteRule pattern target [R=301,L]` | Limited | Redirect only |
| `RewriteRule pattern target [R=302,L]` | Limited | Redirect only |
| nested `RewriteRule` relative matching | Yes | Pattern is evaluated relative to the `.htaccess` directory |
| `DirectoryIndex local-url [local-url] ...` | Limited | Multiple candidate filenames can be specified in priority order. Existence cannot be checked, so the first name in the list is always used (see the note below) |
| `DirectoryIndex disabled` | Limited | Disables index lookup in that scope and prevents an inherited DirectoryIndex scope from applying |

#### Not Supported

Unsupported directives reject the upload and keep the last published KVS config.

| Apache feature | Support | Reason / alternative |
| --- | --- | --- |
| `AuthType Digest` | No | Digest nonce/challenge validation is too stateful for this lightweight CloudFront Functions design |
| `AuthUserFile .htpasswd` | Yes | Reads `.htpasswd` from the same S3 directory |
| `AuthDigestProvider`, `AuthDigestDomain`, `AuthDigestNonceLifetime` | No | Digest auth is not supported |
| `.htpasswd` `{SHA}` | Yes | Generate with `htpasswd -s`; bcrypt, Apache MD5, and crypt are unsupported |
| `AuthGroupFile` | No | Group auth is out of scope |
| `Require user ...` | No | Only maintenance-style `Require valid-user` is supported |
| `Require group ...` | No | Group auth is out of scope |
| IPv6 in `Require ip` | No | Deliberate design decision. See "About the IPv6 decision" below for details |
| `Order`, `Allow`, `Deny`, `Satisfy` | No | Apache 2.2 access-control compatibility is out of scope |
| `RewriteCond` | No | Conditions are too Apache-context dependent. SPA fallback use cases (file-existence checks with `!-f`/`!-d`) cannot be implemented this way. See "About SPA fallback" |
| `RewriteBase` | No | Nested rules are normalized by Lambda instead |
| `FallbackResource` | No | Same limitation as `RewriteCond` — depends on file-existence checks that this implementation cannot perform. See "About SPA fallback" |
| internal `RewriteRule` without `R` | No | Only redirects are supported |
| `RewriteRule` flags other than `R` and `L` | No | Keep CloudFront Functions runtime logic small and predictable |
| `QSA`, `QSD`, `NE`, `NC`, `PT`, `END` | No | Query/string and rewrite flag compatibility is out of scope |
| `Header` | No | Use CloudFront Response Headers Policy |
| `ExpiresActive`, `ExpiresByType` | No | Use CloudFront cache policy or S3 metadata |
| `AddType`, `AddEncoding` | No | Use S3 object metadata / upload tooling |
| `Options` | No | Apache directory behavior does not apply to S3 |
| `ErrorDocument` | No | Use CloudFront custom error responses |
| `Files`, `FilesMatch`, `Directory`, `IfModule` | No | Apache config contexts are out of scope |
| `SetEnv`, `SetEnvIf` | No | Apache environment variables do not exist at CloudFront |

### About the IPv6 decision

Apache's `Require ip` natively supports both IPv4 and IPv6 (see the [official documentation](https://httpd.apache.org/docs/2.4/mod/mod_authz_host.html)). On the IPv4 side, it also supports multiple notations: full addresses, partial addresses (subnet restriction by the first 1-3 bytes), network/netmask pairs, CIDR, and space-separated lists of multiple IPs.

This implementation supports only IPv4 CIDR notation (`a.b.c.d/nn`; omitting the CIDR suffix defaults to `/32`).

- Additional IPv4 notations (partial addresses, netmask pairs, etc.): not supported. Adding more notations increases the number of parsing/validation branches, which raises the risk of subtle misjudgment bugs.
- IPv6: not supported. IPv4 handling here uses 32-bit integer bitwise operations (`ipv4ToInt`/`ipv4InCidr`). IPv6's 128-bit addresses require a structurally different implementation to handle safely, and maintaining that alongside the existing IPv4-only logic was judged to add more maintenance complexity than the feature is worth.

If you need flexible IPv4/IPv6 IP restrictions, consider using CloudFront distribution settings or an AWS WAF IP set instead. These provide AWS-native IP restriction independent of `.htaccess` parsing.

### About SPA fallback

SPA client-side routing fallback (rewriting every non-existent path to `index.html` so the client-side router, such as React Router or Vue Router, can handle it) is not supported.

Apache implements this with `RewriteCond %{REQUEST_FILENAME} !-f` combined with `RewriteRule`, or with the `FallbackResource` directive. Both depend on the server checking whether a path exists on the filesystem. This implementation's architecture cannot reproduce that check.

`DirectoryIndex` has a similar existence-check dependency (Apache serves the first candidate file that actually exists), but is supported in a limited form. Unlike `RewriteCond`/`FallbackResource`, which need to handle "any non-existent path" (an unbounded space), `DirectoryIndex` only deals with a small, fixed set of candidate filenames in the same directory. Skipping the existence check and always using the first candidate is a simplification that stays practical in that narrower scope. As a result, if the first candidate filename doesn't actually exist, the request returns 404 (there is no automatic fallback to the next candidate, unlike Apache).

In this implementation, `DirectoryIndex` is an explicit opt-in. If it is absent from `.htaccess`, the URI is left unchanged. A scope with `DirectoryIndex disabled` also prevents an inherited DirectoryIndex scope from applying.

- Lambda (`htaccess_bridge.py`) only statically parses `.htaccess` content; it has no visibility into the content bucket's object listing
- CloudFront Functions (`handler.js`) run on every request but cannot pre-fetch the S3 origin (see [CloudFront Functions restrictions](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/cloudfront-function-restrictions.html))

The common way to implement SPA fallback on CloudFront is `CustomErrorResponses` (converting 403/404 to 200 + `/index.html`), which this implementation does not configure. Reasons:

- With the default OAI (Origin Access Identity) setup, which does not grant `s3:ListBucket`, the S3 origin returns the same 403 for both "the file genuinely doesn't exist" and "access denied" cases. This implementation intentionally does not grant `s3:ListBucket`, so 403 and 404 cannot be distinguished
- Granting `s3:ListBucket` would let S3 distinguish 404 from 403 (see [official documentation](https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_cloudfront_origins/README.html)), but it also makes the bucket's object key listing enumerable, a risk this implementation avoids
- Without `s3:ListBucket`, unconditionally converting 403 to `index.html` would mask genuine access-denial issues (misconfigured permissions, etc.) behind a 200 response, making troubleshooting harder

If you need to host a SPA on S3 + CloudFront, consider a separate CloudFront distribution configuration that includes `CustomErrorResponses`. This implementation targets traditional static sites migrated from Apache, where the directory structure maps directly to URL paths.

### Lambda Environment Variables

- `RULES_KEY`: Optional exact S3 key to watch. If omitted, every `.htaccess` in the bucket is considered.
- `RULES_SUFFIX`: S3 key suffix to watch when `RULES_KEY` is omitted. Default: `.htaccess`.
- `HISTORY_PREFIX`: History prefix. Default: `_control-history`.
- `KVS_ARN`: CloudFront KeyValueStore ARN. If omitted, site configuration is not published to KVS; automatic `Cache-Control` processing for ordinary content still runs.
- `KVS_CONFIG_KEY`: KVS key for published config. Default: `htaccess-config`.
- `ALLOWED_EXTERNAL_HOSTS`: comma-separated allowlist for external redirect targets.
- `DEFAULT_CACHE_CONTROL`: `Cache-Control` added to ordinary content when absent. Default: `public, max-age=60`.

### Lambda Permissions

The Lambda execution role needs permissions equivalent to:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::YOUR_BUCKET"
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:GetObjectLegalHold",
        "s3:GetObjectRetention",
        "s3:GetObjectVersion",
        "s3:PutObject",
        "s3:PutObjectTagging"
      ],
      "Resource": [
        "arn:aws:s3:::YOUR_BUCKET/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": ["cloudfront-keyvaluestore:DescribeKeyValueStore", "cloudfront-keyvaluestore:UpdateKeys"],
      "Resource": "YOUR_KVS_ARN"
    }
  ]
}
```

### S3 Event

Configure S3 Event Notification for ordinary content Put/Post/completed multipart uploads, plus Copy/removal events with `.htaccess` and `.htpasswd` suffixes. A directly notified Lambda function must be in the same AWS Region as the S3 bucket.

Recommended events:

```text
all keys   -> s3:ObjectCreated:Put / Post / CompleteMultipartUpload
.htaccess  -> s3:ObjectCreated:Copy / s3:ObjectRemoved:*
.htpasswd  -> s3:ObjectCreated:Copy / s3:ObjectRemoved:*
```

Excluding `ObjectCreated:Copy` from the all-key rule prevents the same-key metadata copy from invoking Lambda in a loop. A deployment that copies ordinary content must supply `Cache-Control` in its copy request. The metadata update itself still emits a real `ObjectCreated:Copy` event, so other S3 notifications, EventBridge rules, replication, and audit consumers must tolerate or exclude it. Do not combine all-key and suffix `ObjectCreated:*` notifications because S3 rejects overlapping rules. Use the overlap-checking procedure in the [integration guide](integration-guide.md), including for existing notifications.

When either `.htaccess` or `.htpasswd` changes, Lambda reloads the site configuration and publishes it to KVS. Deleting the last `.htaccess` publishes an empty config. If Basic auth is enabled without a matching `.htpasswd`, the update is rejected and the last valid config remains active.

For ordinary content, Lambda preserves an explicit `Cache-Control` value and adds `DEFAULT_CACHE_CONTROL` only when absent. Processing is asynchronous, and the default `public, max-age=60` does not promise a changeover exactly 60 seconds later. The `CachingOptimized` managed policy has minimum/default/maximum TTL values of 1/86,400/31,536,000 seconds; its default is used only when the origin sends no cache header. `max-age=60` is within the bounds, while explicit `no-cache`, `no-store`, or `private` values are still cached by CloudFront for the one-second minimum. A response cached before the metadata update keeps its prior TTL, so initial rollout can require waiting for expiry or a one-time invalidation of affected keys.

A single `CopyObject` has a 5 GiB API limit, and the Lambda timeout is 30 seconds, so completion is not guaranteed for large objects below that limit either. Copy resets the ACL to `private`, so Bucket owner enforced with OAC/OAI is assumed and legacy ACL-based publication is unsupported. SSE-KMS customer managed keys require separate `kms:Decrypt`, `kms:GenerateDataKey`, and applicable key-policy grants for Lambda. Objects whose Object Lock retention/legal-hold settings are visible in HEAD are skipped. SSE-C is unsupported because its customer-provided key cannot be supplied by this copy request. Object Annotations are distinct from checksums and are not preserved because the request uses `AnnotationDirective=EXCLUDE`; checksums are preserved by normal S3 CopyObject behavior. In a versioned bucket, this creates another full object version, so account for noncurrent-version storage and Lifecycle. With Versioning suspended, the current null version is replaced and concurrency protection is best-effort through ETag conditions because no version-ID guard is available.

Lambda compares the event version/ETag with the latest HEAD and conditions the copy on the ETag. Detectably stale events and HTTP 412 responses finish successfully; HTTP 409 responses are raised for Lambda's asynchronous retry. Because an ETag does not represent metadata, this cannot always detect a concurrent upload with identical content but different metadata. Avoid concurrent writes to the same key.

#### History

Successful publishes are written under:

```text
_control-history/published/YYYYMMDDTHHMMSSZ-<source-hash>.json
```

Rejected uploads are written under:

```text
_control-history/rejected/YYYYMMDDTHHMMSSZ-<source-hash>.json
```

History is append-only by object, not by appending to one large file. Configure S3 Lifecycle rules and CloudWatch Logs retention if needed. Choose retention based on the bucket's versioning state and audit requirements; for a versioned bucket, account for noncurrent versions as well.

Example:

```text
_control-history/rejected/*   expire after 90 days
_control-history/published/*  expire after 90 days
Lambda log group              retain for 7 days (template default)
```

For a versioned bucket, expiring current objects as shown above does not remove noncurrent versions. Configure their retention separately.

### Notes

- Associate the function in CloudFront Functions with the target cache behavior's `viewer-request` event. It also returns 403 for `.htaccess`, `.htpasswd`, and `_control-history` URLs.
- Basic auth requires `.htpasswd` in the same directory. Missing or invalid credentials are rejected and the last valid configuration remains active.
- When used alone, `Require ip` denies non-matching IPs with 403. When combined with Basic auth, matching IPs bypass authentication.
- KVS propagation may take time. Wait 60–90 seconds after a configuration update before testing.
- Configure S3 Lifecycle rules for history objects and Lambda log retention if needed.
