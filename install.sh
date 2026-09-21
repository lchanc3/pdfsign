#!/usr/bin/env bash
# 在 Debian / Ubuntu（含 Proxmox LXC）安裝簽章工具並設為系統服務。
#   sudo bash install.sh
#
# 環境變數：PDFSIGN_DIR（預設 /opt/pdfsign）、PDFSIGN_PORT（預設 80）
#           VERBOSE=1 顯示完整安裝過程
set -euo pipefail

DIR=${PDFSIGN_DIR:-/opt/pdfsign}
PORT=${PDFSIGN_PORT:-80}
VERBOSE=${VERBOSE:-0}
SRC=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)

# 容器 template 通常沒產生 locale，先壓掉 perl/apt 的警告
export LC_ALL=C LANG=C DEBIAN_FRONTEND=noninteractive

[[ $EUID -eq 0 ]] || { echo "請用 root 或 sudo 執行"; exit 1; }
[[ -f "$SRC/pdfsign.py" ]] || { echo "找不到 $SRC/pdfsign.py"; exit 1; }

say() { printf '  \033[36m→\033[0m %s\n' "$*"; }

# 靜默執行；失敗時才把完整輸出吐出來
run() {
  if [[ $VERBOSE == 1 ]]; then
    "$@"
    return
  fi
  local log rc=0
  log=$(mktemp)
  "$@" >"$log" 2>&1 || rc=$?
  if (( rc != 0 )); then
    printf '\n指令失敗（exit %d）：%s\n\n' "$rc" "$*" >&2
    cat "$log" >&2
    rm -f "$log"
    exit "$rc"
  fi
  rm -f "$log"
}

APT=(apt-get -o Dpkg::Use-Pty=0 -o Dpkg::Progress-Fancy=0 -qq)

say "安裝系統套件與楷體字型（約 60MB，請稍候）"
run "${APT[@]}" update
run "${APT[@]}" install -y --no-install-recommends \
  python3 python3-venv python3-pip fontconfig fonts-cns11643-kai
fc-cache -f >/dev/null 2>&1 || true

say "建立 $DIR"
install -d "$DIR"
install -m 644 "$SRC/pdfsign.py" "$DIR/pdfsign.py"
install -m 644 "$SRC/requirements.txt" "$DIR/requirements.txt"

say "建立 venv 並安裝相依套件"
run python3 -m venv "$DIR/venv"
run "$DIR/venv/bin/pip" install --upgrade pip
run "$DIR/venv/bin/pip" install -r "$DIR/requirements.txt"

say "設定 systemd 服務"
cat > /etc/systemd/system/pdfsign.service <<EOF
[Unit]
Description=PDF signing tool
After=network.target

[Service]
Type=simple
WorkingDirectory=$DIR
# 暫存的 PDF 放 /var/lib/pdfsign；systemd 會建好目錄並把路徑放進 STATE_DIRECTORY
StateDirectory=pdfsign
Environment=PDFSIGN_PORT=$PORT
ExecStart=$DIR/venv/bin/python $DIR/pdfsign.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

run systemctl daemon-reload
run systemctl enable pdfsign
run systemctl restart pdfsign

for _ in $(seq 1 20); do
  systemctl is-active --quiet pdfsign && break
  sleep 1
done

if systemctl is-active --quiet pdfsign; then
  IP=$(hostname -I | awk '{print $1}')
  URL="http://$IP"
  if [[ $PORT != 80 ]]; then URL="$URL:$PORT"; fi
  printf '  \033[32m✓\033[0m 服務已啟動   %s\n' "$URL"
else
  echo "服務沒有起來：journalctl -u pdfsign -n 40 --no-pager" >&2
  exit 1
fi
