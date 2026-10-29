"""网页端「双配额」守卫（2026-10-06 用户要求彻底核查两端数据是否各算各的）。

## 背景：网页端历史上存在**两套**每日配额
1. **旧的开源版遗留**：`_download_quota` / `_convert_quota` 是 `ip -> {date,count}`
   的**按 IP 计数**字典（app.py 的 `_check_download_quota` / `_check_convert_quota`），
   由环境变量 `VDL_DOWNLOAD_REQUIRE_SUB` / `VDL_CONVERT_REQUIRE_SUB` 开启
   （默认 false）。IP 可换 ⇒ 换 IP 即重置。
2. **现行账号级**：`cloud_quota_gate`（resource="cloud"）→ `store.quota_state("cloud")`，
   账号级 + 异步上云（授权中心按账号累计，App/网页共享），hk 回派 cn 为权威。

web-dev 的 app.py 注释已明确写「旧 IP 粒度墙已被本机制取代，该环境变量必须保持
false —— 否则会双重计数」。

## 本守卫钉住
① 旧 IP 配额墙的开关**默认必须是 false**（代码默认值，不依赖部署者记得配）；
② 旧 IP 墙与账号级云端墙**不得同时启用**（双重计数：同一个下载既按 IP 记一次、
   又按账号记一次，结果是限次翻倍 / 计数打架）；
③ 账号级云端墙必须**真的**接了 `use_daily` / `quota_state`（防「注释说取代了，
   代码其实没接」）。

为什么必须在仓库里钉：线上 cn/hk 两台机的进程环境**当前**都没有设置这些变量
（走代码默认 false，实测），但只要有人为了「开源版体验」把它打开，就会同时按 IP
和按账号双计数 —— 那种 bug 表现为「限额时灵时不灵」，极难排查。
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_SERVER = _HERE.parent
_REPO = _SERVER.parent
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + name + (("  —— " + detail) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def _web_app_py() -> str:
    """读**网页版仓**的 server/app.py（网页端入口）。

    🔴 2026-10-29：三仓拆分后 `web-dev` 分支已不存在（`git show web-dev:...` 恒空），
    改为按工作树路径读：① 环境变量 `VDL_WEB_REPO` ② 默认同级 `video-downloader` 仓。
    """
    import os
    roots = []
    env = (os.environ.get("VDL_WEB_REPO") or "").strip()
    if env:
        roots.append(pathlib.Path(env))
    roots.append(_REPO.parent / "video-downloader")
    for root in roots:
        p = root / "server" / "app.py"
        try:
            if p.is_file():
                src = p.read_text(encoding="utf-8", errors="ignore")
                if src:
                    return src
        except OSError:
            continue
    print("  ❌ 读不到网页版仓的 server/app.py —— 无法核查网页端配额"
          "（可用 VDL_WEB_REPO 指定网页版仓路径）")
    FAILS.append("read web app.py")
    return ""


def test_ip_quota_defaults_off() -> None:
    print("\n[A] 旧 IP 粒度配额墙的开关默认必须是 false")
    src = _web_app_py()
    if not src:
        return
    for name in ("VDL_CONVERT_REQUIRE_SUB", "VDL_DOWNLOAD_REQUIRE_SUB"):
        # 实际写法：os.environ.get("VDL_XXX", "false").strip().lower() == "true"
        m = re.search(r'=\s*os\.environ\.get\(\s*["\']' + re.escape(name)
                      + r'["\']\s*,\s*["\']([^"\']*)["\']', src)
        got = m.group(1).strip().lower() if m else None
        check(f"{name} 默认值 = false", got == "false",
              f"实际默认 = {got if got is not None else '（未找到，需人工核对）'}")
    # 还有一道保险：SUB_ENABLED 需同时具备「开关 + 密钥」，空密钥也算关闭
    for name in ("CONVERT_SUB_ENABLED", "DOWNLOAD_SUB_ENABLED"):
        m = re.search(re.escape(name) + r'\s*=\s*(\w+)\s+and\s+bool', src)
        check(f"{name} 需同时有开关且有密钥", bool(m),
              "未找到「开关 and 密钥」的双条件写法，单开关可能绕过")


def test_no_dual_counting() -> None:
    print("\n[B] 旧 IP 墙与账号级云端墙不得同时启用（会双重计数）")
    src = _web_app_py()
    if not src:
        return
    # 账号级墙存在时，旧 IP 墙必须仍是被标注为「已被取代」的历史实现：
    # 要求 app.py 里明确留有告警注释，且 cloud_quota_gate 是主路径。
    has_account_level = bool(re.search(r'def cloud_quota_gate', src))
    check("账号级云端墙 cloud_quota_gate 存在", has_account_level,
          "web-dev app.py 里没有 cloud_quota_gate —— 账号级配额缺失")
    check("app.py 注明旧 IP 墙已被取代",
          "已被本机制取代" in src or "取代" in src,
          "缺少「旧 IP 墙已被取代」的说明，后来人可能同时打开两套")
    # 云端墙开启时，旧 IP 墙的开关必须被显式关掉（或旧墙根本不再被调用）
    ip_gates_used = len(re.findall(r'_check_(?:download|convert)_quota\(', src))
    check("旧 IP 墙的调用点不超过下载/转换各一处",
          ip_gates_used <= 2, f"发现 {ip_gates_used} 处调用，需确认没被多处叠加")


def test_account_level_wired() -> None:
    print("\n[C] 账号级云端墙必须真接了配额引擎（不能只留注释）")
    src = _web_app_py()
    if not src:
        return
    # 🔴 2026-10-06 拆池：web-dev app.py 不再有 `quota_state("cloud")` /
    # `use_daily("cloud")` 字面量 —— resource 由 gate 携带（动态变量），
    # 兜底形态是 `store.quota_state(resource)` / `store.use_daily(resource, n=n)`；
    # 主路径是 `cloud_quota_remote(tok, lifetime=n, resource=resource)`。
    # 拦截点字面量（resource="cloud_convert" 等）在 routers/*.py，由
    # test_feature_usage_gate.py 的 CROSS_END_RESOURCES 钉住。
    check("quota_state 兜底（动态 resource）", "store.quota_state(resource)" in src)
    check("use_daily 兜底（动态 resource）", "store.use_daily(resource" in src)
    check("中心主路径扣减带 per-resource 键",
          "resource=resource" in src,
          "拆池后中心扣减必须携带 resource（各功能独立终身额度）")
    # hk 必须回派 cn 为权威，否则两台机的云端额度各算各的
    check("hk 节点回派 cn（_cloud_quota_relay）", "_cloud_quota_relay" in src,
          "没有回派逻辑 ⇒ hk 与 cn 各算各的")
    check("回派走 /api/member/quota/use", "/api/member/quota/use" in src,
          "回派端点变了，需同步核对 cn 侧是否提供")


def main() -> None:
    print("=== 网页端双配额守卫 ===")
    test_ip_quota_defaults_off()
    test_no_dual_counting()
    test_account_level_wired()
    print()
    if FAILS:
        print(f"❌ 失败 {len(FAILS)} 项：")
        for f in FAILS:
            print("   -", f)
        sys.exit(1)
    print("✅ 网页端配额：旧 IP 墙默认关闭、账号级墙已接线、无双重计数")


if __name__ == "__main__":
    main()
