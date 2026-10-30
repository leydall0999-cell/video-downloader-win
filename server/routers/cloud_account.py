"""server/routers/cloud_account.py — 云端账号（登录资质，替代一机一码）。

为什么有这个文件：
  旧版把会员绑死在一台机器的指纹上（P2 一机一码），换硬盘/重装/换 Mac 就要人工
  解绑，用户反馈「卡得太死」。新语义三步走：
    · 账号密码登录（云端仲裁），会员权益挂账号不挂机器
    · 同一账号最多 N 台设备（默认 2），超出时淘汰**最久没活动**的那台
    · 本机权益写进全局 member_store，未登录也能享用；被挤掉才降级为免费档

对外接口（全部 /api/cloud/*，桌面端本机回环调用）：
  POST /register {email, password}   注册并直接登录
  POST /login    {email, password}   登录（占用一个设备位）
  POST /logout                        本地登出（权益保留）
  GET  /status                        账号快照 + 设备列表（离线可读）
  POST /sync                         心跳：拉权益续期 + 查自己是否被挤出
  POST /redeem  {code}               卡密充值到当前账号
  POST /unbind  {fp}                 登出某台设备，腾位置

设计约束：
  - **fail-open**：网络异常一律不动本地权益（断网不惩罚已付费用户）。
  - token 不外泄给前端：/status 只吐邮箱、设备列表、是否登录。
"""
from __future__ import annotations

import logging
import platform
import socket
import threading
from typing import Any, Optional

from fastapi import APIRouter, Body, Request

router = APIRouter()
logger = logging.getLogger(__name__)

_SYNC_MIN_INTERVAL = 1800.0   # 自动同步最小间隔（秒），避免每次请求都打云端


def _store():
    import app
    return app.member_store


def _sync_userdata(store, password: str) -> None:
    """登录 / 注册成功后，把「用户信息类数据」与云端对齐一次。

    换机场景就靠这一步：新机登录 → 拉云端资料 → 设置与登录态落地，用户无感。
    走**后台线程**，不阻塞登录响应；失败只记日志（同步是增强，不是登录前置）。

    Cookie 是端到端加密的（服务端只存密文），所以这里必须带上登录密码才能派生
    密钥 —— 密码只在本调用栈内存里用，不落盘、不进日志。
    """
    getter = getattr(store, "_cloud_token", None)
    token = ""
    try:
        token = str(getter() or "").strip() if callable(getter) else ""
    except Exception:
        token = ""
    if not token:
        return

    def _run() -> None:
        try:
            import user_data_cloud as udc
            rep = udc.sync(token, password or None)
            logger.info("[cloud] 用户资料同步: action=%s ok=%s",
                        rep.get("action"), rep.get("ok"))
        except Exception as e:                      # 同步失败绝不影响已完成的登录
            logger.info("[cloud] 用户资料同步跳过: %s", e)

    threading.Thread(target=_run, name="vdl-userdata-sync", daemon=True).start()


# --------------------------------------------------------------------------- #
# 云账号归属绑定（安全审计 2026-10-11，P1-8「云账号全局身份」）
# --------------------------------------------------------------------------- #
# 🔴 缺陷：`/api/cloud/status|sync|logout|unbind` 一律操作**全局** member_store，
#   且完全不校验调用者。于是任何能访问该端口的人都能：
#     · GET /api/cloud/status  → 读出本机云账号的邮箱与设备列表（隐私泄漏）
#     · POST /api/cloud/logout → 把别人登出（DoS）
#     · POST /api/cloud/unbind → 踢掉他人设备（占位/骚扰）
#   桌面端本意是「本机回环调用」，但网页版同源部署时这些路由同样可达。
# 现在统一绑定到**当前经验证账号**：必须携带有效登录 token，且该账号注册邮箱
# 与全局账本 account.email 一致（与 user_membership.current_member_store 的
# same_owner 口径完全相同）。非归属者：读接口回空快照，写接口 403。
def _verified_owner_email(request: Optional[Request]) -> str:
    """当前请求的**经验证**账号邮箱；未登录 / token 无效 → 空串。"""
    if request is None:
        return ""
    try:
        import app
        uid = app.get_current_user_id(request)
    except Exception:
        uid = None
    if not uid:
        return ""
    try:
        from auth_store import user_identifier
        return str(user_identifier(uid) or "").strip().lower()
    except Exception:
        return ""


def _global_account_email() -> str:
    import app
    g = app.member_store
    g._ensure_loaded()
    return str((g._state.get("meta") or {}).get("account", {}).get("email") or "").strip().lower()


def _is_cloud_owner(request: Optional[Request]) -> bool:
    email = _verified_owner_email(request)
    if not email:
        return False
    gemail = _global_account_email()
    return bool(gemail) and gemail == email


def _require_cloud_owner(request: Optional[Request]):
    """写接口门禁：非归属者一律 403。返回全局 store（已确认调用者就是归属者）。"""
    import app
    if not _verified_owner_email(request):
        raise app.HTTPException(status_code=403, detail="请先登录账号")
    if not _is_cloud_owner(request):
        raise app.HTTPException(status_code=403, detail="当前账号不是本机云账号的归属者")
    return app.member_store


