"""server/routers/cloud_drive.py — 网盘文件源路由（阶段 0：百度网盘）。

提供：
- POST /api/cloud-drive/resolve    解析网盘链接 → 返回可下载文件列表（直链 + 所需 cookie）
- POST /api/cloud-drive/credential 保存百度网盘登录态（完整 Cookie 头 或 裸 BDUSS）
- GET  /api/cloud-drive/credential/status  查询是否已保存

设计：本路由只做「解析 + 凭据管理」，真正的下载仍走现有 POST /api/download
（把返回的 dlink 作为 url、cookie 作为 cookie 字段即可，下载器会带登录态且不加 Referer）。
不侵入视频解析主链路（core.py），风险面最小。

登录态契约（百度特有）：解析四步匿名可过，但 **dlink 下载必须带登录态**。
所以存在第三态：解析成功但没登录态 ⇒ ok=True + need_credential=True，
前端要提示「已读到文件，但下载需要百度登录态」。
"""
from __future__ import annotations

import logging
import re
import time

import app
from fastapi import APIRouter
from pydantic import BaseModel

from cloud_drives import baidu
from credential_store import get_baidu_bduss, set_baidu_bduss

# 会员优先排序 + BDUSS 提取（Change 1，2026-10-29 根因修复）：必须在模块级可见，
# _baidu_login_cookies 与 _baidu_local_cookies 都用到，函数内局部 import 不会外泄到调用方作用域。
from cookie_pool import _bduss_of, member_bduss_set  # noqa: E402

logger = logging.getLogger(__name__)

router = APIRouter()

# Python 内部错误的特征串 —— 这类文本绝不能原样甩给用户（2026-10-27 线上出现过
# 「百度网盘解析失败: 'str' object has no attribute 'get'」这种英文堆栈碎片）。
_PY_ERR_RE = re.compile(
    r"object has no attribute|not subscriptable|NoneType|positional argument|"
    r"unexpected keyword|Traceback|KeyError|IndexError|TypeError|AttributeError"
)


def _human_err(msg: object) -> str:
    """把后端异常洗成一句用户能看懂的中文。"""
    t = str(msg or "").strip()
    if not t:
        return "百度网盘解析失败，请稍后重试"
    if _PY_ERR_RE.search(t) or not re.search(r"[\u4e00-\u9fff]", t):
        return "百度网盘接口返回异常（可能触发了风控，或分享链接已失效），请稍后重试"
    return t[:120]

_BAIDU_HOSTS = ("pan.baidu.com", "baidu.com")


class CloudDriveResolveReq(BaseModel):
    url: str
    pwd: str = ""


class CloudDriveCredentialReq(BaseModel):
    bduss: str


def _auto_contrib(header: str, source: str = "auto") -> None:
    """从浏览器/缓存自动取到的百度登录态 ⇒ 自动加入共享池（2026-10-25 用户定档）。

    用户要求：浏览器里本来就有百度登录态的，用户不用手填，我们自动取到后
    自动进共享池 —— 这样网页版与别的用户也能用上同一份账号。

    与「用户手动点贡献」的区别只在于触发方式（自动 vs 显式），入池与推云端
    走的是同一条路。本地池 + 云端池各做一次；任何一步失败都不抛 ——
    共享池挂了不能影响本次下载。

    ⚠️ 通用同步通道（/api/cookie/sync/auto、/api/cookie/sync/from-local）仍然
    排除百度 —— 那两条是「隐式读取浏览器」的通用通道，不该替用户决定共享账号；
    这里只走网盘这条专用路径，语义明确、可控。
    """
    header = (header or "").strip()
    if not header:
        return
    # 🔴 判断「是否已含 BDUSS」而不是「是否以 BDUSS= 开头」：百度 Cookie 是一整串
    # （BAIDUID/BIDUPSID/BDUSS/STOKEN/... 并列），缓存里的 header 往往以 BAIDUID= 开头。
    # 用 startswith 判断会以为「没有 BDUSS」而补一个前缀 ⇒ 变成
    # `BDUSS=BAIDUID=...`（长度多 6 字节），这种畸形头验真必被拒 ⇒ 自动入池永远失败。
    # 只有「整段只有裸 BDUSS 值」时才补前缀。
    if "bduss=" not in header.lower():
        header = f"BDUSS={header}"
    try:
        from cookie_pool import add_cookie
        add_cookie("baidu.com", header, source=source)
    except Exception:  # noqa: BLE001
        pass
    try:
        import app as _app
        url, token = _app._cloud_sync_config()
        if url and token:
            _app._push_cookie_to_cloud("baidu.com", header, url, token)
    except Exception:  # noqa: BLE001
        pass


