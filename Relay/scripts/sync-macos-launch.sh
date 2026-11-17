#!/bin/bash
# 将 launch-relay-macos.sh 同步到 ~/Scripts（供 LaunchAgent 执行，避免外置卷 exit 126）
set -e
REPO_SCRIPT="$(cd "$(dirname "$0")" && pwd)/launch-relay-macos.sh"
DEST="${HOME}/Scripts/pulse-relay-macos.sh"
mkdir -p "${HOME}/Scripts"
cp "$REPO_SCRIPT" "$DEST"
chmod +x "$DEST"
echo "已更新: $DEST"