_EMPTY_ACCOUNT_VIEW = {
    "logged_in": False, "email": "", "device_fp": "", "device_name": "",
    "devices": [], "max_devices": 2, "evicted": False, "last_sync": 0.0,
}


def _local_token_for(email: str, password: str) -> str:
    """云端账号即本地账号：保证两者密码一致。

    云端注册/登录成功后，确保本地 auth_store 里也有同名账号，并签发本地 token
    （功能门禁、个人中心、客服等都依赖它）。云端账号不存在时按同密码自动建本地账号，
    因此用户只需记一套邮箱+密码。
    """
    from auth_store import create_user, authenticate, issue_token
    ident = email.strip().lower()
    uid = authenticate(ident, password)
    if not uid:
        uid = create_user(ident, password)
        if uid:
            uid = authenticate(ident, password)
    if not uid:
        raise RuntimeError("本地账号签发失败")
    return issue_token(uid)


def _adopt_local_account(email: str, password: str, fp: str, name: str) -> bool:
    """把「只存在本机」的老账号补建到云端，返回是否已建号（调用方应重试登录）。

    🔴 为什么需要（2026-09-22 实测）：云端注册过去只收邮箱，手机号注册会被拒，
    而前端注册流程吞掉了这个失败、本地照常注册成功 → 这批用户「本机有账号、
    云端没有」，会员权益只在本机，换机/重装即丢，登录时还会被云端回一句
    「账号不存在」误导。

    **必须本机密码校验通过才迁移**：既杜绝替他人建号，也避免密码打错时
    在云端静默建出一个永远用不上的空账号。
    """
    try:
        from auth_store import authenticate
        if not authenticate(email, password):
            return False
    except Exception:
        return False
    try:
        import license_client
        rr = license_client.register_remote(email.strip(), password, fp, name)
    except Exception:
        return False
    return bool(rr and rr.get("ok"))


def _heal_cloud_password(email: str, password: str) -> bool:
    """云端密码与本机不一致时，把本机密码推上云端（返回是否推成功）。

    🔴 为什么需要（2026-09-22）：账号是**两套库**（本机 auth_store / 云端授权中心），
    各存一份密码哈希。只要有一侧单独改过密码（例如断网时在本机改了、或用户在网络不通
    时重置过），两边就分叉 —— 用户「这台能登、换台说密码错」，云端登录失败还会连累
    会员权益同步。这里让分叉在**下一次登录时自动收敛**。

    **三重前提，缺一不推**（防越权改别人账号的密码）：
      ① 本机账号表里该账号存在，且**当前输入的密码在本机校验通过**（用户确实持有密码）
      ② 本机保存着该账号的云端登录 token（说明这台机器上有过合法的云端会话）
      ③ 云端确认该账号存在且确实不认这个密码（调用方只在 BAD_PASSWORD 时进来）
    """
    try:
        from auth_store import authenticate
        if not authenticate(email, password):
            return False
    except Exception:
        return False
    store = _store()
    try:
        store._ensure_loaded()
        acc = (store._state.get("meta") or {}).get("account") or {}
        from credential_store import resolve_token
        token = resolve_token(acc)
        if not token or (acc.get("email") or "").strip().lower() != email.strip().lower():
            return False
        import license_client
        r = license_client.set_password_remote(email.strip(), password, token=token)
    except Exception:
        return False
    return bool(r and r.get("ok") and r.get("synced", True))


def _fp_name() -> tuple[str, str]:
    try:
        import device_id
        fp, _strong = device_id.fingerprint()
    except Exception:
        fp = ""
    if not fp:
        return "", ""
    try:
        name = f"{platform.node() or 'Mac'} · {socket.gethostname() or ''}".strip(" ·")
    except Exception:
        name = "未命名设备"
    return fp, name[:64]


def _device_meta() -> dict[str, Any]:
    """设备画像（2026-10-13）：随心跳上报，供后台「安装 / 插件次数统计（按平台）」。

    · platform：macos / windows / linux（复用 platform_model 的平台判定）；
    · app_version：App 当前版本号；
    · ext：浏览器扩展信息（browser = **优先**按扩展请求自带的 UA 判定
      chrome/edge/brave/opera/vivaldi/yandex/chromium/firefox/safari，
      认不出再退回扩展加载目录路径推断；version = 扩展 manifest 的 version）。
    任何一项取不到就省略 —— 画像缺失只影响统计丰富度，绝不让心跳失败。
    """
    meta: dict[str, Any] = {}
    try:
        import platform_model
        p = platform_model.current_platform()
        # 🔴 current_platform() 返回的是 Platform 枚举（str() 会得到
        #    "Platform.MACOS"，服务端白名单会把它丢掉）——必须取 .value。
        p = str(getattr(p, "value", p) or "").strip().lower()
        if p and p != "web":
            meta["platform"] = p
    except Exception:  # noqa: BLE001
        pass
    try:
        import system
        v = str(getattr(system, "VERSION", "") or "").strip()
        if v and v != "0.0.0":
            meta["app_version"] = v[:32]
    except Exception:  # noqa: BLE001
        pass
    try:
        import extension_sync
        cfg = extension_sync.get_config() or {}
        load_dir = str(cfg.get("load_dir") or "").strip() or str(extension_sync.managed_dir())
        ver = str(extension_sync.read_version(load_dir) or "").strip()
        # 🔴 2026-10-13（接续待办 #4 精确化）：浏览器优先按**扩展请求自带的 UA**判定
        #    —— 扩展在哪个浏览器里跑，请求的 UA 就是那个浏览器（无需改扩展）。
        #    UA 认不出再退回「加载目录路径推断」；两者都认不出时**不再瞎填 chromium**。
        browser = extension_sync.browser_from_ua(cfg.get("last_ua") or "")
        if not browser:
            browser = extension_sync.browser_from_load_dir(load_dir)
        if ver or browser:
            ext: dict[str, Any] = {}
            if browser:
                ext["browser"] = browser
            if ver:
                ext["version"] = ver[:32]
            if ext:
                meta["ext"] = ext
    except Exception:  # noqa: BLE001
        pass
    return meta


