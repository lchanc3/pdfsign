#!/usr/bin/env bash
#
# 在 Proxmox VE 上建立一個 LXC 容器並安裝簽章工具。
# 在「PVE 節點的 shell」執行，不是在容器裡。
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/lchanc3/pdfsign/main/pve-install.sh)"
#
# 可用環境變數（全部都有預設值）：
#   CTID=201 CT_HOSTNAME=pdfsign DISK=8 CORES=1 RAM=512
#   BRIDGE=vmbr0 IPV4=dhcp                       # 或 IPV4=10.0.0.50/24 GATEWAY=10.0.0.1
#   STORAGE=local-lvm TEMPLATE_STORAGE=local
#   PORT=80 PASSWORD=<自訂 root 密碼> VERBOSE=1
#
set -euo pipefail

RAW=${RAW:-https://raw.githubusercontent.com/lchanc3/pdfsign/main}
VERBOSE=${VERBOSE:-0}

say() { printf '  \033[36m→\033[0m %s\n' "$*"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
die() { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

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

command -v pct >/dev/null || die "找不到 pct，這個腳本要在 Proxmox VE 節點上執行"
[[ $EUID -eq 0 ]] || die "請用 root 執行"

# ---------------------------------------------------------------- 參數

CTID=${CTID:-$(pvesh get /cluster/nextid)}
# 注意：不能用 HOSTNAME 當變數名，那是 bash 內建變數（值為本機主機名稱）
CT_HOSTNAME=${CT_HOSTNAME:-pdfsign}
DISK=${DISK:-8}
CORES=${CORES:-1}
RAM=${RAM:-512}
BRIDGE=${BRIDGE:-vmbr0}
IPV4=${IPV4:-dhcp}
GATEWAY=${GATEWAY:-}
PORT=${PORT:-80}
PASSWORD=${PASSWORD:-$(head -c 18 /dev/urandom | base64 | tr -d '/+=' | head -c 16)}

pct status "$CTID" &>/dev/null && die "CTID $CTID 已被使用，換一個：CTID=xxx bash $0"

if [[ -z ${STORAGE:-} ]]; then
  STORAGE=$(pvesm status -content rootdir 2>/dev/null | awk 'NR>1 && $3=="active" {print $1; exit}')
  [[ -n $STORAGE ]] || die "找不到可放容器磁碟的儲存，請指定：STORAGE=local-lvm bash $0"
fi

if [[ -z ${TEMPLATE_STORAGE:-} ]]; then
  TEMPLATE_STORAGE=$(pvesm status -content vztmpl 2>/dev/null | awk 'NR>1 && $3=="active" {print $1; exit}')
  [[ -n $TEMPLATE_STORAGE ]] || die "找不到可放 template 的儲存，請指定：TEMPLATE_STORAGE=local bash $0"
fi

echo
echo "  建立容器 $CTID（$CT_HOSTNAME）"
echo "  儲存 $STORAGE ・ 磁碟 ${DISK}G ・ ${CORES} 核 ・ ${RAM}MB ・ $BRIDGE / $IPV4"
echo

# ---------------------------------------------------------------- Template

say "確認 Debian 13 template"
pveam update >/dev/null 2>&1 || true

TEMPLATE=$(pveam list "$TEMPLATE_STORAGE" 2>/dev/null \
           | awk '/debian-13-standard/ {print $1}' | sort -V | tail -1)

if [[ -z $TEMPLATE ]]; then
  AVAIL=$(pveam available --section system | awk '/debian-13-standard/ {print $2}' | sort -V | tail -1)
  [[ -n $AVAIL ]] || die "找不到 debian-13-standard template"
  say "下載 $AVAIL（約 500MB，請稍候）"
  run pveam download "$TEMPLATE_STORAGE" "$AVAIL"
  TEMPLATE="$TEMPLATE_STORAGE:vztmpl/$AVAIL"
fi
ok "template 就緒"

# ---------------------------------------------------------------- 建立容器

NET="name=eth0,bridge=$BRIDGE,ip=$IPV4"
[[ -n $GATEWAY ]] && NET="$NET,gw=$GATEWAY"

say "建立容器"
# nesting=1：Debian 13 的 systemd 257 在 unprivileged LXC 內需要它才能正常運作
run pct create "$CTID" "$TEMPLATE" \
  --hostname "$CT_HOSTNAME" \
  --cores "$CORES" \
  --memory "$RAM" \
  --swap 512 \
  --rootfs "$STORAGE:$DISK" \
  --net0 "$NET" \
  --unprivileged 1 \
  --features nesting=1 \
  --password "$PASSWORD" \
  --onboot 1 \
  --tags pdfsign
ok "容器已建立"

say "啟動容器"
run pct start "$CTID"

for i in $(seq 1 30); do
  pct exec "$CTID" -- getent hosts deb.debian.org &>/dev/null && break
  (( i == 30 )) && die "容器內連不到網路，檢查 IP / Gateway / DNS 設定"
  sleep 2
done
ok "網路已連通"

# ---------------------------------------------------------------- 安裝

say "傳入程式檔"
SELF_DIR=""
if [[ -n ${BASH_SOURCE[0]:-} ]]; then
  SELF_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd) || SELF_DIR=""
fi

pct exec "$CTID" -- mkdir -p /opt/pdfsign-src
TMPDIR_LOCAL=$(mktemp -d)
trap 'rm -rf "$TMPDIR_LOCAL"' EXIT

for f in pdfsign.py requirements.txt install.sh; do
  if [[ -n $SELF_DIR && -f "$SELF_DIR/$f" ]]; then
    run pct push "$CTID" "$SELF_DIR/$f" "/opt/pdfsign-src/$f"
  else
    curl -fsSL "$RAW/$f" -o "$TMPDIR_LOCAL/$f" || die "下載 $f 失敗（RAW=$RAW）"
    run pct push "$CTID" "$TMPDIR_LOCAL/$f" "/opt/pdfsign-src/$f"
  fi
done
ok "程式檔已傳入"

pct exec "$CTID" -- env PDFSIGN_PORT="$PORT" VERBOSE="$VERBOSE" \
  bash /opt/pdfsign-src/install.sh

# ---------------------------------------------------------------- 完成

IP=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}')
URL="http://${IP}"
if [[ $PORT != 80 ]]; then URL="${URL}:${PORT}"; fi

echo
ok "安裝完成"
echo
echo "  網址       $URL"
echo "  容器       $CTID（$CT_HOSTNAME）"
echo "  root 密碼   $PASSWORD"
echo
echo "  進容器     pct enter $CTID"
echo "  看紀錄     pct exec $CTID -- journalctl -u pdfsign -n 50 --no-pager"
echo "  重新啟動   pct exec $CTID -- systemctl restart pdfsign"
echo
