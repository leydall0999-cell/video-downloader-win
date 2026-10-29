"""直链 UA 选优回归（2026-10-29）。

背景（用户实测：端到端只有 1.4MB/s，而同一条 dlink 手动用 P2SP UA 跑 8~32 连有 8~14MB/s）：
旧逻辑是「Range: bytes=0-0 探 1 字节，第一个能过就用」，候选表里浏览器 UA 排第一。
而浏览器 UA 恰恰是「1 字节能过、正式 4MB 分块大量 403」——
实测 8 连 4MB 分块全 403，于是整个下载变成退避循环。

所以 `_pick_netdisk_ua` 必须改成「能过 + 实测最快」两步选，这两条测试守的就是它。
"""
from __future__ import annotations

import sys
from pathlib import Path

import requests

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import downloader as dl  # noqa: E402


class _R:
    def __init__(self, status: int, body: bytes = b"", text: str = ""):
        self.status_code = status
        self._body = body
        self.text = text

    def iter_content(self, size):
        for i in range(0, len(self._body), max(size, 1)):
            yield self._body[i:i + max(size, 1)]

    def close(self):
        pass


def _install(monkeypatch, handler):
    monkeypatch.setattr(requests, "get", handler)
    monkeypatch.setattr(dl, "_UA_PROBE_BYTES", 64 * 1024)
    monkeypatch.setattr(dl, "_UA_PROBE_SECONDS", 1.0)
    monkeypatch.setattr(dl, "_netdisk_ua_hint", "")


def test_pick_ua_skips_one_byte_only_ua(monkeypatch):
    """浏览器 UA 只有 1 字节能过、采样拉数据就 403 ⇒ 必须选 P2SP。"""
    def fake_get(url, headers=None, **kw):
        ua = (headers or {}).get("User-Agent", "")
        rng = (headers or {}).get("Range", "")
        if ua == dl._NETDISK_DOWNLOAD_UA:      # 浏览器 UA：小 Range 过、大 Range 403
            if rng == "bytes=0-0":
                return _R(206, b"\0")
            return _R(403, text='{"error_code":31326,"user is not authorized"}')
        return _R(206, b"\0" * (64 * 1024))

    _install(monkeypatch, fake_get)
    picked = dl._pick_netdisk_ua("https://d.pcs.baidu.com/file/x", "BDUSS=x", "")
    assert picked == dl._NETDISK_UA, "浏览器 UA 只有 1 字节能过时不该被选中"
    assert dl._netdisk_ua_hint == dl._NETDISK_UA, "选中的 UA 要记进 hint，别每次重探"


def test_pick_ua_prefers_faster_when_both_work(monkeypatch):
    """两个 UA 都能过时，按实测速率挑，而不是按候选顺序挑。"""
    import time as _t

    def fake_get(url, headers=None, **kw):
        ua = (headers or {}).get("User-Agent", "")
        if ua == dl._NETDISK_DOWNLOAD_UA:
            _t.sleep(0.20)                      # 慢：64KB 要 0.2s
            return _R(206, b"\0" * (64 * 1024))
        return _R(206, b"\0" * (64 * 1024))     # 快：几乎瞬时

    _install(monkeypatch, fake_get)
    picked = dl._pick_netdisk_ua("https://d.pcs.baidu.com/file/x", "BDUSS=x", "")
    assert picked == dl._NETDISK_UA, "两个都可用时应选实测更快的那一个"


def test_pick_ua_falls_back_when_all_unusable(monkeypatch):
    """全 403 时回候选表第一个，不能抛异常（交给后面的 403 退避兜）。"""
    _install(monkeypatch, lambda *a, **kw: _R(403, text="denied"))
    assert dl._pick_netdisk_ua("https://d.pcs.baidu.com/file/x", "", "") == \
        dl._NETDISK_UA_CANDIDATES[0]