def _apply_account_state(store, acct: dict[str, Any], mirror: bool = True) -> None:
    """云端权益落地（2026-09-25 防破解核心切换点）。

    授权中心带 authority 快照（v>=1）→ 用云端真值**覆盖**本地 memberships
    （篡改的 expire_at/积分一联网即回滚）；老服务端无快照 → 退回旧的幂等追加。

    mirror=True 时顺带把同一份快照镜像到「该账号对应的 per-user store」——
    个人中心与门禁读的是后者，不镜像就会出现「云端改了、App 数字不动」
    （见 _mirror_to_user_store 的详细说明）。
    """
    auth = (acct or {}).get("authority") or {}
    if isinstance(auth, dict) and int(auth.get("v") or 0) >= 1:
        store.apply_cloud_authoritative(acct)
    else:
        store.apply_cloud_purchases(acct.get("purchases") or [])
    if mirror:
        _mirror_to_user_store(store, acct)
    # 2026-10-16：is_admin 上云 —— 云端快照是权威，落到本机账号表镜像，
    # 超管提权/降级因此全端生效；断网时本机标记仍可兜底门禁。
    _sync_cloud_is_admin(acct)


def _sync_cloud_is_admin(acct: dict[str, Any]) -> None:
    """把云端账号快照里的 is_admin 同步到本机账号表（仅本机已存在的镜像）。

    fail-open：任何异常都不影响登录与门禁（本机标记本就是离线兜底）。
    """
    try:
        acct = acct or {}
        if "is_admin" not in acct:
            return
        email = str(acct.get("email") or "").strip().lower()
        if not email:
            return
        from auth_store import set_user_admin, user_id_of, user_is_admin
        uid = user_id_of(email)
        if not uid:
            return  # 本机没有这个账号的镜像（该端没登录过）—— 不代建
        flag = bool(acct.get("is_admin"))
        if flag != bool(user_is_admin(uid)):
            set_user_admin(uid, flag)
    except Exception:  # noqa: BLE001 — 同步失败不影响登录主链路
        pass


def _mirror_to_user_store(store, acct: dict[str, Any]) -> None:
    """把云端账号快照镜像到该账号对应的 per-user store（memberships/{uid}.json）。

    🔴 为什么需要（2026-09-30 实测定位）：会员积分存在**两份互相独立的账本** ——
      · 全局 member_store（~/.video-downloader/membership.json）：登录/心跳/同步
        写这一份，云端账号快照只落在这里；
      · per-user store（~/.video-downloader/memberships/{user_id}.json）：个人中心
        (`/api/member/status`)、门禁、超管会员表读的是这一份。
      `current_member_store` 仅在「per-user 自己没有权益」时才回退全局，而只要该
      文件里还留着历史本地权益（早期 via=ui_test 激活、超管调分、之后被消耗），
      它就**永远**被选中，后果是：
        ① 云端真值看不见（云端改 300/300，App 仍显示 800/-350 的旧账）；
        ② 该文件没有 meta.account.token → spend_credits 的上报链路静默 return，
           积分消耗根本不进云端，两份账各自漂移；
        ③ 反作弊留了口子：手改 per-user JSON 白嫖积分不会被云端权威覆盖。
      这里让 per-user store 也持有账号快照（含 token/fp）并套用云端权威值，
      三条一起解决：显示=云端真值、消耗能上报、手改一联网即回滚。

    失败静默（fail-open）：账号/权益已落全局，镜像失败不影响登录与门禁。
    """
    try:
        store._ensure_loaded()
        gacc = (store._state.get("meta") or {}).get("account") or {}
        email = str((acct or {}).get("email") or gacc.get("email") or "").strip().lower()
        from credential_store import resolve_token
        token = resolve_token(gacc)
        if not email or not token:
            return
        uid = None
        try:
            from auth_store import _load_users
            data = _load_users() or {}
            uid = (data.get("by_identifier") or {}).get(email)
            if not uid:
                for u in data.get("users", []):
                    if str(u.get("identifier") or "").strip().lower() == email:
                        uid = u.get("user_id")
                        break
        except Exception:
            uid = None
        if not uid:
            return  # 云端账号在本机没有对应本地账号（纯云端用户）：无需镜像
        from user_membership import get_user_store
        us = get_user_store(uid)
        if us is store:
            return
        us.save_account(email, token, acct, fp=str(gacc.get("fp") or ""),
                        name=str(gacc.get("name") or ""))
        # mirror=False 防递归：镜像只写 per-user store，不再回头镜像
        _apply_account_state(us, acct, mirror=False)
    except Exception:
        pass


