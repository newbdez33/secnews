# secnews

每日 IT 安全摘要（中文 / 日本語）的原始稿件与静态网站生成器。
日次セキュリティ情報（中国語・日本語）の原稿と静的サイト生成スクリプトです。

Web: https://newbdez33.github.io/secnews/
（旧 / 旧サイト: https://secnews-list-gcu.pages.dev/ — no longer updated）

## Layout

- `archive/` — daily digests: `YYYY-MM-DD_zh.md` (Chinese), `YYYY-MM-DD_ja.txt` (Japanese email: subject line, blank line, body), `YYYY-MM-DD_ja_body.txt` (body only, some days)
- `site/` — `build.py` turns `archive/` into a static site (base URL set by `DEFAULT_BASE_URL`, `SECNEWS_BASE_URL` or `--base-url`); `package.json` is only for the legacy Cloudflare Pages deploy
- `.github/workflows/pages.yml` — every push to `main` rebuilds and deploys to GitHub Pages
- `tools/reverse.py` — one-off script used on 2026-11-17 to rebuild 11-07..11-16 sources from the live pages and sent mail

## Build

```sh
python3 site/build.py --input archive --output _site   # local preview build
git push                                                # GitHub Actions deploys
```

Note: 2026-11-07..11-16 Chinese files were reconstructed from the published pages, so internal notes stripped at publish time are not included.
