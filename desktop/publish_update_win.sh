#!/usr/bin/env bash
# publish_update_win.sh — 发布 Windows 版「应用内更新」清单（latest-win.json）。
#
# 与 macOS 的 publish_update.sh 平行，但**只发清单、不搬大包**：
#   · 安装包本体托管在 CF R2（download.hanyuxz.top），清单里的 url 直接指向 R2
#     ⇒ Windows 用户走 CF 边缘全速下载，不占 ECS 那 3.2Mbps 的出口；
#   · 发布动作只是把几 KB 的 latest-win.json + 它的 .sig 传到更新源。
#
# 用法：
#   VDL_UPDATE_SIGNING_KEY=~/.vdl_update_signing_key.pem bash desktop/publish_update_win.sh
#   bash desktop/publish_update_win.sh --version 1.0.69 --file dist/VideoDownloader-Setup-1.0.69.exe
#   bash desktop/publish_update_win.sh --version 1.0.69 --sha256 <hex> --size <bytes>  # 已有指纹时不下载
#   bash desktop/publish_update_win.sh --force        # 允许发布不高于线上版本的版本
#   bash desktop/publish_update_win.sh --yes          # 跳过人工确认
#
# 更新说明默认取自 server/changelog.py 里该版本的条目（与 Mac 版同源），
# 可用 --notes-file <文件> 覆盖（一行一条）。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VER="$(tr -d '[:space:]' < "$REPO/VERSION" 2>/dev/null || echo "")"
LOCAL_FILE=""
SHA256_ARG=""
SIZE_ARG=""
ASSUME_YES=0
FORCE=0
NOTES_FILE="${VDL_RELEASE_NOTES_FILE:-}"

VPS_HOST="${VDL_UPDATE_HOST:-root@8.138.223.3}"
VPS_DIR="${VDL_UPDATE_DIR:-/opt/vdl-update}"
PUB_BASE="${VDL_UPDATE_BASE_URL:-http://8.138.223.3:8765}"
SIGN_KEY="${VDL_UPDATE_SIGNING_KEY:-$HOME/.vdl_update_signing_key.pem}"

while [ $# -gt 0 ]; do
  case "$1" in
    --version) VER="$(tr -d '[:space:]' <<<"$2")"; shift 2;;
    --file) LOCAL_FILE="$2"; shift 2;;
    --sha256) SHA256_ARG="$2"; shift 2;;
    --size) SIZE_ARG="$2"; shift 2;;
    --notes-file) NOTES_FILE="$2"; shift 2;;
    --yes|-y) ASSUME_YES=1; shift;;
    --force) FORCE=1; shift;;
    *) echo "未知参数：$1"; exit 1;;
  esac
done

[ -n "$VER" ] || { echo "✗ 无法确定版本号（VERSION 文件缺失？可用 --version 指定）"; exit 1; }

R2_URL="${VDL_R2_INSTALLER_WINDOWS:-https://download.hanyuxz.top/VideoDownloader-Setup-${VER}.exe}"
OUT_DIR="$REPO/dist"
MANIFEST="$OUT_DIR/latest-win.json"
mkdir -p "$OUT_DIR"

PY="$REPO/.build_venv/bin/python"
[ -x "$PY" ] || PY="python3"

echo "▶ Windows 更新清单发布 v$VER"
echo "   安装包 : $R2_URL"

# ── 1) 取指纹（size + sha256）──────────────────────────────────────────────
# 优先级：命令行指定 > 本地文件 > 从 R2 流式下载计算。
if [ -n "$SHA256_ARG" ] && [ -n "$SIZE_ARG" ]; then
  SHA="$SHA256_ARG"; SIZE="$SIZE_ARG"
  echo "   指纹   : 由命令行给定"
