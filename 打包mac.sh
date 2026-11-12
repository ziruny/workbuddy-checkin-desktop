#!/usr/bin/env bash
# 在 macOS 上把桌面面板打成 .app（并顺带打一个 .dmg）
#   用法：bash 打包mac.sh
#   跳过依赖安装：NO_INSTALL=1 bash 打包mac.sh
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
NAME="${APP_NAME:-WorkBuddySignin}"
APP="dist/${NAME}.app"
DMG="dist/${NAME}.dmg"

if [ "${NO_INSTALL:-0}" != "1" ]; then
  echo "==> 安装依赖"
  "$PY" -m pip install --upgrade --quiet pywebview pyinstaller
fi

echo "==> 打包 .app（约 1 分钟）"
"$PY" -m PyInstaller --noconfirm --clean --windowed \
  --name "${NAME}" \
  --icon "assets/app.icns" \
  --osx-bundle-identifier com.workbuddy.signin \
  --add-data "desktop/ui:ui" \
  --paths . --paths desktop \
  --collect-all webview \
  --hidden-import webview.platforms.cocoa \
  desktop/app.py

# 未签名应用首次打开会被 Gatekeeper 拦；先 ad-hoc 签名 + 去隔离标记。
# 要做「双击无提示」的正式分发，需要 Apple 开发者账号做 codesign + notarytool 公证。
echo "==> ad-hoc 签名"
codesign --force --deep --sign - "$APP" 2>/dev/null || echo "   （签名失败可忽略，首次打开用右键→打开）"
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true

if command -v hdiutil >/dev/null 2>&1; then
  echo "==> 生成 dmg"
  STAGE="$(mktemp -d)"
  cp -R "$APP" "$STAGE/"
  ln -s /Applications "$STAGE/Applications"
  rm -f "$DMG"
  hdiutil create -volname "${NAME}" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
  rm -rf "$STAGE"
  echo "==> 完成：$DMG"
else
  echo "==> 完成：$APP"
fi
