"""桌面端：浏览器/缓存里已有百度登录态时，**自动**加入共享池（2026-10-25 用户定档）。

用户要求：百度 cookies 主要由管理员提供、用户自己填的入口隐藏；但用户浏览器里
本来就有百度登录态的，我们自动取到后自动进共享池（网页版与别人也能用上）。

钉住的契约：
  1. 手动保存的那份仍然优先（用户显式指定的账号盖过自动抓到的）；
  2. 从缓存命中 ⇒ 自动入本地池 + 推云端，两步都做；
  3. 从浏览器解密命中 ⇒ 同上；
  4. 自动贡献失败**不能**抛（不能因为共享池挂了影响本次下载）；
  5. 共享池兜底仍然排最后（自己的账号永远优先于别人的）。

全程离线：cookie_cache / cookie_pool / app 的云端推送都被替换成假实现。
运行：cd server && python tests/test_cloud_drive_auto_contrib.py
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from unittest import mock

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

os.environ.setdefault("VDL_DATA_DIR", "/tmp/vdl_test_auto_contrib")
os.environ.setdefault("VDL_CLOUD_LINK", "0")

import app as server_app  # noqa: E402,F401 — 必须先 import app：routers.cloud_drive 里有 `import app`，
# 直接先导入 routers.cloud_drive 会撞「partially initialized module」循环导入
import routers.cloud_drive as rt  # noqa: E402


def _fake_cache(peek: str = "", browser: str = ""):
    return types.SimpleNamespace(
        peek_cached_cookie_header=lambda host: peek if host == "pan.baidu.com" else "",
        get_cached_cookie_header=lambda host: browser if host == "pan.baidu.com" else "",
    )


def test_manual_bduss_wins_and_is_not_auto_contributed():
    """手动保存的那份优先，且**不**自动入池（它是显式指定的账号）。"""
    added: list = []
    pushed: list = []
    with mock.patch.dict(sys.modules, {"cookie_cache": _fake_cache(peek="BDUSS=from-cache")}), \
         mock.patch.object(rt, "get_baidu_bduss", return_value="BDUSS=manual"), \
         mock.patch("cookie_pool.add_cookie",
                    side_effect=lambda d, h, source="sync": added.append((d, h, source)) or True), \
         mock.patch("app._cloud_sync_config", return_value=("http://cloud", "tok")), \
         mock.patch("app._push_cookie_to_cloud",
                    side_effect=lambda d, h, u, t: pushed.append((d, h)) or {"ok": True}):
        got = rt._baidu_login_cookie()
    assert got == "BDUSS=manual", got
    assert not added, f"手动那份不应自动入池：{added}"
    assert not pushed, f"手动那份不应自动推云端：{pushed}"
    print("✅ 手动保存的登录态仍优先，且不会被自动共享")


def test_cache_hit_auto_contributes_to_pool_and_cloud():
    added: list = []
    pushed: list = []
    with mock.patch.dict(sys.modules, {"cookie_cache": _fake_cache(peek="BDUSS=from-cache")}), \
         mock.patch.object(rt, "get_baidu_bduss", return_value=""), \
         mock.patch("cookie_pool.add_cookie",
                    side_effect=lambda d, h, source="sync": added.append((d, h, source)) or True), \
         mock.patch("app._cloud_sync_config", return_value=("http://cloud", "tok")), \
         mock.patch("app._push_cookie_to_cloud",
                    side_effect=lambda d, h, u, t: pushed.append((d, h)) or {"ok": True}):
        got = rt._baidu_login_cookie()
    assert got == "BDUSS=from-cache", got
    assert added, "缓存里取到的登录态应自动入本地池"
    assert added[0][0] == "baidu.com", added
    assert pushed, "自动入池的同时应推云端（网页版与别人才能用上）"
    assert pushed[0][0] == "baidu.com", pushed
    print("✅ 缓存里的百度登录态会自动入本地池并推云端")


def test_browser_hit_auto_contributes():
    added: list = []
    with mock.patch.dict(sys.modules, {"cookie_cache": _fake_cache(browser="BDUSS=from-browser")}), \
         mock.patch.object(rt, "get_baidu_bduss", return_value=""), \
         mock.patch("cookie_pool.add_cookie",
                    side_effect=lambda d, h, source="sync": added.append((d, h, source)) or True), \
         mock.patch("app._cloud_sync_config", return_value=("", "")), \
         mock.patch("app._push_cookie_to_cloud", return_value={"ok": True}):
        got = rt._baidu_login_cookie()
    assert got == "BDUSS=from-browser", got
    assert added and added[0][0] == "baidu.com", added
    print("✅ 浏览器里解出来的登录态也会自动入池")


def test_auto_contrib_failure_does_not_break_download():
    """共享池挂了不能影响本次下载（自动贡献必须吞掉所有异常）。"""
    def _boom(*_a, **_k):
        raise RuntimeError("pool on fire")

    with mock.patch.dict(sys.modules, {"cookie_cache": _fake_cache(peek="BDUSS=from-cache")}), \
         mock.patch.object(rt, "get_baidu_bduss", return_value=""), \
         mock.patch("cookie_pool.add_cookie", side_effect=_boom), \
         mock.patch("app._cloud_sync_config", side_effect=_boom), \
         mock.patch("app._push_cookie_to_cloud", side_effect=_boom):
        got = rt._baidu_login_cookie()
    assert got == "BDUSS=from-cache", got
    print("✅ 共享池故障不影响本次取用登录态")


def test_pool_fallback_is_last():
    """共享池兜底排最后：自己的账号优先于别人的。"""
    with mock.patch.dict(sys.modules, {"cookie_cache": _fake_cache()}), \
         mock.patch.object(rt, "get_baidu_bduss", return_value=""), \
         mock.patch("cookie_pool.get_cookie", return_value="BDUSS=from-pool"):
        got = rt._baidu_login_cookie()
    assert got == "BDUSS=from-pool", got
    print("✅ 共享池兜底仍然排最后")


def test_real_baidu_cookie_header_is_not_prefixed():
    """🔴 真实百度 Cookie 是一整串（BAIDUID=...; BDUSS=...; STOKEN=...），
    开头通常是 BAIDUID 而不是 BDUSS。

    若用 startswith("bduss=") 判断「有没有 BDUSS」，就会给整串补一个
    `BDUSS=` 前缀 ⇒ 变成 `BDUSS=BAIDUID=...`（长度多 6 字节），这种畸形头
    验真必被拒 ⇒ 自动入池永远失败（本机实测踩到：reject-invalid len=2726）。
    正确判据是「整串里是否含 bduss=」。
    """
    added: list = []
    real_header = "BAIDUID=ABC123; BDUSS=REAL_BDUSS_VALUE; STOKEN=TOK; PANPSC=x"
    with mock.patch.dict(sys.modules, {"cookie_cache": _fake_cache(peek=real_header)}), \
         mock.patch.object(rt, "get_baidu_bduss", return_value=""), \
         mock.patch("cookie_pool.add_cookie",
                    side_effect=lambda d, h, source="sync": added.append((d, h, source)) or True), \
         mock.patch("app._cloud_sync_config", return_value=("", "")), \
         mock.patch("app._push_cookie_to_cloud", return_value={"ok": True}):
        got = rt._baidu_login_cookie()
    assert got == real_header, f"取回的登录态被改了：{got[:60]!r}"
    assert added, "没有入池"
    assert added[0][1] == real_header, \
        f"入池的 header 被加了畸形前缀：{added[0][1][:60]!r}"
    assert not added[0][1].lower().startswith("bduss=baiduid="), \
        "出现了 BDUSS=BAIDUID= 这种畸形头"
    print("✅ 真实百度 Cookie 整串原样入池，不被加畸形前缀")


def test_bare_bduss_value_still_gets_prefix():
    """反之：用户只给了一个裸 BDUSS 值时，仍要补上 BDUSS= 前缀。"""
    added: list = []
    with mock.patch.dict(sys.modules, {"cookie_cache": _fake_cache(peek="RAWVALUE123")}), \
         mock.patch.object(rt, "get_baidu_bduss", return_value=""), \
         mock.patch("cookie_pool.add_cookie",
                    side_effect=lambda d, h, source="sync": added.append((d, h, source)) or True), \
         mock.patch("app._cloud_sync_config", return_value=("", "")), \
         mock.patch("app._push_cookie_to_cloud", return_value={"ok": True}):
        got = rt._baidu_login_cookie()
    assert added and added[0][1] == "BDUSS=RAWVALUE123", added
    print("✅ 裸 BDUSS 值仍会补上 BDUSS= 前缀")


if __name__ == "__main__":
    for fn in (
        test_manual_bduss_wins_and_is_not_auto_contributed,
        test_cache_hit_auto_contributes_to_pool_and_cloud,
        test_browser_hit_auto_contributes,
        test_auto_contrib_failure_does_not_break_download,
        test_pool_fallback_is_last,
        test_real_baidu_cookie_header_is_not_prefixed,
        test_bare_bduss_value_still_gets_prefix,
    ):
        fn()
    print("\n🎉 百度登录态自动共享池测试全部通过")