def _mirror_evicted(store, evicted: bool) -> None:
    """把「设备位被挤出」标记同步到该账号的 per-user store。

    _mirror_to_user_store 之后，门禁与个人中心实际读的是 per-user 那份，而
    `status()` 的降级只看该文件自己的 meta.account.evicted —— 不同步就会出现
    「被挤下线了但权益照用」（save_account 每次都会把 evicted 重置为 False，
    登录/心跳成功时正好是想要的语义，被挤掉时就不是了）。失败静默。
    """
    try:
        store._ensure_loaded()
        gacc = (store._state.get("meta") or {}).get("account") or {}
        email = str(gacc.get("email") or "").strip().lower()
        if not email:
            return
        from auth_store import _load_users
        uid = (_load_users().get("by_identifier") or {}).get(email)
        if not uid:
            return
        from user_membership import get_user_store
        us = get_user_store(uid)
        if us is store:
            return
        us.set_evicted(bool(evicted))
    except Exception:
        pass


def _after_login(store, email: str, resp: dict[str, Any], fp: str, name: str) -> None:
    acct = resp.get("account") or {}
    store.save_account(email, resp.get("token", ""), acct, fp=fp, name=name)
    _apply_account_state(store, acct)
    # 🔴 2026-10-06 跨端云端额度回灌：登录后用云端余额覆盖本机 quota.json 缓存。
    # 不做这步的话，用户在另一台机器/网页用掉的额度本机看不见，面板会显示
    # 「还剩 3 次」→ 体验上等于没修。失败静默（记账路径另有 fail-open）。
    try:
        from quota import QuotaManager
        QuotaManager(token_fn=lambda: str(store._cloud_token() or "")).sync_from_cloud()
    except Exception:
        pass


# ---- 积分扣减上云（防破解：本地余额只是缓存，消耗流水以云端为准）------------
def _pending_spends_path_guard(store) -> list[dict]:
    try:
        store._ensure_loaded()
        meta = store._state.setdefault("meta", {})
        q = meta.setdefault("pending_spends", [])
        if not isinstance(q, list):
            q = meta["pending_spends"] = []
        return q
    except Exception:
        return []


def _queue_pending_spends(store, items: list[dict]) -> None:
    try:
        q = _pending_spends_path_guard(store)
        q.extend(items)
        store._state["meta"]["pending_spends"] = q[-200:]  # 上限 200 条
        store._persist()
    except Exception:
        pass


def _report_spend(store, items: list[dict]) -> None:
    """同步上报（在后台线程里跑）：成功则用云端权威快照校准本地余额。"""
    try:
        store._ensure_loaded()
        from credential_store import resolve_token
        token = resolve_token((store._state.get("meta") or {}).get("account") or {})
        if not token or not items:
            return
        import license_client
        r = license_client.spend_remote(token, items)
        if r and r.get("ok"):
            auth = r.get("authority") or {}
            if auth:
                store.apply_cloud_authoritative({"authority": auth})
        else:
            _queue_pending_spends(store, items)
    except Exception:
        _queue_pending_spends(store, items)  # 断网/超时：排队下次补报


def report_spend_async(store, amount: int, ai_taken: int = 0, perm_taken: int = 0,
                       reason: str = "") -> None:
    """membership.spend_credits 的上云钩子（fire-and-forget，不阻塞功能主流程）。

    items 按 spend_credits 的实际扣减拆分（先 AI 后永久），幂等 id 唯一 ——
    重试/补报不会在云端重复扣。
    离线测试环境（pytest / VDL_OFFLINE_TESTS）直接跳过：后台线程会污染全局
    store 单例并发起真实网络请求。
    """
    import os
    if "PYTEST_CURRENT_TEST" in os.environ or os.environ.get("VDL_OFFLINE_TESTS"):
        return
    import threading
    import uuid
    ev = uuid.uuid4().hex[:16]
    items: list[dict] = []
    if ai_taken > 0:
        items.append({"pool": "ai", "cost": int(ai_taken), "op": reason[:40],
                      "id": f"{ev}-ai"})
    if perm_taken > 0:
        items.append({"pool": "permanent", "cost": int(perm_taken), "op": reason[:40],
                      "id": f"{ev}-perm"})
    if not items:
        return
    try:
        threading.Thread(target=_report_spend, args=(store, items),
                         daemon=True, name="vdl-spend-report").start()
    except Exception:
        _queue_pending_spends(store, items)


def flush_pending_spends(store) -> None:
    """联网时补报离线期间积累的扣减（登录/心跳成功后调用）。"""
    try:
        store._ensure_loaded()
        meta = store._state.get("meta") or {}
        pending = meta.get("pending_spends") or []
        if not pending:
            return
        token = str((meta.get("account") or {}).get("token") or "")
        if not token:
            return
        import license_client
        r = license_client.spend_remote(token, pending)
        if r and r.get("ok"):
            meta["pending_spends"] = []
            store._persist()
            auth = r.get("authority") or {}
            if auth:
                store.apply_cloud_authoritative({"authority": auth})
    except Exception:
        pass  # 仍离线：保留队列下次再试


