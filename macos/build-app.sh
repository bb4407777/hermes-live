#!/bin/bash
# 构建 Hermes-Live 菜单栏 app → ~/Applications/Hermes-Live.app（免费 CLT + 签名）。
# 做法承自 gpt-live build-app.sh：钥匙串有稳定开发证书就用（TCC 权限跨重编译不掉），
# 没有则 ad-hoc（能用，但重编译后首次开麦会重新弹权限）。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_ROOT="$HOME/Applications"
APP_PATH="$APP_ROOT/Hermes-Live.app"
CONTENTS="$APP_PATH/Contents"

IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null |
  awk -F'"' '/Apple Development:|Developer ID Application:/{print $2; exit}')"
IDENTITY="${IDENTITY:--}"

swift build --disable-sandbox --package-path "$SCRIPT_DIR" -c release
BIN="$(swift build --disable-sandbox --package-path "$SCRIPT_DIR" -c release --show-bin-path)/HermesLiveMac"

rm -rf "$APP_PATH"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources"
cp "$BIN" "$CONTENTS/MacOS/HermesLiveMac"
cp "$SCRIPT_DIR/Resources/Info.plist" "$CONTENTS/Info.plist"
[[ -f "$SCRIPT_DIR/Resources/AppIcon.icns" ]] && cp "$SCRIPT_DIR/Resources/AppIcon.icns" "$CONTENTS/Resources/AppIcon.icns"
chmod 755 "$CONTENTS/MacOS/HermesLiveMac"
codesign --force --sign "$IDENTITY" --identifier "cn.gao.hermes-live" "$APP_PATH"
codesign --verify --strict "$APP_PATH"

if [[ "$IDENTITY" == "-" ]]; then
  echo "已构建（ad-hoc 签名）：$APP_PATH —— 重编译后首次开麦会重新弹麦克风授权"
else
  echo "已构建（稳定证书 $IDENTITY）：$APP_PATH"
fi
[[ "${1:-}" == "--build-only" ]] || open "$APP_PATH"
