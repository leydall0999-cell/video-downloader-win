"""百度网盘解析器（cloud_drives/baidu.py）。

职责：输入一个百度网盘分享链接（+ 提取码）+ 用户百度登录态 Cookie，
产出可交给 VDL 下载器的直链。不负责实际下载 —— 下载由 server/downloader.py
走现有 per-task cookie 透传完成（百度 dlink 的 CDN host 不在 CHINA_DOMAINS，
下载器不会强加 Referer，而登录态 cookie 会注入）。

合规定位：只下载「用户自己的网盘文件 / 别人分享给用户」的文件，不批量抓取、
不破解限速。登录态只落本机（Keychain 或 VDL_DATA_DIR 隔离目录），
不进云端公共 cookie 池。

⚠️ 本模块基于 2026-10-23 对 Chrome 156 时代百度新接口的实测
   （详见 linkswift_research/BAIDU_API_156_FINDINGS.md），四步为：
     ① POST /share/verify        提取码 → 种 BDCLND cookie（即 sekey）
     ② GET  /share/tplconfig     取 sign / timestamp
     ③ GET  /share/list          列文件（顺带拿到 uk / share_id）
     ④ GET  /api/filemetas?dlink=1  换 dlink（**明文字段 info[].dlink**）
   关键分工：①②③④ 全链路**匿名可过**，但 dlink 的 GET 下载**必须带登录态**
   （匿名 403 / error_code 31326 "user is not authorized"）。
   ⇒ 解析用的 Session 刻意**不带**登录 cookie（实测带全套 cookie 时
     tplconfig 会返回 HTML 而非 JSON）；登录态只塞进 CloudFile.cookie 给下载器。

🔄 2026-10-24 换直链接口变更（重要，别改回去）：
   原 ④ `/api/sharedownload` 已把 `list` 从数组换成 **AES 密文的 base64 字符串**
   （单文件 4184 字符 / 3136 字节，16 字节对齐；带登录态后变 4192 字符但仍是密文）。
   密钥不在客户端可获得范围内（sekey / sign / md5 等候选都试过解不开），
   旧代码 `for item in list` 会直接 AttributeError。
   现有两条实测可用的替代路径：
     · `GET /api/filemetas?dlink=1&fsids=[..]&uk=&shareid=&type=dlink` → 明文 dlink，主路径
     · dlink 下载时 UA 必须**非浏览器**（浏览器 UA 一律 403 31326），见 DLINK_UA
   另注：同一 IP 短时间高频请求会触发 `hitcode:104` 频控（403），下载器需退避重试。
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_PAN = "pan.baidu.com"
_BASE = f"https://{_PAN}"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")
_TIMEOUT = 20

# 🔴 面向用户的**唯一**失败文案（2026-10-27 乐懂定档，别改回去）。
# 前端不再展示 errno / 会员等级 / 浏览器锁库等内部细节，一律只显示这一句；
# 真实原因必须走 logger 进运维监控（每条替换点都已配 logger.warning/info）。
# 其他模块请引用本常量（routers/cloud_drive.py → baidu.USER_ERROR_TEXT；
# downloader.py → _user_error_text()），不要在别处再写一份字面量，否则又会漂。
USER_ERROR_TEXT = "百度网盘接口返回异常（可能触发了风控，或分享链接已失效），请稍后重试"

# 百度 errno → 人话（实测汇总，详见 FINDINGS §3）
# ⚠️ 仅供**日志与运维监控**使用：这些细节不再直接上屏（见上面的 USER_ERROR_TEXT）。
_ERRNO_MSG = {
    0: "成功",
    2: "参数错误",
    # -6 曾被写成「接口已下线」，是误判：2026-10-27 实测 filemetas 匿名请求回 -6、
    # 带上 BDUSS 后回 0（只是 info 为空），说明它是「缺登录态」而不是接口没了。
    -6: "缺少有效的百度登录态",
    -12: "提取码校验失败",
    116: "分享不存在",
    105: "分享链接已失效，或提取码不正确",
    118: "没有下载权限",
    113: "页面已过期",
    121: "文件数量过多",
    -1: "分享违规",
    31326: "下载需要百度登录态",
    31023: "不支持的预览格式",
}


def _errno_msg(errno, default: str = "百度接口返回错误") -> str:
    try:
        errno = int(errno)
    except (TypeError, ValueError):
        return f"{default}（errno={errno}）"
    msg = _ERRNO_MSG.get(errno)
    return f"{msg}（errno={errno}）" if msg else f"{default}（errno={errno}）"


@dataclass
class CloudFile:
    """一个可下载文件：直链 + 下载它所需的 cookie / 额外头。"""
    name: str
    size: int
    dlink: str
    cookie: str = ""
    headers: dict = field(default_factory=dict)
    need_login: bool = False   # True = 直链已拿到但没有百度登录态，下载会 403
    # 文件在分享里的相对目录（如 "言情/"），只用于**展示与去重**；
    # 落盘文件名永远只用 name —— 带 "/" 会让 showSaveFilePicker / a.download 直接报错。
    path: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "size": self.size,
            "dlink": self.dlink,
            "cookie": self.cookie,
            "headers": self.headers,
            "need_login": self.need_login,
            "path": self.path,
        }


class NeedMemberLogin(ValueError):
    """换直链时百度返回密文 —— 说明当前用的是**普通账号**（vip_type=0）。

    2026-10-27 实测，同一分享链接 × 三个池内账号，可稳定复现：

      · vip_type=2（SVIP）→ /api/sharedownload 的 list 是明文数组，元素含 dlink；
      · vip_type=0（普通）→ 同一接口的 list 是 4 KB 的 AES 密文 base64 串。

    密钥不在客户端可得范围（sekey / sign / md5 都试过），所以这条路对普通账号
    是死的。结论：这不是「百度换了接口、要升级客户端」，而是「这个账号等级
    拿不到明文直链」—— 上层应当换池里下一个账号重试，而不是让用户去升级。
    """


# ——————————————————————————————————————————————————————————————
# 底层 HTTP（独立成函数，便于单测 mock）
# ——————————————————————————————————————————————————————————————
def _logid() -> str:
    """百度要求的 logid：base64(32位大写hex + ':FG=1')。"""
    h = binascii.hexlify(os.urandom(16)).decode().upper()
    return urllib.parse.quote(base64.b64encode((h + ":FG=1").encode()).decode())


def common_query() -> str:
    """四个接口共用的查询串（整条链路共用一个 logid，与浏览器行为一致）。"""
    return ("channel=chunlei&web=1&app_id=250528&bdstoken=&"
            f"logid={_logid()}&clienttype=0")


def new_session(referer: str) -> requests.Session:
    """匿名 Session（不注入登录态，见模块文档）。"""
    s = requests.Session()
    s.headers.update({
        "User-Agent": _UA,
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
        # 必须：部分环境下 gzip 响应解压有坑（实测踩过），强制不压缩
        "Accept-Encoding": "identity",
    })
    return s


def _apply_login_cookie(s: requests.Session, cookie: str) -> None:
    """把一整段 Cookie 头逐条塞进 session（域 .baidu.com）。

    不用 `s.headers["Cookie"] = cookie`：那样会把分享页刚种下的 BDCLND（本次
    分享的提取码票据）整个顶掉，实测后续 /share/list 直接 errno=-9。逐条 set
    只加不减，BDCLND 保留 share_verify 刚写进去的新值。

    跳过 cookie 自带的 BDCLND：那是**上一次**分享的票据，串到本次分享上会失效。
    """
    if not cookie:
        return
    for part in cookie.split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not k or k.upper() == "BDCLND":
            continue
        try:
            s.cookies.set(k, v, domain=".baidu.com", path="/")
        except Exception:  # noqa: BLE001 — 个别畸形键名不该拖垮整次解析
            continue


def _json(resp) -> dict:
    """解析百度接口的 JSON。

    百度偶发返回非对象（HTML 风控页、被 gzip 成字符串、空体）——直接交回调用方
    会炸成 `'str' object has no attribute 'get'` 这种看不懂的报错（2026-10-27 线上
    出过），所以这里统一转成人话异常。
    """
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        data = None
    if not isinstance(data, dict):
        raise ValueError("百度网盘接口返回异常（可能触发了风控，或分享链接已失效），请稍后重试")
    return data


# ——————————————————————————————————————————————————————————————
# URL 分类
# ——————————————————————————————————————————————————————————————

_SHARE_URL_RE = re.compile(
    r"https?://(?:pan|yun)\.baidu\.com/[^\s\u4e00-\u9fff\"'<>，。；、）)】]+", re.I)
_SHARE_PWD_RE = re.compile(
    r"(?:提取码|密码|pwd)\s*[:：=]?\s*([0-9A-Za-z]{4})(?![0-9A-Za-z])", re.I)
_URL_TAIL_JUNK = ".,;:!?)）】]」》\"'"


def extract_share_url(text: str) -> tuple[str, str]:
    """从「整段分享文案」里提取 (分享链接, 提取码)。

    用户在百度网盘 App / 网页点「复制链接」，拿到的不是裸 URL，而是多行文案::

        通过百度网盘分享的文件：狂野俄罗斯西伯利亚.mp4
        链接:https://pan.baidu.com/s/1hgu12HSQdxe47CaGb-jx7Q
        提取码:fhh1
        复制这段内容打开「百度网盘APP 即可获取」

    整段粘进输入框是最自然的操作 ⇒ 这里统一做一次规范化，
    前端（web/app.js 的 panExtractShare）保持同样的正则口径。
    """
    raw = (text or "").strip()
    if not raw:
        return "", ""
    m = _SHARE_URL_RE.search(raw)
    if not m:
        return "", ""
    url = m.group(0).rstrip(_URL_TAIL_JUNK)
    pwd = ""
    pm = _SHARE_PWD_RE.search(raw)
    if pm:
        pwd = pm.group(1)
    if not pwd:
        try:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            pwd = (q.get("pwd") or [""])[0]
        except Exception:  # noqa: BLE001
            pwd = ""
    return url, pwd


def parse_baidu_url(url: str) -> dict:
    """把用户输入归类。

    返回 {"kind": "personal"|"share"|"unknown", ...}。
    - share:    {"kind":"share", "surl":"<去前导1>", "raw":"1<surl>", "pwd":...}
                （verify / list 用 surl；tplconfig 用带前导 1 的 raw）
    - personal: {"kind":"personal", "path": "/..."}
    """
    url = (url or "").strip()
    # 整段分享文案（标题 + 「链接:」 + 「提取码:」）先规范化，只留真正的 URL；
    # 提取码同时带出来，供后面的 share 分支兜底使用。
    url, _text_pwd = extract_share_url(url)
    if not url:
        return {"kind": "unknown"}
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)

    if "/share/" in parsed.path or parsed.path.startswith("/s/") or "surl" in query:
        surl = (query.get("surl") or [""])[0]
        if not surl and parsed.path.startswith("/s/"):
            raw = parsed.path[len("/s/"):].strip("/")
            # /s/1xxxx 中前导 1 是固定前缀，真正的 surl 是去掉它的部分
            surl = raw[1:] if raw.startswith("1") else raw
        if not surl:
            return {"kind": "unknown"}
        return {
            "kind": "share",
            "surl": surl,
            "raw": "1" + surl,
            "pwd": ((query.get("pwd") or [""])[0] or _text_pwd),
        }

    frag = parsed.fragment or ""
    path = ""
    m = re.search(r"path=([^&]+)", parsed.query + "&" + frag)
    if m:
        path = urllib.parse.unquote(m.group(1))
    return {"kind": "personal", "path": path or "/"}


# ——————————————————————————————————————————————————————————————
# 分享页 yunData（兜底用：list 响应没给 uk / share_id 时才抓页面）
# ——————————————————————————————————————————————————————————————

def _extract_yun_data(html: str) -> dict:
    """从分享页 HTML 提取 window.yunData 里的分享元数据。

    兼容百度两种页面形态：
    1) 对象字面量：window.yunData = { ... share_uk:"..." ... }  （当前真实页面）
    2) 点赋值：window.yunData.shareid = '...'; window.yunData.uk = '0'; （老页面）
    真正的分享者 uk 在 share_uk（点赋值形态下页面另有个占位的 uk:'0'），统一映射到 uk。
    """
    data: dict = {}
    for key in ("shareid", "share_uk", "uk", "sign", "timestamp", "bdstoken"):
        val = None
        kpat = r"\b" + key if key == "uk" else key   # uk 需词边界，避免误匹配 share_uk
        # 🔴 对象字面量里 key 是带引号的（"share_uk":"1100294005954"），
        #    冒号前必须允许可选的收尾引号 —— 少了 [\"']? 就永远匹配不到（2026-10-23 实测踩坑）
        m = re.search(kpat + r"""\s*["']?\s*:\s*["']?([^"'\s,}]+)""", html)
        if m:
            val = m.group(1)
        if val is None:
            m = re.search(r"yunData\." + key + r"\s*=\s*['\"]?([^'\"\s;}]+)", html)
            if m:
                val = m.group(1)
        if val is None:
            continue
        if key == "uk" and val in (0, "0", ""):
            continue    # 占位假值 uk:'0'
        if key in ("timestamp", "shareid", "share_uk"):
            try:
                val = int(val)
            except ValueError:
                pass
        data[key] = val
    if "share_uk" in data:
        data["uk"] = data["share_uk"]
    return data


def _yun_data_from_page(s: requests.Session, share_url: str) -> dict:
    try:
        resp = s.get(share_url, timeout=_TIMEOUT)
        return _extract_yun_data(resp.text or "")
    except Exception:  # noqa: BLE001
        return {}


# ——————————————————————————————————————————————————————————————
# 新接口四步
# ——————————————————————————————————————————————————————————————

def share_verify(s: requests.Session, surl: str, pwd: str, q: str) -> str:
    """① 校验提取码。成功返回 sekey（URL-decode 后的 randsk）；无提取码返回 ""。

    副作用：百度会往 Session 里种 BDCLND cookie（= sekey），后续接口靠它认会话。
    """
    if not pwd:
        return ""
    url = f"{_BASE}/share/verify?t={int(time.time() * 1000)}&bioc=1&surl={urllib.parse.quote(surl)}&{q}"
    resp = s.post(url, data={"pwd": pwd, "vcode": "", "vcode_str": ""},
                  timeout=_TIMEOUT)
    j = _json(resp)
    if j.get("errno") != 0:
        raise ValueError(_errno_msg(j.get("errno"), "提取码校验失败"))
    randsk = j.get("randsk") or ""
    return urllib.parse.unquote(randsk)


def share_tplconfig(s: requests.Session, raw: str, q: str) -> tuple[str, str]:
    """② 取 sign / timestamp。"""
    url = (f"{_BASE}/share/tplconfig?surl={urllib.parse.quote(raw)}"
           f"&fields=sign,timestamp&view_mode=1&{q}")
    j = _json(s.get(url, timeout=_TIMEOUT))
    if j.get("errno") != 0:
        raise ValueError(_errno_msg(j.get("errno"), "取分享签名失败"))
    data = j.get("data") or {}
    return data.get("sign") or "", str(data.get("timestamp") or "")


def share_list(s: requests.Session, surl: str, q: str) -> tuple[list, str, str]:
    """③ 列文件。返回 (records, uk, share_id)。"""
    url = (f"{_BASE}/share/list?web=5&app_id=250528&desc=1&showempty=0"
           f"&page=1&num=100&order=time&shorturl={urllib.parse.quote(surl)}"
           f"&root=1&view_mode=1&{q}")
    j = _json(s.get(url, timeout=_TIMEOUT))
    if j.get("errno") != 0:
        raise ValueError(_errno_msg(j.get("errno"), "读取分享内容失败"))
    lst = j.get("list")
    if not isinstance(lst, list):
        # 只有遇到被风控改写的响应才会到这里 —— 别让上层拿字符串去 .get()
        logger.warning("[baidu] share/list 非数组: %s", json.dumps(j, ensure_ascii=False)[:300])
        raise ValueError("读取分享内容失败（百度返回了异常数据），请稍后重试")
    records = [it for it in lst if isinstance(it, dict)]
    return (records,
            str(j.get("uk") or ""),
            str(j.get("share_id") or ""))


# ——————————————————————————————————————————————————————————————
# 文件夹分享：进子目录 + 递归展开（2026-10-27 实测）
# ——————————————————————————————————————————————————————————————
# 现实里 90% 的百度分享根节点是一个文件夹，只列根目录等于什么都看不到。
# 子目录列举**不是**根目录那条接口，参数差三处，少一个都 errno=2 / 不生效：
#   ① 必须用 uk + shareid（**不要** shorturl、**不要** root=1）
#   ② dir = 该目录**真实路径**（如 /我的资源/小说/xxx），取自父级记录自带的 path 字段
#   ③ dir 要整体 URL 编码（连斜杠一起编成 %2F），sekey 也要编
# 另需 is_from_web=true；sekey 就是提取码换算出来的 BDCLND。
_SHARE_MAX_FILES = 300     # 单次解析最多返回 300 个文件（足够用，且不会把响应撑爆）
_SHARE_MAX_DIRS = 50       # 最多翻 50 个目录
_SHARE_MAX_DEPTH = 4       # 最多翻 4 层
_SHARE_PAGE_SIZE = 100     # 每页 100 条，单目录最多翻 3 页


def share_list_dir(s: requests.Session, uk: str, share_id: str, sekey: str,
                   dirpath: str, q: str) -> list:
    """列分享内的一个子目录，返回 records（dict 列表）。

    dirpath 用父级记录自带的 `path`；失败返回 []（不抛），
    —— 单个目录读不到不该让整次解析失败，其它目录的文件照样能用。
    """
    if not (uk and share_id and dirpath):
        return []
    sk = f"&sekey={urllib.parse.quote(sekey, safe='')}" if sekey else ""
    out: list = []
    for page in range(1, 4):
        url = (f"{_BASE}/share/list?uk={uk}&shareid={share_id}{sk}&is_from_web=true"
               f"&dir={urllib.parse.quote(dirpath, safe='')}"
               f"&page={page}&num={_SHARE_PAGE_SIZE}"
               f"&order=name&desc=0&showempty=0&view_mode=1&{q}")
        try:
            j = _json(s.get(url, timeout=_TIMEOUT))
        except Exception:  # noqa: BLE001
            logger.warning("[baidu] 列子目录异常 dir=%s", dirpath, exc_info=True)
            break
        if j.get("errno") != 0:
            logger.warning("[baidu] 列子目录失败 dir=%s errno=%s", dirpath, j.get("errno"))
            break
        lst = j.get("list")
        if not isinstance(lst, list):
            break
        out.extend(it for it in lst if isinstance(it, dict))
        if len(lst) < _SHARE_PAGE_SIZE:
            break
    return out


def _share_walk(s: requests.Session, uk: str, share_id: str, sekey: str,
                roots: list, files: list, q: str) -> None:
    """BFS 展开 roots 里的所有目录，把文件追加进 files（元素 = (record, 相对路径)）。

    防炸：目录数 / 层数 / 文件数三重上限；单目录失败只跳过它自己。
    """
    queue = []
    for r in roots:
        name = str(r.get("server_filename") or "")
        dpath = r.get("path") or ""
        queue.append((dpath, name + "/", 1))
    seen = 0
    while queue and seen < _SHARE_MAX_DIRS and len(files) < _SHARE_MAX_FILES:
        dpath, rel, depth = queue.pop(0)
        if depth > _SHARE_MAX_DEPTH:
            continue
        seen += 1
        for k in share_list_dir(s, uk, share_id, sekey, dpath, q):
            kname = str(k.get("server_filename") or k.get("filename") or "")
            if int(k.get("isdir") or 0):
                kpath = k.get("path") or (dpath.rstrip("/") + "/" + kname if dpath else "")
                queue.append((kpath, rel + kname + "/", depth + 1))
            else:
                files.append((k, rel))
            if len(files) >= _SHARE_MAX_FILES:
                break


# 百度 dlink（d.pcs.baidu.com）的 UA 白名单 —— 2026-10-24 实测：
#   带**浏览器 UA** 一律 403 `{"error_code":31326,"error_msg":"user is not authorized"}`；
#   官方客户端 UA / 空 UA / 非浏览器 UA（aria2、requests 默认）都能正常 206。
# 这是百度的防盗链设计（浏览器 UA 不允许直取 PCS 直链），所以下载时必须
# 显式指定客户端 UA，否则用户看到的是「未授权」而不是文件。
DLINK_UA = "netdisk;P2SP;3.0.20.138"

# filemetas 单次 fsids 上限（官方限制 100，超出返回 errno=2）
_FILEMETAS_BATCH = 100


def _sharedownload(s: requests.Session, sign: str, ts: str, uk: str, share_id: str,
                   ids: list, sekey: str, q: str) -> tuple:
    """调 /api/sharedownload，返回 (dlinks, 密文长度)。

    密文长度 > 0 就是百度给的是 AES 密文串 —— 这不是网络问题，是**账号等级**
    问题（普通账号拿不到明文），由调用方决定换号还是报错。
    """
    url = (f"{_BASE}/api/sharedownload?sign={urllib.parse.quote(sign)}"
           f"&timestamp={ts}&{q}")
    body = {
        "product": "share",
        "encrypt": "0",
        "timestamp": "",
        "sign": "",
        "uk": uk,
        "primaryid": share_id,
        "fid_list": json.dumps(ids),
        "path_list": "[]",
        "isPublic": "false",
        "extra": json.dumps({"sekey": sekey}),
    }
    # ⚠️ 必须 form 编码（data=），用 json= 百度回 errno=2
    j = _json(s.post(url, data=body, timeout=_TIMEOUT))
    errno = j.get("errno")
    lst = j.get("list")
    if isinstance(lst, str):
        return {}, len(lst)
    if errno != 0:
        # 抛而不是静默返回 {}：上层要能「换下一个账号再试」，全试完还要把
        # 真实原因（errno + 人话）报给用户，不能被吞成一句「没拿到直链」。
        # 🔧 2026-10-27 修：_errno_msg 自己已拼「（errno=N）」，外层再 "（errno=%s）"
        #    会打成「参数错误（errno=2）（errno=2）」。这条只进日志与运维监控，
        #    上屏一律走 USER_ERROR_TEXT（routers/cloud_drive.py 统一收口）。
        logger.info("[baidu] sharedownload errno=%s → %s",
                    errno, _errno_msg(errno, "获取下载直链失败"))
        raise ValueError(_errno_msg(errno, "获取下载直链失败"))
    if not isinstance(lst, list):
        return {}, 0
    out: dict = {}
    for item in lst:
        if not isinstance(item, dict):     # 防御：偶发夹带非对象条目
            continue
        fid = str(item.get("fs_id") or "")
        if fid and item.get("dlink"):
            out[fid] = item["dlink"]
    return out, 0


def _filemetas(s: requests.Session, uk: str, share_id: str, ids: list, q: str) -> dict:
    """调 /api/filemetas?dlink=1（兜底路径，返回 {fs_id: dlink}）。"""
    out: dict = {}
    for i in range(0, len(ids), _FILEMETAS_BATCH):
        batch = ids[i:i + _FILEMETAS_BATCH]
        url = (
            f"{_BASE}/api/filemetas?dlink=1"
            f"&fsids={urllib.parse.quote(json.dumps(batch))}"
            f"&uk={uk}&shareid={share_id}&type=dlink&{q}"
        )
        try:
            j = _json(s.get(url, timeout=_TIMEOUT))
        except Exception:  # noqa: BLE001  网络抖动 → 下一个批次 / 交给上层
            continue
        if j.get("errno") != 0:
            continue
        for item in j.get("info") or []:
            if not isinstance(item, dict):
                continue
            fid = str(item.get("fs_id") or "")
            if fid and item.get("dlink"):
                out[fid] = item["dlink"]
    return out


def share_dlinks(s: requests.Session, sign: str, ts: str, uk: str, share_id: str,
                 fs_ids: list, sekey: str, q: str) -> dict:
    """④ 换直链。返回 {fs_id: dlink}；拿不到返回 {}，普通账号则抛 NeedMemberLogin。

    顺序（2026-10-27 重排，依据同一链接 × 三个账号的交叉实测）：

    ① **/api/sharedownload —— 主路径**
       会员账号（vip_type>0）返回明文数组，元素直接带 dlink，一次可批全部 fsid。
       ⚠️ 这一步**必须带登录态**：匿名会话拿到的是游客缓存的旧链（1 小时过期，
          下载必报 31360），普通账号拿到的是 AES 密文串。

    ② **/api/filemetas?dlink=1 —— 兜底**
       2026-10-24 曾作主路径；现在对分享文件普遍返回 `errno=0, info=[]`（与
       账号等级无关），所以降为兜底。留着成本只有一次请求，且万一吃到转存场景
       还能给链。

    旧结论「sharedownload 对谁都返回密文」是错的：当时池里只有普通账号，
    把「账号等级」误判成了「百度换接口」。
    """
    ids = [int(f) for f in fs_ids if str(f).isdigit()]
    if not ids:
        return {}

    out, cipher_len = _sharedownload(s, sign, ts, uk, share_id, ids, sekey, q)
    if out:
        return out

    out = _filemetas(s, uk, share_id, ids, q)
    if out:
        return out

    if cipher_len:
        # 真实原因只进日志（前端统一 USER_ERROR_TEXT）。样本：vip_type=2 给明文，
        # vip_type=0/1 都回密文 —— 所以「会员」不够，得是超会。
        logger.warning("[baidu] 换直链只拿到密文：%d 字节（该账号等级不够，非超会）", cipher_len)
        raise NeedMemberLogin(USER_ERROR_TEXT)
    return out


# ——————————————————————————————————————————————————————————————
# 对外入口
# ——————————————————————————————————————————————————————————————

def _has_baidu_login(cookie: str) -> bool:
    """判断 Cookie 头里是否含**百度网盘**的有效登录凭证。

    只认 BDUSS。BAIDUID / BIDUPSID / PSTM / H_PS_PSSID 这些是百度搜索侧的
    匿名标识，任何访客都有，跟网盘登录无关。2026-10-24 实测：把一份
    「只有百度搜索匿名 Cookie」的头传给 sharedownload，百度照样返回加密 list
    （`list` 字段是 4 KB 的密文而不是数组），于是只能报「请填 BDUSS」。
    所以判定必须严格到 BDUSS —— 否则会自动读出来的匿名 Cookie 冒充登录态，
    把「缺登录」这条可读提示盖成一句莫名的解密失败。
    """
    return bool(cookie) and "BDUSS=" in cookie


def _bduss_of(cookie: str) -> str:
    """取出 BDUSS 值（用于候选去重）。"""
    for part in (cookie or "").split(";"):
        k, _, v = part.partition("=")
        if k.strip() == "BDUSS":
            return v.strip()
    return ""


def _stoken_of(cookie: str) -> str:
    """取出 STOKEN 值（百度网盘的会话票，同账号可有多个）。"""
    for part in (cookie or "").split(";"):
        k, _, v = part.partition("=")
        if k.strip() == "STOKEN":
            return v.strip()
    return ""


def _cookie_candidates(login_cookie: str, extra: Optional[list] = None) -> list:
    """合并候选登录态：只留含 BDUSS 的，按 BDUSS+STOKEN 去重（首选在前）。

    🔴 不能只按 BDUSS 去重（2026-10-28 实测）：同一账号的两个 STOKEN 快照，
    换直链结果可能一个明文一个密文 —— 只留第一条等于赌运气。STOKEN 不同就是
    不同会话，必须都作为候选逐个试。
    """
    out: list = []
    seen: set = set()
    for c in [login_cookie] + list(extra or []):
        if not _has_baidu_login(c):
            continue
        key = _bduss_of(c)
        if not key:
            continue
        key = key + "|" + _stoken_of(c)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


_PERSONAL_MAX_FILES = 300      # 个人网盘单次最多展开多少个文件
_PERSONAL_MAX_DEPTH = 3        # 目录最多翻 3 层
_PERSONAL_PAGE_SIZE = 100


def _personal_list(s: requests.Session, dirpath: str, q: str) -> list:
    """列个人网盘目录（**必须带登录态**，匿名回 errno=-6）。

    与分享的 /share/list 完全不同的接口：参数是 dir + limit，没有 uk / shareid。
    """
    j = _json(s.get(f"{_BASE}/api/list", params={
        "dir": dirpath or "/", "order": "time", "desc": "1",
        "limit": str(_PERSONAL_PAGE_SIZE), "web": "1",
        "clienttype": "0", "app_id": "250528",
    }, timeout=_TIMEOUT))
    errno = j.get("errno")
    if errno not in (0, None):
        raise ValueError(_errno_msg(errno, "读取个人网盘目录失败"))
    return [it for it in (j.get("list") or []) if isinstance(it, dict)]


def _personal_metas(s: requests.Session, paths: list, q: str) -> dict:
    """个人网盘：路径列表 → {path: (dlink, size)}。

    🔴 2026-10-28 实测：这是**唯一**对非超会账号也给明文的取链路径。
       分享接口（/api/sharedownload）对 vip_type=0/1 一律回 4KB 密文，
       但对**自己名下**的文件，/api/filemetas?dlink=1 返回的 dlink 是明文
       （298~400 字符），与账号等级无关。⇒ 「转存 / 本来就在自己网盘」就能下。
    """
    out: dict = {}
    batch_size = 50
    for i in range(0, len(paths), batch_size):
        batch = [p for p in paths[i:i + batch_size] if p]
        if not batch:
            continue
        url = (f"{_BASE}/api/filemetas?dlink=1"
               f"&target={urllib.parse.quote(json.dumps(batch, ensure_ascii=False))}"
               f"&type=dlink&{q}")
        try:
            j = _json(s.get(url, timeout=_TIMEOUT))
        except Exception as e:  # noqa: BLE001
            logger.info("[baidu] 个人网盘 filemetas 异常：%r", e)
            continue
        errno = j.get("errno")
        if errno not in (0, None):
            logger.info("[baidu] 个人网盘 filemetas errno=%s → %s",
                        errno, _errno_msg(errno, "取个人网盘直链失败"))
            continue
        for item in j.get("info") or []:
            if not isinstance(item, dict):
                continue
            p = item.get("path") or ""
            if p and item.get("dlink"):
                try:
                    size = int(item.get("size") or 0)
                except (TypeError, ValueError):
                    size = 0
                out[p] = (item["dlink"], size)
    return out


def _personal_walk(s: requests.Session, root: str, q: str,
                   depth: int = 0, out: Optional[list] = None) -> list:
    """递归展开个人网盘目录，返回文件记录列表（带 path 字段）。"""
    out = out if out is not None else []
    try:
        items = _personal_list(s, root, q)
    except ValueError as e:
        logger.info("[baidu] 个人网盘列目录跳过 %s：%s", (root or "")[:80], e)
        return out
    for it in items:
        if len(out) >= _PERSONAL_MAX_FILES:
            return out
        if int(it.get("isdir") or 0):
            if depth < _PERSONAL_MAX_DEPTH:
                _personal_walk(s, it.get("path") or root, q, depth + 1, out)
        else:
            out.append(it)
    return out


def _personal_files(s: requests.Session, cookie: str, path: str, q: str) -> list:
    """个人网盘路径（文件或目录）→ CloudFile 列表（明文直链）。"""
    p = (path or "/").strip() or "/"
    metas = _personal_metas(s, [p], q)
    hit = metas.get(p)
    if hit and hit[0]:
        return [CloudFile(name=os.path.basename(p.rstrip("/")) or p,
                          size=hit[1], dlink=hit[0], cookie=cookie,
                          headers={"User-Agent": DLINK_UA})]

    items = _personal_walk(s, p, q)
    if not items:
        return []
    paths = [it.get("path") for it in items if it.get("path")]
    metas = _personal_metas(s, paths, q)
    files: list = []
    for it in items:
        path_i = it.get("path") or ""
        m = metas.get(path_i)
        if not m or not m[0]:
            continue
        rel = "/".join(path_i.split("/")[1:-1])
        files.append(CloudFile(
            name=it.get("server_filename") or "file",
            size=m[1] or int(it.get("size") or 0),
            dlink=m[0], cookie=cookie,
            headers={"User-Agent": DLINK_UA},
            path=rel,
        ))
    return files


def resolve_personal(path: str, cookie: str = "",
                     extra_cookies: Optional[list] = None) -> list[CloudFile]:
    """个人网盘路径 / 文件夹 → 明文直链列表。

    2026-10-28 由「阶段 2 再做」的空壳改成可用实现：个人网盘走
    /api/list + /api/filemetas?dlink=1，**不需要超会**也拿得到明文 dlink。
    这条路同时是分享链路的兜底（见 _reuse_from_own_netdisk）。
    """
    cands = _cookie_candidates(cookie, extra_cookies)
    if not cands:
        logger.warning("[baidu] 收到个人网盘路径但没有任何百度登录态：%s",
                       (path or "")[:120])
        raise ValueError(USER_ERROR_TEXT)

    q = common_query()
    for ck in cands:
        s = new_session(f"{_BASE}/disk/home")
        _apply_login_cookie(s, ck)
        try:
            files = _personal_files(s, ck, path, q)
        except ValueError as e:
            logger.info("[baidu] 个人网盘解析失败（换下一个账号）：%s", e)
            continue
        if files:
            logger.info("[baidu] 个人网盘 \u2192 %d 个文件（明文直链）", len(files))
            return files
    logger.warning("[baidu] 个人网盘路径解析失败（已试 %d 个账号）：%s",
                   len(cands), (path or "")[:120])
    raise ValueError(USER_ERROR_TEXT)


_OWN_INDEX_MAX_FILES = 5000     # 建索引最多扫多少个条目（实测 2 万条要 137s，太慢）
_OWN_INDEX_MAX_DEPTH = 6        # 目录最多翻 6 层
_OWN_INDEX_PAGE = 1000          # /api/list 单页条数
_OWN_INDEX_DEADLINE = 25.0      # 建索引总耗时上限（秒），超了就用已扫到的部分
_REUSE_MAX_ACCOUNTS = 3         # 复用探测最多试几个账号（每个都要扫一遍网盘）


def _own_scan_dir(s: requests.Session, dirpath: str, q: str,
                  budget: int, seen: int, idx: dict, pending: set = None) -> tuple:
    """扫描单个目录（自翻页），文件写进 idx；返回 (已扫条目数, 子目录列表)。

    必须自己 `start += len(items)` 翻页：/api/list 默认只回 100 条，目录里文件
    超过一页就会漏 —— `_personal_walk` 是不翻页的，超过 100 个文件的目录只看得到
    前 100 个。

    `order=time&desc=1`：用户「网盘里已有的文件」绝大多数是刚转存/刚保存的，
    按时间倒序能把要命中的那几个排在最前面。

    `pending` 是还在找的文件名集合，命中一个就移出去 —— 全空时调用方提前收工，
    不用把整个网盘扫完。
    """
    dirs: list = []
    start = 0
    while seen < budget:
        try:
            j = _json(s.get(f"{_BASE}/api/list", params={
                "dir": dirpath or "/", "order": "time", "desc": "1",
                "start": str(start), "limit": str(_OWN_INDEX_PAGE), "web": "1",
                "clienttype": "0", "app_id": "250528",
            }, timeout=_TIMEOUT))
        except Exception as e:  # noqa: BLE001
            logger.info("[baidu] 自己网盘列目录异常 %s：%r", (dirpath or "/")[:80], e)
            return seen, dirs
        errno = j.get("errno")
        if errno not in (0, None):
            logger.info("[baidu] 自己网盘列目录 errno=%s → %s（%s）", errno,
                        _errno_msg(errno, "读取个人网盘目录失败"), (dirpath or "/")[:80])
            return seen, dirs
        items = [it for it in (j.get("list") or []) if isinstance(it, dict)]
        if not items:
            return seen, dirs
        for it in items:
            seen += 1
            if seen > budget:
                break
            p = it.get("path") or ""
            if not p:
                continue
            if int(it.get("isdir") or 0):
                dirs.append(p)
                continue
            name = it.get("server_filename") or ""
            if not name:
                continue
            try:
                size = int(it.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            idx["exact"][(name, size)] = p
            idx["byname"].setdefault(name, []).append((size, p))
            if pending is not None:
                pending.discard(name)
        start += len(items)
        if len(items) < _OWN_INDEX_PAGE:
            return seen, dirs
    return seen, dirs


def _own_file_index(s: requests.Session, q: str, want: Optional[set] = None,
                    budget: int = 0, deadline: float = 0.0) -> dict:
    """自己网盘索引 → {"exact": {(名,大小): path}, "byname": {名: [(大小,path)]}}。

    🔴 2026-10-30 实测：**不要用 /api/search**。5 种参数变体（含补 bdstoken、去掉
       recursion、用完整文件名当 wd）在该账号上一律回 `errno=2`，一条都命中不了 ——
       旧实现（search + recursion）等于永不命中，日志永远落到最后一句「自己网盘里
       也没有同名文件」，用户看到的就是「我网盘里明明有这片子，它说没有」。
       改走 /api/list 逐层翻页自建索引，这是唯一能用的路。

    want: 要找的文件名集合。传了就在扫描过程中边扫边对，**全命中立刻收工** ——
          常见场景（文件刚保存、在根目录）只需一两次请求。
    deadline: 总耗时上限，超时返回已扫到的部分（如实记日志，不假装扫全了）。
    """
    budget = budget or _OWN_INDEX_MAX_FILES
    deadline = deadline or _OWN_INDEX_DEADLINE
    idx: dict = {"exact": {}, "byname": {}}
    pending = set(n for n in (want or ()) if n) or None
    seen = 0
    level = [""]                      # 本轮待扫目录（广度优先）
    depth = 0
    t0 = time.time()
    done_reason = "扫完"
    while level and seen < budget and depth <= _OWN_INDEX_MAX_DEPTH:
        if pending is not None and not pending:
            done_reason = "全命中"
            break
        if time.time() - t0 > deadline:
            done_reason = "超时"
            break
        nxt: list = []
        for d in level:
            if seen >= budget or time.time() - t0 > deadline:
                break
            seen, dirs = _own_scan_dir(s, d, q, budget, seen, idx, pending)
            nxt.extend(dirs)
        level = nxt
        depth += 1
    logger.info("[baidu] 自己网盘索引：%s（扫 %d 项 / 文件 %d 个 / %.1fs%s）",
                done_reason, seen, len(idx["exact"]), time.time() - t0,
                "，仍在找 %d 个" % len(pending) if pending else "")
    return idx


def _find_own_path(index: dict, name: str, size: int) -> str:
    """在已建好的自己网盘索引里按文件名（+ 大小）找一条路径；找不到返回 ""。

    大小未知（分享页没给 size）时退化为按名匹配；同名多个则取第一个。
    """
    if not name or not isinstance(index, dict):
        return ""
    if size:
        hit = (index.get("exact") or {}).get((name, size))
        if hit:
            return hit
    for fsize, p in (index.get("byname") or {}).get(name) or []:
        if not size or not fsize or fsize == size:
            return p
    return ""


def _reuse_from_own_netdisk(files: list, cands: list, q: str) -> tuple:
    """分享换不到明文时，尝试复用自己网盘里同名同大小的文件。

    返回 ({fs_id: dlink}, 使用的 cookie)。全程只读：不转存、不写盘、不占额度。
    """
    # 先把「要找什么」摊平，索引才能边扫边对、命中即收工
    want: set = set()
    rows: list = []
    for rec, _rel in files:
        fid = str(rec.get("fs_id") or "")
        if not fid:
            continue
        name = rec.get("server_filename") or rec.get("filename") or ""
        try:
            size = int(rec.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if name:
            want.add(name)
        rows.append((fid, name, size))
    if not rows:
        return {}, ""

    for ck in cands[:_REUSE_MAX_ACCOUNTS]:
        s = new_session(f"{_BASE}/disk/home")
        _apply_login_cookie(s, ck)
        index = _own_file_index(s, q, want=want)
        if not index["exact"]:
            continue                  # 这个号个人网盘读不出文件，换下一个
        hits: dict = {}               # fs_id -> 自己网盘里的路径
        for fid, name, size in rows:
            own = _find_own_path(index, name, size)
            if own:
                hits[fid] = own
        if not hits:
            continue
        # 批量取直链：/api/filemetas 支持一次 50 条，别一个文件发一次请求
        metas = _personal_metas(s, list(set(hits.values())), q)
        out: dict = {}
        for fid, own in hits.items():
            m = metas.get(own)
            if m and m[0]:
                out[fid] = m[0]
                logger.info("[baidu] 自己网盘已有同名文件，直接复用明文直链：%s", own[:100])
        if out:
            return out, ck
    return {}, ""


def _share_page_missing(s: requests.Session, share_url: str) -> bool:
    """分享页是否直接 404（链接被取消 / 过期 / 压根不存在）。

    早一步判掉比让后面的 verify 返回 errno=105 好得多：105 会显示成「提取码校验
    失败」，用户会以为是自己提取码打错了，实际是链接没了（2026-10-27 线上踩过）。
    """
    try:
        r = s.get(share_url, timeout=_TIMEOUT)
    except Exception:  # noqa: BLE001
        return False
    if r.status_code == 200:
        return False
    # 错误页不带 charset，requests 会按 latin-1 猜 ⇒ 中文变乱码，必须自己按 utf-8 解
    try:
        body = (r.content or b"").decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        body = ""
    if r.status_code == 404:
        return "yunData" not in body
    return ("页面不存在" in body or "分享的文件已经被取消" in body) and "yunData" not in body


def resolve_share(url: str, login_cookie: str = "", pwd: str = "",
                  extra_cookies: Optional[list] = None) -> list[CloudFile]:
    """分享链接 → 直链列表。

    login_cookie：首选百度登录态（完整 Cookie 头）。
    extra_cookies：备选登录态（池里其他账号）。百度只给会员账号明文直链，
      所以换直链这一步要能按序换号（见函数内的候选循环）。

    没登录态也能拿到**文件清单**（verify / list 不需要登录），只是换不到直链，
    这时返回的 CloudFile.need_login=True，前端据此提示去配凭据。
    """
    p = parse_baidu_url(url)
    if p.get("kind") != "share" or not p.get("surl"):
        raise ValueError("不是有效的百度网盘分享链接")
    surl = p["surl"]
    raw = p.get("raw") or ("1" + surl)
    pwd = pwd or p.get("pwd") or ""
    share_url = f"{_BASE}/s/{raw}"

    q = common_query()
    s = new_session(share_url)

    if _share_page_missing(s, share_url):
        raise ValueError("分享链接不存在或已失效 —— 请在百度网盘里重新生成分享链接再粘贴")

    sekey = share_verify(s, surl, pwd, q)
    if not sekey:
        # 有些分享百度把 sekey 只种在 cookie 里
        sekey = urllib.parse.unquote(s.cookies.get("BDCLND", "") or "")

    sign, ts = share_tplconfig(s, raw, q)
    records, uk, share_id = share_list(s, surl, q)
    if not records:
        raise ValueError("分享内容为空，或链接已失效 / 需要提取码")
    if not (uk and share_id):
        yun = _yun_data_from_page(s, share_url)
        uk = uk or str(yun.get("uk") or "")
        share_id = share_id or str(yun.get("shareid") or "")

    # 根节点常常就是一个文件夹（现实里的分享 90% 如此）⇒ 递归展开，
    # 每个文件带上它相对分享根的目录，前端才能区分「言情/同名.txt」这类重名。
    picked: list = [(r, "") for r in records if int(r.get("isdir") or 0) == 0]
    roots = [r for r in records if int(r.get("isdir") or 0)]
    if roots:
        _share_walk(s, uk, share_id, sekey, roots, picked, q)

    files: list = []
    _seen: set = set()
    for rec, rel in picked:            # 去重：多目录翻页偶发重复回同一条
        fid_key = str(rec.get("fs_id") or "")
        if not fid_key or fid_key in _seen:
            continue
        _seen.add(fid_key)
        files.append((rec, rel))
        if len(files) >= _SHARE_MAX_FILES:
            break
    if not files:
        raise ValueError("分享里没有可下载的文件（可能只有空文件夹）")

    fs_ids = [r.get("fs_id") for r, _rel in files if r.get("fs_id")]
    # 🔴🔴 换直链必须用**带登录态的会话**（2026-10-24 实测）：
    # 用匿名会话请求 /api/sharedownload，百度会返回「游客缓存」里的旧直链 ——
    # dstime 停在一小时前、expires=1h，一拿到就过期，下载必报
    # 31360 expire time out。换成带登录态的会话，同一个分享返回的是
    # dstime=now、expires=8h 的活链，下载 206 正常（SVIP 实测 3.6 MB/s）。
    # ⌁ 注意只有**这一步**要登录态：verify / tplconfig / list 仍必须匿名，
    #   带登录态请求 tplconfig 会返回 HTML（踩过）。
    # 🔴🔴 换直链必须用**会员账号的会话**（2026-10-27 实测，同一链接 × 三个账号）：
    #   · 匿名会话  → 游客缓存里的旧链（1 小时过期，下载必报 31360）；
    #   · 普通账号  → /api/sharedownload 直接回 AES 密文串，根本拿不到 dlink；
    #   · 会员账号  → 明文数组，dlink 有效期 8 小时。
    # 所以这里按「候选账号」逐个试，第一个给出明文直链的胜出。只试一条会随机踩到
    # 普通号 —— 线上「百度这次没返回可用的下载直链（返回了加密数据）」的真因。
    cands = _cookie_candidates(login_cookie, extra_cookies)
    login_ok = bool(cands)
    dlinks: dict = {}
    used_cookie = ""
    last_err: Optional[Exception] = None
    for ck in (cands or [""]):
        s_dl = new_session(share_url)
        if ck:
            _apply_login_cookie(s_dl, ck)
        try:
            got = share_dlinks(s_dl, sign, ts, uk, share_id, fs_ids, sekey, q)
        except NeedMemberLogin as e:
            last_err = e
            logger.info("[baidu] 该账号非会员，换下一个：%s", e)
            continue
        except ValueError as e:
            last_err = e
            logger.info("[baidu] 换直链失败，换下一个：%s", e)
            continue
        if got:
            dlinks = got
            used_cookie = ck
            break

    if not dlinks:
        if not login_ok:
            # 完全没有登录态（匿名 / 只有百度搜索匿名 Cookie）：不当作致命错误，
            # 把「文件清单 + need_login」返回给前端，让用户看到「有什么文件、
            # 缺什么才能下」，而不是一个空白面板半句英文报错。
            dlinks = {}
        elif isinstance(last_err, NeedMemberLogin):
            # 真实原因（试了几个号、都拿密文）只进日志；上屏统一 USER_ERROR_TEXT。
            logger.warning(
                "[baidu] 换直链失败：已试 %d 个百度账号，全部只拿到密文"
                "（百度仅对超级会员 vip_type=2 返回明文 dlink）", max(1, len(cands)))
            # 🔴 2026-10-28 兜底：非超会账号换不到**分享**直链，但**自己网盘**里
            #    同名同大小的文件给的是明文 dlink。命中就直接复用 —— 只读、不转存、
            #    不占额度，也不需要超会。
            dlinks, reused_ck = _reuse_from_own_netdisk(files, cands, q)
            if dlinks:
                used_cookie = reused_ck
                logger.info("[baidu] 改用自己网盘的明文直链：%d 个文件", len(dlinks))
            else:
                logger.info("[baidu] 自己网盘里也没有同名文件；如需下载请先转存到自己网盘")
                raise NeedMemberLogin(USER_ERROR_TEXT)
        elif last_err is not None:
            # 有登录态但接口真的报错（errno≠0 / 风控）：把原始语义抛给用户，
            # 不要退化成「列表有了、下载按钮全是灰的」这种猜谜状态。
            raise last_err
        else:
            dlinks = {}

    # 批量漏掉的逐个补一次（百度偶发只回一部分）
    missing = [f for f in fs_ids if str(f) not in dlinks]
    if missing and used_cookie:
        s_dl = new_session(share_url)
        _apply_login_cookie(s_dl, used_cookie)
        for fid in missing:
            try:
                dlinks.update(share_dlinks(s_dl, sign, ts, uk, share_id, [fid], sekey, q))
            except Exception:  # noqa: BLE001
                continue

    results: list[CloudFile] = []
    for r, rel in files:
        fid = str(r.get("fs_id") or "")
        dlink = dlinks.get(fid) or ""
        if not dlink and used_cookie:
            continue     # 有登录态却拿不到直链 = 这条真失败（最后统一报错）
        try:
            size = int(r.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        results.append(CloudFile(
            name=r.get("server_filename") or r.get("filename") or "file",
            size=size,
            dlink=dlink,
            cookie=used_cookie or "",
            headers={"User-Agent": DLINK_UA},
            need_login=not used_cookie,
            path=rel,
        ))
    if not results:
        raise ValueError("未能从分享链接取到下载直链")
    return results


def resolve(url: str, login_cookie: str = "", pwd: str = "",
            extra_cookies: Optional[list] = None) -> list[CloudFile]:
    """根据链接类型分发。"""
    p = parse_baidu_url(url)
    kind = p.get("kind")
    if kind == "share":
        return resolve_share(url, login_cookie, pwd, extra_cookies)
    if kind == "personal":
        return resolve_personal(p.get("path", "/"), login_cookie, extra_cookies)
    raise ValueError("无法识别的百度网盘链接")