# ---- 每日用量上云（同一账号 App/网页版共用每日免费配额；本地只是预检缓存）----
def _pending_usages_path_guard(store) -> list[dict]:
    try:
        store._ensure_loaded()
        meta = store._state.setdefault("meta", {})
        q = meta.setdefault("pending_usages", [])
        if not isinstance(q, list):
            q = meta["pending_usages"] = []
        return q
    except Exception:
        return []


def _queue_pending_usages(store, items: list[dict]) -> None:
    try:
        q = _pending_usages_path_guard(store)
        q.extend(items)
        store._state["meta"]["pending_usages"] = q[-200:]  # 上限 200 条
        store._persist()
    except Exception:
        pass


def _report_usage(store, items: list[dict]) -> None:
    """同步上报（在后台线程里跑）：成功则用云端权威快照校准本地每日用量。"""
    try:
        store._ensure_loaded()
        token = str(((store._state.get("meta") or {}).get("account") or {}).get("token") or "")
        if not token or not items:
            return
        import license_client
        r = license_client.daily_remote(token, items)
        if r and r.get("ok"):
            auth = r.get("authority") or {}
            if auth:
                store.apply_cloud_authoritative({"authority": auth})
        else:
            _queue_pending_usages(store, items)
    except Exception:
        _queue_pending_usages(store, items)  # 断网/超时：排队下次补报


def report_usage_async(store, resource: str, n: int = 1, title: str = "",
                       platform: str = "", ref: str = "") -> None:
    """membership.use_daily 的上云钩子（fire-and-forget，幂等 id 防重复计）。

    离线测试环境（pytest / VDL_OFFLINE_TESTS）直接跳过：后台线程会污染全局
    store 单例并发起真实网络请求。

    title / platform（2026-10-19）：随用量一起上报「具体做了什么」，
    授权中心按天归档，后台「使用详情」据此显示可定位的明细。

    ref（2026-10-20）：本次用量的唯一标识，直接当云端幂等键 —— 下载结束后
    `report_usage_result_async` 用同一个键回写成功/失败，云端按 id 找到这条
    明细把结果补上，不会重复计数。
    """
    import os
    if "PYTEST_CURRENT_TEST" in os.environ or os.environ.get("VDL_OFFLINE_TESTS"):
        return
    try:
        n = int(n)
    except (TypeError, ValueError):
        return
    res = str(resource or "").strip()[:32]
    if n <= 0 or not res:
        return
    import threading
    import uuid
    ev = str(ref or "").strip()[:64] or uuid.uuid4().hex[:16]
    it: dict = {"res": res, "n": n, "id": f"{ev}-{res[:16]}"}
    _t = str(title or "").strip()[:120]
    if _t:
        it["t"] = _t
        it["p"] = str(platform or "").strip()[:64]
    items = [it]
    try:
        threading.Thread(target=_report_usage, args=(store, items),
                         daemon=True, name="vdl-usage-report").start()
    except Exception:
        _queue_pending_usages(store, items)


def report_usage_result_async(store, resource: str, ref: str, r: int) -> None:
    """把「这次用量到底有没有成功」回写授权中心（2026-10-20）。

    `n=0` + `r` ⇒ 云端**只更新结果、不加计数**：次数在下单那一刻已经扣过，
    这里补的是「下载完成并保存到本机」这个事实。断网入队，下次心跳补发。
    """
    import os
    if "PYTEST_CURRENT_TEST" in os.environ or os.environ.get("VDL_OFFLINE_TESTS"):
        return
    res = str(resource or "").strip()[:32]
    key = str(ref or "").strip()[:64]
    if not res or not key:
        return
    try:
        rv = 1 if int(r) > 0 else 0
    except (TypeError, ValueError):
        rv = 0
    # 幂等键必须与计数时**完全一致**（计数侧带 `-<res>` 后缀），否则云端找不到那条明细
    items = [{"res": res, "n": 0, "r": rv, "id": f"{key}-{res[:16]}"}]
    import threading
    try:
        threading.Thread(target=_report_usage, args=(store, items),
                         daemon=True, name="vdl-usage-result").start()
    except Exception:
        _queue_pending_usages(store, items)


def flush_pending_usages(store) -> None:
    """联网时补报离线期间积累的每日用量（登录/心跳成功后调用）。"""
    try:
        store._ensure_loaded()
        meta = store._state.get("meta") or {}
        pending = meta.get("pending_usages") or []
        if not pending:
            return
        token = str((meta.get("account") or {}).get("token") or "")
        if not token:
            return
        import license_client
        r = license_client.daily_remote(token, pending)
        if r and r.get("ok"):
            meta["pending_usages"] = []
            store._persist()
            auth = r.get("authority") or {}
            if auth:
                store.apply_cloud_authoritative({"authority": auth})
    except Exception:
        pass  # 仍离线：保留队列下次再试


