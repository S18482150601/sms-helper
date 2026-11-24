#!/bin/sh
cd "$(dirname "$0")" || exit 1
if ! command -v node >/dev/null 2>&1; then
  echo "没有检测到 Node.js，请先安装：https://nodejs.org"
  exit 1
fi
[ -f config.json ] || { cp config.example.json config.json; echo "已生成 config.json（默认模拟模式）"; }
exec node server.js
