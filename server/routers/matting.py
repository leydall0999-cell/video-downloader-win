"""server/routers/matting.py — 一键抠图（图片去背景）路由。

照 routers/dewatermark.py 的图片链路：上传图片 → 异步 job（executor 提交）
→ 轮询状态 → 下载透明 PNG。独立 job 存储，不与去水印混用。

模型权重首次使用时才下载（默认 birefnet-general · 927MB，MIT 可商用），
状态接口会带上下载进度，前端据此显示「首次下载模型 xx%」，避免长任务看起来像卡死。

依赖（onnxruntime/numpy/Pillow，App 内已打包）缺失时返回 503，优雅降级。
"""
import logging
import threading

import app
import membership as _mem
import matting_ai as mat
import vision_client
from fastapi import APIRouter
from stats import record_event
import capability as _cap  # 内存分级（HR 发丝引擎仅 ≥16GB 可选）

router = APIRouter()
logger = logging.getLogger("matting")


def _cloud_report(resource: str, title: str = "") -> None:
    """纯积分功能用量补上报授权中心（抠图原先只落本机，云端用量榜看不到）。"""
    try:
        from routers.cloud_account import report_feature_usage_cloud
        report_feature_usage_cloud(resource, title=title)
    except Exception:  # noqa: BLE001
        pass


def _credit_gate(request, op: str, sub: str | None = None, reason: str | None = None) -> str | None:
    """AI 积分门禁：按成本扣费，None=放行，字符串=拦截原因（供 402 detail）。"""
    store = app.current_member_store(request)
    return _mem.gate_message(store, op, sub, reason)


def _make_cloud_charge_hook(request, store=None) -> dict:
    """造一个「云端算力记账」钩子，供 `matting_ai` 在**真实升级那一刻**调用。

    🔴 背景（财务漏洞，2026-10-05 修）：`matting_ai` 会在本地预检失败时**自动升级**
    到火山 MediaKit（`_force_cloud = force_cloud or local_auto_escalated`）。
    那时 router 层的 `if fc:` 门禁早已判完（`fc` 只来自前端表单，升级时仍为 False）
    ⇒ 真实花了云端算力却没扣积分。

    为什么用回调而不是在 router 预扣：预扣会把「本地成功出图」也一并扣钱
    （绝大多数本地抠图根本不用云端）。升级发生在**后台线程**里、拿不到 `request`，
    所以由 router 注入一个闭包：线程内触发 → 闭包在有 request 的上下文里扣费。

    语义：
      · `already_charged=True`（显式 force_cloud）→ 不重复扣。
      · 扣费失败**不抛**，只记 warning：宁可少收这一次，也不能让用户白等/白跑。
      · `charged` 标志供测试与 job meta 断言用。
    """
    box = {"already_charged": False, "charged": False, "op": "", "error": ""}

    def can_charge(op: str) -> str:
        """**只查不扣**：返回 "" 表示积分够用，返回人话原因表示不够。

        🔴 2026-10-06（用户「没积分会提示吗」）：升级发生在后台线程、事前无法预知，
        原实现只有「事后扣、扣不到就算了」⇒ 积分用完的用户自动升级那次会**白嫖且
        界面零提示**。有了这个探针，matting_ai 可以在**真正跑云端之前**先问一次：
        不够就**不升级**（回退本机结果，不白嫖），并在 meta 里留下原因给前端提示。
        🔴 **不能用 `gate_message` 当探针**（实测踩过）：它会真 `spend_credits`，
        余额不够时还会 `trial_consume` 烧掉「首次体验」名额 ⇒ 问一次就少一次
        免费机会。改用同模块的零副作用 `membership.can_afford`。
        """
        try:
            st = store if store is not None else app.current_member_store(request)
            return _mem.can_afford(st, op)
        except Exception as e:  # noqa: BLE001 — 探针异常按「够用」处理，交由事后扣费兜底
            app.logger.warning("[matting] 积分探针异常（按可升级处理）: %s", e)
            return ""

    box["can_charge"] = can_charge

    def _hook(op: str) -> None:
        if box["already_charged"] or box["charged"]:
            return
        box["charged"] = True
        box["op"] = op
        try:
            st = store if store is not None else app.current_member_store(request)
            msg = _mem.gate_message(st, op, reason=f"matting_auto_escalate:{op}")
            if msg:
                box["error"] = msg
                app.logger.warning("[matting] 云端升级记账被拒（已出图，未扣分）：%s", msg)
        except Exception as e:  # noqa: BLE001
            box["error"] = str(e)
            app.logger.warning("[matting] 云端升级记账异常（已出图，未扣分）：%s", e)

    # 🔴 注入进 job 的是 **hook 函数本身**（`meta["on_cloud_charge"]`），所以探针与
    #   幂等状态必须挂在函数属性上，工作线程（matting_ai）才拿得到。
    _hook.can_charge = can_charge          # 只查不扣：升级前探积分（B 段）
    _hook.state = box                      # already_charged / charged 实时状态
    box["hook"] = _hook
    return box