def report_demand_async(host: str, url: str = "", category: str = "unsupported_platform",
                        ts: int = 0) -> None:
    """解析失败需求上报授权中心（与本地落库并行；fire-and-forget）。

    ts=落库时间（resolve_demand.record_demand 传入）：云端按它落记录，历史
    ts（>1h 前）在中心侧放行 60s 节流（补报语义）。成功后推进本地回填水位，
    避免启动回填把同一条重报。离线测试环境（pytest / VDL_OFFLINE_TESTS）直接
    跳过。token 从会员引擎单例取，未登录（无 token）静默跳过。
    """
    import os
    if "PYTEST_CURRENT_TEST" in os.environ or os.environ.get("VDL_OFFLINE_TESTS"):
        return
    import threading
    h = str(host or "").strip().lower()[:120]
    if not h or "/" in h or " " in h:
        return
    cat = str(category or "unsupported_platform")[:32]
    if cat not in ("pending_extractor", "unsupported_platform"):
        cat = "unsupported_platform"
    try:
        ts = int(ts or 0)
    except (TypeError, ValueError):
        ts = 0

    def _run() -> None:
        try:
            import app as _app
            store = getattr(_app, "member_store", None)
            if store is None:
                return
            token = str(((store._state.get("meta") or {}).get("account") or {}).get("token") or "")
            if not token:
                return
            import license_client
            item: dict = {"host": h, "url": str(url or "")[:200], "category": cat}
            if ts > 0:
                item["ts"] = ts
            license_client.demand_report_remote(token, [item])
            if ts > 0:
                import resolve_demand as _rd
                _rd.mark_synced(ts)
        except Exception:
            pass

    try:
        threading.Thread(target=_run, daemon=True, name="vdl-demand-report").start()
    except Exception:
        pass


def backfill_demand_to_center(max_batches: int = 20) -> dict:
    """把本机解析需求日志里「云端没见过」的历史记录批量补报授权中心（回填）。

    断点=resolve_demand 水位文件（已成功上报的最大 ts），只补水位之后的记录，
    每批成功推进一次水位 ⇒ 重复调用 / 重启重跑都幂等。历史 ts 在中心侧放行
    节流（>1h 前），不会 账号×host 60s 节流拦掉回填批量。未登录 / 离线 /
    测试环境直接跳过。由启动钩子与后台「解析需求」刷新触发（后台线程）。
    """
    import os
    if "PYTEST_CURRENT_TEST" in os.environ or os.environ.get("VDL_OFFLINE_TESTS"):
        return {"ok": False, "reason": "offline_tests"}
    try:
        import app as _app
        store = getattr(_app, "member_store", None)
        if store is None:
            return {"ok": False, "reason": "no_store"}
        token = str(((store._state.get("meta") or {}).get("account") or {}).get("token") or "")
        if not token:
            return {"ok": False, "reason": "not_logged_in"}

        import resolve_demand as _rd
        import license_client
        pending = _rd.pending_for_sync()
        if not pending:
            return {"ok": True, "sent": 0}
        sent = 0
        for i in range(0, len(pending), _rd.SYNC_BATCH):
            if sent and i >= max_batches * _rd.SYNC_BATCH:
                break  # 单次最多 max_batches 批，剩余下次（水位保证断点续传）
            batch = pending[i:i + _rd.SYNC_BATCH]
            r = license_client.demand_report_remote(
                token, [{"host": it["host"], "url": it["url"],
                         "category": it["category"], "ts": it["ts"]} for it in batch])
            if not isinstance(r, dict) or not r.get("ok"):
                return {"ok": False, "reason": "cloud_rejected", "sent": sent}
            sent += len(batch)
            _rd.mark_synced(batch[-1]["ts"])
        return {"ok": True, "sent": sent}
    except Exception as e:  # noqa: BLE001 - 回填是旁路，绝不影响主流程
        return {"ok": False, "reason": type(e).__name__}


def _backfill_demand_in_background() -> None:
    """后台线程跑一次回填（启动钩子 / 管理面板刷新共用；fire-and-forget）。"""
    import threading

    def _run() -> None:
        try:
            backfill_demand_to_center()
        except Exception:
            pass

    try:
        threading.Thread(target=_run, daemon=True,
                         name="vdl-demand-backfill").start()
    except Exception:
        pass


def report_feature_usage_cloud(resource: str, title: str = "", platform: str = "") -> None:
    """纯积分 / 免配额功能成功后：把用量计到授权中心（不参与配额闸门）。

    这些功能（本地一键抠图、图片·视频去水印、超分等）原先完全不上云 ⇒ 后台「使用详情」
    只见下载。这里补上报，让「功能侧重调整」有全量数据底座。token 从会员引擎单例取，
    调用方无需传 store；离线测试环境跳过。
    """
    import os
    if "PYTEST_CURRENT_TEST" in os.environ or os.environ.get("VDL_OFFLINE_TESTS"):
        return
    try:
        import app as _app
        store = getattr(_app, "member_store", None)
        if store is None:
            return
        report_usage_async(store, str(resource or "").strip()[:32], n=1,
                           title=title, platform=platform)
    except Exception:
        pass


