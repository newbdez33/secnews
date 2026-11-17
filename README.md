# secnews

每日 IT 安全摘要（中文 / 日本語）的原始稿件与静态网站生成器。
日次セキュリティ情報（中国語・日本語）の原稿と静的サイト生成スクリプトです。

Web: https://secnews-list-gcu.pages.dev/

## Layout

- `archive/` — daily digests: `YYYY-MM-DD_zh.md` (Chinese), `YYYY-MM-DD_ja.txt` (Japanese email: subject line, blank line, body), `YYYY-MM-DD_ja_body.txt` (body only, some days)
- `site/` — `build.py` turns `archive/` into a static site in `dist/`; `package.json` pins Wrangler for Cloudflare Pages deploys
- `tools/reverse.py` — one-off script used on 2026-11-17 to rebuild 11-07..11-16 sources from the live pages and sent mail

## Build

```sh
cd site
python3 build.py --input ../archive --output dist
npm ci && npx wrangler pages deploy dist --project-name <project> --branch main
```

Note: 2026-11-07..11-16 Chinese files were reconstructed from the published pages, so internal notes stripped at publish time are not included.