MAT_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
MAT_MODEL_EXTS = {".onnx"}

# 独立 job 存储（与去水印 DW_JOBS 隔离，避免 key 冲突）
MAT_JOBS: dict[str, dict] = {}
MAT_LOCK = threading.Lock()

# 🧲 智能选块 analyze 结果缓存：同图(内容 hash)+同参数 10 分钟内秒回，
# 避免用户切「📝 文字检测」开关 / 重复切工具时每次都重跑 13s BiRefNet + VLM。
_ANALYZE_CACHE: dict[str, dict] = {}
_ANALYZE_CACHE_TTL = 600.0
_ANALYZE_CACHE_LOCK = threading.Lock()


def _analyze_cache_key(path, model: str, with_text: bool) -> str:
    try:
        import hashlib as _hl

        h = _hl.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return f"{h.hexdigest()[:24]}|{model}|{1 if with_text else 0}"
    except Exception:  # noqa: BLE001
        return ""


def _save_upload(file, prefix: str) -> app.Path:
    """流式落盘上传文件到 DW_DIR，返回保存路径。"""
    suffix = app.Path(file.filename or "upload.bin").suffix.lower() or ".bin"
    save_path = app.DW_DIR / f"{prefix}_{app.uuid.uuid4().hex[:12]}{suffix}"
    written = 0
    try:
        with save_path.open("wb") as fh:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > app.UPLOAD_MAX_BYTES:
                    fh.close()
                    save_path.unlink(missing_ok=True)
                    raise app.HTTPException(status_code=413, detail="文件超过上传大小上限")
                fh.write(chunk)
    except app.HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        save_path.unlink(missing_ok=True)
        raise app.HTTPException(status_code=500, detail=f"保存上传文件失败：{e}")
    finally:
        file.file.close()
    return save_path


def _run_matting(job_id: str, src: list | None = None, box: list | None = None, model: str | None = None, vision_guide: bool = False, polygon: list | None = None, click: list | None = None, blocks: list | None = None, sam_refine: bool = False, prompt: str | None = None, keep_lasso_all: bool = False, force_cloud: bool = False) -> None:
    job = MAT_JOBS.get(job_id)
    if not job:
        return
    try:
        src_path = app.Path(src)
        out_path = app.DW_DIR / f"mat_{job_id}.png"
        # 诊断日志：记录选区原始参数，便于排查"圈了但结果不对"
        app.logger.info(
            "matting %s start: box=%s polygon=%s click=%s blocks=%s vision_guide=%s sam=%s model=%s",
            job_id,
            box,
            f"{len(polygon)}pts" if polygon else None,
            click,
            f"{len(blocks)}blk" if blocks else None,
            vision_guide,
            sam_refine,
            model,
        )
        job["prompt"] = prompt or ""
        vision_box = None
        vision_used = False
        if vision_guide:
            try:
                # 带验证的定位：定位后裁片自检「框的是不是用户要的」，框错则
                # 强调方位词重试一次（qwen-vl 对方位词+特定物体描述会框错位置）
                _fn = getattr(vision_client, "detect_subject_checked", vision_client.detect_subject)
                det = _fn(str(src_path), prompt=(prompt or "").strip() or None)
                vb = det["subject"]["box"]
                vision_box = [float(v) for v in vb[:4]]
                # 外扩 bbox：VLM 给的框通常只框「主体核心」（如文字笔画），不含底板/投影
                # 外延。直接用它会把阴影/外延切掉（实测「带底板+投影的设计图」主体 3/4 丢失）。
                # 向四周外扩 ~15%（贴边的封顶到 0/1），让框包住完整元素再抠。
                vx0, vy0, vx1, vy1 = vision_box
                ex = (vx1 - vx0) * 0.15
                ey = (vy1 - vy0) * 0.15
                vision_box = [
                    max(0.0, vx0 - ex), max(0.0, vy0 - ey),
                    min(1.0, vx1 + ex), min(1.0, vy1 + ey),
                ]
                vision_used = True
                job["vision_label"] = det["subject"].get("label", "")
            except Exception as e:  # noqa: BLE001
                logger.warning("vision_guide 失败，回退基础抠图: %s", e)
                vision_box = None
                vision_used = False
                job["vision_error"] = str(e)[:200]
        # 🤖 自动模式（无描述、无手动选区）下，VLM 的硬裁剪框语义是「框外强制透明」，
        # 而 VLM 通常只框住文字笔画，会把底板/投影/外延当框外裁掉 → 主体大面积缺失
        # （实测「带底板+投影的设计图」3/4 丢失）。此时放弃裁剪框，退化为云端整图
        # 抠图——云端本身就能完整抠出含阴影的主体。
        # 「说扣什么」(prompt 非空) 或任何手动选区场景下仍保留 VLM 定位。
        if vision_box is not None and not prompt and not (box or polygon or click or blocks):
            logger.info("auto 模式放弃 VLM 裁剪框，退化为云端整图（避免切掉底板/阴影）")
            vision_box = None
        mat.matting_image(src_path, out_path, box=box, model=model, vision_box=vision_box, polygon=polygon, click=click, blocks=blocks, sam_refine=sam_refine, vision_label=job.get("vision_label", ""), meta=job, keep_lasso_all=keep_lasso_all, force_cloud=force_cloud)
        if not out_path.exists() or out_path.stat().st_size == 0:
            raise RuntimeError("抠图未产出有效文件")
        job["status"] = "completed"
        job["filename"] = out_path.name
        job["out_path"] = str(out_path)
        job["vision_used"] = vision_used
        # 记录本次实际使用的选区模式（魔棒多块 > 点图 > 多边形 > AI 视觉 > 矩形框 > 自动）
        job["mode"] = "blocks" if blocks else ("click" if click else ("lasso" if polygon else ("vision" if vision_box else ("box" if box else "auto"))))
        job["sam_used"] = sam_refine and not blocks and bool(polygon or click or vision_box or box)
        # 把 polygon 的 bbox 也记下来，方便和用户画的圈对照
        if polygon:
            xs = [p[0] for p in polygon]
            ys = [p[1] for p in polygon]
            job["polygon_bbox"] = [min(xs), min(ys), max(xs), max(ys)]
            job["polygon_pts"] = len(polygon)
        app.logger.info("matting %s done -> %s (mode=%s vision_used=%s)", job_id, out_path.name, job["mode"], vision_used)
    except Exception as e:  # noqa: BLE001
        job["status"] = "failed"
        job["error"] = str(e)[:500]
        app.logger.warning("matting %s failed: %s", job_id, e)