def _baidu_login_cookie() -> str:
    """取百度登录态：手动保存的 BDUSS → 本机 cookie 缓存 → 实时解密浏览器 → 共享池。

    顺序说明（2026-10-24 改：**手动优先**）：
    用户在网盘页「保存登录态」里填的 BDUSS 是**显式指定账号**，必须盖过自动抓的
    缓存 —— 否则他想用会员账号下载时，会被本机 Chrome 缓存里的非会员账号盖掉，
    界面表现就是「填了会员 BDUSS，速度没变」（百度限速按账号等级，非会员 ~80KB/s）。
    没填才用缓存（扩展推送/浏览器同步，命中快）；
    最后才实时解密浏览器 —— Chrome 156 起 cookie 走应用绑定加密（ABE），
    这条路大概率失败且慢，只在前面全空时试一次。
    2026-10-25 追加**共享池兜底**：别人贡献的百度登录态（用户显式点过「贡献到
    共享池」的那批）。自己的登录态永远优先于别人的，所以它排最后。
    """
    stored = get_baidu_bduss() or ""
    if stored:
        return stored

    try:
        import cookie_cache
        for host in _BAIDU_HOSTS:
            got = cookie_cache.peek_cached_cookie_header(host)
            if got:
                _auto_contrib(got, "cache")
                return got
    except Exception:  # noqa: BLE001
        pass

    try:
        import cookie_cache
        got = cookie_cache.get_cached_cookie_header("pan.baidu.com")
        if got:
            _auto_contrib(got, "browser")
            return got
    except Exception:  # noqa: BLE001
        pass

    # 共享池兜底（2026-10-25 用户定档：加入共享池，给其他用户一起用）
    # ⚠️ 必须放在上面所有「本机登录态」之后：自己的账号永远优先于别人的。
    # ⚠️ 也不能写在这之前任何 `return` 的后面 —— 那是死代码，跑不到（实测踩过：
    #    第一版把兜底写在 get_cached_cookie_header 的 return 之后，池子里明明有
    #    条目却取不到，页面表现为「共享池没生效」）。
    try:
        from cookie_pool import get_cookie
        return get_cookie("pan.baidu.com") or ""
    except Exception:  # noqa: BLE001
        return ""


_CLOUD_BD_TTL_OK = 300      # 云端命中缓存 5 分钟（池约 20 分钟轮换，够新鲜）
_CLOUD_BD_TTL_MISS = 60     # 未命中/失败负缓存 1 分钟，避免每次解析都吃满超时
_CLOUD_BD_TIMEOUT = 6       # 国内 ECS 单程 6s 足够，再慢当作不可达
_cloud_bd_mem: dict = {"ts": 0.0, "headers": []}


def _baidu_cloud_cookies() -> list:
    """从云端共享池取百度登录态候选（字段多的在前）。

    背景（2026-10-28）：百度是 contrib_only 域 —— 只有用户显式点过「贡献到共享池」
    的账号才会进云端。上一版云端 `pool-admin get` 对百度一律回
    `reason=contrib_only`，于是「贡献了、云端也存着，但谁也取不回来」，共享池对
    百度彻底空转。云端已放行读取（同一把同步令牌），这里补桌面端的回读分支。

    约定：
      · 失败一律返回 []，绝不影响本机解析主流程；
      · 6s 超时 + 命中 5 分钟 / 未命中 1 分钟缓存，别让解析预算被拖垮；
      · 显式禁用环境代理 —— 云端是国内 IP，桌面进程常继承 clash，走代理必 407。
    """
    now = time.time()
    age = now - float(_cloud_bd_mem.get("ts") or 0)
    if age < (_CLOUD_BD_TTL_OK if _cloud_bd_mem.get("headers") else _CLOUD_BD_TTL_MISS):
        return list(_cloud_bd_mem["headers"] or []) if _cloud_bd_mem.get("headers") else []

    headers: list = []
    try:
        from downloader import _cloud_pool_cfg
        url, token = _cloud_pool_cfg()
        if url and token:
            import json as _json
            import urllib.request as _url
            body = _json.dumps({"action": "get", "domain": "pan.baidu.com",
                                "token": token}).encode("utf-8")
            req = _url.Request(url.rstrip("/") + "/api/cookie/pool-admin", data=body,
                               headers={"Content-Type": "application/json",
                                        "User-Agent": "VDL-Desktop"},
                               method="POST")
            opener = _url.build_opener(_url.ProxyHandler({}))   # 显式不走代理
            with opener.open(req, timeout=_CLOUD_BD_TIMEOUT) as resp:
                d = _json.loads(resp.read().decode("utf-8") or "{}")
            if d.get("ok") and d.get("found"):
                headers = [h for h in (d.get("headers") or []) if h]
                if not headers and d.get("header"):
                    headers = [d["header"]]
    except Exception as e:  # noqa: BLE001 — 网络/旧版云端/令牌失效都只当没有
        logger.info("[cloud_drive] 云端百度登录态回读失败：%s", str(e)[:160])
        headers = []
    if headers:
        _cloud_bd_mem["ts"], _cloud_bd_mem["headers"] = now, headers
    else:
        # 未命中也要写 ts，否则每次解析都重试一次 6s 超时
        _cloud_bd_mem["ts"] = now - _CLOUD_BD_TTL_OK + _CLOUD_BD_TTL_MISS
        _cloud_bd_mem["headers"] = []
    logger.info("[cloud_drive] 云端百度登录态 n=%s", len(headers))
    return headers


