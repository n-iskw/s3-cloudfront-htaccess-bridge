# S3 + CloudFront .htaccess Bridge

Use a small, safe subset of Apache `.htaccess` with static websites hosted on Amazon S3 and CloudFront.

Upload `.htaccess` files alongside your content. A Lambda function validates and converts them, publishes the normalized rules to CloudFront KeyValueStore, and a viewer-request function in CloudFront Functions applies them at the edge.

Amazon S3 + CloudFront で配信する静的サイトで、Apache `.htaccess` の安全な一部を利用できるようにする仕組みです。コンテンツと一緒に `.htaccess` をS3へアップロードすると、Lambdaが検証・変換してCloudFront KeyValueStoreへ反映し、CloudFront Functions の関数がエッジで適用します。

パスリダイレクト、Basic認証によるメンテナンスモード、ディレクトリ単位の設定、限定的な `DirectoryIndex` に対応します。Apacheとの完全互換ではありません。新規環境は[構築手順](docs/standalone-guide.md)、既存環境への追加は[組み込みガイド](docs/integration-guide.md)を参照してください。

## What it supports

- Path redirects: `Redirect` and redirect-only `RewriteRule`
- Basic-auth maintenance mode, optionally bypassed by IPv4/CIDR
- Per-directory rules through nested `.htaccess` files
- A limited `DirectoryIndex`
- Safe rejection of unsupported directives and redirect loops
- Automatic blocking of `.htaccess`, `.htpasswd`, and `_control-history` URLs

It intentionally does not emulate all of Apache. Features such as `RewriteCond`, SPA fallback, `.htpasswd` parsing, Digest authentication, and IPv6 access rules are out of scope.

## How it works

```text
.htaccess uploaded to S3
        ↓ S3 event
Lambda validates all .htaccess files
        ↓
CloudFront KeyValueStore
        ↓ viewer-request
CloudFront Functions → S3 origin
```

Invalid updates are recorded under `_control-history/rejected/` and do not replace the last valid configuration.

## Get started

Choose the guide for your environment:

- New S3 + CloudFront environment: follow the [standalone setup guide](docs/standalone-guide.md).
- Existing S3 + CloudFront environment: follow the [integration guide](docs/integration-guide.md).
- Content editors: see the [content creator guide](docs/content-creator-guide.md), available in Japanese and English.

Build the Lambda deployment archive:

```sh
./scripts/build-lambda.sh
```

Run the tests:

```sh
python3 -m unittest discover -s lambda -p 'test_*.py' -v
node cloudfront-function/test_handler_logic.js
```

## Important limitation

`DirectoryIndex index.html index.htm` always selects the first name without checking whether the object exists. This differs from Apache. See the [full reference](docs/reference.md) before using the project in production.

## Documentation

- [Integration guide / 既存環境への組み込み](docs/integration-guide.md)
- [Standalone setup / 新規環境の構築](docs/standalone-guide.md)
- [Content creator guide / コンテンツ制作者向けガイド](docs/content-creator-guide.md)
- [Full reference / 詳細仕様](docs/reference.md)
- [Contributing](CONTRIBUTING.md)

## License

Copyright 2026 Naoya Ishikawa. Licensed under the [Apache License 2.0](LICENSE).
