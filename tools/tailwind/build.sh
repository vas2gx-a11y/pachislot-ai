#!/bin/sh
# static/css/tailwind.css を作り直す。テンプレートに新しいクラスを書いたら実行してコミットする。
# 本番(Docker)にはNodeを入れていないので、生成したCSSはリポジトリに置いている。
# バージョンは以前使っていた Play CDN (v3系) と同じ挙動になるよう固定している。
set -e
cd "$(dirname "$0")/../.."
npx --yes tailwindcss@3.4.19 \
  -c tools/tailwind/tailwind.config.js \
  -i tools/tailwind/input.css \
  -o static/css/tailwind.css \
  --minify