def _baidu_key(cookie: str) -> str:
    """候选去重键：BDUSS|STOKEN（同账号不同 STOKEN 是不同会话，见下）。"""
    kv: dict = {}
    for part in (cookie or "").split(";"):
        k, _, v = part.partition("=")
        kv.setdefault(k.strip(), v.strip())
    bduss = kv.get("BDUSS") or ""
    if not bduss:
        return ""
    return bduss + "|" + (kv.get("STOKEN") or "")


def _baidu_rank_key(cookie: str) -> int:
    """排序键：字段越多越靠前（换直链要完整的 pan cookie）。"""
    return -sum(1 for part in (cookie or "").split(";") if "=" in part)


def _dedupe_baidu(cands) -> list:
    picked: list = []
    seen: set = set()
    for c in cands or []:
        key = _baidu_key(c or "")
        if not key or key in seen:
            continue
        seen.add(key)
        picked.append(c)
    return picked


# 多 profile 浏览器抓取冷却（2026-10-29）：枚举本机全部浏览器 profile（Chrome/Edge/UC/QQ/
# 360 …）逐个解密入库，很慢，不能每次解析都跑。进程内冷却 10 分钟，既保证「首次解析就把
# 用户多个浏览器的百度账号全抓进池」，又避免高频重复解密 + 重复验真打百度接口。
# 解析主流程只在冷却到期时才触发，平时直接读已入池的候选。
_HARVEST_TS: float = 0.0
_HARVEST_INTERVAL: float = 600.0  # 秒


def _ensure_browser_baidu_harvest() -> None:
    """进程内冷却后，全量抓取本机浏览器百度登录态入池（幂等、可失败）。"""
    global _HARVEST_TS
    now = time.time()
    if now - _HARVEST_TS < _HARVEST_INTERVAL:
        return
    _HARVEST_TS = now
    try:
        import downloader
        downloader.harvest_baidu_browser_cookies()
    except Exception:  # noqa: BLE001
        pass


def _baidu_local_cookies() -> list:
    """**本机**百度登录态候选（字段多的在前）：手动保存 → 本机缓存 → 浏览器 → 本机池。

    为什么需要「多条」：同一个 BDUSS 可以有多个 STOKEN，而换直链的结果可能一个
    明文一个密文（2026-10-28 实测）。只取一条等于赌运气，所以把候选全交出去，
    由 baidu.resolve(..., extra_cookies=cands) 逐个试。
    """
    out: list = []
    stored = get_baidu_bduss() or ""
    if stored:
        out.append(stored if "=" in stored else f"BDUSS={stored}")

    try:
        import cookie_cache
        for host in _BAIDU_HOSTS:
            got = cookie_cache.peek_cached_cookie_header(host)
            if got:
                _auto_contrib(got, "cache")
                out.append(got)
                break
    except Exception:  # noqa: BLE001
        pass

    try:
        import cookie_cache
        got = cookie_cache.get_cached_cookie_header("pan.baidu.com")
        if got:
            _auto_contrib(got, "browser")
            out.append(got)
    except Exception:  # noqa: BLE001
        pass

    # 本机共享池：全部候选（池里可能混着普通号，交给解析器逐个试）
    # 🔴 先全量抓一遍浏览器（冷却 10 分钟一次）：把每个浏览器 profile 各自的百度登录态
    # 都入池，否则只靠 find_source 只会进一个账号，会员优先排序也无从挑起。
    try:
        _ensure_browser_baidu_harvest()
    except Exception:  # noqa: BLE001
        pass
    try:
        from cookie_pool import get_cookies
        out.extend(get_cookies("pan.baidu.com") or [])
    except Exception:  # noqa: BLE001 — 池读失败不该挡住已拿到的本机登录态
        logger.warning("[cloud_drive] read cookie pool failed", exc_info=True)

    picked = _dedupe_baidu(out)
    # 字段更全的快照优先：换直链要的是**完整的 pan cookie**，实测残缺快照
    # （32 字段）回密文、完整快照（38 字段）回明文。按字段数降序能一般命中最优
    # 那条，省掉一次 4~9 秒的失败往返（也少一次撞风控的机会）。稳定排序，
    # 字段数相同的仍保持「手动保存 → 本机缓存 → 本机池」的原有先后。
    picked.sort(key=_baidu_rank_key)
    return picked