def _dl_snapshot() -> dict:
    """当前模型下载进度（MB 单位，便于前端直接展示）。"""
    d = mat.download_progress()
    mb = 1024 * 1024
    return {
        "active": bool(d.get("active")),
        "model": d.get("model", ""),
        "pct": float(d.get("pct") or 0.0),
        "done_mb": round(float(d.get("done") or 0) / mb, 1),
        "total_mb": round(float(d.get("total") or 0) / mb, 1),
        "error": d.get("error", ""),
    }


@router.post("/api/matting/image")
def create_matting_image(
    file: app.UploadFile = app._FastAPIFile(...),
    box: str = app.Form(None),
    model: str = app.Form(None),
    vision_guide: str = app.Form(None),
    prompt: str = app.Form(None),
    polygon: str = app.Form(None),
    click_point: str = app.Form(None),
    blocks: str = app.Form(None),
    sam_refine: str = app.Form(None),
    keep_lasso_all: str = app.Form(None),
    force_cloud: str = app.Form(None),
    request: app.Request = None,
) -> dict:
    """一键抠图：上传图片，返回 job_id；轮询 /api/matting/image/{job_id} 拿状态。

    box（可选）：JSON 字符串 "[x, y, w, h]"，归一化 0~1，表示用户手动框选的
    主体区域。给定时只抠框内主体（照片里有多个物体时可指定抠哪一个）。
    polygon（可选）：JSON 字符串 "[[x,y],[x,y],...]"（≥3 点，归一化 0~1），
    表示用户用**套索工具**自由圈出的多边形。比矩形框精确得多，能贴合不规则主体
    （如只圈主标题、避开紧贴的副标题/装饰）。给了 polygon 就以它为准。
    click_point（可选）：JSON 字符串 "[x,y]"（归一化 0~1），**点图抠图**——
    用户直接在预览图上单击某个元素（主标题/图案/按钮等），后端跑全图显著性，
    找到点击处所属的连通显著块并只抠那一块。优先级最高（用户点哪抠哪）。
    sam_refine（可选）：1/true 时若本次有语义选区（polygon/box/click/vision_guide），
    用 🔬 MobileSAM 像素级分割精抠（边缘远优于显著性）；无选区/多块自动回退。
    model（可选）：模型名（birefnet-general / birefnet-general-lite / rmbg-2.0）。
    给定时用指定模型，否则用全局默认（当前 birefnet-general，MIT 可商用）。
    rmbg-2.0 为 CC BY-NC 4.0 仅非商用，仅限个人 / 非商业场景显式选用。
    """
    if not mat.available():
        raise app.HTTPException(status_code=503, detail="一键抠图不可用（缺少 onnxruntime / numpy / Pillow 依赖）")
    app._check_rate_limit(request)
    suffix = app.Path(file.filename or "upload.png").suffix.lower()
    if suffix not in MAT_IMAGE_EXTS:
        raise app.HTTPException(status_code=409, detail="请上传图片文件（png/jpg/webp/bmp 等）")

    parsed_box = None
    if box:
        try:
            import json as _json

            raw = _json.loads(box)
            if isinstance(raw, (list, tuple)) and len(raw) >= 4:
                parsed_box = [float(v) for v in raw[:4]]
        except Exception:  # noqa: BLE001
            parsed_box = None  # 解析失败就退回整图抠图，不报错

    # 🪢 套索选区：[[x,y],...] 归一化点列表，≥3 点才有效（无效则忽略）
    parsed_polygon = None
    if polygon:
        try:
            import json as _json2

            praw = _json2.loads(polygon)
            if isinstance(praw, list) and len(praw) >= 3:
                pts = [[float(p[0]), float(p[1])] for p in praw if isinstance(p, (list, tuple)) and len(p) >= 2]
                if len(pts) >= 3:
                    parsed_polygon = pts
        except Exception:  # noqa: BLE001
            parsed_polygon = None  # 解析失败就忽略套索，退回框选/整图

    # 校验模型名（未知名字退回全局默认，不报错）
    sel_model = model if (model and model in mat.MODELS) else None
    # HR 发丝引擎需 ≥16GB 内存，低内存设备拒绝（列表已隐藏，双保险防直调 API 触发 OOM）
    if sel_model == "birefnet-hr-matting":
        _ram = 0.0
        try:
            _ram = float(_cap.probe_capability(lama_available=False, diffusion_supported=False).get("ram_gb", 0) or 0)
        except Exception:  # noqa: BLE001
            _ram = 0.0
        if _ram < 16:
            raise app.HTTPException(status_code=400, detail="高清发丝(HR)引擎需 16GB+ 内存，当前设备不支持")
    # 🔒 会员专享引擎门禁：birefnet-portrait/birefnet-hr-matting/isnet-general-use/
    # sam-matting 仅会员可选；免费用户直调这些引擎时拒绝（列表已隐藏，双保险防直调绕过）。
    if sel_model and mat.MODELS.get(sel_model, {}).get("member_only"):
        _ms = app.current_member_store(request).status()
        _is_member = bool(_ms["download_member"]["active"]) or bool(_ms["ai_member"]["active"])
        if not _is_member:
            raise app.HTTPException(
                status_code=402,
                detail="MEMBER_QUOTA|该抠图引擎为会员专享（" + sel_model + "）— 开通会员解锁全部高级引擎",
            )
    # 🤖 AI 视觉定位开关：开启时后端先调 VLM 看懂图、自动框出主体再抠
    vg = (vision_guide or "").strip().lower() in ("1", "true", "yes", "on")
    kla = (keep_lasso_all or "").strip().lower() in ("1", "true", "yes", "on")

    # 👆 点图抠图：点击预览图上的元素（归一化 [x,y]），抠点击处所在显著块
    parsed_click = None
    if click_point:
        try:
            import json as _json3

            c = _json3.loads(click_point)
            if isinstance(c, (list, tuple)) and len(c) >= 2:
                fx, fy = float(c[0]), float(c[1])
                if 0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0:
                    parsed_click = [fx, fy]
        except Exception:  # noqa: BLE001
            parsed_click = None  # 解析失败忽略，退回其它选区

    # 🧲 智能魔棒多选：用户选中的多个元素块。元素可为纯轮廓数组 [[[x,y]...], ...]
    # 或带标记对象 [{"contour": [[x,y]...], "tag": "auto"|"text"}, ...]（tag=text 表示
    # 📝 VLM 文字块——显著性弱，抠图时需走局部裁切推理才能把字显出来）。
    parsed_blocks = None
    if blocks:
        try:
            import json as _json4

            braw = _json4.loads(blocks)
            if isinstance(braw, list) and len(braw) >= 1:
                out_blk = []
                for blk in braw:
                    tag = "auto"
                    if isinstance(blk, dict):
                        pts = blk.get("contour") or blk.get("polygon")
                        tag = str(blk.get("tag") or "auto")
                    elif isinstance(blk, list):
                        pts = blk
                    else:
                        pts = None
                    if isinstance(pts, list) and len(pts) >= 3:
                        clean = [[float(p[0]), float(p[1])] for p in pts if isinstance(p, (list, tuple)) and len(p) >= 2]
                        if len(clean) >= 3:
                            out_blk.append({"contour": clean, "tag": tag})
                if out_blk:
                    parsed_blocks = out_blk
        except Exception:  # noqa: BLE001
            parsed_blocks = None

    save_path = _save_upload(file, "mat_up")
    job_id = app.uuid.uuid4().hex[:12]
    with MAT_LOCK:
        MAT_JOBS[job_id] = {
            "status": "running", "out_path": "", "error": "", "filename": "",
            "kind": "matting",
        }
    sr = (sam_refine or "").strip().lower() in ("1", "true", "yes", "on")
    prompt_text = (prompt or "").strip()
    fc = (force_cloud or "").strip().lower() in ("1", "true", "yes", "on")
    # C2 门禁：云端抠图（火山 MediaKit，真实服务端算力）按次扣 AI 积分（不足拦截）。
    # 2026-10-05 扩围：本机重算力也计费（用户定档「云端 + 本机都算」），
    # 所以本地抠图改为扣 `local_matting_ai`；显式云端扣 `matting_cloud`。
    #
    # 🔴 财务漏洞（Explore 2026-10-05 定位）：本地预检判失败时 `matting_ai` 会
    #   **自动升级**到火山（`_force_cloud = … or local_auto_escalated`），
    #   但那时 `fc` 仍是 False → 上面这个门禁早就判完了 → 白嫖云端算力。
    #   修法见 `_make_cloud_charge_hook` 与 `matting_ai.py` 升级处的 `on_cloud_charge` 回调。
    _charge_hook = _make_cloud_charge_hook(request, store=app.current_member_store(request))
    if fc:
        _gate = _credit_gate(request, "matting_cloud", reason="cloud_matting")
        if _gate:
            raise app.HTTPException(status_code=402, detail=_gate)
        _charge_hook["already_charged"] = True      # 显式云端：上面已扣过，升级回调别重复扣
    else:
        _gate = _credit_gate(request, "local_matting_ai", reason="local_matting")
        if _gate:
            raise app.HTTPException(status_code=402, detail=_gate)
    # 🔴 2026-10-11 定档：一键抠图**取消每日免费次数**，纯按 AI 积分计费
    #   （本机 `local_matting_ai` 10 / 显式云端 `matting_cloud` 50，见上方门禁）。
    #   原先这里还有一道「免费 8 次/日 → 会员 500 次/日」的本地日配额墙，与积分
    #   门禁叠加 ⇒ 用户有积分也会被日配额拦（第 9 次必 402），与定档冲突，故移除。
    # 把云端升级记账钩子挂进 job —— job 会作为 `meta` 传给 `matting_ai.matting_image`，
    # 于是升级真实发生那一刻（matting_ai.py:1670 附近）能回调扣费。
    # job 里不能存闭包以外的不可序列化对象吗？—— job 只在进程内用，不落盘，OK。
    with MAT_LOCK:
        MAT_JOBS[job_id]["on_cloud_charge"] = _charge_hook["hook"]
    app.executor.submit(_run_matting, job_id, str(save_path), parsed_box, sel_model, vg, parsed_polygon, parsed_click, parsed_blocks, sr, prompt_text, kla, fc)
    if fc:
        record_event("matting_cloud", {"model": sel_model})
        _cloud_report("matting_cloud", sel_model)
    else:
        record_event("matting", {"model": sel_model})
        _cloud_report("matting", sel_model)
    return {"job_id": job_id, "status": "running", "kind": "matting", "box": parsed_box, "model": sel_model, "vision_guide": vg, "polygon": bool(parsed_polygon), "click": parsed_click, "blocks": bool(parsed_blocks), "sam_refine": sr}