elif [ -n "$LOCAL_FILE" ] && [ -f "$LOCAL_FILE" ]; then
  read -r SHA SIZE < <("$PY" - "$LOCAL_FILE" <<'PY'
import hashlib, sys, os
p = sys.argv[1]
h = hashlib.sha256()
with open(p, "rb") as f:
    for b in iter(lambda: f.read(1024 * 1024), b""):
        h.update(b)
print(h.hexdigest(), os.path.getsize(p))
PY
)
  echo "   指纹   : 由本地文件计算（$LOCAL_FILE）"
else
  echo "   指纹   : 本地无文件，从 R2 流式下载计算（约 182MB，请稍候）"
  if [ -n "$LOCAL_FILE" ] && [ ! -f "$LOCAL_FILE" ]; then
    echo "   ⚠️ --file 指定的文件不存在：$LOCAL_FILE（改为下载计算）"
  fi
  read -r SHA SIZE < <("$PY" - "$R2_URL" <<'PY'
import hashlib, sys, urllib.request
url = sys.argv[1]
h = hashlib.sha256()
total = 0
req = urllib.request.Request(url, headers={"User-Agent": "VDL-Update/1.0"})
with urllib.request.urlopen(req, timeout=1800) as r:
    cl = int(r.headers.get("Content-Length", "0") or 0)
    while True:
        chunk = r.read(1024 * 1024)
        if not chunk:
            break
        h.update(chunk)
        total += len(chunk)
        if cl:
            print("\r   已读 %3d%%（%d/%d 字节）" % (total * 100 // cl, total, cl),
                  end="", file=sys.stderr, flush=True)
print("", file=sys.stderr)
print(h.hexdigest(), total)
PY
)
fi
echo "   size   : $SIZE"
echo "   sha256 : $SHA"

# ── 2) 更新说明（默认取 changelog.py 里该版本的条目）─────────────────────
"$PY" - "$REPO" "$VER" "$MANIFEST" "$R2_URL" "$SIZE" "$SHA" "$NOTES_FILE" <<'PY'
import json, sys, os
from pathlib import Path

repo, ver, out, url, size, sha, notes_file = sys.argv[1:8]
# 🔴 显式给了 notes 文件就以它为准（**空文件 = 更新说明留白**），不再兜底默认文案
#    （2026-10-31 修：原先空文件会走进「性能优化与问题修复」兜底，等于替用户代写）。
explicit_notes = bool(notes_file and Path(notes_file).is_file())
notes_list = []
if explicit_notes:
    notes_list = [ln.strip() for ln in Path(notes_file).read_text(encoding="utf-8").splitlines() if ln.strip()]
elif os.environ.get("VDL_RELEASE_NOTES"):
    notes_list = [ln.strip() for ln in os.environ["VDL_RELEASE_NOTES"].splitlines() if ln.strip()]
if not notes_list and not explicit_notes:
    sys.path.insert(0, str(Path(repo) / "server"))
    try:
        from changelog import CHANGELOG
        for item in CHANGELOG:
            if str(item.get("version")) == ver:
                notes_list = [str(x).strip() for x in item.get("items", []) if str(x).strip()]
                break
    except Exception:
        notes_list = []
if not notes_list and not explicit_notes:
    notes_list = ["性能优化与问题修复"]

data = {
    "version": ver,
    "notes": "\n".join(notes_list),
    "notes_list": notes_list,
    "published_at": __import__("datetime").date.today().isoformat(),
    "url": url,
    "size": int(size),
    "sha256": sha.lower(),
    # 与 macOS 清单区分：客户端据此知道这是 Windows 的 Setup.exe（无增量）
    "platform": "windows",
    "installer": True,
}
Path(out).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("   ✔ 已生成 %s（%d 条更新说明）" % (out, len(notes_list)))
PY

# ── 3) 版本单调性检查（防误发旧版本）─────────────────────────────────────
online="$(curl -s --noproxy '*' -m 15 "$PUB_BASE/latest-win.json" | "$PY" -c 'import json,sys
try:
    print(json.load(sys.stdin).get("version") or "")
except Exception:
    print("")' || true)"
if [ -n "$online" ] && [ "$FORCE" != "1" ]; then
  newer="$("$PY" - "$online" "$VER" <<'PY'
import sys
def pv(v):
    return tuple(int("".join(c for c in p if c.isdigit()) or 0) for p in v.split(".")[:3]) + (0, 0, 0)
a, b = pv(sys.argv[1]), pv(sys.argv[2])
print("1" if b > a else "0")
PY
)"
  if [ "$newer" != "1" ]; then
    echo "✗ 线上已是 v$online，待发布 v$VER 不高于它（需要覆盖请加 --force）"
    exit 1
  fi
  echo "   线上   : v$online → v$VER（版本递增，通过）"
