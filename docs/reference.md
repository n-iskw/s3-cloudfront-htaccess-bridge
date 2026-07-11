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

このリファレンス実装は、S3 にアップロードされた `.htaccess` を Lambda で検証・変換し、CloudFront KeyValueStore に反映します。CloudFront Functions の viewer request 関数コードは、KVS の設定を使って以下を処理します。

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
- ディレクトリアクセス時のデフォルトドキュメント配信（Apache の `DirectoryIndex` 相当。`.htaccess` でファイル名の優先順位リストを指定できるが、実在確認はできず常にリストの先頭を使う）

SPA（Single Page Application）のクライアントサイドルーティング用フォールバック（存在しないパスをすべて `index.html` に落とす動作。Apache の `RewriteCond %{REQUEST_FILENAME} !-f` や `FallbackResource` に相当）は対象外です。理由は「対応しない Apache 機能」節を参照してください。

### 構成

```text
S3 upload client
  |
  | upload / update / delete .htaccess
  v
S3 Event Notification
  |
  v
Lambda
  |
  | parse and validate all .htaccess files
  v
CloudFront KeyValueStore
  |
  v
CloudFront Functions
  |
  v
S3 origin
```

CloudFront Functions はリクエストごとに S3 の `.htaccess` を読みません。`.htaccess` の作成・更新・削除時に Lambda が全 `.htaccess` を読み直し、1 つの正規化済み設定として KVS に publish します。

### ファイル

- `lambda/htaccess_bridge.py`: S3 Event Lambda と `.htaccess` パーサー
- `lambda/test_htaccess_bridge.py`: パーサーとバリデータのテスト
- `cloudfront-function/handler.js`: CloudFront Functions JavaScript runtime 2.0 サンプル
- `examples/.htaccess`: サポート対象構文のサンプル
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

`infra/standalone.yaml` は新規構築用テンプレートです。既存の S3 バケット・CloudFront Distribution に組み込む場合は [組み込みガイド](integration-guide.md) を参照してください。Lambda ZIP はS3アーティファクトバケットを使わず、スタック作成後に直接アップロードします。

新規構築の場合の手順:

1. `lambda/htaccess_bridge.py` を Lambda 関数としてデプロイします。
2. CloudFront KeyValueStore を作成し、ARN を `KVS_ARN` に設定します。
3. Basic 認証の認証情報を Secrets Manager に保存し、`BASIC_AUTH_SECRET_ID` に設定します。
4. S3 Event Notification で `.htaccess` の作成・更新・削除イベントを Lambda に送ります。
5. 既存の index document routing 用 CloudFront Functions 関数コードに `cloudfront-function/handler.js` の処理順序を統合します。
6. 任意の S3 アップロードクライアントで `examples/.htaccess` を S3 バケットにアップロードします。
7. `_control-history/published/` に published JSON が作成されることを確認します。
8. CloudFront 経由で動作を確認します。

```text
/.htaccess       -> 403
/old/foo.html    -> 301 /new/foo.html
/                -> /index.html origin request
maintenance ON   -> Basic auth, except allowed IPs
```

コンテンツ制作者向けの利用手順は [docs/content-creator-guide.md](content-creator-guide.md) を参照してください。

### サポートする `.htaccess` サブセット

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
| `Require ip IPv4[/CIDR]` | 限定対応 | 同じメンテナンススコープで Basic 認証をバイパス |
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
| `DirectoryIndex disabled` | 限定対応 | この実装では index 探索の完全な無効化はできない。`.htaccess` に `DirectoryIndex disabled` を設定した場合、そのスコープではデフォルトの `index.html` にフォールバックする（Apache 本来の「一覧表示または 404」とは異なる） |

#### 非対応

未対応機能が含まれる `.htaccess` は rejected になり、本番の KVS 設定は更新されません。

| Apache feature | 対応 | 理由 / 代替 |
| --- | --- | --- |
| `AuthType Digest` | 非対応 | nonce/challenge 検証が必要で軽量な CloudFront Functions 設計に合わない |
| `AuthUserFile` | 非対応 | 認証情報は Secrets Manager で管理 |
| `AuthDigestProvider`, `AuthDigestDomain`, `AuthDigestNonceLifetime` | 非対応 | Digest 認証は対象外 |
| `.htpasswd` 解析 | 非対応 | コンテンツバケットに秘密情報を置かない |
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
- `KVS_ARN`: CloudFront KeyValueStore ARN。未指定の場合、Lambda は検証と履歴保存だけを行います。
- `KVS_CONFIG_KEY`: KVS に保存する設定 key。既定値は `htaccess-config`。
- `ALLOWED_EXTERNAL_HOSTS`: 外部 redirect 先として許可する host のカンマ区切り allowlist。
- `BASIC_AUTH_SECRET_ID`: Basic 認証情報を保存した Secrets Manager secret ID。

