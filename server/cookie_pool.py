"""公共 Cookie 池（与『仅本机个人缓存』严格隔离）。

设计边界：
- cookie_cache.py 管的是「本机浏览器自动解密的个人缓存」，原则『仅本机、不外传、不跨用户』。
- 本模块管的是「用户经 /api/cookie/sync 自愿上报、知情同意的指定站点登录态」，
  用于网页版公共服务复用，让访客无需手动粘贴 Cookie。两者存储目录与用途完全分离。

安全 / 合规：
- 仅允许白名单域（金牌影院 chrqj/nmwchxx、B 站、优酷、腾讯系、YouTube 等），
  绝不收其他网站 Cookie。
- 存储加密（VDL_COOKIE_ENC_KEY；未配则降级为 600 权限明文），不落明文日志。
- 入池前用该 Cookie 试一次目标站验真（确认有效才收，垃圾 / 失效直接拒）。
- 后台定时探测（verify_and_prune）剔除失效项，避免公共池静默失效。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

import atomic_io

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

try:
    from cryptography.fernet import Fernet
except Exception:  # pragma: no cover
    Fernet = None


def _resolve_pool_dir() -> Path:
    """解析公共池存储目录。优先级：
    1. VDL_COOKIE_POOL_DIR —— 显式指定（已是完整目录，直接采用）
    2. RAILWAY_VOLUME_MOUNT_PATH —— Railway 持久卷自动注入，拼 /cookie_pool
    3. 兜底 —— ~/.videodownloader/cookie_pool（本地 / 无卷环境，向后兼容）
    这样挂了 Railway 持久卷后，池文件自动落到卷上、跨容器重启存活。
    """
    custom = os.environ.get("VDL_COOKIE_POOL_DIR")
    if custom:
        return Path(custom)
    mount = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if mount:
        return Path(mount) / "cookie_pool"
    return Path.home() / ".videodownloader" / "cookie_pool"


_POOL_DIR = _resolve_pool_dir()
_TTL = 30 * 24 * 3600  # 30 天，超时视为失效
_LOCK = threading.Lock()
# 「最近一次使用」的落盘节流窗口：get_cookie 每次解析都会命中，不节流等于
# 每次解析都重写池文件（读-改-写 + 原子替换）。60s 对「最近一次使用」这个
# 展示粒度完全够，却能把写放大压到「每条每分钟一次」。
_USED_MARK_INTERVAL = 60

# 白名单基础集（根域）。实际允许范围由 _root_domains() 动态计算：
#   = 基础集 + downloader._COOKIE_HARDENED_DOMAINS 派生 + env VDL_COOKIE_POOL_DOMAINS 扩展。
# 这样「哪些站允许上报登录态」不再写死 chrqj，加站只需改清单/配 env。
# 注意：weixin.qq.com 经 _strip_sub 归一化为 qq.com（腾讯系登录态都是 qq.com 级
# Cookie，微信视频号 wap_sid2 / 微视 / 腾讯视频 vus_session 同域），故白名单实际
# 放行整个腾讯系——与 _STRUCTURAL_FIELDS 里已有的 qq.com 条目语义一致。
_BASE_DOMAINS = {"chrqj.com", "bilibili.com", "youku.com", "weixin.qq.com"}

# 额外放行域：需要「桌面端把本机登录态自动推送到云端池」、但**不适合列入**
# downloader._COOKIE_HARDENED_DOMAINS 的站。二者拆开是因为 hardened 还有副作用
# ——它会把 403 文案改写成「该站需要登录 Cookie」，从而盖掉 YouTube 403 真正
# 该排查的代理/地区线索（见 tests/test_friendly_error_403.py）。
# youtube.com：2025 起对数据中心 IP 强制 bot 检测，网页版海外解析必须带登录态；
# 而 hardened 之外没有任何机制能让它进池（/api/cookie/sync 会以 400 拒收）。
_POOL_EXTRA_DOMAINS = {"youtube.com"}

# 🔴 2026-10-25 用户定档：百度网盘登录态（BDUSS）加入共享池，给其他用户一起用。
# 但**只允许显式贡献**，绝不进自动同步：
#   ① BDUSS = 完整网盘账号权限（能看该账号所有文件、拿会员带宽），敏感度远高于
#      视频平台 Cookie，未经同意推上云等于把用户的百度账号送人；
#   ② `_sync_domains_to_cloud` 会遍历 `_root_domains()` 自动解密并推送每个桌面用户
#      浏览器里的登录态 —— 百度一旦进那个集合，就等于替所有用户做了决定；
#   ③ 贡献入口只有网盘页那个「贡献到共享池」按钮（带确认弹窗说明后果），
#      用的是用户**手动保存**的 BDUSS，语义上是明确的「我愿意」。
_CONTRIB_ONLY_DOMAINS = {"baidu.com"}

# 🔴 2026-10-28：分享页 / 短链 / 极速版域 —— **不进自动同步遍历**。
# 硬依据在 downloader._CDN_TO_COOKIE_DOMAIN / _cookie_login_host()：解析取 Cookie 时
# 这些域会被先映射回主站（iesdouyin→douyin、gifshow/chenzhongtech→kuaishou、
# b23.tv→bilibili），注释原话是「这些域自己那份 Cookie 往往只有设备指纹，直接拿它
# 去解析 = 匿名请求」。⇒ 云端池里存这些域的条目**永远读不到**，纯冗余。
# 实测（2026-10-28 云端池 46 条）：iesdouyin 6 / chenzhongtech 4 / gifshow 3 = 13 条
# 占 28%，valid 0 条，而它们长期占着后台「缺口」列表，把「哪些域真缺登录态」淹没。
# ⚠️ 别误读成「这些域是游客态」：实测 iesdouyin 确有 sessionid/sid_tt、
# chenzhongtech 确有 passToken/userId，是根域同会话的**同名继承**。
# 另有实打实的耗时：每个域都要解一次浏览器 Cookies 库（macOS 走钥匙串），而自动
# 同步是「任何站点登录都触发一轮」，等于每轮多解 3 个域 + 3 次 1s 间隔。
# 注意：只排除**自动遍历**，手动贡献仍放行（现有行为不挡用户显式操作）。
_SHARE_ONLY_DOMAINS = {
    "iesdouyin.com",      # 抖音分享落地页
    "gifshow.com",        # 快手极速版 / 小剧场
    "chenzhongtech.com",  # 快手极速版（另一域）
    "xhslink.com",        # 小红书分享短链
    "xhslink.cn",
    "b23.tv",             # B 站短链（本就淡出白名单，列出以防将来被加回）
}

logger = logging.getLogger(__name__)

# 金牌影院验真用的签名参数（chrqj.com / nmwchxx.com 同一站，共用密钥与视频 ID）
_CHRQJ_SIGN_KEY = "cb808529bae6b6be45ecfab29a4889bc"
_CHRQJ_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_CHRQJ_TEST = ("116537", "877419")  # 与 chrqj.py _TESTS 样例一致


def _cipher():
    if Fernet is None:
        return None
    k = os.environ.get("VDL_COOKIE_ENC_KEY")
    if not k:
        return None
    try:
        return Fernet(k.encode() if isinstance(k, str) else k)
    except Exception:
        return None


def _norm_domain(domain: str) -> str:
    domain = (domain or "").strip().lower()
    for p in ("https://", "http://", "//"):
        if domain.startswith(p):
            domain = domain[len(p):]
    return domain.split("/")[0].split(":")[0]


def _strip_sub(domain: str) -> str:
    """提取根域（近似 eTLD+1）：去掉任意子域前缀，保留最后两段。"""
    d = _norm_domain(domain)
    if not d:
        return ""
    parts = d.split(".")
    if len(parts) <= 2:
        return d
    return ".".join(parts[-2:])


def _root_domains() -> set:
    """白名单根域集合 = 基础集 + hardened 清单派生 + env 扩展 + 仅贡献域。"""
    ds: set = {_strip_sub(d) for d in _BASE_DOMAINS if _strip_sub(d)}
    # 额外放行域（不需要「改写 403 文案」副作用的那批，如 youtube.com）
    ds |= {_strip_sub(d) for d in _POOL_EXTRA_DOMAINS if _strip_sub(d)}
    # 仅显式贡献域（百度网盘）：进 is_allowed，但被 auto/from-local 排除
    ds |= {_strip_sub(d) for d in _CONTRIB_ONLY_DOMAINS if _strip_sub(d)}
    # env 扩展：逗号/分号/空格分隔的域名列表
    extra = os.environ.get("VDL_COOKIE_POOL_DOMAINS", "")
    for d in extra.replace(";", ",").replace(" ", ",").split(","):
        d = _strip_sub(d)
        if d:
            ds.add(d)
    # 从下载层强反爬清单派生（惰性，避免拉重依赖）
    try:
        import downloader  # noqa: F401
        for d in getattr(downloader, "_COOKIE_HARDENED_DOMAINS", ()):
            d = _strip_sub(d)
            if d:
                ds.add(d)
    except Exception:
        pass
    return ds


def is_allowed(domain: str) -> bool:
    """判断域名是否在公共池白名单内（精确根域 或 其子域）。"""
    d = _strip_sub(domain)
    if not d:
        return False
    return any(d == r or d.endswith("." + r) for r in _root_domains())


def is_contrib_only(domain: str) -> bool:
    """是否「仅显式贡献」域（百度网盘）：不接受任何自动/隐式推送。"""
    d = _strip_sub(domain)
    if not d:
        return False
    return any(d == r or d.endswith("." + r) for r in _CONTRIB_ONLY_DOMAINS)


def is_share_only(domain: str) -> bool:
    """是否「分享页/短链」域：不进自动同步遍历（见 _SHARE_ONLY_DOMAINS 注释）。

    只用于**遍历过滤**，不要拿它去卡 is_allowed —— 存量客户端仍会推这些域，
    服务端一旦改成 400 拒收，它们会在每轮自动同步里反复重试。
    """
    d = _strip_sub(domain)
    if not d:
        return False
    return any(d == r or d.endswith("." + r) for r in _SHARE_ONLY_DOMAINS)


def _pool_file(domain: str) -> Path:
    safe = domain.replace("/", "_").replace("\\", "_").replace(":", "_")
    return _POOL_DIR / f"{safe}.json"


def _candidates(domain: str):
    """归一化后的候选 host（带 / 不带 www，并回退到根域）。

    例如 v.douyin.com 的视频请求要能匹配到存在 douyin.com.json 里的公共池 Cookie。
    """
    d = _norm_domain(domain)
    if not d:
        return []
    root = _strip_sub(d)
    seen: set[str] = set()
    out: list[str] = []
    for cand in (d, root):
        if not cand or cand in seen:
            continue
        seen.add(cand)
        out.append(cand)
        if cand.startswith("www."):
            no_www = cand[4:]
            if no_www not in seen:
                seen.add(no_www)
                out.append(no_www)
        else:
            with_www = "www." + cand
            if with_www not in seen:
                seen.add(with_www)
                out.append(with_www)
    return out


def _decrypt_item(c: dict) -> str:
    header = c.get("header")
    if header:
        return header
    enc = c.get("header_enc")
    cipher = _cipher()
    if enc and cipher:
        try:
            return cipher.decrypt(enc.encode()).decode()
        except Exception:
            return ""
    return ""


# ────────────────────────────────────────────────────────────────────────────
# 账号身份 / 会话变体识别（2026-10-18）
#
# 背景：池子里同一条「账号」会被反复抓取（buvid 每次刷新都会变），旧逻辑只按
# 整条 Cookie 的哈希去重 ⇒ 同一账号可以无限堆叠。实测 B 站池 1905 条 = 同 1 个
# 账号（DedeUserID 2092800535）的 1905 次快照，其中 1902 条是**未登录**游客态，
# 而 get_cookie 又取「最新一条」⇒ 解析永远拿到废 Cookie，恒 412。
# 这里改为按「登录身份字段」归一：同账号只留最近几条，同会话只验一次真。
# ────────────────────────────────────────────────────────────────────────────
_IDENTITY_FIELDS: dict[str, tuple[str, ...]] = {
    "bilibili.com": ("DedeUserID",),
    "youtube.com": ("SAPISID", "SSID", "HSID"),
    "douyin.com": ("sid_tt", "sessionid"),
    "iesdouyin.com": ("sid_tt", "sessionid"),
    "kuaishou.com": ("userId", "kuaishou.webid"),
    "xiaohongshu.com": ("web_session",),
    "youku.com": ("P__yk__uck", "unb", "__ysuid"),
    "qq.com": ("wxuin", "uin", "vus_session", "wap_sid2"),
    "tiktok.com": ("sid_tt", "sessionid"),
    "chenzhongtech.com": ("userId",),
    "baidu.com": ("BDUSS",),
}

# 会话变体字段：同账号可能同时存在多份「会话」（重新登录会换 SESSDATA），
# 同一 SESSDATA 的所有快照共享同一有效性 ⇒ 验真按变体做，一次即可代表整组。
_SESSION_FIELDS: dict[str, tuple[str, ...]] = {
    "bilibili.com": ("SESSDATA", "bili_jct"),
    "youtube.com": ("SAPISID", "SSID"),
    # 百度：同一 BDUSS 下可以有多个 STOKEN，换直链的成功率还不一样
    # （2026-10-28 实测：一个 STOKEN 拿明文直链，另一个只拿 4 KB 密文，
    #  被误判成「非超会」）。不加这条 → 两条快照同会话 → 只留最新那条
    #  ⇒ 恰好留下密文那份，解析必失败。见 routers/cloud_drive._baidu_login_cookies。
    "baidu.com": ("STOKEN",),
}

_MAX_PER_IDENTITY = 3   # 同一账号最多留几条快照
_MAX_ANON = 5           # 无身份字段的游客 Cookie 每域最多几条
_MAX_PER_DOMAIN = 30    # 每域总量兜底
_PRUNE_VERIFY_BUDGET = 8  # 单次探测最多验真几个会话变体（防风控）


def _parse_cookie(header: str) -> dict:
    """把 `k=v; k2=v2` 解析成字典（同名取最后一个，与浏览器行为一致）。"""
    out: dict = {}
    for part in (header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k:
            out[k] = v
    return out


def _identity(domain: str, header: str) -> str | None:
    """返回该 Cookie 的账号身份标识（同账号恒定）；无身份字段则 None。"""
    d = _strip_sub(domain)
    fields = _IDENTITY_FIELDS.get(d)
    if not fields:
        return None
    kv = _parse_cookie(header)
    for f in fields:
        v = kv.get(f)
        if v:
            return "%s:%s=%s" % (d, f, hashlib.sha256(v.encode()).hexdigest()[:10])
    return None


def _session_key(domain: str, header: str) -> str:
    """返回「会话变体」标识：身份 + 会话字段哈希（无会话字段时退化为身份）。"""
    d = _strip_sub(domain)
    ident = _identity(d, header) or "anon"
    fields = _SESSION_FIELDS.get(d) or ()
    kv = _parse_cookie(header)
    if not fields:
        return ident
    raw = "|".join("%s=%s" % (f, kv.get(f, "")) for f in fields)
    return ident + "#" + hashlib.sha256(raw.encode()).hexdigest()[:8]


def _entry_identity(domain: str, c: dict) -> str | None:
    """条目的身份标识（优先读入池时记下的 id，老条目现算）。"""
    ident = c.get("id")
    if isinstance(ident, str) and ident:
        return ident
    return _identity(domain, _decrypt_item(c))


def _cap_cookies(domain: str, cookies: list) -> list:
    """裁剪：同身份留最新 N 条、游客条目限量、域总量兜底。

    保持既有顺序约定「旧的在前、新的在后」（get_cookie 用 reversed 取最新）。
    """
    kept: list = []
    per_id: dict = {}
    anon = 0
    # 按「列表顺序的倒序」取最新：池文件与内存列表都约定「旧的在前、新的在后」，
    # 用 ts 排序在同一秒内多次入池时不可靠（ts 相同），用顺序才是真实先后。
    for c in reversed(cookies):
        ident = _entry_identity(domain, c)
        if ident:
            n = per_id.get(ident, 0)
            if n >= _MAX_PER_IDENTITY:
                continue
            per_id[ident] = n + 1
        else:
            if anon >= _MAX_ANON:
                continue
            anon += 1
        kept.append(c)
        if len(kept) >= _MAX_PER_DOMAIN:
            break
    return list(reversed(kept))


def _save(domain: str, cookies: list) -> bool:
    try:
        _POOL_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(_POOL_DIR, 0o700)
        except Exception:
            pass
        cipher = _cipher()
        payload = []
        for c in cookies:
            item = {"ts": c.get("ts", int(time.time())), "source": c.get("source", "sync")}
            # 身份标识要一起落盘：否则每次读取都要重新解析 Cookie 才能去重
            ident = c.get("id") or _identity(domain, _decrypt_item(c))
            if ident:
                item["id"] = ident
            # 「最近一次使用」痕迹必须 survives _save：_save 是按字段白名单重建
            # 条目的，漏掉 used_ts 会让每次 add/prune 都把使用记录清掉，
            # 后台「最近一次使用」那一列永远空着。
            used = int(c.get("used_ts") or 0)
            if used:
                item["used_ts"] = used
            # 会员标记（仅百度）要随条目存活：会员号（vip_type>0）标记为 True，
            # get_cookies 据此把会员账号排在最前，确保换直链先吃会员带宽。
            member = c.get("member")
            if member:
                item["member"] = True
            header = c.get("header") or ""
            enc = c.get("header_enc") or ""
            if not header and not enc:
                # 双空条目（历史写坏/旧 bug 产物）直接丢弃，避免池文件无限膨胀
                continue
            if cipher:
                # 有明文 → 加密存储；只有密文 → 原样保留
                # （修复：旧实现用 c.get("header") 取明文，加密记录取到空串后
                #  走 else 把已有密文重写成空明文条目，导致每次 _save 丢失历史记录）
                item["header_enc"] = cipher.encrypt(header.encode()).decode() if header else enc
            else:
                if header:
                    item["header"] = header
                elif enc:
                    # 无 cipher 但有密文：尝试解密回明文，失败则保留密文
                    plain = _decrypt_item(c)
                    if plain:
                        item["header"] = plain
                    else:
                        item["header_enc"] = enc
            payload.append(item)
        f = _pool_file(domain)
        # 保留既有 ckey 字段（add_cookie/add 同步写入时不能把已贡献的 ckey 清掉）
        prev_ckey = None
        try:
            _prev = json.loads(f.read_text()) if f.exists() else {}
            prev_ckey = _prev.get("ckey")
        except Exception:
            prev_ckey = None
        out = {"cookies": payload}
        if prev_ckey:
            out["ckey"] = prev_ckey
        text = json.dumps(out, ensure_ascii=False)
        # 原子写（见 atomic_io）：同目录**唯一**临时名再换入，避免跨进程并发
        # （VPS 推送 / Railway prune / 读取）读到「写一半」的撕裂文件
        #（之前诡异 65 字节空条目的根因之一）。历史写法用固定名 `.tmp`，两个写者会互截断。
        atomic_io.atomic_write_text(f, text)
        logger.info("[cookie_pool] _save ok domain=%s file=%s bytes=%s", domain, f, len(text))
        return True
    except Exception as e:
        logger.error("[cookie_pool] _save failed domain=%s dir=%s err=%s", domain, _POOL_DIR, e)
        return False


def get_cookie(domain: str) -> str | None:
    """返回该域最新有效的明文 Cookie（最近上报优先）；无则返回 None。"""
    candidates = _candidates(domain)
    logger.info("[cookie_pool] get domain=%s candidates=%s", domain, candidates)
    for d in candidates:
        f = _pool_file(d)
        logger.info("[cookie_pool] get file=%s exists=%s", f, f.exists())
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text())
            for c in reversed(data.get("cookies", [])):  # 最新优先
                if time.time() - c.get("ts", 0) > _TTL:
                    continue
                header = _decrypt_item(c)
                if header:
                    logger.info("[cookie_pool] get hit domain=%s candidate=%s len=%s", domain, d, len(header))
                    # 记录「最近一次使用」：后台据此判断哪条 Cookie 真在被消耗、
                    # 哪些常年没人用（补注入 / 清理的唯一依据）。节流见 _mark_used。
                    _mark_used(f, header)
                    return header
        except Exception as e:
            logger.warning("[cookie_pool] get read error domain=%s file=%s: %s", domain, f, e)
            continue
    logger.info("[cookie_pool] get miss domain=%s", domain)
    return None


def get_cookies(domain: str, limit: int = 8) -> list:
    """返回该域**全部**可用明文 Cookie（最新在前，按登录会话去重）。

    与 get_cookie 的唯一区别是「给多条」。存在的理由是百度网盘 2026-10-27 起
    把「换下载直链」按**账号等级**分流（实测三个账号，同一链接交叉复现）：

      · vip_type=2（SVIP）→ /api/sharedownload 返回明文 list，含真直链；
      · vip_type=0（普通）→ 同一接口返回 4 KB 的 AES 密文串，无法使用。

    池里同时存在会员号和普通号时，只取最新一条会随机踩到普通号 ⇒ 解析必然
    报「返回了加密数据」。所以换直链这一环必须能按序换号重试（见
    cloud_drives/baidu.py 的 resolve_share）。其他域仍用 get_cookie。

    ⚠️ 这里**不**调 `_mark_used`：多读出来的是「候选」，真正被用掉的那条由
    上层（解析成功后）决定，把候选全标成「最近使用」会让后台的清理判断失真。
    """
    out: list = []
    out_members: list = []
    seen: set = set()
    for d in _candidates(domain):
        f = _pool_file(d)
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text())
        except Exception as e:  # noqa: BLE001
            logger.warning("[cookie_pool] get_cookies read error domain=%s file=%s: %s",
                           domain, f, e)
            continue
        for c in reversed(data.get("cookies", [])):   # 最新优先
            if time.time() - c.get("ts", 0) > _TTL:
                continue
            header = _decrypt_item(c)
            if not header:
                continue
            key = _session_key(d, header)
            if key in seen:      # 同一登录会话的多个快照只留最新一条
                continue
            seen.add(key)
            out.append(header)
            out_members.append(bool(c.get("member")))
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    # 百度网盘：会员账号（vip_type>0）排在最前，确保换直链先吃会员带宽；
    # 会员失效（限速/掉会员）时再落到后面的非会员号。其他域不做重排。
    if domain == "baidu.com" and len(out) > 1:
        ranked = sorted(zip(out, out_members), key=lambda x: (not x[1],))
        out = [h for h, _ in ranked]
    logger.info("[cookie_pool] get_cookies domain=%s n=%s", domain, len(out))
    return out


def _mark_used(f: Path, header: str) -> None:
    """把这条 Cookie 标记为「刚被用过」（后台「最近一次使用」列的数据源）。

    两个设计约束：
    1. **节流**：同一条目 60s 内最多落盘一次（_USED_MARK_INTERVAL）。解析高峰
       期 get_cookie 每秒被打很多次，不节流等于每次都重写池文件。
    2. **只改一个字段**：不走 _save()——它按 ts/source/id 白名单重建整张表，
       会把别的字段顺带重写；这里读原文件、只改 used_ts、原样原子写回。

    匹配按**明文**：同文件内明文唯一（add_cookie 按明文去重），解密比对最稳，
    不依赖 ts/id 在并发写入后发生变化。任何异常都静默吞掉：使用痕迹丢一条
    无所谓，绝不能让它影响解析主流程。
    """
    now = int(time.time())
    with _LOCK:
        try:
            if not f.exists():
                return
            data = json.loads(f.read_text())
            for c in data.get("cookies") or []:
                if _decrypt_item(c) != header:
                    continue
                if now - int(c.get("used_ts") or 0) < _USED_MARK_INTERVAL:
                    return  # 刚记过，不写盘
                c["used_ts"] = now
                atomic_io.atomic_write_text(f, json.dumps(data, ensure_ascii=False))
                return
        except Exception:
            pass


def add_cookie(domain: str, header: str, source: str = "sync") -> bool:
    """入池。返回 True=新增 / 更新，False=重复或非法域。"""
    domain = _norm_domain(domain)
    allowed = is_allowed(domain)
    logger.info("[cookie_pool] add domain=%s allowed=%s source=%s len=%s", domain, allowed, source, len(header or ""))
    if not allowed:
        return False
    # 统一存根域：weixin.qq.com → qq.com，使 channels.weixin.qq.com 等子域
    # 经 _candidates 回退能读到（候选含 root=qq.com）；bilibili/youku 根域不变。
    root = _strip_sub(domain)
    if root:
        domain = root
    header = (header or "").strip()
    if not header:
        return False
    # 入池前验真：明确无效（如 B 站未登录游客态）直接拒。
    # 2026-10-18 前这里没有验真，服务器每 20 分钟抓一次的死 Cookie 全进来了，
    # 而 get_cookie 取「最新一条」⇒ 永远拿到废的（实测 1905 条里 1902 条无效）。
    if verify_cookie(domain, header) is False:
        logger.warning("[cookie_pool] reject-invalid domain=%s source=%s len=%s",
                       domain, source, len(header))
        return False
    ident = _identity(domain, header)
    # 百度网盘：入池时探一次会员身份（uinfo 的 vip_type>0=会员），给「会员优先」排序用。
    # 网络不通时静默降级为 False（非会员），不影响入池，只是排序优先级低。
    member = False
    if domain == "baidu.com":
        try:
            _ok, _info = baidu_probe(header)
            member = int((_info or {}).get("vip_type") or 0) > 0
        except Exception:
            member = False
    now = int(time.time())
    with _LOCK:
        f = _pool_file(domain)
        logger.info("[cookie_pool] add file=%s exists=%s", f, f.exists())
        cookies = []
        if f.exists():
            try:
                cookies = json.loads(f.read_text()).get("cookies", [])
            except Exception:
                cookies = []
        for c in cookies:  # 去重：同明文已存在则仅刷新 ts
            if _decrypt_item(c) == header:
                c["ts"] = now
                c["source"] = source
                if ident:
                    c["id"] = ident
                c["member"] = member
                return _save(domain, _cap_cookies(domain, cookies))
        item = {"header": header, "ts": now, "source": source}
        if ident:
            item["id"] = ident
        item["member"] = member
        cookies.append(item)
        ok = _save(domain, _cap_cookies(domain, cookies))
        logger.info("[cookie_pool] add saved domain=%s total=%s ident=%s ok=%s",
                    domain, len(cookies), ident or "-", ok)
        return ok


def verify_chrqj(header: str, domain: str = "chrqj.com") -> bool | None:
    """用 Cookie 试一次金牌影院签名接口。True=有效, False=明确无效, None=无法判定(网络不可达)。

    domain 传实际站域（chrqj.com / nmwchxx.com）：两者是同一站（老域名首页
    302 → 新域名），API/签名密钥/视频 ID 全共用，但 API 主机与 Referer 要跟着
    域走，跨域 Referer 有被拒的风险（2026-10-21 实测同对 ID 两边都能出片）。
    """
    if requests is None:
        return None
    try:
        d = _strip_sub(domain) or "chrqj.com"
        base = f"https://www.{d}"
        vid, nid = _CHRQJ_TEST
        params = {"clientType": "1", "id": vid, "nid": nid}
        t = str(int(time.time() * 1000))
        g = "&".join("%s=%s" % (k, params[k]) for k in sorted(params))
        h = "%s&key=%s&t=%s" % (g, _CHRQJ_SIGN_KEY, t)
        sign = hashlib.sha1(hashlib.md5(h.encode()).hexdigest().encode()).hexdigest()
        headers = {
            "User-Agent": _CHRQJ_UA,
            "Referer": f"{base}/",
            "sign": sign,
            "t": t,
            "deviceId": str(uuid.uuid4()),
            "authorization": "",
            "Cookie": header,
        }
        r = requests.get(f"{base}/mw-movie/anonymous/v2/video/episode/url",
                         params=params, headers=headers, timeout=10)
        if r.status_code != 200:
            return False
        dd = r.json()
        if dd.get("code") == 200 and (dd.get("data") or {}).get("list"):
            return True
        return False
    except Exception:
        return None


_BILI_NAV = "https://api.bilibili.com/x/web-interface/nav"

# 百度网盘登录态验真（2026-10-25 共享池接入）：问 xpan/nas uinfo。
# errno=0 ⇒ 登录态有效（响应里带 vip_type，可判断是不是会员账号）；
# errno≠0 ⇒ 明确无效（BDUSS 过期/被踢下线时百度回 -6）；
# 网络不可达 ⇒ None（放行，与 verify_cookie「仅明确无效才拒」的口径一致）。
_BAIDU_UINFO = "https://pan.baidu.com/rest/2.0/xpan/nas?method=uinfo"
_BAIDU_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def baidu_probe(header: str) -> tuple[bool | None, dict]:
    """问百度 uinfo：返回 (判定, 响应 dict)。判定 True=有效 / False=明确无效 / None=问不出来。"""
    import json
    import urllib.error
    import urllib.request
    req = urllib.request.Request(_BAIDU_UINFO, headers={
        "Cookie": header or "",
        "User-Agent": _BAIDU_UA,
        "Referer": "https://pan.baidu.com/",
    })
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        # 4xx 带 JSON 体时仍能拿到 errno（百度对过期 BDUSS 回 200 + errno=-6）
        try:
            data = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return (None, {})
    except Exception:
        return (None, {})
    if not isinstance(data, dict):
        return (None, {})
    return ((int(data.get("errno") or 0) == 0), data)


def verify_baidu(header: str) -> bool | None:
    """百度网盘登录态验真（真问站点，不是结构校验）。"""
    ok, _info = baidu_probe(header)
    return ok


def verify_bilibili(header: str) -> bool | None:
    """B 站登录态验真：nav 接口 isLogin 为真才算有效。

    背景（2026-10-18）：服务器上的无头浏览器每 20 分钟抓一次 Cookie，抓到的
    全是「未登录」游客态（只有 buvid 和历史的 DedeUserID，SESSDATA 已失效）；
    而 B 站既不在结构校验表、也没有测试 URL ⇒ 旧逻辑返回 None（放行）⇒ 1905 条
    里 1902 条是废的，且 get_cookie 取最新一条 = 永远是废的 ⇒ 解析恒 412。

    返回 False 仅当站点明确回答「未登录」(-101)；网络不可达 / 风控 412 返回
    None（保留，避免把好 Cookie 误杀）。
    """
    if requests is None:
        return None
    try:
        r = requests.get(_BILI_NAV, headers={
            "Cookie": header,
            "User-Agent": _CHRQJ_UA,
            "Referer": "https://www.bilibili.com/",
        }, timeout=8)
        if r.status_code != 200:
            return None
        d = r.json()
    except Exception:
        return None
    code = d.get("code")
    if code == 0:
        return bool((d.get("data") or {}).get("isLogin"))
    if code == -101:  # 账号未登录
        return False
    return None


# 站点通用验真用的测试 URL（domain 根域 -> 该站任意一个可解析的视频页）。
# 只有「接口签名特殊、需专属验真」的站才写专属 verify；其余站用 _verify_generic
# 拿这里的 URL 试解析判定 Cookie 有效性。env VDL_COOKIE_POOL_TEST_URLS 可追加：
#   "domain=url;domain2=url2"
_TEST_URLS = {
    "chrqj.com": "https://www.chrqj.com/vod/play/116537/1/877419",
    # 金牌影院新域名（同一站）：站内标准形态，供 yt-dlp 通用验真兜底
    "nmwchxx.com": "https://nmwchxx.com/vod/play/116537/sid/877419",
}


def _test_url(domain: str) -> str | None:
    d = _strip_sub(domain)
    if d in _TEST_URLS:
        return _TEST_URLS[d]
    extra = os.environ.get("VDL_COOKIE_POOL_TEST_URLS", "")
    for part in extra.split(";"):
        if "=" in part:
            k, _, v = part.partition("=")
            if _strip_sub(k) == d:
                return v.strip()
    return None


def _verify_generic(domain: str, header: str) -> bool | None:
    """yt-dlp 通用验真兜底：带 Cookie 试解析该站测试 URL，成功拿到 info 即视为有效。

    适用于一切有 yt-dlp 提取器的平台（douyin/快手/bilibili/v.qq 等），
    无需为每站单独写签名验真。无测试 URL 时返回 None（无法判定，放行入池）。
    """
    url = _test_url(domain)
    if not url:
        return None
    try:
        from yt_dlp import YoutubeDL
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "socket_timeout": 15,
            "retries": 1,
            "http_headers": {"Cookie": header, "User-Agent": _CHRQJ_UA},
        }
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return bool(info and info.get("id"))
    except Exception:
        return False


# 结构校验白名单：对 yt-dlp 试解析不稳定（常误报 False）的站，改为只检查
# Cookie 是否含该站关键登录态字段。命中即视为「结构有效」放行入池，不再
# 依赖 yt-dlp 试解析（优酷会员/受限内容常被 -3007 误判为无效）。
# 字段名集合取自各站公开登录态 cookie 命名（仅校验键名存在，不读取值）。
_STRUCTURAL_FIELDS: dict[str, set[str]] = {
    "youku.com": {"P__yk__uck", "yktk", "cna", "x5sec", "sess_vkey", "unb"},
    "v.qq.com": {"vus_session", "video_platform", "uid_tt", "login_ecookie"},
    "qq.com": {
        "vus_session", "video_platform", "uid_tt", "login_ecookie",
        # 微信视频号登录态关键字段（channels.weixin.qq.com / finder 会话）
        "wap_sid2", "pass_ticket", "appmsg_token", "wxuin", "wxsid", "skey",
    },
    # YouTube：走 yt-dlp 试解析验真在国内机房必然失败（连不上或被 bot 拦），
    # 会被误判 False 而把有效 Cookie 拒之池外。改为只校验「是否含登录态字段」。
    "youtube.com": {
        "SID", "HSID", "SSID", "APISID", "SAPISID", "LOGIN_INFO",
        "__Secure-1PSID", "__Secure-3PSID",
        "__Secure-1PAPISID", "__Secure-3PAPISID",
    },
    # 抖音 / 快手 / TikTok / 小红书：这些站没有联网探针（yt-dlp 试解析在机房
    # 易被风控而误判 False，会把有效 Cookie 拒之池外）。改为只校验「是否含
    # 登录态字段」：命中即「结构有效」，后台标「结构校验」，不冒充站点确认。
    # 🔴 字段只取「登录后才存在」的：小红书 a1 游客也有，绝不能当登录态。
    "douyin.com": {"sessionid", "sid_tt", "passport_csrf_token"},
    "iesdouyin.com": {"sessionid", "sid_tt"},
    "kuaishou.com": {"passToken", "userId", "kuaishou.webid"},
    "chenzhongtech.com": {"passToken", "userId"},
    "tiktok.com": {"sessionid", "sid_tt"},
    "xiaohongshu.com": {"web_session"},
}


def _structural_check(domain: str, header: str) -> bool | None:
    """轻量结构校验：命中白名单站关键登录态字段即返回 True（结构有效）。

    返回 True  = 含关键字段，结构有效，放行入池（无需 yt-dlp 试解析）。
    返回 None  = 该域无结构校验表（交给 _verify_generic 判定）。
    不返回 False（结构校验只做「存在即放行」，不做「缺失即否决」——
    缺失关键字段的站仍走通用验真，避免误杀仅用部分字段即可生效的 Cookie）。
    """
    d = _strip_sub(domain)
    fields = _STRUCTURAL_FIELDS.get(d)
    if not fields:
        return None
    # 归一化 cookie 头为键名集合（兼容 "k=v; k2=v2" 与 "k=v; " 等写法）
    keys: set[str] = set()
    for part in (header or "").split(";"):
        k = part.split("=", 1)[0].strip()
        if k:
            keys.add(k)
    if keys & fields:
        logger.info("[cookie_pool] structural_check hit domain=%s", d)
        return True
    logger.info("[cookie_pool] structural_check miss domain=%s（缺关键登录态字段）", d)
    return None


def is_guest_only(domain: str, header: str) -> bool:
    """本机这份 Cookie 是否「有表可查、且一个登录态字段都没有」＝游客态。

    与 `_structural_check` 的分工：
      - `_structural_check` 返回 None **不代表无效**（它只做「存在即放行」，缺失要
        交给通用验真），所以不能拿 None 直接否决入池；
      - 但「推送到云端池」这个动作不一样：结构表里那批字段**都是登录后才出现的**，
        一个都没命中就说明本机根本没登录该站，推上去只会污染池子。实测证据
        （2026-10-28）：qq.com 3 条、tiktok.com 2 条全部 invalid，正是这类。
    返回 False 表示「判不了」（该域无结构表），不要在这种情况下否决。
    """
    d = _strip_sub(domain)
    if not _STRUCTURAL_FIELDS.get(d):
        return False
    return _structural_check(d, header) is None


def add_ckey(domain: str, ckey: str, source: str = "contrib") -> bool:
    """存优酷 ckey 播放签名（与 Cookie 同域存储，独立时效）。

    ckey 是优酷 UPS 接口必需的播放签名参数（YoukuIE 不会生成，缺则 -3007）。
    它有时效（数小时~1天），过期需用户重新 Copy as cURL 贡献。
    """
    domain = _norm_domain(domain)
    if not is_allowed(domain):
        logger.warning("[cookie_pool] add_ckey 非白名单域=%s 拒绝", domain)
        return False
    ckey = (ckey or "").strip()
    if not ckey:
        return False
    with _LOCK:
        f = _pool_file(domain)
        data = {}
        if f.exists():
            try:
                data = json.loads(f.read_text())
            except Exception:
                data = {}
        data["ckey"] = {"ckey": ckey, "ts": int(time.time()), "source": source}
        try:
            _POOL_DIR.mkdir(parents=True, exist_ok=True)
            atomic_io.atomic_write_json(f, data, indent=None)
            logger.info("[cookie_pool] add_ckey ok domain=%s len=%s", domain, len(ckey))
            return True
        except Exception as e:
            logger.error("[cookie_pool] add_ckey failed domain=%s err=%s", domain, e)
            return False


def get_ckey(domain: str) -> str | None:
    """读取该域最新有效的 ckey（最近贡献优先）；无/过期则返回 None。"""
    for d in _candidates(domain):
        f = _pool_file(d)
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text())
            item = data.get("ckey")
            if not item:
                continue
            if time.time() - item.get("ts", 0) > _TTL:
                continue
            ck = item.get("ckey")
            if ck:
                logger.info("[cookie_pool] get_ckey hit domain=%s candidate=%s len=%s", domain, d, len(ck))
                return ck
        except Exception as e:
            logger.warning("[cookie_pool] get_ckey read error domain=%s: %s", d, e)
    logger.info("[cookie_pool] get_ckey miss domain=%s", domain)
    return None


def verify_cookie(domain: str, header: str) -> bool | None:
    """按域分发验真：金牌影院一族（chrqj/nmwchxx）走专属签名验真，其余优先结构校验、再 yt-dlp 通用验真。

    口径统一为「仅明确无效(False)才拒，无法判定(None)放行」，与
    verify_and_prune 对 None 保留的语义一致。这样 yt-dlp 试解析不稳定的站
    （如优酷 -3007）不会因误判 False 而被拒之池外。
    """
    d = _strip_sub(domain)
    # 金牌影院一族（老域名 chrqj.com + 新域名 nmwchxx.com，同一站）走签名验真
    if d in ("chrqj.com", "nmwchxx.com"):
        return verify_chrqj(header, d)
    # B 站：游客态 Cookie 与登录态「长得一样」（都有 DedeUserID/buvid），
    # 只能问站点本身；这是唯一能区分两者的判据。
    if d == "bilibili.com":
        return verify_bilibili(header)
    # 百度网盘（2026-10-25）：同理，游客态也带一堆字段，只能问站点；
    # 且这里要的是「这个账号是不是真登录、是不是会员」，问一次全知道。
    if d == "baidu.com":
        return verify_baidu(header)
    # 健康检查探针占位（SID=RELAYTEST 之类），不是真实登录态，永远不该入池
    if "RELAYTEST" in (header or ""):
        return False
    # 优先结构校验：命中白名单站关键字段即放行
    s = _structural_check(d, header)
    if s is True:
        return True
    # 未命中结构字段表 / 未含关键字段：降级走 yt-dlp 通用试解析
    return _verify_generic(d, header)


def _prune_one(domain: str) -> int:
    """对单个规范化域文件做剔除，返回剩余有效数。

    2026-10-18 起改为「按会话变体验真，每个变体只验一次」：旧实现逐条验真，
    B 站 1905 条就是 1905 次请求（必然触发 412/封 IP），而且完全没必要——
    同一 SESSDATA 的所有快照共享同一有效性。顺带按身份裁剪（同账号留最新 3 条）。
    """
    with _LOCK:
        f = _pool_file(domain)
        if not f.exists():
            return 0
        try:
            cookies = json.loads(f.read_text()).get("cookies", [])
        except Exception:
            return 0
        now = time.time()
        groups: dict = {}
        order: list = []
        for c in cookies:
            header = _decrypt_item(c)
            if not header:
                continue
            if now - c.get("ts", 0) > _TTL:
                continue
            key = _session_key(domain, header)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append((c, header))
        kept: list = []
        budget = _PRUNE_VERIFY_BUDGET
        for key in order:
            items = sorted(groups[key], key=lambda x: x[0].get("ts") or 0, reverse=True)
            ok = None
            if budget > 0:
                budget -= 1
                ok = verify_cookie(domain, items[0][1])
            if ok is False:  # 明确无效：整个会话变体都是死的，整组丢弃
                logger.info("[cookie_pool] prune drop domain=%s session=%s n=%s",
                            domain, key, len(items))
                continue
            kept.extend(item for item, _ in items[:_MAX_PER_IDENTITY])
        kept.sort(key=lambda x: x.get("ts") or 0)   # 旧的在前，get_cookie 用 reversed 取最新
        kept = _cap_cookies(domain, kept)
        _save(domain, kept)
        return len(kept)


def verify_and_prune(domain: str) -> int:
    """后台探测：对域及其 www 变体剔除过期/失效项，返回剩余有效总数。"""
    domain = _norm_domain(domain)
    if not is_allowed(domain):
        return 0
    total = 0
    for d in _candidates(domain):
        total += _prune_one(d)
    return total


def all_domains() -> list:
    return sorted(_root_domains())


# ----------------------------------------------------------------------------
# 按需补推（Railway -> VPS 守护进程）
# 场景：Railway 容器重建/重启后持久卷被清空，或下载时公共池 miss。
# 主动召唤 VPS 守护进程的 /v1/push 立即推送一次 Cookie，使池在 ~30s 内补满，
# 不再依赖人工补推。只在配置了 VDL_COOKIE_REFILL_URL + VDL_COOKIE_REFILL_TOKEN 时生效。
# ----------------------------------------------------------------------------
_REFILL_COOLDOWN: dict[str, float] = {}


def request_refill(domain: str, blocking: bool = False) -> bool:
    """请求 VPS 守护进程立即补推一次指定域 Cookie。

    - 带每域冷却（默认 60s），避免重启后请求洪峰反复触发。
    - blocking=False 时后台线程执行（不阻塞当前请求）；True 时同步等待结果。
    返回是否成功发起/完成。
    """
    domain = _norm_domain(domain)
    if not is_allowed(domain):
        return False
    base = os.environ.get("VDL_COOKIE_REFILL_URL")
    token = os.environ.get("VDL_COOKIE_REFILL_TOKEN")
    if not base or not token:
        logger.info("[cookie_pool] request_refill skipped (未配置 REFILL_URL/TOKEN) domain=%s", domain)
        return False
    now = time.time()
    last = _REFILL_COOLDOWN.get(domain, 0.0)
    if now - last < 60:
        return False
    _REFILL_COOLDOWN[domain] = now

    def _do():
        try:
            import urllib.request

            url = (base.rstrip("/") + "/v1/push?token="
                   + urllib.parse.quote(token, safe=""))
            req = urllib.request.Request(
                url, method="POST",
                headers={"User-Agent": "vdl-cookie-refill"},
            )
            with urllib.request.urlopen(req, timeout=90) as r:
                data = r.read().decode() or "{}"
            ok = bool(json.loads(data).get("ok"))
            logger.info("[cookie_pool] request_refill done domain=%s ok=%s", domain, ok)
        except Exception as e:
            logger.warning("[cookie_pool] request_refill failed domain=%s: %s", domain, e)

    if blocking:
        _do()
        return True
    t = threading.Thread(target=_do, daemon=True)
    t.start()
    return True

