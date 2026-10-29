"""百度「会员 Cookie 优先」排序单元测试（离线，全程 mock）。

钉死 2026-10-29 用户实测根因修复（移植自 mac 仓 83d5c01 Change 1）：

现象：网盘下载任务绑的是**免费** cookie 而不是会员那枚 → 单连 ~128KB/s，
而会员号能到 ~3.7MB/s。

根因：解析候选只按「Cookie 字段数降序」排（_baidu_rank_key），完全不看会员等级。
免费号字段往往更多（38 vs 22），于是永远压会员号在前面；个人网盘通道
（resolve_personal）任何账号都给明文直链，第一个（免费）直接胜出。

契约：
  1. member_bduss_set 只返回「被标记为会员 + 未过 TTL」的 BDUSS；
  2. _baidu_login_cookies 里会员号排最前，**哪怕它字段更少**；
  3. 同档内仍按字段数降序（完整快照优先，省一次失败往返）；
  4. 没有会员标记时，排序退化成原来的「纯字段数」，不改变既有行为。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import app as _server_app  # noqa: E402,F401 — 必须先 import app：routers.cloud_drive 顶层
# `import app`，直接先导入 routers.cloud_drive 会撞 partially initialized module 循环导入
import cookie_pool  # noqa: E402
import routers.cloud_drive as rt  # noqa: E402


def _header(bduss: str, fields: int) -> str:
    """造一条字段数可控的 Cookie 头（BDUSS 之外补普通字段）。"""
    parts = [f"BDUSS={bduss}"]
    parts += [f"k{i}=v{i}" for i in range(max(fields - 1, 0))]
    return "; ".join(parts)


def test_member_bduss_set_only_returns_valid_members(tmp_path, monkeypatch):
    """只收「member=True 且未过 TTL」的 BDUSS：免费号、过期会员号都要排除。"""
    now = int(time.time())
    pool_file = tmp_path / "baidu.com.json"
    pool_file.write_text(json.dumps({
        "cookies": [
            {"header": _header("MEMBER_OK", 3), "ts": now, "member": True},
            {"header": _header("FREE_ONE", 9), "ts": now, "member": False},
            {"header": _header("MEMBER_STALE", 3), "ts": now - 40 * 24 * 3600, "member": True},
            {"header": _header("NO_MEMBER_FLAG", 3), "ts": now},
        ]
    }), encoding="utf-8")

    monkeypatch.setattr(cookie_pool, "_POOL_DIR", tmp_path)
    got = cookie_pool.member_bduss_set("baidu.com")
    assert got == {"MEMBER_OK"}, f"期望只有未过期的会员号，实际 {got}"


def test_member_ranks_first_even_with_fewer_fields(monkeypatch):
    """会员号字段更少也必须排最前 —— 这就是本次根因修复的核心断言。"""
    free_fat = _header("FREE_FAT", 9)      # 免费号，字段最多（原先靠字段数排第一）
    member_thin = _header("MEMBER_THIN", 2)  # 会员号，字段最少（原先被压到最后）
    free_mid = _header("FREE_MID", 4)

    monkeypatch.setattr(rt, "_baidu_local_cookies",
                        lambda: [free_fat, member_thin, free_mid])
    monkeypatch.setattr(rt, "_baidu_cloud_cookies", lambda: [])
    monkeypatch.setattr(rt, "member_bduss_set", lambda domain: {"MEMBER_THIN"})

    out = rt._baidu_login_cookies()
    assert out[0] == member_thin, f"会员号没排第一，实际首位={out[0][:40]}"
    # 同档内仍按字段数降序
    assert out[1:] == [free_fat, free_mid], "非会员档内没有按字段数降序"


def test_without_member_mark_order_degrades_to_field_count(monkeypatch):
    """池里没有任何会员标记时，排序必须退化成原来的「纯字段数」，不能打乱既有行为。"""
    a = _header("A", 2)
    b = _header("B", 7)
    c = _header("C", 4)

    monkeypatch.setattr(rt, "_baidu_local_cookies", lambda: [a, b, c])
    monkeypatch.setattr(rt, "_baidu_cloud_cookies", lambda: [])
    monkeypatch.setattr(rt, "member_bduss_set", lambda domain: set())

    assert rt._baidu_login_cookies() == [b, c, a]


def test_cloud_candidates_still_come_after_local(monkeypatch):
    """会员优先不能打破「本机候选整体在云端之前」这条老契约。"""
    local_free = _header("LOCAL_FREE", 3)
    cloud_member = _header("CLOUD_MEMBER", 2)

    monkeypatch.setattr(rt, "_baidu_local_cookies", lambda: [local_free])
    monkeypatch.setattr(rt, "_baidu_cloud_cookies", lambda: [cloud_member])
    monkeypatch.setattr(rt, "member_bduss_set", lambda domain: {"CLOUD_MEMBER"})

    out = rt._baidu_login_cookies()
    assert out[0] == local_free, "云端会员号抢到了本机候选前面"
    assert out[1] == cloud_member
