#!/bin/bash
# SIP 线路测试台启动脚本：缺 baresip 就装，然后起本地面板并打开浏览器
cd "$(dirname "$0")"
if ! command -v baresip >/dev/null 2>&1; then
  echo "未找到 baresip，正在通过 Homebrew 安装..."
  brew install baresip || { echo "安装失败"; exit 1; }
fi
exec python3 server.py "$@"
