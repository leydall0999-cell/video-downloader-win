"""下载任务的内存状态仓库。

只保存"当前进程内"的任务，重启即清空；文件落在 downloads/<task_id>/ 下，
配合 TTL 定期清理，避免磁盘无限增长。
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

from atomic_io import atomic_write_json

TaskStatus = Literal["pending", "downloading", "merging", "paused", "pausing",
                     "completed", "failed", "canceled"]
StepStatus = Literal["pending", "running", "done", "error"]

# 终态：TTL 清理（purge_expired）只处理这些状态，进行中的一律跳过。
# 🔴 安全审计 2026-10-11（P2-18）：此前用「非 completed 即删」的判据，
#    会把下载超过 1 小时的任务就地删除。
_TERMINAL_STATUSES = frozenset({"completed", "failed", "canceled"})

TASK_TTL_SECONDS = 60 * 60  # 成品文件保留 1 小时
# 🔴 断点保留期（2026-10-30）：1 小时只够「成品」，不够「断点」。
#    用户最常做的不是点「继续下载」，而是**重新粘贴一遍链接**——那会 create 出新任务、
#    新工作目录，旧分片就此孤悬（当天实测：314MB 下到 44.6%（169MB）后重贴链接从 0 开始）。
#    现在给「失败 + 磁盘上确有分片」的任务 7 天保留期，重贴链接时能认领回来。
RESUME_TTL_SECONDS = 7 * 24 * 60 * 60
TASK_ID_LENGTH = 16
TASK_DIR_PATTERN = re.compile(rf"^[0-9a-f]{{{TASK_ID_LENGTH}}}$")
_MAX_LOG_LINES = 200

# ---- 落盘持久化（B2，2026-10-02）----
# 任务状态写「数据目录」/tasks_state.json（不再写 DOWNLOAD_DIR，见 TaskStore 的
# TCC 说明）：App 重启后未完成任务恢复为
# 「失败 + 可续传」（磁盘分片本来就在，同任务点继续即可续），完成任务保留
# 最近 50 条元数据（文件仍在 TTL 内才能重新保存）。
STATE_FILENAME = "tasks_state.json"
_PERSIST_INTERVAL = 2.0        # 进度类变更的落盘节流（秒）
_MAX_PERSISTED_COMPLETED = 50  # 状态文件里最多保留多少条已完成任务

_PERSIST_FIELDS = (
    "url", "title", "platform", "quality", "kind", "quality_key", "status",
    "progress", "downloaded_bytes", "total_bytes", "filename", "filesize",
    "error", "hint", "created_at", "extract_mode",
    "concurrent_fragments", "downloader_type", "cookie", "proxy", "referer",
    "source_url", "play_url", "watch_options", "is_hls", "resumable",
    "file_expired", "owner_id", "children", "name_hint",
)


_PARTIAL_SUFFIXES = (".part", ".aria2", ".ytdl")


def _is_partial_name(name: str) -> bool:
    """是不是「可续传的分片/控制文件」（与 downloader._has_partial 同口径）。

    🔴 必须包含 `netdisk.part.map`（网盘断点的块位图）：它不以 `.part` 结尾，只靠后缀
    判断会被漏掉 —— 于是 .part 搬走了、位图留在旧目录，续传会按全零位图重下已完成的块
    （或直接判数据错）。用「含 `.part` 子串」覆盖 `.part` / `.part.map` / `.part-FragN`
    这几类，同时不会误伤 `departure.mp4` 这种正常文件名（`.` 是名字的一部分）。
    """
    return name.endswith(_PARTIAL_SUFFIXES) or ".Frag" in name or ".part" in name


def _has_partial_file(workdir: Path | None) -> bool:
    """工作目录里是否残留可续传的部分文件（与 downloader._has_partial 同口径，避免循环导入）。"""
    if not workdir or not workdir.is_dir():
        return False
    try:
        for p in workdir.iterdir():
            if p.is_file() and _is_partial_name(p.name):
                return True
        return False
    except OSError:
        return False


@dataclass
class DownloadTask:
    id: str
    url: str
    title: str
    platform: str
    quality: str
    # 任务类型（2026-10-12 图片下载 P0）："video"=yt-dlp 视频流水线；
    # "image"=image_download.run_image_download 直链落盘。决定重试/暂停走哪条
    # 执行器（routers/core.py 按此分流），前端据此显示图片徽标。
    kind: str = "video"
    quality_key: str = "best"          # 原始清晰度 key（如 best/1080/audio），重试时用它重下
    status: TaskStatus = "pending"
    progress: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed: float = 0.0
    eta: int = 0
    filename: str = ""
    filesize: int = 0
    error: str = ""
    hint: str = ""
    # 慢速告警：下载中速率持续过低时由看门狗写入，前端据此弹出「建议换清晰度/代理」提示
    slow_warning: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    cancel_requested: bool = False
    pause_requested: bool = False
    workdir: Path | None = None
    filepath: Path | None = None
    # 提取文案：模式（"spoken"/"description"/"both"/""）+ 状态 + 结果
    extract_mode: str = ""
    extract_status: str = "none"   # none / running / done / error
    extracted_text: dict = field(default_factory=dict)
    source_url: str = ""
    # 断点续传：保存首次下载时的关键参数，供「重试/继续」时原样复用（避免从 0 重下）
    concurrent_fragments: int = 0
    downloader_type: str = ""
    cookie: str = ""
    proxy: str = ""
    # 续传标记：取消/失败时若工作目录残留 .part 分片，置 True，前端据此提示「可断点续传」
    resumable: bool = False
    # B5（2026-10-02）：成品已随 TTL 清理的历史条目（completed 但文件没了）。
    # True 时保留卡片作历史记录，前端显示「成品已清理」+「重新下载」入口。
    file_expired: bool = False
    # A3（2026-10-02）：取消原因（""=用户取消 / "stall"=看门狗停滞 / "timeout"=硬超时）。
    # 供 DownloadCanceled 处理分支区分「用户取消」与「系统终止」，给出可行动的错误信息。
    cancel_reason: str = ""
    # 在线观看：解析时提取的播放地址与清晰度列表，存入任务后前端可在任务面板直接打开观看
    play_url: str = ""
    watch_options: list[dict] = field(default_factory=list)
    is_hls: bool = False
    # CDP 嗅探直链的防盗链 Referer（嗅探来源页），重试/续传时原样复用
    referer: str = ""
    # 任务归属（安全审计 P0-2，2026-10-11）：创建者的账号/设备标识。
    # 此前下载任务链**没有任何归属校验**，/api/tasks 会返回全站任务、任何人可
    # 下载他人成品或取消他人任务（IDOR）。空串 = 升级前的历史遗留任务，
    # 为兼容仍可访问；新任务一律由 app._task_owner(request) 写入。
    owner_id: str = ""
    # 用量明细标识（2026-10-20）：下单时 use_daily 生成，下载结束后按它把
    # 「是否真的下载完成并保存到本机」回写到账本（本机 + 授权中心）。
    # 只在本次进程内有效 —— 不写进 _PERSIST_FIELDS：重启后任务已终止，
    # 再回写没有意义，留空即「结果未上报」（面板显示灰色未上报，而不是假成功）。
    usage_ref: str = ""
    # 过程展示：结构化步骤 + 文本日志
    steps: list[dict] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    # 🔴 2026-10-12 图片分享页展开（用户报「没看懂，图片呢」）：
    #   分享链接展开出的子任务在 1 秒内就下完，前端轮询到时它们已是 completed，
    #   而 syncMissingCards 只给「活跃状态」补卡片 ⇒ 图片在服务端却一张都不显示。
    #   把子任务 ID 挂在父任务上随 to_public_dict 下发，前端据此建卡片（见 app.js）。
    children: list[str] = field(default_factory=list)
    # 🔴 2026-10-12 落盘文件名的**可读名提示**（用户报「一堆文件夹我上哪找去」）：
    #   分享链接展开的图片，其 URL 末段是 CDN 的哈希串
    #   （如 1040g2sg31...!nd_dft_wlteh_webp_3.webp），落盘名与卡片标题都是一串天书。
    #   父任务在展开时写入 name_hint（"图片-1"…），执行器据此命名落盘文件。
    #   刻意做成独立字段而不是复用 title：title 会在完成时被改写为最终文件名，
    #   且用户直贴的图片直链不该被改名。
    name_hint: str = ""

    def __post_init__(self):
        # 初始任务自动带有"排队等待"步骤，让前端过程面板一创建就有东西展示
        if not self.steps:
            self.add_step("排队等待", "pending")

    @property
    def is_finished(self) -> bool:
        return self.status in ("completed", "failed", "canceled")

    def add_step(self, name: str, status: StepStatus = "running", detail: str = "") -> None:
        """记录/更新一个执行步骤。同名步骤会覆盖状态，避免列表无限膨胀。"""
        now = time.time()
        for step in self.steps:
            if step.get("name") == name:
                step["status"] = status
                step["detail"] = detail
                step["updated_at"] = now
                return
        self.steps.append({
            "name": name,
            "status": status,
            "detail": detail,
            "created_at": now,
            "updated_at": now,
        })

    def log(self, line: str) -> None:
        """追加一行带时间戳的运行日志，限制最大行数防止内存泄漏。"""
        if not line:
            return
        ts = time.strftime("%H:%M:%S", time.localtime())
        self.logs.append(f"{ts}  {line.strip()}")
        if len(self.logs) > _MAX_LOG_LINES:
            self.logs[:] = self.logs[-_MAX_LOG_LINES:]

    def to_public_dict(self) -> dict:
        d = {
            "task_id": self.id,
            "status": self.status,
            "kind": self.kind,
            "progress": round(self.progress, 2),
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "speed": round(self.speed, 2),
            "eta": self.eta,
            "title": self.title,
            "platform": self.platform,
            "quality": self.quality,
            "filename": self.filename,
            "filesize": self.filesize,
            "error": self.error,
            "hint": self.hint,
            "slow_warning": self.slow_warning,
            "extract_mode": self.extract_mode,
            "extract_status": self.extract_status,
            "extracted_text": self.extracted_text,
            "source_url": self.source_url,
            "resumable": self.resumable,
            "file_expired": self.file_expired,
            "play_url": self.play_url,
            "watch_options": self.watch_options,
            "is_hls": self.is_hls,
            "steps": self.steps,
            "logs": self.logs,
            "children": list(self.children or []),
            "name_hint": self.name_hint,
        }
        # 桌面版额外下发成品的**本机绝对路径**（用户报「你得让我知道在哪里啊，
        # 一堆文件夹我上哪找去」）：前端据此显示「已下载到本机：<路径>」并提供
        # 「在访达中显示」直达。网页版**绝不下发** —— 那是服务器上的路径，对网页
        # 用户毫无意义，还把部署目录结构泄露出去。能力探测失败也不影响序列化。
        try:
            from platform_model import is_desktop
            if is_desktop() and self.filepath:
                d["local_path"] = str(self.filepath)
        except Exception:  # noqa: BLE001
            pass
        return d


class TaskStore:
    """线程安全的任务表。状态落盘 tasks_state.json，重启后可恢复。

    🔴 2026-10-03：状态文件与工作目录**解耦**。原来两者同在 DOWNLOAD_DIR
    （~/Downloads/VideoDownloader），而「下载」是 macOS TCC 保护目录：每次重新
    ad-hoc 签名安装后系统都会重新索要授权，无人点击时 open() 永久阻塞
    （表现：App 进程在、但 8321 端口永不监听，接口全部超时）。工作目录是用户
    可见的下载落地处，必须留在 Downloads；状态文件是 App 内部数据，改为写到
    数据目录（VDL_DATA_DIR / ~/.video-downloader），彻底避开 TCC。
    旧位置的 tasks_state.json 不迁移（读取它等于又碰 Downloads）——那只是历史
    元数据，磁盘分片仍在原处，用同一链接重新发起即续传。
    """

    def __init__(self, root: Path, state_dir: Optional[Path] = None) -> None:
        self._root = root
        self._tasks: dict[str, DownloadTask] = {}
        self._lock = threading.Lock()
        self._root.mkdir(parents=True, exist_ok=True)
        if state_dir is not None:
            state_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = (state_dir or root) / STATE_FILENAME
        self._last_persist = 0.0
        self._persist_dirty = False
        self._load_state()

    # ---- 落盘 ----

    def _snapshot_locked(self) -> list[dict]:
        rows = []
        for t in self._tasks.values():
            row = {k: getattr(t, k) for k in _PERSIST_FIELDS}
            row["id"] = t.id
            row["filepath"] = str(t.filepath) if t.filepath else ""
            rows.append(row)
        # 状态文件只保留最近 N 条已完成，未完成任务全量保留（它们才是恢复的意义所在）
        finished = sorted(
            (r for r in rows if r["status"] in ("completed", "failed", "canceled")),
            key=lambda r: r.get("created_at") or 0, reverse=True)
        if len(finished) > _MAX_PERSISTED_COMPLETED:
            drop = {r["id"] for r in finished[_MAX_PERSISTED_COMPLETED:]}
            rows = [r for r in rows if r["id"] not in drop]
        return rows

    def _persist_locked(self, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self._last_persist) < _PERSIST_INTERVAL:
            self._persist_dirty = True
            return
        self._last_persist = now
        self._persist_dirty = False
        # 原子落盘（守卫 test_config_atomic_write 抓过固定名 .tmp 的撕裂反证）：
        # tasks_state.json 与配额/账号表同级——半截 JSON 会让重启恢复直接丢光。
        try:
            atomic_write_json(self._state_path, {
                "version": 1, "saved_at": now, "tasks": self._snapshot_locked(),
            })
        except OSError:
            pass

    def flush(self) -> None:
        """立即落盘（供关键节点与关停前调用）。"""
        with self._lock:
            self._persist_locked(force=True)

    def _load_state(self) -> None:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            return
        now = time.time()
        restored = 0
        for row in raw.get("tasks", []):
            tid = str(row.get("id") or "")
            if not TASK_DIR_PATTERN.match(tid) or tid in self._tasks:
                continue
            status = row.get("status")
            workdir = self._root / tid
            fp = None
            file_expired = bool(row.get("file_expired"))
            if status == "completed" and not file_expired:
                fp = Path(str(row.get("filepath") or "")) if row.get("filepath") else None
                if not fp or not fp.exists():
                    # B5：成品已随 TTL 清理 → 不再丢弃，恢复为历史条目（可重新下载）
                    file_expired = True
            if status != "completed" and not workdir.is_dir():
                continue  # 未完成任务没有工作目录：只剩元数据没有意义
            created_at = float(row.get("created_at") or now)
            task = DownloadTask(
                id=tid, url=str(row.get("url") or ""), title=str(row.get("title") or ""),
                platform=str(row.get("platform") or ""), quality=str(row.get("quality") or ""),
                kind=str(row.get("kind") or "video"),
                quality_key=str(row.get("quality_key") or "best"),
                status=status if isinstance(status, str) else "pending",
                progress=float(row.get("progress") or 0.0),
                downloaded_bytes=int(row.get("downloaded_bytes") or 0),
                total_bytes=int(row.get("total_bytes") or 0),
                filename=str(row.get("filename") or ""),
                filesize=int(row.get("filesize") or 0),
                error=str(row.get("error") or ""), hint=str(row.get("hint") or ""),
                created_at=created_at,
                extract_mode=str(row.get("extract_mode") or ""),
                concurrent_fragments=int(row.get("concurrent_fragments") or 0),
                downloader_type=str(row.get("downloader_type") or ""),
                cookie=str(row.get("cookie") or ""), proxy=str(row.get("proxy") or ""),
                referer=str(row.get("referer") or ""),
                owner_id=str(row.get("owner_id") or ""),
                source_url=str(row.get("source_url") or ""),
                play_url=str(row.get("play_url") or ""),
                watch_options=list(row.get("watch_options") or []),
                is_hls=bool(row.get("is_hls")), workdir=workdir, filepath=fp,
                # 分享页展开出的子任务 ID（2026-10-12）：不还原的话，
                # 重启/刷新后父任务就「忘了」自己派生了哪些图片，前端无从建卡片。
                children=[str(c) for c in (row.get("children") or [])],
                # 可读名提示（2026-10-12）：漏还原则中断重试后落盘名又变回 CDN 天书串
                name_hint=str(row.get("name_hint") or ""),
            )
            task.steps = []   # 恢复条目不带走历史步骤，__post_init__ 重建初始步骤
            task.__post_init__()
            if status == "completed":
                task.file_expired = file_expired
                if file_expired:
                    task.filepath = None
                    task.add_step("成品文件", "done", "已超过保留期，文件已清理（可重新下载）")
                else:
                    task.add_step("成片已就绪", "done")
            elif status in ("pending", "downloading", "merging", "paused", "pausing"):
                # 中断的任务统一恢复为「失败 + 可续传」，前端出「继续下载」按钮
                # 安全审计 2026-10-11（P2-19）：`pausing`（暂停信号已发出但尚未被
                # 进度回调消费，例如卡在合并阶段）原先落到下面的 else 分支被原样
                # 恢复 ⇒ 任务永久停在 pausing，既不可续也不可控。这里一并按中断处理。
                task.status = "failed"
                task.error = "应用退出，任务已中断"
                task.hint = "分片仍在磁盘上，点「继续下载」可从断点续传"
                task.resumable = _has_partial_file(workdir)
                task.created_at = now   # 重置 TTL，给用户时间点继续
                task.add_step("下载", "error", "应用重启中断")
            else:  # failed / canceled：恢复原状态；有分片则保留续传入口并重置 TTL
                task.status = status
                task.resumable = _has_partial_file(workdir)
                if task.resumable:
                    task.created_at = now
            with self._lock:
                self._tasks[tid] = task
            restored += 1
        if restored:
            self._persist_locked(force=True)

    def _adopt_resume_partials(self, *, url: str, workdir: Path, kind: str,
                               owner_id: str, quality_key: str) -> DownloadTask | None:
        """同一链接上次没下完 → 把残留分片搬进新任务目录，让这一单接着下。

        为什么需要（2026-10-30 用户实测）：任务中断/失败后，用户在界面里做的动作通常
        是**把链接再粘一遍**，而不是点「继续下载」。重贴 = 新 task_id = 新工作目录，
        旧目录里的 .part / netdisk.part 虽然还在磁盘上，却永远接不上（169MB 白下）。

        接管条件全部满足才动手：同一 URL、同一 kind、同一归属、旧任务已停下
        （failed/canceled/paused —— 进行中的分片正在被写，不能抢）、旧目录确有分片。
        只搬分片文件（同盘 rename，不产生拷贝），旧目录空了再连记录一起收掉，
        避免列表里出现两张同链接的卡。

        返回被接管的任务（供调用方记日志），没有可接管的返回 None。
        """
        key = (url or "").strip()
        if not key:
            return None
        donor: DownloadTask | None = None
        with self._lock:
            for t in self._tasks.values():
                if (getattr(t, "kind", "video") or "video") != kind:
                    continue
                if (t.url or "").strip() != key:
                    continue
                if (t.owner_id or "") != (owner_id or ""):
                    continue
                # 清晰度必须一致：换了档位分片就不是同一个文件（网盘 .part 更是按体积
                # 预分配的），接错了比从头下更糟。
                if (t.quality_key or "") != (quality_key or ""):
                    continue
                if t.status not in ("failed", "canceled", "paused"):
                    continue
                if not _has_partial_file(t.workdir):
                    continue
                if donor is None or t.created_at > donor.created_at:
                    donor = t
        if donor is None:
            return None
        moved = 0
        try:
            for src in list(donor.workdir.iterdir()):
                if not src.is_file() or not _is_partial_name(src.name):
                    continue
                try:
                    shutil.move(str(src), str(workdir / src.name))
                    moved += 1
                except (OSError, shutil.Error):
                    continue
        except OSError:
            return None
        if moved <= 0:
            return None
        donor.resumable = False
        donor.add_step("断点", "done", "已转交给重新发起的新任务")
        try:
            empty = not any(donor.workdir.iterdir())
        except OSError:
            empty = False
        if empty:
            self.remove(donor.id)
        return donor

    def create(self, *, url: str, title: str, platform: str, quality: str,
                quality_key: str = "best", extract_mode: str = "",
                concurrent_fragments: int = 0, downloader_type: str = "",
                cookie: str = "", proxy: str = "",
                play_url: str = "", watch_options: list[dict] | None = None,
                is_hls: bool = False, referer: str = "",
                owner_id: str = "", kind: str = "video",
                name_hint: str = "") -> DownloadTask:
        task_id = uuid.uuid4().hex[:TASK_ID_LENGTH]
        workdir = self._root / task_id
        workdir.mkdir(parents=True, exist_ok=True)
        _donor = self._adopt_resume_partials(url=url, workdir=workdir,
                                             kind=(kind or "video"), owner_id=owner_id,
                                             quality_key=quality_key)
        task = DownloadTask(
            id=task_id, url=url, title=title, platform=platform, quality=quality,
            quality_key=quality_key, workdir=workdir, extract_mode=extract_mode,
            concurrent_fragments=concurrent_fragments, downloader_type=downloader_type,
            cookie=cookie, proxy=proxy,
            play_url=play_url, watch_options=watch_options or [], is_hls=is_hls,
            referer=referer, owner_id=owner_id, kind=kind or "video",
            name_hint=name_hint,
        )
        if _donor is not None:
            # 分片已搬进本任务目录 → 执行器按 resume=True 起来（调用方用 _has_partial 判断）
            task.resumable = True
            task.add_step("断点", "done", "接上上次未完成的分片，继续下载")
        with self._lock:
            self._tasks[task_id] = task
            self._persist_locked(force=True)
        return task

    def get(self, task_id: str) -> DownloadTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def update(self, task_id: str, **fields) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            changed = False
            for key, value in fields.items():
                if getattr(task, key, None) != value:
                    changed = True
                setattr(task, key, value)
            if changed and any(k in _PERSIST_FIELDS for k in fields):
                self._persist_locked()

    def request_cancel(self, task_id: str) -> bool:
        """标记取消。仍在解析阶段（无进度回调）时直接置为已取消，让界面立即响应。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.is_finished:
                return False
            task.cancel_requested = True
            if task.status in ("pending", "paused"):
                # pending：还没起线程；paused：线程已退出、没人再消费标志——
                # 两者都必须就地置终态，否则「暂停中点取消」永远停不下来（2026-10-26）。
                task.status = "canceled"
                task.error = "已取消下载"
                task.add_step("排队等待", "error", "用户已取消")
            self._persist_locked()
        return True

    def clear_files(self, task_id: str) -> None:
        """清空任务的临时文件，但保留任务记录（便于前端读取终态）。"""
        task = self.get(task_id)
        if task and task.workdir and task.workdir.exists():
            shutil.rmtree(task.workdir, ignore_errors=True)
            task.workdir.mkdir(parents=True, exist_ok=True)

    def remove(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks.pop(task_id, None)
            self._persist_locked(force=True)
        if task and task.workdir:
            shutil.rmtree(task.workdir, ignore_errors=True)

    def list_all(self) -> list["DownloadTask"]:
        """返回当前所有任务的安全快照（不暴露内部 dict），供队列概览使用。

        ⚠️ 安全审计 P0-2：这是**不过滤归属**的全量视图，仅限内部使用（如 TTL 清理、
        取消全部）。对外端点必须改用 `list_for_owner`，否则会跨用户泄漏任务。
        """
        with self._lock:
            return list(self._tasks.values())

    def list_for_owner(self, owner_id: str) -> list["DownloadTask"]:
        """按归属列出任务（安全修复 2026-10-12，web-dev 同源漏洞）：

        · owner_id 为空（匿名请求：无登录态也无设备标识）→ **一律返回空**。
          旧规则「无归属任务对所有人可见」会让任何匿名请求拿到他人任务列表
          （含 source_url / play_url / watch_options 成品直链）。
        · 非匿名 → 本人任务 + 历史遗留（升级前创建、owner_id 为空）任务。
          桌面端单用户本机，保留老任务可见性，避免升级后历史凭空消失。
        """
        with self._lock:
            if not owner_id:
                return []
            return [t for t in self._tasks.values()
                    if t.owner_id == owner_id or not getattr(t, "owner_id", "")]

    def purge_expired(self, ttl: int = TASK_TTL_SECONDS) -> int:
        """TTL 清理：**只处理已终结的任务**，进行中的任务永不被清理。

        🔴 安全审计 2026-10-11（P2-18「数据误删/竞态」）：原先的判据是
        「created_at 超过 TTL 且非 completed → remove」，于是**下载超过 1 小时的任务
        会被就地删除**（大文件、慢链路、暂停中的任务全中招）——用户在下载中途发现
        任务凭空消失、分片被清掉。TTL 的语义是「**成品**保留 1 小时」，
        与「进行中任务的存活时长」无关。

        现在：
          · 进行中（pending/downloading/merging/paused/pausing）→ **一律跳过**；
          · completed → 降级为历史条目（清文件、留元数据）；
          · failed / canceled → 连记录一起清掉（它们是真正的终态残留）。
        """
        now = time.time()
        deadline = now - ttl
        resume_deadline = now - RESUME_TTL_SECONDS
        demoted = 0
        with self._lock:
            stale = []
            for tid, t in self._tasks.items():
                if t.file_expired:
                    continue                     # 已是历史条目
                if t.status not in _TERMINAL_STATUSES:
                    continue                     # 进行中：绝不清理（P2-18 核心）
                # 断点优先（2026-10-30）：失败且磁盘上确有分片 → 按 7 天保留，
                # 否则 1 小时就把「重贴链接接着下」的机会清掉了。completed 不受影响。
                keep_until = deadline
                if t.status == "failed" and _has_partial_file(t.workdir):
                    keep_until = resume_deadline
                if t.created_at >= keep_until:
                    continue                     # 终态但未到期
                if t.status == "completed":
                    stale.append((tid, "demote"))
                else:
                    stale.append((tid, "remove"))
            if stale:
                self._persist_dirty = True
        for task_id, action in stale:
            if action == "demote":
                demoted += self._demote_completed(task_id)
            else:
                self.remove(task_id)
        return demoted

    def _demote_completed(self, task_id: str) -> int:
        """B5：到期完成任务降级为历史条目——清空工作目录释放空间，保留元数据
        （标题/平台/大小/来源 URL），卡片变「成品已清理」，可整条重新下载。
        历史上限 _MAX_PERSISTED_COMPLETED 条，超限把最老的连记录一起清掉。

        🔴 安全审计 2026-10-11（P2-18 竞态）：进入临界区后**必须复核状态** ——
        `purge_expired` 的快照与实际降级之间存在窗口，若期间用户点了「重试」
        （状态回到 pending、workdir 里正在写入新分片），此时 rmtree 会把在途分片
        一并清掉。现在：状态不再是 completed 就**放弃降级**，返回 0。
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return 0
            if task.status != "completed":
                # 期间被重试/取消 → 不再是「到期成品」，绝不动它的工作目录
                return 0
            task.filepath = None
            task.file_expired = True
            task.resumable = False
            task.add_step("成品文件", "done", "已超过保留期，文件已清理（可重新下载）")
            # 超限：把最老的已完成历史条目整条移除（含记录）
            completed = [t for t in self._tasks.values()
                         if t.status == "completed" and t.file_expired]
            overflow: list[str] = []
            if len(completed) > _MAX_PERSISTED_COMPLETED:
                completed.sort(key=lambda t: t.created_at)
                overflow = [t.id for t in completed[:len(completed) - _MAX_PERSISTED_COMPLETED]]
            workdir = task.workdir
            self._persist_locked(force=True)
        for tid in overflow:
            self.remove(tid)
        if workdir and workdir.exists():
            shutil.rmtree(workdir, ignore_errors=True)
            try:
                workdir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        return 1

    def purge_orphans(self) -> int:
        """清理上次进程遗留的任务目录（任务状态只存在内存中，重启后即为孤儿）。"""
        with self._lock:
            known = set(self._tasks)
        removed = 0
        for path in self._root.iterdir():
            if path.is_dir() and TASK_DIR_PATTERN.match(path.name) and path.name not in known:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        return removed
