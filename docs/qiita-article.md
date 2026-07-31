# S3 + CloudFrontで.htaccessを使えるようにした

S3 + CloudFrontで静的サイトを配信する場合、リダイレクトやBasic認証はCloudFront Functionsに書くことが多いと思います。

技術的にはそれで困りません。ただ、VPSやレンタルサーバーから移行すると運用が変わります。

以前はデザイナーがHTMLと一緒に`.htaccess`をアップロードしていたのに、移行後はリダイレクトを1件追加するだけでもエンジニアがCloudFront Functionsを修正することになります。

そこを変えたくなかったので、S3へアップロードした`.htaccess`をCloudFront Functionsの設定として使う仕組みを作りました。

[n-iskw/s3-cloudfront-htaccess-bridge](https://github.com/n-iskw/s3-cloudfront-htaccess-bridge)

Apache互換サーバーを作るものではありません。リダイレクト、Basic認証、`DirectoryIndex`など、静的サイトの運用で使いそうな範囲だけに対応しています。

## コンテンツ側の操作

導入後の操作は、S3へコンテンツと`.htaccess`をアップロードするだけです。AWS ConsoleでもCyberduckでも構いません。

リダイレクトは普通の`.htaccess`と同じように書けます。

```apache
Redirect 301 /old/ /new/
Redirect 302 /campaign-old/ /campaign/
```

Basic認証は次のように設定します。

```apache
AuthType Basic
AuthName "Maintenance"
AuthUserFile .htpasswd
Require valid-user
```

同じディレクトリへ`.htpasswd`もアップロードします。現在対応しているのは`{SHA}`形式です。

```bash
htpasswd -cs .htpasswd preview
```

確認用のIPアドレスを認証対象から外すこともできます。

```apache
Require ip 203.0.113.10 198.51.100.0/24
```

下位ディレクトリに`.htaccess`を置いた場合は、そのディレクトリ以下に適用します。ルートの設定は下位へ継承します。

コンテンツを管理する人はCloudFront FunctionsやKeyValueStoreを触りません。ここがこの仕組みの目的です。

## 構成

`.htaccess`をリクエストのたびにS3から読むことはできないため、更新時にCloudFront KeyValueStoreへ変換しておきます。

```text
S3へ.htaccessまたは.htpasswdをアップロード
  ↓ S3イベント
Lambdaで全設定を読み直して検証
  ↓
CloudFront KeyValueStoreへ保存
  ↓ viewer-request
CloudFront Functionsで適用
```

S3上のファイルが正本です。Lambdaは`.htaccess`を解析し、CloudFront Functionsから扱いやすい形へ変換してKeyValueStoreへ保存します。

CloudFront Functionsでは次の順に処理します。

1. `.htaccess`や`.htpasswd`など、公開してはいけないパスを拒否
2. Basic認証
3. リダイレクト
4. `DirectoryIndex`の解決

## 設定を間違えた場合

未対応のディレクティブやリダイレクトループが含まれていた場合は反映しません。それまで使われていた設定を維持します。

結果はS3へ保存します。

```text
_control-history/published/
_control-history/rejected/
```

`.htaccess`、`.htpasswd`、`_control-history/`へのHTTPアクセスはCloudFront Functions側で403にしています。

自由に書ける`.htaccess`をそのまま実行するのではなく、対応している記述だけを検証して反映する作りです。

## 対応していないもの

Apacheの`.htaccess`を完全に再現しているわけではありません。現在の主な非対応項目は次のとおりです。

- `RewriteCond`
- SPAフォールバック
- Digest認証
- IPv6のアクセスルール
- bcrypt、Apache MD5、crypt形式の`.htpasswd`

`DirectoryIndex`にも制限があります。

```apache
DirectoryIndex index.html index.htm
```

このように複数書いても、常に先頭の`index.html`を使います。CloudFront FunctionsからS3上のファイルの有無を確認しないため、`index.html`がなければ`index.htm`には進まず404になります。

未対応の記述を適当に読み替えると、書いた人が想定していない動作になります。そのため、解釈できない設定は反映しない方針にしています。

## 既存サイトへの導入

既存のS3バケットとCloudFront Distributionへ追加するためのCloudFormationテンプレートを入れています。

テンプレートが作るのはCloudFront KeyValueStore、Lambda、IAMロール、CloudWatch Logs、CloudFront Functionsです。既存バケットの通知設定とCloudFront Distributionは変更しません。

この2か所は既存設定との衝突があり得るため、現在の設定を確認してからCLIまたは既存のIaCで接続します。

特にviewer-requestのCloudFront Functionsは、同じCache Behaviorへ複数登録できません。すでに関数を使っている場合は、既存の処理と統合する必要があります。

導入手順は[Integration Guide](https://github.com/n-iskw/s3-cloudfront-htaccess-bridge/blob/main/docs/integration-guide.md)に書きました。

## まとめ

CloudFront Functionsを直接管理した方がいいサイトもあります。リダイレクトをIaCで厳密に管理したい場合は、その方が素直です。

この仕組みは、これまで`.htaccess`をコンテンツと一緒に管理してきた現場向けです。配信基盤をS3 + CloudFrontへ変えても、簡単な変更までエンジニアの作業に戻さないために作りました。

- [GitHubリポジトリ](https://github.com/n-iskw/s3-cloudfront-htaccess-bridge)
- [コンテンツ制作者向けガイド](https://github.com/n-iskw/s3-cloudfront-htaccess-bridge/blob/main/docs/content-creator-guide.md)
- [詳細仕様](https://github.com/n-iskw/s3-cloudfront-htaccess-bridge/blob/main/docs/reference.md)
