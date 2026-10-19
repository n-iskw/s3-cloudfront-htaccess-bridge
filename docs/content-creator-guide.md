# Content Creator Guide / コンテンツ制作者向けガイド

## Languages

- [日本語](#日本語)
- [English](#english)

## 日本語

### 概要

通常の S3 アップロード手段でサイトコンテンツをアップロードしてください。S3 クライアントは限定しません。AWS Console、AWS CLI、Cyberduck、CMS のエクスポート機能、その他のアップロードツールを利用できます。

リダイレクトやメンテナンスモードを変更する場合は、対象ディレクトリに `.htaccess` をアップロードします。

### コンテンツのCache-Control

通常コンテンツを新しくアップロードし、アップロード時に`Cache-Control`を指定していない場合、システムが管理者設定の既定値（初期値は`public, max-age=60`）を非同期で追加します。アップロードツールから明示した`Cache-Control`はそのまま保持されます。S3内のCopy／renameで配置するツールは自動付与の対象外なので、copy時に`Cache-Control`を指定してください。

付与結果はS3 Consoleのmetadata表示、または次のコマンドで確認できます。

```bash
aws s3api head-object \
  --bucket YOUR-CONTENT-BUCKET \
  --key path/to/file.html \
  --query CacheControl
```

この処理はS3通知を受けてから実行されるため、アップロード直後はmetadata更新前の場合があります。また、`max-age=60`はキャッシュ鮮度を指定する値であり、アップロードから厳密に60秒後の切り替えを保証するものではありません。表示確認までの時間にはCloudFront側の設定や更新前から残っているキャッシュも影響します。

### メンテナンスモード

Basic 認証を有効にする例:

```apache
AuthType Basic
AuthName "Maintenance"
AuthUserFile .htpasswd
Require valid-user
```

同じディレクトリに置く `.htpasswd` を作成してアップロードします。対応するのは `{SHA}` 形式です。

```bash
htpasswd -cs .htpasswd preview
```

既存ファイルへユーザーを追加・更新する場合は `-c` を外します。

```bash
htpasswd -s .htpasswd preview
```

`.htaccess` と `.htpasswd` はどちらを先にアップロードしても構いません。認証を有効にした `.htaccess` だけが存在する間は設定が rejected になりますが、`.htpasswd` が揃った時点で自動的に反映されます。bcrypt、Apache MD5、crypt形式は非対応です。

特定の確認用 IP アドレスだけ Basic 認証を通過させる例:

```apache
Require ip 203.0.113.10 198.51.100.0/24
```

メンテナンスモードを無効にするには、`.htaccess` から Basic 認証の行を削除します。redirect ルールも不要であれば `.htaccess` 自体を削除できます。

### リダイレクト

恒久リダイレクト:

```apache
Redirect 301 /old/ /new/
```

一時リダイレクト:

```apache
Redirect 302 /campaign-old/ /campaign/
```

`Redirect` は prefix match です。自分自身や自分の子パスへ redirect しないでください。

悪い例:

```apache
Redirect 302 /campaign /campaign/
```

この設定は loop する可能性があるため rejected になります。

### Refererによる個別エラーページへの転送

ルートの `.htaccess` で、`/secret.html` を特定ページからの遷移に限定する例です。
`/access-error.html` も通常のコンテンツとしてアップロードしてください。

```apache
RewriteEngine On
RewriteCond %{HTTP_REFERER} !^https://example\.com/entrance\.html(\?.*)?$
RewriteRule ^secret\.html$ /access-error.html [R=302,L]
```

`/entrance.html`（クエリ付きも可）からの遷移だけ通し、Refererなし・別ページからの
アクセスは302転送します。他のページには影響しません。URLが転送先へ変わり、
エラーページ自体は通常200で配信されます。S3の403カスタムエラーは経由しません。
Refererがないアクセスだけを転送したい場合は、条件を `RewriteCond %{HTTP_REFERER} ^$`
に置き換えます。下位 `.htaccess` の `RewriteRule` は配置ディレクトリからの相対パスです。

条件の先頭の `!` は否定、条件行末の `[NC]` は大文字小文字を区別しない指定です。
複数の条件行はANDで結合し、直後の1つの `RewriteRule` だけに適用します。
条件付き転送は `Cache-Control: no-store` を返します。転送先を条件の対象に含めないでください。
判定対象はDirectoryIndex書き換え前のURLなので、同じページへの別URLも必要に応じてルールに含めます。
Refererは省略・偽装できるため、認証や機密情報の保護には使えません。

### DirectoryIndex（ディレクトリのデフォルトドキュメント）

ディレクトリへのアクセス時にどのファイルを表示するかを指定できます。

```apache
DirectoryIndex index.html index.php
```

複数指定した場合は、リストの先頭から順に候補として扱われます。ただしこの実装では実際にファイルが存在するかどうかを確認できないため、**常にリストの先頭のファイル名が使われます**。1番目の候補ファイルが実際に存在しない場合、404 になります（Apache のように2番目以降の候補へ自動フォールバックしません）。

`DirectoryIndex` を設定していない場合、DirectoryIndex による書き換えは行われません。`index.html` を使う場合も、次のように `.htaccess` に明示的に記述してください。

`DirectoryIndex disabled` を明示した場合は、そのディレクトリでは index 探索を行いません。

### 下位ディレクトリへの継承

ルート（サイト直下）の `.htaccess` に設定した `DirectoryIndex`（および Basic 認証・リダイレクト）は、下位ディレクトリすべてに自動的に継承されます。下位ディレクトリごとに同じ `.htaccess` を再配置する必要はありません。

```text
/.htaccess                  <- DirectoryIndex index.html index.php
  /about/                   <- ルートの設定がそのまま適用される
  /members/portal/          <- 深い階層でも同様に継承される
```

下位ディレクトリだけ異なる設定にしたい場合は、そのディレクトリに個別の `.htaccess` を追加します。最も具体的な（浅い階層から見て一番深い）ディレクトリの設定が優先されます。

```text
/.htaccess                  <- DirectoryIndex index.html
/members/.htaccess           <- DirectoryIndex portal.html index.html
  /members/                 <- portal.html が優先される
  /members/profile/         <- 同じスコープ内なので portal.html が優先される
  /about/                   <- ルートの設定（index.html）が使われる
```

### 下位ディレクトリのルール

下位ディレクトリにも `.htaccess` を置けます。

```text
members/.htaccess
```

`members/.htaccess` のルールは `/members/` 配下に適用されます。

### アップロード後の動作

`.htaccess` をアップロードすると、システムが自動で検証します。

- 有効な場合: CloudFront に publish されます。
- 無効な場合: 公開サイトは最後に成功した設定のままです。
- rejected 理由: `_control-history/rejected/` に保存されます。

アップロード後にサイトの動作が変わらない場合は、rejected 履歴または Lambda logs を確認してください。

## English

### Overview

Upload site content to S3 with your usual S3 upload method. The client is not fixed. You can use AWS Console, AWS CLI, Cyberduck, a CMS export tool, or another upload tool.

To change redirects or maintenance mode, upload a `.htaccess` file to the target directory.

### Content Cache-Control

When ordinary content is newly uploaded without `Cache-Control`, the system asynchronously adds the administrator-configured default (`public, max-age=60` initially). A value supplied by the upload tool is preserved. Tools that publish through an S3 Copy/rename are outside automatic normalization and must set `Cache-Control` on the copy request.

Check the result in the S3 Console metadata view or with:

```bash
aws s3api head-object \
  --bucket YOUR-CONTENT-BUCKET \
  --key path/to/file.html \
  --query CacheControl
```

The metadata update runs after an S3 notification, so it might not be complete immediately after upload. `max-age=60` controls freshness; it does not guarantee a changeover exactly 60 seconds after upload. CloudFront configuration and a response cached before the metadata update also affect when new content is observed.

### Maintenance Mode

Enable Basic authentication:

```apache
AuthType Basic
AuthName "Maintenance"
AuthUserFile .htpasswd
Require valid-user
```

Create and upload `.htpasswd` in the same directory. Only the `{SHA}` format is supported:

```bash
htpasswd -cs .htpasswd preview
```

Remove `-c` when adding or updating a user in an existing file. Upload order does not matter; the configuration is published when both files are present. bcrypt, Apache MD5, and crypt hashes are not supported.

Allow specific review IP addresses to bypass Basic authentication:

```apache
Require ip 203.0.113.10 198.51.100.0/24
```

To disable maintenance mode, remove the Basic authentication lines from `.htaccess`. If no redirect rules are needed, you can delete the `.htaccess` file.

### Redirects

Permanent redirect:

```apache
Redirect 301 /old/ /new/
```

Temporary redirect:

```apache
Redirect 302 /campaign-old/ /campaign/
```

`Redirect` uses prefix matching. Do not redirect a path to itself or to its own child path.

Bad example:

```apache
Redirect 302 /campaign /campaign/
```

This can loop and will be rejected.

### Referer-conditioned redirects to an individual error page

In the root `.htaccess`, allow `/secret.html` only when reached from a specific page:

```apache
RewriteEngine On
RewriteCond %{HTTP_REFERER} !^https://example\.com/entrance\.html(\?.*)?$
RewriteRule ^secret\.html$ /access-error.html [R=302,L]
```

Upload `/access-error.html` as ordinary content. The allowed entrance page (including
query strings) passes through; missing Referer or any other page causes a 302 redirect.
Other paths are unaffected. The browser URL changes and the destination normally returns
200; S3's custom 403 error mapping is not involved. To redirect only requests without a
Referer, replace the condition with `RewriteCond %{HTTP_REFERER} ^$`.
In nested `.htaccess` files, `RewriteRule` matches relative to that directory.

`!` negates a condition and optional `[NC]` makes it case-insensitive. Consecutive
conditions are ANDed and apply only to the immediately following `RewriteRule`.
Conditional redirects return `Cache-Control: no-store`. Keep the error destination
outside the matched paths. Matching precedes DirectoryIndex rewriting: include other
URLs that lead to the same content in your rules when necessary.
Referer can be omitted or forged; this is navigation control, not authentication.

### DirectoryIndex (default document for a directory)

You can specify which file to serve when a directory is requested.

```apache
DirectoryIndex index.html index.php
```

When multiple names are given, they are treated as a priority list starting from the first one. However, this implementation cannot check whether a file actually exists, so **the first name in the list is always used**. If the first candidate file doesn't actually exist, the request returns 404 (there is no automatic fallback to the next candidate, unlike Apache).

If `DirectoryIndex` is not set, no DirectoryIndex rewrite is applied. To use `index.html`, declare it explicitly in `.htaccess`:

```apache
DirectoryIndex index.html
```

When `DirectoryIndex disabled` is declared, index lookup is disabled for that directory.

### Inheritance into Subdirectories

`DirectoryIndex` (as well as Basic auth and redirects) set in the root (top-level) `.htaccess` is automatically inherited by all subdirectories. You don't need to place the same `.htaccess` in every subdirectory.

```text
/.htaccess                  <- DirectoryIndex index.html index.php
  /about/                   <- inherits the root setting as-is
  /members/portal/          <- inherited even at deeper levels
```

If you want a subdirectory to use a different setting, add a separate `.htaccess` in that subdirectory. The most specific (deepest) matching directory's setting takes priority.

```text
/.htaccess                  <- DirectoryIndex index.html
/members/.htaccess           <- DirectoryIndex portal.html index.html
  /members/                 <- portal.html takes priority
  /members/profile/         <- still within the same scope, portal.html takes priority
  /about/                   <- root setting (index.html) applies
```

### Subdirectory Rules

You can place `.htaccess` in a subdirectory.

```text
members/.htaccess
```

Rules in `members/.htaccess` apply to `/members/` and below.

### What Happens After Upload

After uploading `.htaccess`, the system validates it automatically.

- If valid, it is published to CloudFront.
- If invalid, the live site keeps the last valid settings.
- The rejected reason is saved under `_control-history/rejected/`.

If the site behavior does not change after upload, check the rejected history or Lambda logs.
