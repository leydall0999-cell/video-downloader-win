"""百度直链**并发分块**下载回归（2026-10-28）。

背景：用户实测 Mac 侧能破 10MB/s、Windows 侧只有 0.09~0.30MB/s。逐项测下来
根因不在平台，而在**连接数** —— 百度对每条 TCP 连接单独限速，实测恒速
~0.30MB/s，8/16/24/32/48 连严格线性（2.47/5.33/7.45/9.63/14.55 MB/s）。
旧实现 `while True: 4MB 有界 Range` 是串行单连接，所以永远卡在单连接速率。

这三条测试守的分别是：① 真的开了多条连接；② 断点续传按位图跳过已完成块；
③ 暂停保住 .part + 位图（多线程乱序写盘，不能再拿文件大小当进度）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import requests

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import downloader as dl  # noqa: E402
from test_netdisk_chunked import _Resp, _Store, _Task, patch_http  # noqa: E402

# ── 2026-10-28 并发提速回归（百度按**每条连接**限速 ~0.30MB/s，只能靠叠加连接）──

def test_netdisk_runs_parallel_connections(tmp_path, monkeypatch):
    """守「别退回单连接」：8 块文件必须有多条连接同时在飞。

    实测：1 连 0.30MB/s、8 连 2.47、16 连 5.33、24 连 7.45、32 连 9.63。
    一旦有人把实现改回串行 while，这条会立刻红。
    """
    import threading as _th
    import time as _t

    ch = 256 * 1024
    total = ch * 8
    lock = _th.Lock()
    inflight = [0]
    peak = [0]

    class _Slow(_Resp):
        def iter_content(self, size):
            with lock:
                inflight[0] += 1
                peak[0] = max(peak[0], inflight[0])
            try:
                for piece in super().iter_content(size):
                    _t.sleep(0.02)          # 拉长窗口，让别的线程有机会同时进来
                    yield piece
            finally:
                with lock:
                    inflight[0] -= 1

    def fake_get(url, headers=None, **kw):
        rng = (headers or {}).get("Range", "")
        start, end = (int(x) for x in rng[len("bytes="):].split("-"))
        n = min(end - start + 1, total - start)
        return _Slow(start, b"\0" * n, total)

    patch_http(monkeypatch, fake_get)
    monkeypatch.setattr(dl, "_pick_netdisk_ua", lambda *a, **kw: "netdisk;P2SP;3.0.20.138")
    monkeypatch.setenv("VDL_NETDISK_CHUNK", str(ch))
    monkeypatch.setenv("VDL_NETDISK_CONN", "4")

    task, store = _Task(tmp_path), _Store()
    task.total_bytes = 0
    dl._download_netdisk_direct(task, store, cookie="BDUSS=x")

    assert store.status == "completed", store.updates[-1]
    assert (tmp_path / "demo.mp4").stat().st_size == total
    assert peak[0] >= 2, f"未观察到并发连接（peak={peak[0]}）——很可能退回单连接了"


def test_netdisk_resume_skips_done_blocks(tmp_path, monkeypatch):
    """断点续传：位图里已完成的块不再重下，且已有字节不被覆盖。"""
    ch = 256 * 1024
    total = ch * 4
    part = tmp_path / "netdisk.part"
    bmap = tmp_path / "netdisk.part.map"
    part.write_bytes(b"\xaa" * (ch * 2) + b"\0" * (ch * 2))
    bmap.write_bytes(bytes([1, 1, 0, 0]))

    calls = []

    def fake_get(url, headers=None, **kw):
        rng = (headers or {}).get("Range", "")
        calls.append(rng)
        start, end = (int(x) for x in rng[len("bytes="):].split("-"))
        n = min(end - start + 1, total - start)
        return _Resp(start, b"\xbb" * n, total)

    patch_http(monkeypatch, fake_get)
    monkeypatch.setattr(dl, "_pick_netdisk_ua", lambda *a, **kw: "netdisk;P2SP;3.0.20.138")
    monkeypatch.setenv("VDL_NETDISK_CHUNK", str(ch))

    task, store = _Task(tmp_path), _Store()
    task.total_bytes = total
    dl._download_netdisk_direct(task, store, cookie="BDUSS=x")

    out = tmp_path / "demo.mp4"
    assert store.status == "completed", store.updates[-1]
    data = out.read_bytes()
    assert data[:ch * 2] == b"\xaa" * (ch * 2), "已完成块被重下覆盖"
    assert data[ch * 2:] == b"\xbb" * (ch * 2)
    fetched = set(c for c in calls if c != "bytes=0-0")
    assert fetched == {f"bytes={ch * 2}-{ch * 3 - 1}", f"bytes={ch * 3}-{total - 1}"}, fetched


def test_netdisk_pause_keeps_part_and_bitmap(tmp_path, monkeypatch):
    """暂停必须保住 .part + 位图，否则多线程续传无从判断已完成块。"""
    ch = 256 * 1024
    total = ch * 8
    task, store = _Task(tmp_path), _Store()
    task.total_bytes = 0
    done_blocks = [0]

    class _Slow(_Resp):
        def iter_content(self, size):
            for piece in super().iter_content(size):
                yield piece
            # 恰好下完一块再暂停：验证「已完成块」的位图在暂停后仍被保住
            done_blocks[0] += 1
            if done_blocks[0] >= 1:
                task.pause_requested = True

    def fake_get(url, headers=None, **kw):
        rng = (headers or {}).get("Range", "")
        start, end = (int(x) for x in rng[len("bytes="):].split("-"))
        n = min(end - start + 1, total - start)
        return _Slow(start, b"\0" * n, total)

    patch_http(monkeypatch, fake_get)
    monkeypatch.setattr(dl, "_pick_netdisk_ua", lambda *a, **kw: "netdisk;P2SP;3.0.20.138")
    monkeypatch.setenv("VDL_NETDISK_CHUNK", str(ch))
    monkeypatch.setenv("VDL_NETDISK_CONN", "2")

    dl._download_netdisk_direct(task, store, cookie="BDUSS=x")

    assert store.status == "paused", store.updates[-1]
    assert (tmp_path / "netdisk.part").exists(), "暂停后 .part 被删了，无法续传"
    assert (tmp_path / "netdisk.part.map").exists(), "暂停后位图被删了，续传会从头下"
    assert not (tmp_path / "demo.mp4").exists()
