"""网盘 403 风控「自动断点续传」薄包装层单元测试（离线，全程 mock）。

钉死 2026-10-29 移植自 mac 仓 83d5c01 的 Change 2 契约：

背景：下载到一半撞百度「按 IP 瞬时风控」403，win 侧原来直接把任务判死，
必须用户手动点「重试」。新增 `_netdisk_run` 在 win 权威 403 实现（降档并发 +
3/6/12/24s 短冷却 + 单连接补漏 + 保断点）之上再包一层：跨分钟长冷却后自动续传。

必须守住的边界（这层最容易写坏）：
  1. 成功就收手，不冷却、不重试；
  2. 只有「风控中断」（seen403>0 → _netdisk_risk_blocked）才续传；
  3. 非风控失败（链接失效 31360 / 404 / 磁盘满）绝不重试 —— 越重试风控越死；
  4. 最多 3 轮，冷却阶梯 60 / 120 / 180s；
  5. 冷却期间用户取消 / 暂停立即退出，不再发起新轮次。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import downloader as d  # noqa: E402


class _Task:
    id = "t1"

    def __init__(self):
        self.cancel_requested = False
        self.pause_requested = False
        self.steps: list = []
        self.logs: list = []

    def add_step(self, *a, **kw):
        self.steps.append((a, kw))

    def log(self, msg):
        self.logs.append(msg)


class _Store:
    def __init__(self):
        self.updates: list = []

    def update(self, tid, **kw):
        self.updates.append((tid, kw))


def _run(rounds, cancel_at_cooldown=False):
    """rounds: 每轮的行为，元素为 'ok' / 'risk' / 'fail'。"""
    task, store = _Task(), _Store()
    cooldowns: list = []
    calls: list = []
    seq = iter(rounds)

    def fake_direct(t, s, cookie="", proxy=""):
        calls.append(1)
        action = next(seq, "ok")
        if action == "risk":
            t._netdisk_risk_blocked = True
        elif action == "fail":
            t._netdisk_risk_blocked = False       # 非风控失败：链接死 / 磁盘满
        else:
            t._netdisk_risk_blocked = False

    def fake_cooldown(seconds, t):
        cooldowns.append(seconds)
        if cancel_at_cooldown:
            t.cancel_requested = True

    with mock.patch.object(d, "_download_netdisk_direct", side_effect=fake_direct), \
         mock.patch.object(d, "_netdisk_cooldown", side_effect=fake_cooldown):
        d._netdisk_run(task, store, cookie="ck", proxy="")
    return calls, cooldowns, task, store


def test_success_stops_immediately():
    calls, cooldowns, _task, _store = _run(["ok"])
    assert len(calls) == 1
    assert cooldowns == [], "成功的下载不该触发任何冷却"


def test_risk_blocked_triggers_cooldown_and_resume():
    """第一轮撞风控 → 冷却 60s → 续传成功，只跑两轮。"""
    calls, cooldowns, task, store = _run(["risk", "ok"])
    assert len(calls) == 2, "风控中断后没有自动续传"
    assert cooldowns == [60]
    steps = [str(s[0]) for s in task.steps]
    assert any("断点续传" in s for s in steps), f"缺少自动续传的进度提示：{steps}"
    # 续传前要把上一轮落的 failed 拉回 downloading，否则前端停在失败态
    assert any(kw.get("status") == "downloading" for _tid, kw in store.updates)


def test_non_risk_failure_never_retries():
    """非风控失败（链接失效/磁盘满）直接结束 —— 无谓重试只会把风控越撞越死。"""
    calls, cooldowns, _task, _store = _run(["fail"])
    assert len(calls) == 1
    assert cooldowns == []


def test_max_three_rounds_with_ladder_cooldown():
    """连续风控：最多 3 轮，冷却 60 / 120（第三轮后不再冷却）。"""
    calls, cooldowns, _task, _store = _run(["risk", "risk", "risk"])
    assert len(calls) == 3, "轮次超过 3 轮"
    assert cooldowns == [60, 120], f"冷却阶梯不对：{cooldowns}"


def test_cancel_during_cooldown_stops_further_rounds():
    """冷却期间用户取消 → 立即退出，不再发起新轮次。"""
    calls, cooldowns, _task, _store = _run(["risk", "ok"], cancel_at_cooldown=True)
    assert len(calls) == 1, "取消后仍然发起了续传"
    assert cooldowns == [60]


def test_cooldown_is_interruptible():
    """_netdisk_cooldown 必须分段睡、可被取消打断（否则退出要等满 180s）。"""
    task = _Task()
    slept: list = []

    def fake_sleep(seconds):
        slept.append(seconds)
        task.cancel_requested = True      # 第一段睡完就取消

    with mock.patch.object(d, "_netdisk_sleep", side_effect=fake_sleep):
        d._netdisk_cooldown(180, task)

    assert len(slept) == 1, f"取消后仍在睡：{slept}"
