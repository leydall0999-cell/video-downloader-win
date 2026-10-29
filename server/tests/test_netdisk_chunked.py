"""网盘直链分块下载回归（2026-10-28）。

背景（实测同一条百度 dlink，非超会账号的个人网盘直链）：
    · 不带 Range            → 403
    · bytes=0-（开放区间）  → 403
    · bytes=0-1023 / 0-4MB  → 206
所以 `_download_netdisk_direct` 绝不能一次整包 GET，必须**按有界 Range 分块循环**。
这条测试守的就是「别再把 Range 写回 bytes=0- / 别退回单次请求」。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import requests

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import downloader as dl  # noqa: E402

CHUNK = 256 * 1024          # 与 CHUNK 下限一致
TOTAL = CHUNK + 10          # 两块：满块 + 10 字节尾巴


def patch_http(monkeypatch, fake):
    """同时替换模块级 requests.get 与 Session.get。

    并发版每条 worker 复用一个 requests.Session（省 TCP/TLS 握手），探测走模块级
    requests.get —— 两处都得打桩，否则测试会真连公网。
    """
    monkeypatch.setattr(requests, "get", fake)
    monkeypatch.setattr(requests.Session, "get", lambda self, *a, **kw: fake(*a, **kw))


class _Task:
    def __init__(self, workdir):
        self.id = "t1"
        self.url = "https://d.pcs.baidu.com/file/xxx"
        self.workdir = str(workdir)
        self.total_bytes = TOTAL
        self.cookie = "BDUSS=x"
        self.proxy = ""
        self.cancel_requested = False
        self.pause_requested = False
        self.is_finished = False
        self.title = "demo.mp4"
        self.steps = []
        self.logs = []

    def add_step(self, *a, **kw):
        self.steps.append((a, kw))

    def log(self, msg):
        self.logs.append(msg)


class _Store:
    def __init__(self):
        self.updates = []
        self.status = None

    def update(self, tid, **kw):
        self.updates.append(kw)
        if "status" in kw:
            self.status = kw["status"]


class _Resp:
    def __init__(self, start, body, total):
        self.status_code = 206
        self.headers = {
            "Content-Range": f"bytes {start}-{start + len(body) - 1}/{total}",
            "Content-Type": "video/mp4",
        }
        self.url = "https://d.pcs.baidu.com/file/xxx"
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, size):
        for i in range(0, len(self._body), size):
            yield self._body[i:i + size]


def test_netdisk_uses_bounded_range_chunks(tmp_path, monkeypatch):
    calls = []

    def fake_get(url, headers=None, **kw):
        rng = (headers or {}).get("Range", "")
        calls.append(rng)
        assert rng.startswith("bytes=") and not rng.endswith("-"), \
            f"必须是有界 Range，实际 {rng!r}"
        start, end = (int(x) for x in rng[len("bytes="):].split("-"))
        n = min(end - start + 1, TOTAL - start)
        return _Resp(start, b"\0" * n, TOTAL)

    patch_http(monkeypatch, fake_get)
    monkeypatch.setattr(dl, "_pick_netdisk_ua", lambda *a, **kw: "netdisk;P2SP;3.0.20.138")
    monkeypatch.setenv("VDL_NETDISK_CHUNK", str(CHUNK))

    task, store = _Task(tmp_path), _Store()
    dl._download_netdisk_direct(task, store, cookie="BDUSS=x")

    out = tmp_path / "demo.mp4"
    assert store.status == "completed", store.updates[-1]
    assert out.exists() and out.stat().st_size == TOTAL
    # 第一个请求是 1 字节探测（拿 Content-Range 里的总长），本身也必须是有界 Range
    assert calls[0] == "bytes=0-0", calls
    chunks = [c for c in calls if c != "bytes=0-0"]
    # 分块覆盖整份文件：首块 + 尾块（TOTAL = CHUNK + 10）
    assert f"bytes=0-{CHUNK - 1}" in chunks, chunks
    assert f"bytes={CHUNK}-{TOTAL - 1}" in chunks, chunks
    # 收尾要把中间文件与位图清干净
    assert not (tmp_path / "netdisk.part").exists()
    assert not (tmp_path / "netdisk.part.map").exists()


def test_netdisk_403_falls_back_to_unified_hint(tmp_path, monkeypatch):
    """整块 403（直链过期 / 登录态失效）→ 上屏统一文案，真实原因进日志。"""

    class _Bad(_Resp):
        def __init__(self):
            super().__init__(0, b"", 0)
            self.status_code = 403

    patch_http(monkeypatch, lambda *a, **kw: _Bad())
    monkeypatch.setattr(dl, "_pick_netdisk_ua", lambda *a, **kw: "netdisk;P2SP;3.0.20.138")

    task, store = _Task(tmp_path), _Store()
    dl._download_netdisk_direct(task, store, cookie="BDUSS=x")
    assert store.status == "failed"
    assert store.updates[-1].get("hint") == dl._user_error_text()
    assert any("403" in entry for entry in task.logs + [str(s) for s in task.steps])
