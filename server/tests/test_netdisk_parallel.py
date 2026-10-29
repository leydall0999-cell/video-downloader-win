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


# ── 2026-10-29 实测翻车回归：24 连下 361MB，第 15 块起 HTTP 403 ──
# 百度按「连接数 / 时间窗」限流，不是权限问题：同一账号同一 dlink，单连接从不 403。
# 旧实现一处 403 就判整只任务死刑，还把 .part 删了 —— 361MB 下到 17% 全丢。

class _Forbidden:
    """403 响应桩（不走 _Resp：那条路径是 206）。"""

    status_code = 403

    def __init__(self):
        self.headers = {"Retry-After": "1"}
        self.url = "https://d.pcs.baidu.com/file/xxx"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, size):
        return iter(())


def _rng_of(headers):
    rng = (headers or {}).get("Range", "")
    return int(rng[len("bytes="):].split("-")[0]), int(rng[len("bytes="):].split("-")[1])


def test_netdisk_403_retries_then_succeeds(tmp_path, monkeypatch):
    """403 必须靠退避重试缓过来，而不是一次就整只任务失败。"""
    import threading as _th

    ch = 256 * 1024
    total = ch * 3
    tried = {}
    lock = _th.Lock()

    def fake_get(url, headers=None, **kw):
        st, en = _rng_of(headers)
        if en - st == 0:                       # 1 字节探测
            return _Resp(0, b"\0", total)
        with lock:
            tried[st] = tried.get(st, 0) + 1
            n = tried[st]
        if n <= 2:                             # 前两次撞限流
            return _Forbidden()
        return _Resp(st, b"\xcc" * (en - st + 1), total)

    patch_http(monkeypatch, fake_get)
    monkeypatch.setattr(dl, "_pick_netdisk_ua", lambda *a, **kw: "netdisk;P2SP;3.0.20.138")
    monkeypatch.setattr(dl, "_netdisk_sleep", lambda s: None)   # 退避不真睡
    monkeypatch.setenv("VDL_NETDISK_CHUNK", str(ch))
    monkeypatch.setenv("VDL_NETDISK_CONN", "2")

    task, store = _Task(tmp_path), _Store()
    task.total_bytes = 0
    dl._download_netdisk_direct(task, store, cookie="BDUSS=x")

    assert store.status == "completed", store.updates[-1]
    assert (tmp_path / "demo.mp4").stat().st_size == total


def test_netdisk_403_keeps_resume_state(tmp_path, monkeypatch):
    """某块始终 403：任务判失败，但 .part + 位图必须留着，重试才不用从头下。"""
    ch = 256 * 1024
    total = ch * 3
    bad_start = ch * 2                          # 第 3 块永远 403

    def fake_get(url, headers=None, **kw):
        st, en = _rng_of(headers)
        if en - st == 0:
            return _Resp(0, b"\0", total)
        if st == bad_start:
            return _Forbidden()
        return _Resp(st, b"\xdd" * (en - st + 1), total)

    patch_http(monkeypatch, fake_get)
    monkeypatch.setattr(dl, "_pick_netdisk_ua", lambda *a, **kw: "netdisk;P2SP;3.0.20.138")
    monkeypatch.setattr(dl, "_netdisk_sleep", lambda s: None)
    monkeypatch.setenv("VDL_NETDISK_CHUNK", str(ch))
    monkeypatch.setenv("VDL_NETDISK_CONN", "2")

    task, store = _Task(tmp_path), _Store()
    task.total_bytes = 0
    dl._download_netdisk_direct(task, store, cookie="BDUSS=x")

    assert store.status == "failed", store.updates[-1]
    assert not (tmp_path / "demo.mp4").exists()
    part, bmap = tmp_path / "netdisk.part", tmp_path / "netdisk.part.map"
    assert part.exists(), "失败时把 .part 删了 —— 重试只能从头下"
    assert bmap.exists(), "失败时把位图删了 —— 续传无从判断已完成块"
    assert sum(bmap.read_bytes()) == 2, "已完成的 2 块应保留在位图里"
    assert "断点" in (store.updates[-1].get("hint") or "")


def test_netdisk_conn_count_capped_for_browser_ua(monkeypatch):
    """浏览器 UA（网页下载通道）必须压低并发 —— 24 连会在第 15 块起被 403。

    实测对照：同一台机器、同一时段，P2SP 通道 32 连零 403；浏览器通道 24 连必撞。
    """
    chunk = 4 * 1024 * 1024
    big = chunk * 200

    monkeypatch.delenv("VDL_NETDISK_CONN", raising=False)
    assert dl._netdisk_conn_count(big, chunk, "netdisk;P2SP;3.0.20.138") == 12
    assert dl._netdisk_conn_count(big, chunk, "Mozilla/5.0 (Macintosh) Chrome/131") == 4

    # 小文件按块数收敛，不开多余连接
    assert dl._netdisk_conn_count(chunk * 2, chunk, "netdisk;P2SP;3.0.20.138") == 2

    # 环境变量仍然可以强行覆盖（排查用）
    monkeypatch.setenv("VDL_NETDISK_CONN", "24")
    assert dl._netdisk_conn_count(big, chunk, "Mozilla/5.0 Chrome/131") == 24
