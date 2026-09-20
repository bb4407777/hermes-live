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

# 版本号单一来源是 server/__init__.py，package.json 里那份手工同步过没有？没有——
# 一直是 0.1.0，直接发会把 0.4.7 的代码贴上 0.1.0 发出去（或被 npm 判为重版本）。
python3 - "$DIST/package.json" <<'PY'
import json, re, sys
from pathlib import Path
ver = re.search(r'__version__ = "([^"]+)"',
                Path("server/__init__.py").read_text(encoding="utf-8")).group(1)
p = Path(sys.argv[1])
data = json.loads(p.read_text(encoding="utf-8"))
old = data.get("version")
data["version"] = ver
p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"npm version: {old} -> {ver}")
PY

cd "$DIST"
if [[ "${1:-}" == "--dry" ]]; then
  npm pack --dry-run
else
  npm publish
fi
