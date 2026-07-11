# Content Creator Guide / コンテンツ制作者向けガイド

## Languages

- [日本語](#日本語)
- [English](#english)

## 日本語

### 概要

通常の S3 アップロード手段でサイトコンテンツをアップロードしてください。S3 クライアントは限定しません。AWS Console、AWS CLI、Cyberduck、CMS のエクスポート機能、その他のアップロードツールを利用できます。

リダイレクトやメンテナンスモードを変更する場合は、対象ディレクトリに `.htaccess` をアップロードします。

### メンテナンスモード

Basic 認証を有効にする例:

```apache
AuthType Basic
AuthName "Maintenance"
Require valid-user
```

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

### DirectoryIndex（ディレクトリのデフォルトドキュメント）

ディレクトリへのアクセス時にどのファイルを表示するかを指定できます。

```apache
DirectoryIndex index.html index.php
```

複数指定した場合は、リストの先頭から順に候補として扱われます。ただしこの実装では実際にファイルが存在するかどうかを確認できないため、**常にリストの先頭のファイル名が使われます**。1番目の候補ファイルが実際に存在しない場合、404 になります（Apache のように2番目以降の候補へ自動フォールバックしません）。

`DirectoryIndex` を設定していない場合は `index.html` が使われます。

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

### Maintenance Mode

Enable Basic authentication:

```apache
AuthType Basic
AuthName "Maintenance"
Require valid-user
```

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

### DirectoryIndex (default document for a directory)

You can specify which file to serve when a directory is requested.

```apache
DirectoryIndex index.html index.php
```

When multiple names are given, they are treated as a priority list starting from the first one. However, this implementation cannot check whether a file actually exists, so **the first name in the list is always used**. If the first candidate file doesn't actually exist, the request returns 404 (there is no automatic fallback to the next candidate, unlike Apache).

If `DirectoryIndex` is not set, `index.html` is used.

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
