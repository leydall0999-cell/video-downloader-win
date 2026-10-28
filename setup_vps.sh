#!/usr/bin/env bash
# setup_vps.sh — 国内 VPS 上一键部署 cn_proxy.py 回源代理（Basic 鉴权 + systemd 守护）
#
# 用法：
#   sudo ./setup_vps.sh --auth 你的user:你的强密码 [--port 18888] [--src /path/to/cn_proxy.py]
#
# 参数：
#   --auth  必填。格式 user:pass，作为代理 Basic 鉴权凭据（Railway 的 VDL_PROXY_CN 用同一对）
#   --port  可选。监听端口，默认 18888
#   --src   可选。本地 cn_proxy.py 路径；不传则优先用当前目录的，再不行从 GitHub main 拉取
#
# 做完的事：装 python3（若缺）→ 放脚本到 /opt/vdl-proxy → 写 systemd 单元（含 CN_PROXY_AUTH）
#           → 开机自启+崩溃重启 → 自检鉴权（无密码 407 / 带密码 200）→ 放行防火墙
#           → 打印 Railway 要填的 VDL_PROXY_CN
set -euo pipefail

PORT=18888
AUTH=""
SRC=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --auth) AUTH="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --src)  SRC="$2"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "✗ 未知参数: $1"; exit 1 ;;
  esac
done

GREEN=$'\033[0;32m'; RED=$'\033[0;31m'; YEL=$'\033[0;33m'; NC=$'\033[0m'
ok(){ echo "${GREEN}✓${NC} $*"; }
bad(){ echo "${RED}✗${NC} $*"; }
warn(){ echo "${YEL}!${NC} $*"; }

# 必须以 root 运行（systemd 单元要写系统目录）
[[ $EUID -eq 0 ]] || { bad "请用 root 运行：sudo $0 ..."; exit 1; }
# 校验 --auth 格式
if [[ -z "$AUTH" || "$AUTH" != *:* || "${AUTH%%:*}" = "" ]]; then
  bad "必须传 --auth user:pass（含冒号，user 不能为空）"; exit 1
fi

INSTALL_DIR=/opt/vdl-proxy
UNIT=/etc/systemd/system/cn_proxy.service
PY=$(command -v python3 || true)

echo "==> 检测 Python3"
if [[ -z "$PY" ]]; then
  warn "未找到 python3，尝试安装…"
  if command -v apt-get >/dev/null; then
    apt-get update -y && apt-get install -y python3
  elif command -v dnf >/dev/null; then
    dnf install -y python3
  elif command -v yum >/dev/null; then
    yum install -y python3
  else
    bad "无法自动安装 python3，请手动安装后重试"; exit 1
  fi
  PY=$(command -v python3)
fi
ok "python3 = $PY"

echo "==> 准备目录 $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"

echo "==> 放置 cn_proxy.py"
SCRIPT_SRC=""
if [[ -n "$SRC" && -f "$SRC" ]]; then
  SCRIPT_SRC="$SRC"
elif [[ -f "./cn_proxy.py" ]]; then
  SCRIPT_SRC="./cn_proxy.py"
else
  warn "本地无 cn_proxy.py，尝试从 GitHub main 拉取…"
  # 2026-10-28 仓拆分：自更新必须指向 video-downloader-web（VPS 跑的就是网页版线）。
  # 旧地址 video-downloader/main 拆仓后冻结在旧代码上，从那里拉会把 cn_proxy.py
  # 静默回退成旧版 —— 比 404 更难发现。
  if curl -fsSL "https://raw.githubusercontent.com/leydall0999-cell/video-downloader-web/main/cn_proxy.py" -o "$INSTALL_DIR/cn_proxy.py"; then
    SCRIPT_SRC="$INSTALL_DIR/cn_proxy.py"
  fi
fi
if [[ -z "$SCRIPT_SRC" ]]; then
  bad "找不到 cn_proxy.py：请用 --src 指定路径，或确保 VPS 能访问 GitHub"; exit 1
fi
[[ "$SCRIPT_SRC" != "$INSTALL_DIR/cn_proxy.py" ]] && cp -f "$SCRIPT_SRC" "$INSTALL_DIR/cn_proxy.py"
chmod 644 "$INSTALL_DIR/cn_proxy.py"
ok "已就位: $INSTALL_DIR/cn_proxy.py"

echo "==> 写 systemd 单元 $UNIT"
cat > "$UNIT" <<UNIT
[Unit]
Description=VDL 国内回源代理 (cn_proxy.py)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
Environment=CN_PROXY_AUTH=$AUTH
ExecStart=$PY $INSTALL_DIR/cn_proxy.py $PORT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
ok "单元已写"

echo "==> 启动服务"
systemctl daemon-reload
systemctl enable --now cn_proxy
sleep 2
if systemctl is-active --quiet cn_proxy; then
  ok "cn_proxy 运行中 (active)"
else
  bad "服务未起来，查日志: journalctl -u cn_proxy -n 50"; exit 1
fi

echo "==> 自检鉴权"
NO=$(curl -s --max-time 8  -x "http://127.0.0.1:$PORT"                 http://www.bilibili.com -o /dev/null -w "%{http_code}" || echo 000)
YES=$(curl -s --max-time 12 -x "http://$AUTH@127.0.0.1:$PORT" http://www.bilibili.com -o /dev/null -w "%{http_code}" || echo 000)
if [[ "$NO" == "407" && "$YES" =~ ^(200|301)$ ]]; then
  ok "鉴权正常：无密码→$NO，带密码→$YES"
else
  warn "自检结果：无密码→$NO，带密码→$YES（若 VPS 无法直连 B站可能非 200，但服务应已运行）"
fi

echo "==> 防火墙放行"
if command -v firewall-cmd >/dev/null && systemctl is-active --quiet firewalld; then
  firewall-cmd --permanent --add-port=$PORT/tcp && firewall-cmd --reload
  ok "firewalld 已放行 $PORT/tcp"
elif command -v ufw >/dev/null && systemctl is-active --quiet ufw; then
  ufw allow $PORT/tcp
  ok "ufw 已放行 $PORT/tcp"
else
  warn "未检测到 firewalld/ufw；请去云厂商控制台安全组放行 TCP $PORT（来源建议限 Railway 出口 IP）"
fi

PUB_IP=$(curl -fsS --max-time 8 https://api.ipify.org || hostname -I | awk '{print $1}')
echo
ok "部署完成！Railway 环境变量填这一行，然后重新部署："
echo "    VDL_PROXY_CN=http://$AUTH@$PUB_IP:$PORT"