# 云端共享池账号的会员等级缓存：本机池的 member 标记只覆盖「入过本机池」的账号，
# 云端号永远认不出会员（见 _cloud_member_bduss）。探测本身是一次网络请求/号，
# 不能每次解析都发，进程内缓存 15 分钟足够（会员等级不会分钟级变化）。
_CLOUD_VIP_MEM: dict = {"ts": 0.0, "bduss": set()}
_CLOUD_VIP_TTL: float = 900.0


def _cloud_member_bduss(cloud: list) -> set:
    """云端共享池候选里「实测是会员」的 BDUSS 集合（进程内 15 分钟缓存）。

    🔴 2026-10-29 用户实测根因：本机池的 member 标记只在入池时用 baidu_probe 打，
    云端共享池的账号从没进过本机池，所以 `member_bduss_set` 里永远没有它们 ——
    「会员优先」对云端号完全失效。表现就是：本机有一条免费号（或已失效的号）时，
    云端那枚超会号被当普通号排到最后，个人网盘通道第一个（免费）就成功返回，
    下载全程走免费带宽。

    云端池通常只有个位数账号，这里现探一次补上会员标记，结果缓存复用。
    """
    if not cloud:
        return set()
    now = time.time()
    if now - float(_CLOUD_VIP_MEM.get("ts") or 0) < _CLOUD_VIP_TTL:
        return set(_CLOUD_VIP_MEM.get("bduss") or ())

    found: set = set()
    try:
        from cookie_pool import baidu_probe
    except Exception:  # noqa: BLE001 — 池模块不可用就当没有会员号
        return set()
    for c in cloud:
        try:
            ok, info = baidu_probe(c)
            if ok and int((info or {}).get("vip_type") or 0) > 0:
                bd = _bduss_of(c)
                if bd:
                    found.add(bd)
        except Exception:  # noqa: BLE001 — 单个号探测失败不影响其他号
            continue
    _CLOUD_VIP_MEM["ts"] = now
    _CLOUD_VIP_MEM["bduss"] = found
    return found


def _baidu_login_cookies() -> list:
    """解析用候选（首选在前）：本机 + 云端共享池，**统一按会员优先排序**。

    `_baidu_login_cookie()` 仍返回第一条，供单条场合（如「是否已配置登录态」）。

    排序契约（两条都要守住）：
      1. 🔴🔴 **会员优先是全局主键，跨本机/云端生效**（2026-10-29 二次根因修复）：
         上一版把「本机整体在前」当成硬约束，会员只做了**组内**排序 —— 本机只要有一条
         免费号（哪怕它是已失效的）就压在云端超会号前面，会员带宽永远轮不到。
      2. **非会员档内本机仍排在云端之前**：全是免费号时，用户自己的账号不该被别人贡献
         的号抢前面。注意必须把「来源」写进排序键，不能只靠稳定排序 —— 字段数是变量，
         sort 会把字段更多的云端免费号提到本机免费号前面（test_router_login_cookies_
         appends_cloud_after_local 就是钉这个的）。

    最终键：`(是否会员, 来源[本机=0/云端=1], -字段数)`。
    云端号的会员等级靠 _cloud_member_bduss 现探 + 缓存补上（本机池里没有它们的标记）。
    """
    member_bduss = set(member_bduss_set("baidu.com"))

    local = _baidu_local_cookies()
    cloud = _dedupe_baidu(_baidu_cloud_cookies())
    seen = {_baidu_key(c) for c in local}
    cloud = [c for c in cloud if _baidu_key(c) not in seen]

    member_bduss |= _cloud_member_bduss(cloud)

    tagged = [(c, 0) for c in local] + [(c, 1) for c in cloud]
    tagged.sort(key=lambda it: (0 if _bduss_of(it[0]) in member_bduss else 1,
                                it[1],
                                _baidu_rank_key(it[0])))
    return [c for c, _ in tagged]


