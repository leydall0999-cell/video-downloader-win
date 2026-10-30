"""server/routers/admin.py — 后台管理面板 API（/api/admin/*）。

全部接口需「已登录 + 超级用户」权限：经 Authorization: Bearer <user_token> 携带
用户 token，并由 users.json 中 is_admin=True 的账号放行。非超级用户或缺失 token
一律 401。

超级用户名单由 server/auth_store.ensure_superusers 维护：admin.json 的 admin_identifiers
或环境变量 VDL_ADMIN_IDENTIFIER；两者均未配置时，首个注册的账号自动成为超级用户。
后台内可经 /api/admin/users/{id}/set-admin 提权 / 降权其他账号。

功能模块：用户管理 / 会员管理 / 使用统计 / 系统配置。
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, Request

router = APIRouter()

# 静态扫描用：server/ 根目录与解说管线 scripts/ 目录（不在则跳过该根）。
# 🔴 绝不在源码里硬编码某台机器的绝对路径：开发机跑得通、部署到 ECS 就找不到
# （构建产物里也没有 WorkBuddy/问问题 这层目录）。优先读环境变量
# VDL_PIPELINE_DIR（build_mac.sh 用的就是 COMMENTARY_PIPELINE_DIR），其次找
# 同级 ../commentary-pipeline/scripts（源码仓布局）。
_SERVER_DIR = str(Path(__file__).resolve().parent.parent)
_PIPELINE_DIR = ""
for _c in (os.environ.get("VDL_PIPELINE_DIR", "").strip(),
           os.environ.get("COMMENTARY_PIPELINE_DIR", "").strip()):
    if _c and Path(_c).is_dir():
        _PIPELINE_DIR = str(Path(_c).resolve())
        break
if not _PIPELINE_DIR:
    _guess = Path(_SERVER_DIR).parent / "commentary-pipeline" / "scripts"
    if _guess.is_dir():
        _PIPELINE_DIR = str(_guess)

from admin_store import (
    list_users,
    set_user_disabled,
    reset_user_password,
    delete_user,
    list_memberships,
    grant_membership,
    adjust_credits,
    usage_stats,
    system_config,
    reset_stats,
    save_smtp_accounts,
    save_plan_overrides,
)
from user_membership import get_current_user_id
from auth_store import user_is_admin, set_user_admin
import license_client  # 纯 stdlib 叶子模块：面板对云端账号的操作直接走它


def _resolve_identifier(user_id: str) -> str:
    """user_id → 账号标识（邮箱/手机号，云端主键）。

    云端行的 user_id 就是规范化邮箱；本机行是 u_xxx，需查本机账号表。

    🔴 2026-10-22 修（手机号账号提权「点了没反应」的根因）：
    入参也可能**本身就是账号标识** —— 云端 uid = 手机号（如 "15638319956"），
    而面板行的 user_id 恰恰就是云端 uid。旧实现只认「含 @ 的字符串」，其余
    一律当本机 user_id 去查表 —— 本机 user_id 是 u_xxx，查不到 ⇒ 返回空 ⇒
    `_cloud_sync("")` 直接跳过云端（notice「未找到对应账号标识，本次仅本机生效」）
    ⇒ 本机改了、**云端没改**，而面板列表读的是云端数据 ⇒ 用户看到「点了完全没反应」。
    现在补上「原样当标识」的兜底。
    """
    raw = (user_id or "").strip()
    if not raw:
        return ""
    if "@" in raw:
        return raw.lower()
    try:
        from auth_store import user_identifier
        ident = str(user_identifier(raw) or "").strip().lower()
        if ident:
            return ident
    except Exception:
        pass
    # 本机按 user_id 没查到：入参很可能就是账号标识本身（手机号 / 非邮箱标识）。
    # 云端 uid 就是规范化标识 ⇒ 原样（小写）返回即可。
    # u_ 开头的是本机 user_id，查不到说明账号已删 ⇒ 返回空。
    if raw.startswith("u_"):
        return ""
    return raw.lower()


def _cloud_sync(ident: str, op) -> str:
    """把超管写操作同步到云端授权中心（2026-10-16「每端数据统一」：全端生效）。

    op(admin_token) -> dict。未配令牌 / 云端不可达时返回提示文案（附在响应
    notice 里），**不阻断本机操作** —— 本机是离线兜底，云端是真源。
    """
    if not ident:
        return "（未找到对应账号标识，本次仅本机生效）"
    import admin_store
    import license_client
    token = admin_store._license_admin_token()  # noqa: SLF001 — 同文件家族的内部约定
    if not token:
        return "（未配置授权中心管理令牌，本次仅本机生效）"
    try:
        r = op(token)
    except Exception as e:  # noqa: BLE001 — 云端异常只提示不阻断
        return f"（云端同步失败：{e}）"
    if isinstance(r, dict) and not r.get("ok"):
        return f"（云端同步失败：{r.get('error') or r.get('detail') or '未知'}）"
    return ""


def _local_uid(user_id: str) -> str:
    """入参是本机 user_id（u_xxx）或账号标识（邮箱/手机号）→ 本机 user_id。

    云端行的 user_id 就是账号标识（云端 uid = 规范化邮箱），本机没有该账号
    镜像时返回 "" —— 调用方据此走「纯云端」分支。
    """
    raw = (user_id or "").strip()
    if not raw:
        return ""
    try:
        import auth_store
        data = auth_store._load_users()
    except Exception:  # noqa: BLE001 — 账号表读失败不该让面板 500
        return ""
    for u in data.get("users", []):
        if u.get("user_id") == raw and not u.get("deleted_at"):
            return raw
    uid = (data.get("by_identifier") or {}).get(raw.strip().lower())
    return str(uid or "")


def _admin_mutate(user_id: str, local_op, cloud_op, *,
                  cloud_when_local: bool = True) -> dict[str, Any]:
    """超管写操作统一入口：本机（存在时）+ 云端授权中心（唯一真源）。

    🔴 2026-10-16 修「面板按钮点了没反应 / 报用户不存在」：用户与会员列表改为
    云端总库后，行上的 user_id 是**账号标识**（云端 uid = 邮箱），而各操作端点
    一律按本机 user_id 去本机账号表里查 ⇒ 云端账号全部「用户不存在」。
    这里统一解析标识：
      · 本机有该账号 → 先本机（沿用原语义，失败即返回），再同步云端；
      · 本机没有（纯云端账号）→ 只写云端，云端成功才算成功。
    cloud_when_local=False 用于「本机操作内部已自行推云」的场景（赠送 / 调分），
    避免同一笔发放向云端推两次。
    """
    ident = _resolve_identifier(user_id)
    lu = _local_uid(user_id)
    if lu:
        out = local_op(lu)
        if not (isinstance(out, dict) and out.get("ok")):
            return out if isinstance(out, dict) else {"ok": False, "error": "本机操作失败"}
    else:
        out = {"ok": True, "user_id": ident or user_id, "cloud_only": True}
    if lu and not cloud_when_local:
        import admin_store
        out["notice"] = ("" if admin_store._license_admin_token()
                         else "（未配置授权中心管理令牌，本次未同步云端）")
        return out
    out["notice"] = _cloud_sync(ident, lambda tok: cloud_op(ident, tok))
    if not lu:
        # 本机没有这个账号：云端是唯一落点，云端没成就是整单失败
        if not ident:
            return {**out, "ok": False, "error": "未找到对应账号标识，无法定位云端账号"}
        if out["notice"]:
            return {**out, "ok": False, "error": str(out["notice"]).strip("（）")}
    return out


def require_admin(request: Request) -> None:
    from fastapi import HTTPException
    uid = get_current_user_id(request)
    if not uid or not user_is_admin(uid):
        raise HTTPException(status_code=401, detail="未授权：需要管理员权限")


def require_super(request: Request) -> None:
    """写操作闸门：只有**唯一超管**放行（2026-10-22 权限拆两级）。

    分工：
      require_admin —— 能进后台、能看（用户列表 / 会员 / 统计 / 配置只读）
      require_super —— 能改（设/撤管理员、封号、发权益、改价、改配置、重置密码、删除）

    管理员拿合法 token 调写接口一律 **403**（不是 401）：身份没问题，是权限不够。
    前端据此提示「仅超级管理员可操作」，而不是把已登录的人踢回登录页。

    🔴 2026-10-22 文案订正：写操作要的是**超级管理员**权限，原文案写「需要管理员权限」
    会被误解成「管理员就能改」。这里明确区分两级：
      连后台都进不去 → 401「未授权：需要管理员权限」（先得是管理员）
      是管理员但不是超管 → 403「仅超级管理员可执行此操作（管理员为只读）」
    """
    from fastapi import HTTPException
    uid = get_current_user_id(request)
    if not uid:
        raise HTTPException(status_code=401, detail="未授权：请先登录")
    if not user_is_admin(uid):
        raise HTTPException(status_code=401, detail="未授权：需要超级管理员权限")
    from auth_store import user_role
    if user_role(uid) != "super":
        raise HTTPException(status_code=403,
                            detail="仅超级管理员可执行此操作（管理员为只读）")


# 凭据来源的中文说法（面板展示用；2026-10-05 探测式改造引入）。
# env=环境变量下发 / managed=管理员受管配置 / user=用户自己填的（我们已不再要求）
_SOURCE_CN = {"env": "环境变量", "managed": "管理员下发", "user": "本机文件"}


# 🔴 2026-10-06：后台「免费额度」分栏要用它标出「配了价但根本没扣费」的功能。
# 起因是盘点时发现 `commentary_vision`（单次真实成本 ¥0.24，全表最贵）、
# `voice_clone`、`matting_cloud_enhance` 三项在业务代码里只有后台能配价、
# 没有任何地方真正调 gate —— 也就是**现在完全免费、随便用**，属财务漏洞。
# 前端据此打「⚠ 未接入」角标，避免管理员以为配了价就等于在收钱。
# 判定不靠人工维护清单（会过期），而是每次请求时静态扫一遍真实调用点。
# 真正出现「op 作为计费参数」的调用形态（_credit_gate / gate_message / spend_for）
# 🔴 判定「这个 op 真在扣费」的实现（2026-10-06 两次返工后定稿）：
#
# 前两次都栽在正则上 —— ① 只认 `_credit_gate(op)`，把 `_cb("op")` 回调和
# `return "op"` 误判成「没接入」；② 放宽成多行匹配后，`[^)]{0,200}?` 又在
# `gate_message(\n  store,\n  "op",\n)` 这种多行实参上失配，把明明在扣费的
# matting_vision / subtitle_asr / local_matting_ai 误判成「没接入」。
# **误报成财务漏洞比不报更坏**（会去改本来正确的代码），所以彻底不用正则。
#
# 现在只做一件事：在**业务源码**里找该 op 的字符串字面量出现位置，记为
# 「候选调用点」；是否真扣费由 `_COST_GATE_SITES` 里人工登记的文件名确认。
# 人工登记的那张表由守卫测试 test_ai_credit_costs.py 双向核对（新登记的 op
# 必须真有调用点，成本表里新增却没登记的会红），不会静默腐烂。
_COST_GATE_SITES: dict[str, tuple[str, ...]] = {
    # 有真实扣费拦截点的 op → 出现该字面量的业务文件
    "matting_cloud": ("routers/matting.py",),
    "matting_vision": ("routers/matting.py",),
    "local_matting_ai": ("routers/matting.py",),
    "commentary_llm": ("routers/quota.py",),
    "commentary_local_mlx": ("routers/quota.py",),
    "subtitle_asr": ("routers/subtitle.py",),
    "subtitle_translate": ("routers/subtitles.py",),
    "dewatermark_ai": ("routers/dewatermark.py",),
    "dewatermark_video_ai": ("routers/dewatermark.py",),
    # 🔴 2026-10-06 补上两项真实扣费点：
    #   · commentary_vision（50 积分，单次真实成本 ¥0.24，全表最贵）此前只在
    #     成本表登记、代码里从无扣费点 ⇒ 用户勾「画面理解」会真去调多模态模型
    #     却不扣分（白嫖）。现由解说任务入口按 `vision` 开关扣。
    #   · voice_clone（30 积分）——/api/voice-studio/tts 此前完全无鉴权无计费，
    #     任何能打到本机端口的人都能无限免费调 TTS。
    "commentary_vision": ("routers/quota.py",),
    "voice_clone": ("routers/voice_studio.py",),
    # ⚠️ `matting_cloud_enhance` **故意不登记**（用户 2026-10-06 定档）：
    # 它是云端抠图的**内部步骤**（人像场景自动触发），外层 `matting_cloud` 已收
    # 50 积分；单独再收 15 会对同一次操作重复收费。故不设独立拦截点。
}
def _has_cost_gate(op: str) -> bool:
    """该 op 是否真有扣费拦截点。

    查 `_COST_GATE_SITES` 登记表，并**顺带校验**登记的文件里确实出现了该 op 的
    字面量 —— 双重保险：登记表过期（功能挪了文件）会退化成 False，而不是继续
    报一个假的「在扣费」。
    """
    sites = _COST_GATE_SITES.get(op)
    if not sites:
        return False
    for rel in sites:
        p = os.path.join(_SERVER_DIR, rel)
        try:
            with open(p, encoding="utf-8", errors="ignore") as fh:
                if f'"{op}"' in fh.read() or f"'{op}'" in fh.read():
                    return True
        except OSError:
            continue
    return False


def _cloud_quota_limits() -> tuple[int, int]:
    """云端免费额度上限（终身 / 每日 auto）。

    优先级：`plans.json` 的 `free_quota.{cloud_lifetime, daily_auto}` 覆盖
    → `quota.py` 的模块常量（代码默认值）。

    🔴 2026-10-06：此前只读模块常量 ⇒ **后台看得到数字但改不了**（实测往
    `free_quota` 写 99/88 完全不生效，要改代码 + 重新构建）。用户要求
    「后台能不能改」—— 能，所以加覆盖层，与 `effective_daily_limits()` 同套路：
    未配置时落回代码常量（默认值永不丢失），脏值忽略而非炸表。
    """
    try:
        from quota import LIFETIME_CLOUD_EVENTS, DAILY_AUTO_RUNS
        life, daily = int(LIFETIME_CLOUD_EVENTS), int(DAILY_AUTO_RUNS)
    except Exception:
        life, daily = 3, 1
    # 🔴 局部 import：模块顶层没有 `load_plan_overrides`，此前直接调用会 NameError
    # 被下面的 except 吞掉 → 永远返回默认 (3,1)，后台改了额度却显示没变（实测）。
    try:
        from membership import load_plan_overrides
        fq = (load_plan_overrides().get("free_quota") or {})
    except Exception:
        return life, daily
    if not isinstance(fq, dict):
        return life, daily
    # ⚠️ 不能写 `for key, cur in (...)` 再 `cur = n` —— 改的是循环变量，
    # 不会写回 life/daily（第一版就是这么写的，后台改值不生效）。
    v = fq.get("cloud_lifetime")
    if v is not None:
        try:
            n = int(v)
            if n >= 0:
                life = n
        except (TypeError, ValueError):
            pass                          # 脏值忽略：宁可回退默认
    v = fq.get("daily_auto")
    if v is not None:
        try:
            n = int(v)
            if n >= 0:
                daily = n
        except (TypeError, ValueError):
            pass
    return life, daily


def _cloud_lifetime_per_resource() -> dict[str, int]:
    """per-resource 终身免费次数上限（与 quota.py::cloud_lifetime_limits 同源）。

    plans.json 的 `free_quota.cloud_lifetime` 支持：
      · 整数（旧总池）→ 应用到全部 4 个 resource（向后兼容）；
      · 字典 {resource: n} → 按 resource 覆盖（管理员可让某功能更松/更紧）。
    默认每个 resource 各 `LIFETIME_CLOUD_EVENTS` 次。
    """
    try:
        from quota import LIFETIME_CLOUD_EVENTS, CLOUD_RESOURCES
        defaults = {r: int(LIFETIME_CLOUD_EVENTS) for r in CLOUD_RESOURCES}
    except Exception:
        return {}
    try:
        from membership import load_plan_overrides
        fq = (load_plan_overrides().get("free_quota") or {})
    except Exception:
        return defaults
    if not isinstance(fq, dict):
        return defaults
    v = fq.get("cloud_lifetime")
    if isinstance(v, dict):
        for r in CLOUD_RESOURCES:
            if r in v:
                try:
                    n = int(v[r])
                    if n >= 0:
                        defaults[r] = n
                except (TypeError, ValueError):
                    pass
    elif v is not None:
        try:
            n = int(v)
            if n >= 0:
                defaults = {r: n for r in CLOUD_RESOURCES}
        except (TypeError, ValueError):
            pass
    return defaults


def _M_free_quota_mode() -> str:
    """生效的免费额度口径（on/off）—— 独立小包装便于测试打桩。"""
    from membership import free_quota_mode
    return free_quota_mode()


def _M_free_quota_modes() -> tuple[str, ...]:
    from membership import FREE_QUOTA_MODES
    return FREE_QUOTA_MODES


def _daily_feature_rows() -> list[dict[str, Any]]:
    """后台「免费额度」右栏的功能列表（**按用户看到的功能名**）。

    🔴 2026-10-06 用户 14:22 要求：「得按功能来：视频下载、订阅追更、视频解说、
    本地字幕提取、音视频格式转换、音乐转换等，这样更清楚」—— 之前只写
    「云端算力 / 本地重算力」是内部技术词，管理员看不出管哪些功能。

    实现要点：
      · 直接用 `FEATURE_USAGE_DEFS` 的**功能行**（名称 = 首页卡片原文）；
      · 同一 `resource` 的多个功能行**共用一份额度**，故用 `shared_group` 标注，
        前端据此显示「与 XXX 共用」；改任一行都改同一份（这是刻意的）；
      · 仍以**配额表为真源**兜底：某个 resource 若没登记功能行，自动补一行
        技术名 + `known: false`，保证「配额表里有的键后台一定能看到能改」
        （这正是当初漏 `cloud` 的教训）。

    ⚠️ 返回的 `free_limit` / `member_limit` 是**生效值**（已叠加后台覆盖），
    不是代码默认值 —— 否则页面显示 3 而实际生效的是管理员改过的 10。
    """
    from membership import FEATURE_USAGE_DEFS, effective_daily_limits
    mem_limits, free_limits = effective_daily_limits()

    # 同一 resource 的功能行 → 该 resource 的展示名（取第一行），用于共用提示
    first_name: dict[str, str] = {}
    for x in FEATURE_USAGE_DEFS:
        r = str(x.get("resource") or "")
        if r and r not in first_name:
            first_name[r] = str(x.get("name") or r)

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for x in FEATURE_USAGE_DEFS:
        r = str(x.get("resource") or "")
        if not r:
            continue
        seen.add(r)
        rows.append({
            "key": x.get("key") or r,
            "name": x.get("name") or r,
            "resource": r,                     # 决定改哪个数（输入框的 data-key）
            "unit": x.get("unit") or "次",
            "free_limit": int(free_limits.get(r, x.get("free_limit") or 0)),
            "member_limit": int(mem_limits.get(r, x.get("member_limit", -1))),
            "shared_group": first_name.get(r, ""),   # 共用同一额度的「首个功能名」
            "shared_note": x.get("shared_note") or "",
            "known": True,
        })

    # 兜底：配额表里有、但没登记功能行的 resource（技术名显示，标 known=false）
    # 🔴 2026-10-06 拆键：跳过「老键」（LEGACY_QUOTA_KEYS，如 app_compute）。
    # 它只用于存量用量归集，业务已不再写入 —— 出现在配置表里会让管理员以为
    # 「转换/压缩/修复/桥接还共用这一份」，与拆键后的事实相反。
    legacy = set()
    try:
        from membership import LEGACY_QUOTA_KEYS
        legacy = set(LEGACY_QUOTA_KEYS)
    except Exception:
        pass
    for r, free_v in free_limits.items():
        if r in seen or r in legacy:
            continue
        rows.append({
            "key": r, "name": r, "resource": r, "unit": "次",
            "free_limit": int(free_v),
            "member_limit": int(mem_limits.get(r, -1)),
            "shared_group": "", "shared_note": "⚠ 未登记功能名（配额表里有此键）",
            "known": False,
        })
    return rows


@router.get("/api/admin/users")
def admin_list_users(request: Request = None) -> dict[str, Any]:
    require_admin(request)
    rows = list_users()
    # 「当前账号」标记：云端行的 user_id 是邮箱，前端拿本机 uid 比不出来（会把
    # 自己那行也渲染成「取消管理员 / 禁用」）⇒ 由后端按标识判定后回填。
    me = _resolve_identifier(get_current_user_id(request))
    for r in rows:
        r["self"] = bool(me) and str(r.get("identifier") or "").strip().lower() == me
    return {"ok": True, "users": rows, "total": len(rows)}


@router.post("/api/admin/users/{user_id}/set-admin")
def admin_set_admin(user_id: str, payload: dict[str, Any] = Body(...), request: Request = None) -> dict[str, Any]:
    require_super(request)
    flag = bool(payload.get("is_admin", False))
    target = _resolve_identifier(user_id)
    # 🔴 唯一超管不可被降级（2026-10-22 权限拆两级）：超管身份由授权名单 /
    #    「最早注册」推导，**不由这个接口决定**。允许在这里取消会直接把后台锁死
    #    （全库无超管 ⇒ 所有写操作 403，且没有入口能恢复）。
    from auth_store import is_super_identifier
    if not flag and is_super_identifier(target):
        return {"ok": False,
                "error": "超级管理员不可被取消（超管唯一，身份由授权名单决定）"}
    # 自己不能被降权（云端行 user_id 是邮箱，前端 self 判断已由后端 self 字段兜住，
    # 这里再做一次硬拦：误点「取消管理员」会把唯一超管锁在门外）。
    if not flag and target == _resolve_identifier(get_current_user_id(request)):
        return {"ok": False, "error": "不能取消当前登录账号的管理员权限"}
    return _admin_mutate(
        user_id,
        lambda uid: set_user_admin(uid, flag),
        lambda ident, tok: license_client.set_admin_remote(ident, flag, tok))


@router.post("/api/admin/users/{user_id}/disable")
def admin_disable_user(user_id: str, request: Request = None) -> dict[str, Any]:
    require_super(request)
    if _resolve_identifier(user_id) == _resolve_identifier(get_current_user_id(request)):
        return {"ok": False, "error": "不能禁用当前登录的账号"}
    return _admin_mutate(
        user_id,
        lambda uid: set_user_disabled(uid, True),
        lambda ident, tok: license_client.ban_remote(ident, True, tok, note="admin disable"))


@router.post("/api/admin/users/{user_id}/enable")
def admin_enable_user(user_id: str, request: Request = None) -> dict[str, Any]:
    require_super(request)
    return _admin_mutate(
        user_id,
        lambda uid: set_user_disabled(uid, False),
        lambda ident, tok: license_client.ban_remote(ident, False, tok, note="admin enable"))


@router.post("/api/admin/users/{user_id}/reset-password")
def admin_reset_password(user_id: str, payload: dict[str, Any] = Body(...), request: Request = None) -> dict[str, Any]:
    require_super(request)
    new_pw = str(payload.get("new_password") or "")
    if not new_pw or len(new_pw) < 6:
        return {"ok": False, "error": "新密码至少 6 位"}
    return _admin_mutate(
        user_id,
        lambda uid: reset_user_password(uid, new_pw),
        lambda ident, tok: license_client.set_password_remote(ident, new_pw, admin_token=tok))


@router.post("/api/admin/users/{user_id}/delete")
def admin_delete_user(user_id: str, request: Request = None) -> dict[str, Any]:
    """删除账号（本机软删除 + 云端销号）：不可删自己；超管不可删。"""
    require_super(request)
    me = _resolve_identifier(get_current_user_id(request))
    ident = _resolve_identifier(user_id)
    if user_id == get_current_user_id(request) or (ident and ident == me):
        return {"ok": False, "error": "不能删除当前登录的账号"}
    row = next((r for r in list_users()
                if r.get("user_id") == user_id
                or str(r.get("identifier") or "").strip().lower() == ident), None)
    if row and row.get("is_admin"):
        return {"ok": False, "error": "超级用户不可删除，请先取消其管理员"}
    return _admin_mutate(
        user_id,
        lambda uid: delete_user(uid),
        lambda i, tok: license_client.delete_user_remote(i, tok))


@router.get("/api/admin/memberships")
def admin_list_memberships(request: Request = None) -> dict[str, Any]:
    require_admin(request)
    items = list_memberships()
    return {"ok": True, "memberships": items, "total": len(items)}


def _cloud_credit_ops(ident: str, tok: str, delta: int, pool: str) -> list:
    """云端调分序列：把本地 pool 语义映射成云端 adjust(pool, delta) 调用列表。

    - ai / permanent：原样一条；
    - auto：正=充永久；负=先扣 AI 订阅余量再扣永久（与 membership.add_credits 一致）。
      云端账号没有本地 store，AI 余量改从云端 authority 取（多一次只读调用）。
    """
    # 2026-10-27（1.0.97）：gift=会员赠送池，只存在于云端授权中心，原样转发一条
    if pool in ("ai", "permanent", "gift"):
        return [(pool, delta)]
    if delta >= 0:
        return [("permanent", delta)]
    amount = -delta
    ai_left = 0
    try:
        u = license_client.usage_remote(ident, tok)
        ai_left = int(((u or {}).get("authority") or {}).get("ai_credits_left") or 0)
    except Exception:  # noqa: BLE001 — 取不到就保守按「全扣永久」
        ai_left = 0
    ai_d = min(max(ai_left, 0), amount)
    ops: list = []
    if ai_d > 0:
        ops.append(("ai", -ai_d))
    if amount - ai_d > 0:
        ops.append(("permanent", -(amount - ai_d)))
    return ops


@router.post("/api/admin/memberships/grant")
def admin_grant(payload: dict[str, Any] = Body(...), request: Request = None) -> dict[str, Any]:
    require_super(request)
    user_id = str(payload.get("user_id") or "")
    code = str(payload.get("code") or "").strip()
    if not user_id or not code:
        return {"ok": False, "error": "user_id 与 code 必填"}
    # 本机账号：grant_membership 落本机账本后**内部已 setstate 推云** ⇒ 不再重复推
    # 云端账号（本机没有镜像）：云端 grant 直接开套餐
    return _admin_mutate(
        user_id,
        lambda uid: grant_membership(uid, code),
        lambda ident, tok: license_client.grant_remote(ident, code, tok, note="admin grant"),
        cloud_when_local=False)


@router.post("/api/admin/memberships/credits")
def admin_credits(payload: dict[str, Any] = Body(...), request: Request = None) -> dict[str, Any]:
    require_super(request)
    user_id = str(payload.get("user_id") or "")
    try:
        delta = int(payload.get("delta"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "delta 必须为整数"}
    # pool: "ai"=AI 订阅积分 / "permanent"=永久积分 / "gift"=会员赠送积分（随会员到期清零）；
    # 缺省 auto=旧语义（正充永久，负先 AI 后永久）
    pool = str(payload.get("pool") or "auto").strip() or "auto"
    if pool not in ("auto", "ai", "permanent", "gift"):
        return {"ok": False, "error": f"未知积分池：{pool}"}

    def _cloud_adjust(ident: str, tok: str) -> dict[str, Any]:
        last: dict[str, Any] = {"ok": True}
        for cpool, cdelta in _cloud_credit_ops(ident, tok, delta, pool):
            if cdelta == 0:
                continue
            last = license_client.adjust_remote(tok, ident, cpool, cdelta, note="admin credits")
            if isinstance(last, dict) and not last.get("ok"):
                return last
        return last

    # 本机账号：adjust_credits 落本机账本后内部已推云 ⇒ 不再重复推
    # 2026-10-27（1.0.97）：gift 池本机账本没有（只有云端授权中心有），本机一侧空过，
    # 直接走云端；cloud_when_local=True 才会触发 _cloud_sync。
    if pool == "gift":
        return _admin_mutate(
            user_id,
            lambda uid: {"ok": True, "gift_cloud_only": True},
            _cloud_adjust,
            cloud_when_local=True)
    return _admin_mutate(
        user_id,
        lambda uid: adjust_credits(uid, delta, pool),
        _cloud_adjust,
        cloud_when_local=False)


@router.get("/api/admin/stats")
def admin_stats(request: Request = None) -> dict[str, Any]:
    require_admin(request)
    out = {"ok": True, **usage_stats()}
    # 叠加全站功能用量（授权中心按账号聚合，含走免配额闸门的功能，
    # 不再只显示本机 stats.json 里那点下载量）。云端不可达时如实略过。
    try:
        from admin_store import _license_admin_token
        tok = _license_admin_token()
        if tok:
            import license_client
            ov = license_client.usage_overview_remote(tok, days=30)
            if isinstance(ov, dict) and ov.get("ok"):
                out["cloud_overview"] = {
                    "by_resource": ov.get("by_resource") or {},
                    "by_day": ov.get("by_day") or {},
                    "active_users": ov.get("active_users") or 0,
                    "days": ov.get("days") or 30,
                }
    except Exception:
        pass
    return out


@router.get("/api/admin/stats/install")
def admin_stats_install(request: Request = None) -> dict[str, Any]:
    """超级管理员：安装 / 插件次数统计（按平台）——数据源=授权中心设备记录。

    🔴 2026-10-13 新增（用户要求「app安装包安装/插件次数统计，不同平台的统计，
    为后续计划做铺垫」）。设备画像（platform / app_version / ext）随心跳上报，
    授权中心在设备记录上落 `first_seen`（= 安装时间）并聚合。本端点只做
    转发：管理面板 → 本机 /api/admin/*（require_admin）→ 授权中心
    /api/license/stat_query（ADMIN_TOKEN）。云端不可达时如实回报，不抛出。
    """
    require_admin(request)
    try:
        import admin_store
        token = admin_store._license_admin_token()  # noqa: SLF001 — 复用既有令牌读取
    except Exception:  # noqa: BLE001
        token = ""
    if not token:
        return {"ok": False, "reason": "no_admin_token"}
    try:
        import license_client
        r = license_client.stat_query_remote(token)
    except Exception as e:  # noqa: BLE001 - 云端异常只降级不炸面板
        return {"ok": False, "reason": type(e).__name__}
    if not isinstance(r, dict) or not r.get("ok"):
        return {"ok": False,
                "reason": str((r or {}).get("error") or (r or {}).get("detail") or "cloud_rejected")}
    return {"ok": True, "stats": (r.get("stats") or {})}


@router.post("/api/admin/stats/reset")
def admin_stats_reset(request: Request = None) -> dict[str, Any]:
    require_super(request)
    return reset_stats()


@router.get("/api/admin/config")
def admin_config(request: Request = None) -> dict[str, Any]:
    require_admin(request)
    return {"ok": True, **system_config()}


# ── 解析需求榜（2026-10-17，Downie 机制本土化第一环）─────────────────────────
# 解析失败（白名单空白站点 / 未知域名）自动落库后的聚合视图：按「站点×周」计数
# （2026-10-23 起；原按天），重试免疫。用途：每月挑 Top 站点写成专用提取器——把
# Downie「用户报一个写一个」的十年机制，变成我们可执行的开发排期输入。
def _merge_center_demand(rows: list, days: int) -> tuple[bool, list]:
    """拉授权中心 demand_query（桌面 App 经 demand_report 上报的需求榜）合并。

    授权中心是桌面端需求的**唯一真源**（Mac/Win 各自本机日志已不再互通，且本机日志
    也会上报到中心）。故以中心为准，本机日志只补「中心里没有」的离线 / 未登录需求，
    避免本机与中心重复计数（本机需求已包含在中心里）。
    中心不可达 / 未配 admin token → 退回本机日志（center_ok=False 明示，不阻断后台）。
    """
    from admin_store import _license_admin_token
    tok = _license_admin_token()
    if not tok:
        return False, rows
    try:
        import license_client
        r = license_client.demand_query_remote(tok, days=days)
    except Exception:
        return False, rows
    if not isinstance(r, dict) or not r.get("ok"):
        return False, rows
    center_rows = r.get("demand") or []
    if not center_rows:
        # 中心可达但暂无数据：本机日志兜底（仍算 center_ok=True，不误导成「没配」）
        return True, rows
    # 中心为桌面端需求唯一真源（已含本机上报）；本机日志只补「中心没有」的
    # 离线 / 未登录需求，避免本机与中心重复计数（本机需求已包含在中心里）。
    by_host = {cr["host"]: dict(cr) for cr in center_rows}
    for lr in rows:
        h = lr.get("host")
        if not h or h in by_host:
            continue  # 中心已含本机上报的同站需求，跳过避免重复
        row = dict(lr)
        row["nodes"] = sorted(set(lr.get("nodes") or []) | {"local"})
        by_host[h] = row
    out = sorted(
        by_host.values(),
        key=lambda x: (-int(x.get("weeks_count") or 0), -float(x.get("last_ts") or 0)))
    return True, out


@router.get("/api/admin/resolve-demand")
def admin_resolve_demand(days: int = 84, request: Request = None) -> dict[str, Any]:
    require_admin(request)
    import resolve_demand
    rows = resolve_demand.demand_summary(days=days)
    center_ok, merged = _merge_center_demand(rows, days)
    return {"ok": True, "days": days, "demand": merged,
            "total_sites": len(merged), "center_ok": center_ok}


@router.post("/api/admin/config/smtp")
def admin_config_smtp(payload: dict[str, Any] = Body(...), request: Request = None) -> dict[str, Any]:
    require_super(request)
    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        return {"ok": False, "error": "accounts 必须为数组"}
    return save_smtp_accounts(accounts)


@router.post("/api/admin/config/plans")
def admin_config_plans(payload: dict[str, Any] = Body(...), request: Request = None) -> dict[str, Any]:
    require_super(request)
    out = save_plan_overrides(payload or {})
    # 🔴 2026-10-06 终身免费次数下发中心：放行判定在云端，本机 plans.json 只是缓存。
    #   保存 free_quota（含 cloud_lifetime）后立刻下发，两端口径一致。
    #   失败**不回滚保存**（云端不可达时本机仍应记住管理员意图），只在响应里如实回报。
    if isinstance(payload, dict) and "free_quota" in payload:
        import membership as _M
        out = dict(out)
        out["cloud_free_quota"] = _M.push_free_quota_to_cloud()
    return out


# ── 会员中心公告（2026-10-18）──────────────────────────────────────────────
# 用户要求：会员中心套餐卡上方加一条**可手动编辑**的公告（首条为
# 「下方价格为测试期间优惠价格」）。公告**必须走云端**——只写本机等于只有
# 管理员这台能看到，其他用户的会员中心不会出现任何东西。
# GET 读当前生效值（云端为准，读不到即为空公告）；POST 写云端并即时刷新缓存。
@router.get("/api/admin/member-notice")
def admin_member_notice_get(request: Request = None) -> dict[str, Any]:
    require_admin(request)
    import membership as _M
    return {"ok": True, "notice": _M.cloud_member_notice()}


@router.post("/api/admin/member-notice")
def admin_member_notice_set(payload: dict[str, Any] = Body(...),
                            request: Request = None) -> dict[str, Any]:
    require_super(request)
    enabled = bool(payload.get("enabled", True))
    text = str(payload.get("text") or "").strip()
    if len(text) > 500:
        return {"ok": False, "error": "公告最多 500 字"}
    if enabled and not text:
        return {"ok": False, "error": "公告内容不能为空（不想展示请关闭开关）"}
    import membership as _M
    out = _M.push_member_notice_to_cloud(enabled, text)
    return {"ok": bool(out.get("ok")), "cloud": out,
            "notice": out.get("notice") or {"enabled": enabled, "text": text}}


# ── AI 积分成本配置（2026-10-05 新增）────────────────────────────────────────
# 用户定档：「所有 Key 由后台配好」+「每个功能每次消耗多少积分要能在后台配」。
# 这两个接口把 `membership.AI_CREDIT_COSTS` 的运行时生效价暴露出来并允许改价。
#
# 优先级：这里写入的 `credit_costs`（plans.json 覆盖层）**高于**代码默认表。
# GET 只返回成本数字与展示字段，**不含任何凭据**。
@router.get("/api/admin/ai/credit-costs")
def admin_ai_credit_costs(request: Request = None) -> dict[str, Any]:
    """超级管理员：读取 AI 积分成本表（生效价 + 代码默认价 + 真实调用点）。"""
    require_admin(request)
    from membership import (AI_CREDIT_COSTS, FEATURE_USAGE_DEFS,
                            credit_cost_table, free_trial_policy)
    pol = free_trial_policy()
    try:
        from membership import load_plan_overrides
        ov = (load_plan_overrides().get("free_quota") or {})
    except Exception:
        ov = {}
    return {
        "ok": True,
        "costs": credit_cost_table(),
        "registered": len(AI_CREDIT_COSTS),
        # 🔴 2026-10-06 用户要求「所有功能都列出来（含纯免费的）」：这里是后台
        # 「系统配置 → 套餐与积分成本 → 免费额度」分栏的数据源。把两套东西
        # 一起返回，前端分两组渲染：
        #   · 纯免费功能（FEATURE_USAGE_DEFS）：不消耗积分，走**日配额**（免费 N 次/日）
        #   · 消耗积分的功能（AI_CREDIT_COSTS）：扣积分，可用**首次体验**免费名额
        # 两者口径不同，混在一起会让人以为「下载 10/日」和「首次体验 1 次」是一回事。
        "free_quota": {
            # 🔴 免费额度口径（2026-10-13「免费额度做个可选项」）：on=按下面数字执行
            #    （默认）；off=免费用户不再享有每日免费（回到付费墙，会员每日不变）。
            #    与 daily_*_limits 同层覆盖、同一个「保存免费额度」按钮，即时生效。
            "mode": _M_free_quota_mode(),
            "modes": list(_M_free_quota_modes()),
            "daily_features": _daily_feature_rows(),
            "credit_features": [
                {
                    "op": op,
                    "name": cfg.get("name") or op,
                    "where": cfg.get("where") or "",
                    "real_cost": cfg.get("real_cost") or "",
                    "note": cfg.get("note") or "",
                    # 有没有真实的计费拦截点（无 = 现在完全免费随便用，属财务漏洞）
                    "has_gate": _has_cost_gate(op),
                }
                for op, cfg in AI_CREDIT_COSTS.items()
            ],
            "overrides": ov if isinstance(ov, dict) else {},
            "limits": {
                "cloud_lifetime": _cloud_quota_limits()[0],
                "cloud_daily_auto": _cloud_quota_limits()[1],
                # 🔴 2026-10-06 拆池：终身免费次数按功能独立（视频解说 / 在线转码 /
                # 在线去水印 / 在线字幕处理 各自一份），后台可逐功能配。
                # 兼容旧前端：cloud_lifetime 仍为单值（取默认 resource 的值）；
                # cloud_lifetime_per_resource 是 per-resource 字典，新前端用它渲染 4 个独立输入。
                "cloud_lifetime_per_resource": _cloud_lifetime_per_resource(),
            },
        },
        # 免费用户「首次体验」策略（2026-10-05 晚用户定档：账号首次使用免费一次）。
        # 与 `credit_costs` 同处 plans.json 覆盖层，改完即时生效、不需要重新打包。
        "free_trial": pol,
        "trial_modes": ["per_op", "once", "off"],
        # 计费口径说明，前端要如实展示给管理员（这是产品定档，不是实现细节）
        "policy": {
            "scope": "云端算力 + 本机重算力都计费（2026-10-05 用户定档）",
            "granularity": "按「次」定价：一次操作内发生多次模型调用只扣一份，重试成本含在单价里",
            "local_note": "本机功能（字幕提取 / 去水印 / 本地抠图 / 本机大模型解说 / 声音克隆）同样计费，"
                          "因为它们真实占用用户的 CPU 与内存。",
            "trial": "只要是首次使用的账号，其第一次使用 AI 功能免费一次（2026-10-05 用户定档）；"
                     "已经在 AI 会员有效期内或有足够积分的用户不消耗名额（先花自己的积分）。",
        },
    }


@router.post("/api/admin/ai/credit-costs")
def admin_ai_set_credit_costs(
    request: Request = None,
    payload: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    """超级管理员：调整各功能的积分单价。

    payload: `{"costs": {"matting_cloud": 60, ...}, "reset": ["op", ...],
               "free_trial": {"enabled": true, "mode": "once"|"per_op"|"off", ...}}`
      · 只提交要改的项，其余保持不变（不覆盖别人的改动）。
      · 传 `0` = 该功能免费（显式免费，不再依赖「表外默认 0」）。
      · `reset` 列出要恢复代码默认价的 op。
      · **op 必须已在 `AI_CREDIT_COSTS` 中登记**，未知 op 直接报错 ——
        防止打错字导致配置无效（且避免把 typo 写进 plans.json 变成死配置）。

    `free_trial` 是免费用户「首次体验」策略（2026-10-05 晚定档：账号首次使用免费一次）。
    同样按字段合并，**只传要改的键**（比如只切 `enabled`）即可，其余保持原值。
    """
    require_super(request)
    from fastapi import HTTPException
    from membership import (AI_CREDIT_COSTS, credit_cost_table, free_trial_policy,
                            load_plan_overrides, save_plan_overrides)

    costs_in = payload.get("costs")
    if costs_in is not None and not isinstance(costs_in, dict):
        raise HTTPException(status_code=400, detail="costs 必须是对象 {op: 积分}")

    current = dict(load_plan_overrides().get("credit_costs") or {})
    unknown = [k for k in (costs_in or {}) if k not in AI_CREDIT_COSTS]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"未登记的计费项：{unknown}（可用项：{sorted(AI_CREDIT_COSTS)}）")

    for op, v in (costs_in or {}).items():
        try:
            val = int(v)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{op} 的积分必须是整数，收到 {v!r}")
        if val < 0:
            raise HTTPException(status_code=400, detail=f"{op} 的积分不能为负")
        current[op] = val

    reset = payload.get("reset") or []
    if isinstance(reset, list):
        for op in reset:
            if op in AI_CREDIT_COSTS:
                # 🔴 用 `None` 标记删除，**不能**在 Python 侧先 pop 掉再提交：
                #   `membership.save_plan_overrides` 的合并语义是「表内逐条合并、
                #   条目值为 null 视为删除」（2026-09-24 起）。若提交时少了这个键，
                #   旧值会原样留在 plans.json —— 表现为「点了恢复默认，价没变」（实测踩到）。
                current[op] = None

    trial_in = payload.get("free_trial")
    if trial_in is not None and not isinstance(trial_in, dict):
        raise HTTPException(status_code=400, detail="free_trial 必须是对象")
    if costs_in is None and not reset and not trial_in:
        raise HTTPException(status_code=400, detail="没有要保存的改动")

    # 一次落盘两张表（同一个 plans.json），避免中间态被别人读到
    patch: dict[str, Any] = {"credit_costs": current}
    if isinstance(trial_in, dict) and trial_in:
        patch["free_trial"] = _validate_free_trial(trial_in, AI_CREDIT_COSTS)
    save_plan_overrides(patch)
    return {"ok": True, "costs": credit_cost_table(),
            "free_trial": free_trial_policy()}


def _validate_free_trial(trial_in: dict[str, Any], known_ops: dict) -> dict[str, Any]:
    """校验并归一 `free_trial` 载荷：只留认识的键，非法值直接 400。

    🔴 只认识 `DEFAULT_FREE_TRIAL_POLICY` 里的键 —— 后台把 `per_op` 拼错成
    `per-op` 之类，写进 plans.json 会造成**整条策略静默失效**（读表时收敛回默认值），
    管理员看着「明明配了」却不起效，属于最难查的一类事故，所以宁可当场报错。

    ⚠️ 恢复默认必须传 None（不能用 Python 侧 pop）：合并语义见上方 `reset` 注释。
    """
    from fastapi import HTTPException as _HE
    from membership import DEFAULT_FREE_TRIAL_POLICY, _TRIAL_MODES
    out: dict[str, Any] = {}
    unknown = [k for k in trial_in if k not in DEFAULT_FREE_TRIAL_POLICY]
    if unknown:
        raise _HE(status_code=400,
                  detail=f"未知的试用策略字段：{unknown}（可用：{sorted(DEFAULT_FREE_TRIAL_POLICY)}）")
    for k, v in trial_in.items():
        if v is None:
            out[k] = None            # 显式删除 ⇒ 回退代码默认值
            continue
        if k == "enabled" or k == "members_too":
            if not isinstance(v, bool):
                raise _HE(status_code=400, detail=f"{k} 必须是 true/false")
            out[k] = v
        elif k == "mode":
            mode = str(v).strip().lower()
            if mode not in _TRIAL_MODES:
                raise _HE(status_code=400,
                          detail=f"未知的试用口径：{mode}（可选：{list(_TRIAL_MODES)}）")
            out[k] = mode
        elif k == "exclude":
            if not isinstance(v, (list, tuple)):
                raise _HE(status_code=400, detail="exclude 必须是数组 [op, ...]")
            bad = [x for x in v if str(x) not in known_ops]
            if bad:
                raise _HE(status_code=400,
                          detail=f"exclude 里有未登记的计费项：{bad}（可用项：{sorted(known_ops)}）")
            out[k] = [str(x) for x in v]
        elif k == "max_cost":
            try:
                mc = int(v)
            except (TypeError, ValueError):
                raise _HE(status_code=400, detail=f"max_cost 必须是整数，收到 {v!r}")
            if mc < 0:
                raise _HE(status_code=400, detail="max_cost 不能为负")
            out[k] = mc
        else:  # pragma: no cover — 上面已经过一遍键名白名单
            out[k] = v
    return out


# ── AI 大模型账户（2026-09-24 新增）────────────────────────────────────────
# 超级管理员面板用：汇总各 AI 提供方的「余额 / 使用模块 / 账号标识 / 充值入口」。
# DeepSeek 余额走网关实时取（Key 仅在服务端）；百炼/火山无通用余额 REST，
# 标记为 console（前端给出控制台/充值链接）。
import json
import os
import urllib.error
import urllib.request


def _gw_get(url: str, token: str) -> dict:
    """带 Bearer token 调网关，绕过本机代理（direct:// 语义），失败返回 ok=False。"""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=12) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _gateway_base() -> tuple:
    """返回 (base_url, token) 或 (None, None)。

    🔴 2026-10-05：原先这里**硬编码 `~/.video-downloader/gateway_managed.json`**
    并重复实现了一遍 url 归一化（`replace("direct://")` + `rstrip("/")`），绕过了
    `gateway_config` 的 `VDL_HOME` 覆盖与三级优先级 —— 于是：
      · 测试/隔离实例里 `VDL_HOME` 指临时目录，本函数却仍读家目录 ⇒ 恒判「无网关」；
      · `save_managed_gateway()` 明明写成功了，面板还是显示未配置。
    改为直接调 `gateway_config.get_gateway_config()`，单一真源。
    """
    try:
        from gateway_config import get_gateway_config, _strip_direct
        cfg = get_gateway_config()
    except Exception:
        return None, None
    url = _strip_direct(str(cfg.get("url") or ""))
    return (url or None), (cfg.get("token") or None)


def _mask(s: str) -> str:
    s = str(s or "")
    return "****" if len(s) <= 8 else f"{s[:4]}\u2026{s[-4:]}"


def _deepseek_account() -> dict:
    info = {
        "id": "deepseek",
        "name": "DeepSeek（解说大模型）",
        "provider": "deepseek",
        "model": "",
        # 🔴 2026-10-05 原为硬编码两条文案（"modules": ["视频解说 / 解说词生成", …]），
        # 在没配网关 / 余额耗尽时仍显示成可用。改为空，末尾按真实就绪态填充。
        "modules": [],
        "account": "",
        "status": "unknown",
        "balance": None,
        "balances": [],
        "currency": None,
        "is_available": None,
        "balance_source": "none",
        "modules": [],      # 探测式填充，见函数末尾
        "recharge_url": "https://platform.deepseek.com/top_up",
        "console_url": "https://platform.deepseek.com",
        "note": "",
    }
    base, token = _gateway_base()
    if not base:
        info["note"] = "未找到网关配置（gateway_managed.json）"
        info["status"] = "no_gateway"
        info["modules"] = [
            "✗ 未配置网关：视频解说 / 解说词生成不可用",
            "✗ 未配置网关：长片云端兜底 LLM 不可用",
        ]
        return info
    # 模型名 + 健康
    try:
        h = _gw_get(base + "/health", token)
        if h and h.get("ok"):
            info["model"] = (h.get("models") or [""])[0]
        elif h:
            info["note"] = h.get("error", "")
    except Exception as e:
        info["note"] = f"网关查询失败：{e}"
    # 余额（实时）
    try:
        b = _gw_get(base + "/balance", token)
        if b and b.get("ok"):
            info["balance"] = b.get("balance")
            info["balances"] = b.get("balances") or []
            info["currency"] = b.get("currency")
            info["is_available"] = b.get("is_available")
            info["balance_source"] = "live"
            info["status"] = "ok" if b.get("is_available") else "insufficient"
            if not b.get("is_available"):
                info["note"] = "余额已耗尽/为负，云端解说调用会被拒，请尽快充值"
            info["account"] = f"网关令牌 {_mask(token)}（上游 Key 仅在服务端）"
        elif b:
            info["balance_source"] = "error"
            info["status"] = "error"
            info["note"] = b.get("error", "余额查询失败")
            info["account"] = f"网关令牌 {_mask(token)}"
    except Exception as e:
        info["status"] = "error"
        info["note"] = f"余额查询异常：{e}"
    # 🔴 2026-10-05 模块清单改为**探测式**（原先是硬编码文案，见下方说明）。
    #   两条模块的真实落点都已核实：
    #     · 视频解说 / 解说词生成 → commentary-pipeline/scripts/llm_script.py:157
    #       `_call_llm()`，LLM_API_KEY 硬依赖（缺失即报错）。
    #     · 长片云端兜底 LLM     → llm_script.py:2364
    #       `_local_capability_exceeded(total_duration, len(transcript))`，
    #       视频 >30min（VDL_LOCAL_MAX_VIDEO_SEC）或转写稿 >2.6 万字时，把本次任务
    #       的 LLM_ENGINE 由 auto 切成 cloud（长稿上本机 3B 模型会退化成复读机）。
    #   硬编码文案会在「没配网关 / 余额耗尽」时仍显示成可用 —— 与本轮
    #   「不显示做不到的事」定档冲突，故按真实就绪态标注。
    _gw_ok = bool(token) and info.get("status") in ("ok", "unknown", "insufficient")
    _flag = "✓" if _gw_ok else "✗"
    _suffix = "" if _gw_ok else " —— 不可用"
    info["modules"] = [
        f"{_flag} 视频解说 / 解说词生成{_suffix}",
        f"{_flag} 长片云端兜底 LLM（视频 >30 分钟自动转云端）{_suffix}",
    ]
    return info


def _dashscope_account() -> dict:
    info = {
        "id": "dashscope",
        "name": "阿里百炼 DashScope（视觉 / VLM）",
        "provider": "dashscope",
        "model": "",
        "modules": [],      # 探测式填充，见下方 try 块
        "account": "",
        "status": "unknown",
        "balance": None,
        "balances": [],
        "currency": None,
        "is_available": None,
        "balance_source": "console",
        "recharge_url": "https://billing.console.aliyun.com/?#/account/balance",
        "console_url": "https://dashscope.console.aliyun.com/",
        "note": "余额请登录阿里云费用中心查看；有免费额度，中文 OCR 强。",
    }
    try:
        from vision_config import get_vision_config, managed_status as _vmg
        cfg = get_vision_config()
        key = (cfg.get("api_key") or "").strip()
        info["model"] = cfg.get("model") or ""
        info["account"] = _mask(key) if key else "(未配置 Key)"
        info["status"] = "configured" if key else "not_configured"
        # 🔴 2026-10-05 探测式：区分「有云端 Key」与「走本机回退」两种能力档位。
        #   provider=auto 且无 Key 时并非不可用 —— 管线会回退本机离线 OCR
        #   （`vision_analysis.py` 文档 + process.py:547 的优雅降级分支），
        #   所以文案不能说成「不可用」，要说清「当前走哪一档」。
        mg = _vmg()
        has_key = bool(key) or bool(mg.get("configured"))
        source = mg.get("source", "user")
        if has_key:
            info["modules"] = [
                f"✓ 视觉理解 / 图片 OCR（Key 来源：{_SOURCE_CN.get(source, source)}）",
                "✓ 抠图 VLM 自动分类（AI 智能识别）",
            ]
        else:
            info["modules"] = [
                "○ 视觉理解 / 图片 OCR —— 未下发云端 Key，自动档回退本机离线识别",
                "○ 抠图 VLM 自动分类 —— 未下发云端 Key，自动档回退本机引擎",
            ]
            info["note"] = ("凭据由管理员在本面板下发；未下发时 Mac 上自动走本机离线 OCR"
                            "（免费、无需 Key），不会报错。")
    except Exception as e:  # noqa: BLE001
        info["note"] = f"读取视觉配置失败：{e}"
        info["status"] = "error"
    return info


def _volcengine_account() -> dict:
    """火山引擎账户卡（信息与实际链路对齐，2026-09-25 校正）：

    - 云端抠图（说扣什么）：visual.volcengineapi.com，SigV4 签名，用 **AK/SK**。
    - 抠图增强 / AI 画质增强：AI MediaKit，用 **Bearer Key**（mediakit_api_key）。
    - 云端去水印：**不依赖火山**（本地 LaMa 精修链路），旧卡片把它挂在火山名下是旧信息。
    """
    info = {
        "id": "volcengine",
        "name": "火山引擎 Volcengine（云端抠图 / 画质增强）",
        "provider": "volcengine",
        "model": "visual · mediakit",
        "modules": [],
        "account": "",
        "status": "unknown",
        "balance": None,
        "balances": [],
        "currency": None,
        "is_available": None,
        "balance_source": "console",
        "recharge_url": "https://console.volcengine.com/wallet",
        "console_url": "https://console.volcengine.com/",
        "note": "按量计费，余额请登录火山控制台「费用中心」查看。",
    }
    try:
        from cloud_matting_config import cloud_matting_source, get_cloud_matting_config
        cfg = get_cloud_matting_config()
        ak = (cfg.get("access_key") or "").strip()
        sk = (cfg.get("secret_key") or "").strip()
        mk = (cfg.get("mediakit_api_key") or "").strip()
        enabled = bool(cfg.get("enabled"))
        # 2026-10-13：凭据可能由**服务端**代持（网关代做，火山钥匙不落到用户机器上）
        info["key_source"] = cloud_matting_source()          # server / local / none
        by_server = info["key_source"] == "server"
        visual_ready = bool(ak and sk) or by_server
        mk_ready = bool(mk) or by_server
        info["account"] = ("本产品服务端（钥匙不下发）" if by_server
                           else (_mask(ak) if ak else "(未配置 AccessKey)"))
        # 模块清单带实时就绪标记，用户一眼看到哪条链路可用，以及凭据落在哪一侧
        info["modules"] = [
            "云端抠图 / 说扣什么（visual）" + (
                "✓ 服务端代做" if by_server else ("✓ 本机 AK/SK" if visual_ready else "✗ 未配置")),
            "抠图增强 / AI 画质增强（AI MediaKit）" + (
                "✓ 服务端代做" if by_server else ("✓ 本机 Key" if mk_ready else "✗ 未配置")),
        ]
        if not enabled:
            info["status"] = "disabled"
            info["note"] = "云端抠图开关未启用（抠图设置 → ☁️ 云端抠图）；启用后「说扣什么」走火山像素级。"
        elif visual_ready:
            info["status"] = "enabled"
            # 🔴 2026-10-05：原先只说「MediaKit 增强已就绪 / 未配…回退」，顶部状态标签
            # 却是「已启用」—— 用户会以为画质增强也能用。补明确说明。
            # 🔴 2026-10-13：服务端代做时钥匙根本不在本机，文案必须如实说清来源。
            note = ("云端抠图已就绪：由**本产品服务端**代做，火山凭据不下发到本机。"
                    if by_server else "云端抠图已就绪，凭据来自本机配置。")
            if not mk_ready:
                note += ("⚠️ AI 画质增强 / 增强抠图**不可用**（缺 MediaKit 凭据），"
                         "目前会回退 visual/本地处理 —— 右上角「已启用」仅代表云端抠图。")
            info["note"] = note
        else:
            info["status"] = "no_api_key"
            info["note"] = ("未取得云端抠图凭据（服务端未启用代做，本机也未配置）："
                            "将回退本地 SAM/MODNet。")
    except Exception as e:  # noqa: BLE001
        info["note"] = f"读取火山配置失败：{e}"
        info["status"] = "error"
    return info


def _collect_ai_accounts() -> list:
    return [_deepseek_account(), _dashscope_account(), _volcengine_account()]


@router.get("/api/admin/ai/accounts")
def admin_ai_accounts(request: Request = None) -> dict[str, Any]:
    """超级管理员：汇总各 AI 提供方账户（余额 / 模块 / 账号 / 充值入口）。"""
    require_admin(request)
    return {"ok": True, "accounts": _collect_ai_accounts()}


# ── AI 凭据下发（2026-10-05）──────────────────────────────────────────────────
# 背景：产品定档「所有 Key 都由后台配好，用户不需要自己填」。用户界面上的
# Key 输入框已全部隐藏，但**此前没有任何下发通道** —— 云端抠图的 AK/SK 只能
# 手工写进 `cloud_matting.json`：换机/重装/分发即丢，新用户机器上根本没有，
# 界面却显示「管理员已配置」。这是本组接口要补的最后一环。
#
# 🔴 安全约定（务必保持）：
#   1. 三个端点都只接受「已登录 + 超级用户」（require_admin）。
#   2. **GET 绝不返回明文 Key**，只返回 `*_masked`（前 6 + 末 4）与就绪布尔。
#   3. 写入走 *_managed.json（0600），用户文件改不动它。
@router.get("/api/admin/ai/managed")
def admin_ai_managed(request: Request = None) -> dict[str, Any]:
    """超级管理员：读取三家的受管凭据状态（**永不返回明文**）。"""
    require_admin(request)
    from cloud_matting_config import cloud_matting_source
    from cloud_matting_config import managed_status as mat_managed
    from vision_config import managed_status as vis_managed
    from gateway_config import gateway_status
    gw = gateway_status()
    return {
        "ok": True,
        "volcengine": mat_managed(),
        "dashscope": vis_managed(),
        "deepseek": {
            "configured": bool(gw.get("enabled") and gw.get("has_token")),
            "source": gw.get("source", ""),
            "url": gw.get("url", ""),
            "token_masked": gw.get("token_masked", ""),
        },
        # 2026-10-13：云端抠图可能由服务端代做（钥匙只在网关机）——把来源摊开给后台看
        "matting_source": cloud_matting_source(),
    }


@router.post("/api/admin/ai/managed/volcengine")
def admin_ai_set_volcengine(
    request: Request = None,
    payload: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    """超级管理员：下发云端抠图（火山）凭据到受管配置。

    空字符串的字段**保持原值不变**（便于前端只提交改了的那几项，不会把没填
    的 Key 抹掉）；显式传 `"__clear__"` 才清空。
    """
    require_super(request)
    from cloud_matting_config import get_cloud_matting_config, save_managed_config
    cur = get_cloud_matting_config()

    def _pick(new: str, old: str) -> str:
        new = str(new or "").strip()
        if new == "__clear__":
            return ""
        return new or old

    save_managed_config({
        "access_key": _pick(payload.get("access_key"), cur.get("access_key", "")),
        "secret_key": _pick(payload.get("secret_key"), cur.get("secret_key", "")),
        "mediakit_api_key": _pick(payload.get("mediakit_api_key"), cur.get("mediakit_api_key", "")),
        "enhance_version": str(payload.get("enhance_version") or cur.get("enhance_version") or "professional"),
        "enabled": bool(payload.get("enabled", True)),
    })
    return {"ok": True, "status": admin_ai_managed(request)["volcengine"]}


@router.post("/api/admin/ai/managed/dashscope")
def admin_ai_set_dashscope(
    request: Request = None,
    payload: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    """超级管理员：下发视觉理解（DashScope / 任意 OpenAI 兼容多模态）凭据。"""
    require_super(request)
    from vision_config import get_vision_config, save_managed_vision_config
    cur = get_vision_config()

    def _pick(new: str, old: str) -> str:
        new = str(new or "").strip()
        if new == "__clear__":
            return ""
        return new or old

    save_managed_vision_config({
        "provider": str(payload.get("provider") or cur.get("provider") or "auto"),
        "api_key": _pick(payload.get("api_key"), cur.get("api_key", "")),
        "base_url": _pick(payload.get("base_url"), cur.get("base_url", "")),
        "model": str(payload.get("model") or cur.get("model") or ""),
    })
    return {"ok": True, "status": admin_ai_managed(request)["dashscope"]}


@router.post("/api/admin/ai/managed/deepseek")
def admin_ai_set_deepseek(
    request: Request = None,
    payload: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    """超级管理员：下发云端网关（解说大模型）配置。

    🔴 这里下发的是**网关地址 + 令牌**，不是 DeepSeek 官方 Key —— 真实 Key 只留在
    ECS 网关侧（`upstream.json`），本机永远不接触上游凭据。
    """
    require_super(request)
    from gateway_config import get_gateway_config, save_managed_gateway
    cur = get_gateway_config()

    def _pick(new: str, old: str) -> str:
        new = str(new or "").strip()
        if new == "__clear__":
            return ""
        return new or old

    save_managed_gateway({
        "url": _pick(payload.get("url"), cur.get("url", "")),
        "token": _pick(payload.get("token"), cur.get("token", "")),
        "enabled": bool(payload.get("enabled", True)),
    })
    return {"ok": True, "status": admin_ai_managed(request)["deepseek"]}


# ── 用户使用详情（2026-10-03）───────────────────────────────────────────────
# 动机：用户反馈「用不了/很卡」时，管理员需要能看到他真实的权益、配额消耗、
# 激活与购买历史、客服原文（含自动附加的诊断日志），而不是靠猜。
# 纯函数 _usage_bundle 组装 → 便于离线测试；接口只做鉴权 + 拼装。

_USAGE_DAYS = 14          # 使用历史回看天数
_USAGE_MSG_PREVIEW = 160  # 客服消息摘要字数


def _usage_bundle(uid: str, ident: str, disabled: bool, is_admin: bool,
                  created_at: Any, store: Any) -> dict[str, Any]:
    """把一个用户的「使用情况」拼成一份可直接渲染的结构（纯函数，不碰网络）。"""
    import time as _t

    now = _t.time()
    try:
        st = store.status()
    except Exception:  # noqa: BLE001 — 状态读失败不该让整个面板 500
        st = {}

    # ---- 使用历史（按天，末 N 天；新的在前）----
    try:
        hist = dict(getattr(store, "_state", {}).get("usage_history") or {})
    except Exception:  # noqa: BLE001
        hist = {}
    def _counts(row: Any) -> dict[str, int]:
        """只取「资源名 → 次数」，跳过 date 这类非数值字段。"""
        out: dict[str, int] = {}
        for k, v in (row or {}).items():
            try:
                n = int(v or 0)
            except (TypeError, ValueError):
                continue        # date="2026-10-03" 之类，不是计数
            if n > 0:
                out[str(k)] = n
        return out

    def _details(row: Any) -> dict[str, list[dict[str, Any]]]:
        """取「资源名 → 明细列表」（2026-10-19：让面板显示下载了什么）。"""
        out: dict[str, list[dict[str, Any]]] = {}
        raw = (row or {}).get("details")
        if not isinstance(raw, dict):
            return out
        for k, v in raw.items():
            if not isinstance(v, list):
                continue
            items: list[dict[str, Any]] = []
            for it in v[-50:]:
                if not isinstance(it, dict):
                    continue
                t = str(it.get("t") or "")[:120]
                if not t:
                    continue
                try:
                    at = int(it.get("at") or 0)
                except (TypeError, ValueError):
                    at = 0
                _row = {"t": t, "p": str(it.get("p") or "")[:64], "at": at}
                # 2026-10-20：把「这次是否下载成功」透传给面板
                # （r=1 成功＝下载完成并保存到本机 / r=0 未完成 / 无 r＝未上报）。
                # 老数据没有 r ⇒ 保持缺省，面板显示灰色「未上报」，不假装成功。
                if it.get("r") is not None:
                    try:
                        _row["r"] = 1 if int(it.get("r") or 0) > 0 else 0
                    except (TypeError, ValueError):
                        pass
                items.append(_row)
            if items:
                out[str(k)[:32]] = items
        return out

    days = []
    for day in sorted(hist.keys(), reverse=True)[:_USAGE_DAYS]:
        items = _counts(hist.get(day))
        days.append({"date": day, "total": sum(items.values()), "items": items,
                     "details": _details(hist.get(day))})
    # 2026-10-20 修：`usage_history` 只装**已日切**的日子 —— 「今天」的数据还在
    # `daily_usage` / `daily_details` 里，根本不在 hist 里。旧写法是「遍历 days
    # 找今天、找到才补明细」，等于永远补不上；更致命的是把 `daily_details`
    # 这个**字典本身**喂给了 `_details()`（它要的是带 `details` 键的一整行）⇒
    # 取到 None ⇒ 明细永远为空。这就是「下载记录还是看不到详情」的真身。
    # 现在：按 `daily_usage["date"]`（真实日期，不硬按今天算）补一行进 days。
    _state = getattr(store, "_state", {}) or {}
    _today_details = _details({"details": _state.get("daily_details") or {}})
    _today_row_raw = _state.get("daily_usage") or {}
    _live_day = str(_today_row_raw.get("date") or "")[:10]
    _live_counts = _counts(_today_row_raw)
    if _live_day and (_live_counts or _today_details):
        _live = next((d for d in days if d["date"] == _live_day), None)
        if _live is None:
            _live = {"date": _live_day, "total": sum(_live_counts.values()),
                     "items": _live_counts, "details": {}}
            days.append(_live)
            days.sort(key=lambda d: str(d.get("date") or ""), reverse=True)
        if _today_details:
            _live.setdefault("details", {})
            for _k, _v in _today_details.items():
                _live["details"][_k] = _v
    today_row = _today_row_raw
    today_items = _live_counts

    # ---- 激活 / 购买历史（倒序）----
    meta = (getattr(store, "_state", {}) or {}).get("meta") or {}
    acts = []
    for h in list(meta.get("history") or [])[-50:][::-1]:
        acts.append({
            "code": str(h.get("code") or ""),
            "type": str(h.get("type") or ""),
            "via": str(h.get("via") or ""),
            "at": float(h.get("at") or 0),
        })

    # 2026-10-22 权限拆两级：弹窗标题的标签要跟着「超管 / 管理员」分开展示
    from auth_store import is_super_identifier as _is_super_ident
    _role = ""
    if is_admin:
        _role = "super" if _is_super_ident(ident) else "admin"
    return {
        "user_id": uid,
        "identifier": ident,
        "disabled": bool(disabled),
        "is_admin": bool(is_admin),
        "role": _role,
        "created_at": created_at,
        "membership": st,
        "today": {"date": today_row.get("date") or _t.strftime("%Y-%m-%d"),
                  "items": today_items,
                  "total": int(sum(today_items.values()))},
        "usage_days": days,
        "usage_summary": {
            "days": len(days),
            "active_days": sum(1 for d in days if d["total"] > 0),
            "total": sum(d["total"] for d in days),
        },
        "activations": acts,
        "device_fp": str(meta.get("device_fp") or ""),
        "activated_at": float(meta.get("activated_at") or 0),
        "account_bound_at": float(meta.get("account_bound_at") or 0),
    }


def _usage_support(uid: str) -> dict[str, Any]:
    """该用户的客服会话（排障关键：用户原文 + 错误上报时自动附加的诊断日志）。"""
    try:
        from routers.support import _read_threads
        threads = [t for t in _read_threads() if t.get("user_id") == uid]
    except Exception:  # noqa: BLE001
        threads = []
    threads.sort(key=lambda t: t.get("updated_at") or 0, reverse=True)
    out = []
    for t in threads[:5]:
        msgs = t.get("messages") or []
        last_user = ""
        for m in msgs:
            if m.get("role") == "user":
                last_user = str(m.get("text") or "")
        out.append({
            "id": t.get("id"),
            "status": t.get("status"),
            "msg_count": len(msgs),
            "created_at": t.get("created_at"),
            "updated_at": t.get("updated_at"),
            "last_user_text": last_user[:_USAGE_MSG_PREVIEW],
            "has_diagnostics": "[错误上报]" in last_user,
        })
    return {"threads": out, "total_threads": len(threads)}


def _usage_cloud_only(ident: str) -> dict[str, Any]:
    """云端账号（本机无镜像）的排障视图：只呈现云端档案，不谎报本机用量为 0。

    2026-10-16：用户列表改为云端总库后，本机没有镜像的账号（在别的端注册）
    点「使用详情」原先直接报「用户不存在」—— 现在改为给云端视角：
    权益（会员到期 / 积分）取云端 authority，购买流水取云端 purchases，
    设备取云端 devices；本机用量与客服记录如实标注为空。
    """
    now = time.time()
    cloud = _usage_cloud(ident)
    data = (cloud.get("data") or {}) if cloud.get("ok") else {}
    auth = data.get("authority") or {}
    dl_until = float(auth.get("member_until_dl") or 0)
    ai_until = float(auth.get("member_until_ai") or 0)
    ai_left = int(auth.get("ai_credits_left") or 0)
    perm = int(auth.get("perm_credits") or 0)
    dl_active = dl_until > now
    ai_active = ai_until > now
    devs = [d for d in (data.get("devices") or []) if isinstance(d, dict)]
    acts = []
    for p in reversed(list(data.get("purchases") or [])[-50:]):
        if not isinstance(p, dict):
            continue
        acts.append({"code": str(p.get("plan_code") or p.get("code") or ""),
                     "type": "purchase", "via": "pay",
                     "at": float(p.get("at") or 0)})
    return {
        "user_id": ident,
        "identifier": ident,
        "source": "cloud",
        "disabled": bool(data.get("banned")),
        "is_admin": bool(data.get("is_admin")),
        "created_at": data.get("created_at"),
        "membership": {
            "download_member": {"active": dl_active, "plan": None,
                                "expire_at": dl_until,
                                "source": "cloud" if dl_active else None},
            "ai_member": {"active": ai_active, "plan": None,
                          "expire_at": ai_until,
                          "credits_left": ai_left if ai_active else 0,
                          "grant_credits": 0},
            "permanent_credits": perm,
            "credits_total": ai_left + perm,
            "daily_usage": {},
        },
        "today": {"date": time.strftime("%Y-%m-%d"), "items": {}, "total": 0},
        "usage_days": [],
        "usage_summary": {"days": 0, "active_days": 0, "total": 0},
        "activations": acts,
        "device_fp": str(devs[0].get("fp") or "") if devs else "",
        "activated_at": 0.0,
        "account_bound_at": 0.0,
        "support": {"threads": [], "total_threads": 0},
        "cloud": cloud,
        # 云端资料备份的元数据（分区清单 / Cookie 平台清单 / 条数 / 更新与设备），
        # 不含密文与内容 —— 面板按这个渲染「云端资料」区块。
        "cloud_data": data.get("data_meta"),
        "cloud_only": True,
    }


@router.get("/api/admin/users/{user_id}/usage")
def admin_user_usage(user_id: str, request: Request = None) -> dict[str, Any]:
    """超级管理员：查看单个用户的完整使用情况（排障用）。

    含：账号档案 / 会员与积分 / 今日与近 14 天配额消耗 / 激活购买历史 /
    客服会话原文 / 云端授权状态（可拉时）。

    🔴 2026-10-16：列表改为云端总库后 user_id 可能是**账号标识**（本机没有
    镜像的账号，如在别的端注册）—— 这类账号改为返回云端档案，不再报
    「用户不存在」。
    """
    require_admin(request)
    import auth_store
    from user_membership import get_user_store

    uid = _local_uid(user_id)
    ident = _resolve_identifier(user_id)
    user = next((u for u in auth_store._load_users().get("users", [])
                 if u.get("user_id") == uid and not u.get("deleted_at")), None) if uid else None
    if not user:
        return {"ok": True, "usage": _usage_cloud_only(ident)}
    store = get_user_store(uid)
    bundle = _usage_bundle(
        uid,
        str(user.get("identifier") or uid),
        bool(user.get("disabled")),
        bool(user.get("is_admin")),
        user.get("created_at"),
        store,
    )
    bundle["support"] = _usage_support(uid)
    bundle["cloud"] = _usage_cloud(user.get("identifier") or "")
    # 2026-10-16：云端资料（用户信息类数据备份）的**元数据**——只给「有哪些分区、
    # Cookie 平台清单/条数/更新时间」，不含密文也不含内容，与云端账号口径统一。
    bundle["cloud_data"] = ((bundle["cloud"].get("data") or {}).get("data_meta")
                            if bundle["cloud"].get("ok") else None)
    return {"ok": True, "usage": bundle}


def _usage_cloud(identifier: str) -> dict[str, Any]:
    """云端授权状态（设备数 / 最近心跳 / 额度）。拉不到就降级，不影响本机数据。"""
    ident = str(identifier or "").strip()
    if not ident:
        return {"ok": False, "reason": "无账号标识"}
    try:
        import json as _json
        import urllib.request as _rq
        from admin_store import _license_admin_token
        from license_client import license_base
        token = _license_admin_token()
        if not token:
            return {"ok": False, "reason": "未配置授权中心管理员令牌"}
        url = f"{str(license_base() or '').rstrip('/')}/api/license/usage"
        body = _json.dumps({"token": token, "email": ident}).encode("utf-8")
        req = _rq.Request(url, data=body, method="POST",
                          headers={"Content-Type": "application/json"})
        with _rq.urlopen(req, timeout=6) as resp:
            data = _json.loads(resp.read().decode("utf-8") or "{}")
        if isinstance(data, dict) and data.get("ok") is False:
            return {"ok": False, "reason": str(data.get("error") or "云端无记录")}
        return {"ok": True, "data": data}
    except Exception as e:  # noqa: BLE001 — 云端不可达只提示，不阻塞
        return {"ok": False, "reason": f"云端不可达：{e}"}


# ── 套餐档位快速上下架（2026-10-03）─────────────────────────────────────────
# 场景：某档要临时停售（活动结束 / 出问题），运营不该先去「⚙ 营销」里找复选框
# 再点保存——那个保存是全表单提交，会把其它未保存的编辑一起带上去。
# 所以单开一个端点：只改这一档的 on_sale，走字段级合并，并同步云端。
# 注意：前端「下架」不是删除 —— 已购买用户的权益不受影响，前台该档隐藏且无法下单。

_SALE_TABLES = ("download_plans", "ai_plans", "credit_packs")

# 前端界面分段 id → 真实表名。前端历史上传过分段 id（dl/ai/cp）导致
# 「未知套餐类别：dl」整条下架链路不可用，这里做兼容：新旧前端都能用。
_SALE_TABLE_ALIAS = {
    "dl": "download_plans", "download": "download_plans", "download_member": "download_plans",
    "ai": "ai_plans", "ai_member": "ai_plans",
    "cp": "credit_packs", "packs": "credit_packs", "credits": "credit_packs",
    "cost": "",  # AI 积分成本不是可售档位
}


def _norm_plan_table(value: str) -> str:
    v = (value or "").strip()
    if v in _SALE_TABLES:
        return v
    return _SALE_TABLE_ALIAS.get(v, "")


def _find_plan_table(code: str) -> str:
    """按 code 在本机覆盖层里找归属表；找不到就回退到内置常量所在的表。"""
    from membership import load_plan_overrides
    ov = load_plan_overrides() or {}
    for t in _SALE_TABLES:
        if code in (ov.get(t) or {}):
            return t
    from membership import AI_PLANS, CREDIT_PACKS, DOWNLOAD_PLANS
    if code in DOWNLOAD_PLANS:
        return "download_plans"
    if code in AI_PLANS:
        return "ai_plans"
    if code in CREDIT_PACKS:
        return "credit_packs"
    return ""


@router.post("/api/admin/plans/{code}/sale")
def admin_plan_sale(code: str, payload: dict[str, Any] = Body(...),
                    request: Request = None) -> dict[str, Any]:
    """超级管理员：单个档位快速上架 / 下架（不落其它表单改动）。"""
    require_super(request)
    from admin_store import save_plan_overrides as _save
    import membership as M

    # 归属表以「按 code 定位」为权威：客户端传错表名（曾出现把分段 id 'dl' 当表名，
    # 也有传入 'ai' 却操作下载档的情况）时若盲信客户端，会把 on_sale 写进错误的表 ——
    # 表现为「提示已下架/上架，前台却没变化」。客户端 table 只在定位不到时作兜底。
    table = _find_plan_table(code) or _norm_plan_table(str(payload.get("table") or ""))
    if table not in _SALE_TABLES:
        return {"ok": False, "error": f"未知套餐类别：{payload.get('table') or '（无法定位该档）'}"}
    on_sale = bool(payload.get("on_sale", True))
    try:
        _save({table: {code: {"on_sale": on_sale}}})
    except OSError as e:  # noqa: BLE001
        return {"ok": False, "error": f"写入失败：{e}"}
    # 下架要立刻同步云端：网页版与真实收款也读那份表，否则网页还能卖下架的档
    cloud = M.push_plans_to_cloud()
    st = M.plan_sales_state(M.effective_plans().get(table, {}).get(code) or {}, now=time.time())
    return {"ok": True, "code": code, "table": table, "on_sale": on_sale,
            "state": st, "cloud": cloud}