# 注册需求上报钩子：任何 record_demand 真的落库时，都会异步推一份到授权中心，
# 这样 Mac/Win 的解析需求在任一端后台都能看到（否则各看各的本机日志）。
import resolve_demand as _RD  # noqa: E402
try:
    _RD.set_demand_reporter(report_demand_async)
except Exception:  # noqa: BLE001
    pass


def _pub(store, extra: Optional[dict] = None) -> dict[str, Any]:
    out = {"ok": True, "account": store.account_view()}
    if extra:
        out.update(extra)
    return out


@router.post("/api/cloud/register")
def cloud_register(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    import license_client
    email = str(payload.get("email") or "").strip()
    password = str(payload.get("password") or "")
    if not email or not password:
        return {"ok": False, "error": "请输入邮箱和密码"}
    fp, name = _fp_name()
    if not fp:
        return {"ok": False, "error": "无法取得本机设备标识，请重启 App 后重试"}
    try:
        r = license_client.register_remote(email, password, fp, name)
    except license_client.LicenseCloudError as e:
        return {"ok": False, "error": f"{e}（需要联网）", "code": "CLOUD_UNREACHABLE"}
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "注册失败",
                "code": r.get("code") or "REJECTED"}
    store = _store()
    _after_login(store, email, r, fp, name)
    _sync_userdata(store, password)        # 用户信息类数据：与云端对齐（后台）
    try:
        local_token = _local_token_for(email, password)
    except Exception:
        local_token = ""
    return _pub(store, {"registered": True, "local_token": local_token})


@router.post("/api/cloud/login")
def cloud_login(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    import license_client
    email = str(payload.get("email") or "").strip()
    password = str(payload.get("password") or "")
    if not email or not password:
        return {"ok": False, "error": "请输入邮箱和密码"}
    fp, name = _fp_name()
    if not fp:
        return {"ok": False, "error": "无法取得本机设备标识，请重启 App 后重试"}
    try:
        r = license_client.login_remote(email, password, fp, name)
    except license_client.LicenseCloudError as e:
        return {"ok": False, "error": f"{e}（登录需要联网）", "code": "CLOUD_UNREACHABLE"}
    if not r.get("ok"):
        # 老账号自愈：云端不认识这个账号，但本机账号表里有且密码正确 → 说明这是
        # 当年用手机号注册、云端从没建号的用户，就地补建后重试一次登录。
        healed = False
        if r.get("code") == "NO_ACCOUNT":
            healed = _adopt_local_account(email, password, fp, name)
        elif r.get("code") == "BAD_PASSWORD":
            # 两端密码分叉（本机改过、云端没跟上）→ 把本机密码推上云端再试一次。
            # 这样用户永远不会遇到「本机密码对、云端说密码错」的死结。共用一个重试。
            healed = _heal_cloud_password(email, password)
            if not healed:
                from auth_store import authenticate
                if authenticate(email, password):
                    # 本机密码是对的 → 分叉且本机没有云端会话凭证（登出过/换机），
                    # 设计上无法自愈（推密码需要 token 或旧云端密码，两者都没有）。
                    # 如实说明，别让用户对着「密码不正确」怀疑自己输错。
                    return {"ok": False, "code": "PASSWORD_DIVERGED",
                            "error": "本机密码正确，但云端记录的密码与本机不一致，"
                                     "且本机没有云端会话凭证、无法自动对齐。"
                                     "请联系管理员把云端密码重置为本机密码后重新登录。"}
        if healed:
            try:
                r = license_client.login_remote(email, password, fp, name)
            except license_client.LicenseCloudError as e:
                return {"ok": False, "error": f"{e}（登录需要联网）", "code": "CLOUD_UNREACHABLE"}
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "登录失败",
                "code": r.get("code") or "REJECTED"}
    store = _store()
    _after_login(store, email, r, fp, name)
    _sync_userdata(store, password)        # 用户信息类数据：与云端对齐（后台）
    extra = {}
    try:
        extra["local_token"] = _local_token_for(email, password)
    except Exception:
        extra["local_token"] = ""
    if r.get("evicted"):
        extra["notice"] = "已挤下线一台闲置设备（该账号最多同时登录 " \
                          f"{store.account_view().get('max_devices', 2)} 台）"
    return _pub(store, extra)


@router.post("/api/cloud/logout")
def cloud_logout(request: Request) -> dict[str, Any]:
    """本地登出。权益不做清除（换账号/换机都不丢已买的会员）。

    安全审计 2026-10-11（P1-8）：必须是本机云账号的**归属者**才能登出，
    否则任何人都能一句话把别人踢下线。
    """
    store = _require_cloud_owner(request)
    store.clear_account()
    return _pub(store)


@router.get("/api/cloud/status")
def cloud_status(request: Request) -> dict[str, Any]:
    """离线可读：账号快照 + 设备列表 + 是否被挤出。

    安全审计 2026-10-11（P1-8）：绑定当前经验证账号；非归属者（含未登录）
    只回空快照，不泄漏本机云账号的邮箱与设备列表。
    """
    if not _is_cloud_owner(request):
        return {"ok": True, "account": dict(_EMPTY_ACCOUNT_VIEW)}
    return _pub(_store())