@router.post("/api/matting/analyze")
def matting_analyze(
    file: app.UploadFile = app._FastAPIFile(...),
    model: str = app.Form(None),
    with_text: str = app.Form(None),
    request: app.Request = None,
) -> dict:
    """🧲 智能元素块分析：上传图 → 全图显著性 → 返回元素块清单。
    前端 hover 高亮 + 点击选块用。分析用轻量模型，速度快。
    with_text=1 时并行调 VLM 文字检测，把「主标题/按钮文字」等显著性弱、
    BiRefNet 成不了块的文字元素也作为 tag=text 的块插入最前（补足候选，
    让装饰风字体可 hover/选中）。VLM 未配置/失败自动降级只返回 BiRefNet 块。
    """
    if not mat.available():
        raise app.HTTPException(status_code=503, detail="一键抠图不可用（缺少 onnxruntime / numpy / Pillow 依赖）")
    app._check_rate_limit(request)
    suffix = app.Path(file.filename or "upload.png").suffix.lower()
    if suffix not in MAT_IMAGE_EXTS:
        raise app.HTTPException(status_code=409, detail="请上传图片文件（png/jpg/webp/bmp 等）")
    sel_model = model if (model and model in mat.MODELS) else "birefnet-general-lite"
    save_path = _save_upload(file, "mat_anl")
    # 🔴 2026-10-05 补计费：analyze 此前**完全没有配额与积分门禁**，却会真实跑
    #   BiRefNet（本机 ONNX，~13s）+ with_text=1 时再调一次 qwen-vl 文字块检测
    #   （vision_client.py:701）。按用户定档「本机重算力也计费」，
    #   统一扣 `matting_vision`（= VLM 定位/图像理解那一档，含本机与云端两侧理解开销）。
    #   缓存命中会秒回，但为避免「先扣后返」的不一致，扣费放在缓存判定**之后**。
    try:
        from PIL import Image

        with Image.open(save_path) as im:
            im.load()
            rgb = im.convert("RGB")
        wt = (with_text or "").strip().lower() in ("1", "true", "yes", "on")

        # 缓存命中直接秒回（同图重分析 / 切文字检测开关不再等十几秒）
        ckey = _analyze_cache_key(str(save_path), sel_model, wt) if save_path else ""
        if ckey:
            import time as _t

            with _ANALYZE_CACHE_LOCK:
                hit = _ANALYZE_CACHE.get(ckey)
                if hit and _t.time() - hit.get("ts", 0) < _ANALYZE_CACHE_TTL:
                    return {"ok": True, "blocks": hit["blocks"], "model": sel_model,
                            "total": len(hit["blocks"]), "text_used": hit["text_used"],
                            "text_total": hit["text_total"], "cached": True}

        # 缓存未命中 = 真的要跑模型了，此时才扣费（避免「先扣后返」）。
        _gate = _credit_gate(request, "matting_vision", reason="matting_analyze")
        if _gate:
            raise app.HTTPException(status_code=402, detail=_gate)

        blocks = mat.analyze_blocks(rgb, model=sel_model)
        text_used = False
        text_total = 0
        if wt:
            # BiRefNet 块 + VLM 文字检测并行跑，总耗时 ≈ max(13s, VLM) 而非串行 26s
            from concurrent.futures import ThreadPoolExecutor

            def _vlm_blocks():
                tb = vision_client.detect_text_blocks(str(save_path), timeout=45)
                W, H = rgb.size
                vblocks = []
                for i, b in enumerate(tb):
                    x1, y1, x2, y2 = b["box"]
                    contour = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
                    vblocks.append({
                        "id": -1 - i,
                        "bbox": [x1, y1, x2, y2],
                        "contour": contour,
                        "area": int((x2 - x1) * (y2 - y1) * W * H),
                        "tag": "text",
                        "label": b.get("label", "文字"),
                    })
                return vblocks

            try:
                with ThreadPoolExecutor(max_workers=2) as ex:
                    fut_blocks = ex.submit(mat.analyze_blocks, rgb, model=sel_model)
                    try:
                        vb = _vlm_blocks()
                    except Exception:  # noqa: BLE001  未配 Key / VLM 失败 → 降级
                        vb = []
                    blocks = fut_blocks.result()
                if vb:
                    blocks = vb + blocks
                    text_used = True
                    text_total = len(vb)
            except Exception:  # noqa: BLE001
                pass
        # 重排 id（文字块在前的整体序号），避免负 id / 冲突
        for i, b in enumerate(blocks):
            b["id"] = i
        out = {"ok": True, "blocks": blocks, "model": sel_model, "total": len(blocks),
               "text_used": text_used, "text_total": text_total}
        if ckey:
            with _ANALYZE_CACHE_LOCK:
                _ANALYZE_CACHE[ckey] = {
                    "ts": __import__("time").time(),
                    "blocks": blocks,
                    "text_used": text_used,
                    "text_total": text_total,
                }
        return out
    except Exception as e:  # noqa: BLE001
        raise app.HTTPException(status_code=500, detail=f"元素分析失败：{e}")