@router.post('/api/cloud-drive/contribute')
def cloud_drive_contribute() -> dict:
    """把已保存的百度登录态贡献到共享池（用户在网盘页显式点击，弹窗已说明后果）。

    与 /api/cookie/contribute 的分工：那条是「访客粘当前页 Cookie」的通用通道，
    对百度是**禁用**的（is_contrib_only）；百度只走这里 —— 用的是用户手动保存的
    BDUSS，语义上就是「我愿意把这个账号分享给别人用」。

    流程：读已存 BDUSS → 真问百度验真（顺带取会员等级）→ 入本机池 → 有云端配置
    就推一份上云。验真不过（errno≠0，BDUSS 过期/被踢）直接拒，不把废Cookie共享出去。

    🔴 2026-10-28：推上云的必须是**字段最全**的那份快照，不能只推手动保存的裸
    BDUSS。实测同一账号：32 字段那份换直链只回 4 KB 密文、38 字段那份回明文
    —— 推残缺份上去，共享池对别人就是废的（提交时也看不到任何异常）。
    所以这里从本机全部候选里挑字段最多的一条推。
    """
    from cookie_pool import add_cookie, baidu_probe, get_cookie as _pool_get

    stored = get_baidu_bduss() or ""
    if not stored:
        # 真实原因（用户还没保存 BDUSS）只进日志；上屏统一 baidu.USER_ERROR_TEXT。
        logger.warning("[cloud_drive] contribute 被拒：本机未保存百度登录态")
        return {"ok": False, "error": baidu.USER_ERROR_TEXT}
    # 手动保存的 BDUSS 只作为「明确同意」的凭据；实际上云用字段最全的快照。
    manual = stored if "=" in stored else f"BDUSS={stored}"
    header = manual
    try:
        cands = _baidu_local_cookies()
        if cands:
            header = cands[0]      # 已按字段数降序
    except Exception:  # noqa: BLE001 — 取候选失败就退回手动保存那条
        logger.warning("[cloud_drive] contribute 取最全快照失败，退用手动保存", exc_info=True)

    ok, info = baidu_probe(header)
    if ok is False:
        logger.warning("[cloud_drive] contribute 被拒：登录态验真未通过（info=%s）",
                       str(info)[:200])
        return {"ok": False, "error": baidu.USER_ERROR_TEXT}
    vip_type = int((info or {}).get("vip_type") or 0)

    added = add_cookie("baidu.com", header, source="contrib")
    # 本机已有同内容时不重复记（add_cookie 返回 False 表示内容已存在，仍算成功）
    # reason 上屏用统一文案，真实原因（未配置云端同步 / 推送异常）见下面 logger。
    cloud: dict = {"pushed": False, "reason": baidu.USER_ERROR_TEXT}
    try:
        import app as _app
        url, token = _app._cloud_sync_config()
        if url and token:
            resp = _app._push_cookie_to_cloud("baidu.com", header, url, token)
            cloud = {"pushed": bool((resp or {}).get("ok")),
                     "verified": (resp or {}).get("verified")}
        else:
            logger.info("[cloud_drive] contribute 未推云端：未配置云端同步")
    except Exception as e:  # 网络/限流：本机池已生效，不影响主流程
        cloud = {"pushed": False, "reason": baidu.USER_ERROR_TEXT}
        logger.warning("[cloud_drive] contribute 推云端失败：%s", str(e)[:200])

    # 回读确认池里真能取到（别对用户谎报成功）
    reachable = bool(_pool_get("pan.baidu.com"))
    logger.info("[cloud_drive] contribute baidu added=%s vip_type=%s cloud=%s reachable=%s",
                added, vip_type, cloud, reachable)
    return {
        "ok": True,
        "added": bool(added) or reachable,
        "verified": ok is True,
        "vip_type": vip_type,
        "cloud": cloud,
        "reachable": reachable,
    }