fi

# ── 4) 签名（Ed25519；客户端 fail-closed，未签名的清单会被拒绝）───────────
SIG="${MANIFEST}.sig"
rm -f "$SIG"
if [ ! -f "$SIGN_KEY" ]; then
  echo "✗ 签名私钥不存在：$SIGN_KEY"
  echo "  生成：VDL_UPDATE_SIGNING_KEY=... $PY tools/gen_update_signing_key.py"
  exit 1
fi
if "$PY" - "$MANIFEST" "$SIGN_KEY" <<'PY'
import base64, sys
from cryptography.hazmat.primitives import serialization
path, keyfile = sys.argv[1], sys.argv[2]
key = serialization.load_pem_private_key(open(keyfile, "rb").read(), password=None)
raw = open(path, "rb").read()
open(path + ".sig", "wb").write(base64.b64encode(key.sign(raw)))
PY
then
  echo "   ✔ 已生成签名 latest-win.json.sig"
else
  echo "✗ 签名生成失败（cryptography 是否可用？用的是 $(basename "$PY")）"
  exit 1
fi

# ── 5) 人工确认（默认开启：发布后所有 Windows 用户都会看到这次更新）───────
if [ "$ASSUME_YES" != "1" ]; then
  echo
  echo "⚠️  即将把 Windows v$VER 发布为线上最新版本，所有 Windows 用户的「检查更新」都会看到它。"
  printf "   确认已验证过该安装包？请输入 %s 以继续（直接回车取消）: " "$VER"
  read -r confirm || confirm=""
  [ "$confirm" = "$VER" ] || { echo "已取消"; exit 1; }
fi

# ── 6) 上传更新源 + 核验 ──────────────────────────────────────────────────
scp -o BatchMode=yes "$MANIFEST" "$SIG" "$VPS_HOST:$VPS_DIR/" >/dev/null
echo "   ✔ 已上传到 $VPS_HOST:$VPS_DIR/"

local_sha="$(shasum -a 256 "$MANIFEST" | awk '{print $1}')"
# ⚠️ awk 留在本地管道里：写进 ssh 的远程命令时 \$1 的转义在 macOS bash 下会被吃掉
remote_sha="$(ssh -o BatchMode=yes "$VPS_HOST" "sha256sum $VPS_DIR/latest-win.json" | awk '{print $1}')"
[ "$local_sha" = "$remote_sha" ] || { echo "✗ 线上清单与本地不一致（$remote_sha ≠ $local_sha）"; exit 1; }

got="$(curl -s --noproxy '*' -m 15 "$PUB_BASE/latest-win.json" | "$PY" -c 'import json,sys
d=json.load(sys.stdin); print(d.get("version",""), d.get("size",""), d.get("sha256","")[:12])')"
sig_ok="$(curl -s -o /dev/null -w '%{http_code}' --noproxy '*' -m 15 "$PUB_BASE/latest-win.json.sig")"
echo "   ✔ 线上可访问：version/size/sha = $got ；签名文件 HTTP $sig_ok"
echo
echo "✅ Windows 更新清单已发布：v$VER"
echo "   Windows 用户打开「关于本应用 → 检查更新」即可看到本次更新。"