@router.post("/api/matting/blocks/split")
def matting_split_block(
    file: app.UploadFile = app._FastAPIFile(...),
    block: str = app.Form(None),
    model: str = app.Form(None),
    request: app.Request = None,
) -> dict:
    """🧲 块细分：把 hover/选中的一块（如连片的主标题+小标题）按颜色聚类拆子块。"""
    if not mat.available():
        raise app.HTTPException(status_code=503, detail="一键抠图不可用（缺少 onnxruntime / numpy / Pillow 依赖）")
    app._check_rate_limit(request)
    suffix = app.Path(file.filename or "upload.png").suffix.lower()
    if suffix not in MAT_IMAGE_EXTS:
        raise app.HTTPException(status_code=409, detail="请上传图片文件（png/jpg/webp/bmp 等）")
    parsed_blk = None
    if block:
        try:
            import json as _json5

            b = _json5.loads(block)
            if isinstance(b, dict):
                parsed_blk = b
            elif isinstance(b, list) and len(b) >= 3:
                parsed_blk = {"contour": b}
        except Exception:  # noqa: BLE001
            parsed_blk = None
    if not parsed_blk:
        raise app.HTTPException(status_code=400, detail="缺少待细分的块轮廓")
    sel_model = model if (model and model in mat.MODELS) else "birefnet-general-lite"
    save_path = _save_upload(file, "mat_spl")
    try:
        from PIL import Image

        with Image.open(save_path) as im:
            im.load()
            rgb = im.convert("RGB")
        sub_blocks = mat.split_block(rgb, parsed_blk, model=sel_model)
        return {"ok": True, "blocks": sub_blocks, "total": len(sub_blocks)}
    except Exception as e:  # noqa: BLE001
        raise app.HTTPException(status_code=500, detail=f"块细分失败：{e}")