`BASIC_AUTH_SECRET_ID` は次のどちらかの JSON を参照できます。

```json
{"authorization":"Basic dXNlcjpwYXNz"}
```

または:

```json
{"username":"user","password":"pass"}
```

生成される KVS 設定には、期待する `Authorization` ヘッダー値だけを保存します。平文パスワードは保存しません。

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
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": [
        "arn:aws:s3:::YOUR_BUCKET/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": ["cloudfront-keyvaluestore:DescribeKeyValueStore", "cloudfront-keyvaluestore:UpdateKeys"],
      "Resource": "YOUR_KVS_ARN"
    },
    {
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "YOUR_BASIC_AUTH_SECRET_ARN"
    }
  ]
}
```

メンテナンスモードを使わない場合、`BASIC_AUTH_SECRET_ID` と Secrets Manager 権限は省略できます。

### S3 Event

S3 Event Notification で、suffix `.htaccess` の `ObjectCreated` と `ObjectRemoved` を Lambda に送ります。通常コンテンツすべてで Lambda を起動しないでください。

推奨イベント:

```text
s3:ObjectCreated:Put
s3:ObjectCreated:Post
s3:ObjectCreated:Copy
s3:ObjectCreated:CompleteMultipartUpload
s3:ObjectRemoved:Delete
s3:ObjectRemoved:DeleteMarkerCreated
```

`.htaccess` が作成・更新・コピー・multipart upload・削除されるたびに、Lambda はバケット内に残っている全 `.htaccess` を読み直し、1 つの正規化済み設定を KVS に publish します。最後の `.htaccess` が削除された場合は空設定を publish し、`.htaccess` 由来の認証と redirect を無効化します。

成功履歴:

```text
_control-history/published/YYYYMMDDTHHMMSSZ-<source-hash>.json
```

失敗履歴:

```text
_control-history/rejected/YYYYMMDDTHHMMSSZ-<source-hash>.json
```

履歴は 1 つの巨大ファイルに追記せず、試行ごとに小さな JSON オブジェクトを作成します。S3 Lifecycle と CloudWatch Logs retention を必ず設定してください。

例:

```text
_control-history/rejected/*   90日で削除
_control-history/published/*  1年保持、または90日後に Glacier へ移行
Lambda log group              30日または90日保持
```

### 注意事項

- CloudFront Functions の関数を対象Cache Behaviorの `viewer-request` に関連付けてください。これにより `.htaccess`、`.htpasswd`、`_control-history` へのアクセスも403で拒否されます。
- Basic 認証を使う場合は `BASIC_AUTH_SECRET_ID` が必須です。未設定の更新は rejected になり、直前の有効設定が維持されます。
- `Require ip` は Basic 認証のバイパス専用で、汎用アクセス制御ではありません。
- KVS の反映には時間がかかる場合があります。`.htaccess` 更新後の確認は 60〜90 秒待ってください。
- 履歴と Lambda ログには Lifecycle／retention を設定してください。

## English

### Overview

This reference implementation validates and converts `.htaccess` files uploaded to S3 with Lambda, then publishes normalized rules to CloudFront KeyValueStore. CloudFront Functions viewer-request code uses that KVS config to handle:

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
- serving a default document on directory access (equivalent to Apache's `DirectoryIndex`; `.htaccess` can specify a priority list of filenames, but existence is not checked so the first name is always used)

SPA (Single Page Application) client-side routing fallback (rewriting every non-existent path to `index.html` so the client-side router can handle it) is out of scope. See "About SPA fallback" for details.

### Architecture

```text
S3 upload client
  |
  | upload / update / delete .htaccess
  v
S3 Event Notification
  |
  v
Lambda
  |
  | parse and validate all .htaccess files
  v
CloudFront KeyValueStore
  |
  v
CloudFront Functions
  |
  v
S3 origin
```

CloudFront Functions does not read `.htaccess` from S3 on each request. Whenever a `.htaccess` file is created, updated, or deleted, Lambda reloads all `.htaccess` files and publishes one flattened config to KVS.

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

`infra/standalone.yaml` creates a new environment. For an existing S3 bucket and CloudFront Distribution, follow the [integration guide](integration-guide.md). The Lambda ZIP is uploaded directly after stack creation, without an S3 artifact bucket.

Steps for building from scratch:

1. Deploy `lambda/htaccess_bridge.py` as a Lambda function.
2. Create a CloudFront KeyValueStore and set its ARN as `KVS_ARN`.
3. Store the Basic auth credential in Secrets Manager and set `BASIC_AUTH_SECRET_ID`.
4. Configure S3 Event Notification for `.htaccess` create/update/delete events.
5. Merge `cloudfront-function/handler.js` into the existing viewer-request CloudFront Functions code that performs index document routing.
6. Upload `examples/.htaccess` to the S3 bucket with any S3 upload client.
7. Confirm that Lambda writes a published JSON under `_control-history/published/`.
8. Confirm CloudFront behavior:

```text
/.htaccess       -> 403
/old/foo.html    -> 301 /new/foo.html
/                -> /index.html origin request
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
| `Require ip IPv4[/CIDR]` | Limited | Bypasses Basic auth for matching viewer IPs in the same maintenance scope |
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
| `DirectoryIndex disabled` | Limited | This implementation cannot fully disable index lookup. With `DirectoryIndex disabled`, the scope falls back to the default `index.html` (different from Apache's "listing or 404" behavior) |

#### Not Supported

Unsupported directives reject the upload and keep the last published KVS config.

| Apache feature | Support | Reason / alternative |
| --- | --- | --- |
| `AuthType Digest` | No | Digest nonce/challenge validation is too stateful for this lightweight CloudFront Functions design |
| `AuthUserFile` | No | Credentials are managed in Secrets Manager |
| `AuthDigestProvider`, `AuthDigestDomain`, `AuthDigestNonceLifetime` | No | Digest auth is not supported |
| `.htpasswd` parsing | No | Do not store secrets in the content bucket |
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
- `KVS_ARN`: CloudFront KeyValueStore ARN. If omitted, Lambda only validates and writes history.
- `KVS_CONFIG_KEY`: KVS key for published config. Default: `htaccess-config`.
- `ALLOWED_EXTERNAL_HOSTS`: comma-separated allowlist for external redirect targets.
- `BASIC_AUTH_SECRET_ID`: optional Secrets Manager secret for Basic auth credential.

`BASIC_AUTH_SECRET_ID` can point to either:

```json
{"authorization":"Basic dXNlcjpwYXNz"}
```

or:

```json
{"username":"user","password":"pass"}
```

The generated KVS config stores only the expected `Authorization` header value, not the plain password.

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
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": [
        "arn:aws:s3:::YOUR_BUCKET/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": ["cloudfront-keyvaluestore:DescribeKeyValueStore", "cloudfront-keyvaluestore:UpdateKeys"],
      "Resource": "YOUR_KVS_ARN"
    },
    {
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "YOUR_BASIC_AUTH_SECRET_ARN"
    }
  ]
}
```

If maintenance mode is never used, `BASIC_AUTH_SECRET_ID` and the Secrets Manager permission can be omitted.

### S3 Event

Configure S3 Event Notification for `ObjectCreated` and `ObjectRemoved` with suffix `.htaccess`. Do not trigger this Lambda for every content upload.

Recommended events:

```text
s3:ObjectCreated:Put
s3:ObjectCreated:Post
s3:ObjectCreated:Copy
s3:ObjectCreated:CompleteMultipartUpload
s3:ObjectRemoved:Delete
s3:ObjectRemoved:DeleteMarkerCreated
```

When any `.htaccess` is uploaded, overwritten, copied, multipart-uploaded, or deleted, Lambda lists all remaining `.htaccess` files and publishes one flattened KVS config. If the last `.htaccess` is deleted, Lambda publishes an empty config, disabling `.htaccess`-driven auth and redirects.

Successful publishes are written under:

```text
_control-history/published/YYYYMMDDTHHMMSSZ-<source-hash>.json
```

Rejected uploads are written under:

```text
_control-history/rejected/YYYYMMDDTHHMMSSZ-<source-hash>.json
```

History is append-only by object, not by appending to one large file. Configure S3 Lifecycle rules and CloudWatch Logs retention.

### Notes

- Associate the function in CloudFront Functions with the target cache behavior's `viewer-request` event. It also returns 403 for `.htaccess`, `.htpasswd`, and `_control-history` URLs.
- `BASIC_AUTH_SECRET_ID` is required when Basic auth is enabled. Invalid updates are rejected and the last valid configuration remains active.
- `Require ip` only bypasses Basic auth; it is not general-purpose access control.
- KVS propagation may take time. Wait 60–90 seconds before testing an `.htaccess` update.
- Configure S3 Lifecycle rules for history objects and log retention for Lambda.
