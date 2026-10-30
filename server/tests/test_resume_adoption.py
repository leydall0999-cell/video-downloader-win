"""断点跨任务复用回归测试（2026-10-30）。

用户最常见的动作不是点「继续下载」，而是把链接重新粘一遍。此前那会 create 出
新 task_id / 新工作目录，旧目录里的 .part 再也接不上（实测 169MB 白下）。

现在 TaskStore.create 会把「同 URL + 同 kind + 同归属 + 同清晰度 + 旧任务已停下」
的残留分片搬进新任务目录，并把新任务标成 resumable。

同时锁住：换清晰度 / 换链接 / 旧任务还在跑 / 换归属 一律不接管。
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tasks as tasks_mod  # noqa: E402

URL = "https://www.bilibili.com/video/BV1xx411c7mD"


def _store() -> tasks_mod.TaskStore:
    return tasks_mod.TaskStore(Path(tempfile.mkdtemp()))


def _fail_with_partial(st, *, url=URL, quality_key="best", owner_id="u1", name="a.mp4.part"):
    t = st.create(url=url, title="t", platform="B站", quality="1080P",
                  quality_key=quality_key, owner_id=owner_id)
    (t.workdir / name).write_bytes(b"x" * 4096)
    st.update(t.id, status="failed", error="网络中断")
    return t


def test_repaste_same_link_adopts_partials():
    st = _store()
    old = _fail_with_partial(st)
    new = st.create(url=URL, title="t", platform="B站", quality="1080P",
                    quality_key="best", owner_id="u1")
    assert new.resumable is True, "重贴同一链接必须接管断点"
    assert (new.workdir / "a.mp4.part").exists(), "分片必须搬进新任务目录"
    assert not old.workdir.exists() or not list(old.workdir.iterdir()), "旧任务目录应被腾空"
    assert st.get(old.id) is None, "旧任务记录应一并收掉，避免列表出现两张同链接的卡"


def test_netdisk_part_map_moves_together():
    """网盘 .part 是预分配容器，.part.map 的块位图必须跟着一起走，否则续传全错。"""
    st = _store()
    old = _fail_with_partial(st, name="netdisk.part")
    (old.workdir / "netdisk.part.map").write_bytes(b"\x00" * 16)
    new = st.create(url=URL, title="t", platform="B站", quality="1080P",
                    quality_key="best", owner_id="u1")
    assert (new.workdir / "netdisk.part").exists()
    assert (new.workdir / "netdisk.part.map").exists()


def test_no_adopt_when_quality_differs():
    st = _store()
    _fail_with_partial(st, quality_key="best")
    new = st.create(url=URL, title="t", platform="B站", quality="720P",
                    quality_key="720p", owner_id="u1")
    assert new.resumable is False, "换清晰度不能接旧分片（体积/格式都不是同一个文件）"


def test_no_adopt_when_url_differs():
    st = _store()
    _fail_with_partial(st)
    new = st.create(url="https://www.bilibili.com/video/BV1zz411c7mD", title="t",
                    platform="B站", quality="1080P", quality_key="best", owner_id="u1")
    assert new.resumable is False


def test_no_adopt_across_owners():
    st = _store()
    _fail_with_partial(st, owner_id="u1")
    new = st.create(url=URL, title="t", platform="B站", quality="1080P",
                    quality_key="best", owner_id="u2")
    assert new.resumable is False, "别人的断点不能接管（越权拿到他人分片）"


def test_no_adopt_while_donor_still_running():
    st = _store()
    t = st.create(url=URL, title="t", platform="B站", quality="1080P",
                  quality_key="best", owner_id="u1")
    (t.workdir / "a.mp4.part").write_bytes(b"x")
    st.update(t.id, status="downloading")
    new = st.create(url=URL, title="t", platform="B站", quality="1080P",
                    quality_key="best", owner_id="u1")
    assert new.resumable is False, "进行中的分片正在被写，不能被抢"
    assert (t.workdir / "a.mp4.part").exists()


def test_failed_with_partials_outlives_default_ttl():
    """1 小时 TTL 会把断点连同记录一起清掉 —— 失败+有分片的必须按 7 天保留。"""
    st = _store()
    t = _fail_with_partial(st)
    t.created_at = time.time() - 2 * 3600          # 两小时前失败
    st.purge_expired()
    assert st.get(t.id) is not None
    assert (t.workdir / "a.mp4.part").exists()


def test_failed_without_partials_still_purged():
    st = _store()
    t = st.create(url=URL, title="t", platform="B站", quality="1080P", owner_id="u1")
    st.update(t.id, status="failed", error="链接已失效")
    t.created_at = time.time() - 2 * 3600
    st.purge_expired()
    assert st.get(t.id) is None, "没有分片的失败任务不该长期占着列表"
