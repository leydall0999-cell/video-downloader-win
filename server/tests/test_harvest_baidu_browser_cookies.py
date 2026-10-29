"""harvest_baidu_browser_cookies 多 profile 抓取单元测试（离线，全程 mock）。

钉死 2026-10-29 多账号抓取的契约（移植自 mac 仓 83d5c01，读取路径按 Windows 适配）：
- Chrome 多个 profile（Default / Profile 1）里**每个**含 baidu.com 登录态的都要被抓到
  并 add_cookie 入池（原先只抓「单个最优 profile」，多账号只剩 1 个）；
- 没有 baidu.com 登录态的 profile 必须跳过，不污染候选；
- Safari 单独走一条路径也要抓到（Safari 不在 browser_cookies 来源表里）；
- 🔴 Windows 关键路径：**自研 read_cookies 能读到时完全不碰 yt-dlp**（本机实测 Chrome
  报 DPAPI 失败、Edge 被锁、UC/QQ yt-dlp 不认识，只有自研能读出百度登录态）；
- 自研读不出（库锁 / v20 加密）时才回退 yt-dlp；
- 单 profile 抛异常绝不影响其他账号。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import browser_cookies as _bc  # noqa: E402


class _FakeCookie:
    __slots__ = ("domain", "name", "value")

    def __init__(self, domain, name, value):
        self.domain = domain
        self.name = name
        self.value = value


class _FakeJar:
    def __init__(self, cookies):
        self._c = list(cookies)

    def __iter__(self):
        return iter(self._c)


_PROFILES = {
    "chrome": [("Default", "/x/Default/Network/Cookies"),
               ("Profile 1", "/x/Profile 1/Network/Cookies")],
    "uc": [("Default", "/u/Default/Network/Cookies")],
}


def _rows(bduss: str):
    return [{"host": ".baidu.com", "name": "BDUSS", "value": bduss,
             "path": "/", "secure": True},
            {"host": ".baidu.com", "name": "STOKEN", "value": "s-" + bduss,
             "path": "/", "secure": True}]


def _bc_fake(read_map):
    """假 browser_cookies：chrome 两个 profile + uc 一个；read_cookies 按 db 查表。

    read_map 的值可以是 rows 列表，也可以是 (rows, reason) 二元组。
    """
    sources = tuple({"key": k, "kind": "chromium", "roots": ("/x",)} for k in _PROFILES)

    def _read(db, kind, domains):
        v = read_map.get(db)
        if v is None:
            return [], ""
        return v if isinstance(v, tuple) else (v, "")

    return mock.MagicMock(
        _BROWSER_SOURCES=sources,
        iter_profiles=lambda src: list(_PROFILES.get(src["key"], [])),
        read_cookies=_read,
        build_cookie_header=_bc.build_cookie_header,
    )


def _extract_fake(browser, profile=None, **kw):
    if browser == "safari":
        return _FakeJar([_FakeCookie(".baidu.com", "BDUSS", "safari_bduss")])
    if browser == "chrome" and profile == "Profile 1":
        # 自研读不到时（库锁）靠 yt-dlp 兜底的这个 profile
        return _FakeJar([_FakeCookie(".baidu.com", "BDUSS", "chrome_p1_bduss")])
    return _FakeJar([])


def _run(read_map, extract=None, browser_cookies=None):
    import downloader

    added = []

    def fake_add(domain, header, source="browser"):
        added.append((domain, header, source))
        return True

    with mock.patch.object(downloader, "_browser_cookies",
                           browser_cookies if browser_cookies is not None else _bc_fake(read_map)), \
         mock.patch("yt_dlp.cookies.extract_cookies_from_browser",
                    side_effect=extract or _extract_fake) as ex, \
         mock.patch("cookie_pool.add_cookie", side_effect=fake_add):
        headers = downloader.harvest_baidu_browser_cookies()
        return headers, added, ex


def test_harvest_reads_all_profiles_via_self_decrypt():
    """自研能读到时：三个 profile 全部入池，且完全不碰 yt-dlp。"""
    read_map = {
        "/x/Default/Network/Cookies": _rows("chrome_default"),
        "/x/Profile 1/Network/Cookies": _rows("chrome_p1"),
        "/u/Default/Network/Cookies": _rows("uc_default"),
    }
    # 自研全命中时，yt-dlp 只应被 Safari 那一条路径调用（不是逐 profile 调用）
    headers, added, ex = _run(read_map)

    bodies = [h for _, h, _ in added]
    assert any("chrome_default" in h for h in bodies), "Default profile 没被抓"
    assert any("chrome_p1" in h for h in bodies), "Profile 1 没被抓"
    assert any("uc_default" in h for h in bodies), "UC profile 没被抓"
    assert len(headers) == 4, f"期望 3 个 profile + Safari = 4 份 header，实际 {len(headers)}"
    assert all("bduss=" in h.lower() for h in headers)
    assert all(src == "browser" for _, _, src in added), "入池 source 必须是 browser"

    # Windows 关键契约：自研读得出时，逐 profile 不该再走 yt-dlp（只该剩 Safari 那一次）
    calls = [(c.args[0], c.kwargs.get("profile")) for c in ex.call_args_list]
    assert calls == [("safari", None)], f"自研已命中的 profile 仍调了 yt-dlp：{calls}"


def test_harvest_falls_back_to_ytdlp_when_self_decrypt_empty():
    """自研读不到（库锁 / v20 加密）→ 回退 yt-dlp 把那一个 profile 捞回来。"""
    read_map = {}   # 全空
    headers, added, _ex = _run(read_map)

    bodies = [h for _, h, _ in added]
    assert any("chrome_p1_bduss" in h for h in bodies), "yt-dlp 兜底没生效"
    assert any("safari_bduss" in h for h in bodies), "Safari 没被抓"


def test_harvest_skips_profile_without_baidu():
    """没有 baidu 登录态的 profile 绝不能入池（否则会污染候选、白撞风控）。"""
    read_map = {
        "/x/Default/Network/Cookies": [{"host": ".example.com", "name": "foo", "value": "bar"}],
    }
    _headers, added, _ex = _run(read_map)
    assert not any("foo=bar" in h for _, h, _ in added), "无百度的 profile 不应入池"


def test_harvest_skips_when_browser_cookies_unavailable():
    import downloader
    with mock.patch.object(downloader, "_browser_cookies", None):
        assert downloader.harvest_baidu_browser_cookies() == []


def test_harvest_never_raises_on_broken_profile():
    """单 profile 炸掉（库锁/解密异常）不能连累其他账号。"""
    def boom(db, kind, domains):
        if "Profile 1" in db:
            raise PermissionError("locked")
        return _rows("ok_bduss"), ""

    bc = mock.MagicMock(
        _BROWSER_SOURCES=({"key": "chrome", "kind": "chromium", "roots": ("/x",)},),
        iter_profiles=lambda src: list(_PROFILES["chrome"]),
        read_cookies=boom,
        build_cookie_header=_bc.build_cookie_header,
    )
    headers, added, _ex = _run({}, extract=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("dpapi")),
                               browser_cookies=bc)
    bodies = [h for _, h, _ in added]
    assert any("ok_bduss" in h for h in bodies), "坏 profile 把好 profile 也带崩了"
    assert all("bduss=" in h.lower() for h in headers)