@router.post('/api/cloud-drive/resolve')
def cloud_drive_resolve(req: CloudDriveResolveReq, request: app.Request = None) -> dict:
    cands = _baidu_login_cookies()
    cookie = cands[0] if cands else ""
    try:
        files = baidu.resolve(req.url, cookie, req.pwd or "", cands)
    except ValueError as e:
        # 🔴 2026-10-27 乐懂定档：前端统一只显示 baidu.USER_ERROR_TEXT。
        # 真实原因（errno / 账号等级 / 分享状态）一律进日志，交运维监控排查。
        logger.warning("[cloud_drive] resolve 失败：%s", str(e)[:300])
        return {"ok": False, "error": baidu.USER_ERROR_TEXT, "need_credential": (not cookie)}
    except Exception as e:  # noqa: BLE001
        # ⚠️ 别写成 f"百度网盘解析失败：{_human_err(e)}" —— _human_err 的兜底句
        #    正是这句统一文案，拼起来会变成「…失败：…返回异常…」（2026-10-27 踩过）。
        logger.warning("[cloud_drive] resolve failed: %s", str(e)[:200], exc_info=True)
        return {"ok": False, "error": baidu.USER_ERROR_TEXT, "need_credential": (not cookie)}
    # 第三态：解析成功但缺下载登录态（百度 dlink 匿名下载会 403）
    need_login = any(getattr(f, "need_login", False) for f in files)
    # 🔴 账号级每日配额（2026-10-28，对齐网页版同键同档）：资源键 cloud_drive，
    # 免费 3 次/日、会员 200 次/日（membership.py 两张配额表）。计数点 = 解析成功
    # （拿到 dlink）即一次 —— 桌面端真下载走通用 /api/download（无百度专用回写点），
    # 与网页版「下载流起始计一次」口径对齐取最接近的实现点。need_login（没有可用
    # 直链，用户下不了）不计次，等补了登录态重新解析才计。
    if not need_login:
        try:
            _q = app.current_member_store(request).use_daily(
                "cloud_drive", 1, title="百度网盘", platform="baidupan")
        except Exception:  # noqa: BLE001 — fail-open：配额引擎异常不打断本机使用
            _q = {"ok": True}
        if not _q.get("ok"):
            raise app.HTTPException(
                status_code=402,
                detail="MEMBER_QUOTA|" + (_q.get("error") or "今日网盘下载次数已用完，开通VIP会员可解锁 200 次/日"))
    return {
        "ok": True,
        "platform": "baidupan",
        "files": [f.to_dict() for f in files],
        # 文件夹分享是一次性展开的（上限见 baidu._SHARE_MAX_FILES），到顶要说明
        "truncated": len(files) >= getattr(baidu, "_SHARE_MAX_FILES", 300),
        "need_credential": need_login,
        "need_login": need_login,
    }


@router.post('/api/cloud-drive/credential')
def cloud_drive_set_credential(req: CloudDriveCredentialReq) -> dict:
    bduss = (req.bduss or "").strip()
    if not bduss:
        logger.warning("[cloud_drive] credential 保存被拒：BDUSS 为空")
        return {"ok": False, "error": baidu.USER_ERROR_TEXT}
    loc = set_baidu_bduss(bduss)   # 内部兼容完整 Cookie 头 / 裸 BDUSS
    return {"ok": True, "stored_in": loc}


@router.get('/api/cloud-drive/credential/status')
def cloud_drive_credential_status() -> dict:
    return {"set": bool(get_baidu_bduss()) or bool(_baidu_login_cookie())}


@router.post('/api/cloud-drive/harvest-browser-cookies')
def cloud_drive_harvest_browser_cookies() -> dict:
    """立刻全量重扫本机浏览器百度登录态（每个浏览器 profile 各自入池）。

    与 `_baidu_local_cookies` 里冷却 10 分钟的「静默自动抓取」不同，这是用户/前端
    主动触发的「现在就扫一遍」，绕过冷却，用于「刚登录了新账号、想立刻用上」的场景。
    返回本次抓到的账号数（去重后）。任何浏览器读不出都跳过，绝不影响其他来源。
    """
    n = 0
    try:
        import downloader
        from cookie_pool import _bduss_of as _bduss_of_header
        headers = downloader.harvest_baidu_browser_cookies()
        # 去重到登录会话（BDUSS），返回真实账号数而非 header 条数
        seen: set = set()
        for h in headers or []:
            b = _bduss_of_header(h)
            if b and b not in seen:
                seen.add(b)
                n += 1
    except Exception as e:  # noqa: BLE001
        logger.warning("[cloud_drive] harvest browser cookies 失败：%s", str(e)[:200])
    logger.info("[cloud_drive] harvest browser cookies 触发，账号数=%s", n)
    return {"ok": True, "accounts": n}