@router.get("/api/matting/image/{job_id}")
def matting_image_status(job_id: str) -> dict:
    job = MAT_JOBS.get(job_id)
    if not job or job.get("kind") != "matting":
        raise app.HTTPException(status_code=404, detail="抠图任务不存在")
    return {
        "status": job["status"],
        "error": job.get("error", ""),
        "filename": job.get("filename", ""),
        # 首次使用会在这段时间下载模型权重，前端据此显示百分比
        "download": _dl_snapshot(),
        # 选区模式与诊断：套索/矩形/AI/自动，以及套索 bbox（便于核对坐标）
        "mode": job.get("mode", "auto"),
        "polygon_bbox": job.get("polygon_bbox"),
        "polygon_pts": job.get("polygon_pts"),
        # 🤖 AI 视觉定位结果：未配置/出错时自动回退基础抠图，前端据此提示
        "vision_used": job.get("vision_used", False),
        "vision_label": job.get("vision_label", ""),
        "vision_error": job.get("vision_error", ""),
        # 🔬 SAM 精细抠图是否被本次任务请求（有选区且勾选；自动模式/多块不适用）
        "sam_used": job.get("sam_used", False),
        # ☁️ 云端抠图（火山视觉智能，豆包级）：是否实际走了云端，以及失败原因
        "cloud_used": job.get("cloud_used", False),
        "cloud_provider": job.get("cloud_provider", ""),
        "cloud_error": job.get("cloud_error", ""),
        # 🛡️ 双保险（本地优先 + 质量自检）诊断字段：前端据此显示提示 / 自动升级状态 / 手动升级按钮
        "local_first": job.get("local_first", False),
        "local_engine": job.get("local_engine", ""),
        "local_simple": job.get("local_simple", False),
        "local_quality_ok": job.get("local_quality_ok", None),
        "local_quality_bad": job.get("local_quality_bad", False),
        "local_quality_reason": job.get("local_quality_reason", ""),
        "local_auto_escalated": job.get("local_auto_escalated", False),
        "forced_cloud": job.get("forced_cloud", False),
        # 🔴 2026-10-06 积分闸门诊断（B+A）：
        #   cloud_escalate_skipped = 积分不足、本次**没有**升级到云端（已回退本机）
        #   cloud_charge_error     = 云端真跑了但记账被拒/异常（已出图、未扣分）
        # 前端据此提示用户，避免「白嫖了却不知道」。
        "cloud_escalate_skipped": job.get("cloud_escalate_skipped", ""),
        "cloud_charge_error": job.get("cloud_charge_error", ""),
    }