@router.post("/api/cloud/sync")
def cloud_sync(request: Request) -> dict[str, Any]:
    """主动同步：续期 + 拉新权益 + 检查设备名额。断网时不动本地（fail-open）。

    安全审计 2026-10-11（P1-8）：心跳会用**本机云账号的 token** 去授权中心
    续期并刷新设备列表，必须限归属者（否则等于替他人操作账号）。
    """
    import license_client
    store = _require_cloud_owner(request)
    acc = (store._state.get("meta") or {}).get("account") or {}
    token = str(acc.get("token") or "")
    if not token:
        return {"ok": False, "error": "尚未登录", "code": "NOT_LOGGED_IN"}
    fp, name = _fp_name()
    if not fp:
        return {"ok": False, "error": "无法取得本机设备标识"}
    try:
        r = license_client.heartbeat_remote(token, fp, meta=_device_meta())
    except license_client.LicenseCloudError as e:
        return {"ok": True, "offline": True, "warning": str(e)}
    if not r.get("ok"):
        if r.get("code") == "DEVICE_EVICTED":
            store.set_evicted(True)
            _mirror_evicted(store, True)
            return _pub(store, {"evicted": True,
                                "error": "该账号已在其他两台设备登录，请重新登录以使用本机"})
        return {"ok": False, "error": r.get("error") or "同步失败",
                "code": r.get("code") or "REJECTED"}
    acct = r.get("account") or {}
    store.set_evicted(False)
    store.save_account(acc.get("email", ""), token, acct, fp=fp, name=name)
    _apply_account_state(store, acct)
    flush_pending_spends(store)
    flush_pending_usages(store)
    return _pub(store)


@router.post("/api/cloud/redeem")
def cloud_redeem(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """卡密通道已下线（2026-09-26，充值统一走支付宝/微信在线支付）。

    保留路由是为了老客户端拿到明确提示而不是 404；授权中心侧同步用
    VDL_REDEEM_DISABLED 关闭了核销，卡密不再构成任何入账/发货通路。
    """
    return {"ok": False, "error": "卡密充值已下线，请在会员中心使用支付宝/微信扫码充值",
            "code": "REDEEM_OFFLINE"}


@router.post("/api/cloud/unbind")
def cloud_unbind(payload: dict[str, Any] = Body(...), request: Request = None) -> dict[str, Any]:
    """把某台设备踢下线（腾出名额给新机器）。

    安全审计 2026-10-11（P1-8）：必须限本机云账号的归属者，否则任何人
    都能踢掉他人设备（骚扰 / 抢占设备位）。
    """
    import license_client
    fp = str(payload.get("fp") or "").strip()
    if not fp:
        return {"ok": False, "error": "缺少设备标识"}
    store = _require_cloud_owner(request)
    acc = (store._state.get("meta") or {}).get("account") or {}
    token = str(acc.get("token") or "")
    if not token:
        return {"ok": False, "error": "请先登录账号", "code": "NOT_LOGGED_IN"}
    try:
        r = license_client.unbind_remote(token, fp)
    except license_client.LicenseCloudError as e:
        return {"ok": False, "error": f"{e}（需要联网）", "code": "CLOUD_UNREACHABLE"}
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "操作失败",
                "code": r.get("code") or "REJECTED"}
    acct = r.get("account") or {}
    store.save_account(acc.get("email", ""), token, acct,
                       fp=acc.get("fp", ""), name=acc.get("name", ""))
    return _pub(store)


# ⚠️ /api/cloud/pay/* 已迁移至 routers/payment.py（通道无关 + 本地订单 + 付款即发权益）。
# 旧实现直连已下线的 pay.hanyuxz.top，故此处移除，避免重复路由。


def maybe_sync_account(store) -> None:
    """member_status 的惰性钩子：登录过的前提下，每隔 _SYNC_MIN_INTERVAL 同步一次。

    - 未登录：直接返回（老用户不受影响）
    - 网络异常：不改动本地权益（fail-open）
    """
    import time

    try:
        store._ensure_loaded()
        acc = (store._state.get("meta") or {}).get("account") or {}
        token = str(acc.get("token") or "")
        if not token:
            return
        if time.time() - float(acc.get("last_sync") or 0) < _SYNC_MIN_INTERVAL:
            return
        import license_client
        fp, name = _fp_name()
        if not fp:
            return
        r = license_client.heartbeat_remote(token, fp, meta=_device_meta())
        if r.get("ok"):
            acct = r.get("account") or {}
            store.set_evicted(False)
            store.save_account(acc.get("email", ""), token, acct, fp=fp, name=name)
            _apply_account_state(store, acct)
            flush_pending_spends(store)
            flush_pending_usages(store)
        elif r.get("code") == "DEVICE_EVICTED":
            store.set_evicted(True)
            _mirror_evicted(store, True)
        elif r.get("code") == "ACCOUNT_BANNED":
            # 云端封禁：落 meta.account.banned → device_lock_reason 全局锁权益
            try:
                store._ensure_loaded()
                store._state.setdefault("meta", {}).setdefault("account", {})["banned"] = True
                store._persist()
            except Exception:
                pass
    except Exception:
        pass  # 网络/指纹异常：保持现状（宽限语义）
