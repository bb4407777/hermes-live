#!/bin/bash
# npm 发布：临时目录组包（README 用公众版 README-npm.md，仓库 README 不进包）。
# 用法: scripts/publish-npm.sh [--dry]   （HOME 须是真身 /Users/gao 以带上 npm 登录态）
set -euo pipefail
cd "$(dirname "$0")/.."

DIST=$(mktemp -d /tmp/hermes-live-npm.XXXXXX)
trap 'rm -rf "$DIST"' EXIT

cp package.json cli.js requirements-core.txt "$DIST/"
cp README-npm.md "$DIST/README.md"
rsync -a --exclude='__pycache__' server "$DIST/"
rsync -a web "$DIST/"

cd "$DIST"
if [[ "${1:-}" == "--dry" ]]; then
  npm pack --dry-run
else
  npm publish
fi