@router.get("/api/matting/image/{job_id}/file")
def matting_image_file(job_id: str) -> app.FileResponse:
    job = MAT_JOBS.get(job_id)
    if not job or job.get("kind") != "matting":
        raise app.HTTPException(status_code=404, detail="抠图任务不存在")
    if job["status"] != "completed":
        raise app.HTTPException(status_code=409, detail="处理尚未完成")
    out = app.Path(job["out_path"])
    if not out.exists():
        raise app.HTTPException(status_code=410, detail="结果文件已清理")
    return app.FileResponse(
        path=str(out),
        filename=out.name,
        media_type="image/png",
        # 用 inline（不是 attachment）—— 否则 WKWebView 收到 attachment 头的 PNG 会
        # 把响应当下载而不是图片，结果区 img 元素静默加载失败（只显灰底）。
        # 浏览器看图渲染，下载按钮 matDownload 仍带 download 属性也能正常下载。
        headers={
            "Content-Disposition": f'inline; filename="{out.name}"',
            # 透明 PNG 缓存，避免反复请求（抠图结果 ID 唯一，不重复）
            "Cache-Control": "private, max-age=86400",
        },
    )


@router.get("/api/matting/models")
def matting_models(request: app.Request = None) -> dict:
    """模型列表（含是否已下载）+ 当前下载进度 + 当前模型的备用下载 URL。

    当自动下载失败时，前端用 `download_urls` 给用户展示可手动打开的镜像链接。
    """
    cur = mat.current_model()
    meta = mat.MODELS.get(cur, {})
    # 高清发丝(HR)引擎显存/内存占用高，仅 ≥16GB 设备可选；低内存设备直接从列表隐藏。
    _ram = 0.0
    try:
        _ram = float(_cap.probe_capability(lama_available=False, diffusion_supported=False).get("ram_gb", 0) or 0)
    except Exception:  # noqa: BLE001
        _ram = 0.0
    _models = [m for m in mat.list_models() if _ram >= float(m.get("min_ram_gb", 0) or 0)]
    # 🔒 会员专享引擎：birefnet-portrait/birefnet-hr-matting/isnet-general-use/sam-matting
    # 仅会员可见；免费用户直接从列表隐藏（与 HR 的 RAM 过滤同机制）。
    _is_member = False
    try:
        _ms = app.current_member_store(request).status()
        _is_member = bool(_ms["download_member"]["active"]) or bool(_ms["ai_member"]["active"])
    except Exception:  # noqa: BLE001
        _is_member = False
    _models = [m for m in _models if (not m.get("member_only")) or _is_member]
    return {
        "default": cur,
        "models": _models,
        "download": _dl_snapshot(),
        "available": mat.available(),
        "download_urls": list(meta.get("urls", [])),
        "model_filename": meta.get("filename", ""),
        "model_dir": str(mat._model_dir()),
        "model_size_mb": meta.get("size_mb", 0),
    }


