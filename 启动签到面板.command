#!/usr/bin/env bash
# macOS 双击启动：优先开打包好的 .app，没有就跑源码
cd "$(dirname "$0")" || exit 1

for candidate in "dist/WorkBuddy签到.app" "dist/WorkBuddySignin.app"; do
  if [ -d "$candidate" ]; then
    open "$candidate"
    exit 0
  fi
done

if ! command -v python3 >/dev/null 2>&1; then
  echo "没找到 python3，也没有打包好的 app。"
  echo "请先安装 Python 3（https://www.python.org/downloads/macos/），再执行："
  echo "  pip3 install pywebview"
  echo "  bash 打包mac.sh"
  read -r -p "按回车关闭…" _
  exit 1
fi

exec python3 desktop/app.py
