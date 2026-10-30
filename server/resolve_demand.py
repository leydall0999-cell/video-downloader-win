"""解析需求登记 —— Downie「用户报一个写一个」机制的地基。

背景（2026-10-17 竞品分析结论）：Downie 的护城河不是 591 个手写提取器这个
数字，而是十年来「用户要一个写一个」的持续机制。VDL 缺的是把这个闭环的
**第一环**补上：解析失败不再消失在对话框里，而是落库成可聚合的需求信号。

哪些失败算「需求」？只有两类（在 downloader._friendly_error 对应分支里调用）：
  - pending_extractor：平台已收录但 yt-dlp 无提取器（白名单内空白站点）；
  - unsupported_platform：完全未知域名且 yt-dlp 也不行（第三方聚合站等）。
其余失败（403 / 超时 / 会员墙）是**解析能力问题**，不是新站点需求，不记。

设计约束：
  - 存储 JSONL（append-only）：天然抗并发、无 schema 迁移、坏了不拖垮主流程；
  - 聚合口径「站点×周」（2026-10-23 起，原为「站点×天」）：同一 host 同一
    自然周（周一为起点）只计 1 次需求（重试 10 次 ≠ 10 个需求；不同周反复出现
    说明持续有人要，照计）——防刷也防低估。按周而非按天：同一站点一天内被
    几个用户同时要，并不比一周内持续被要更有开发价值，周计数才反映「持续性」；
  - 写入双节流：同 host 60s 内只落一条（内存态）+ 文件超 2MB 截断保底，
    保证一个失控站点也刷不爆磁盘；
  - URL 只留前 200 字符当「样例链接」供开发直接测试用；不记 Cookie/token。
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

LOG_NAME = ".resolve_demand.log"
DEFAULT_WINDOW_DAYS = 84           # 默认看 12 周：按周计数窗口太短会全是 1 分
MAX_BYTES = 2 * 1024 * 1024          # 超限截断（保留尾部 KEEP_TAIL 行）
KEEP_TAIL = 2000
THROTTLE_SECONDS = 60.0              # 同 host 写入节流窗口
MAX_URL_CHARS = 200

_lock = threading.Lock()
_last_write: dict[str, float] = {}   # host -> 上次落库 ts（进程内节流）

# 可选的上报钩子：注册后，每次「真的落库」的需求会异步上报到授权中心，
# 实现 Mac/Win 需求统一收口（否则各端只看自己本机日志，互不可见）。
_reporter = None


def set_demand_reporter(fn) -> None:
    """注册一个 ``(host, url, category) -> None`` 上报回调（由 cloud_account 注入）。

    回调在后台线程里异步执行，异常一律静默，绝不影响主流程。
    """
    global _reporter
    _reporter = fn


def _demand_path(base_dir: Path | None = None) -> Path:
    base = Path(base_dir) if base_dir else Path(
        os.environ.get("VDL_DATA_DIR") or (Path.home() / ".video-downloader"))
    return base / LOG_NAME


def record_demand(host: str, url: str = "", category: str = "unsupported_platform",
                  base_dir: Path | None = None) -> bool:
    """登记一条站点需求。返回是否真写了（节流命中返回 False）。

    任何失败都静默吞掉——需求登记是旁路统计，绝不能影响用户正在等的报错。
    """
    host = (host or "").strip().lower()
    if not host:
        return False
    now = time.time()
    with _lock:
        last = _last_write.get(host, 0.0)
        if now - last < THROTTLE_SECONDS:
            return False
        _last_write[host] = now
    try:
        path = _demand_path(base_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": int(now),
            "host": host,
            "url": (url or "")[:MAX_URL_CHARS],
            "category": category,
        }
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        # 超限截断：读出来 → 保留尾部 → **整文件重写**（早先版本只算了尾部却
        # 仍用 append 追加，截断等于没做，文件会无限涨——2026-10-17 守卫抓出）。
        if path.exists() and path.stat().st_size > MAX_BYTES:
            data = path.read_bytes()
            tail = b"".join(data.splitlines(True)[-KEEP_TAIL:])
            with open(path, "wb") as f:
                f.write(tail)
                f.write(line.encode("utf-8"))
        else:
            with open(path, "ab") as f:
                f.write(line.encode("utf-8"))
    except Exception:
        return False
    # 真正落库的需求，异步上报授权中心（旁路信号，失败静默）。
    if _reporter is not None:
        try:
            _reporter(host, (url or "")[:MAX_URL_CHARS], category)
        except Exception:
            pass
    return True


def _week_key(ts: float) -> str:
    """该时间戳所在自然周（周一为起点）的周一日期串，作为「周」去重键。"""
    try:
        d = datetime.fromtimestamp(float(ts)).date()
    except (OSError, OverflowError, ValueError):
        return ""
    return (d - timedelta(days=d.weekday())).isoformat()


def demand_summary(days: int = DEFAULT_WINDOW_DAYS, base_dir: Path | None = None) -> list[dict]:
    """按站点聚合需求榜：[{host, weeks_count, days_count, total, weeks, last_ts, last_url, categories}]。

    weeks_count = 该站点在窗口内被记录的**不同周数**（周一为界，站点级热度，重试免疫）；
    days_count  = 同值别名（旧字段名，保留给未升级的对端/前端，勿新增引用）；
    total = 窗口内原始记录数（含同一周多条，供参考）；
    weeks = 命中周的周一日期列表（新→旧，最多 12 条，供界面展示趋势）；
    按 weeks_count 降序，再按 last_ts 降序。
    """
    path = _demand_path(base_dir)
    if not path.exists():
        return []
    cutoff = time.time() - max(1, days) * 86400
    per_host: dict[str, dict] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                ts = float(rec.get("ts") or 0)
                if ts < cutoff:
                    continue
                host = str(rec.get("host") or "").lower()
                if not host:
                    continue
                week = _week_key(ts) or time.strftime("%Y-%m-%d", time.localtime(ts))
                agg = per_host.setdefault(host, {
                    "host": host, "weeks": set(), "total": 0,
                    "last_ts": 0.0, "last_url": "", "categories": {},
                })
                agg["weeks"].add(week)
                agg["total"] += 1
                if ts >= agg["last_ts"]:
                    agg["last_ts"] = ts
                    agg["last_url"] = str(rec.get("url") or "")[:MAX_URL_CHARS]
                cat = str(rec.get("category") or "unknown")
                agg["categories"][cat] = agg["categories"].get(cat, 0) + 1
    except Exception:
        return []
    out = []
    for host, agg in per_host.items():
        weeks = sorted(agg["weeks"], reverse=True)
        out.append({
            "host": host,
            "weeks_count": len(weeks),
            "days_count": len(weeks),          # 兼容别名（旧前端/旧对端只认这个键）
            "weeks": weeks[:12],
            "total": agg["total"],
            "last_ts": int(agg["last_ts"]),
            "last_url": agg["last_url"],
            "categories": agg["categories"],
        })
    out.sort(key=lambda r: (-r["weeks_count"], -r["last_ts"]))
    return out