@router.post("/api/matting/model")
def matting_set_model(payload: dict) -> dict:
    """切换抠图模型。切换后若新模型未下载，首次抠图会自动下载。"""
    name = (payload or {}).get("name") or ""
    if name not in mat.MODELS:
        raise app.HTTPException(status_code=400, detail=f"未知模型：{name}")
    mat.set_model(name)
    return {"ok": True, "model": mat.current_model(), "models": mat.list_models()}


@router.post("/api/matting/warmup")
def matting_warmup() -> dict:
    """后台预热：下载模型 + 建 session（不阻塞请求，用 /api/matting/models 轮询进度）。"""
    if not mat.available():
        raise app.HTTPException(status_code=503, detail="一键抠图不可用（缺少 onnxruntime / numpy / Pillow 依赖）")

    def _go():
        try:
            mat.warmup()
        except Exception as e:  # noqa: BLE001
            app.logger.warning("matting warmup failed: %s", e)

    threading.Thread(target=_go, name="matting-warmup", daemon=True).start()
    return {"ok": True, "model": mat.current_model(), "started": True}


@router.post("/api/matting/upload-model")
def matting_upload_model(
    file: app.UploadFile = app._FastAPIFile(...),
    payload: str = app.Form(None),
    request: app.Request = None,
) -> dict:
    """用户在浏览器手动下载到本地后，从 App 内上传到正确目录（不再走命令行）。

    query/form: model=<name>（不指定则用当前选中的模型）
    form:     file=<下载的 .onnx>
    """
    # 安全审计 2026-10-11（R-14）：本端点会**覆盖 onnx 模型文件**，原先无任何门禁
    # —— 任意能访问端口者即可写入模型目录（投毒 / DoS）。补来源校验 + 登录要求。
    if app._cross_site_rejected(request):
        raise app.HTTPException(status_code=403, detail="拒绝来自外部站点的跨源请求")
    if not app.get_current_user_id(request):
        raise app.HTTPException(status_code=401, detail="请先登录后再上传模型")
    target_name = ""
    if payload:
        try:
            import json as _json
            target_name = (_json.loads(payload) or {}).get("model", "") or ""
        except Exception:  # noqa: BLE001
            pass
    if not target_name:
        target_name = mat.current_model()
    if target_name not in mat.MODELS:
        raise app.HTTPException(status_code=400, detail=f"未知模型：{target_name}")

    # 文件名直接用 MODELS 里注册的标准名（避免用户下载时重命名）
    target_filename = mat.MODELS[target_name]["filename"]
    suffix = app.Path(file.filename or target_filename).suffix.lower() or ".onnx"
    if suffix not in MAT_MODEL_EXTS:
        raise app.HTTPException(status_code=409, detail=f"请上传 .onnx 文件（当前是 {suffix}）")

    dest = app.Path(mat._model_dir()) / target_filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".upload")

    written = 0
    try:
        with tmp.open("wb") as fh:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > app.UPLOAD_MAX_BYTES:
                    fh.close()
                    tmp.unlink(missing_ok=True)
                    raise app.HTTPException(status_code=413, detail="文件超过上传大小上限")
                fh.write(chunk)
    except app.HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        raise app.HTTPException(status_code=500, detail=f"保存上传文件失败：{e}")
    finally:
        file.file.close()

    if written == 0:
        tmp.unlink(missing_ok=True)
        raise app.HTTPException(status_code=411, detail="上传文件为空")

    tmp.replace(dest)  # 原子落盘；下次 _get_session 自动命中
    mat.set_model(target_name)  # 切换到刚上传的模型
    app.logger.info("matting user-uploaded model %s -> %s (%d bytes)", target_name, dest.name, written)
    return {
        "ok": True,
        "model": target_name,
        "path": str(dest),
        "size_bytes": written,
        "size_mb": round(written / 1024 / 1024, 1),
    }
