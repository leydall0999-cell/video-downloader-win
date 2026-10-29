"""yt-dlp 封装层：解析视频信息 + 执行下载并回报进度。"""

from __future__ import annotations

import logging
import os
import re
import glob
import shutil
import sqlite3
import subprocess
import sys
import json
import base64
import random
import time
import threading
from pathlib import Path
from typing import Any

# 在 import yt_dlp 之前加载本地自定义提取器插件（如 chrqj.com）。
# yt-dlp 会在自身导入时扫描 sys.path 上的 yt_dlp_plugins 包并自动注册其中的 IE。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
try:
    import yt_dlp_plugins  # noqa: F401  (触发插件自动注册)
except ImportError:
    pass

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError, GeoRestrictedError, UnsupportedError

from platforms import LinkError, is_china_host, SUPPORTED_PLATFORMS
from tasks import DownloadTask, TaskStore
import socket
import urllib.request
import tempfile

try:
    import requests as _requests
except Exception:  # pragma: no cover
    _requests = None  # type: ignore[assignment]

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7
    _CRYPTO_OK = True
except Exception:  # pragma: no cover
    Cipher = algorithms = modes = PKCS7 = None  # type: ignore[assignment]
    _CRYPTO_OK = False

def _cookie_diag(key: str, value: str = "") -> None:
    """写 Cookie 诊断日志到临时文件（打包后可读，不影响正常运行）。"""
    try:
        path = os.path.join(tempfile.gettempdir(), "vdl_cookie_diag.txt")
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        with open(path, "a") as f:
            f.write(f"[{ts}] {key}={value}\n")
    except Exception:
        pass
from urllib.parse import urlparse, parse_qs, parse_qsl, urlencode

logger = logging.getLogger(__name__)

SOCKET_TIMEOUT = 30  # 国内 CDN 偶发慢响应，30 秒更稳
# 2026-09-26 实测：用户 VPN 代理节点出海质量差（同一时刻 curl 10/10 成、requests 0/5 挂，
# yt-dlp 连续 502 Tunnel/SSL EOF）——每请求约一半概率失败。PROBE_RETRIES=1 时解析
# 要连发 ~5 个请求，全活的概率太低。提到 3：P(单请求 4 连挂)=6%，解析可穿越代理抖动。
# 代价：最坏 4 次重试 × 30s socket_timeout，由路由层 wait_for 超时兜底（YouTube 110s）。
PROBE_RETRIES = 3
# 下载健壮性：防止站点/CDN 假死导致任务永久挂起、占满并发槽拖垮后续任务
# 1) 下载阶段：已开始下分片但 N 秒无字节增量 → 判定停滞，自动终止
# 2) 整体硬上限：解析+下载任意阶段超过此秒数 → 强制结束（兜底；腾讯等限速站常需更久）
DOWNLOAD_STALL_TIMEOUT = int(os.environ.get("VDL_DOWNLOAD_STALL_TIMEOUT", "180"))
# 腾讯等站按 IP/单连接限速（实测 ~16KB/s），1800s 仅够下 29MB，故放宽到 7200s 兜底
DOWNLOAD_HARD_TIMEOUT = int(os.environ.get("VDL_DOWNLOAD_HARD_TIMEOUT", "7200"))
WATCHDOG_POLL = int(os.environ.get("VDL_WATCHDOG_POLL", "5"))  # 看门狗轮询间隔（秒）
# 暂停标志在「无进度回调阶段」（合并/后处理/socket 停滞）的兜底宽限：暂停/取消都只挂在
# yt-dlp 进度钩子上，而这些阶段一条钩子都不发 ⇒ 标志永远没人消费。取消有主等待循环兜底，
# 暂停此前没有 ⇒ 任务永久卡在 pausing（2026-10-27 用户实测：抖音分离轨下载中点暂停）。
# 宽限超过该秒数仍没落地，就由主等待循环就地置 paused。
PAUSE_FORCE_LAND_SECONDS = int(os.environ.get("VDL_PAUSE_FORCE_LAND", "30"))

def _macos_system_proxy() -> str:
    """读取 macOS 系统代理设置（系统偏好里开启的那个），返回 yt-dlp 可用的代理 URL。

    优先顺序：HTTPS 代理 > HTTP 代理 > SOCKS 代理。macOS 里这些通常都是一个
    HTTP CONNECT 代理，所以用 http:// 形式返回。
    """
    try:
        out = subprocess.run(
            ["scutil", "--proxy"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    values: dict[str, str] = {}
    for line in out.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        values[key.strip()] = val.strip()
    host = values.get("HTTPSProxy") or values.get("HTTPProxy")
    port = values.get("HTTPSPort") or values.get("HTTPPort")
    if host and port:
        return f"http://{host}:{port}"
    socks_host = values.get("SOCKSProxy")
    socks_port = values.get("SOCKSPort")
    if socks_host and socks_port:
        return f"socks5://{socks_host}:{socks_port}"
    return ""

# 常见本地代理端口（仅 macOS 兜底扫描用）：GUI 应用读不到 shell 代理时启用
_PROXY_PORTS = (
    (7890, "http"), (7891, "socks"), (7892, "http"), (7893, "socks"),
    (10808, "http"), (10809, "socks"), (6152, "http"), (6153, "socks"),
    (1079, "http"), (1080, "socks"), (1081, "socks"), (8888, "http"),
)
_PROXY_PROBE_CACHE: str | None = None  # None=未探测, ""=无命中, str=代理串

def _probe_local_proxy_ports() -> str:
    """扫描 127.0.0.1 上的常见代理端口，命中监听的第一个即返回 yt-dlp 代理串。

    仅作兜底：当 scutil 未配置系统代理、但本机确在跑 Clash/V2Ray/Surge 等时启用。
    双击 .app 是 GUI 进程、不继承终端 http_proxy，靠此兜底避免 YouTube 直连 403。
    结果缓存到模块级变量，避免每次 YouTube 解析都重扫（约 3s 开销）。
    """
    global _PROXY_PROBE_CACHE
    if _PROXY_PROBE_CACHE is not None:
        return _PROXY_PROBE_CACHE
    for port, kind in _PROXY_PORTS:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.2)
        try:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                _PROXY_PROBE_CACHE = (
                    f"socks5://127.0.0.1:{port}" if kind == "socks"
                    else f"http://127.0.0.1:{port}"
                )
                return _PROXY_PROBE_CACHE
        except OSError:
            pass
        finally:
            s.close()
    _PROXY_PROBE_CACHE = ""
    return ""

def _host_of(url: str) -> str:
    """从链接里取出主机名（去掉 www./m. 前缀），解析失败返回空串。"""
    try:
        host = (urlparse(url).hostname or "").lower()
        return host.removeprefix("www.").removeprefix("m.")
    except ValueError:
        return ""

# B站 URL 归一化：统一转为 www.bilibili.com/video/BVxxx 长链。
#
# 历史策略曾把长链转 b23.tv 短链，因为无 Cookie 时长链被 B站 412 风控；但网页端
# (Railway) 必须使用登录态 Cookie，而 yt-dlp 的 BiliBili 提取器只识别 bilibili.com
# 长链，对 b23.tv 随机短码会退化为 [generic] 提取器，无法注入 Cookie/Referer，
# 导致代理 IP 下仍 412。因此新策略：所有 B站 链接统一归一化为 bilibili.com 长链，
# 由 BiliBili 提取器处理；对 b23.tv 短码先用 HEAD 请求展开真实长链。
_BILIBILI_LONG_URL_RE = re.compile(r"https?://(?:www\.|m\.)?bilibili\.com/video/(BV[0-9A-Za-z]+)")
_BILIBILI_SHORT_BV_RE = re.compile(r"https?://(?:www\.|m\.)?b23\.tv/(BV[0-9A-Za-z]+)")
# 根路径 BV（无 /video/ 前缀，B站 在某些分享场景下会给出此形态）：
# yt-dlp 原生正则只认 /video/ 前缀，此形态会退化成 generic 提取器导致 403；
# 必须归一化为标准 /video/BVxxx 长链，API 兜底正则也依赖 /video/ 形态。
_BILIBILI_ROOT_BV_RE = re.compile(r"https?://(?:www\.)?bilibili\.com/(BV[0-9A-Za-z]+)")

def _expand_b23tv_url(url: str, proxy: str = "") -> str:
    """把 b23.tv 随机短码短链展开为真实 bilibili.com 长链。

    yt-dlp 对 b23.tv/xxxxx 随机短码会使用 [generic] 提取器，无法按 B站 逻辑注入
    Cookie/Referer/UA；在代理 IP 风控较严时会被 412 拦截。b23.tv 会返回 302
    Location，指向真实 www.bilibili.com/video/BVxxx 长链，后续即可交给
    BiliBili 提取器处理。

    实现策略：
    1) 优先 HEAD 取 Location（不下载 body，最省带宽）；
    2) HEAD 失败/无有效 Location 时回退 GET + allow_redirects=True，取最终 URL；
    3) 兼容相对 Location；
    4) 全链路记录日志，便于线上排查。
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in ("b23.tv", "www.b23.tv", "m.b23.tv"):
        return url
    # 已经是 /BVxxx 的短链可以直接推导成长链，不必发请求；保留 p/t 参数
    m = _BILIBILI_SHORT_BV_RE.match(url)
    if m:
        kept = [(k, v) for k, v in parse_qsl(parsed.query) if k in ("p", "t")]
        query = urlencode(kept)
        return f"https://www.bilibili.com/video/{m.group(1)}" + (f"?{query}" if query else "")

    try:
        import requests
    except Exception as e:
        logger.warning("[b23.tv expand] requests not available: %s", e)
        return url

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://b23.tv/",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    timeout = int(os.environ.get("VDL_B23TV_EXPAND_TIMEOUT", "15"))

    def _is_bili_location(location: str) -> bool:
        return bool(location) and "bilibili.com" in location

    def _abs_location(resp) -> str:
        loc = resp.headers.get("Location") or ""
        if loc and not loc.startswith(("http://", "https://")):
            # 相对 Location：按 RFC 3986 拼成绝对 URL
            from urllib.parse import urljoin
            loc = urljoin(resp.url, loc)
        return loc

    # 1) 先尝试 HEAD（轻量）
    try:
        resp = requests.head(
            url,
            headers=headers,
            proxies=proxies,
            timeout=timeout,
            allow_redirects=False,
        )
        expanded = _abs_location(resp)
        if resp.status_code in (301, 302, 307, 308) and _is_bili_location(expanded):
            logger.info("[b23.tv expand] HEAD %s -> %s", url, expanded)
            return expanded
        logger.info(
            "[b23.tv expand] HEAD %s status=%s location=%s",
            url,
            resp.status_code,
            expanded[:200],
        )
    except Exception as e:
        logger.info("[b23.tv expand] HEAD %s failed: %s", url, str(e)[:200])

    # 2) 回退 GET + 自动跟随重定向（某些环境 HEAD 被 CDN 丢弃/不返回 Location）
    try:
        resp = requests.get(
            url,
            headers=headers,
            proxies=proxies,
            timeout=timeout,
            allow_redirects=True,
            stream=True,
        )
        final_url = resp.url
        # b23.tv 随机短码有时会返回 200 HTML 页面（前端 JS 跳转），
        # 此时 resp.url 仍是 b23.tv 自身，需从 body 解析真实 bilibili.com 链接兜底。
        if not _is_bili_location(final_url):
            try:
                body = resp.text
                m = re.search(r"https?://(?:www\.|m\.)?bilibili\.com/[^\"'\\s<>]+", body)
                if m:
                    cand = m.group(0)
                    if _is_bili_location(cand):
                        logger.info("[b23.tv expand] body-parse %s -> %s", url, cand)
                        resp.close()
                        return cand
            except Exception as be:
                logger.info("[b23.tv expand] body-parse %s failed: %s", url, str(be)[:120])
        resp.close()
        if _is_bili_location(final_url):
            logger.info("[b23.tv expand] GET %s -> %s", url, final_url)
            return final_url
        logger.info(
            "[b23.tv expand] GET %s final=%s history=%s",
            url,
            final_url,
            [r.status_code for r in resp.history],
        )
    except Exception as e:
        logger.info("[b23.tv expand] GET %s failed: %s", url, str(e)[:200])

    return url

def _expand_iqiyi_short_url(url: str, proxy: str = "") -> str:
    """把爱奇艺短链（iqy.net / qy.net 官方 302 跳转短链服务）展开为 iqiyi.com 真实视频页。

    yt-dlp 的 IqiyiIE 仅识别 iqiyi.com / iq.com 域；对 iqy.net/i/<id> 会落 [generic]
    提取器，而 iqy.net 页面本身不是 HTML 视频页，generic 拿不到视频流 → 「无法从该链接中
    找到视频」。把短链展开成长链后即可走 IqiyiIE 正常拿到 info。

    实现策略（沿用 _expand_b23tv_url 模式）：
    1) 优先 HEAD 取 Location（不下载 body，最省带宽）；
    2) HEAD 失败/无有效 Location 时回退 GET + allow_redirects=True，取最终 URL；
    3) Location 仅在指向 iqiyi.com / iq.com 时才接受，避免被跳到无关域。
    4) 展开失败保留原 URL —— 让下游 yt-dlp 走 generic 给出更明确的「找不到视频」错误。
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in ("iqy.net", "www.iqy.net", "m.iqy.net",
                    "qy.net", "www.qy.net", "m.qy.net"):
        return url

    try:
        import requests
    except Exception as e:
        logger.warning("[iqy.net expand] requests not available: %s", e)
        return url

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://iqiyi.com/",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    timeout = int(os.environ.get("VDL_B23TV_EXPAND_TIMEOUT", "15"))

    def _is_iqiyi_location(location: str) -> bool:
        return bool(location) and ("iqiyi.com" in location or "iq.com" in location)

    def _abs_location(resp) -> str:
        loc = resp.headers.get("Location") or ""
        if loc and not loc.startswith(("http://", "https://")):
            from urllib.parse import urljoin
            loc = urljoin(resp.url, loc)
        return loc

    # 1) HEAD（轻量，跟随重定向拿到最终 iqiyi.com 链接）
    try:
        resp = requests.head(
            url, headers=headers, proxies=proxies, timeout=timeout, allow_redirects=True,
        )
        expanded = resp.url
        if _is_iqiyi_location(expanded):
            logger.info("[iqiyi short expand] HEAD %s -> %s", url, expanded)
            return expanded
        logger.info(
            "[iqiyi short expand] HEAD %s status=%s final=%s",
            url, resp.status_code, expanded[:200],
        )
    except Exception as e:
        logger.info("[iqiyi short expand] HEAD %s failed: %s", url, str(e)[:200])

    # 2) GET + 自动跟随重定向（HEAD 被 CDN 丢弃/不返回 Location 时回退）
    try:
        resp = requests.get(
            url, headers=headers, proxies=proxies, timeout=timeout,
            allow_redirects=True, stream=True,
        )
        final_url = resp.url
        resp.close()
        if _is_iqiyi_location(final_url):
            logger.info("[iqy.net expand] GET %s -> %s", url, final_url)
            return final_url
        logger.info(
            "[iqy.net expand] GET %s final=%s history=%s",
            url, final_url, [r.status_code for r in resp.history],
        )
    except Exception as e:
        logger.info("[iqy.net expand] GET %s failed: %s", url, str(e)[:200])

    # 展开失败：保留原 URL，让 yt-dlp 走 generic 给出「找不到视频」的明确错误
    return url

def _expand_generic_302(url: str, proxy: str = "", allowed_hosts: tuple[str, ...] = (),
                        referer: str = "") -> str:
    """通用短链 302 展开（如 shturl.cc → inke.cn、xhslink.cn → xiaohongshu.com），带目标域名白名单校验。

    与 _expand_iqiyi_short_url 同思路：yt-dlp 的 InkeIE 只认 inke.cn 域，
    shturl.cc 短链会落 [generic] 失败，需先 HEAD/GET 跟随 302 展开。

    安全：展开后目标 host 必须在 allowed_hosts 白名单内，否则视为展开失败
    返回原 URL（防恶意短链把用户带到无关站点）。
    """
    import requests as _requests

    host = _host_of(url) or ""
    # 原始 URL 已在目标域内（非短链域）→ 无需展开，直接返回
    if any(h in host for h in allowed_hosts):
        return url
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "Referer": referer or ("https://www." + (allowed_hosts[0] if allowed_hosts else "") + "/"),
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        r = _requests.get(url, headers=headers, proxies=proxies,
                          allow_redirects=True, timeout=15)
        final = r.url or url
        final_host = _host_of(final) or ""
        if any(h in final_host for h in allowed_hosts):
            return final
    except Exception as e:  # noqa: BLE001
        logger.info("[short expand] %s failed: %s", url, str(e)[:120])
    return url

# 小红书笔记页路径（explore / discovery 两种形态，ID 为 24 位 hex）
_XHS_NOTE_PATH_RE = re.compile(r"/(?:explore|discovery/item)/[\da-f]{8,}")


def _expand_xhs_short(url: str, proxy: str = "") -> str:
    """把 xhslink.cn / xhslink.com 分享短链展开成 xiaohongshu.com 笔记页。

    2026-10-25 实测更正：短链**本身可以展开**，之前「网页端一律 302 回首页、
    任何下载工具都无法解析」的结论是错的。真正的坑是展开结果跟 UA 有关系：

      · 移动 UA（iPhone/Android）→ 302 直达
        https://www.xiaohongshu.com/discovery/item/<id>?xsec_token=…
      · 桌面 Chrome UA → 302 到
        https://www.xiaohongshu.com/login?redirectPath=<URL 编码的笔记页>

    旧实现用桌面 UA，终点永远落在 /login?…，而判定条件只认 URL 里带
    /explore/ 或 /discovery/item/，于是「展开成功却当成失败」，把短链原样
    交给 yt-dlp → [generic] 失败 → 反过来又套上「短链无法解析」的错误文案。

    这里做三路兜底：① 终点 URL 含笔记路径直接用；② 落在 /login 时从
    redirectPath 里解出笔记页；③ 前面都没命中时回看 302 跳转链的 Location。
    """
    import requests as _requests
    from urllib.parse import unquote as _unquote

    _mobile_ua = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
        "Mobile/15E148 Safari/604.1"
    )
    proxies = {"http": proxy, "https": proxy} if proxy else None

    def _note_url(candidate: str) -> str | None:
        """从候选 URL（可能是 /login?redirectPath=… 或纯笔记页）里提取笔记页。"""
        if not candidate:
            return None
        if _XHS_NOTE_PATH_RE.search(candidate) and "xiaohongshu.com" in candidate:
            # 兜底把 http:// 升到 https://（redirectPath 里是 http 形态）
            return candidate.replace("http://www.xiaohongshu.com", "https://www.xiaohongshu.com", 1)
        from urllib.parse import parse_qs as _parse_qs
        try:
            qs = _parse_qs(urlparse(candidate).query)
        except Exception:
            return None
        for key in ("redirectPath", "redirect_path", "redirect"):
            for raw in qs.get(key, [])[:1]:
                inner = _unquote(raw)
                if _XHS_NOTE_PATH_RE.search(inner) and "xiaohongshu.com" in inner:
                    return inner.replace(
                        "http://www.xiaohongshu.com", "https://www.xiaohongshu.com", 1
                    )
        return None

    try:
        r = _requests.get(
            url,
            headers={"User-Agent": _mobile_ua, "Referer": "https://www.xiaohongshu.com/"},
            proxies=proxies,
            allow_redirects=True,
            timeout=15,
        )
        for hop in [r.url] + [h.headers.get("Location", "") for h in (r.history or [])]:
            note = _note_url(hop)
            if note:
                logger.info("[xhs expand] %s -> %s", url, note[:160])
                return note
        logger.info("[xhs expand] %s 未拿到笔记页，终点=%s", url, (r.url or "")[:160])
    except Exception as e:  # noqa: BLE001
        logger.info("[xhs expand] %s failed: %s", url, str(e)[:160])
    return url

def _normalize_bilibili_url(url: str) -> str:
    """把 B站 长链/b23.tv BV 短链统一归一化为 www.bilibili.com/video/BVxxx 长链。

    保留分P（p）和时间戳（t）参数；去掉 vd_source/spm_id_from 等追踪参数。
    非 B站 链接原样返回。
    """
    # 先尝试长链（/video/BVxxx）
    m = _BILIBILI_LONG_URL_RE.match(url)
    if not m:
        # 再尝试 b23.tv/BVxxx 短链
        m = _BILIBILI_SHORT_BV_RE.match(url)
    if not m:
        # 根路径 BV（无 /video/ 前缀，B站 部分分享场景给出此形态）
        m = _BILIBILI_ROOT_BV_RE.match(url)
    if not m:
        return url
    bvid = m.group(1)
    parsed = urlparse(url)
    params: dict[str, str] = {}
    if parsed.query:
        for k, v in parse_qsl(parsed.query):
            if k in ("p", "t"):
                params[k] = v
    query = urlencode(params)
    return f"https://www.bilibili.com/video/{bvid}" + (f"?{query}" if query else "")

# --------------------------------------------------------------------------- #
# 通用链接归一化：平台无关净化 + B站 归一为 bilibili.com 长链
# --------------------------------------------------------------------------- #
# 通用追踪/推广参数（地址栏常带，对解析无益；部分平台会据此触发更严的防盗链/风控）
_URL_TRACKING_PARAMS = frozenset({
    "vd_source", "spm_id_from", "from", "from_source",     "share_source",
    "share_medium", "share_from", "share_id", "shareid", "share_channel", "timestamp",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "is_search", "scene", "sid", "campaign", "ame_from", "monitor",
    "xhsshare", "appuid", "wxfcache", "m_source",
})

def _strip_tracking_params(url: str, keep: frozenset[str] | set[str] | None = None) -> str:
    """通用：剥掉 vd_source/spm_id_from/from/share_* 等追踪参数，其余保留。

    keep: 指定需要保留的参数名集合（大小写敏感，按需使用）。用于爱奇艺
    playShare.html 等场景，其中 shareId / positiveId 是视频标识而非追踪参数。
    """
    parsed = urlparse(url)
    if not parsed.query:
        return url
    keep_set = keep or set()
    kept: list[tuple[str, str]] = []
    for k, v in parse_qsl(parsed.query, keep_blank_values=True):
        if k in keep_set:
            kept.append((k, v))
            continue
        if k.lower() in _URL_TRACKING_PARAMS:
            continue
        kept.append((k, v))
    if not kept:
        return parsed._replace(query="").geturl()
    return parsed._replace(query=urlencode(kept)).geturl()

def _normalize_share_url(url: str, proxy: str = "") -> str:
    """链接归一化入口（平台无关）。

    1. B站：所有链接统一归一化为 www.bilibili.com/video/BVxxx 长链。
       b23.tv 随机短码会先用 HEAD 请求 302 展开，确保 yt-dlp 使用 BiliBili
       提取器（能正确注入 Cookie/Referer/UA）。桌面端 IP 风控较松时短链也
       能走 generic 通过；网页端(Railway 代理 IP)必须走 bilibili 提取器。
    2. 爱奇艺：iqy.net / qy.net 短链先 HEAD/GET 302 展开为 iqiyi.com 长链，
       否则 yt-dlp IqiyiIE 不认 iqy.net 域会落 [generic] 返回空。
    3. 其他平台：剥离追踪参数，降低防盗链/风控识别概率。
       不做伪短链转换——抖音/快手/小红书短链是服务端随机 token，
       无法从长链静态推导，强行构造会破坏解析（需调分享 API，不在本范围）。
    4. YouTube：youtu.be / shorts / live / embed / m. / music. 统一为
       www.youtube.com/watch?v=。**下载入口同样必须归一化**：yt-dlp 的
       http_headers Cookie 按初始 URL 的域作用域，短链会把有效 Cookie 挡在
       innertube 之外 → 解析成功但下载被判 bot（详见 canonicalize_video_url）。
    """
    # YouTube 优先：短链/子域/嵌入页统一成长链，解析与下载两条路径共用同一入口
    try:
        _yt_host = (urlparse(url).hostname or "").lower()
    except ValueError:
        _yt_host = ""
    if _is_youtube_host(_yt_host):
        normalized = canonicalize_video_url(url)
        if normalized != url:
            logger.info("[normalize] %s -> %s", url, normalized)
        return normalized

    # B站 特例优先：先把短链展开成长链，再统一规范化为 bilibili.com 长链
    if "bilibili.com" in url or "b23.tv" in url:
        expanded = _expand_b23tv_url(url, proxy=proxy)
        normalized = _strip_tracking_params(_normalize_bilibili_url(expanded))
        logger.info("[normalize] %s -> %s", url, normalized)
        return normalized

    # 爱奇艺 短链 iqy.net / qy.net 展开为 iqiyi.com 长链（yt-dlp IqiyiIE 仅识别 iqiyi.com）
    if "iqy.net" in url or "qy.net" in url:
        expanded = _expand_iqiyi_short_url(url, proxy=proxy)
        if expanded != url:
            # playShare 分享页的 shareId / positiveId 是视频标识，必须保留
            keep = {"shareId", "positiveId"} if "playShare" in expanded else None
            normalized = _strip_tracking_params(expanded, keep=keep)
            logger.info("[normalize] %s -> %s", url, normalized)
            return normalized
        # 展开失败：保留原 URL，让 yt-dlp 走 generic 给「找不到视频」更明确的错误
        return _strip_tracking_params(url)

    # 花椒直播短链 shturl.cc/xxx：302 展开为 inke.cn 真实直播/回放页
    # （yt-dlp InkeIE 只认 inke.cn 域，shturl.cc 会落 generic 失败）
    if "shturl.cc" in url:
        expanded = _expand_generic_302(url, proxy=proxy, allowed_hosts=("inke.cn",))
        if expanded != url:
            logger.info("[normalize] %s -> %s", url, expanded)
            return expanded
        return _strip_tracking_params(url)

    # 小红书短链 xhslink.cn / xhslink.com/o/xxx：展开为 xiaohongshu.com 笔记页
    # （yt-dlp XiaohongShuIE 只认 xiaohongshu.com 域，短链会落 generic 失败）
    # 2026-10-25 实测更正：短链可以展开，只是必须用移动 UA；桌面 UA 会落在
    # /login?redirectPath=…，需从参数里解出笔记页（细节见 _expand_xhs_short）。
    if "xhslink.cn" in url or "xhslink.com" in url:
        expanded = _expand_xhs_short(url, proxy=proxy)
        if expanded != url:
            logger.info("[normalize] %s -> %s", url, expanded)
            return expanded
        return url

    # 爱奇艺 直接 playShare 分享页（www.iqiyi.com/playShare.html?shareId=...）：
    # shareId / positiveId 是视频标识，必须保留，否则 bare playShare.html 会被
    # 爱奇艺跳到 error.html?errortype=2（"内容暂时无法观看"），worker 也抓不到 m3u8。
    if "iqiyi.com" in url and "playShare" in url:
        normalized = _strip_tracking_params(url, keep={"shareId", "positiveId"})
        logger.info("[normalize] %s -> %s", url, normalized)
        return normalized

    # 抖音：分享短链 v.douyin.com 展开后常为 iesdouyin.com/xg/video/ID，
    # 该域名 Playwright 解析拿不到视频流；归一化为 douyin.com/video/ID 即可正常解析。
    m = re.search(r"iesdouyin\.com/xg/video/(\d{15,})", url)
    if m:
        normalized = f"https://www.douyin.com/video/{m.group(1)}"
        logger.info("[normalize] %s -> %s", url, normalized)
        return normalized

    # 西瓜视频 ixigua.com/<ID>：**不要**改写成 douyin.com/video/<ID>。
    # 2026-10-25 实测：两者视频 ID 并不互通，改写后抖音侧一律回「视频不存在」，
    # 白等 30s+ 还把真实原因盖掉。西瓜走 _ixigua_info（yt-dlp IxiguaIE + 浏览器 Cookie）。

    # 腾讯视频：把带 vid 的分享/合集链统一成单集页 —— yt-dlp 的 vqq:video 只认
    # `v.qq.com/x/page/<vid>.html`。
    # ① 移动端分享 http://m.v.qq.com/play/play.html?vid=z3183jgqz3e
    #    → 直接喂 yt-dlp 是 "Unsupported URL"
    # ② 合集页带参数 v.qq.com/x/cover/<cid>.html?vid=z3183jgqz3e
    #    → 归一化后实测可出 8 条格式（2026-10-25 用真实微短剧分享链验证）
    if "qq.com" in url:
        _vm = re.search(r"[?&]vid=([a-z0-9]{8,})", url)
        if _vm:
            normalized = f"https://v.qq.com/x/page/{_vm.group(1)}.html"
            logger.info("[normalize] %s -> %s", url, normalized)
            return normalized

    # 其余平台：仅做通用追踪参数净化，零风险
    return _strip_tracking_params(url)

def _to_int(v):
    """安全转 int：B站 view API 的 duration/stat 字段常是字符串。"""
    if v is None or v == "":
        return None
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None

def _bilibili_api_extract(url: str, proxy: str = "", cookie: str = "") -> dict[str, Any] | None:
    """B站 专用兜底解析器：当 yt-dlp 网络栈反复 IncompleteRead 时，直接用 requests 调 B站 API。

    这是「绕过 yt-dlp 网络层」的最后手段。只处理最常见的 www.bilibili.com/video/BVxxx
    单 P 视频，构造一个足够前端展示 + 后续下载的 info dict。若 API 返回需 WBI 签名或
    其他复杂形态，返回 None 让外层继续抛原异常。
    """
    m = re.search(r"bilibili\.com/(?:video/)?(BV[0-9A-Za-z]+)", url)
    if not m:
        return None
    bvid = m.group(1)
    parsed = urlparse(url)
    page = 1
    try:
        page = int(parse_qs(parsed.query).get("p", ["1"])[0])
    except Exception:
        pass

    try:
        import requests
    except Exception as e:
        logger.warning("[bilibili api fallback] requests not available: %s", e)
        return None

    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "close",
    }
    if cookie:
        headers["Cookie"] = _clean_header_value(cookie)

    # 1) 取视频元数据
    view_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
    try:
        r = requests.get(view_url, headers=headers, proxies=proxies, timeout=30)
        r.raise_for_status()
        view_data = r.json()
        if view_data.get("code") != 0:
            logger.warning("[bilibili api fallback] view API code=%s msg=%s", view_data.get("code"), view_data.get("message"))
            return None
        video_info = view_data["data"] or {}
        pages = video_info.get("pages") or []
        page_info = pages[page - 1] if 1 <= page <= len(pages) else (pages[0] if pages else {})
        cid = page_info.get("cid")
        if not cid:
            logger.warning("[bilibili api fallback] no cid found")
            return None
    except Exception as e:
        logger.warning("[bilibili api fallback] view request failed: %s", str(e)[:200])
        return None

    # 2) 取播放地址（优先 DASH）
    play_url = (
        f"https://api.bilibili.com/x/player/playurl"
        f"?bvid={bvid}&cid={cid}&qn=127&fnver=0&fnval=4048&fourk=1"
    )
    try:
        r = requests.get(play_url, headers=headers, proxies=proxies, timeout=30)
        r.raise_for_status()
        play_data = r.json()
        if play_data.get("code") != 0:
            logger.warning("[bilibili api fallback] playurl API code=%s msg=%s", play_data.get("code"), play_data.get("message"))
            return None
        play_info = play_data.get("data") or {}
    except Exception as e:
        logger.warning("[bilibili api fallback] playurl request failed: %s", str(e)[:200])
        return None

    # 3) 构造 formats
    formats: list[dict[str, Any]] = []
    fmt_names = {
        q.get("quality"): q.get("new_description") or q.get("display_desc")
        for q in play_info.get("support_formats") or []
    }

    def _mime_ext(mime: str) -> str:
        return {"video/mp4": "m4s", "audio/mp4": "m4s"}.get(mime, "mp4")

    dash = play_info.get("dash") or {}

    # DASH 视频
    for v in dash.get("video") or []:
        url0 = v.get("baseUrl") or v.get("base_url") or v.get("url")
        if not url0:
            continue
        fid_match = re.search(r"-([0-9]+)\.m4s\\?", url0)
        fmt_id = str(v.get("id")) if not fid_match else fid_match.group(1)
        formats.append({
            "format_id": fmt_id,
            "url": url0,
            "ext": _mime_ext(v.get("mimeType") or v.get("mime_type") or ""),
            "vcodec": (v.get("codecs") or "").split(".")[0] or "avc",
            "acodec": "none",
            "width": v.get("width"),
            "height": v.get("height"),
            "fps": _to_int(v.get("frameRate")) or _to_int(v.get("frame_rate")),
            "tbr": (_to_int(v.get("bandwidth")) or 0) / 1000.0,
            "filesize": _to_int(v.get("size")),
            "quality": v.get("id"),
            "format": fmt_names.get(v.get("id")),
            "protocol": "https",
            "http_headers": {"Referer": "https://www.bilibili.com/"},
        })

    # DASH 音频
    for a in dash.get("audio") or []:
        url0 = a.get("baseUrl") or a.get("base_url") or a.get("url")
        if not url0:
            continue
        formats.append({
            "format_id": f'a-{a.get("id", "audio")}',
            "url": url0,
            "ext": _mime_ext(a.get("mimeType") or a.get("mime_type") or ""),
            "vcodec": "none",
            "acodec": (a.get("codecs") or "").split(".")[0] or "aac",
            "tbr": (_to_int(a.get("bandwidth")) or 0) / 1000.0,
            "filesize": _to_int(a.get("size")),
            "format": "音频",
            "protocol": "https",
            "http_headers": {"Referer": "https://www.bilibili.com/"},
        })

    # 兼容旧版 durl（FLV/MP4 直链）
    for d in play_info.get("durl") or []:
        url0 = d.get("url")
        if not url0:
            continue
        formats.append({
            "format_id": f'durl-{d.get("order", 0)}',
            "url": url0,
            "ext": "mp4",
            "filesize": _to_int(d.get("size")),
            "protocol": "https",
            "http_headers": {"Referer": "https://www.bilibili.com/"},
        })

    if not formats:
        logger.warning("[bilibili api fallback] no formats extracted from play_info")
        return None

    owner = video_info.get("owner") or {}
    stat = video_info.get("stat") or {}
    info = {
        "id": f'{bvid}{f"_p{page}" if page > 1 else ""}',
        "title": video_info.get("title", bvid),
        "description": video_info.get("desc"),
        "thumbnail": video_info.get("pic"),
        "uploader": owner.get("name"),
        "uploader_id": str(owner.get("mid") or ""),
        "duration": _to_int(video_info.get("duration")),
        "view_count": _to_int(stat.get("view")),
        "like_count": _to_int(stat.get("like")),
        "comment_count": _to_int(stat.get("reply")),
        "timestamp": _to_int(video_info.get("pubdate")),
        "webpage_url": url,
        "extractor": "bilibili",
        "extractor_key": "BiliBili",
        "ext": "mp4",
        "formats": formats,
        "http_headers": {"Referer": "https://www.bilibili.com/"},
    }
    # 模拟"已选流"：yt-dlp process_info 直接吃 info（不经 process_ie_result），
    # 无 requested_formats 会走单文件分支 → dl() 因无顶层 url 抛 No video formats。
    # 手动挑 best 视频轨 + 音频轨构造 requested_formats（同抖音修复经验，
    # 不设顶层 url 避免 HttpFD 合并分支丢音频；format 级已带 protocol=https）。
    _vids = [f for f in formats if f.get("vcodec") and f["vcodec"] != "none"]
    _auds = [f for f in formats if f.get("acodec") and f["acodec"] != "none"]
    if _vids and _auds:
        _vids.sort(key=lambda f: (f.get("height") or 0), reverse=True)
        info["requested_formats"] = [_vids[0], _auds[0]]
    logger.info("[bilibili api fallback] extracted %s formats for %s", len(formats), bvid)
    return info

def _rebuild_requested_formats(info: dict[str, Any], quality_key: str, protocol_filter: str = "") -> None:
    """按用户选择的清晰度重建 requested_formats（B站 API 兜底 info 固定选了 best）。

    yt-dlp process_info 直接吃 info.requested_formats 下载，不会再用 format
    selector 过滤。若不重建，用户选 480P/360P 也会下成兜底固定的 720P。
    仅音频（audio/m4a）→ 单文件音频轨；视频 → <= 目标高度的最高 avc1 轨 + 音频轨。
    A2（2026-10-02）：protocol_filter="hls"/"http" 时只在对应协议族里挑视频轨
    （无可用轨则整体不动，保留原选择），音频轨无同族时保留原样。
    """
    fmts = [f for f in (info.get("formats") or []) if isinstance(f, dict)]
    vids = [f for f in fmts if f.get("vcodec") and f["vcodec"] != "none"]
    auds = [f for f in fmts if f.get("acodec") and f["acodec"] != "none"]

    def _fmt_family(f: dict) -> str:
        proto = (f.get("protocol") or "").split("+")[0].lower()
        return "hls" if (proto.startswith("m3u8") or _is_hls_url(f.get("url") or "")) else "http"

    if protocol_filter:
        _fv = [f for f in vids if _fmt_family(f) == protocol_filter]
        if not _fv:
            return  # 指定协议族没有可用视频轨：不动原选择，避免改坏能下载的配置
        vids = _fv
        _fa = [f for f in auds if _fmt_family(f) == protocol_filter]
        auds = _fa or auds
    if not vids:
        return
    if quality_key in (AUDIO_KEY, M4A_KEY):
        if auds:
            auds.sort(key=lambda f: f.get("abr") or 0, reverse=True)
            info["requested_formats"] = [auds[0]]
            info["url"] = auds[0]["url"]
            info["protocol"] = (auds[0].get("protocol") or "https").split("+")[0]
            info["ext"] = "m4a"
        return
    target = 9999
    try:
        target = int(quality_key)
    except (TypeError, ValueError):
        pass
    cand = [f for f in vids if (f.get("height") or 9999) <= target] or vids
    avc = [f for f in cand if str(f.get("vcodec") or "").startswith("avc")]
    pool = avc or cand
    pool.sort(key=lambda f: (f.get("height") or 0), reverse=True)
    v = pool[0]
    if auds:
        auds.sort(key=lambda f: f.get("abr") or 0, reverse=True)
        info["requested_formats"] = [v, auds[0]]
    else:
        info["requested_formats"] = [v]
        info["url"] = v["url"]
        # A2：协议取自所选格式本身（HLS 轨要标 m3u8_native，写死 https 会重演
        # 「把播放清单当文件下」的直链协议 bug，见 _run_once 的协议归一化注释）
        info["protocol"] = (v.get("protocol") or "https").split("+")[0]
    info["ext"] = "mp4"

def _switch_to_hls(info: dict[str, Any], quality_key: str) -> bool:
    """A2 重试策略②：把本次下载的格式选择切到 HLS（m3u8）协议族。

    直链被风控/403 时，HLS 分片走不同的 CDN 路径，时常能通过。
    仅当 formats 里确实存在 HLS 视频轨、且当前选中配置不是 HLS 时才重建；
    否则返回 False（保持原选择，避免把能下载的配置改坏）。
    """
    fmts = [f for f in (info.get("formats") or []) if isinstance(f, dict)]

    def _is_hls_fmt(f: dict) -> bool:
        proto = (f.get("protocol") or "").split("+")[0].lower()
        return proto.startswith("m3u8") or _is_hls_url(f.get("url") or "")

    if not any(f.get("vcodec") and f["vcodec"] != "none" and _is_hls_fmt(f) for f in fmts):
        return False
    cur = [f for f in (info.get("requested_formats") or [])
           if isinstance(f, dict) and f.get("vcodec") and f["vcodec"] != "none"]
    if cur and all(_is_hls_fmt(f) for f in cur):
        return False  # 当前已选 HLS，无需切换
    if not cur and info.get("vcodec") not in (None, "none") and _is_hls_fmt(info):
        return False  # 单文件 info 本身就是 HLS
    _before = info.get("requested_formats")
    _rebuild_requested_formats(info, quality_key, protocol_filter="hls")
    _after = info.get("requested_formats")
    return bool(_after) and _after != _before

def _clean_header_value(value: str) -> str:
    """过滤 HTTP header 值，只保留 latin-1 安全字符。

    浏览器复制出的 Cookie 串可能混入中文/特殊字符，requests/urllib3 在把 header
    编码为 latin-1 发送时会抛 `UnicodeEncodeError: 'latin-1' ... ordinal not in range(256)`。
    这里按字节清理：保留能 encode 成 latin-1 且解码后不变的字符，其余丢弃。
    """
    if not value:
        return value
    try:
        encoded = value.encode("latin-1")
        return encoded.decode("latin-1")
    except (UnicodeEncodeError, UnicodeDecodeError):
        cleaned = []
        for ch in value:
            try:
                ch.encode("latin-1")
                cleaned.append(ch)
            except UnicodeEncodeError:
                pass
        return "".join(cleaned)

_BILIBILI_PATCH_STATE: dict = {"cookie": "", "proxy": "", "ua": ""}

_BILI_UA_FALLBACK = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _bili_patch_ua(ua: str) -> str:
    """B 站对脚本类 UA（python-requests / curl / urllib）一律回 412。

    实测同一 Cookie、同一 IP：Chrome UA 200，python-requests UA 412。
    非浏览器 UA 一律换成 Chrome，避免请求「打得像脚本」。
    """
    ua = (ua or "").strip()
    if (not ua) or ("Mozilla/" not in ua) or ("python" in ua.lower()):
        return _BILI_UA_FALLBACK
    return ua


def _patch_bilibili_webpage_download(proxy: str = "", cookie: str = "", ua: str = "") -> None:
    """为 BiliBiliIE 打补丁：视频页 HTML 用 requests 预下载，绕过 yt-dlp urllib 经代理 IncompleteRead。

    仅对 bilibili.com/video/BVxxx 的第一次 _download_webpage_handle 生效。
    如果 requests 也失败，回退原 yt-dlp 行为。
    """
    try:
        from yt_dlp.extractor.bilibili import BiliBiliIE
    except Exception:
        return

    # 🔴 2026-10-18：补丁是**全局单例**（打一次永远复用），旧实现把首次请求的
    #    cookie/proxy/ua 冻结在闭包里 ⇒ 服务启动后第一次 B 站请求若没带 Cookie，
    #    之后每次都是裸奔请求，B 站对无 Cookie 的 HTML 一律 412（实测直连无 Cookie
    #    412 / 带池内 Cookie 200）。改为每次调用刷新状态，补丁体按最新值发送。
    if cookie:
        _BILIBILI_PATCH_STATE["cookie"] = cookie
    if proxy:
        _BILIBILI_PATCH_STATE["proxy"] = proxy
    if ua:
        _BILIBILI_PATCH_STATE["ua"] = ua

    attr = "_vdl_webpage_patched"
    if getattr(BiliBiliIE._download_webpage_handle, attr, False):
        return

    _orig = BiliBiliIE._download_webpage_handle

    def _patched(self, url_or_request, video_id, *args, **kwargs):
        url = ""
        try:
            if isinstance(url_or_request, str):
                url = url_or_request
            else:
                url = getattr(url_or_request, "url", "") or str(url_or_request)
        except Exception:
            url = str(url_or_request)

        if url and "/video/BV" in url and "bilibili.com" in url:
            logger.info("[bilibili patch] try requests for %s", url)
            try:
                import email.message
                import io
                import requests
                from urllib.response import addinfourl

                _ck = _BILIBILI_PATCH_STATE.get("cookie") or ""
                if not _ck:
                    # 本次请求没带 Cookie（未登录解析 / 调用方没传）→ 用公共池兜底。
                    # B 站对无 Cookie 的 HTML 请求恒返回 412，而池里已有登录态。
                    # _pooled_cookie：本机池 miss 时回读云端池（桌面端本机池常为空）。
                    _ck = _pooled_cookie("bilibili.com")
                _ua = _bili_patch_ua(_BILIBILI_PATCH_STATE.get("ua") or ua)
                # 🔴 2026-10-18 实测：`/video/BVxxx` **不带尾斜杠**时 B 站直接回 412，
                #    带斜杠同 Cookie 同 UA 回 200（各测 2 次，稳定复现）。
                #    所以先补斜杠再请求，别指望 301 能救——301 响应本身就是 412。
                _req_url = url
                try:
                    from urllib.parse import urlsplit, urlunsplit
                    _sp = urlsplit(_req_url)
                    if _sp.path and not _sp.path.endswith("/"):
                        _req_url = urlunsplit((_sp.scheme, _sp.netloc,
                                               _sp.path + "/", _sp.query, _sp.fragment))
                except Exception:
                    pass
                headers = {
                    "User-Agent": _ua,
                    "Referer": "https://www.bilibili.com/",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                }
                if _ck:
                    headers["Cookie"] = _clean_header_value(_ck)
                _px = _BILIBILI_PATCH_STATE.get("proxy") or proxy
                proxies = {"http": _px, "https": _px} if _px else None
                resp = requests.get(
                    _req_url,
                    headers=headers,
                    proxies=proxies,
                    timeout=30,
                    allow_redirects=True,
                )
                resp.raise_for_status()
                content = resp.content

                # B站 响应头里可能含非 latin-1 字符，直接塞给 yt-dlp 会触发
                # 'latin-1' codec can't encode characters... 回退到原 urllib 路径。
                # 这里只保留 latin-1 安全的头，并用标准库 addinfourl 包装。
                safe_headers = email.message.Message()
                for k, v in resp.headers.items():
                    try:
                        v.encode("latin-1")
                        safe_headers[k] = v
                    except UnicodeEncodeError:
                        logger.debug("[bilibili patch] drop non-latin1 header %s", k)
                fp = io.BytesIO(content)
                urlh = addinfourl(fp, safe_headers, resp.url, code=resp.status_code)

                logger.info("[bilibili patch] requests OK %s bytes=%s", resp.url, len(content))
                return resp.text, urlh
            except Exception as e:
                logger.warning(
                    "[bilibili patch] requests failed for %s: %s, fallback to yt-dlp",
                    url,
                    str(e)[:200],
                    exc_info=False,
                )

        return _orig(self, url_or_request, video_id, *args, **kwargs)

    BiliBiliIE._download_webpage_handle = _patched
    setattr(BiliBiliIE._download_webpage_handle, attr, True)

class _YoutubeDL(YoutubeDL):
    """按是否走代理选择 request handler。

    - 有 proxy（国内站回源代理 / 用户显式代理）：强制用 **Urllib** handler。
      yt-dlp 的 Requests handler 不支持 `proxy` option（构造时忽略），强制
      requests-only 会导致请求直连 → Railway 海外 IP 访问国内站被地理围栏
      403（Cloudflare 1010）。urllib 的 ProxyHandler 对 http 代理支持可靠。
    - 无 proxy（直连场景）：维持 requests-only——urllib 频繁 IncompleteRead，
      requests/urllib3 对连接重置、分块传输不完整更健壮。
    """

    def build_request_director(self, handlers, preferences=None):
        proxy = (self.params or {}).get("proxy") or ""
        all_keys = [getattr(h, "RH_KEY", "?") for h in handlers]

        if proxy:
            urllib_handlers = [h for h in handlers if getattr(h, "RH_KEY", None) == "Urllib"]
            if urllib_handlers:
                logger.info(
                    "[yt-dlp] proxy=%s -> forcing urllib handler only (requests ignores proxy)",
                    proxy,
                )
                return super().build_request_director(urllib_handlers, preferences)
            logger.warning("[yt-dlp] proxy set but no Urllib handler available: %s", all_keys)
            return super().build_request_director(handlers, preferences)

        requests_handlers = [h for h in handlers if getattr(h, "RH_KEY", None) == "Requests"]
        logger.info("[yt-dlp] no proxy, available handlers=%s requests_found=%s", all_keys, bool(requests_handlers))

        if requests_handlers:
            logger.info("[yt-dlp] forcing requests request handler only")
            handlers = requests_handlers
        else:
            logger.warning("[yt-dlp] requests handler NOT available, falling back to all handlers")

        director = super().build_request_director(handlers, preferences)

        # 安全网：如果 filtering 没生效（例如 super() 内部又重新加入了 urllib），
        # 强制把 Urllib handler 从 director 里拿掉。
        if requests_handlers and "Urllib" in director.handlers:
            logger.warning("[yt-dlp] removing Urllib handler from director safety net")
            del director.handlers["Urllib"]

        final_keys = list(director.handlers.keys())
        logger.info("[yt-dlp] final director handlers=%s", final_keys)
        return director

def _patch_requests_handler_retries() -> None:
    """让 yt-dlp 的 Requests handler 启用 urllib3 自动重试。

    yt-dlp 默认传 Retry(False) 禁用重试。Railway→国内代理链路不稳定，
    经常读到一半连接被重置，需要 urllib3 对 GET/HEAD 等幂等请求自动重试。
    """
    try:
        from yt_dlp.networking._requests import RequestsRH
        from yt_dlp.networking._requests import RequestsHTTPAdapter
        import urllib3.util.retry
    except Exception as e:
        logger.warning("[requests patch] cannot import yt-dlp requests handler: %s", e)
        return

    if getattr(RequestsRH._create_instance, "_vdl_retries_patched", False):
        return

    _orig_create_instance = RequestsRH._create_instance

    def _patched_create_instance(self, cookiejar, legacy_ssl_support=None):
        # 先调原方法拿到 session（含默认 adapter 配置）
        session = _orig_create_instance(self, cookiejar=cookiejar, legacy_ssl_support=legacy_ssl_support)

        # 重新 mount 一个带重试策略的 adapter
        # 2026-09-26：默认值提高（5/3/5 → 12/10/10）。用户代理客户端复用连接必挂
        # （实测池化 0/4）+ 新连接约 25% 失败，而失败都很快（502/SSL EOF 亚秒级），
        # 重试几乎不增加耗时却能显著提高单请求成功率（0.25^11 ≈ 2e-7）。
        retry = urllib3.util.retry.Retry(
            total=int(os.environ.get("VDL_REQUESTS_RETRY_TOTAL", "12")),
            connect=int(os.environ.get("VDL_REQUESTS_RETRY_CONNECT", "10")),
            read=int(os.environ.get("VDL_REQUESTS_RETRY_READ", "10")),
            backoff_factor=float(os.environ.get("VDL_REQUESTS_RETRY_BACKOFF", "0.5")),
            # 对 IncompleteRead / Connection reset / read timeout 等做重试
            raise_on_status=False,
            raise_on_redirect=False,
            allowed_methods=["HEAD", "GET", "OPTIONS"],
            status_forcelist=[500, 502, 503, 504, 408, 429],
        )
        adapter = RequestsHTTPAdapter(
            ssl_context=self._make_sslcontext(legacy_ssl_support=legacy_ssl_support),
            source_address=self.source_address,
            max_retries=retry,
        )
        session.adapters.clear()
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        logger.debug("[requests patch] mounted retry adapter total=%s", retry.total)
        return session

    RequestsRH._create_instance = _patched_create_instance
    setattr(RequestsRH._create_instance, "_vdl_retries_patched", True)
    logger.info("[requests patch] enabled urllib3 retries for yt-dlp requests handler")

# 模块加载时即启用 requests 重试补丁
_patch_requests_handler_retries()


# --------------------------------------------------------------------------- #
# 代理客户端「连接复用缺陷」规避补丁（2026-09-26 实测定位）
#
# 用户代理客户端（本机 127.0.0.1:7892，Clash 系）对**复用连接**处理有缺陷：
# 同一时刻对照实验——池化 Session 连发 4/4 全部读超时，每次新建连接 3/4 成功（0.4~1.8s）；
# 原始 socket 逐头排查 CONNECT 全部 200 ⇒ 502/卡死都发生在「复用」环节。
# curl 独立进程（永远新连接）全通；浏览器自动重试 + HTTP/2 复用所以能播；
# yt-dlp 的 requests handler 用持久 Session ⇒ 首个请求之后稳定卡死/502。
#
# 解法：probe/resolve 期间（线程局部开关）每个请求发送前 _clear_instances()
# 丢弃缓存的 Session ⇒ 每请求新建 TCP+TLS。下载阶段不开开关（分片吞吐靠连接复用）。
# --------------------------------------------------------------------------- #
_no_keepalive_local = threading.local()


def _patch_requests_handler_no_keepalive() -> None:
    try:
        from yt_dlp.networking._requests import RequestsRH
    except Exception as e:  # noqa: BLE001
        logger.warning("[no-keepalive patch] cannot import requests handler: %s", e)
        return
    if getattr(RequestsRH._send, "_vdl_no_keepalive_patched", False):
        return

    _orig_send = RequestsRH._send

    def _patched_send(self, request):
        if getattr(_no_keepalive_local, "enabled", False):
            try:
                self._clear_instances()   # 丢弃上一请求留下的 Session/连接池
            except Exception:  # noqa: BLE001
                pass
        return _orig_send(self, request)

    RequestsRH._send = _patched_send
    setattr(_patched_send, "_vdl_no_keepalive_patched", True)
    logger.info("[no-keepalive patch] enabled（resolve 线程禁用 yt-dlp 连接复用）")


_patch_requests_handler_no_keepalive()

def _cn_proxy_url() -> str:
    """国内站回源代理地址。

    Railway 环境（VDL 网页版部署在海外）默认走本机反向隧道代理 127.0.0.1:18889，
    该代理经 WebSocket 隧道把流量透明转发到国内 ECS 的 cn_proxy，绕开跨境入站瓶颈。
    本机（桌面/Mac，非 Railway）留空直连即可。VDL_PROXY_CN 可显式覆盖默认值。

    注意：VDL_PROXY_CN 显式置空（''）视同未设置，必须回落默认值——否则 Railway
    网页版国内站会退化成海外直连，被地理围栏 403（Cloudflare 1010）。
    """
    explicit = os.environ.get("VDL_PROXY_CN", "").strip()
    if explicit:
        return explicit
    on_railway = any(
        os.environ.get(k)
        for k in (
            "RAILWAY_ENVIRONMENT", "RAILWAY_SERVICE_ID", "RAILWAY_PROJECT_ID",
            "RAILWAY_BRANCH", "RAILWAY_DEPLOYMENT_ID", "RAILWAY_REPLICA_ID",
        )
    )
    return "http://127.0.0.1:18889" if on_railway else ""

def _yt_proxy_override() -> str:
    """A4（2026-10-02）：YouTube 独立代理（分流用），未配置返回空串。

    优先级：环境变量 VDL_PROXY_YT > 配置文件 proxy.json 的 "youtube" 键。
    背景：机房/家宽 IP 信誉差会被 YouTube bot 门禁逐视频拦（换 client/Cookie 无效），
    只有 YT 流量改走独立代理（如 WARP proxy 模式 socks5h://127.0.0.1:40000），
    国内站与其它海外站不受影响。配置文件供双击 .app 的 GUI 用户使用
    （GUI 进程不继承终端环境变量）：~/.video-downloader/proxy.json
    （VDL_HOME 可覆盖），内容 {"youtube": "socks5h://127.0.0.1:40000"}。
    """
    explicit = os.environ.get("VDL_PROXY_YT", "").strip()
    if explicit:
        return explicit
    try:
        data = json.loads(_proxy_config_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return str(data.get("youtube") or "").strip()
    except (OSError, ValueError):
        pass
    return ""

def _proxy_config_path() -> Path:
    """proxy.json 的落点：VDL_HOME 可覆盖，默认 ~/.video-downloader/proxy.json。

    设置界面（system.py 的 /api/settings/proxy）与运行时读取（_yt_proxy_override）
    共用此函数，保证「界面写入」和「下载时读取」永远指向同一个文件。
    """
    override = (os.environ.get("VDL_HOME") or "").strip()
    base = Path(override) if override else Path.home() / ".video-downloader"
    return base / "proxy.json"

def _resolve_proxy(host: str = "") -> str:
    """按目标站点所在地区分流代理，海外站和国内站互不干扰。

    YouTube（youtube.com/youtu.be/googlevideo.com）：
      VDL_PROXY_YT / proxy.json["youtube"] > 通用策略。A4 分流：只有 YT 走独立代理。

    国内站（B站/抖音/腾讯/chrqj 等）：
      VDL_PROXY_CN（国内出口回源代理）> 直连。
      服务部署在海外（Railway 等）时，国内站会被地理围栏 403，必须配 VDL_PROXY_CN
      指向一台国内机器的 HTTP 代理；本机跑在国内则留空直连即可。

    海外站（Twitter 等）：
      VDL_PROXY > macOS 系统代理（scutil）> 标准 http(s)_proxy 环境变量。
      刻意避开 WorkBuddy 注入的 127.0.0.1:57885（实测不通海外）。

    关键：绝不能用同一个变量兜住两边——国内代理出不去海外，海外代理进不来国内。
    """
    if host:
        _h = host.lower()
        if any(_h == d or _h.endswith("." + d) for d in _YOUTUBE_HOSTS + ("googlevideo.com",)):
            yt = _yt_proxy_override()
            if yt:
                return yt
    if host and is_china_host(host):
        return _cn_proxy_url()
    explicit = os.environ.get("VDL_PROXY", "").strip()
    if explicit:
        return explicit
    if sys.platform == "darwin":
        mac = _macos_system_proxy()
        if mac:
            return mac
        # scutil 读空时（双击 .app 是 GUI 进程，不继承终端 http_proxy；
        # Clash 等开了但没写系统代理时）扫描本机常见代理端口兜底
        probed = _probe_local_proxy_ports()
        if probed:
            return probed
    return os.environ.get("https_proxy") or os.environ.get("http_proxy") or ""
DOWNLOAD_RETRIES = 3
# 下载体积上限（MB）：防止被当成免费大盘偷跑带宽 / 撑爆磁盘。设为 0 表示不限。
# 2026-10-02 B1：2048 → 20480。2GB 把 4K/长视频直接拒之门外（大文件体验的第一杀手）；
# 撑爆磁盘的风险改由下载前「磁盘余量预检」兜底（见 _preflight_space_check），
# 而不是靠一刀切的体积上限。
_MAX_FILE_MB = int(os.environ.get("VDL_MAX_FILE_MB", "20480") or 20480)
_MAX_FILE_BYTES = _MAX_FILE_MB * 1024 * 1024
# 国内站 m3u8 分片并行下载段数。低并发易触发 CDN 慢速 trickle（单连接被限速到几 KB/s），
# 适度提高可让多连接分摊带宽、显著改善长视频下载速度。
# 腾讯等平台实测：单连接限速 ~1KB/s，但**单 IP 总带宽硬顶 ~18KB/s**（与并发数无关）。
# 16 并发已吃满该上限（VPS 实测：5并发=5KB/s, 16并发=18KB/s, 32/64/aria2c 均未突破）。
# 通过 VDL_CONCURRENT_FRAGMENTS 环境变量或下载请求字段可调（1-64，腾讯以外平台可能受益于更高值）。
CONCURRENT_FRAGMENTS = int(os.environ.get("VDL_CONCURRENT_FRAGMENTS", "16") or 16)
# 可选的外部下载器：aria2c 对大量小 .ts 分片可开更多并行连接，某些平台比内置并发上限更高。
# 需本机已安装 aria2c（打包 app 运行时依赖 PATH 上的 aria2c，缺失则自动回退原生下载器）。
# 通过 VDL_DOWNLOADER 环境变量或下载请求字段切换（值为 "aria2c" 时启用）。
# B4（2026-10-02）：默认 auto —— 本机 PATH 或打包内置（Resources/bin/aria2c）有 aria2c
# 就启用（HTTP 直链多连接分段提速），没有则静默回退原生。显式 native/aria2c 仍可经
# VDL_DOWNLOADER 环境变量或下载请求字段 downloader_type 强制。
VDL_DOWNLOADER = (os.environ.get("VDL_DOWNLOADER") or "auto").strip().lower()
_MAX_CONCURRENT = 64  # 单任务并发上限，防止被腾讯封总连接数

# ---------------------------------------------------------------------------
# 海外站「直连可达」探测：有 VPN（TUN 透明接管 / 系统代理）时应本机直连下载
# （速度 = VPN 速度），只有确实出不去才转发香港节点兜底。
# 探测结果按 host 缓存 60 秒，避免批量任务反复握手。
# ---------------------------------------------------------------------------
_DIRECT_REACH_CACHE: dict = {}
_DIRECT_REACH_LOCK = threading.Lock()
_DIRECT_REACH_TTL = 60.0        # 缓存秒数
_DIRECT_REACH_TIMEOUT = 2.5     # 单次握手超时

def can_download_directly(host: str) -> bool:
    host = (host or "").strip().lower()
    if not host:
        return False
    now = time.time()
    with _DIRECT_REACH_LOCK:
        hit = _DIRECT_REACH_CACHE.get(host)
        if hit and now - hit[0] < _DIRECT_REACH_TTL:
            return hit[1]
    ok = False
    # 1) 系统级代理（macOS/Windows 系统代理设置）→ yt-dlp 会自动沿用，本机即可出海。
    #    🔴 只认系统配置，**忽略进程环境变量**：从终端/工具链启动时 HTTP_PROXY 常被
    #    注入本地服务代理（127.0.0.1:xxxx），并不代表用户真开了 VPN，据此误判会
    #    把海外任务留在本机然后卡死。Finder/Dock 正常启动无此问题。
    try:
        import urllib.request as _ur
        env_proxies = _ur.getproxies_environment()
        sys_proxies = {k: v for k, v in _ur.getproxies().items() if env_proxies.get(k) != v}
        if sys_proxies:
            ok = True
    except Exception:  # noqa: BLE001
        pass
    # 2) TLS 握手探测（覆盖 TUN 模式与真裸连）。必须带 TLS：GFW 的 DNS 污染
    #    常给出能完成 TCP 握手的假 IP，只有证书校验通过的 TLS 才算真连通。
    if not ok:
        try:
            import socket as _socket
            import ssl as _ssl
            raw = _socket.create_connection((host, 443), timeout=_DIRECT_REACH_TIMEOUT)
            raw.settimeout(_DIRECT_REACH_TIMEOUT)
            ctx = _ssl.create_default_context()
            tls = ctx.wrap_socket(raw, server_hostname=host)
            tls.close()
            ok = True
        except Exception:  # noqa: BLE001
            ok = False
    with _DIRECT_REACH_LOCK:
        _DIRECT_REACH_CACHE[host] = (now, ok)
    return ok

def _clamp_concurrency(value: int) -> int:
    if not value or value < 1:
        return CONCURRENT_FRAGMENTS
    return max(1, min(_MAX_CONCURRENT, int(value)))

def _is_windows() -> bool:
    """当前是否 Windows。抽成函数是为了让 offline 测试能注入平台而不真跑 Windows。"""
    return os.name == "nt"


def _aria2c_path() -> str | None:
    """返回 aria2c 可执行路径；未安装返回 None（调用方回退原生下载器）。

    查找顺序：PATH（本机 brew/apt 安装）→ 打包内置（PyInstaller 冻结的 Resources/bin/aria2c，
    或 macOS .app 的 Contents/Resources/bin/aria2c）。找到打包内置版时把它所在目录前置到
    os.environ["PATH"]，确保 yt-dlp 的 subprocess 能按名检索到（yt-dlp 仅按名调用外部下载器）。
    """
    import shutil

    found = shutil.which("aria2c")
    if found:
        return found
    candidates = []
    # 🔴 Windows 的可执行文件必须带 .exe（打包内置同样是 bin/aria2c.exe）。
    # 旧版这里写死无后缀的 "aria2c"，在 Windows 上 os.path.isfile 永远 False
    # ⇒ 即使包里带着 aria2c 也永远当「未安装」。
    _sfx = ".exe" if _is_windows() else ""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "bin", "aria2c" + _sfx))
    exe = sys.executable
    if exe:
        # macOS .app: Contents/MacOS/VideoDownloader -> ../Resources/bin/aria2c
        candidates.append(os.path.join(os.path.dirname(exe), "..", "Resources", "bin",
                                       "aria2c" + _sfx))
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            bin_dir = os.path.dirname(os.path.abspath(c))
            # ⚠️ 必须先判重再前置：本函数**每个下载任务都会调用一次**，而它改的是
            # 进程级 os.environ["PATH"]。无去重时每下载一次就把该目录再前置一遍
            # （实测每次 +89 字节），几十个任务后 PATH 会膨胀成一大串重复项，
            # 拖慢所有功能的子进程按名检索，并可能触达系统 PATH 长度上限。
            # 2026-09-16 修复。
            _parts = os.environ.get("PATH", "").split(os.pathsep)
            if bin_dir not in _parts:
                os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
            return c
    return None

def _build_aria2c_args(concurrency: int) -> list[str]:
    n = str(_clamp_concurrency(concurrency))
    # -x 每服务器最大连接 / -s 分片数 / -j 整体并行下载数 / -k 最小分片大小
    #
    # --enable-cookie=true 必须显式打开：aria2 默认关闭 cookie 支持，此时它把
    # yt-dlp 传进来的 --header="Cookie: ..." 收进内部 cookieStorage 却**不发送**，
    # 请求头里根本没有 Cookie 一行 ⇒ 需要登录态的源（百度网盘直链等）直接 403。
    # 2026-10-24 实测：不加该开关 d.pcs.baidu.com 返回 403 且抓包无 Cookie 头，
    # 加上后 Cookie 正常随请求发出。
    return [
        "-x", n, "-s", n, "-j", n, "-k", "1M",
        "--continue=true", "--max-tries=5", "--enable-cookie=true",
    ]


# —— 百度网盘直链（2026-10-24 本机实测）——
# 限速是服务端按**账号总带宽**给的，不是按连接：1 连接 78.8 KB/s，4/8/16 连接
# 80.9/61.0/36.6 KB/s（8/16 连还会被掐断、拿不满字节）。多连接在这里是纯负优化。
# 但**客户端身份**能拿到更高限速档：curl 默认 UA 77 KB/s、Chrome 81 KB/s，而
# 老 PC 客户端的 P2SP 标识 ~145 KB/s（1.5MB 交替 A/B 两轮一致）。
# ⌁ 同一 UA 复测出现过 166 / 66 的跳变，疑似「账号+UA 分桶、桶内配额用多降档」，
#   所以这是榨取而非破解，对外不承诺固定倍数。
# ⇒ 网盘直链一律：单连接（禁 aria2c、禁分片并发）+ P2SP UA。
_NETDISK_UA = "netdisk;P2SP;2.2.60.26"
# 🔴 下载直链**不能**用上面那个 P2SP 身份（2026-10-24 实测，同一链接交替验证）：
#   P2SP UA + Range → 403 {"error_code":31326,"user is not authorized"}
#   浏览器 UA + Range → 206 正常，SVIP 账号 3.70 MB/s（非会员 ~120 KB/s，31 倍）
# 而且**必须带 Range**：不带 Range 的裸 GET 一律 403，错误码会伪装成
# 31326「无权限」或 31360「直链过期」——极具误导性，别再往"账号权限/私密分享"方向查。
_NETDISK_DOWNLOAD_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_NETDISK_HOST_RE = re.compile(r"(?:^|\.)(?:baidupcs\.com|pcs\.baidu\.com)$")

# 🔴 UA 必须**运行时协商**，不能写死（2026-10-24 两次实测结果相反）：
#   · 10-24 白天（SVIP 账号）：浏览器 UA + Range → 206 / 3.70 MB/s；
#                               P2SP UA + Range → 403 31326
#   · 10-24 晚上（非会员账号）：P2SP UA + Range → 206 / 123 KB/s；
#                               浏览器 UA + Range → 403 31326
# 结论：百度按「账号 + UA」分桶，哪个 UA 能过取决于登录的是哪个账号。
# 所以这里同时准备两种身份，先探 1 字节确认哪个可用，再拿它下发正式请求；
# 并记住上次成功的那一个，避免每次下载都多花一次探测请求（百度有频控）。
_NETDISK_UA_CANDIDATES: tuple[str, ...] = (_NETDISK_DOWNLOAD_UA, _NETDISK_UA)
_netdisk_ua_hint: str = ""


def _pick_netdisk_ua(url: str, cookie: str, proxy: str) -> str:
    """探测哪个 UA 能取到百度直链（返回可用的 UA，全失败则回默认）。

    用 `Range: bytes=0-0`（1 字节）探，代价极小；命中后记进进程级 hint，
    同一台机器后续下载直接复用，不再重复探测。
    """
    global _netdisk_ua_hint
    candidates = list(_NETDISK_UA_CANDIDATES)
    if _netdisk_ua_hint in candidates:      # 上次成功的优先
        candidates.remove(_netdisk_ua_hint)
        candidates.insert(0, _netdisk_ua_hint)
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        import requests
    except Exception:  # pragma: no cover
        return candidates[0]
    for ua in candidates:
        try:
            h = {"User-Agent": ua, "Range": "bytes=0-0"}
            if cookie:
                h["Cookie"] = cookie
            r = requests.get(url, headers=h, proxies=proxies, stream=True,
                             timeout=(10, 20), allow_redirects=True)
            ok = r.status_code in (200, 206)
            detail = ""
            if not ok:
                try:
                    detail = r.text[:120]
                except Exception:
                    pass
            r.close()
            if ok:
                _netdisk_ua_hint = ua
                logger.info("[网盘] 直链 UA 协商成功：%s", ua)
                return ua
            logger.info("[网盘] UA 不可用（%s）：%s %s", ua[:24], r.status_code, detail)
        except Exception as exc:  # noqa: BLE001
            logger.info("[网盘] UA 探测异常（%s）：%s", ua[:24], str(exc)[:80])
    return candidates[0]


def _is_netdisk_direct(url_or_host: str) -> bool:
    """是否百度网盘直链（d.pcs.baidu.com → 302 → *.baidupcs.com）。"""
    h = (url_or_host or "").strip().lower()
    if "://" in h:
        h = _host_of(h) or ""
    return bool(h) and bool(_NETDISK_HOST_RE.search(h))

def _has_partial(workdir: Path | None) -> bool:
    """工作目录里是否残留可续传的部分文件（.part / .aria2 控制文件 / .FragN 分片）。"""
    if not workdir or not workdir.is_dir():
        return False
    try:
        for p in workdir.iterdir():
            if not p.is_file():
                continue
            name = p.name
            if name.endswith(".part") or name.endswith(".aria2") or ".Frag" in name or name.endswith(".ytdl"):
                return True
        return False
    except OSError:
        return False

def _fmt_gb(n: float) -> str:
    """字节数的人类可读表示（预检报错文案用）。"""
    for unit, factor in (("B", 1), ("KB", 1024), ("MB", 1024**2), ("GB", 1024**3)):
        if n < factor * 1024 or unit == "GB":
            return f"{n / factor:.1f} {unit}" if unit != "B" else f"{int(n)} B"
    return f"{n:.0f} B"

def _estimate_info_bytes(info: dict[str, Any]) -> int:
    """从解析结果估算下载总体积（字节，0=未知）。

    口径：requested_formats（DASH 双流取和）→ filesize/filesize_approx 逐级兜底。
    HLS 清单通常拿不到整卷体积，返回 0，由「磁盘余量预检」按已知信息兜底。
    """
    if not isinstance(info, dict):
        return 0
    total = 0
    fmts = info.get("requested_formats") or []
    if fmts:
        for f in fmts:
            total += int(f.get("filesize") or f.get("filesize_approx") or 0)
        return total
    return int(info.get("filesize") or info.get("filesize_approx") or 0)

def _preflight_space_check(task: DownloadTask, info: dict[str, Any]) -> None:
    """下载前预检：体积上限与磁盘余量（B1，2026-10-02）。

    上限超了/磁盘不够都在**开下之前**给出人话错误，而不是让 yt-dlp 半途
    静默跳过（max-filesize 超限是「无声无果」）或 ENOSPC 死在 90%。
    体积未知（HLS）时只查磁盘「绝对紧张」兜底（余量 < 1GB 直接拦）。
    """
    est = _estimate_info_bytes(info)
    if _MAX_FILE_BYTES and est > _MAX_FILE_BYTES:
        raise ResolveError(
            f"视频约 {_fmt_gb(est)}，超过当前下载上限 {_MAX_FILE_MB} MB",
            "可在启动参数设置环境变量 VDL_MAX_FILE_MB 调高上限，或选择更低清晰度。",
            category="too_large",
        )
    workdir = task.workdir
    if not workdir:
        return
    try:
        free = shutil.disk_usage(workdir).free
    except OSError:
        return
    margin = 512 * 1024 * 1024   # 合并/封装还要一份临时空间，留 512MB 余量
    if est:
        need = est + margin
    else:
        need = 1024 * 1024 * 1024  # 体积未知：至少要求 1GB 可用
    if free < need:
        raise ResolveError(
            f"磁盘空间不足：预计需要 {_fmt_gb(need)}，当前仅剩 {_fmt_gb(free)}",
            "请清理磁盘空间或选择更低清晰度后重试。",
            category="disk_full",
        )

MAX_TITLE_CHARS = 80
MAX_HINT_CHARS = 180
DOWNLOAD_PHASE_CEILING = 97.0  # 下载阶段最多显示到 97%，剩余留给合并/转码

# 直链透传：用户贴的是单个可直接下载的媒体文件（.mp4 等）时，让前端直接从源站
# 把文件拉到本地，跳过服务器落盘与带宽消耗（真正只下一遍）。
# ⚠️ 不要把 m3u8 放进来。HLS 清单不是「媒体文件」而是分片索引：一旦命中，
# probe() 会在 _looks_like_direct_file() 短路（跳过 yt-dlp 直接当直链），
# 前端再把 manifest 当文件存下来 ⇒ 用户得到一个几百字节的废文本。
# 2026-09-27 实测：粘贴裸 m3u8 链接即可复现（is_hls=True 的同时 direct_url=该 m3u8）。
_DIRECT_EXT_RE = re.compile(
    r"\.(mp4|webm|m4a|mp3|mov|mkv|ogg|flac|avi|wmv|m4v|ts|flv|f4v)(\?|#|$|&)", re.IGNORECASE
)
# 这些域名即使是媒体扩展名结尾，也属于需经 yt-dlp 解析的平台，不能用直链透传绕过
_KNOWN_PLATFORM_HOSTS = {
    "bilibili.com", "b23.tv", "douyin.com", "tiktok.com", "tiktokv.com",
    "youtube.com", "youtu.be", "twitch.tv", "twitter.com", "x.com",
    "vimeo.com", "facebook.com", "instagram.com", "weibo.com", "qq.com",
    "v.qq.com", "iqiyi.com", "youku.com", "chrqj.com",
}

def _looks_like_direct_file(url: str) -> str | None:
    """若 URL 指向单个可直接下载的媒体文件（非已知平台），返回该 URL，否则 None。"""
    host = _host_of(url)
    if not host or host in _KNOWN_PLATFORM_HOSTS or is_china_host(host):
        return None
    if _DIRECT_EXT_RE.search(urlparse(url).path or ""):
        return url
    return None

def _cache_user_cookie(host: str, cookie: str) -> None:
    """把用户在「高级选项」手动粘贴的 Cookie 持久化到本地缓存。

    这样同站点后续解析/下载自动带登录态，不必每次重粘。
    复用 cookie_cache 模块（chmod 600、30 天 TTL、仅本机），合规且不外传。
    """
    try:
        from cookie_cache import _save
        text = cookie.strip()
        if text.lower().startswith("cookie:"):
            text = text[7:].strip()
        if text:
            _save(host, text)
    except Exception:
        pass

def _detect_direct_url(info: dict[str, Any]) -> str | None:
    """yt-dlp 解析结果若本身就是单个可直接下载的媒体文件，返回其直链。"""
    if not info.get("direct"):
        return None
    url = info.get("url") or ""
    if not url:
        return None
    protocol = (info.get("protocol") or "").split("+")[0].lower()
    if protocol not in ("http", "https", ""):
        return None
    # HLS 清单永远不是「可直取的文件」：manifest 只是分片索引，存下来是废文本。
    # 护栏有双重意义 —— 既挡住 m3u8 扩展名，也挡住「无扩展名但实际是清单」的源
    # （此时 protocol 常为 m3u8_native，上面那行已拦；这里再兜一次 _is_hls_url）。
    if protocol in ("m3u8", "m3u8_native") or _is_hls_url(url):
        return None
    if _DIRECT_EXT_RE.search(url) or _DIRECT_EXT_RE.search(f".{info.get('ext') or ''}"):
        return url
    return None

ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")
NOISE_PATTERN = re.compile(
    r"(please report this issue.*|Confirm you are on the latest version.*|"
    r"\s*;?\s*filling out the appropriate issue template.*)",
    re.IGNORECASE | re.DOTALL,
)

QUALITY_PRESETS: tuple[tuple[int, str], ...] = (
    (2160, "4K 2160P"),
    (1440, "2K 1440P"),
    (1080, "1080P 高清"),
    (720, "720P 高清"),
    (480, "480P 标清"),
    (360, "360P 流畅"),
)

BEST_KEY = "best"
AUDIO_KEY = "audio"
WEBM_KEY = "webm"
M4A_KEY = "m4a"

class ResolveError(LinkError):
    """解析阶段失败（链接失效、需要登录、地区限制等）。"""

class ResolveRestricted(LinkError):
    """视频疑似会员专享 / 付费 / 地区限制，yt-dlp 拿不到真实数据流。

    与 ResolveError 的区别：这类链接 yt-dlp 往往不报错，而是回填占位符
    元数据（标题形如 "vqq-video video #xxx"、时长为空），属于确认无解的受限内容。
    """

class DownloadCanceled(Exception):
    """用户主动取消下载。"""

class DownloadPaused(Exception):
    """用户暂停下载——保留 .part 文件，后续可断点续传。"""

# --------------------------------------------------------------------------- #
# 信息解析
# --------------------------------------------------------------------------- #

# —— 强反爬平台：服务端校验游客 Cookie（如抖音 s_v_web_id），匿名请求直接被拒 ——
# 这类平台无法直接匿名下载，需从用户已登录/访问过的浏览器读取 Cookie。
# 一旦检测到本机浏览器含该站 Cookie，VDL 自动注入，实现“粘贴链接即下”。
_COOKIE_HARDENED_DOMAINS: tuple[str, ...] = (
    "douyin.com", "iesdouyin.com",
    "kuaishou.com", "chenzhongtech.com", "gifshow.com",
    "xiaohongshu.com", "xhslink.com", "xhslink.cn",
    "tiktok.com", "instagram.com",
    # 腾讯视频：限免/会员视频走另一套播放 API，需要登录态 cookie；
    # 加入后 app 会自动从本机浏览器读 cookie 并注入请求，提示用户粘贴。
    "v.qq.com",
    # 优酷：会员/受限视频需要登录态 Cookie，yt-dlp 解析时会报 -3007"请先登录"。
    # 加入 hardened 后桌面端自动读取浏览器 Cookie，云端可经公共池共享。
    "youku.com",
    # chrqj 影视聚合站（= 金牌影院老域名，首页 302 → nmwchxx.com，同一套
    # mw-movie API）：视频流（m3u8/ts CDN）校验播放页会话 Cookie，缺则拒绝。
    # 加入后自动从本机浏览器读该站 Cookie 并注入视频流请求头（无需手动粘贴）。
    # 🔴 两个域名都要列：站点已迁到 nmwchxx.com，只认老域名等于新域名的登录态
    # 桌面端不会自动读（2026-10-21 收编时补）。
    "chrqj.com",
    "nmwchxx.com",
)
# 注意：YouTube **故意不列入** hardened —— hardened 会把 403 文案改写成
# 「该站需要登录 Cookie」，从而盖掉 YouTube 403 真正该排查的代理/地区线索
# （见 tests/test_friendly_error_403.py::test_youtube_403_is_cdn_forbidden）。
# 但 YouTube 的 Cookie 又必须能进公共池（否则桌面端推来会被 400 拒收），
# 因此走 cookie_pool._POOL_EXTRA_DOMAINS 单独放行，两件事互不干扰。

# 候选浏览器来源改为跨平台表（真源见 browser_cookies.py）。
#
# 2026-10-24 根因修复：这里原先写死 macOS 的 4 条路径
# （~/Library/Application Support/...）。在 Windows 上这些 glob 全部落空，
# 于是「自动读取浏览器登录态」在 Windows 端**一次都没生效过** ——
# 百度网盘解析只能报「请填写 BDUSS」、B站/YouTube 也拿不到登录态，
# 全是这一处导致的，不是网络或账号问题。
#
# 新表覆盖 Chromium 系 + 国产壳（UC/360/QQ/搜狗/2345）+ Firefox，并且
# **优先当前正在运行的浏览器**（用户此刻登录的 profile 才是有效会话）。
# 下方保留派生视图仅供旧引用兼容，新代码请直接用 browser_cookies。
try:
    import browser_cookies as _browser_cookies
except Exception:  # pragma: no cover
    _browser_cookies = None  # type: ignore[assignment]

# 🔴 2026-10-28 修复：必须展开**全部** roots，不能只取 roots[0]。
# roots 是跨平台三元组（Windows / macOS / Linux），roots[0] 恒为 `%LOCALAPPDATA%\...`，
# 在 macOS / Linux 上既不会展开也 glob 不到 ⇒ 派生出空表 ⇒ 下游
# `app._cookie_db_signature()` 返回 `{}` ⇒ `/api/cookie/sync/auto` 的
# 「浏览器登录态未变化就 skipped」永远不成立 ⇒ **每轮全量解密 + 推送**。
# 实测代价（2026-10-28 本机 Mac）：2 小时 934 次 /api/cookie/sync，其中 218 次撞
# 429 限流（23%），且每轮都要读钥匙串解 Chrome Cookies。Windows 上因 roots[0] 正好
# 命中而看不出问题——这是典型的「只在 Mac/Linux 复现」的跨平台漏写。
_BROWSER_COOKIE_PROFILES: tuple[tuple[str, str], ...] = tuple(
    (s["key"], root)
    for s in (_browser_cookies._BROWSER_SOURCES if _browser_cookies else ())
    for root in s["roots"]
)


def cookie_db_paths() -> list[str]:
    """本机所有浏览器 Cookies **数据库文件**的绝对路径（跨平台去重）。

    🔴 别用 `_BROWSER_COOKIE_PROFILES` 里的 root 目录做「登录态变化」快照：
    root 是**目录**，而 Cookie 是**原地写**进 db 文件的 —— 只改内容不改父目录
    mtime 与 size，拿目录做签名会恒判「未变化」，该推的新登录态永远推不上去。
    必须精确到 db 文件（`<profile>/Cookies` 或 `<profile>/Network/Cookies`）。

    实现委托 `browser_cookies.iter_profiles()`：它已处理 Chromium 新旧两代布局、
    Firefox 的 `cookies.sqlite`、Opera 这类 root 即 profile 的差异，且遍历全部
    跨平台 roots。本机实测 5 个库 / 3.5ms，可放在每轮同步里跑。
    """
    if _browser_cookies is None:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for src in _browser_cookies._BROWSER_SOURCES:
        try:
            for _profile, db in _browser_cookies.iter_profiles(src):
                if db not in seen:
                    seen.add(db)
                    out.append(db)
        except Exception:  # noqa: BLE001 — 单个浏览器探测失败不能拖垮整体签名
            continue
    return out

# yt-dlp 认识的浏览器名（其余如 UC/360/QQ 只能走自解密）
_YTDLP_BROWSERS: frozenset[str] = frozenset(
    {"brave", "chrome", "chromium", "edge", "firefox", "opera", "safari", "vivaldi", "whale"}
)

def is_cookie_hardened_host(host: str) -> bool:
    """判断是否为需要浏览器 Cookie 才能解析的强反爬平台。"""
    host = (host or "").lower()
    return any(host == d or host.endswith(f".{d}") for d in _COOKIE_HARDENED_DOMAINS)

def _detect_browser_cookie_source() -> str | None:
    """探测本机已安装 Cookie 库的浏览器，返回浏览器名（优先正在运行的）。

    用于「浏览器装了但该站没登录」时的提示文案，让用户知道去哪个浏览器登录。
    """
    if _browser_cookies is None:
        return None
    try:
        installed = _browser_cookies.list_installed_browsers()
    except Exception:
        return None
    if not installed:
        return None
    # 正在运行的排前面：提示用户「去你正在用的那个浏览器登录」最省事
    installed.sort(key=lambda x: (not x.get("running"), x.get("browser") or ""))
    return installed[0].get("browser")

def _root_domain(host: str) -> str:
    """取根域：v.qq.com → qq.com；www.douyin.com → douyin.com；a.b.com.cn → b.com.cn。"""
    parts = (host or "").strip().lower().split(".")
    if len(parts) <= 2:
        return (host or "").strip().lower()
    if len(parts) >= 3 and parts[-2] in ("com", "net", "org", "gov", "edu", "co") \
            and parts[-1] in ("cn", "hk", "tw", "jp", "uk", "kr", "sg"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])

# ── CDN 域名 → Cookie 登录域映射 ──────────────────────────────────────
# 部分平台的视频流 URL 托管在独立 CDN 域（如 bilivideo.cn），但浏览器
# Cookie 存在主站域（如 bilibili.com）。_find_host_cookie_profile 按播放
# URL 的 host 查 Cookie 会落空，需回溯到正确的登录域。
_CDN_TO_COOKIE_DOMAIN: dict[str, str] = {
    # B 站：视频 CDN 在 *.bilivideo.[com|cn]，Cookie 在 bilibili.com
    "bilivideo.com": "bilibili.com",
    "bilivideo.cn": "bilibili.com",
    # YouTube：用户粘贴的常是 youtu.be 短链，但登录 Cookie 全在 youtube.com 域
    # （2026-09-22 实测：youtu.be 链接按 youtu.be 查 Cookie → 0 命中 → 不带登录态
    # 转发对端 → 误报「需要登录 Cookie」；www.youtube.com 链接则一切正常）
    "youtu.be": "youtube.com",
    # 分享域 / 极速版域不承载登录态，登录态在主站（2026-10-21 实测浏览器 Cookie 库）：
    #   .chenzhongtech.com → did,didv            （快手极速版，无 passToken）
    #   .iesdouyin.com     → ttwid               （抖音分享页，无 sessionid）
    # 直接拿它们自己的 Cookie 去解析 = 匿名请求（低清晰度 / 会员内容失败）。
    "chenzhongtech.com": "kuaishou.com",
    "gifshow.com": "kuaishou.com",
    "iesdouyin.com": "douyin.com",
    # B 站短链（云端池只存 bilibili.com，原 _base_options 里是硬编码 if）
    "b23.tv": "bilibili.com",
}

def _cookie_domains_for_host(host: str) -> list[str]:
    """返回用于查找 Cookie 的候选域名列表（含 CDN→登录域回溯）。

    优先返回 host 自身的根域，再追加映射的登录域（去重）。
    """
    root = _root_domain(host)
    candidates = [root] if root else []
    mapped = _CDN_TO_COOKIE_DOMAIN.get(root)
    if mapped and mapped not in candidates:
        candidates.append(mapped)
    return candidates

def _cookie_login_host(host: str) -> str:
    """把 URL host 归一成「真正存登录态的那个域」。

    短链 / 分享域 / 极速版域自身不带登录态（只有设备指纹），Cookie 全在主站：
      b23.tv            → bilibili.com
      chenzhongtech.com → kuaishou.com （只 did/didv）
      iesdouyin.com     → douyin.com   （只 ttwid）
    取 Cookie 必须换到主站域，否则拿到的是匿名设备指纹，等于没登录。
    映射表单一真源：_CDN_TO_COOKIE_DOMAIN。
    """
    h = (host or "").strip().lower()
    if not h:
        return host
    mapped = _CDN_TO_COOKIE_DOMAIN.get(_root_domain(h))
    return mapped or host


# ────────────────────────────────────────────────────────────────────────────
# 云端共享池回读（桌面端，2026-10-25）
#
# 背景：桌面 App 的本机池目录（~/.videodownloader/cookie_pool）在从未喂过池的
# 机器上**根本不存在** ⇒ 本机池恒 miss ⇒ 桌面用户没在浏览器登录的站一律解析
# 失败（实测小红书必败），而 VPS/Railway 上的云端池其实是满的（守护进程每
# ~20 分钟收割推送）。这里让桌面端在本机池 miss 时回读云端池，把「共享登录态」
# 也惠及桌面用户，与网页版口径一致。
#
# 通道：POST {云端}/api/cookie/pool-admin {"action":"get","domain":…,"token":…}
#   —— 与桌面管理后台看池概览同一把令牌（VDL_COOKIE_SYNC_TOKEN，该令牌本就
#      允许 inject 写池，读不扩大暴露面），云端只放行白名单域。
#   —— 旧版云端（无 get 动作）返回 400 ⇒ 视为 miss，绝不影响解析主流程。
#
# 缓存三档：命中 5 分钟 / 未命中 1 分钟（负缓存）/ 云端整体不可达全局冷却 60s
#   —— 否则每次解析都去吃一次 6s 连接超时，拖垮路由层超时预算。
# 代理：显式禁用环境代理。云端是国内 IP（ECS），桌面进程常继承 clash/系统
#   代理，走代理访问自家节点必被 407 拦（与 _call_vps_worker 同坑）。
# ────────────────────────────────────────────────────────────────────────────
_CLOUD_POOL_HIT_TTL = 300     # 命中缓存 5 分钟（池约 20 分钟轮换，够新鲜）
_CLOUD_POOL_MISS_TTL = 60     # 未命中/失败负缓存 1 分钟
_CLOUD_POOL_TIMEOUT = 6       # 国内 VPS 单程 6s 足够；再慢当作不可达
_CLOUD_POOL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
# domain -> (ts, header, ckey)；header/ckey 允许其一为空（优酷可能只有 ckey）
_cloud_pool_mem: dict[str, tuple[float, str, str]] = {}
_cloud_pool_down_until: float = 0.0   # 云端整体不可达的全局冷却截止点


def _cloud_pool_cfg() -> tuple[str, str]:
    """云端池地址与令牌：env 优先，回落 ~/.videodownloader/cloud_sync.json。"""
    url = (os.environ.get("VDL_COOKIE_SYNC_URL") or "").strip()
    token = (os.environ.get("VDL_COOKIE_SYNC_TOKEN") or "").strip()
    if not url or not token:
        try:
            cfg = Path.home() / ".videodownloader" / "cloud_sync.json"
            if cfg.exists():
                d = json.loads(cfg.read_text(encoding="utf-8"))
                url = url or str(d.get("url") or "").strip()
                token = token or str(d.get("token") or "").strip()
        except Exception:
            pass
    return url, token


def _cloud_pool_fetch(domain: str) -> tuple[str, str]:
    """回读云端池一次，返回 (header, ckey)；任何失败都返回 ("","") 不抛。"""
    global _cloud_pool_down_until
    domain = (domain or "").strip().lower()
    if not domain or _requests is None:
        return "", ""
    if time.time() < _cloud_pool_down_until:
        return "", ""
    url, token = _cloud_pool_cfg()
    if not url or not token:
        return "", ""
    try:
        r = _requests.post(
            url.rstrip("/") + "/api/cookie/pool-admin",
            json={"action": "get", "domain": domain, "token": token},
            headers={"User-Agent": _CLOUD_POOL_UA},
            timeout=_CLOUD_POOL_TIMEOUT,
            proxies={"http": None, "https": None},
        )
        if r.status_code != 200:
            # 旧版云端无 get 动作（400）/ 令牌失效（403）⇒ 当作没有，不报警不重试
            _cookie_diag("cloud_pool_http", f"domain={domain} code={r.status_code}")
            return "", ""
        d = r.json() or {}
        if not d.get("ok"):
            return "", ""
        header = str(d.get("header") or "")
        ckey = str(d.get("ckey") or "")
        if header:
            _cookie_diag("cloud_pool_hit", f"domain={domain} len={len(header)}")
        return header, ckey
    except Exception as e:
        # 云端不可达：全局冷却 60s，避免此后每个域每种解析都去吃一次连接超时
        _cloud_pool_down_until = time.time() + 60
        _cookie_diag("cloud_pool_error", f"domain={domain} {str(e)[:120]}")
        return "", ""


def _cloud_pool_cookie(domain: str) -> str:
    """云端池 Cookie（TTL 缓存；桌面端本机池 miss 时调用）。"""
    domain = (domain or "").strip().lower()
    if not domain:
        return ""
    now = time.time()
    item = _cloud_pool_mem.get(domain)
    if item:
        ts, header, _ck = item
        if now - ts < (_CLOUD_POOL_HIT_TTL if header else _CLOUD_POOL_MISS_TTL):
            return header
    header, ckey = _cloud_pool_fetch(domain)
    _cloud_pool_mem[domain] = (now, header, ckey)
    return header


def _cloud_pool_ckey(domain: str) -> str:
    """云端池优酷 ckey（与 Cookie 同一次回读，共享 TTL 缓存）。"""
    domain = (domain or "").strip().lower()
    if not domain:
        return ""
    now = time.time()
    item = _cloud_pool_mem.get(domain)
    if item:
        ts, _h, ckey = item
        if now - ts < (_CLOUD_POOL_HIT_TTL if _h else _CLOUD_POOL_MISS_TTL):
            return ckey
    header, ckey = _cloud_pool_fetch(domain)
    _cloud_pool_mem[domain] = (now, header, ckey)
    return ckey


def _pooled_cookie(domain: str) -> str:
    """共享登录态单一入口：本机公共池 → 云端池回读，任一成功即返回。"""
    try:
        from cookie_pool import get_cookie as _pool_get
        header = _pool_get(domain) or ""
    except Exception:
        header = ""
    if header:
        return header
    return _cloud_pool_cookie(domain)


def _pooled_ckey(domain: str) -> str:
    """优酷 ckey 单一入口：本机公共池 → 云端池回读。"""
    try:
        from cookie_pool import get_ckey as _pool_ckey_get
        ck = _pool_ckey_get(domain) or ""
    except Exception:
        ck = ""
    if ck:
        return ck
    return _cloud_pool_ckey(domain)

def _find_host_cookie_profile(host: str) -> tuple[str, str] | None:
    """探测哪个浏览器的哪个 Profile 含有目标站点的 cookie，返回 (browser, profile)。

    2026-10-24 重写。原实现有两个硬伤：
      1. 路径表只有 macOS 的 ~/Library/...，Windows 上 glob 全落空 →
         永远返回 None，Windows 端「自动读登录态」从上线起就没生效过；
      2. 「第一个命中即返回」：同一站点多个 Profile 都有 Cookie 时容易挑到
         游客态/过期态（数据中心 IP 上的 YouTube 只认完整会话，拿游客态
         转发会被判 cookie_required）。

    现在统一交给 browser_cookies.find_source：
      - **正在运行的浏览器优先**（用户此刻登录的 profile 才是有效会话）；
      - 同档按命中 Cookie 数量打分（数量越多越可能是完整会话）；
      - 跨平台覆盖 Chromium 系 + 国产壳（UC/360/QQ/搜狗/2345）+ Firefox；
      - 支持 CDN 域回溯（bilivideo.cn → bilibili.com）。
    """
    domains = _cookie_domains_for_host(host)
    if not domains or _browser_cookies is None:
        return None
    try:
        src = _browser_cookies.find_source(domains)
    except Exception:
        return None
    if not src:
        return None
    return src["browser"], src["profile"]

def find_browser_cookie_source(host: str) -> dict[str, Any] | None:
    """返回可读的 Cookie 来源（浏览器/Profile/Cookie 数），供排障与前端展示。

    只返回**确实能读出来**的来源；「浏览器开着但库被锁」由 locked_browser_hint
    单独处理（那种情况无法确认库里有没有目标站 Cookie，不能当成已检测到登录态）。
    """
    domains = _cookie_domains_for_host(host)
    if not domains or _browser_cookies is None:
        return None
    try:
        return _browser_cookies.find_source(domains)
    except Exception:
        return None


def _user_error_text() -> str:
    """网盘类失败的统一上屏文案（唯一真源：cloud_drives/baidu.py 的 USER_ERROR_TEXT）。

    2026-10-27 乐懂定档：前端不再展示 errno / 账号等级 / 浏览器锁库等内部细节，
    一律只显示这一句；真实原因由各调用点写 logger，进运维监控。
    函数内 import 是刻意为之 —— 避免 downloader 在 server 启动早期就拉起
    cloud_drives 包（也避开潜在的 import 顺序问题）。
    """
    try:
        from cloud_drives.baidu import USER_ERROR_TEXT
        return USER_ERROR_TEXT
    except Exception:  # noqa: BLE001 - 兜底：失败路径本身绝不能再抛异常
        return "百度网盘接口返回异常（可能触发了风控，或分享链接已失效），请稍后重试"


def locked_browser_hint() -> str:
    """有浏览器正开着、但 Cookie 库读不出来时，返回可操作的提示语；否则空串。

    Windows 上只要 Chromium 系浏览器开着，Cookies 库就被独占（反 cookie-stealer），
    这是「读不到」而不是「没登录」，提示必须说清楚，否则用户会以为要重新登录。
    """
    if _browser_cookies is None:
        return ""
    try:
        locked = _browser_cookies.locked_browsers()
    except Exception:
        return ""
    if not locked:
        return ""
    names = "、".join(dict.fromkeys(x["label"] for x in locked))
    # 真实原因（浏览器锁库）只进日志；上屏统一文案（2026-10-27 定档）。
    logger.warning("[cookie] %s 正在运行并锁定了 Cookie 库，登录态读不出来", names)
    return _user_error_text()

def get_browser_cookie_header(host: str, url: str) -> str | None:
    """若本机浏览器含目标站点的登录 Cookie，提取并构造可用于请求头的 Cookie 字符串。

    返回 None 表示无可用 Cookie（浏览器未安装 / 该站未登录 / 解密失败 /
    库被占用）。两级取法：

      ① **自解密**（browser_cookies）：Windows 上这是唯一可行路径。原因有两层：
         - yt-dlp 在 Windows 读不了正在运行的 Chromium（upstream #7271，
           Chromium 对 Cookies 加独占锁，复制必然失败）；
         - UC / 360 / QQ / 搜狗 这些国产壳 yt-dlp 根本不认识。
         同时这条路径**优先挑正在运行的浏览器**，拿到的才是用户此刻的会话。
      ② yt-dlp 回退：macOS / Linux 的主路径（钥匙串 + PBKDF2 由它处理），
         Windows 上对「已关闭的浏览器」也有效。

    注意短链域（v.kuaishou.com）：登录 Session 和反爬 token 在 id./www. 主域，
    只按短链域匹配会拿到匿名设备指纹，故候选 URL 要铺开合并。
    """
    domains = _cookie_domains_for_host(host)
    if not domains:
        return None

    # ① 自解密
    if _browser_cookies is not None:
        try:
            src = _browser_cookies.find_source(domains)
            if src and src.get("status") == "ok":
                rows, reason = _browser_cookies.read_cookies(src["db"], src["kind"], domains)
                header = _browser_cookies.build_cookie_header(rows)
                if header:
                    _cookie_diag(
                        "browser_cookie_ok",
                        f"{src['browser']}/{src['profile']} running={src['running']} n={len(rows)}",
                    )
                    return header
                _cookie_diag("browser_cookie_empty", f"{src['browser']} reason={reason}")
            elif src:
                _cookie_diag(
                    "browser_cookie_blocked",
                    f"{src['browser']}/{src['profile']} status={src.get('status')}",
                )
        except Exception as exc:  # noqa: BLE001
            _cookie_diag("browser_cookie_error", str(exc)[:120])

    # ② yt-dlp 回退
    found = _find_host_cookie_profile(host)
    if not found:
        return None
    browser, profile = found
    if browser not in _YTDLP_BROWSERS:
        return None  # UC/360/QQ 等壳：yt-dlp 不认识，自解密已试过
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
        from urllib.request import Request
        jar = extract_cookies_from_browser(browser, profile)

        # 生成候选 URL 列表：原始域名 + 常见主域变体 + 登录域映射
        # 短链/CDN 域名的登录态在主域，必须合并才能拿到完整 Session
        parts = host.split(".")
        candidates = [url]
        # 映射出的登录域（如 youtu.be → youtube.com）也必须进候选，
        # 否则 add_cookie_header 按短链域匹配拿不到 .youtube.com 的登录 Cookie
        # （merged_names 按 name 去重，候选重复无副作用）
        # 🔴 `id.` 必带：快手把 passToken 放在 id.kuaishou.com（登录子域），
        # 只取 www./裸域会漏掉它 ⇒ 服务端回「sid 不合法」，整份 Cookie 白给。
        for d in domains:
            for sub in ("www", "id", "m"):
                candidates.append(f"https://{sub}.{d}/")
            candidates.append(f"https://{d}/")
        if len(parts) >= 2:
            base = ".".join(parts[-2:])  # e.g. kuaishou.com from v.kuaishou.com
            for sub in ("www", "id", "m"):
                c_url = f"https://{sub}.{base}/"
                if c_url != url:
                    candidates.append(c_url)

        merged_names: dict[str, str] = {}  # name → value (后者覆盖前者)
        for c_url in candidates:
            req = Request(c_url)
            jar.add_cookie_header(req)
            hdr = req.get_header("Cookie")
            if hdr:
                for pair in hdr.split("; "):
                    if "=" in pair:
                        name, _, val = pair.partition("=")
                        name = name.strip()
                        # 空名会拼出 `; =` 这种非法对，平台侧可能整份拒收
                        if name:
                            merged_names[name] = val.strip()

        return "; ".join(f"{n}={v}" for n, v in merged_names.items()) or None
    except Exception:
        return None

def detect_browser_cookie(host: str) -> dict[str, Any]:
    """探测本机浏览器是否含目标站点的 Cookie，供前端「检测登录态」按钮与解析结果展示。

    返回 status 让前端能给出**可操作**的提示，而不是笼统的「未检测到登录态」：
      ok / locked / abe / decrypt_failed / absent

    线上最常遇到的是 locked：Windows 上浏览器开着就独占锁住 Cookies 库
    （Chromium 反 cookie-stealer 设计），此时不是「没登录」而是「读不到」——
    只能提示用户关掉浏览器再试、或装我们的扩展直推（扩展在浏览器内，不受锁影响）。
    """
    domains = _cookie_domains_for_host(host)
    src = find_browser_cookie_source(host)
    if src and src.get("count", 0) > 0:
        rows: list[dict[str, Any]] = []
        reason = ""
        if _browser_cookies is not None:
            try:
                rows, reason = _browser_cookies.read_cookies(src["db"], src["kind"], domains)
            except Exception:
                pass
        label = src.get("label") or src.get("browser")
        if rows:
            return {
                "available": True,
                "browser": src.get("browser"),
                "label": label,
                "profile": src.get("profile"),
                "running": src.get("running", False),
                "count": len(rows),
                "status": "ok",
                "hint": "",
            }
        is_abe = reason == "abe_v20"
        # 真实原因（v20 加密 / 解密失败）只进日志；上屏统一文案（2026-10-27 定档）。
        logger.warning("[cookie] %s 有该站记录但解不出：status=%s reason=%s",
                       label, "abe_v20" if is_abe else "decrypt_failed", reason or "未知")
        return {
            "available": False,
            "browser": src.get("browser"),
            "label": label,
            "profile": src.get("profile"),
            "running": src.get("running", False),
            "count": src.get("count", 0),
            "status": "abe" if is_abe else "decrypt_failed",
            "hint": _user_error_text(),
        }
    # 没有可读来源：优先提示「开着但被锁」，因为这是 Windows 上最常见的情况
    hint = locked_browser_hint()
    if hint:
        locked = _browser_cookies.locked_browsers() if _browser_cookies else []
        return {
            "available": False,
            "browser": locked[0]["browser"] if locked else None,
            "label": "、".join(dict.fromkeys(x["label"] for x in locked)) if locked else None,
            "profile": None,
            "running": True,
            "count": 0,
            "status": "locked",
            "hint": hint,
        }
    # 浏览器装了、但该站无 Cookie：仍返回浏览器名，便于提示「请先在浏览器登录」
    b = _detect_browser_cookie_source()
    return {"available": False, "browser": b, "profile": None, "status": "absent", "hint": ""}


def _win_self_cookie_header(host: str) -> str:
    """Windows 专用：用自研 browser_cookies 读出目标站的 Cookie 头。

    为什么必须自己读、不能交给 yt-dlp 的 cookiesfrombrowser：
      · yt-dlp 复制 Chromium 的 Cookies 库时，浏览器开着就 PermissionError
        （upstream #7271），而它选不到浏览器时会**默认 chrome**，在本机
        （只装了 Edge/UC/QQ）直接抛
        "Could not copy Chrome cookie database"；
      · 该异常发生在提取器之前，等于把整次解析带崩 —— 2026-10-25 实测，
        小红书短链就是因为这个原因报成「短链无法解析」。
    读不到就返回空串：不带 Cookie 继续，让站点自己给出「需登录」的真实错误，
    比抛一个和站点无关的异常好排障。
    """
    domains = _cookie_domains_for_host(host)
    if not domains or _browser_cookies is None:
        return ""
    try:
        src = _browser_cookies.find_source(domains)
        if not src or src.get("status") != "ok":
            return ""
        rows, _reason = _browser_cookies.read_cookies(src["db"], src["kind"], domains)
        header = _browser_cookies.build_cookie_header(rows)
        if header:
            _cookie_diag(
                "cfb_win_self",
                f"{src['browser']}/{src['profile']} n={len(rows)} len={len(header)}",
            )
        return header or ""
    except Exception as exc:  # noqa: BLE001
        _cookie_diag("cfb_win_self_error", str(exc)[:160])
        return ""


def _cloud_pool_cookie(domain: str) -> str:
    """桌面端回读云端共享池：本地无该域 Cookie 时，向线上 pool-admin?action=get 取回明文。

    仅桌面端用（桌面本机池通常为空，共享登录态都在云端）。与 _push_cookie_to_cloud
    同口径：带浏览器 UA 绕过 Cloudflare 对 Python-urllib 的拦截。贡献域（baidu.com）
    不在此取——走网盘专属链路。任何异常都返回空串，调用方回退浏览器兜底，绝不阻断解析。
    """
    domain = (domain or "").strip()
    if not domain or "baidu.com" in domain:
        return ""
    import json as _json
    import urllib.request as _req
    import urllib.error as _err  # noqa: F401
    from pathlib import Path as _P
    url = (os.environ.get("VDL_COOKIE_SYNC_URL") or "").strip()
    token = (os.environ.get("VDL_COOKIE_SYNC_TOKEN") or "").strip()
    cfg = _P.home() / ".videodownloader" / "cloud_sync.json"
    if (not url or not token) and cfg.exists():
        try:
            d = _json.loads(cfg.read_text(encoding="utf-8"))
            url = url or (d.get("url") or "").strip()
            token = token or (d.get("token") or "").strip()
        except Exception:
            pass
    if not url or not token:
        return ""
    target = url.rstrip("/") + "/api/cookie/pool-admin"
    body = _json.dumps({"action": "get", "domain": domain, "token": token}).encode()
    req = _req.Request(
        target, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
        },
    )
    try:
        with _req.urlopen(req, timeout=15) as r:
            data = _json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return ""
    if data.get("ok") and data.get("found"):
        return (data.get("header") or "").strip()
    return ""


def _base_options(retries: int = DOWNLOAD_RETRIES, host: str = "", *, cookie: str = "", proxy: str = "") -> dict[str, Any]:
    _cookie_diag("base_options_enter", f"host={host!r} cookie_len={len(cookie)}")
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "noprogress": True,
        "socket_timeout": SOCKET_TIMEOUT,
        "retries": retries,
        "extractor_retries": retries,
        "fragment_retries": int(os.environ.get("VDL_FRAGMENT_RETRIES", "10")),
        "file_access_retries": int(os.environ.get("VDL_FILE_ACCESS_RETRIES", "10")),
        "skip_unavailable_fragments": True,
        "ignoreerrors": False,
        "continue": True,   # 断点续传：上次中断的 .part 可从断点接着下，大文件更稳
    }
    # 代理：用户显式传入优先；否则按平台自动策略（VDL_PROXY 环境变量 / 国内站直连 / macOS 系统代理）
    effective_proxy = proxy or _resolve_proxy(host)
    if is_china_host(host):
        # 国内站：海外部署（Railway 等）必须经 VDL_PROXY_CN 回源到国内出口，
        # 否则被地理围栏 403；本机在国内直连时该变量为空，显式置空避免 yt-dlp
        # 误读系统/环境变量里的海外代理导致超时/被拒。
        # 用户显式传入的 proxy 优先（高级选项 → 代理），否则用默认回源代理。
        options["proxy"] = proxy or _cn_proxy_url()
    elif effective_proxy:
        options["proxy"] = effective_proxy
        # 走代理时（Clash/V2Ray/Surge 等常做 HTTPS MITM 中间人解密），
        # 代理替换了 SSL 证书，必须跳过证书校验否则直接 SSL 握手失败
        options["no_check_certificates"] = True
    # 国内站（B站/抖音等）反爬严格：缺 Referer/UA 常被直接 412，无论是否带 cookie 都先补上浏览器请求头
    headers = options.setdefault("http_headers", {})
    if is_china_host(host):
        # 防盗链 Referer 必须用站点自身 origin（与在线观看代理 _stream_referer 一致）。
        # 注意 _host_of() 已剥掉 www. 前缀（host=bilibili.com），若直接 f"https://{host}/"
        # 会生成 https://bilibili.com/ —— bilibili API 校验 Referer 只认带 www 的
        # https://www.bilibili.com/，无 www 直接 403（"Unable to download JSON metadata"）。
        # 抖音也同理（www.douyin.com）。其余站用裸域 origin 即可（chrqj.com 等聚合站
        # 写死 bilibili.com 反而会拿到错误 Referer → CDN 403）。
        if "douyin" in host or "iesdouyin" in host:
            referer = "https://www.douyin.com/"
        elif "bilibili.com" in host or "b23.tv" in host:
            referer = "https://www.bilibili.com/"
        elif "iqiyi.com" in host:
            # 爱奇艺与 B站同理：页面/接口校验 Referer 只认带 www 的 https://www.iqiyi.com/
            referer = "https://www.iqiyi.com/"
        else:
            referer = f"https://{host}/"
        headers.setdefault("Referer", referer)
        headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        # B站 经国内代理回源时，偶发 IncompleteRead/连接重置：禁用 keep-alive + 关闭压缩，
        # 让代理/服务端按短连接完整传输页面 HTML，避免 chunked/gzip 半包问题。
        if host and ("bilibili.com" in host or "b23.tv" in host):
            headers.setdefault("Connection", "close")
            headers.setdefault("Accept-Encoding", "identity")
    # Cookie：用户粘贴的会话 Cookie（字符串）优先注入请求头，覆盖环境变量级的浏览器 Cookie
    cookie_text = cookie.strip()
    if cookie_text.lower().startswith("cookie:"):
        cookie_text = cookie_text[7:].strip()
    if cookie_text:
        # 容错：用户从 DevTools 复制的常是「裸值」（只复制了 SESSDATA 那一格的 Value，
        # 没有 SESSDATA= 前缀）。B站 用 SESSDATA 单键鉴权，检测到裸值自动补前缀，
        # 避免「明明填了 Cookie 还 403」的困惑。后端直接把该字符串当 Cookie 头发，
        # 裸值会被 B站 当作无效 Cookie 导致 403。
        is_bili = host and ("bilibili.com" in host or "b23.tv" in host)
        if is_bili and "=" not in cookie_text:
            cookie_text = f"SESSDATA={cookie_text}"
            _cookie_diag("cookie_bare_value_fixed", "bilibili bare SESSDATA auto-prefixed")
        headers["Cookie"] = cookie_text
    # YouTube 专用参数：player_client 选择。
    # 2026-09-21 曾强制 web_safari（当时与 bgutil PO Token 同 client 可用）。
    # 🔴 2026-09-26 实测（用户 VPN 出口 + yt-dlp 2026.08.19）：YouTube 端 SABR
    # 调整后 web_safari/mweb/web/tv 全部抛 "Requested format is not available"，
    # **只有不传 player_client（走 yt-dlp 默认客户端链）才能拿到完整格式列表
    # （实测 31 个格式，解析成功）**。故删除强制覆盖，交还 yt-dlp 默认。
    # 注意：yt-dlp 的 player_client 是「合并」模式而非「依次尝试」，
    # 多 client 列表会导致空 SABR 结果污染整体，必须只传一个。
    _is_yt = bool(host) and ("youtube.com" in host or "youtu.be" in host)
    if _is_yt:
        # YouTube 必须带 JS runtime + EJS 求解器，否则 nsig 挑战无解 ——
        # 带登录态会卡在 "The page needs to be reloaded"，不带则被 bot 拦截。
        # 实测矩阵与依赖来源见 _js_challenge_options 上方注释。
        options.update(_js_challenge_options())
        # 提速：player_skip=configs 跳过「为每个非 webpage 客户端额外下载一次 ytcfg」的请求。
        # 2026-09-27 香港节点实测（yt-dlp 2026.8.19，默认客户端链，同一视频跑两遍）：
        #   格式数 49→49、协议分布 mhtml×4 + m3u8_native×17 + https×28 完全不变
        #   解析耗时 5.54s→4.18s / 4.11s→4.43s（噪声内，不劣化）
        #   ⇒ 少一次请求，且不裁掉 HLS/DASH（skip 类参数最容易误伤格式列表）。
        # 来源：竞品 DataTool 的 yt_dlp_bridge.py build_info_opts 同款提速项。
        # 紧急回滚开关：VDL_YT_PLAYER_SKIP=0（不需要重新发版）。
        if os.environ.get("VDL_YT_PLAYER_SKIP", "1").strip().lower() not in ("0", "false", "no", "off"):
            options.setdefault("extractor_args", {}).setdefault("youtube", {})["player_skip"] = ["configs"]
    if not _is_yt and not cookie_text:
        # 自动登录态：仅当用户未手动粘贴 Cookie 时才尝试（用户粘贴的优先级最高，
        # 避免本机缓存/公共池覆盖用户显式提供的登录态）。
        # 短链 / 分享域 / 极速版域 → 主站登录域，统一由映射表决定（单一真源）：
        #   b23.tv → bilibili.com（云端池只存主站，否则无登录态 → B站 412）
        #   chenzhongtech.com / gifshow.com → kuaishou.com（极速版域只有 did/didv）
        #   iesdouyin.com → douyin.com（分享页只有 ttwid）
        # 不换域的话，拿到的只是设备指纹 Cookie，等于匿名请求。
        cookie_host = _cookie_login_host(host)
        try:
            from cookie_cache import get_cached_cookie_header
            cached = get_cached_cookie_header(cookie_host)
        except Exception as e:
            cached = None
            _cookie_diag("cache_exception", str(e)[:200])
        if cached:
            headers["Cookie"] = cached
            _cookie_diag("cache_hit", f"len={len(cached)} names={[p.split('=')[0] for p in cached.split('; ')]}")
        else:
            # 公共池：服务器（Railway）无本机浏览器、本机缓存也不存在时，用
            # 「用户/开发端上报、验真过的共享登录态」兜底，让 douyin/快手等
            # hardened 站在网页版也能带 Cookie 下载（配合 VDL_PROXY_CN 即闭环）。
            pooled = None
            try:
                from cookie_pool import get_cookie as _pool_get
                pooled = _pool_get(cookie_host)
            except Exception as e:
                _cookie_diag("pool_exception", str(e)[:200])
            if not pooled:
                # 本机池 miss（桌面端从未喂过池时恒 miss）→ 回读云端共享池，
                # 让桌面用户零操作复用 VPS/Railway 上那份登录态（2026-10-25）。
                pooled = _cloud_pool_cookie(cookie_host)
            logger.info("[cookie] host=%s cookie_host=%s pool=%s", host, cookie_host, "hit" if pooled else "miss")
            if pooled:
                headers["Cookie"] = pooled
                _cookie_diag("pool_hit", f"len={len(pooled)}")
            else:
                # 🔴 2026-10-25 新增：本地池空 → 回读云端共享池（桌面本机池通常为空，
                # 共享登录态都在云端；拿到即可直接复用，无需等 VPS 补推 / 开浏览器）。
                cloud_ck = _cloud_pool_cookie(cookie_host)
                if cloud_ck:
                    headers["Cookie"] = cloud_ck
                    _cookie_diag("cloud_pool_hit", f"len={len(cloud_ck)}")
                else:
                    # 公共池 miss 时，异步召唤 VPS 守护进程补推（非阻塞、带冷却），
                    # 下次请求即可命中登录态，实现「容器重建/重启后秒级自愈」。
                    try:
                        from cookie_pool import request_refill as _pool_refill
                        _pool_refill(cookie_host)
                    except Exception:
                        pass
                    browser = os.environ.get("VDL_COOKIES_FROM_BROWSER", "").strip()
                _cookie_diag("cache_miss", f"host={host}")
                if browser:
                    options["cookiesfrombrowser"] = (browser,)
                    _cookie_diag("cfb_env", browser)
                elif os.name == "nt":
                    # Windows：一律不把 cookiesfrombrowser 交给 yt-dlp。
                    # 原因见 _win_self_cookie_header 的注释（浏览器开着必炸，
                    # 且默认去读根本不存在的 Chrome）。改走自研读取，读不到
                    # 就不带 Cookie 继续，避免整次解析被无关异常打断。
                    _win_hdr = _win_self_cookie_header(cookie_host)
                    if _win_hdr:
                        headers["Cookie"] = _win_hdr
                    else:
                        _cookie_diag("cfb_skip_windows", "self-read empty, continue without cookie")
                else:
                    # 全站默认尝试从本机浏览器读登录态（不再局限于白名单），
                    # 覆盖更多需要 Cookie 的站点（影视聚合站、会员专享、地区限制等）。
                    # 精确定位「含目标站点 cookie」的具体 Profile（登录态常不在 Default）。
                    # 用登录域（不是 URL host）打分：极速版/分享域自身的 cookie 只有
                    # 设备指纹，按它打分可能选到游客 Profile。
                    found = _find_host_cookie_profile(cookie_host)
                    _cookie_diag("find_profile", str(found))
                    if found:
                        options["cookiesfrombrowser"] = found  # (browser, profile)
                        _cookie_diag("cfb_found", str(found))
                    else:
                        # 回退：探测不到具体 Profile 时仍用默认（Default）读，至少给一次机会
                        b = _detect_browser_cookie_source()
                        if b:
                            options["cookiesfrombrowser"] = (b,)
    return options

def _clean_message(raw: str) -> str:
    """去掉 yt-dlp 输出里的 ANSI 颜色码与"请去 GitHub 提 issue"之类的噪声。"""
    text = ANSI_PATTERN.sub("", raw)
    text = NOISE_PATTERN.sub("", text)
    text = text.replace("ERROR:", "").strip(" ;\n")
    return " ".join(text.split())[:MAX_HINT_CHARS]

def _effective_cookie_source(options: dict[str, Any], user_cookie: str = "") -> str:
    """根据 _base_options 产物判断 Cookie 实际来源。"""
    if user_cookie.strip():
        return "user_pasted"
    headers = options.get("http_headers", {})
    if headers.get("Cookie"):
        # 用户未粘贴时，_base_options 会从 cache / pool 注入 Cookie
        return "auto"
    if options.get("cookiesfrombrowser"):
        return "browser"
    return "none"

def _build_diag_context(
    url: str,
    cookie: str = "",
    proxy: str = "",
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造下载异常（403 等）所需的诊断上下文。"""
    host = _host_of(url) or ""
    headers = (options or {}).get("http_headers", {})
    effective_proxy = proxy or _resolve_proxy(host)
    return {
        "url": url,
        "host": host,
        "is_hardened": is_cookie_hardened_host(host),
        "is_china": is_china_host(host),
        "cookie_present": bool(headers.get("Cookie")),
        "cookie_source": _effective_cookie_source(options or {}, cookie),
        "proxy_used": bool(effective_proxy),
        "proxy": effective_proxy if effective_proxy else None,
        "referer": headers.get("Referer"),
        "user_agent": headers.get("User-Agent"),
        "is_cloud": os.environ.get("VDL_INSTANCE", "").strip().lower() == "cloud",
    }

def _diag_403(context: dict[str, Any], exc: Exception) -> None:
    """把 403 发生的上下文结构化写入临时日志，供线上排查。"""
    try:
        path = os.path.join(tempfile.gettempdir(), "vdl_403_diag.txt")
        from datetime import datetime
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a") as f:
            f.write(
                f"[{ts}] 403 host={context.get('host')} "
                f"url={str(context.get('url', ''))[:120]} "
                f"cookie_source={context.get('cookie_source')} "
                f"proxy={context.get('proxy')} "
                f"referer={context.get('referer')} "
                f"ua={context.get('user_agent')} "
                f"exc={type(exc).__name__}: {str(exc)[:200]}\n"
            )
    except Exception:
        pass

def _friendly_error(exc: Exception, context: dict[str, Any] | None = None) -> ResolveError:
    """把 yt-dlp 的英文异常转成用户能看懂的提示，并对 403 做根因分层。"""
    text = _clean_message(str(exc))
    lowered = text.lower()
    ctx = context or {}
    is_cloud = ctx.get(
        "is_cloud", os.environ.get("VDL_INSTANCE", "").strip().lower() == "cloud"
    )

    # —— 浏览器 Cookie 库被占用（Windows 上 yt-dlp 读运行中的 Chromium）——
    # 这条必须最先拦：它跟站点无关，却会把任何站点的解析都带崩，
    # 不单独识别的话会落到「视频解析失败」这种无从下手的提示上。
    if ("could not copy" in lowered and "cookie" in lowered) or (
        "cookie database" in lowered
    ):
        return ResolveError(
            "读不到浏览器 Cookie（浏览器正在运行并锁定 Cookie 库）",
            "浏览器运行时会把 Cookie 数据库独占锁住，程序无法读取里面的登录态——"
            "注意这**不代表你没登录**。\n\n"
            "二选一：\n"
            "① 关掉该浏览器 → 在视频工坊重新解析（读完首次后会缓存，之后可正常开着用）；\n"
            "② 装「浏览器扩展同步」直接推送登录态（扩展在浏览器内运行，不受文件锁影响）。",
            category="cookie_db_locked",
        )

    # —— 小红书专属分层（2026-09-25）：打包 yt-dlp 已带 XiaoHongShuIE，失败只有三种形态 ——
    # ① xhslink 短链：**可以展开**（移动 UA 直达笔记页，桌面 UA 落在 /login?
    #    redirectPath=…，两种都能解出笔记页），展开逻辑见 _expand_xhs_short。
    #    只有连展开都失败（网络/代理问题）时才会走到这里 —— 文案必须说真话，
    #    别再声称「任何下载工具都无法解析」。
    # ② xiaohongshu.com 直链但本机浏览器无小红书登录态：网页版 2026 年起强制登录，
    #    无 Cookie 时 SSR 页是空壳 → "No video formats found"。
    # ③ /explore? 或 /discovery/item? 后面直接跟 "?"（无 24 位笔记 ID）：残缺链接。
    #    实测（2026-09-25）：该形态无论是否带登录 Cookie、是否带真实 xsec_token，
    #    服务端都只回首页 feed（noteDetailMap 恒为空）——链接本身不含笔记引用，
    #    无法自动展开，只能指引重新获取完整链接。分别给精准指引。
    # 注意：必须放在 UnsupportedError 分支之前，否则 xiaohongshu.com（在白名单内）
    # 会先落进误导性的「暂未实现该站的解析器」。
    _xhs_ctx_host = (ctx.get("host", "") or "").lower()
    _xhs_hit = ("xiaohongshu" in _xhs_ctx_host or "xhslink" in _xhs_ctx_host
                or "xiaohongshu" in text.lower() or "xhslink" in text.lower()
                or "[xiaohongshu]" in lowered)
    if _xhs_hit and ("xhslink" in _xhs_ctx_host or "xhslink" in text.lower()):
        return ResolveError(
            "这条小红书短链没能展开成笔记页",
            "短链本身有效，但我们没能拿到它指向的笔记页（通常是网络或代理中断在"
            "跳转途中，少数是短链已失效）。\n\n"
            "① 确认代理/网络正常后再点一次「解析链接」；\n"
            "② 仍不行就直接粘完整链接：用浏览器打开该笔记，复制地址栏里 "
            "www.xiaohongshu.com/explore/… 或 /discovery/item/… 开头的那条；\n"
            "③ 小红书网页版需要登录才能看到笔记，请先在浏览器登录一次（登录态会自动读取）。",
            category="xhs_short_link",
        )
    if _xhs_hit and ("no video formats" in lowered or "unable to extract" in lowered):
        # 动态诊断（2026-10-25）：把「到底有没有读到登录态」摆到用户眼前。
        # 用户体感常常是「我明明登录了」，实际是本机浏览器里一条小红书 Cookie
        # 都没有（登录发生在手机 App / 另一台机器），光看笼统提示会一直重试。
        _xhs_src = None
        try:
            _xhs_src = find_browser_cookie_source("xiaohongshu.com")
        except Exception:  # noqa: BLE001
            _xhs_src = None
        if _xhs_src:
            _xhs_diag = (
                f"已从「{_xhs_src.get('browser') or '浏览器'}」读到 "
                f"{_xhs_src.get('count') or '若干'} 条小红书 Cookie，但站点仍判定未登录 —— "
                "这份登录态多半已经过期（网页端退出/被风控登出）。\n"
                "① 在该浏览器里重新打开 www.xiaohongshu.com 确认是已登录状态；\n"
                "② 再回来点一次「解析链接」（无需重启 VDL）。\n"
            )
        else:
            _xhs_diag = (
                "本机浏览器里**没有**小红书的登录 Cookie —— 已逐个检查 Chrome / Edge / "
                "UC / QQ / 360 等浏览器（你手机 App 里的登录不会同步到电脑）。\n"
                "① 用 Chrome 或 Edge 打开 www.xiaohongshu.com 登录账号；\n"
                "② 回到视频工坊重新粘贴链接——登录态会自动读取注入，无需手动配置。\n"
            )
        return ResolveError(
            "小红书笔记需要登录后才能解析",
            "小红书网页端 2026 年起强制登录：未登录时页面是空壳，拿不到视频流"
            "（这不是解析器坏了，任何工具都必须带登录态）。\n\n"
            + _xhs_diag +
            "\n若登录后仍报错，可改用「高级选项 → Cookie」粘贴 F12 里的 Cookie 串；"
            "或把地址栏完整链接（含 xsec_token）粘过来重试。",
            category="xhs_login_required",
        )
    # ③ 残缺链接：/explore? /discovery/item? 后无笔记 ID（XiaoHongShuIE 的
    #    _VALID_URL 要求路径里有 24 位 hex ID，缺了就没有 extractor 匹配 → UnsupportedError）。
    if _xhs_hit and re.search(
        r"xiaohongshu\.com/(?:explore|discovery/item)\?", ctx.get("url", "") or ""
    ):
        return ResolveError(
            "这条小红书链接缺了笔记 ID，无法解析",
            "粘贴的链接是 xiaohongshu.com/explore? 参数形态，/explore/ 后面没有那串 24 位笔记 ID——"
            "链接本身不指向任何笔记（多半是复制时只截取了半截，或分享渠道吞掉了 ID）。\n\n"
            "请重新获取完整链接：\n"
            "① 在小红书 App 打开该笔记 → 分享 → 复制链接，粘贴到电脑浏览器打开（需登录小红书）；\n"
            "② 浏览器地址栏里的完整地址应形如 www.xiaohongshu.com/explore/6a8b085b0000000024……"
            "（explore/ 后面有一长串字母数字）——把地址栏完整链接粘贴到视频工坊即可。\n\n"
            "提示：正规 App 分享链接一定含笔记 ID 和 xsec_token 参数；若粘贴后仍不见 ID，"
            "说明复制来源不完整，可把笔记从 App 发到微信/QQ 后再复制完整链接。",
            category="xhs_no_note_id",
        )

    # yt-dlp 抛出 UnsupportedError 通常意味着「域名不在 yt-dlp 支持的 extractor 列表」
    # ——对 VDL 用户来说，含义比 1625 行通用提示更具体：要么找原视频、要么找
    # 页面里的 m3u8/iframe 直链、要么通知我们加 extractor。
    if isinstance(exc, UnsupportedError) or "unsupported url" in lowered:
        host = ctx.get("host", "")
        # yt-dlp 原生支持的平台（仅因链接形态不对才落 UnsupportedError），给更精准的提示，
        # 不误导成"暂未实现解析器"。2026-08-24 补：hotstar / kinopoisk yt-dlp 已有提取器。
        _YTDLP_SUPPORTED = ("hotstar.com", "kinopoisk.ru", "hd.kinopoisk.ru")
        if host and any(h == host or host.endswith("." + h) for h in _YTDLP_SUPPORTED):
            return ResolveError(
                f"该链接形态无法被 {host} 解析器识别",
                "该平台 yt-dlp 已支持，但当前链接不是可解析的播放页形态。\n\n"
                "请确认：\n"
                "① 粘贴具体的视频/影片播放页链接（而非首页或分类页）；\n"
                f"② {host} 多为地区限制内容，需对应支持地区网络与账号（数据中心 IP 常被 geo 拦截）。",
                category="unsupported_url_form",
            )
        # 已收录平台（白名单内但 yt-dlp 无 extractor）与完全未知域名区分提示，
        # 避免误报「不在支持列表」（2026-08-22 实测 yy.com/inke.cn 等已在白名单）。
        if host and any(
            host == d or host.endswith(f".{d}")
            for p in SUPPORTED_PLATFORMS
            for d in p.domains
        ):
            # 需求登记（2026-10-17）：白名单内空白站点 = 明确的开发需求信号。
            # Downie 护城河是「用户报一个写一个」的机制，这是它的第一环。
            try:
                import resolve_demand
                resolve_demand.record_demand(host, ctx.get("url", ""), "pending_extractor")
            except Exception:
                pass
            return ResolveError(
                f"该平台已收录（{host}），但暂未实现该站的解析器",
                "yt-dlp 未提供该网站的解析器，VDL 正在为它开发专用提取器。\n\n"
                "当前可尝试：\n"
                "① 若页面里有 m3u8 / mp4 直链（右键查看源代码搜索），直接粘贴直链可下载；\n"
                "② 等后续版本支持（该站需求已自动记录，支持热度会影响开发顺序）。",
                category="pending_extractor",
            )
        # 需求登记（2026-10-17）：未知域名且 yt-dlp 也不行 = 新站点候选
        # （nmwchxx 就是从这类信号里长出来的）。旁路统计，失败静默。
        try:
            import resolve_demand
            resolve_demand.record_demand(host, ctx.get("url", ""), "unsupported_platform")
        except Exception:
            pass
        return ResolveError(
            f"该链接暂不在 VDL 支持的 {len(SUPPORTED_PLATFORMS)} 个平台列表中",
            f"yt-dlp 也未提供 {host or '该域名'} 的解析器（可能是第三方视频聚合/解析站）。\n\n"
            "请尝试以下任一方式：\n"
            "① 在该网站播放页面右键 → 查看页面源代码，搜索 `m3u8` / `mp4` / `<video` / `<iframe src=`，"
            "把找到的直链 URL 粘贴到 VDL（m3u8 / mp4 直链可被 yt-dlp 直接下载）。\n"
            "② 跳转到原视频平台（YouTube / B站 / 抖音等）后再粘贴。\n"
            "③ 如果是您常用的网站，告诉我该网站任意一个播放页，我可以为它写个 extractor 加进 VDL。\n\n"
            "该站需求已自动记录，常用站会优先开发。",
            category="unsupported_platform",
        )

    cloud_cookie_hint = (
        "网页版由「桌面版 VDL」共享登录态：请在桌面版 VDL 中打开并保持该平台登录，"
        "点『同步 Cookie 到云端』刷新后重试；或直接用桌面版 VDL 解析本链接。"
    )

    # 403 单独做根因分层，并记录结构化诊断日志
    if any(word in lowered for word in ("403", "forbidden", "http error 403")):
        _diag_403(ctx, exc)
        host = ctx.get("host", "")
        hardened = ctx.get("is_hardened", is_cookie_hardened_host(host)) if host else False
        cookie_present = ctx.get("cookie_present", False)
        if hardened and not cookie_present:
            return ResolveError(
                "下载被服务器拒绝（403）：该站需要登录 Cookie",
                cloud_cookie_hint if is_cloud else (
                    "该平台为强反爬站点，未检测到有效 Cookie。"
                    "请在常用浏览器登录该平台后重试，或到「高级选项 → Cookie」手动粘贴 Cookie。"
                ),
                category="cookie_required",
                context=ctx,
            )
        if hardened and cookie_present:
            return ResolveError(
                "下载被服务器拒绝（403）：Cookie 无效或已过期",
                "已检测到 Cookie，但服务器仍拒绝访问。可能原因："
                "① Cookie 已过期，请重新登录并同步；② 账号权限不足；"
                "③ 该视频为会员/付费/地区限制。",
                category="cookie_invalid_or_expired",
                context=ctx,
            )
        # 兜底通用 403
        hint = (
            "该链接被目标网站 CDN 拒绝。可能原因：①该站需要登录或 Cookie；②视频有防盗链/地区限制；"
            "③若为 YouTube：确认代理已开启且对 VDL 生效（双击 .app 不继承终端代理，需在 Clash 开启「系统代理」或 TUN 模式）；"
            "④换更低画质重试；⑤稍后再试"
        )
        # 临时诊断：把实际生效的代理/Referer 拼进 detail，定位「改了没生效」类问题
        if ctx and (ctx.get("proxy") or ctx.get("referer")):
            hint += (
                f"\n[diag] host={ctx.get('host')} proxy={ctx.get('proxy') or '(none)'} "
                f"referer={ctx.get('referer') or '(none)'} is_china={ctx.get('is_china')} "
                f"cookie={ctx.get('cookie_source') or '(none)'}"
            )
        # 折中：B站 不进 _COOKIE_HARDENED_DOMAINS（避免一刀切改文案），
        # 仅在通用 403 文案后追加专属提示，保留全部排查步骤。
        if host and ("bilibili.com" in host or "b23.tv" in host):
            hint += "；B站 高画质/会员视频常需登录态 Cookie，请先在本机常用浏览器登录 B站 后重试（VDL 会自动读取浏览器 Cookie 注入请求）。"
        return ResolveError(
            "下载被服务器拒绝（403）",
            hint,
            category="cdn_forbidden",
            context=ctx,
        )

    # 云端(网页版)实例遇到强反爬站需要登录态时，提示用户依赖桌面版共享登录态，
    # 而不是让用户去"本机浏览器登录"（网页版访客没有本机浏览器）。
    cloud_note = ""
    if is_cloud:
        cloud_note = cloud_cookie_hint
    # 每条规则带 category，使前端能按错误类型给出差异化行动建议
    # （cookie_required → 去粘贴 Cookie；network → 重试；restricted → 官方渠道）。
    # 注意：优酷 -3007、会员/登录类文案也归入 cookie_required，让「去粘贴 Cookie」
    # 按钮在该出现的场景都能出现。
    rules: tuple[tuple[tuple[str, ...], str, str, str], ...] = (
        (("fresh cookies", "not necessarily logged in", "-3007", "please log in",
          "login required", "sign in", "authentication", "请登录", "需登录", "会员"),
         "该平台需要登录/游客 Cookie 才能访问",
         ("请在常用浏览器（Chrome 等）打开并登录过该平台，VDL 会自动读取浏览器 Cookie；"
          "或到「高级选项 → Cookie」手动粘贴该平台的 Cookie 字符串")
         if not cloud_note else cloud_note, "cookie_required"),
        (("private", "members-only"), "该视频需要登录或为私密内容", "请更换公开可访问的视频链接", "cookie_required"),
        (("geo", "not available in your country", "region"), "该视频在当前网络所在地区不可播放", "可尝试更换网络环境后重试", "restricted"),
        (("unsupported url", "no video"), "无法从该链接中找到视频", "请确认链接指向的是视频播放页，而不是首页或列表页", "unknown"),
        (("404", "not found", "removed", "unavailable", "does not exist"), "视频不存在或已被删除", "请检查链接是否正确、视频是否仍然在线", "unknown"),
        # 2026-09-26：代理隧道类失败单独分层（必须排在通用 network 规则之前）。
        # 实测用户 VPN 节点抖动时报 "Tunnel connection failed: 502 Bad Gateway" /
        # SSL UNEXPECTED_EOF，用户完全看不懂；翻成可行动的「换节点」指引。
        (("tunnel connection failed", "unable to connect to proxy", "502 bad gateway", "proxyerror"),
         "代理/VPN 节点出海失败",
         "当前代理节点不稳定（实测约一半请求被拒）。建议：①在代理/VPN 软件里切换一个节点后重试；"
         "②或到「高级选项 → 代理」换一个代理地址；③稍等片刻再试（节点恢复后即可正常解析）", "network"),
        (("timed out", "timeout", "connection", "network", "resolve", "proxy", "ssl"), "网络连接超时", "请检查本机网络（部分海外站点需要代理）后重试", "network"),
        (("drm", "protected"), "该视频有版权保护，无法下载", "请通过官方渠道观看", "restricted"),
        (("extractor error", "keyerror", "unable to extract"), "无法识别该链接对应的视频", "请确认链接完整且指向具体的视频页面", "unknown"),
        (("ffmpeg", "postprocessing", "post processing", "merging"), "音视频合并失败，可能是该画质源文件格式兼容性问题", "建议：①点「重试」试一次（偶发）；②换 720P 或其他画质重新下载；③仍不行请反馈该链接", "unknown"),
    )
    for keywords, message, hint, category in rules:
        if any(word in lowered for word in keywords):
            return ResolveError(message, hint, category=category)
    return ResolveError("视频解析失败", text)

def _is_restricted_placeholder(info: dict[str, Any]) -> bool:
    """判断 yt-dlp 是否只扒到一个"壳"——标题是占位符、时长缺失。

    腾讯等平台的会员/付费受限视频，提取器不会报错，而是回填形如
    "vqq-video video #q4100..." 的占位标题且 duration 为空。这是确认无解的受限内容。
    """
    title = info.get("title")
    duration = info.get("duration")
    if duration is None and isinstance(title, str) and title.startswith("vqq-video video #"):
        return True
    # 兜底：标题完全缺失、时长缺失、且无缩略图 —— 视为根本没有解析到内容
    if duration is None and not title and not info.get("thumbnail"):
        return True
    return False

# --------------------------------------------------------------------------- #
# 抖音（douyin）专用解析：走 VPS Playwright 真实浏览器拦截视频流
# --------------------------------------------------------------------------- #
# yt-dlp 抖音提取器在抖音 2026 初反爬升级后失效（aweme_detail API 需 a_bogus
# 签名，未实现，报 "Fresh cookies needed"），即使带完整登录态 cookie 也无解。
# 故网页端抖音改走 VPS 上的 Playwright 无头浏览器（douyin_resolve.py），拿到真实
# 视频/音频轨 URL（音视频分离），再交给 yt-dlp process_info 下载 + ffmpeg 合并。
_DOUYIN_HOSTS: tuple[str, ...] = ("douyin.com", "iesdouyin.com")
_DOUYIN_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

def _is_douyin_host(host: str) -> bool:
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in _DOUYIN_HOSTS)

# ---- VPS worker 解析结果短时缓存（resolve → 下载复用）----
# 用户先点「解析」拿直链，再点「下载」——_run_once 对 worker 平台会再次调用
# _xxx_info → _call_vps_worker，等于 VPS 上二次冷启动 Chromium（20-60s）。
# 这里按 (platform, url) 缓存 90s（覆盖「看完解析结果再点下载」的典型间隔；
# 签名直链时效：抖音 dy_q ~1h、cc auth_key ~5min、bestv s 短期，90s 保守安全），
# 命中后下载阶段解析从 20-60s 降到 <1s。失败结果不缓存。
_RESOLVE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_RESOLVE_CACHE_LOCK = threading.Lock()
_RESOLVE_CACHE_TTL = 90.0
# 单次 /v1/resolve 的 HTTP 上限（2026-10-26 上调 90 → 130）：
# 云端 daemon 侧抖音最坏链路 = 快路预算 20s + Playwright 85s 硬上限 ≈ 105s，
# 90s 会在 daemon 给出结论前先断，把「慢」误报成「解析服务不可达」。
# 真正的闸门是 routers/core.py 的 asyncio.wait_for（按平台给预算，抖音 120s），
# 这里只要比它大就不会抢先触发。
_WORKER_HTTP_TIMEOUT = 130
# 复用连接（keep-alive）省去每次 TLS 握手/建连（~100-300ms）
_worker_http: Any = None

def _resolve_cache_get(key: str) -> dict[str, Any] | None:
    with _RESOLVE_CACHE_LOCK:
        item = _RESOLVE_CACHE.get(key)
        if item and time.time() - item[0] <= _RESOLVE_CACHE_TTL:
            return dict(item[1])
        if item:
            _RESOLVE_CACHE.pop(key, None)
    return None

def _resolve_cache_put(key: str, data: dict[str, Any]) -> None:
    with _RESOLVE_CACHE_LOCK:
        if len(_RESOLVE_CACHE) > 200:  # 防膨胀：超限先清过期
            now = time.time()
            for k, (ts, _v) in list(_RESOLVE_CACHE.items()):
                if now - ts > _RESOLVE_CACHE_TTL:
                    _RESOLVE_CACHE.pop(k, None)
        _RESOLVE_CACHE[key] = (time.time(), data)

def _maybe_refresh_vps_token() -> str:
    """403/401 自愈：桌面端经线上 /v1/resolve 转发时，若 cloud_sync.json 的
    token 已更新（_ensure_vps_env 只在启动注入一次），重读并刷新 env，
    返回新 token（供 _call_vps_worker 重建 endpoint 重试一次）。"""
    try:
        cfg = Path.home() / ".videodownloader" / "cloud_sync.json"
        if not cfg.exists():
            return ""
        d = json.loads(cfg.read_text(encoding="utf-8"))
        tok = (d.get("token") or "").strip()
        if tok and tok != os.environ.get("VDL_COOKIE_SYNC_TOKEN", ""):
            os.environ["VDL_COOKIE_SYNC_TOKEN"] = tok
            return tok
    except Exception:  # noqa: BLE001
        pass
    return ""

def _call_vps_worker(platform: str, url: str, cookie: str = "") -> dict[str, Any]:
    """调用 VPS Playwright 解析 worker（/v1/resolve?platform=xx），返回真实流元数据。

    ``cookie``：可选的用户登录态 Cookie，原样透传给 VPS worker（如微信视频号
    finder worker 用其注入浏览器会话；为空则 worker 回退 VPS 本地共享 Cookie）。

    默认经反向隧道访问 VPS 本机 daemon：Railway 侧连 127.0.0.1:18889 隧道代理，
    转发到 ECS 127.0.0.1:18731。通过显式 ``proxies`` 参数覆盖 Railway 环境变量中的
    ``http_proxy``/``https_proxy``，避免外部代理把本地隧道/内网请求误拦截为 407。

    配置优先级：
      1. VDL_WORKER_URL / VDL_WORKER_PROXY（推荐，语义最清晰）
      2. VDL_COOKIE_REFILL_URL / VDL_COOKIE_PULL_PROXY（向后兼容）
      3. 默认值 http://127.0.0.1:18731 经 http://127.0.0.1:18889 隧道代理
    """
    worker_base = os.environ.get("VDL_WORKER_URL") or os.environ.get("VDL_COOKIE_REFILL_URL", "")
    _wp_env = (os.environ.get("VDL_WORKER_PROXY") or "").strip()
    if _wp_env.lower() in ("none", "off", "disable"):
        # 显式禁用代理：桌面 App 直连线上转发端点（https://hanyuxz.top/v1/resolve），
        # 无本地隧道，绝不能走 18889 默认代理（会连接拒绝）
        worker_proxy = None
    else:
        worker_proxy = _wp_env or os.environ.get("VDL_COOKIE_PULL_PROXY", "http://127.0.0.1:18889")
    if not worker_base:
        worker_base = "http://127.0.0.1:18731"
    # 兼容旧配置：若把隧道代理地址错填成 worker 目标，自动纠正为 daemon 目标
    if ":18889" in worker_base:
        worker_base = "http://127.0.0.1:18731"
    # 兼容旧配置：worker 目标若指向 cn_proxy(18888，需 Basic 认证)，应改为直连
    # daemon(18731) 并经隧道 18889 访问，否则会被 cn_proxy 拦成 407 误报「需要 Cookie」。
    # ⚠️ 同时检查 worker_proxy：环境变量错把 VDL_COOKIE_PULL_PROXY 配成 18888
    # 时（连到 cn_proxy 而非 tunnel client），会被拒成 400 "不可达"。
    if ":18888" in worker_base:
        worker_base = "http://127.0.0.1:18731"
        worker_proxy = "http://127.0.0.1:18889"
    if worker_proxy and ":18888" in worker_proxy:
        worker_proxy = "http://127.0.0.1:18889"

    token = os.environ.get("VDL_COOKIE_REFILL_TOKEN") or os.environ.get("VDL_COOKIE_SYNC_TOKEN", "")
    if not token:
        raise ResolveError(
            "视频解析服务未配置",
            "该平台下载依赖 VPS 解析节点，请配置 VDL_COOKIE_REFILL_URL / VDL_COOKIE_REFILL_TOKEN 或 VDL_COOKIE_SYNC_TOKEN",
        )
    # 命中缓存：probe() 刚解析过（90s 内），下载阶段直接复用，跳过二次 Playwright
    # ⚠️ cookie 参与缓存 key：带用户登录态的解析结果不能与无登录态共享，
    #    否则「无 Cookie 用户命中带 Cookie 用户解析出的直链」会绕过登录态语义。
    _ckey = platform + "|" + url + ("|ck" if cookie else "")
    _cached = _resolve_cache_get(_ckey)
    if _cached is not None:
        logger.info("[worker cache] hit %s %s", platform, url[:80])
        return _cached

    def _build_endpoint(_tok: str) -> str:
        _ep = (
            worker_base.rstrip("/") + "/v1/resolve?token="
            + urllib.parse.quote(_tok, safe="")
            + "&platform=" + urllib.parse.quote(platform, safe="")
            + "&url=" + urllib.parse.quote(url, safe="")
        )
        if cookie:
            _ep += "&cookie=" + urllib.parse.quote(cookie, safe="")
        return _ep

    endpoint = _build_endpoint(token)
    # 显式指定代理并覆盖环境变量代理，确保本地隧道/内网请求不被外部 http_proxy 截获
    proxies = {"http": worker_proxy, "https": worker_proxy} if worker_proxy else None
    try:
        if _requests is None:
            raise RuntimeError("requests 库未安装")
        global _worker_http
        if _worker_http is None:
            _worker_http = _requests.Session()
        r = _worker_http.get(
            endpoint,
            headers={"User-Agent": "vdl-platform-resolve"},
            proxies=proxies,
            timeout=_WORKER_HTTP_TIMEOUT,
        )
        # 403 自愈（桌面端经线上转发）：cloud_sync.json 的 token 更新后，运行中的
        # app 无需重启即可刷新（_ensure_vps_env 只在启动时注入一次）
        if r.status_code == 403 and "127.0.0.1" not in worker_base:
            _new_tok = _maybe_refresh_vps_token()
            if _new_tok and _new_tok != token:
                logger.info("[worker] token 刷新重试 %s %s", platform, url[:60])
                r = _worker_http.get(
                    _build_endpoint(_new_tok),
                    headers={"User-Agent": "vdl-platform-resolve"},
                    proxies=proxies,
                    timeout=_WORKER_HTTP_TIMEOUT,
                )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        status = 0
        body = ""
        try:
            resp = getattr(e, "response", None)
            if resp is not None:
                status = getattr(resp, "status_code", 0)
                body = (getattr(resp, "text", None) or "")[:300]
        except Exception:
            pass
        # 透传 daemon 返回的业务错误（502 时 body 通常含 {"error": "..."}）
        try:
            bd = json.loads(body) if body else {}
            if isinstance(bd, dict) and bd.get("error"):
                err_msg = str(bd["error"])
                # 仅当 body 明确包含爱奇艺业务错误时才提示「需要 Cookie」；
                # 外部代理返回的 407 不应被误归类为 cookie_required。
                if platform == "iqiyi" and status == 407 and "爱奇艺" in err_msg:
                    raise ResolveError(
                        "爱奇艺该链接需要登录 Cookie",
                        "请在「高级选项 → Cookie」粘贴爱奇艺网页版的 Cookie 后重试；粘贴后 VDL 会走 yt-dlp 长期稳定路径直接解析。",
                        category="cookie_required",
                    ) from e
                # 任何 407 都按代理异常提示，避免外部代理认证错误误导成站点需登录
                if status == 407:
                    raise ResolveError(
                        "视频解析服务代理异常",
                        f"本地请求被外部代理拦截（HTTP 407）。原始响应：{err_msg[:120]}",
                    ) from e
                raise ResolveError(err_msg, f"HTTP {status}") from e
        except ResolveError:
            raise
        except Exception:
            pass
        # 非业务错误的网络/代理/超时异常：给运维侧明确提示
        if status == 407:
            raise ResolveError(
                "视频解析服务代理异常",
                f"本地请求被外部代理拦截（HTTP 407），请检查 Railway 环境变量 http_proxy/https_proxy 是否误伤内网地址。原始响应：{body[:120]}",
            ) from e
        raise ResolveError("视频解析服务不可达", f"{_clean_message(str(e))}") from e
    if not data.get("ok"):
        # worker 的 hint 才带可操作诊断（如「视频已删除/作者设为私密/短链已过期」，
        # 含 final_url 与 streams 数），原先只取 error（恒为「视频解析失败」）
        # 会把诊断丢掉、只剩一句笼统提示。优先透传 hint。
        raise ResolveError("视频解析失败", data.get("hint") or data.get("error") or "未知错误")
    # 成功结果写入短时缓存（供「解析 → 立即下载」复用，避免二次 Playwright）
    _resolve_cache_put(_ckey, data)
    return data

_PLAYLIST_PATHS = ("/playlist", "/discover/toplist", "/album/")

def _douyin_info(url: str, cookie: str = "") -> dict[str, Any]:
    """调 VPS worker 拿抖音真实流，构造成 yt-dlp 兼容的 info dict。

    ``cookie``：用户抖音登录态（高级选项粘贴 / Cookie 池），原样透传给 worker。
    2026-10-27 实测：抖音对**匿名**网页解析启用 Argus 风控，worker 匿名通道被拦；
    带登录态注入浏览器会话可绕过该限制（worker 侧已支持该参数）。

    抖音 PC 网页两种流形态（2026-08-23 无声问题根因）：
    - 混合轨（v{id}-web.douyinvod.com 带音轨 mp4）→ 单直链下载（HttpFD，最稳）
    - 分离轨（media-video-avc1 纯视频 + media-audio 音频）→ formats + requested_formats
      让 yt-dlp 逐轨原生下载后 ffmpeg 合并（Windows 无 aria2c，fd=None ⇒ 逐轨 HttpFD，
      有进度回调；此形态下「分片数」参数对直链无效，见 PAUSE_FORCE_LAND_SECONDS 注释）。
    """
    try:
        data = _call_vps_worker("douyin", url, cookie)
    except ResolveError as _e:
        # 风控类失败补一句可操作指引（worker 的 hint 只说明现象，没给解法）
        _hint = str(getattr(_e, "hint", "") or "")
        _msg = str(getattr(_e, "message", "") or "")
        if ("风控" in _hint or "Argus" in _hint or "风控" in _msg
                or "匿名" in _hint or "匿名" in _msg):
            raise ResolveError(
                _msg or "抖音解析失败",
                _hint.rstrip() + (
                    "\n\n👉 可点「高级选项 → Cookie」粘贴抖音网页版 Cookie 后重试："
                    "登录态会注入 worker 的浏览器会话，不受匿名风控限制。"
                ),
                category=getattr(_e, "category", None) or "parse_failed",
            ) from _e
        raise
    headers = {"Referer": "https://www.douyin.com/", "User-Agent": _DOUYIN_UA}
    video_url = data.get("video_url") or ""
    audio_url = data.get("audio_url") or ""
    video_has_audio = bool(data.get("video_has_audio"))
    if not video_url:
        raise ResolveError("抖音解析失败", "worker 未返回视频流", category="parse_failed")
    # 混合轨（视频自带音轨）：单直链下载，绝不构造分离 format（避免无谓合并/双音轨）
    if video_has_audio or not audio_url:
        return {
            "id": data.get("video_id") or "",
            "title": data.get("title") or "抖音视频",
            "duration": data.get("duration"),
            "thumbnail": data.get("thumbnail") or "",
            "webpage_url": data.get("webpage_url") or url,
            "extractor_key": "Douyin",
            "extractor": "douyin",
            "ext": "mp4",
            "url": video_url,
            "protocol": "https",
            "direct": True,
            "http_headers": dict(headers),
        }
    # 分离轨：视频 + 音频两轨合并
    height = int(data.get("height") or 0) or 720
    width = int(data.get("width") or 0) or (height * 16 // 9)
    formats: list[dict[str, Any]] = [{
        "url": video_url,
        "format_id": "dy-video",
        "format": "dy-video",
        "ext": "mp4",
        "protocol": "https",
        "vcodec": "avc1",
        "acodec": "none",
        "width": width,
        "height": height,
        "http_headers": dict(headers),
    }, {
        "url": audio_url,
        "format_id": "dy-audio",
        "format": "dy-audio",
        "ext": "m4a",
        "protocol": "https",
        "vcodec": "none",
        "acodec": "mp4a",
        "abr": 128,
        "http_headers": dict(headers),
    }]
    return {
        "id": data.get("video_id") or "",
        "title": data.get("title") or "抖音视频",
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail") or "",
        "webpage_url": data.get("webpage_url") or url,
        "extractor_key": "Douyin",
        "extractor": "douyin",
        "ext": "mp4",
        "requested_formats": list(formats),
        "formats": formats,
        "http_headers": dict(headers),
    }

# 快手（kuaishou.com）：SSR 无设备指纹 Cookie 时 __APOLLO_STATE__ 为空，纯 requests
# 拿不到主视频数据；改走 VPS Playwright 解析（kuaishou_resolve.py），返回合并好的
# mp4 直链（音视频已合并，无需 ffmpeg 再合）。
_KUAISHOU_HOSTS: tuple[str, ...] = ("kuaishou.com", "chenzhongtech.com", "gifshow.com")

def _is_kuaishou_host(host: str) -> bool:
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in _KUAISHOU_HOSTS)

def _kuaishou_info(url: str) -> dict[str, Any]:
    """调 VPS worker 拿快手真实流（合并 mp4），构造成 yt-dlp 兼容的 info dict。

    快手视频是「音视频已合并的单个 mp4」，走 process_info 单文件分支直接下载，
    无需 requested_formats。快手 CDN 不校验 Referer，带 UA 即可。
    """
    data = _call_vps_worker("kuaishou", url)
    video_url = data.get("video_url") or ""
    return {
        "id": data.get("video_id") or "",
        "title": data.get("title") or "快手视频",
        "uploader": data.get("uploader") or "",
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail") or "",
        "webpage_url": data.get("webpage_url") or url,
        "extractor_key": "Kuaishou",
        "extractor": "kuaishou",
        "ext": "mp4",
        # worker 返回的是「音视频已合并的单个 mp4 直链」，标记为直接可下载媒体，
        # 否则 summarize → _detect_direct_url 因缺少 direct 标记丢弃直链，play_url 为空
        "direct": True,
        "url": video_url,
        "protocol": "https",
        "http_headers": {"User-Agent": _DOUYIN_UA},
    }

def _douyu_info(url: str) -> dict[str, Any]:
    """调 VPS worker 拿斗鱼直播/回放流，构造成 yt-dlp 兼容的 info dict。

    斗鱼直播间返回当前直播 m3u8/flv 流（is_live 由调用方按 m3u8 判断），
    回放页返回回放流。worker 用 Playwright 真实浏览器渲染，绕过 yt-dlp
    DouyuTVIE 旧正则（room_id 格式已改）与 DouyuShowIE 的 PhantomJS 依赖。
    """
    data = _call_vps_worker("douyu", url)
    video_url = data.get("video_url") or ""
    return {
        "id": data.get("video_id") or "",
        "title": data.get("title") or "斗鱼直播",
        "uploader": data.get("uploader") or "",
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail") or "",
        "webpage_url": data.get("webpage_url") or url,
        "extractor_key": "Douyu",
        "extractor": "douyu",
        "ext": data.get("ext") or "mp4",
        "direct": True,
        "url": video_url,
        "protocol": "https",
        "is_live": bool(data.get("is_live")),
        "http_headers": {"User-Agent": _DOUYIN_UA, "Referer": "https://www.douyu.com/"},
    }

# 央视频（yangshipin.cn）：播放地址来自 playvinfo JSONP 接口（带动态 cKey 签名），
# 纯 requests 无法复现；走 VPS Playwright 解析（ysp_resolve.py），返回带签名 mp4 直链。
def _yangshipin_info(url: str) -> dict[str, Any]:
    """调 VPS worker 拿央视频真实流（签名 mp4），构造成 yt-dlp 兼容的 info dict。"""
    data = _call_vps_worker("yangshipin", url)
    video_url = data.get("video_url") or ""
    # worker 返回的 duration 可能是字符串（"2504.2722"），转 float 兼容
    duration = data.get("duration")
    try:
        if duration is not None:
            duration = float(duration)
    except (TypeError, ValueError):
        duration = None
    return {
        "id": data.get("video_id") or "",
        "title": data.get("title") or "央视频",
        "duration": duration,
        "thumbnail": data.get("thumbnail") or "",
        "webpage_url": data.get("webpage_url") or url,
        "extractor_key": "Yangshipin",
        "extractor": "yangshipin",
        "ext": data.get("ext") or "mp4",
        # worker 返回的是带签名的单个 mp4 直链（音视频已合并）
        "direct": True,
        "url": video_url,
        "protocol": "https",
        "http_headers": {"User-Agent": _DOUYIN_UA, "Referer": "https://www.yangshipin.cn/"},
    }

# 1905 电影网（1905.com）：详情页 SSR + 反爬（数据中心 IP 403），
# 走 VPS Playwright 解析（m1905_resolve.py），返回 vodfile.m1905.com 的 mp4/m3u8 直链。
def _m1905_info(url: str) -> dict[str, Any]:
    """调 VPS worker 拿 1905 真实流，构造成 yt-dlp 兼容的 info dict。"""
    data = _call_vps_worker("1905", url)
    video_url = data.get("video_url") or ""
    duration = data.get("duration")
    try:
        if duration is not None:
            duration = float(duration)
    except (TypeError, ValueError):
        duration = None
    return {
        "id": data.get("video_id") or "",
        "title": data.get("title") or "1905电影网",
        "duration": duration,
        "thumbnail": data.get("thumbnail") or "",
        "webpage_url": data.get("webpage_url") or url,
        "extractor_key": "M1905",
        "extractor": "1905",
        "ext": data.get("ext") or "mp4",
        "direct": True,
        "url": video_url,
        "protocol": "https",
        "http_headers": {"User-Agent": _DOUYIN_UA, "Referer": "https://www.1905.com/"},
    }

# 风行网（fun.tv）：播放地址来自 pm.funshion.com/v7/media/play/ 接口（带
# fudid/token 签名），走 VPS Playwright 解析（fun_resolve.py），返回 mp4 直链。
def _funshion_info(url: str) -> dict[str, Any]:
    """调 VPS worker 拿风行真实流，构造成 yt-dlp 兼容的 info dict。"""
    data = _call_vps_worker("funshion", url)
    video_url = data.get("video_url") or ""
    return {
        "id": data.get("video_id") or "",
        "title": data.get("title") or "风行网",
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail") or "",
        "webpage_url": data.get("webpage_url") or url,
        "extractor_key": "Funshion",
        "extractor": "funshion",
        "ext": data.get("ext") or "mp4",
        "direct": True,
        "url": video_url,
        "protocol": "https",
        "http_headers": {"User-Agent": _DOUYIN_UA, "Referer": "https://www.fun.tv/"},
    }

# 百视TV（bestv.com.cn）：web 端播放地址由 wasm 函数 window.makepreviewquery(vid)
# 生成签名参数 s，再请求 /api/source/preview.m3u8?s={s} 返回 HLS 流；纯 requests 无法
# 复现 wasm 签名，走 VPS Playwright worker 调该函数拿 m3u8 直链。
def _bestv_info(url: str) -> dict[str, Any]:
    """调 VPS worker 拿百视TV HLS 流（preview.m3u8），构造成 yt-dlp 兼容的 info dict。"""
    data = _call_vps_worker("bestv", url)
    video_url = data.get("video_url") or ""
    return {
        "id": data.get("video_id") or "",
        "title": data.get("title") or "百视TV",
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail") or "",
        "webpage_url": data.get("webpage_url") or url,
        "extractor_key": "Bestv",
        "extractor": "bestv",
        "ext": data.get("ext") or "m3u8",
        # m3u8 是 HLS 清单（含 ts 分片），需经 ffmpeg/yt-dlp 拉流；标记 direct 让
        # summarize 当作直链透传给前端（前端用 hls.js 或下载器拉流）。
        "direct": True,
        "url": video_url,
        "protocol": "https",
        "http_headers": {"User-Agent": _DOUYIN_UA, "Referer": "https://www.bestv.com.cn/"},
    }

# 红果短剧（hongguoduanju.com）：字节系短剧平台，播放地址来自字节 CDN
# （*.qznovelvod.com 的 /video/tos/cn/... 链接），URL 无扩展名但属可直接下载的
# 渐进式流；纯 requests 拿不到签名，走 VPS Playwright worker 点开播放器捕获真流。
def _hongguo_info(url: str) -> dict[str, Any]:
    """调 VPS worker 拿红果短剧真流，构造成 yt-dlp 兼容的 info dict。"""
    data = _call_vps_worker("hongguo", url)
    video_url = data.get("video_url") or ""
    # 字节 CDN 校验 Referer 域名：分享页(novelquickapp.com)流必须带同域 referer，
    # 写死 hongguoduanju.com 会 403。worker 已回传页面真实 referer，优先使用。
    referer = data.get("referer") or "https://www.hongguoduanju.com/"
    return {
        "id": data.get("video_id") or "",
        "title": data.get("title") or "红果短剧",
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail") or "",
        "webpage_url": data.get("webpage_url") or url,
        "extractor_key": "Hongguo",
        "extractor": "hongguo",
        # 字节 CDN 流无扩展名，但 ext 固定 mp4 让 _detect_direct_url 经 ext 兜底匹配透传
        "ext": data.get("ext") or "mp4",
        "direct": True,
        "url": video_url,
        "protocol": "https",
        "http_headers": {"User-Agent": _DOUYIN_UA, "Referer": referer},
    }

# 映客直播/回放（inke.cn）：直播间 URL 形如 liveroom/index.html?uid={uid}&id={liveid}。
# 公开接口 live_share_pc 无需登录即可返回主播昵称/直播状态；真实流地址规律为
#   https://record2.inke.cn/record_{liveid}/{liveid}.m3u8?uid=0
# 该 m3u8 带 EXT-X-ENDLIST，是「开播至今的 DVR 窗口」，可一次性下载为 mp4。
# 纯 HTTP 即可构造，走 VPS worker（中国 IP 出口稳，与 bestv/hongguo 一致）。
def _inke_info(url: str) -> dict[str, Any]:
    """调 VPS worker 拿映客直播/回放 m3u8，构造成 yt-dlp 兼容的 info dict。"""
    data = _call_vps_worker("inke", url)
    video_url = data.get("video_url") or ""
    return {
        "id": data.get("video_id") or "",
        "title": data.get("title") or "映客直播",
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail") or "",
        "webpage_url": data.get("webpage_url") or url,
        "extractor_key": "Inke",
        "extractor": "inke",
        "ext": data.get("ext") or "m3u8",
        "direct": True,
        "url": video_url,
        "protocol": "https",
        "is_live": bool(data.get("is_live")),
        "http_headers": {"User-Agent": _DOUYIN_UA, "Referer": "https://www.inke.cn/"},
    }

# 网易CC直播（cc.163.com）：房间 URL 形如 cc.163.com/{cuteid}。
# 公开接口（无需登录）：
#   https://vapi.cc.163.com/video_play_url/{cuteid}?webrtc=0&src=webcc_4000_h5&...
#   → 返回签名 FLV 直链 videourl（alirelayhdl/hsrelayhdl/alirtspull.cc.netease.com），
#     离线频道返回 HTTP 410 + {"code":"Gone","data":"no live"}。
# 标题从房间页 SSR JSON 的 "title" 字段提取。纯 HTTP 走 VPS worker（中国 IP 出口稳）。
def _cc_info(url: str) -> dict[str, Any]:
    """调 VPS worker 拿网易CC直播 FLV 直链，构造成 yt-dlp 兼容的 info dict。"""
    data = _call_vps_worker("cc", url)
    video_url = data.get("video_url") or ""
    return {
        "id": data.get("video_id") or "",
        "title": data.get("title") or "网易CC直播",
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail") or "",
        "webpage_url": data.get("webpage_url") or url,
        "extractor_key": "CCLive",
        "extractor": "cc",
        "ext": data.get("ext") or "flv",
        "direct": True,
        "url": video_url,
        "protocol": "https",
        "is_live": bool(data.get("is_live")),
        "http_headers": {"User-Agent": _DOUYIN_UA, "Referer": "https://cc.163.com/"},
    }

# 微博（weibo.com）：非浏览器请求返回 Sina Visitor System 反爬验证页，yt-dlp 内置
# WeiboIE 也已失效；改走 VPS Playwright 解析（weibo_resolve.py），返回合并 mp4 直链。
_WEIBO_HOSTS: tuple[str, ...] = ("weibo.com", "weibo.cn", "t.cn")

def _is_weibo_host(host: str) -> bool:
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in _WEIBO_HOSTS)

def _weibo_info(url: str) -> dict[str, Any]:
    """调 VPS worker 拿微博真实流（合并 mp4），构造成 yt-dlp 兼容的 info dict。"""
    data = _call_vps_worker("weibo", url)
    video_url = data.get("video_url") or ""
    return {
        "id": data.get("video_id") or "",
        "title": data.get("title") or "微博视频",
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail") or "",
        "webpage_url": data.get("webpage_url") or url,
        "extractor_key": "Weibo",
        "extractor": "weibo",
        "ext": "mp4",
        # worker 返回的是「音视频已合并的单个 mp4 直链」，标记为直接可下载媒体
        "direct": True,
        "url": video_url,
        "protocol": "https",
        "http_headers": {"User-Agent": _DOUYIN_UA, "Referer": "https://weibo.com/"},
    }

# 爱奇艺（iqiyi.com / iq.com）：分享页 playShare.html?shareId=X 是纯 JS SPA，
# yt-dlp IqiyiIE 提取不到 tvid 报 "Can't find any video"；改走 VPS Playwright
# 解析（iqiyi_resolve.py），等 JS 渲染出 data-player-tvid/videoid 后调 tmts API
# 拿 m3u8 直链（音视频合一清单，由 yt-dlp 分段下载）。
_IQIYI_HOSTS: tuple[str, ...] = ("iqiyi.com", "iq.com")

def _is_iqiyi_host(host: str) -> bool:
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in _IQIYI_HOSTS)

def _iqiyi_info(url: str, cookie: str = "") -> dict[str, Any] | None:
    """调 VPS worker 拿爱奇艺真实流，构造成 yt-dlp 兼容的 info dict。

    返回 None 表示应回退 yt-dlp 通用流程（用户已提供 Cookie 且 worker 不可用/失败，
    走 yt-dlp IqiyiIE 直下——不受爱奇艺对 worker 的反爬检测影响，VIP Cookie 也能生效）。

    分发策略（覆盖爱奇艺所有链接形式——playShare 分享页 / v_xxx.html 普通播放页 /
    a_xxx.html 专辑详情页 / intl iq.com 等）：
    1. 用户已贴 Cookie 且链接非 playShare 分享页 → 跳过 worker，直接回退 yt-dlp 直下
       （长期稳定路径，避免 worker 反爬抖动影响已知可用链接，也省 VPS 资源）。
    2. 其余情况（playShare 分享页 / 普通页无 Cookie）→ 优先走 VPS worker，
       worker 成功即返回 m3u8 直链，无需用户 Cookie。
    3. worker 不可用（未配置/不可达）或解析失败（407 需登录 / VIP / 链接失效等）：
       - 用户已贴 Cookie → 回退 yt-dlp 直下兜底；
       - 用户未贴 Cookie → 提示去「高级选项 → Cookie」粘贴爱奇艺网页版 Cookie。
    """
    # 长期稳定路径：用户已贴 Cookie 且非分享页 → 跳过 worker 直下 yt-dlp
    if cookie and "playShare" not in url:
        return None

    try:
        data = _call_vps_worker("iqiyi", url)
    except ResolveError as e:
        cat = getattr(e, "category", None)
        # playShare 分享页只能走 worker（yt-dlp 无对应提取器），直接透传 worker 真实错误——
        # 无论是真需要登录（cookie_required）还是隧道/VPS 故障（不可达/代理异常），
        # 都如实抛出，不伪造提示。
        if "playShare" in url:
            raise
        # 非分享页：用户已贴 Cookie → 回退 yt-dlp 直下兜底（长期稳定路径，VIP Cookie 也生效）
        if cookie:
            return None
        # 非分享页 + 无 Cookie：
        # - worker 明确需要登录（category=cookie_required）→ 透传，提示去「高级选项 → Cookie」粘贴；
        # - 其余（未配置 / 不可达 / 代理异常 / 解析失败）→ 透传服务真实状态，
        #   绝不再伪装成「需要 Cookie」，避免把隧道/VPS 故障误导成登录问题。
        raise

    stream_url = data.get("video_url") or ""
    if not stream_url:
        if cookie:
            return None
        raise ResolveError(
            "爱奇艺解析失败",
            "worker 未返回视频流，可能是付费/VIP 专享、链接失效或页面未加载。",
            category="parse_failed",
        )
    # 2026-08 实测：免费/低清内容播放器走 *.inter.71edge.com 的 f4v 完整直链
    # （FLV 容器），VIP/部分内容仍走 m3u8 HLS。按 URL 形态区分协议与扩展名：
    is_hls = _is_hls_url(stream_url)
    return {
        "id": data.get("video_id") or data.get("tvid") or "",
        "title": data.get("title") or "爱奇艺视频",
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail") or "",
        "webpage_url": data.get("webpage_url") or url,
        "extractor_key": "Iqiyi",
        "extractor": "iqiyi",
        "ext": "mp4" if is_hls else "flv",
        "url": stream_url,
        # f4v 是完整媒体文件（非 HLS），需 direct 标记才能经 _detect_direct_url
        # 透传给前端（play_url / watch_options / 直链下载入口）；
        # m3u8 走 _detect_play_url 的 HLS 检测，无需 direct。
        "direct": not is_hls,
        "protocol": "m3u8_native" if is_hls else "https",
        "http_headers": {"User-Agent": _DOUYIN_UA, "Referer": data.get("webpage_url") or url},
    }

# Rumble（rumble.com）：yt-dlp 的 RumbleIE/RumbleEmbedIE 被 Cloudflare 反爬
# 403 拦截（数据中心 IP + 非浏览器指纹请求），线上实测 embedJS JSON 也 403。
# 方案：带完整浏览器头的直连请求 embedJS/u3 API（SPA 播放器同源接口），
# 解析 mp4/hls 直链（sp.rmbl.ws CDN 不受 Cloudflare 挑战）。若仍 403 则
# 明确提示需要海外浏览器环境，交由通用兜底报错。
_RUMBLE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# 优酷：yt-dlp 内置 YoukuIE 不生成 ckey 播放签名，缺则 UPS 返回 -3007。
# 这里走专用 UPS 通道：带 Cookie（来自共享池）+ ckey（来自共享池）直接拿 m3u8。
# ckey 由用户从已登录浏览器「Copy as cURL」贡献（有时效，过期重新贡献即可）。
# ---------------------------------------------------------------------------
_YOUKU_M_UA = (
    "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Mobile Safari/537.36"
)

def _youku_vid(url: str) -> str:
    m = re.search(r"id_([A-Za-z0-9]+)", url)
    if m:
        return m.group(1)
    # player.youku.com 形态
    m = re.search(r"vid=([A-Za-z0-9]+)", url)
    if m:
        return m.group(1)
    return ""

def _youku_ups_ckey(vid: str, cookie: str, ckey: str, utid: str, proxy: str = "") -> list[dict]:
    """直接打 UPS 接口（带 ckey），返回 stream 列表。失败抛 ResolveError。"""
    import json as _json
    import ssl as _ssl

    cctx = _ssl.create_default_context()
    cctx.check_hostname = False
    cctx.verify_mode = _ssl.CERT_NONE
    # 优酷对境外服务器出口 IP 有地域风控（UPS 返回空 stream）。
    # 优先用调用方传入的 proxy（probe 已通过 _resolve_proxy 算出国内站 cn_proxy），
    # 否则落回 _cn_proxy_url()（Railway 上自动指向 127.0.0.1:18889 国内隧道）。
    _proxy = (proxy or "").strip() or _cn_proxy_url()
    _handlers = {}
    if _proxy:
        _handlers["http"] = _proxy
        _handlers["https"] = _proxy
    _opener = urllib.request.build_opener(urllib.request.ProxyHandler(_handlers)) if _handlers else None
    params = (
        f"vid={vid}&ccode=0501&client_ip=0.0.0.0&app_ver=1.0.75&client_ts=1787502724"
        f"&fu=0&vr=0&rst=mp4&dq=mp4&os=android&bt=phone&bd=&tict=0&d=0&needbf=1"
        f"&site=1&aw=w&vs=1.0&pver=1&wintype=xplayer_m3u8&play_ability=1024"
        f"&utid={utid}&ckey={ckey}"
    )
    api = f"https://ups.youku.com/ups/get.json?{params}"
    req = urllib.request.Request(
        api,
        headers={
            "User-Agent": _YOUKU_M_UA,
            "Referer": "https://m.youku.com/",
            "Origin": "https://m.youku.com",
            "Cookie": cookie or "",
        },
    )
    try:
        if _opener:
            with _opener.open(req, timeout=20) as resp:
                data = _json.loads(resp.read().decode("utf-8", "replace"))
        else:
            with urllib.request.urlopen(req, timeout=20, context=cctx) as resp:
                data = _json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        raise ResolveError(
            "优酷解析失败",
            f"无法连接优酷 UPS 接口（{type(e).__name__}）。",
            category="parse_failed",
        )
    code = data.get("code")
    if code not in (None, 0, "0", 200) and "stream" not in data.get("data", {}):
        raise ResolveError(
            "优酷解析失败",
            f"UPS 返回错误码 {code}：{data.get('msg')}。常见为 ckey 过期或 Cookie 失效，"
            f"请在已登录浏览器重新「Copy as cURL」贡献后重试。",
            category="parse_failed",
        )
    d = data.get("data", {}) or {}
    # 优酷完整可播放流在 streams 字段（会员/有权限内容才有）；stream 是含试看的混合字段。
    # 优先 streams，避免拿试看流当完整片（会下到 6 分钟残片）。
    full = d.get("streams") or []
    if full:
        return full
    # 无完整流：判断是否会员专享受限
    pay = d.get("pay") or {}
    if isinstance(pay, dict) and pay.get("can_play") is False:
        raise ResolveError(
            "优酷视频需会员/付费权限",
            "该优酷视频为 VIP/付费专享，当前共享池账号无播放权限，仅能试看。"
            "请用会员账号的 Cookie+ckey 重新贡献，或更换免费视频。",
            category="need_vip",
        )
    # 兜底诊断：把 UPS 原始返回片段带上，便于定位（境外 IP 风控 / ckey 失效等）
    _ec = (d.get("e") or {}).get("code")
    _has_stream = bool(d.get("stream"))
    raise ResolveError(
        "优酷解析失败",
        f"UPS 未返回可播放流。诊断：e.code={_ec}, stream={_has_stream}, "
        f"ckey_len={len(ckey)}, 可能为 ckey 失效或服务器出口 IP 被优酷地域限制。",
        category="parse_failed",
    )

def _youku_info(url: str, cookie: str = "", ckey: str = "", proxy: str = "") -> dict[str, Any]:
    """优酷专用解析：UPS + ckey 拿 m3u8。返回 yt-dlp 兼容 info dict。"""
    vid = _youku_vid(url)
    if not vid:
        raise ResolveError("优酷解析失败", "无法从链接识别视频 ID。", category="parse_failed")

    # utid = cna cookie 值（UPS 必需）
    utid = ""
    for part in (cookie or "").split(";"):
        k, _, v = part.partition("=")
        if k.strip() == "cna":
            utid = v.strip()
            break

    if not ckey:
        raise ResolveError(
            "优酷需要播放签名(ckey)",
            "该优酷视频需登录态播放签名(ckey)，当前公共池没有有效 ckey。"
            "请在已登录优酷的浏览器中：F12 → Network → 过滤 ups → 刷新 → 右键 "
            "该请求 Copy as cURL，把内容发给我即可自动贡献。",
            category="need_ckey",
        )

    streams = _youku_ups_ckey(vid, cookie, ckey, utid, proxy=proxy)
    if not streams:
        raise ResolveError(
            "优酷解析失败",
            f"UPS 通道未拿到可播放流。ckey_len={len(ckey)}, cookie_len={len(cookie)}, "
            "可能为 ckey 失效或服务器出口 IP 被优酷地域限制。",
            category="parse_failed",
        )

    # 选最高清晰度（按 height 排序）
    def _h(s: dict) -> int:
        return int(s.get("height") or 0)

    streams_sorted = sorted(streams, key=_h, reverse=True)
    best = streams_sorted[0]
    m3u8 = best.get("m3u8_url") or best.get("playurl") or ""
    if not m3u8:
        # 退而求其次：任意含 url 的字段
        for k, v in best.items():
            if "url" in k.lower() and isinstance(v, str) and v.startswith("http"):
                m3u8 = v
                break
    if not m3u8:
        raise ResolveError("优酷解析失败", "UPS 返回的流缺少 m3u8 地址。", category="parse_failed")

    # 标题：优先从网页 <title> 取，失败用 video id
    title = f"优酷视频_{vid}"
    try:
        import requests as _req

        r = _req.get(
            url,
            headers={"User-Agent": _YOUKU_M_UA},
            timeout=10,
            cookies={k.strip(): v.strip() for k, _, v in (p.partition("=") for p in (cookie or "").split(";") if "=" in p)},
        )
        mt = re.search(r"<title>(.*?)</title>", r.text, re.S)
        if mt:
            title = re.sub(r"[_-]?优酷.*$", "", mt.group(1)).strip() or title
    except Exception:
        pass

    return {
        "id": vid,
        "title": title,
        "webpage_url": url,
        "extractor_key": "YoukuCkey",
        "extractor": "youku",
        "ext": "mp4",
        "direct": True,
        "url": m3u8,
        "protocol": "m3u8_native",
        "http_headers": {
            "User-Agent": _YOUKU_M_UA,
            "Referer": "https://m.youku.com/",
            "Cookie": cookie or "",
            "Origin": "https://m.youku.com",
        },
        # 标记优酷 m3u8 直连（下载时用 ffmpeg/yt-dlp 带 header 拉）
        "_youku_m3u8": True,
    }

def _rumble_info(url: str, cookie: str = "") -> dict[str, Any]:
    import json as _json

    # curl_cffi 提供 Chrome TLS 指纹模拟，可绕过 Cloudflare 基础反爬；
    # 未安装（本地测试环境）时降级 urllib（生产 Railway 已装依赖）。
    try:
        from curl_cffi import requests as _cf_requests
    except Exception:
        _cf_requests = None

    m = re.search(r"rumble\.com/(?:embed/)?(?:v(?!ideos))?([0-9a-z]+)", url)
    vid = m.group(1) if m else ""
    if not vid:
        raise ResolveError("Rumble 解析失败", "无法从链接中识别视频 ID。", category="parse_failed")

    headers = {
        "User-Agent": _RUMBLE_UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://rumble.com/",
        "Connection": "keep-alive",
    }

    def _cf_fetch(_u: str) -> str:
        """curl_cffi 带浏览器指纹抓取；403 限流时重试并轮换指纹。返回响应文本。"""
        if _cf_requests is None:
            req = urllib.request.Request(_u, headers=headers)
            return urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
        body = ""
        _last_status = 0
        for _attempt in range(3):
            _imp = ("chrome", "chrome124", "firefox133")[_attempt % 3]
            _resp = _cf_requests.get(_u, headers=headers, impersonate=_imp, timeout=20)
            if _resp.status_code == 200:
                body = _resp.text
                break
            _last_status = _resp.status_code
        if not body:
            raise ResolveError(
                "Rumble 解析失败",
                f"Rumble 接口返回 {_last_status}（Cloudflare 反爬或地区限制）。"
                f"建议：①稍后重试；②在「高级选项」设置海外代理后重试。",
                category="parse_failed",
            )
        return body

    # 关键：v{短id} 与 embedJS 的真实 embed id 未必相同，embedJS 用错 id 会返回
    # 随机/推荐视频（实测同一 URL 三次返回三个不同视频，均为无效 id 的兜底响应）。
    # 对齐 RumbleIE 做法：先抓视频页提取 Rumble("play",{video:"xxx"}) 的 embed id，
    # 提取不到就明确报错（绝不退回短 id 请求 embedJS）。
    try:
        _page_html = _cf_fetch(url)
    except ResolveError:
        raise
    except Exception as e:
        raise ResolveError(
            "Rumble 解析失败",
            f"无法抓取 Rumble 视频页（{type(e).__name__}），可能是 Cloudflare 反爬。"
            f"建议：①稍后重试；②在「高级选项」设置海外代理后重试。",
            category="parse_failed",
        ) from None
    _m = re.search(
        r'Rumble\(\s*"play"\s*,\s*{[^}]*?\bvideo\b\s*:\s*["\']([0-9a-z]+)["\']',
        _page_html,
    )
    if not _m:
        _m = re.search(r"rumble\.com/embed/([0-9a-z]+)", _page_html)
    embed_id = _m.group(1) if _m else ""
    if not embed_id:
        raise ResolveError(
            "Rumble 解析失败",
            "视频页已加载但未找到播放器配置（页面结构可能变化）。请反馈该链接。",
            category="parse_failed",
        )

    api = f"https://rumble.com/embedJS/u3/?request=video&ver=2&v={embed_id}"
    try:
        body = _cf_fetch(api)
    except ResolveError:
        raise
    except urllib.error.HTTPError as e:
        raise ResolveError(
            "Rumble 解析失败",
            f"Rumble 接口返回 {e.code}（Cloudflare 反爬或地区限制）。"
            f"建议：①稍后重试；②在「高级选项」设置海外代理后重试。",
            category="parse_failed",
        ) from None
    except Exception as e:
        raise ResolveError("Rumble 解析失败", f"访问 Rumble 接口失败：{e}", category="parse_failed") from None

    try:
        data = _json.loads(body)
    except Exception:
        raise ResolveError("Rumble 解析失败", "Rumble 接口返回非 JSON 数据（可能被反爬拦截）。",
                           category="parse_failed") from None

    # 无效 id / 无会话时 embedJS 返回的 sys.msg 提示 + 随机推荐内容
    # （实测同一 URL 多次调用返回不同视频——必须拦截，避免误导用户）
    _sys_msg = ((data.get("sys") or {}).get("msg") or "").strip()
    if _sys_msg:
        raise ResolveError(
            "Rumble 解析失败",
            f"Rumble 返回：{_sys_msg}。建议：①稍后重试；"
            f"②在「高级选项」设置海外代理（住宅 IP）后重试。",
            category="parse_failed",
        )

    title = data.get("title") or "Rumble 视频"
    # 流选择：优先 mp4 直链（sp.rmbl.ws，完整文件可直接下载/播放），
    # 无 mp4 时退回 hls 清单。
    stream_url = ""
    ext = "mp4"
    is_hls = False
    ua = data.get("ua") or {}
    mp4s = ua.get("mp4") or {}
    if isinstance(mp4s, dict) and mp4s:
        # 按清晰度降序取最高清直链
        for h in sorted((k for k in mp4s.keys() if isinstance(k, (str, int))), key=lambda x: int(x), reverse=True):
            v = mp4s[h]
            if isinstance(v, dict) and v.get("url"):
                stream_url = v["url"]
                break
    if not stream_url:
        hls = ua.get("hls") or {}
        if isinstance(hls, dict):
            for h, v in hls.items():
                if isinstance(v, dict) and v.get("url"):
                    stream_url = v["url"]
                    ext = "m3u8"
                    is_hls = True
                    break
    if not stream_url:
        raise ResolveError("Rumble 解析失败", "未从 Rumble 接口获取到可用视频流。",
                           category="parse_failed")

    return {
        "id": vid,
        "title": title,
        "duration": data.get("duration"),
        "thumbnail": (data.get("i") or ""),
        "webpage_url": url,
        "extractor_key": "Rumble",
        "extractor": "rumble",
        "ext": ext,
        "url": stream_url,
        "direct": not is_hls,
        "protocol": "m3u8_native" if is_hls else "https",
        "http_headers": {"User-Agent": _RUMBLE_UA, "Referer": "https://rumble.com/"},
    }

# Tubi（tubitv.com）：免费 AVOD（广告支持），内容无 DRM。yt-dlp TubiTvIE 在
# 数据中心 IP 偶发失败（页面 window.__data 提取不到，可能是反爬页或 GDPR 页）。
# 专用解析：抓视频页 → 提取 window.__data 的 video_resources（dash/hlsv3/hlsv6
# manifest，无 DRM）→ 优先 HLS m3u8（适配 VDL 直链模式），失败时带页面特征诊断。
_TUBI_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

def _tubi_info(url: str, cookie: str = "", proxy: str = "") -> dict[str, Any]:
    import json as _json

    m = re.search(r"tubitv\.com/(?:[a-z]{2}-[a-z]{2}/)?(video|movies|tv-shows)/(\d+)", url)
    if not m:
        raise ResolveError("Tubi 解析失败", "无法识别 Tubi 视频 ID。", category="parse_failed")
    vtype, vid = m.group(1), m.group(2)
    page_url = f"https://tubitv.com/{vtype}/{vid}/"
    headers = {
        "User-Agent": _TUBI_UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        # 支持代理（Tubi 需美/加/澳/新/英/墨等住宅 IP；数据中心 IP 返回反爬壳页）
        if proxy:
            _handler = urllib.request.ProxyHandler({
                "http": proxy if proxy.startswith("http") else f"http://{proxy}",
                "https": proxy if proxy.startswith("http") else f"http://{proxy}",
            })
            _opener = urllib.request.build_opener(_handler)
            req = urllib.request.Request(page_url, headers=headers)
            html = _opener.open(req, timeout=30).read().decode("utf-8", "replace")
        else:
            req = urllib.request.Request(page_url, headers=headers)
            html = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise ResolveError(
            "Tubi 解析失败",
            f"Tubi 页面返回 {e.code}（地区限制或反爬）。建议：①稍后重试；"
            f"②在「高级选项」设置美国/加拿大等 Tubi 支持地区的代理。",
            category="parse_failed",
        ) from None
    except Exception as e:
        raise ResolveError("Tubi 解析失败", f"访问 Tubi 页面失败：{e}", category="parse_failed") from None

    # 诊断特征（页面被反爬/GDPR 时 hint 会带出）
    _feat = []
    if "window.__data" not in html:
        _feat.append("no-__data")
    low = html.lower()
    for _k in ("gdpr", "captcha", "robot", "access denied", "just a moment"):
        if _k in low:
            _feat.append(_k)

    # 提取 window.__data 的 video_resources（dash/hlsv3/hlsv6 manifest，均无 DRM）
    dash_url = hls_url = ""
    _m2 = re.search(r"window\.__data\s*=\s*(\{)", html)
    if _m2:
        _start = _m2.start(1)
        _depth = 0
        _i = _start
        while _i < len(html):
            if html[_i] == "{":
                _depth += 1
            elif html[_i] == "}":
                _depth -= 1
                if _depth == 0:
                    break
            _i += 1
        if _i < len(html):
            _raw = html[_start:_i + 1]
            # 简化 js_to_json：key 补引号 + 单引号转双引号 + undefined→null
            try:
                _raw2 = _json.loads(_raw)
            except Exception:
                try:
                    _fixed = re.sub(r"([{,])\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*:", r'\1"\2":', _raw)
                    _fixed = re.sub(r":\s*'((?:[^'\\]|\\.)*)'", r':"\1"', _fixed)
                    _fixed = _fixed.replace("undefined", "null").replace("NaN", "null")
                    _raw2 = _json.loads(_fixed)
                except Exception:
                    _raw2 = None
            if isinstance(_raw2, dict):
                _vd = ((_raw2.get("video") or {}).get("byId") or {}).get(vid) or {}
                for _res in _vd.get("video_resources") or []:
                    _u = (_res.get("manifest") or {}).get("url") or ""
                    if not _u:
                        continue
                    if _u.startswith("//"):
                        _u = "https:" + _u
                    _t = (_res.get("type") or "").lower()
                    if _t == "dash" and not dash_url:
                        dash_url = _u
                    elif _t in ("hlsv3", "hlsv6") and not hls_url:
                        hls_url = _u
                _title = _vd.get("title") or ""
    if not dash_url and not hls_url:
        # 兜底：正则直接抓 manifest URL
        for _mm in re.finditer(r'"manifest"\s*:\s*\{[^}]*?"url"\s*:\s*"([^"]+)"[^}]*?"type"\s*:\s*"([a-z0-9]+)"', html):
            _u = _mm.group(1)
            if _u.startswith("//"):
                _u = "https:" + _u
            _t = _mm.group(2).lower()
            if _t == "dash" and not dash_url:
                dash_url = _u
            elif _t in ("hlsv3", "hlsv6") and not hls_url:
                hls_url = _u

    # 页面无 __data（反爬/GDPR 页）→ 尝试公开清单 API（oz/videos/{id}/manifest.*）。
    # 302 到 gdpr 页时 urllib 跟随后返回 HTML，不匹配 #EXTM3U/<MPD 即跳过。
    if not hls_url and not dash_url:
        for _api in (
            f"https://tubitv.com/oz/videos/{vid}/manifest.m3u8",
            f"https://tubitv.com/oz/videos/{vid}/manifest.mpd",
        ):
            try:
                _req = urllib.request.Request(_api, headers=headers)
                _resp = urllib.request.urlopen(_req, timeout=15)
                _body = _resp.read().decode("utf-8", "replace")
                _ct = (_resp.headers.get("Content-Type") or "").lower()
                if _body.lstrip().startswith("#EXTM3U"):
                    hls_url = _api
                    _feat.append("oz-api-hls")
                    break
                if "<MPD" in _body or "mpd" in _ct or _body.lstrip().startswith("<?xml"):
                    dash_url = _api
                    _feat.append("oz-api-mpd")
                    break
            except Exception:
                continue

    # 优先 HLS（m3u8，VDL 直链可播放可下载）；无 HLS 时 DASH 仅提示（MPD 需 DASH 下载器）
    stream_url = hls_url
    is_hls = bool(hls_url)
    if not stream_url:
        raise ResolveError(
            "Tubi 解析失败",
            f"未找到可用的视频流（页面特征: {', '.join(_feat) or '正常但无 manifest'}）。"
            f"当前网络出口（数据中心 IP）可能不在 Tubi 支持地区（美/加/澳/新/英/墨等）"
            f"或触发反爬。建议在「高级选项」设置 Tubi 支持地区的住宅代理后重试。",
            category="parse_failed",
        )

    return {
        "id": vid,
        "title": _title or "Tubi 视频",
        "duration": None,
        "webpage_url": url,
        "extractor_key": "TubiTv",
        "extractor": "tubitv",
        "ext": "m3u8" if is_hls else "mpd",
        "url": stream_url,
        "direct": True,
        "protocol": "m3u8_native" if is_hls else "https",
        "http_headers": {"User-Agent": _TUBI_UA, "Referer": "https://tubitv.com/"},
    }

# YouTube（youtube.com / youtu.be）：2025 起对数据中心 IP 强制 bot 检测
# （"Sign in to confirm you're not a bot"），所有 player_client 轮换无效，
# 必须带登录态 Cookie 或 PO Token 才能解析。实现自动降级：
#   方法一：无 Cookie + bgutil PO Token（Docker 集成，尽力而为）；
#   方法二：方法一被 bot 拦截 → 自动按序尝试 Cookie 源：
#          用户显式粘贴 > 环境变量 VDL_YOUTUBE_COOKIE > 本机缓存 > 公共池。
# 全部失败 → 抛 cookie_required 提示（粘贴一次即缓存复用，免每次手动）。
_YOUTUBE_HOSTS: tuple[str, ...] = ("youtube.com", "youtu.be", "youtube-nocookie.com")
_BOT_KEYWORDS: tuple[str, ...] = (
    "sign in to confirm", "not a bot", "please sign in",
    "login_required", "confirm you're not", "sign in to continue",
    # Cookie 被 Google 轮换作废（Google 把登录态与出口 IP 绑定，拿到别处用
    # 即整份失效）。与 bot 拦截同样要「换下一个源」，否则会直接抛错、
    # 白白浪费后面本来可用的新鲜源。
    "no longer valid", "rotated in the browser", "cookies are no longer",
)

# 🔴 2026-09-26 墙钟预算：WKWebView(NSURLSession) 对**单个请求**默认 60s 上限，超过就把
# 连接掐断，前端只看到 WebKit 的 "Load failed"（实测一次解析 115.9s，前端 60s 就死了，
# 后端的中文原因永远送不出去）。故 YouTube 解析必须在预算内给出明确结论——宁可早报错。
YOUTUBE_RESOLVE_BUDGET = float(os.environ.get("VDL_YT_RESOLVE_BUDGET", "45"))

def _is_youtube_host(host: str) -> bool:
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in _YOUTUBE_HOSTS)

class _YouTubeBotBlocked(Exception):
    """yt-dlp 返回 bot 检测类错误，应触发 Cookie 降级。"""

# --------------------------------------------------------------------------- #
# YouTube JS 挑战（nsig / signature）求解链 —— 2026-09-27 补，对标竞品 DataTool
# --------------------------------------------------------------------------- #
# 背景（本机 A/B 实测，video=YK9a6TC9j54，同机/同代理）：
#   yt-dlp 的 `--js-runtimes` **默认只启用 deno**（见 yt_dlp/options.py），
#   而不是「自动找 PATH 里任何一个」。本机与打包环境都没有 deno 时，JS 挑战无人可解：
#     实时会话 + 无 JS runtime → "The page needs to be reloaded"（SABR 流，卡死）
#     实时会话 + node + EJS   → ✅ 解析成功（标题、格式都拿到了）
#     缓存快照 + node + EJS   → 仍 bot 拦截（⇒ 会话那一半同样不能少）
#   ⇒ 「会话自持」与「JS 挑战求解」是**互补的两半**，缺一不可。
#
# 依赖侧（见 requirements.txt）：yt-dlp-ejs（本地 EJS 脚本，免 GitHub 远程拉取）
# + deno（PyPI 分发的官方二进制，由 build_mac.sh --add-binary 放进包内 bin/）。
_JS_RUNTIME_ENV = "VDL_JS_RUNTIME"
# 优先级与 yt-dlp 一致（deno > node > quickjs > bun）；把能用的都报上去，
# yt-dlp 会自己挑「已启用且可用」里优先级最高的那个。
_JS_RUNTIME_CANDIDATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("deno", ("deno",)),
    ("node", ("node",)),
    ("bun", ("bun",)),
    ("quickjs", ("qjs", "quickjs")),
)

def _find_js_runtime_binary(name: str) -> str | None:
    """定位某个 JS runtime 的可执行文件；找不到返回 None。

    查找顺序（先打包内置、再开发环境，最后系统）：
      ① `VDL_JS_RUNTIME` 环境变量显式指定（形如 `deno` 或 `deno:/abs/path`）；
      ② 打包内置：`<_MEIPASS>/bin/<exe>`（build_mac.sh 的 `--add-binary …:bin`），
         以及 `dirname(sys.executable)/bin/<exe>`（.app 的 Contents/MacOS/bin）；
      ③ 开发环境：`sysconfig.get_path("scripts")`（= venv 的 bin/，pip 装的 deno 就在这）；
      ④ 系统 PATH。
    """
    import shutil as _shutil
    import sysconfig as _sysconfig

    exe = name + (".exe" if os.name == "nt" else "")

    override = (os.environ.get(_JS_RUNTIME_ENV) or "").strip()
    if override:
        _o_name, _, _o_path = override.partition(":")
        if _o_name.strip().lower() == name and _o_path.strip():
            return _o_path.strip()

    roots = []
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        roots.append(Path(meipass) / "bin")
    try:
        roots.append(Path(sys.executable).parent / "bin")
    except Exception:
        pass
    try:
        roots.append(Path(_sysconfig.get_path("scripts") or ""))
    except Exception:
        pass
    for _root in roots:
        if not str(_root):
            continue
        _cand = _root / exe
        try:
            if _cand.is_file() and os.access(_cand, os.X_OK):
                return str(_cand)
        except Exception:
            continue

    found = _shutil.which(name)
    return found or None

def _js_challenge_options() -> dict[str, Any]:
    """YouTube 解析要用的 JS runtime + EJS 远程组件（yt-dlp 选项）。

    只在能真正找到 runtime / 支持该组件时才写进 options：
    `YoutubeDL._clean_js_runtimes()` 对**不认识的名字会直接 raise ValueError**，
    所以必须拿装好的 yt-dlp 自己的注册表来过滤，而不是硬编码。

    🔴 VDL_YT_JS_RUNTIME=off：整体关闭 JS 求解（运维开关，默认不开）。
    2026-10-19 香港节点实测：2 核小机上 deno 解一次 nsig 挑战要 **43s+**
    （桌面 M1 同操作 1~5s），App 的多级重试链叠加后必然撞穿前端 70s 预算
    ⇒ 网页版 YouTube 解析一律「超时」。关掉后格式列表不变（bare yt-dlp
    实测 50 格式 / 1.3~5.8s），但 DASH 直链的签名不再解密 ⇒ 下载侧必须配合
    「无 POT 时 ≤1080p 优先 HLS 混合流」（见 _yt_prefer_hls）一起用，
    否则会选中签名未解密的 DASH 轨。桌面端（deno 快）不要开。

    🔴 返回的 dict 会被 `options.update(...)` 合并进 yt-dlp 选项 ⇒ off 时
    **必须显式返回 `{"js_runtimes": {}}`**：空 dict 是 update no-op，而
    YoutubeDL 的库默认是 `{'deno': {}}`（YoutubeDL.py: `params.get(
    'js_runtimes', {'deno': {}})`），只返回空 dict 而不写这个键，等于没关
    （deno 照跑）——这是本函数的接线前提，改动前先读这段。
    """
    if os.environ.get("VDL_YT_JS_RUNTIME", "").strip().lower() in (
            "0", "off", "none", "disabled", "false", "no"):
        _cookie_diag("yt_js_challenge", "runtimes=[] components=[] (VDL_YT_JS_RUNTIME=off)")
        return {"js_runtimes": {}}
    try:
        from yt_dlp.globals import supported_js_runtimes, supported_remote_components
        ok_runtimes = set(supported_js_runtimes.value.keys())
        ok_components = list(supported_remote_components.value)
    except Exception:
        ok_runtimes = {n for n, _ in _JS_RUNTIME_CANDIDATES}
        ok_components = ["ejs:github"]

    runtimes: dict[str, dict[str, str]] = {}
    for _name, _ in _JS_RUNTIME_CANDIDATES:
        if _name not in ok_runtimes:
            continue
        _path = _find_js_runtime_binary(_name)
        if _path:
            runtimes[_name] = {"path": _path}

    out: dict[str, Any] = {}
    if runtimes:
        out["js_runtimes"] = runtimes
    # EJS 远程组件只在**本地 yt-dlp-ejs 不可用**时才被 yt-dlp 真正拉取；
    # 声明它是为了「本地包缺失/版本不匹配」时仍有兜底（DataTool 同样这么做）。
    if "ejs:github" in ok_components:
        out["remote_components"] = ["ejs:github"]
    _cookie_diag("yt_js_challenge", f"runtimes={list(runtimes)} components={out.get('remote_components')}")
    return out

# YouTube「登录态」判据（对标 DataTool 的 getYoutubeEmbeddedLoginStatus）。
# 只有真登录过才会有这几个名字；缺了它们，YouTube 一律按匿名处理 → 必然命中
# bot 检测。DataTool 包里那句日志写得很直白：
#   "found cookies but no login session (SID/LOGIN_INFO/__Secure-*PSID);
#    age-restricted / SABR downloads may fail"
_YT_LOGIN_COOKIE_NAMES: tuple[str, ...] = (
    "SID", "__Secure-1PSID", "__Secure-3PSID", "LOGIN_INFO",
)

def youtube_cookie_login_state(header: str) -> str:
    """Cookie 头的 YouTube 登录态：'empty' | 'no_login' | 'logged_in'。"""
    h = (header or "").strip()
    if not h:
        return "empty"
    names = {p.split("=", 1)[0].strip() for p in h.split(";") if "=" in p}
    return "logged_in" if names & set(_YT_LOGIN_COOKIE_NAMES) else "no_login"

def _youtube_source_is_hopeless(src: str, ck: str) -> str | None:
    """自动 Cookie 源是否「必然被拦」而可直接跳过；是则返回原因，否则 None。

    `user` / `env` 不参与判定 —— 用户显式给的仍然照试（个别站点是单键鉴权）。
    自动源（browser / cache / pool）缺 SID / LOGIN_INFO / __Secure-*PSID 时，
    YouTube 100% 按匿名处理，试它只会白烧掉墙钟预算（YOUTUBE_RESOLVE_BUDGET）。
    """
    if src not in ("browser", "cache", "pool"):
        return None
    st = youtube_cookie_login_state(ck)
    if st == "logged_in":
        return None
    return "未登录" if st == "no_login" else "空 Cookie"

def _youtube_cookie_candidates(user_cookie: str) -> list[tuple[str, str]]:
    """收集 YouTube Cookie 候选（去重）。

    顺序（2026-09-27 调整）：user > env > **browser** > cache > pool。

    🔴 为什么把「实时浏览器」插到 cache 前面（有实测依据）：在同一台机器上同时取
    两份做 diff，认证字段完全一致（SID / LOGIN_INFO / __Secure-1PSID / 3PSID /
    SAPISID / HSID / SSID …），**只有 5 个「滚动刷新」字段不同** ——
    `SIDCC`、`__Secure-1PSIDCC`、`__Secure-1PSIDTS`、`__Secure-3PSIDCC`、
    `__Secure-3PSIDTS`。Google 正是靠这几个滚动值判定会话是否还活着；快照一旦
    落后于浏览器，YouTube 就把整份会话当无效（LOGGED_IN:false）。
    而 cookie_cache 的 TTL 长达 30 天 ⇒「有缓存」会**一直**挡在新鲜值前面被尝试
    并失败，表现为「Cookie 明明在更新，却始终被判未登录/bot」。
    竞品 DataTool 之所以稳，就是它每次都导出自己浏览器里那份**活着的**会话
    （同一台机器、同一个出口 IP）——我们缺的从来不是 yt-dlp，而是这一步。
    """
    cands: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(src: str, ck: str) -> None:
        ck = (ck or "").strip()
        if ck and ck not in seen:
            seen.add(ck)
            cands.append((src, ck))

    if user_cookie:
        _add("user", user_cookie)
    _add("env", os.environ.get("VDL_YOUTUBE_COOKIE", ""))
    # 实时解密本机浏览器：同机同 IP、会话仍在滚动更新，是唯一「活着」的那份。
    try:
        _add("browser", get_browser_cookie_header("youtube.com", "https://www.youtube.com/") or "")
    except Exception as e:  # noqa: BLE001
        _cookie_diag("yt_browser_exception", str(e)[:200])
    try:
        from cookie_cache import get_cached_cookie_header
        _add("cache", get_cached_cookie_header("youtube.com") or "")
    except Exception:
        pass
    _add("pool", _pooled_cookie("youtube.com") or "")
    return cands


def _evict_youtube_cookie_cache(reason: str = "") -> None:
    """清掉 YouTube 的**本机 Cookie 缓存**，避免失效快照长期遮蔽后续新鲜源。

    背景（2026-09-26 实测）：cookie_cache 的 TTL 长达 30 天，而候选优先级是
    `user > env > cache > pool`。一旦缓存里躺着一份被 Google 轮换作废的旧快照，
    它会**永远排在**每 30min 新推送到公共池的那份**前面**被尝试并失败 ——
    表现为「明明一直在推新 Cookie，却始终报 bot 拦截」的死循环。

    桌面端删掉后下次会实时重新解密浏览器（拿到最新）；服务端删掉后则自然
    回落到公共池里的新鲜值。
    """
    try:
        from cookie_cache import drop_cached_cookie
        if drop_cached_cookie("youtube.com"):
            logger.info("[youtube] 已清除失效的本地 Cookie 缓存（%s），改用后续源",
                        reason or "resolve failed")
    except Exception:
        pass

def _fetch_youtube_visitor_data(proxy: str = "") -> str:
    """无 Cookie 从 YouTube 首页 HTML 提取 visitorData（PO Token 免 Cookie 链路）。

    2026-08 实测：数据中心 IP 访问 www.youtube.com 首页返回 200，HTML 内含
    "VISITOR_DATA":"Cg..."（ytcfg），无需登录态。yt-dlp 的 GVS PO Token 必须
    绑定 visitor_data，之前因缺它而无法触发 bgutil server 生成 token——
    这里自动补上，即可走「PO Token 免 Cookie」路径。

    失败（被限流/网络）返回空串，调用方回退 Cookie 源。
    """
    import re as _re
    import requests as _requests
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        r = _requests.get(
            "https://www.youtube.com/", headers=headers, proxies=proxies, timeout=8
        )
        if r.status_code == 200:
            m = _re.search(r'"VISITOR_DATA":"([^"]+)"', r.text)
            if m:
                return m.group(1)
    except Exception as _e:  # noqa: BLE001
        logger.info("[youtube] 获取 visitor_data 失败（不影响 Cookie 兜底）: %s", str(_e)[:100])
    return ""

_YT_ID_RE = re.compile(r'(?:youtu\.be/|/shorts/|/live/|/embed/|[?&]v=)([A-Za-z0-9_-]{1,20})')
# 严格版：只有 11 位才是合法视频 ID，供 canonicalize 判断「能不能安全改写」用
_YT_VIDEO_ID_STRICT_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def validate_youtube_id(url: str) -> None:
    """YouTube 视频 ID 固定 11 位；分享链接复制丢字时**提前**给出明确提示。

    否则无效 ID 会流进 bot 检测（对端无 Cookie → 误报「YouTube 需要登录
    Cookie」）或 yt-dlp 的 Unsupported URL（2026-09-22 实测：用户粘贴的
    youtu.be/mGBQMAUayc 只有 10 位 ID，两端各报一种误导性错误）。
    """
    m = _YT_ID_RE.search(url or "")
    if m and len(m.group(1)) != 11:
        bad = m.group(1)
        raise ResolveError(
            "视频链接不完整",
            f"YouTube 视频 ID 应为 11 位，这条链接里的 ID「{bad}」只有 {len(bad)} 位"
            "（通常是复制时丢了字符）。请回到来源重新复制完整链接再试。",
            category="bad_url",
        )


def canonicalize_video_url(url: str) -> str:
    """把等价短链规范化成标准长链（YouTube：各种形态 → www.youtube.com/watch?v=<id>）。

    🔴 2026-09-22 实测：同一份浏览器登录 Cookie，对端解析 watch 形态 200、
    youtu.be 形态 400——Cookie 请求头按初始 URL 的域绑定，youtu.be 302 跳到
    youtube.com 后登录态没跟过去，仍被判 bot 拦截。**解析与下载入口都必须先
    规范化**，否则会出现「解析成功但下载失败」这种更难查的分裂现象。

    2026-09-29 对齐网页版（web-dev）后覆盖全部等价形态：youtu.be/<id>、
    /shorts/、/live/、/embed/、/v/，以及 m./music./裸域 / watch 长链
    —— 这些 host 同样会让 Cookie 变成「只发给该主机」的 cookie，实测仍被判 bot；
    已规范的 www.youtube.com/watch 长链原样保留（不误伤 list / index / t）。
    取不到合法 11 位 ID、或非 YouTube 链接一律原样返回，不猜不改。
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    host = (parsed.hostname or "").lower()
    if not _is_youtube_host(host):
        return url
    path = parsed.path or ""
    vid = ""
    if path == "/watch":
        vid = (parse_qs(parsed.query).get("v") or [""])[0]
        if host == "www.youtube.com":
            return url  # 已是标准长链：保留 list / index / t 等有语义参数
    elif host in ("youtu.be", "www.youtu.be"):
        vid = path.strip("/").split("/")[0]
    elif path.startswith(("/shorts/", "/live/", "/embed/", "/v/")):
        vid = path.strip("/").split("/", 1)[-1].split("/")[0]
    if not _YT_VIDEO_ID_STRICT_RE.match(vid or ""):
        return url
    return f"https://www.youtube.com/watch?v={vid}"


def _bgutil_reachable() -> bool:
    """bgutil PO Token server（127.0.0.1:4416）是否可达。

    🔴 必须用裸 socket 直连探测（绝不能走 yt-dlp 的全局 proxy）：
    2026-10-01 实测，pot:bgutil:http 插件在 server 未启动时对 /ping 的请求
    会被 yt-dlp 全局代理吞掉（proxy=127.0.0.1:7892 → 代理代连 4416 超时重试），
    **一次解析干挂 367 秒**，直接吃穿 YOUTUBE_RESOLVE_BUDGET。
    """
    import socket as _socket
    try:
        with _socket.create_connection(("127.0.0.1", 4416), timeout=0.5):
            return True
    except OSError:
        return False

_POT_USABLE_CACHE: dict[str, bool] = {}

def _pot_usable(proxy: str = "") -> bool:
    """PO Token（pot:bgutil:http）此刻是否真的可用（默认每代理只探测一次）。

    两件事都满足才算可用：
    ① server 在 127.0.0.1:4416 监听（_bgutil_reachable 的裸 socket 预检，
       绝不走代理——走代理会被拖死 367s，见其注释）；
    ② **yt-dlp 实际会用的代理也放行 127.0.0.1:4416** —— 插件的
       _request_webpage 走 yt-dlp 全局网络栈（getpot_bgutil_http.py 里
       `'proxy': request.request_proxy`），代理是 socks/海外节点时
       CONNECT 127.0.0.1:4416 必失败。2026-10-19 香港节点实测：裸 socket
       报「可达」，但经 socks 代理 0.28s 快速失败 ⇒ POT 静默失效，
       而 fetch_pot=always 仍按「可用」在跑，既浪费又掩盖真实状态。
    """
    key = (proxy or "").strip()
    if key in _POT_USABLE_CACHE:
        return _POT_USABLE_CACHE[key]
    result = False
    if _bgutil_reachable():
        if not key:
            # 无代理：插件请求直连本机 4416，server 在监听就跑得通
            result = True
        else:
            try:
                import urllib.request
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": key, "https": key}))
                with opener.open("http://127.0.0.1:4416/ping", timeout=0.8) as resp:
                    result = int(getattr(resp, "status", 0) or resp.getcode() or 0) == 200
            except Exception:
                result = False
    _POT_USABLE_CACHE[key] = result
    return result

def _yt_real_video_heights(formats: list) -> list[int]:
    """真实视频轨高度（排除纯音频与 mhtml 雪碧图/storyboard）。"""
    out: list[int] = []
    for f in formats or []:
        if not isinstance(f, dict):
            continue
        if f.get("vcodec") in (None, "none"):
            continue
        if str(f.get("protocol") or "").startswith("mhtml"):
            continue
        h = f.get("height")
        if h:
            try:
                out.append(int(h))
            except (TypeError, ValueError):
                pass
    return out

def _yt_is_pot_starved(info: dict) -> bool:
    """SABR「PO Token 饥饿」响应检测（2026-10-01 定版）。

    YouTube 2026 起：会话没有 PO Token 时，player 请求成功但 GVS 被 SABR 降级
    —— formats 里只剩 itag18（360p 混合流）+ 雪碧图，**一个自适应视频轨
    （video-only）都没有**。真实上限就是 360p 的视频也必有 video-only 轨，
    故以此判「饥饿」不会误伤；纯音频内容（无任何真实视频轨）也不算。
    """
    fmts = [f for f in (info.get("formats") or []) if isinstance(f, dict)]
    real_heights = _yt_real_video_heights(fmts)
    if not real_heights:
        return False
    for f in fmts:
        if f.get("vcodec") in (None, "none") or f.get("acodec") not in (None, "none"):
            continue  # 只找 video-only 自适应轨
        if str(f.get("protocol") or "").startswith("mhtml"):
            continue
        return False
    return True

def _resolve_youtube(url: str, user_cookie: str = "", proxy: str = "") -> dict[str, Any]:
    """YouTube 自动降级解析：方法一（免 Cookie + PO Token）→ 方法二（Cookie 源自动切换）。

    方法一：自动获取 visitor_data → 注入 yt-dlp（youtube:visitor_data + fetch_pot=always）
    → bgutil server 生成 PO Token → 免 Cookie 解析；
    方法一被 bot 拦截或不可用时，自动按序尝试 Cookie 源（user > env > cache > pool）。

    返回 yt-dlp info dict；全部失败抛 ResolveError（bot 拦截时 category=cookie_required）。
    """
    url = canonicalize_video_url(url)  # 短链先规范化，避免 youtu.be 形态过不了 bot
    host = _host_of(url)
    validate_youtube_id(url)  # ID 不完整直接明确报错，别流进 bot/Cookie 兜底
    effective_proxy = proxy or _resolve_proxy(host)
    _deadline = time.monotonic() + YOUTUBE_RESOLVE_BUDGET

    def _left() -> float:
        """剩余墙钟预算（秒）。见 YOUTUBE_RESOLVE_BUDGET 注释。"""
        return _deadline - time.monotonic()

    # 方法一（免 Cookie）先自动拿 visitorData；拿不到则走纯 Cookie 链路。
    # 🔴 有用户 Cookie 时**不必**抓：visitorData 只服务「免 Cookie + PO Token」路径，
    # 而对 YouTube 首页的这次代理请求实测可白花 15s（2026-09-26）。
    visitor_data = ""
    if not (user_cookie or "").strip():
        visitor_data = _fetch_youtube_visitor_data(effective_proxy)
        if visitor_data:
            logger.info("[youtube] 已自动获取 visitor_data（%s…），启用 PO Token 免 Cookie 路径", visitor_data[:20])

    def _try(cookie_text: str, use_visitor: bool = True, allow_pot_fallback: bool = True) -> dict[str, Any]:
        if _left() <= 5:
            raise ResolveError(
                "YouTube 解析超时",
                f"解析已用满 {int(YOUTUBE_RESOLVE_BUDGET)} 秒预算仍未拿到结果，"
                "多半是当前代理/VPN 节点出海太慢或不稳定。建议：①换一个节点后重试；"
                "②若节点正常仍失败，请到「高级选项 → Cookie」粘贴一次 YouTube 登录 Cookie。",
                category="network",
            )
        opts = _base_options(PROBE_RETRIES, host, cookie=cookie_text, proxy=proxy)
        opts["format"] = None
        # 免 Cookie 路径：注入 visitor_data + 强制 fetch PO Token（bgutil 自动生效）。
        # 🔴 bgutil server 未启动时**绝不能**开 fetch_pot——插件 ping 127.0.0.1:4416
        # 会被全局代理拖死（实测一次 367s，吃穿整个解析预算）。用裸 socket 预检。
        if use_visitor and not cookie_text and visitor_data:
            ya = opts.setdefault("extractor_args", {}).setdefault("youtube", {})
            ya["visitor_data"] = [visitor_data]
            ya["fetch_pot"] = ["always"] if _pot_usable(effective_proxy) else ["never"]
        try:
            with _YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if not info:
                return {}
            # 🔴 2026-10-01 SABR「PO Token 饥饿」自动补救：默认客户端链在会话无
            # PO Token 时只拿到 itag18(360p)+雪碧图（用户看到清晰度最高只有 360P）。
            # 实测矩阵：default/tv/mweb/ios/android/tv_simply 全军覆没或被拦，
            # **web_safari 免 PO Token 即可拿 HLS 全清晰度（144~1080，avc1 可播）**，
            # fetch_pot=never 后单次解析仅 ~5.5s。饥饿时预算内自动重试一次，
            # 只在重试结果的真实最高清晰度更高时才采用。env 可关：VDL_YT_HLS_FALLBACK=0。
            if (allow_pot_fallback and _yt_is_pot_starved(info) and _left() > 12
                    and os.environ.get("VDL_YT_HLS_FALLBACK", "1").strip().lower()
                    not in ("0", "false", "no", "off")):
                _old_max = max(_yt_real_video_heights(info.get("formats") or []), default=0)
                _pot_ok = _pot_usable(effective_proxy)
                try:
                    opts_h = _base_options(PROBE_RETRIES, host, cookie=cookie_text, proxy=proxy)
                    opts_h["format"] = None
                    _ya_h = opts_h.setdefault("extractor_args", {}).setdefault("youtube", {})
                    _ya_h["player_client"] = [
                        os.environ.get("VDL_YT_HLS_CLIENT", "").strip() or "web_safari"]
                    # POT 不可用（server 没跑 / 代理不放行 4416）时必须显式关 POT，
                    # 否则插件 ping 127.0.0.1:4416 会被全局代理拖死（实测 367s）；
                    # POT 可用则保留 POT 拿 DASH 全轨。
                    if not _pot_ok:
                        _ya_h["fetch_pot"] = ["never"]
                    with _YoutubeDL(opts_h) as ydl_h:
                        info_h = ydl_h.extract_info(url, download=False)
                    _new_max = max(_yt_real_video_heights((info_h or {}).get("formats") or []), default=0)
                    if info_h and _new_max > _old_max:
                        logger.info(
                            "[youtube] PO Token 饥饿（最高 %sP）→ web_safari/HLS 补救成功（最高 %sP，pot=%s）",
                            _old_max, _new_max, _pot_ok)
                        return info_h
                except (DownloadError, ExtractorError) as exc_h:
                    logger.info("[youtube] PO Token 饥饿补救失败（保留原结果）: %s", str(exc_h)[:120])
            return info
        except (DownloadError, ExtractorError) as exc:
            low = str(exc).lower()
            if any(k in low for k in _BOT_KEYWORDS):
                raise _YouTubeBotBlocked(str(exc)[:300]) from exc
            # 🔴 2026-09-26 实测：带登录态 Cookie 的请求会触发 YouTube SABR 新流程，
            # 返回 "The page needs to be reloaded"；**无 Cookie 的干净请求反而能拿到
            # 完整格式列表（实测 48 个）**。故带 Cookie 失败且命中 SABR 特征时，
            # 自动剥掉 Cookie 裸重试一次（visitor_data 也一并去掉，二者绑定的是
            # 同一套 PO Token 上下文）。
            # ⚠️ 但在被风控的出口 IP 上这次裸试**必然失败且极慢**（实测 75s 无结果），
            # 会单独吃掉整个预算；故仅在剩余预算充裕（>20s）时才值得试。
            if (cookie_text and _left() > 20
                    and ("page needs to be reloaded" in low
                         or "not available" in low or "format" in low)):
                try:
                    return _try("", use_visitor=False)
                except (DownloadError, ExtractorError):
                    pass  # 裸重试也失败 → 落回下方 extract_flat 降级
            # 非 bot 错误（format not available 等 SABR 问题）：extract_flat 降级一次
            # （同样受预算约束：拿不到真格式时它只能给出元数据，别为它耗尽预算）
            if _left() <= 10:
                raise
            try:
                opts2 = _base_options(PROBE_RETRIES, host, cookie=cookie_text, proxy=proxy)
                opts2["extract_flat"] = "in"
                with _YoutubeDL(opts2) as ydl2:
                    info2 = ydl2.extract_info(url, download=False)
                if info2:
                    return info2
            except Exception:
                pass
            raise

    # 方法一：无 Cookie（bgutil PO Token 尽力）
    try:
        info = _try(user_cookie or "")
        if info:
            return info
    except _YouTubeBotBlocked:
        logger.info("[youtube] %s 被 bot 检测拦截，自动切换 Cookie 源", url[:60])
    except (DownloadError, ExtractorError) as exc:
        # 非 bot 错误（视频不可用/链接失效等）→ 转 ResolveError 透传真实原因
        raise ResolveError("视频解析失败", _clean_message(str(exc))[:300]) from exc

    # 方法二：Cookie 源自动切换（user > env > browser > cache > pool）
    _hopeless: list[str] = []
    for src, ck in _youtube_cookie_candidates(user_cookie):
        if _left() <= 5:
            logger.info("[youtube] 预算耗尽（剩余 %.1fs），停止切换 Cookie 源", _left())
            break
        # 登录态体检（对标 DataTool 的 getYoutubeEmbeddedLoginStatus）：自动源缺
        # SID/LOGIN_INFO/__Secure-*PSID 时 YouTube 100% 按匿名处理，试它只是白烧
        # 墙钟预算；跳过并记账，用于最后给出精确的失败原因。
        _why = _youtube_source_is_hopeless(src, ck)
        if _why:
            _hopeless.append(f"{src}（{_why}）")
            logger.info("[youtube] Cookie 源=%s 无登录态字段（%s），跳过", src, _why)
            if src == "cache":
                _evict_youtube_cookie_cache("无登录态字段")
            continue
        try:
            info = _try(ck)
            if info:
                logger.info("[youtube] bot 拦截后自动切换 Cookie 源=%s 解析成功", src)
                return info
        except _YouTubeBotBlocked:
            logger.info("[youtube] Cookie 源=%s 仍被 bot 拦截，换下一个", src)
            if src == "cache":
                _evict_youtube_cookie_cache("bot 拦截")
            continue
        except (DownloadError, ExtractorError) as exc:
            low = str(exc).lower()
            if any(k in low for k in _BOT_KEYWORDS):
                if src == "cache":
                    _evict_youtube_cookie_cache("bot 拦截")
                continue
            logger.info("[youtube] Cookie 源=%s 报非 bot 错误（可能 Cookie 过期），换下一个: %s",
                        src, str(exc)[:120])
            if src == "cache":
                _evict_youtube_cookie_cache("解析失败")
            continue

    if _hopeless:
        # 自动源里有 Cookie、但都不是登录态 —— 精确告诉用户是哪一步的问题，
        # 而不是笼统地丢一句「需要登录 Cookie」（那种提示会让人以为没配 Cookie）。
        raise ResolveError(
            "浏览器里的 YouTube 登录态不可用",
            "本机浏览器/缓存里确实有 YouTube Cookie，但都不含登录态字段"
            "（SID / LOGIN_INFO / __Secure-*PSID），来源：" + "、".join(_hopeless) + "。\n"
            "常见原因：① 浏览器里 YouTube 已退出登录；② Google 把这份会话作废了"
            "（滚动字段 __Secure-*PSIDTS 过期，浏览器一刷新就换新值）。\n"
            "处理：在本机浏览器打开 youtube.com，确认右上角是已登录状态，然后直接重试即可"
            "（会自动重新读取浏览器里的会话，不需要粘贴 Cookie）。",
            category="cookie_required",
        )
    raise ResolveError(
        "YouTube 需要登录 Cookie 才能解析",
        "YouTube 2025 起对服务器数据中心 IP 强制 bot 检测，需带登录态才能绕过。\n"
        "① 最直接：在「高级选项 → Cookie」粘贴一次 YouTube 登录 Cookie，随后重试；\n"
        "② 免手动：桌面版在本机浏览器登录过 YouTube 后，登录态会被自动同步"
        "（每 30 分钟一次）到服务器，无需粘贴。",
        category="cookie_required",
    )

# =========================================================================== #
# B 类平台专用提取器（平台列表已收录但 yt-dlp 无原生提取器）
#   netease（网易云 MV）/ tudou（土豆→优酷合并）/ weishi（微视）/
#   yy（YY 直播）/ hotstar（Disney+ Hotstar）/ kinopoisk（KinoPoisk）
# 说明：hotstar / kinopoisk 实际 yt-dlp 已有提取器，之前误报"暂未实现"是
# 测试 URL/域名不匹配所致，这里显式分发让它们走 yt-dlp 正常解析。
# =========================================================================== #

# 网易云 MV：yt-dlp 的 NetEaseIE 对 MV 在数据中心 IP 返回 404（风控），且无稳定
# 提取器。改走社区逆向的 weapi 加密（AES-128-ECB 两层，cryptography 库实现，
# 不新增依赖），直连 music.163.com 官方接口拿 MV 直链（多清晰度 brs）。
_NE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_NE_NONCE = b"0CoJUm6Qyw8W8jud"
_NE_SEC_KEY = b"WV9w9Bpu0rX0l3sN"

def _ne_aes_ecb(key: bytes, data: bytes) -> bytes:
    e = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return e.update(data) + e.finalize()

def _ne_weapi(obj: dict) -> tuple[str, str]:
    """网易云 weapi 加密：明文 → AES( nonce, PKCS7 ) → AES( seckey, PKCS7 ) → base64。"""
    text = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    padder = PKCS7(128).padder()
    e1 = _ne_aes_ecb(_NE_NONCE, padder.update(text) + padder.finalize())
    padder2 = PKCS7(128).padder()
    e2 = _ne_aes_ecb(_NE_SEC_KEY, padder2.update(e1) + padder2.finalize())
    return base64.b64encode(e2).decode(), base64.b64encode(_NE_SEC_KEY).decode()

def _ne_http_get(url: str, proxy: str = "", headers: dict | None = None) -> str:
    h = {"User-Agent": _NE_UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    if headers:
        h.update(headers)
    proxies = {"http": proxy, "https": proxy} if proxy else None
    import requests as _req
    r = _req.get(url, headers=h, proxies=proxies, timeout=20)
    r.raise_for_status()
    return r.text

def _ne_trial_only(info: dict[str, Any]) -> bool:
    """判断 yt-dlp 拿到的是「30 秒试听片段」而不是完整音轨。

    网易云对 VIP 专享曲目在未登录时只下发 30s 试听（CDN 路径形如
    `.../jd-musicrep-ts/...mp3`），但 yt-dlp 返回的 `duration` 仍是整首长度 ——
    用户会静悄悄拿到一个 30 秒的文件（2026-10-25 实测《我不难过》：探针报 320s，
    实际落盘 30s / 481KB）。
    判据用「体积 ÷ 时长 ÷ 码率」反推（不依赖 CDN 路径关键字，抗改名）：
    完整音轨应 ≈ duration × abr / 8，试听片段只有十分之一左右。
    """
    duration = float(info.get("duration") or 0)
    if duration <= 0:
        return False
    best = 0.0
    for f in info.get("formats") or []:
        if not isinstance(f, dict):
            continue
        size = f.get("filesize") or f.get("filesize_approx") or 0
        abr = f.get("abr") or 128
        if size and abr:
            ratio = float(size) / (duration * float(abr) * 1000 / 8)
            best = max(best, ratio)
    return 0 < best < 0.5

def _ne_probe(url: str, cookie: str = "", proxy: str = "") -> dict[str, Any]:
    """网易云单曲 / MV 解析：先走 yt-dlp 原生提取器，失败再退 weapi（仅 MV）。

    2026-10-25 实测：打包的 yt-dlp 自带 NetEaseMusic（单曲，1 条音轨）与
    NetEaseMusicMv（MV，3 个清晰度）提取器，直连即可解析。旧实现不分单曲/MV
    一律打 weapi `/mv/detail`，单曲 ID 被当作 MV 去查 ⇒ 接口返回非 JSON ⇒
    用户看到「JSONDecodeError: Expecting value: line 1 column 1」这种没法看的错。
    weapi 保留为 MV 兜底（个别 MV 详情接口比网页版稳）。

    另：VIP 专享曲目未登录只有 30s 试听，必须当场拦下（见 `_ne_trial_only`），
    否则用户会以为下到了整首。
    """
    try:
        opts = _base_options(PROBE_RETRIES, "music.163.com", cookie=cookie, proxy=proxy)
        opts["format"] = None
        with _YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if info and (info.get("formats") or info.get("url")):
            if _ne_trial_only(info):
                raise ResolveError(
                    "这首歌是 VIP 专享，未登录只能试听 30 秒",
                    "网易云对会员专享曲目只给未登录访客 30 秒试听片段，下载下来会是半截音频。\n\n"
                    "两个办法：\n"
                    "① 在 Chrome / Edge / UC 里登录网易云音乐，回到视频工坊重新点「解析」"
                    "（会自动读取浏览器登录态，有会员就能拿完整音轨）；\n"
                    "② 换一首非 VIP 曲目（页面上没有「VIP」角标的）再解析。",
                    category="need_vip",
                )
            return info
    except ResolveError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.info("[netease] yt-dlp 解析失败，尝试 weapi 兜底：%s", str(e)[:140])
    if re.search(r"(?:/mv\?id=|/mv/)", url or ""):
        return _ne_info(url, proxy=proxy)
    raise ResolveError(
        "网易云单曲解析失败",
        "yt-dlp 没能解析出这条单曲（可能版权受限/已下架，或当前出口 IP 被风控限流）。\n"
        "请确认链接形如 music.163.com/song?id=数字，稍后重试，或换一首歌再试。",
        category="parse_failed",
    )

def _ne_info(url: str, proxy: str = "") -> dict[str, Any]:
    """网易云音乐 MV 解析（music.163.com/mv?id=xxx）。"""
    m = re.search(r"(?:/mv\?id=|/mv/|id=)(\d+)", url)
    if not m:
        raise ResolveError(
            "网易云 MV 解析失败", "无法从链接中识别 MV ID（应为 music.163.com/mv?id=数字）。",
            category="parse_failed",
        )
    mv_id = m.group(1)
    params, enc_sec = _ne_weapi({"id": mv_id, "csrf_token": ""})
    data = ("params=%s&encSecKey=%s" % (params, enc_sec)).encode()
    try:
        import requests as _req
        r = _req.post(
            "https://music.163.com/weapi/mv/detail/?csrf_token=",
            data=data,
            headers={
                "User-Agent": _NE_UA,
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": "https://music.163.com/",
            },
            proxies={"http": proxy, "https": proxy} if proxy else None,
            timeout=20,
        )
        j = r.json()
    except Exception as e:
        raise ResolveError(
            "网易云 MV 解析失败", f"请求网易云接口异常：{type(e).__name__} {e}", category="parse_failed"
        ) from None
    if j.get("code") not in (200, 0) or not j.get("data"):
        raise ResolveError(
            "网易云 MV 解析失败",
            "网易云返回空数据（可能是该 MV 已下架，或当前出口 IP 被风控限流）。",
            category="parse_failed",
        )
    d = j["data"]
    # brs 为多清晰度字典 {br: url}，优先选最高清晰度（br 数值越大越清晰）
    brs = d.get("brs") or {}
    best_url = d.get("url") or ""
    if not best_url and brs:
        try:
            _best_br = max(int(k) for k in brs.keys() if str(k).isdigit())
            best_url = brs[str(_best_br)] or brs.get(_best_br) or ""
        except Exception:
            best_url = list(brs.values())[0]
    if not best_url:
        raise ResolveError(
            "网易云 MV 解析失败", "未找到可用的 MV 视频流地址。", category="parse_failed"
        )
    return {
        "id": mv_id,
        "title": d.get("name") or "网易云 MV",
        "duration": (d.get("duration") or 0) / 1000.0 if d.get("duration") else None,
        "thumbnail": d.get("cover") or d.get("picUrl") or "",
        "artist": (d.get("artistName") or ""),
        "webpage_url": url,
        "extractor_key": "NetEaseMV",
        "extractor": "netease",
        "ext": "mp4",
        "direct": True,
        "url": best_url,
        "protocol": "https",
        "http_headers": {"User-Agent": _NE_UA, "Referer": "https://music.163.com/"},
    }

def _write_netscape_cookies(cookie_header: str, domain: str) -> str:
    """把 `k=v; k2=v2` 的 Cookie 头写成 Netscape Cookie 临时文件，返回路径。

    yt-dlp 的 IxiguaIE 用 `self._get_cookies(url)` 判断「有没有 Cookie」，读的是
    cookiejar；直接塞 `http_headers["Cookie"]` 不生效（仍会报 "Cookies are needed"），
    必须先落成 cookiefile 让 yt-dlp 载入。调用方用完负责删除。
    """
    import tempfile
    import time as _t
    lines = ["# Netscape HTTP Cookie File"]
    exp = int(_t.time()) + 86400
    dom = domain if domain.startswith(".") else "." + domain
    for part in cookie_header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name, value = name.strip(), value.strip()
        if not name or not value:
            continue
        lines.append("\t".join([dom, "TRUE", "/", "FALSE", str(exp), name, value]))
    fd, path = tempfile.mkstemp(prefix="vdl_ck_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path

def _ixigua_info(url: str, cookie: str = "", proxy: str = "") -> dict[str, Any]:
    """西瓜视频解析（yt-dlp 原生 IxiguaIE + 浏览器登录态）。

    2026-10-25 实测三种情况：
      ① 不带 Cookie：IxiguaIE 直接抛「Cookies (not necessarily logged in) are needed」；
      ② 只带匿名 `__ac_nonce`：拿到的是反爬挑战页，没有 SSR_HYDRATED_DATA；
      ③ 必须用真实浏览器 Cookie（含 `__ac_signature`），即用户浏览器访问过 xigua.com。
    所以这里：有本机浏览器 Cookie 就带 Cookie 文件解析；没有就直接给可操作提示，
    不做无意义的 20s 重试。旧代码把 ixigua.com/<ID> 改写成 douyin.com/video/<ID>
    （注释称「视频 ID 互通」）——实测不成立，已删除。
    """
    ck = (cookie or "").strip() or (get_browser_cookie_header("www.ixigua.com", url) or "")
    if not ck:
        raise ResolveError(
            "西瓜视频需要浏览器 Cookie",
            "西瓜视频（ixigua.com）从 2026 年起对无 Cookie 的请求下发反爬挑战页，"
            "拿不到播放数据（不是链接失效）。\n\n"
            "解决办法（一次性，约 30 秒）：\n"
            "① 用 Chrome 或 Edge 打开 https://www.ixigua.com ，正常浏览/登录一次；\n"
            "② 回到视频工坊重新点「解析」——本机会自动读取该浏览器 Cookie，无需手动粘贴。\n\n"
            "另一条路：同一个视频通常也能在抖音找到，用抖音链接下载（抖音解析无需登录）。",
            category="cookie_required",
        )
    ck_path = ""
    try:
        ck_path = _write_netscape_cookies(ck, "ixigua.com")
        opts = _base_options(PROBE_RETRIES, "www.ixigua.com", cookie=ck, proxy=proxy)
        opts["format"] = None
        opts["cookiefile"] = ck_path
        hdr = dict(opts.get("http_headers") or {})
        hdr.setdefault("Referer", "https://www.ixigua.com/")
        opts["http_headers"] = hdr
        with _YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if info and info.get("formats"):
            return info
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "SSR_HYDRATED_DATA" in msg or "Cookies" in msg:
            raise ResolveError(
                "西瓜视频反爬校验未通过",
                "浏览器里的西瓜 Cookie 已过期或缺少反爬标记（`__ac_signature`）。\n"
                "请用 Chrome/Edge 打开 https://www.ixigua.com 随便看一个视频（让站点刷新 Cookie），"
                "再回视频工坊重试点「解析」。",
                category="cookie_invalid_or_expired",
            ) from None
        raise ResolveError("西瓜视频解析失败", msg[:300], category="parse_failed") from None
    finally:
        if ck_path:
            try:
                os.remove(ck_path)
            except Exception:
                pass
    raise ResolveError(
        "西瓜视频解析失败", "页面里没有可下载的视频流（可能已删除或仅限 App 观看）。",
        category="parse_failed",
    )


# 金牌影院（nmwchxx.com）：第三方影视聚合站（Next.js SPA），yt-dlp 无提取器。
# 播放地址不在页面里，而是前端 JS 调自签 API：
#   GET /api/mw-movie/anonymous/v2/video/episode/url?clientType=1&id={vodId}&nid={集ID}
# 签名算法（2026-10-14 从前端 webpack 模块 49858 逆出，两组真实抓包交叉验证）：
#   x    = "排序参数串&key=<SIGN_KEY>&t=<毫秒时间戳>"
#   sign = SHA1( MD5_hex(x) )        # 注意是 SHA1(MD5(x)) 双层，不是单层 SHA1
# SIGN_KEY 硬编码在前端 JS 里；匿名接口免登录，deviceId 随机 UUID 即可。
# ⚠️ CDN 侧 m3u8 URL 带 whip=<请求方IP>，IP 绑定：解析与下载必须同机（桌面端
#    本机解析本机下，天然成立；换 IP 直拉会 403）。
_NMW_SIGN_KEY = "cb808529bae6b6be45ecfab29a4889bc"
_NMW_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 这类聚合站会周期性换域名（同一套 API 跟着域名漂移）。只认一个死域名
# ⇒ 对方换一次域，整站解析全挂，而接口其实没变。已知域名 + nmw 前缀兜底。
# chrqj.com 是老域名：2026-10-21 实测首页 302 → nmwchxx.com，同一套
# mw-movie API、同一个签名密钥、同一对视频 ID 都能出《少帅》第5集。
# 早期还给它单写过一套 yt-dlp 插件（yt_dlp_plugins/extractor/chrqj.py），
# 链接形态也不同（三段式 /vod/play/{id}/{源}/{集}），在此一并收编。
_NMW_KNOWN_HOSTS = ("nmwchxx.com", "chrqj.com")
_NMW_HOST_RE = re.compile(
    r"^nmw[a-z0-9]{1,8}\.(?:com|net|cc|tv|top|xyz|vip|site|club|info|pro)$"
)


def _nmw_host(url: str) -> str:
    """从 URL 取小写 host（去端口），取不到返回空串。"""
    m = re.match(r"^https?://([^/?#]+)", url or "", re.I)
    if not m:
        return ""
    return m.group(1).split("@")[-1].split(":")[0].lower()


def _is_nmw_host(host: str) -> bool:
    """是否金牌影院这一族域名（含换过的域）。"""
    h = (host or "").lower()
    if not h:
        return False
    for d in _NMW_KNOWN_HOSTS:
        if h == d or h.endswith("." + d):
            return True
    return bool(_NMW_HOST_RE.match(h))


def _nmw_parse_ids(url: str) -> tuple[str, str, str]:
    """从播放/详情链接里取 (视频 ID, 集 ID, 集序号)。

    站点同一集有好几种链接写法，只认一种就会「今天能解析、明天又不行」
    （2026-10-21 用户第三次报错，粘的就是 /vod/play/116537/877419）：

      /vod/play/116537/sid/877419     站内标准（页面 200）
      /vod/play/116537/877419         分享/外链常见（站内 404，但两个 ID 有效）
      /vod/play/116537/1/877419       老域名 chrqj.com 外链：中间是「源序号」，不是集号
      /vod/play/116537-1-1.html       maccms 老模板（源序号-集序号）
      /vod/detail/116537              详情页：只有视频 ID，集号返回空

    两个 ID 是接口唯一需要的输入，与页面是否 200 无关 ⇒ 只要能取出数字就解析。
    三段式必须排在两段式**之前**：否则 /vod/play/116537/1/877419 会被
    两段式正则吃掉，把「源序号 1」当成集号，解析出错误的集。
    """
    u = url or ""
    # ① maccms 老模板：最后一组数字是集序号（1 起）
    m = re.search(r"/vod/play[/-](\d+)[-_]\d+[-_](\d+)\.html", u)
    if m:
        return m.group(1), "", m.group(2)
    # ② 三段式（老域名外链）：/vod/play/{影片id}/{源序号}/{集id}，取首尾两组
    m = re.search(r"/vod/play/(\d+)/\d+/(\d+)", u)
    if m:
        return m.group(1), m.group(2), ""
    # ③ 播放页：/sid/ 可有可无
    m = re.search(r"/vod/play/(\d+)/(?:sid/)?(\d+)", u)
    if m:
        return m.group(1), m.group(2), ""
    # ④ 详情页
    m = re.search(r"/vod/(?:detail|info)/(\d+)", u)
    if m:
        return m.group(1), "", ""
    # ⑤ 纯 query 形态（?id=116537&nid=877419 / ?vodId=…）
    qv = re.search(r"[?&](?:id|vodId|vod_id|movieId)=(\d+)", u)
    if qv:
        qn = re.search(r"[?&](?:nid|sid|episodeId|episode)=(\d+)", u)
        return qv.group(1), (qn.group(1) if qn else ""), ""
    return "", "", ""


def _nmw_api(path: str, params: dict, referer: str, proxy: str = "", host: str = "") -> dict:
    """调用金牌影院自签 API，返回 JSON dict；网络/解析异常抛 ResolveError。

    host 传入用户实际访问的域：域名漂移时先打它，失败再回落已知域名。
    """
    import hashlib
    import uuid

    import requests as _req

    t = str(int(time.time() * 1000))
    qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
    x = f"{qs}&key={_NMW_SIGN_KEY}&t={t}"
    sign = hashlib.sha1(hashlib.md5(x.encode()).hexdigest().encode()).hexdigest()

    bases: list[str] = []
    h = (host or "").strip().lower()
    if h and _is_nmw_host(h) and h not in _NMW_KNOWN_HOSTS:
        bases.append(h)
    for d in _NMW_KNOWN_HOSTS:
        if d not in bases:
            bases.append(d)

    last: Exception | None = None
    for base in bases:
        try:
            r = _req.get(
                f"https://{base}/api{path}?{qs}",
                headers={
                    "sign": sign,
                    "t": t,
                    "client-type": "1",
                    "deviceId": str(uuid.uuid4()),
                    "authorization": "",
                    "Referer": referer or f"https://{base}/",
                    "User-Agent": _NMW_UA,
                    "Accept": "application/json, text/plain, */*",
                },
                proxies={"http": proxy, "https": proxy} if proxy else None,
                timeout=20,
            )
            return r.json()
        except Exception as e:  # 换域/单个域名不通：继续试下一个
            last = e
            continue
    raise ResolveError(
        "金牌影院解析失败",
        f"请求接口异常：{type(last).__name__} {last}",
        category="parse_failed",
    ) from None


def _nmw_info(url: str, proxy: str = "") -> dict[str, Any]:
    """金牌影院解析（/vod/play/{id}/sid/{nid}，也接受 {id}/{nid} 与详情页）。

    链路：detail 接口拿片名/集名（失败不阻断）→ episode/url 接口拿多清晰度
    m3u8（蓝光/高清/标清；auth_key 带时效，解析后应尽快下载）。
    """
    vid, nid, ep_idx = _nmw_parse_ids(url)
    if not vid:
        raise ResolveError(
            "金牌影院解析失败",
            "无法从链接中识别视频 ID。请粘贴某一集的播放页链接，"
            "形如 nmwchxx.com/vod/play/数字/sid/数字 或 nmwchxx.com/vod/play/数字/数字。",
            category="parse_failed",
        )
    host = _nmw_host(url)

    # 片名 + 集名（detail 失败不阻断：拿不到标题就用 ID 兜底）
    title = f"金牌影院视频 {nid or vid}"
    eps: list[dict[str, Any]] = []
    try:
        d = _nmw_api("/mw-movie/anonymous/video/detail", {"id": vid}, url,
                     proxy=proxy, host=host)
        data = d.get("data") or {}
        vod_name = str(data.get("vodName") or "").strip()
        eps = [e for e in (data.get("episodeList") or []) if isinstance(e, dict)]
        ep = next((e for e in eps if str(e.get("nid")) == nid), None)
        ep_name = str((ep or {}).get("name") or "").strip()
        if vod_name:
            title = f"{vod_name} 第{ep_name}集" if (ep_name and len(eps) > 1) else vod_name
    except ResolveError:
        pass

    # 链接里没集 ID（详情页 / maccms 模板）时，用 detail 的集列表补齐：
    # 给的是集序号就取对应那集，什么都没给就取第一集（站内默认行为）。
    if not nid:
        pick: dict[str, Any] | None = None
        if ep_idx and ep_idx.isdigit():
            i = int(ep_idx) - 1
            if 0 <= i < len(eps):
                pick = eps[i]
        if pick is None and eps:
            pick = eps[0]
        if pick is not None and pick.get("nid"):
            nid = str(pick.get("nid"))
            ep_name = str(pick.get("name") or "").strip()
            if len(eps) > 1 and ep_name and ep_name not in title:
                title = f"{title} 第{ep_name}集"
    if not nid:
        raise ResolveError(
            "金牌影院解析失败",
            "链接里没有剧集 ID，且详情接口也没返回剧集列表（该剧可能已下架）。",
            category="parse_failed",
        )

    r = _nmw_api(
        "/mw-movie/anonymous/v2/video/episode/url",
        {"clientType": "1", "id": vid, "nid": nid}, url, proxy=proxy, host=host,
    )
    if r.get("code") != 200:
        raise ResolveError(
            "金牌影院解析失败",
            f"接口返回：{r.get('msg') or r.get('code')}（该视频可能已下架或链接失效）。",
            category="parse_failed",
        )
    lst = (r.get("data") or {}).get("list") or []
    entries = [e for e in lst if isinstance(e, dict) and e.get("url")]
    if not entries:
        raise ResolveError(
            "金牌影院解析失败", "接口未返回任何播放地址（该集可能已下架）。",
            category="parse_failed",
        )
    entries.sort(key=lambda e: int(e.get("resolution") or 0), reverse=True)
    best = entries[0]
    ref = f"https://{host}/" if (host and _is_nmw_host(host)) else f"https://{_NMW_KNOWN_HOSTS[0]}/"
    formats = [
        {
            "format_id": f"nmw-{e.get('resolution') or 'sd'}",
            "url": e["url"],
            "ext": "mp4",
            "height": int(e.get("resolution") or 0) or None,
            "format": str(e.get("resolutionName") or ""),
            "protocol": "m3u8_native",
            "http_headers": {"User-Agent": _NMW_UA, "Referer": ref},
        }
        for e in entries
    ]
    return {
        "id": nid,
        "title": title,
        "webpage_url": url,
        "extractor_key": "NMWChxx",
        "extractor": "nmwchxx",
        "ext": "mp4",
        "direct": True,
        "url": best["url"],
        "protocol": "m3u8_native",
        "formats": formats,
        "http_headers": {"User-Agent": _NMW_UA, "Referer": ref},
        # 标记 m3u8 直连（下载时用 ffmpeg/yt-dlp 带 header 拉流）
        "_nmw_m3u8": True,
    }

def _tudou_info(url: str, proxy: str = "") -> dict[str, Any]:
    """土豆：2016 年起已并入优酷、主站停止运营。

    若用户给出的是仍能解析的旧土豆视频链接，尝试 302 展开看是否跳转到 youku，
    是则转优酷通道复用其解析能力；否则明确告知已停运，不再误报"暂未实现解析器"。
    """
    try:
        expanded = _expand_generic_302(url, proxy=proxy, allowed_hosts=("youku.com", "v.youku.com"))
        if "youku.com" in expanded and expanded != url:
            logger.info("[tudou] 重定向到优酷，转优酷通道: %s", expanded)
            if expanded.endswith("youku.com"):
                expanded = expanded.replace("www.youku.com", "v.youku.com", 1)
            return _youku_info(expanded, "", "", proxy=proxy)
    except Exception as e:
        logger.info("[tudou] 展开失败: %s", str(e)[:120])
    raise ResolveError(
        "土豆视频已停止运营",
        "土豆网于 2016 年并入优酷，原站视频已迁移/下线。\n"
        "如果该视频在优酷仍有存档，请直接粘贴优酷链接（v.youku.com/...）到 VDL 解析。",
        category="service_discontinued",
    )

def _weishi_info(url: str, proxy: str = "") -> dict[str, Any]:
    """微视（weishi.qq.com）：腾讯系短视频，播放地址原本需 App 端签名（wskey/rticket）。

    解析策略（2026-08-24 接入 VPS worker）：
    1) 若配置了 VPS 解析节点（VDL_COOKIE_REFILL_TOKEN 等），优先走 worker：
       worker 先试 H5 公开接口 WSH5GetPlayPage 拿**无水印**直链（最快），失败则
       Playwright 真实浏览器开播放页抓 <video> 真实流兜底。
    2) 未配置 worker → best-effort 降级：抓取播放页 HTML 看是否有内嵌视频 JSON。
    3) worker 解析失败（接口404/链接失效/无流）→ 透传真实原因，不伪装。
    """
    # 优先走 VPS worker（能真实拿到流，含无水印直链）
    token = os.environ.get("VDL_COOKIE_REFILL_TOKEN") or os.environ.get("VDL_COOKIE_SYNC_TOKEN", "")
    if token:
        try:
            data = _call_vps_worker("weishi", url)
            # worker 成功返回 dict（含 video_url / title / ext）
            return {
                "id": data.get("video_id", ""),
                "title": data.get("title") or "微视视频",
                "duration": data.get("duration"),
                "webpage_url": data.get("webpage_url") or url,
                "extractor_key": "WeiShi",
                "extractor": "weishi",
                "ext": data.get("ext") or "mp4",
                "direct": True,
                "url": data.get("video_url"),
                "protocol": "https",
                "thumbnail": data.get("thumbnail", ""),
                "http_headers": {"User-Agent": _NE_UA, "Referer": "https://h5.weishi.qq.com/"},
            }
        except ResolveError:
            raise  # 透传 worker 真实错误（链接失效/无流/不可达等）

    # 降级 best-effort：无 worker 时抓 HTML 内嵌 JSON（多为广告/失效场景）
    try:
        html = _ne_http_get(url, proxy=proxy, headers={"Referer": "https://weishi.qq.com/"})
        if "feedid" in url or "feed/" in url:
            for pat in (r'"videoUrl"\s*:\s*"([^"]+)"', r'"playUrl"\s*:\s*"([^"]+)"',
                        r'"video_url"\s*:\s*"([^"]+)"'):
                m = re.search(pat, html)
                if m:
                    vurl = m.group(1).replace("\\u0026", "&").replace("\\/", "/")
                    if vurl.startswith("//"):
                        vurl = "https:" + vurl
                    _ok_cdn = ("vweishi" in vurl or "weishi" in vurl or "gtimg.cn/qzone/weishi" in vurl
                               or vurl.endswith(".mp4"))
                    _bad_cdn = ("qzact" in vurl or "act/extern" in vurl or "gtimg.cn/qzact" in vurl)
                    if _ok_cdn and not _bad_cdn:
                        return {
                            "id": "", "title": "微视视频", "duration": None,
                            "webpage_url": url, "extractor_key": "WeiShi",
                            "extractor": "weishi", "ext": "mp4", "direct": True,
                            "url": vurl, "protocol": "https",
                            "http_headers": {"User-Agent": _NE_UA, "Referer": "https://weishi.qq.com/"},
                        }
    except Exception as e:
        logger.info("[weishi] 抓取失败: %s", str(e)[:120])
    raise ResolveError(
        "微视暂不支持解析",
        "微视的播放地址由手机 App 端签名生成，无公开网页直链接口，VDL 暂无法提取。\n"
        "① 若原视频也发在微信/QQ 内，可尝试用腾讯视频链接（v.qq.com）解析；\n"
        "② 页面源码里若有 .mp4 直链，可直接粘贴直链下载。\n"
        "③ 若已配置 VPS 解析节点但仍失败，多为链接已失效（微视已收缩运营，分享页会跳404）。",
        category="pending_extractor",
    )

def _finder_info(url: str, cookie: str = "", proxy: str = "") -> dict[str, Any]:
    """微信视频号（weixin.qq.com/sph/* → channels.weixin.qq.com/finder-preview/*）。

    视频号的播放地址由微信客户端/网页登录态签名生成，游客态（无微信会话 Cookie）
    打开分享链接只会看到「请在微信中打开」的壳，拿不到真实视频流。必须用带微信
    登录态的浏览器会话才能加载播放器并请求 CDN 流。

    解析策略（2026-08-24 接入 VPS worker + 用户自带 Cookie）：
    1) 必须走 VPS worker（finder_resolve）：worker 注入微信登录态 Cookie 后真实
       浏览器抓流。无 worker 配置 → 直接诚实报错（不伪装 best-effort，因为游客态
       真的拿不到）。
    2) 登录态来源优先级：
       a. 用户手动粘贴的微信 Cookie（cookie 参数，最高优先，随请求透传 worker）；
       b. 云端共享池 weixin.qq.com Cookie（其他用户/贡献者上报的兜底）；
       c. VPS 本地 /opt/vdl-worker/cookies/weixin.txt（worker 内部兜底）。
    3) worker 解析失败（Cookie 过期 / 链接失效）→ 透传真实原因。
    """
    token = os.environ.get("VDL_COOKIE_REFILL_TOKEN") or os.environ.get("VDL_COOKIE_SYNC_TOKEN", "")
    if token:
        # 用户没自带 Cookie 时，先试云端共享池兜底（weixin.qq.com 域）
        effective_cookie = (cookie or "").strip()
        if not effective_cookie:
            # 本机池 miss 时回读云端池（桌面端同样受益）
            effective_cookie = _pooled_cookie("weixin.qq.com")
        try:
            data = _call_vps_worker("finder", url, cookie=effective_cookie)
            return {
                "id": data.get("video_id", ""),
                "title": data.get("title") or "微信视频号视频",
                "duration": data.get("duration"),
                "webpage_url": data.get("webpage_url") or url,
                "extractor_key": "WeiXinFinder",
                "extractor": "finder",
                "ext": data.get("ext") or "mp4",
                "direct": True,
                "url": data.get("video_url"),
                "protocol": "https",
                "thumbnail": data.get("thumbnail", ""),
                "http_headers": {"User-Agent": _NE_UA, "Referer": "https://channels.weixin.qq.com/"},
            }
        except ResolveError as e:
            # 视频号登录态相关错误（无 Cookie / 需登录 / 登录态失效）统一归为
            # cookie_required，让前端亮「去粘贴 Cookie」按钮引导用户自带上。
            # ⚠️ 注意检查 hint（worker 的真实错误文本），而非 message（多为通用
            # 的"视频解析失败"，不含平台细节）。
            if getattr(e, "category", None) not in ("cookie_required", "cookie_invalid_or_expired"):
                _msg = str(e) + "\n" + str(getattr(e, "hint", "") or "")
                if any(k in _msg for k in ("微信登录态", "请粘贴", "登录态已失效", "请在微信中打开", "无登录态")):
                    e.category = "cookie_required"
            raise  # 透传 worker 真实错误（无登录态/链接失效/被风控等）

    # 未配置 worker：视频号游客态无论如何拿不到流，诚实告知
    raise ResolveError(
        "微信视频号暂不支持解析",
        "微信视频号的播放地址需微信登录态签名，无登录态时网页只返回"
        "「请在微信中打开」的壳，无法提取视频流。\n"
        "① 请在微信内打开该视频，确认可正常播放；\n"
        "② 本服务已支持经 VPS 解析节点解析视频号，但需先配置微信登录态 Cookie"
        "（在已登录微信的浏览器复制 channels.weixin.qq.com 的 Cookie 粘贴给运维即可）。",
        category="pending_extractor",
    )
def _yy_info(url: str, proxy: str = "") -> dict[str, Any]:
    """YY 直播（yy.com / h.yy.com）：直播流地址需 App 端签名，公开 web 接口拿不到直链。
    best-effort：尝试从播放页/公开 API 提取，失败明确告知。
    """
    try:
        # 公开房间信息接口（无需签名）有时能拿到 stream 相关字段，作为兜底探测
        m = re.search(r"(?:/([a-z0-9_-]+)\.html|uid=|roomId=(\d+)|sid=(\d+))", url)
        html = _ne_http_get(url, proxy=proxy, headers={"Referer": "https://www.yy.com/"})
        for pat in (r'"liveUrl"\s*:\s*"([^"]+)"', r'"streamUrl"\s*:\s*"([^"]+)"',
                    r'"hlsUrl"\s*:\s*"([^"]+)"', r'https?://[^"\'\s]+\.m3u8'):
            mm = re.search(pat, html)
            if mm:
                surl = mm.group(1) if mm.groups() else mm.group(0)
                if surl.startswith("//"):
                    surl = "https:" + surl
                return {
                    "id": "", "title": "YY 直播", "duration": None,
                    "webpage_url": url, "extractor_key": "YYLive",
                    "extractor": "yy", "ext": "mp4", "direct": False,
                    "protocol": "m3u8_native", "url": surl,
                    "http_headers": {"User-Agent": _NE_UA, "Referer": "https://www.yy.com/"},
                }
    except Exception as e:
        logger.info("[yy] 抓取失败: %s", str(e)[:120])
    raise ResolveError(
        "YY 直播暂不支持解析",
        "YY 直播的流地址由客户端签名生成，公开接口无法获取直链，VDL 暂无法提取。\n"
        "① 若是精彩回放且页面源码含 .m3u8 直链，可直接粘贴直链下载；\n"
        "② 更多平台陆续支持中。",
        category="pending_extractor",
    )


# --------------------------------------------------------------------------- #
# 搜狐多分片（SohuIE 返回 _type=multi_video）
# --------------------------------------------------------------------------- #
# 🔴 2026-10-25 实测（用户报「搜狐解析器要重写」）：
#   · tv.sohu.com 一集剧被 SohuIE 切成 N 个 mp4（part1 常只有 6~10s 片头，
#     part2..partN 各 ~300s），返回的是 _type=multi_video；
#   · 原实现两处都只取 entries[0]（probe 与 _run_once 各一处）⇒ 用户下到的是
#     9 秒 / 472KB 的片头，看起来像「解析失败」；
#   · 把整个 multi_video 交给 yt-dlp 也不会自动合并：outtmpl 相同时后片会
#     **覆盖前片**（实测 12 片只剩最后一片），更不能这么做。
# 正确链路：probe 按 format_id 聚合各片（体积/时长求和）→ 下载逐片落到
# 工作目录的隐藏子目录 → ffmpeg concat（-c copy 无损）拼成单片 → 清理分片。
_SOHU_PART_DIRNAME = ".vdlparts"


def _sohu_is_multi(info: dict[str, Any]) -> bool:
    """是否搜狐式「同一集切成 N 片」（multi_video）。

    ⚠️ 只认 multi_video：搜狐返回 playlist 时是**多集列表**，那要按第一集处理，
    绝不能当分片拼起来——拼了就是「整部剧塞一个文件」。
    """
    return info.get("_type") == "multi_video" and bool(info.get("entries"))


def _sohu_aggregate(info: dict[str, Any]) -> dict[str, Any]:
    """把 N 个分片聚合成「单片形态」：formats 按 format_id 合并、时长/体积求和。

    返回的 info 额外带 `_vdl_parts`（每片 format_id → 格式 dict），交给
    `_sohu_download_parts` 逐片下载；probe 返回前会把这个字段剥掉（含直链，
    不进前端）。
    """
    entries = [e for e in (info.get("entries") or []) if e and e.get("formats")]
    if not entries:
        raise ResolveError("该视频无法解析出分片", "请更换链接或稍后重试")
    part_maps: list[dict[str, dict]] = []
    for e in entries:
        m: dict[str, dict] = {}
        for f in e.get("formats") or []:
            fid = str(f.get("format_id") or "").strip()
            if fid and f.get("url"):
                m[fid] = f
        part_maps.append(m)
    # 只保留「每一片都有」的档位：缺片的档位下出来会在中间断掉
    common = set(part_maps[0])
    for m in part_maps[1:]:
        common &= set(m)
    total_duration = sum(float(e.get("duration") or 0) for e in entries) or None
    agg: list[dict[str, Any]] = []
    for fid in sorted(common):
        sizes = [int(m[fid].get("filesize") or 0) for m in part_maps]
        item = dict(part_maps[0][fid])
        item["format_id"] = fid
        item["filesize"] = sum(sizes) if all(sizes) else None
        item["duration"] = total_duration
        agg.append(item)
    if not agg:
        # 档位对不齐（站点偶尔给不同片不同码率档）：退回第一片的档位，
        # 下载时逐片各自选，至少不空手。
        agg = [dict(f) for f in part_maps[0].values()]
    merged = {k: v for k, v in info.items() if k not in ("entries", "_type")}
    merged.update({
        "formats": agg,
        "duration": total_duration,
        # 存**原始 entry**（不是精简 dict）：process_info 依赖 entry 里的
        # display_id/url/format_id/ext/protocol 等字段，字段缺失会退化成
        # "No video formats found"（2026-10-25 实测踩过）。
        "_vdl_parts": entries,
        "_vdl_part_count": len(entries),
    })
    logger.info("[sohu] 多分片聚合：%d 段 / %d 档 / 总时长 %.1fs",
                len(entries), len(agg), total_duration or 0)
    return merged


def _ffmpeg_bin() -> str:
    """定位 ffmpeg（打包版把 ffmpeg 放在 exe 同级/内置目录，需用绝对路径）。"""
    cand = os.environ.get("VDL_FFMPEG_BIN", "").strip()
    if cand and Path(cand).exists():
        return cand
    found = shutil.which("ffmpeg")
    if found:
        return found
    roots = [Path(sys.executable).parent, Path(getattr(sys, "_MEIPASS", "") or ".")]
    for root in roots:
        for name in ("ffmpeg.exe", "ffmpeg"):
            p = root / name
            if p.exists():
                return str(p)
    for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if Path(p).exists():
            return p
    return "ffmpeg"


def _safe_media_name(name: str, default: str = "video") -> str:
    """把标题清洗成 Windows/macOS 都合法的文件名（不含扩展名）。"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", (name or "").strip())
    s = re.sub(r"\s+", " ", s).strip(" .")
    return (s or default)[:MAX_TITLE_CHARS]


def _sohu_concat(part_files: list[Path], out: Path, task: DownloadTask) -> Path:
    """ffmpeg concat demuxer 无损拼接同源分片；失败时逐级降级。"""
    ff = _ffmpeg_bin()
    lst = out.with_suffix(".concat.txt")
    # concat demuxer 的 file 指令：单引号包裹，内部单引号用 '\'' 转义
    lst.write_text(
        "".join("file '%s'\n" % str(p).replace("'", "'\\''") for p in part_files),
        encoding="utf-8",
    )
    attempts = [
        ["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", "-movflags", "+faststart"],
        # 时间戳不连续（个别片首帧 PTS 归零）时补 genpts 再拼
        ["-fflags", "+genpts", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy"],
    ]
    last_err = ""
    for i, args in enumerate(attempts):
        try:
            res = subprocess.run(
                [ff, "-v", "error", "-y", *args, str(out)],
                capture_output=True, text=True, timeout=1800,
            )
            if res.returncode == 0 and out.exists() and out.stat().st_size > 1024:
                lst.unlink(missing_ok=True)
                return out
            last_err = (res.stderr or "")[-300:]
            task.log(f"合并尝试 {i + 1} 未成功：{last_err[:120]}")
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)[:200]
    lst.unlink(missing_ok=True)
    raise ResolveError(
        "分片下载完成但合并失败",
        f"已下完 {len(part_files)} 段，ffmpeg 拼接失败：{last_err}\n"
        "可改用较低清晰度重试，或检查本机 ffmpeg 是否可用。",
    )


def _sohu_pick_format_id(formats: list[dict], quality_key: str) -> str:
    """按用户选的档位在搜狐的分片档里挑 format_id。

    🔴 不能直接套 `_format_selector`：搜狐的 format 没有 vcodec 标记，且**宽银幕
       源短边远小于标称档位**（640x268 在站内叫 360P、1280x534 叫 720P）。按
       `height 350~370` 去匹配一个都不中 ⇒ "Requested format is not available"
       （2026-10-25 端到端实测报的就是这个）。
    这里改成「短边最接近请求档位」，同距离优先 H.264 档。
    """
    qk = (quality_key or "").strip().lower()
    if qk in ("best", "highest"):
        want = 9999
    else:
        try:
            want = int(qk)
        except (TypeError, ValueError):
            return ""   # audio/m4a/webm 等交给原 selector
        if want <= 0:
            return ""
    cands: list[tuple[int, int, str]] = []
    for f in formats or []:
        h = _short_side(f) or f.get("height") or 0
        if not h:
            continue
        fid = str(f.get("format_id") or "")
        if not fid:
            continue
        # 0 = H.264 优先（兼容性最好），1 = 其他编码
        pref = 0 if ("h264" in fid.lower() or fid in ("ori", "super", "src", "nor")) else 1
        cands.append((abs(int(h) - want), pref, fid))
    if not cands:
        return ""
    return min(cands)[2]


def _sohu_download_parts(task: DownloadTask, info: dict[str, Any], base_opts: dict,
                         reporter, quality_key: str = "") -> Path:
    """搜狐多分片：逐片下载 → concat 合并 → 返回成品路径。

    进度天然正确：_ProgressReporter 以 filename 为 key 累加（每片一个文件），
    总进度 = Σ已下 / Σ总量，不需要额外换算。
    """
    parts: list[dict[str, dict]] = info.get("_vdl_parts") or []
    if not parts:
        raise ResolveError("该视频无法解析出分片", "请更换链接或稍后重试")
    workdir = Path(task.workdir or ".")
    pdir = workdir / _SOHU_PART_DIRNAME
    shutil.rmtree(pdir, ignore_errors=True)
    pdir.mkdir(parents=True, exist_ok=True)
    total = len(parts)
    # 搜狐的档位是 format_id（nor/high/super…），站内标称高度与实际短边差得远，
    # 完成后不要报「清晰度不符」（见 _ProgressReporter._actual_note）
    try:
        setattr(task, "_quality_mapped", True)
    except Exception:
        pass
    task.add_step("下载音视频", "running", f"该视频分 {total} 段，逐段下载后自动合并为单个文件…")
    task.log(f"搜狐多分片：共 {total} 段，开始逐段下载（每段约 5 分钟）")

    files: list[Path] = []
    fixed_fmt = _sohu_pick_format_id(info.get("formats") or [], quality_key)
    if fixed_fmt:
        task.log(f"清晰度档位映射到搜狐格式：{fixed_fmt}")
    for i, raw in enumerate(parts):
        if task.cancel_requested:
            raise DownloadCanceled()
        # 🔴 关键：原始 entry 顶层带了 `url`/`format_id`（= 站内最高档 super），
        # yt-dlp 的 process_info 见到顶层 url 就**直接拿它下载、完全忽略 format
        # selector**（2026-10-25 实测：请求 360P 实得 534P/166MB）。而把 url 删掉
        # 只留 formats 又会退化成 "No video formats found"。
        # 正确做法：把选定档的字段**平铺到 entry 顶层**，并去掉 formats，
        # 让 yt-dlp 走「单格式」路径，选中的档就是唯一档，无从选错。
        chosen = None
        for cand in raw.get("formats") or []:
            if str(cand.get("format_id") or "") == fixed_fmt:
                chosen = cand
                break
        if chosen is None:
            cands = [f for f in (raw.get("formats") or []) if f.get("url")]
            cands.sort(key=lambda f: _short_side(f) or f.get("height") or 0, reverse=True)
            chosen = cands[0] if cands else None
        if chosen is None:
            raise ResolveError(
                f"第 {i + 1}/{total} 段没有可用清晰度",
                "请更换链接或稍后重试。",
            )
        entry = {k: v for k, v in raw.items()
                 if k not in ("formats", "requested_formats", "requested_downloads")}
        entry.update(chosen)
        entry["id"] = f"{info.get('id') or 'sohu'}_part{i + 1}"
        entry["title"] = info.get("title") or entry.get("title") or "video"
        for _k in ("playlist_index", "playlist_count", "playlist_id", "playlist_title",
                   "playlist_autonumber", "n_entries", "__last_playlist_index"):
            entry.pop(_k, None)
        _chosen_id = str(chosen.get("format_id") or "")
        opts_i = dict(base_opts)
        opts_i.pop("postprocessors", None)   # 逐片不做 remux/抽音轨，合并后再处理
        opts_i.pop("paths", None)
        opts_i["outtmpl"] = {"default": str(pdir / f"part{i + 1:03d}.%(ext)s")}
        opts_i["overwrites"] = True
        opts_i["noplaylist"] = True
        # entry 只剩一档，selector 必须精确指向它（否则档位 selector 会对不上
        # 搜狐那套 640x268≈360P 的非标高度）
        if _chosen_id:
            opts_i["format"] = _chosen_id
        task.log(f"第 {i + 1}/{total} 段开始下载…")
        with YoutubeDL(opts_i) as ydl_i:
            got_info = ydl_i.process_info(entry) or {}
        if not fixed_fmt:
            if got_info.get("requested_formats"):
                fixed_fmt = "+".join(str(f.get("format_id")) for f in got_info["requested_formats"])
            else:
                fixed_fmt = str(got_info.get("format_id") or "")
            if fixed_fmt:
                task.log(f"全部分段锁定清晰度：{fixed_fmt}")
        filepath = None
        for d in (got_info.get("requested_downloads") or []):
            if d.get("filepath") and Path(d["filepath"]).exists():
                filepath = Path(d["filepath"])
        if filepath is None:
            cand = [p for p in sorted(pdir.glob(f"part{i + 1:03d}.*"))
                    if p.suffix not in (".part", ".ytdl", ".tmp")]
            filepath = cand[0] if cand else None
        if filepath is None:
            raise ResolveError(
                f"第 {i + 1}/{total} 段下载失败",
                "请重试；若反复失败请更换清晰度（低档位分片更小更稳）。",
            )
        files.append(filepath)
        task.log(f"第 {i + 1}/{total} 段完成（{filepath.stat().st_size / 1048576:.1f} MB）")

    title = _safe_media_name(info.get("title") or info.get("id") or "sohu")
    out = workdir / f"{title}.mp4"
    task.add_step("合并与后处理", "running", f"正在合并 {total} 段…")
    task.log(f"开始合并 {total} 段…")
    out = _sohu_concat(files, out, task)
    shutil.rmtree(pdir, ignore_errors=True)
    task.log(f"合并完成：{out.name}（{out.stat().st_size / 1048576:.1f} MB）")
    info["requested_downloads"] = [{"filepath": str(out)}]
    info["_vdl_parts"] = []
    return out

class _NeedYtDlp(Exception):
    """标记该平台应交由 probe() 末尾的 yt-dlp 通用流程解析（已收录且 yt-dlp 支持）。"""

def probe(url: str, cookie: str = "", proxy: str = "") -> dict[str, Any]:
    """probe 包装：本线程禁用 yt-dlp 连接复用（代理客户端复用缺陷，见
    _patch_requests_handler_no_keepalive），再转 _probe_impl。"""
    _no_keepalive_local.enabled = True
    try:
        return _probe_impl(url, cookie=cookie, proxy=proxy)
    finally:
        _no_keepalive_local.enabled = False


def _probe_impl(url: str, cookie: str = "", proxy: str = "") -> dict[str, Any]:
    """只解析不下载，返回 yt-dlp 的原始 info dict。"""
    effective_proxy = proxy or _resolve_proxy(_host_of(url) or "")
    # 归一化前先做非视频页（空间/动态/番剧）可读拦截，避免 yt-dlp 提取失败后
    # 只给用户一句笼统的"视频解析失败"。与归一化入参解耦：此处只做链接形态判断。
    _norm_for_check = _normalize_share_url(url, proxy=effective_proxy)
    if "space.bilibili.com" in _norm_for_check or "bilibili.com/opus/" in _norm_for_check:
        raise ResolveError(
            "这是 B站 个人空间 / 动态页，不是单个视频",
            "请打开该视频，复制浏览器地址栏中以 bilibili.com/video/BVxxxx 开头的播放页链接，再粘贴解析。",
        )
    # 番剧拦截只对 B站 生效：AcFun 的番剧页同样带 /bangumi/（acfun.cn/bangumi/aa…），
    # 旧实现只判路径不判站，会把可正常下载的 AcFun 番剧误报成「B站 番剧 DRM」。
    if "bangumi" in _norm_for_check and "bilibili" in _norm_for_check:
        raise ResolveRestricted(
            "该链接是 B站 番剧 / 影视（会员 DRM 内容）",
            "番剧、电影、纪录片等受版权 DRM 保护，标准下载方式无法解析。请更换为公开可播放的普通视频链接。",
        )
    # 喜马拉雅专辑链接：走专辑功能（/api/playlist + worker），单集才走 yt-dlp。
    # 直接喂 probe 会被 yt-dlp XimalayaIE 报 KeyError('data')（专辑页非单集结构）。
    _probe_host = _host_of(url)
    if _probe_host in ("ximalaya.com",) and "/album/" in (url or "").split("?", 1)[0]:
        raise ResolveError(
            "这是喜马拉雅专辑链接",
            "专辑请使用「歌单 / 专辑下载」功能：粘贴链接后选择专辑模式即可整批下载；"
            "下载单个音频请打开具体集数的播放页链接。",
        )
    url = _norm_for_check
    # 优酷链接归一化：当前主流为 www.youku.com，而 yt-dlp 内置 YoukuIE 仅匹配
    # v/play/player.youku.com，否则会落 generic 提取 → Unsupported URL（pending_extractor）。
    # 归一化到 v.youku.com 让 YoukuIE 接管（共享池 Cookie 已注入 http_headers）。
    if "www.youku.com" in url:
        url = url.replace("www.youku.com", "v.youku.com", 1)
    # 优酷：yt-dlp 内置 YoukuIE 不生成 ckey 播放签名 → -3007。改走专用 UPS 通道
    # （带共享池 Cookie + ckey 直接拿 m3u8）。ckey 仅取公共池（用户 Copy as cURL 贡献）。
    if (_host_of(url).endswith("youku.com")):
        _yk_cookie = cookie
        _yk_ckey = ""
        if not _yk_cookie or True:  # 你库 ckey 一律走公共池（无本机引擎）
            # _pooled_*：本机池 miss 时回读云端池（Cookie 与 ckey 同一次回读）
            _yk_cookie = _yk_cookie or _pooled_cookie("youku.com")
            _yk_ckey = _pooled_ckey("youku.com")
        try:
            return _youku_info(url, _yk_cookie, _yk_ckey, proxy=effective_proxy)
        except ResolveError:
            raise
        except Exception as _e:
            raise ResolveError(
                "优酷解析失败", f"UPS 通道异常：{type(_e).__name__} {_e}", category="parse_failed"
            ) from None
    # 用户手动粘贴的 Cookie 持久化缓存：本次解析成功后写盘，
    # 后续同站点解析/下载自动复用，免去每次重粘。
    host = _host_of(url)
    if cookie and host:
        _cache_user_cookie(host, cookie)
    # 直播模块已下线（2026-08-22 用户要求）：斗鱼 / 映客 / 网易CC 统一拦截
    if host and any(_lb in host for _lb in ("douyu.com", "inke.cn", "cc.163.com")):
        raise ResolveError(
            "该直播平台已暂时下线",
            "直播模块（斗鱼 / 映客 / 网易CC）已于 2026-08-22 下线，恢复时间待定。"
            "可关注后续更新，或使用其他平台。",
        )
    # 西瓜视频：原生 IxiguaIE + 浏览器 Cookie（详见 _ixigua_info；不再改写为抖音链接）
    if "ixigua.com" in (host or ""):
        return _ixigua_info(url, cookie=cookie, proxy=proxy)
    # 腾讯视频合集页（无 vid 参数）：yt-dlp 只会返回空 playlist，提前给可操作提示。
    # 2026-10-25 实测：v.qq.com/x/cover/<cid>.html → entries 恒为空。
    if "qq.com" in (host or "") and re.search(r"/x/cover/[a-z0-9]+\.html", url or "") \
            and "vid=" not in (url or ""):
        raise ResolveError(
            "这是腾讯视频的合集 / 剧集页",
            "合集页只列出剧集清单，解析不到具体视频。\n"
            "请在浏览器打开这部剧，点进任意一集，复制那一集播放页的链接"
            "（形如 v.qq.com/x/cover/…/<vid>.html 或 v.qq.com/x/page/<vid>.html）再解析。\n\n"
            "另：腾讯视频的会员专享内容受版权保护，无法下载；免费集可以正常解析。",
            category="unsupported_url_form",
        )
    # 抖音/快手/微博/爱奇艺：yt-dlp 提取器失效或分享页 JS-only，走 VPS Playwright 真实浏览器解析
    if _is_douyin_host(host):
        return _douyin_info(url, cookie=cookie)
    # DAZN：认证墙 + Widevine DRM + 地区封锁（2026-08-22 深度评估），明确提示
    if "dazn.com" in (host or ""):
        raise ResolveError(
            "DAZN 暂不支持解析",
            "DAZN 为付费体育流媒体：①内容受 Widevine DRM 保护（合规不破解）；"
            "②需 DAZN 支持地区的住宅网络与账号（中国 IP / 数据中心 IP 均被 403 地区封锁）。"
            "免费内容同样走认证+DRM 管线。建议使用已支持的平台或 DAZN 官方离线功能。",
        )
    # Rumble：Cloudflare 反爬 403 拦 yt-dlp，走专用浏览器头接口（embedJS/u3）
    if "rumble.com" in (host or ""):
        return _rumble_info(url, cookie)
    # Tubi：免费 AVOD 无 DRM，yt-dlp 提取器在数据中心 IP 偶发失败，专用页面解析
    if "tubitv.com" in (host or ""):
        return _tubi_info(url, cookie, proxy)
    if _is_kuaishou_host(host):
        return _kuaishou_info(url)
    # 斗鱼：yt-dlp DouyuTVIE 旧正则失效（room_id 格式已改）+ DouyuShowIE 依赖
    # 过时 PhantomJS，走 VPS Playwright 真实浏览器监听流请求
    if "douyu.com" in (host or ""):
        return _douyu_info(url)
    # 央视频：播放地址需 playvinfo JSONP 签名（cKey），走 VPS Playwright 解析
    if "yangshipin.cn" in (host or ""):
        return _yangshipin_info(url)
    # 1905 电影网：详情页反爬（数据中心 IP 403），走 VPS Playwright 解析
    if "1905.com" in (host or ""):
        return _m1905_info(url)
    # 风行网：播放地址需 pm.funshion.com 接口签名，走 VPS Playwright 解析
    if "fun.tv" in (host or ""):
        return _funshion_info(url)
    # 百视TV：web 端播放地址需 wasm 签名（makepreviewquery），走 VPS Playwright 解析
    if "bestv.com.cn" in (host or ""):
        return _bestv_info(url)
    # 金牌影院（nmwchxx.com 及换过的同族域名）：自签 API（SHA1(MD5)）拿 m3u8
    if _is_nmw_host(host):
        return _nmw_info(url, proxy=effective_proxy)
    # 红果短剧：字节系 CDN 流需真实浏览器点开播放器捕获，走 VPS Playwright 解析
    if ("hongguoduanju.com" in (host or "")) or ("novelquickapp.com" in (host or "")):
        return _hongguo_info(url)
    # 映客直播/回放：公开接口拿昵称+状态，流地址规律化构造，走 VPS worker
    if "inke.cn" in (host or ""):
        return _inke_info(url)
    # 网易CC直播：vapi 公开接口拿签名 FLV 直链，走 VPS worker
    if "cc.163.com" in (host or ""):
        return _cc_info(url)
    if _is_weibo_host(host):
        return _weibo_info(url)
    if _is_iqiyi_host(host):
        info = _iqiyi_info(url, cookie=cookie)
        if info is not None:
            return info
        # worker 未配置（本地桌面）且非分享页 → 回退 yt-dlp 通用流程
    # —— B 类平台专用提取器分发（平台已收录但 yt-dlp 无原生提取器）——
    # 网易云专辑 / 歌单 / 歌手 / 排行榜页：不是单曲，_ne_info 只认 MV/单曲，
    # 喂进去会得到 "JSONDecodeError: Expecting value" 这种没法看的报错。
    # 2026-10-25 实测（music.163.com/#/album?id=…）。整批下载走歌单功能。
    if "music.163.com" in (host or "") and re.search(
        r"/(?:#/)?(?:album|playlist|artist|toplist|discover)", url or ""
    ):
        raise ResolveError(
            "这是网易云专辑 / 歌单链接，不是单曲",
            "请打开具体歌曲的播放页（music.163.com/song?id=… 或 /#/song?id=…）再解析；"
            "要整张专辑/歌单批量下载，请用「歌单 / 专辑下载」功能。",
        )
    # 网易云单曲 / MV：yt-dlp 原生提取器优先（单曲音轨、MV 多清晰度均可），
    # weapi 仅作 MV 兜底 —— 见 _ne_probe 注释
    if "music.163.com" in (host or ""):
        return _ne_probe(url, cookie=cookie, proxy=proxy)
    # 土豆：已并入优酷，尝试重定向或明确停运提示
    if "tudou.com" in (host or ""):
        return _tudou_info(url, proxy=proxy)
    # 微视 / YY 直播：腾讯/YY 签名流，best-effort + 清晰提示
    if "weishi.qq.com" in (host or ""):
        return _weishi_info(url, proxy=proxy)
    # 微信视频号：weixin.qq.com/sph/* 短链 或 channels.weixin.qq.com 直链
    if ("weixin.qq.com" in (host or "")) or ("channels.weixin.qq.com" in (host or "")):
        return _finder_info(url, cookie=cookie, proxy=proxy)
    if "yy.com" in (host or ""):
        return _yy_info(url, proxy=proxy)
    # Disney+ Hotstar / KinoPoisk：yt-dlp 已有提取器，显式放行走末尾通用解析，
    # 不再误报"暂未实现该站解析器"（地区/登录限制由 yt-dlp 返回明确错误）。
    if "hotstar.com" in (host or "") or "kinopoisk" in (host or ""):
        pass  # 落入下方 yt-dlp 通用流程
    # YouTube：自动降级（方法一免 Cookie + PO Token → 方法二 Cookie 源自动切换）
    if _is_youtube_host(host):
        return _resolve_youtube(url, user_cookie=cookie, proxy=proxy)
    # YouTube 诊断日志（临时，定位代理/Cookie 问题后可移除）
    _debug_log = os.path.join(os.environ.get("TMPDIR", "/tmp"), "vdl_probe_debug.log")
    try:
        with open(_debug_log, "a") as _f:
            _f.write(f"[{__import__('datetime').datetime.now().isoformat()}] URL={url[:80]} host={host}\n")
            _f.write(f"  proxy={proxy or '(auto)'} cookie={'yes' if cookie else 'no'}\n")
            _effective = proxy or _resolve_proxy(host)
            _f.write(f"  effective_proxy={_effective or '(none)'}\n")
            _sys_p = _macos_system_proxy()
            _f.write(f"  macos_system_proxy={_sys_p or '(none)'}\n")
    except Exception:
        pass
    direct = _looks_like_direct_file(url)
    if direct:
        # 本身就是完整媒体文件，跳过 yt-dlp，直接交给前端从源站下载（不走服务器）
        filename = Path(urlparse(url).path).name or "video.mp4"
        return {
            "direct": True,
            "url": url,
            "title": filename,
            "ext": (Path(filename).suffix or ".mp4").lstrip("."),
            "webpage_url": url,
        }
    # info 可能因 yt-dlp 抛异常（网络重试失败等）从未被赋值，必须初始化，
    # 否则下方 `if not info:` 会抛 UnboundLocalError（搜狐等平台实测触发）
    info: dict[str, Any] | None = None
    # 记录最后一次异常信息，用于 info 为空时透传真实原因
    _last_err: str | None = None
    # 收集 yt-dlp logger 的 WARNING/ERROR 输出，供 `if not info:` 时透传真实业务原因。
    # handler 的添加/移除严格包在内层 try/finally（紧贴 extract_info 调用），
    # 确保任何路径（包括 raise）都清理，不污染后续请求的 logger。
    import logging as _logging

    class _YdlLogCapture(_logging.Handler):
        def emit(self, record):  # noqa: ANN401
            if record.levelno >= _logging.WARNING:
                _ydlp_logs.append(self.format(record))

    _ydlp_logs: list[str] = []

    try:
        opts = _base_options(PROBE_RETRIES, _host_of(url), cookie=cookie, proxy=proxy)
        # B站 经国内代理回源时，yt-dlp 原生 urllib 读取页面偶发 IncompleteRead；
        # 用 requests 预下载视频页 HTML 并注入 extractor，提高连接稳定性。
        _h = _host_of(url)
        if _h and ("bilibili.com" in _h or "b23.tv" in _h):
            # patch 必须带最终生效的 Cookie（含公共池自动注入），而不是用户原始输入
            _patch_bilibili_webpage_download(
                proxy=effective_proxy,
                cookie=(opts.get("http_headers") or {}).get("Cookie", cookie),
                ua=(opts.get("http_headers") or {}).get("User-Agent"),
            )
        # 解析阶段只拿 info dict，不做格式选择（避免 YouTube 等站因格式不匹配
        # 直接抛 "Requested format is not available"）。下载阶段再由 _format_selector 选格式。
        opts["format"] = None
        # ignoreerrors：仅 YouTube 通过代理时格式列表可能不完整，需要跳过格式错误
        # 让 extract_info 尽量返回能拿到的信息（标题/时长/缩略图等）。
        # ⚠️ 绝不能对所有站点生效！国内站（快手/抖音等）提取失败时，
        # ignoreerrors 会吞掉 ExtractorError/DownloadError 导致 extract_info 返回 None，
        # 真正的错误原因（页面结构变更/需要登录等）被完全丢失。
        # ⚠️ 也不对 YouTube 生效！"only_download" 在 2026.07 yt-dlp 下对
        # "This video is unavailable"/"Sign in to confirm you're not a bot" 这类业务
        # 错误既不抛 DownloadError 也不 logger.error，导致前端「未获取到视频信息」
        # 误导。让 yt-dlp 真实抛 DownloadError，由下方 except DownloadError 分支
        # 统一捕获 + extract_flat / web_safari 降级。
        with _YoutubeDL(opts) as ydl:
            # 诊断：记录 yt-dlp 运行时真实配置（proxy/handlers/每 handler proxies）
            try:
                _rd = ydl._request_director
                with open(_debug_log, "a") as _f:
                    _f.write(
                        f"  ytdlp_runtime proxy={ydl.params.get('proxy')!r} "
                        f"handlers={list(_rd.handlers.keys())} "
                        f"hproxies={{k: str(getattr(h,'proxies','N/A')) for k, h in _rd.handlers.items()}}\n"
                    )
            except Exception as _dge:
                try:
                    with open(_debug_log, "a") as _f:
                        _f.write(f"  ytdlp_runtime diag_err={str(_dge)[:120]}\n")
                except Exception:
                    pass
            # yt-dlp logger 捕获（业务错误透传兜底）。内层 try/finally 确保任何路径都清理
            import logging as _logging
            _capture = _YdlLogCapture()
            _capture.setFormatter(_logging.Formatter("%(levelname)s %(name)s: %(message)s"))
            _ydlp_logger = _logging.getLogger("yt_dlp")
            _old_level = _ydlp_logger.level
            _ydlp_logger.addHandler(_capture)
            _ydlp_logger.setLevel(_logging.WARNING)
            try:
                info = ydl.extract_info(url, download=False)
            finally:
                _ydlp_logger.removeHandler(_capture)
                _ydlp_logger.setLevel(_old_level)
        # 诊断：记录 extract_info 返回值
        try:
            _info_keys = list(info.keys()) if info else ["(None)"]
            _info_title = (info or {}).get("title", "(no title)")
            _fmt_count = len((info or {}).get("formats") or [])
            with open(_debug_log, "a") as _f:
                _f.write(f"  extract_info OK: title={str(_info_title)[:60]} formats={_fmt_count} keys={_info_keys[:15]}\n")
        except Exception:
            pass
    except (UnsupportedError, GeoRestrictedError) as exc:
        raise _friendly_error(exc, _build_diag_context(url, cookie=cookie, proxy=proxy, options=opts)) from exc
    except (DownloadError, ExtractorError) as exc:
        # yt-dlp 2026+ 把 \"Unsupported URL\" 包成 ExtractorError 抛出（而非直接的
        # UnsupportedError 实例），必须在此短路到 unsupported_platform 友好提示，
        # 否则会被 2248 `if not info:` 当作未知失败处理。
        if "unsupported url" in str(exc).lower():
            raise _friendly_error(exc, _build_diag_context(url, cookie=cookie, proxy=proxy, options=opts)) from exc
        _last_err = f"{type(exc).__name__}: {str(exc)[:200]}"
        # 网络类错误（隧道重连窗口/链路抖动）：国内站经反向隧道回源时，本机隧道
        # 端（住宅 IP→Cloudflare）偶发断线，client 5s 后重连。撞上窗口会报
        # "Remote end closed / 1010 / connection" 类错误，等 3s 重试一次可显著
        # 提高成功率（重试用的是全新 _base_options + _YoutubeDL，无状态残留）。
        _exc_low = str(exc).lower()
        # 2026-10-26 pomo.mom 实测：该站约 1/3 概率在 TLS 传输中途随机掐连接，
        # 报 "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of
        # protocol"。补 "eof occurred"/"unexpected_eof" 两个特征词进网络类判定。
        _net_like = any(
            w in _exc_low
            for w in ("remote end closed", "connection", "timed out", "1010",
                      "transport", "incompleteread", "reset", "closed",
                      "eof occurred", "unexpected_eof")
        )
        # 2026-10-26：重试不再限国内站——海外站（pomo.mom 实锤）经代理同样有
        # 随机 SSL EOF；单次重试对两类站都有效，失败仍走原错误路径，无副作用。
        if _net_like and _h:
            logger.info("[probe] 网络类错误(%s)，3s 后重试一次", _last_err[:100])
            try:
                import time as _t
                _t.sleep(3)
                _opts2 = _base_options(PROBE_RETRIES, _h, cookie=cookie, proxy=proxy)
                _opts2["format"] = None
                with _YoutubeDL(_opts2) as _ydl2:
                    info = _ydl2.extract_info(url, download=False)
                _last_err = None
                logger.info("[probe] 隧道重试成功")
            except Exception as _re2:
                logger.warning("[probe] 隧道重试仍失败: %s", str(_re2)[:150])
        # B站 走 API 兜底：网络错误（IncompleteRead/连接重置）OR 页面风控（412/403）。
        # 2026-08 B站 风控升级：对 urllib3/requests 的非浏览器 TLS 栈访问视频页返回
        # 412 验证页（curl 不受影响），yt-dlp 抓页面必 412 → 提取器报 403。
        # API（api.bilibili.com）不过页面风控，仍 200，因此直接走 API 构造 info。
        _exc_low = str(exc).lower()
        if _h and ("bilibili.com" in _h or "b23.tv" in _h) and (
            "incompleteread" in _exc_low
            or "error reading response" in _exc_low
            or "connection" in _exc_low
            or "transport" in _exc_low
            or "412" in _exc_low
            or "403" in _exc_low
            or "forbidden" in _exc_low
            or "precondition" in _exc_low
        ):
            logger.info("[probe] yt-dlp Bilibili failed (%s), trying API fallback", _last_err)
            try:
                # 用最终生效的 Cookie（含公共池自动注入），而不是用户原始输入
                _final_cookie = (opts.get("http_headers") or {}).get("Cookie") or cookie
                info = _bilibili_api_extract(url, proxy=effective_proxy, cookie=_final_cookie)
                if info:
                    logger.info("[probe] Bilibili API fallback succeeded")
                    _last_err = None
                else:
                    logger.warning("[probe] Bilibili API fallback returned no info")
            except Exception as fb_err:
                logger.warning("[probe] Bilibili API fallback failed: %s", str(fb_err)[:200])

        if _last_err:
            # 平台专属友好映射短路（2026-09-25 小红书）：必须置于下方 format
            # 降级判断**之前**——"No video formats found" 含 "format" 会被误判成
            # 格式问题走 extract_flat 降级，抛通用「视频解析失败」绕过
            # _friendly_error 的平台分支。仅对明确命中的平台 category 短路，
            # 其余平台错误保持原有行为不变。
            _fe = _friendly_error(exc, _build_diag_context(url, cookie=cookie, proxy=proxy, options=opts))
            if getattr(_fe, "category", "") in (
                "xhs_short_link", "xhs_login_required", "cookie_db_locked"
            ):
                raise _fe from exc
            # YouTube 等站格式选择失败时，降级用 extract_flat 重试（只拿元数据，不含格式列表）
            if "format" in str(exc).lower() or "not available" in str(exc).lower():
                try:
                    opts2 = _base_options(PROBE_RETRIES, _host_of(url), cookie=cookie, proxy=proxy)
                    opts2["extract_flat"] = "in"
                    # 2026-09-26：不再强制 web_safari（SABR 后已失效），走 yt-dlp 默认客户端链
                    with _YoutubeDL(opts2) as ydl2:
                        info = ydl2.extract_info(url, download=False)
                        _last_err = None  # 降级成功
                except Exception as fb_err:
                    _last_err = f"{_last_err}; 降级: {type(fb_err).__name__}: {str(fb_err)[:150]}"
                    raise ResolveError(
                        "视频解析失败",
                        f"建议：①检查代理是否通畅；②在「高级选项」粘贴 Cookie；"
                        f"③更换代理节点。\n详情：{_last_err}"
                    ) from exc
            else:
                # 临时诊断：B站 403/1010 时输出实际生效的 proxy/host/Referer
                _probe_host = _host_of(url) or ""
                _diag_note = (
                    f"\n[diag] host={_probe_host!r} is_china={is_china_host(_probe_host)} "
                    f"opts_proxy={opts.get('proxy')!r} effective_proxy={effective_proxy!r} "
                    f"referer={(opts.get('http_headers') or {}).get('Referer')!r}"
                )
                raise ResolveError(
                    "视频解析失败",
                    _clean_message(str(exc))[:200] + _diag_note,
                ) from exc
    except OSError as exc:  # 网络/DNS 层面的错误
        _last_err = f"{type(exc).__name__}: {_clean_message(str(exc))[:200]}"
        raise ResolveError("网络请求失败", _clean_message(str(exc))) from exc
    except Exception as exc:
        # 兜底：捕获任何未预期异常，保留完整错误用于诊断
        _last_err = f"{type(exc).__name__}: {str(exc)[:300]}"

    # 注意：yt-dlp logger handler 的清理已在 extract_info 调用的内层 finally 完成
    # （确保任何 raise 路径都清理，不污染后续请求）

    if not info:
        detail = "请稍后重试或更换链接"
        # 透传诊断信息：如果 extract_info 静默返回空（未抛异常），补充上下文
        if _last_err:
            diag = _last_err
        elif _ydlp_logs:
            # 优先取 ERROR 行（业务原因如"video is unavailable"），其次 WARNING
            errs = [l for l in _ydlp_logs if l.startswith("ERROR")]
            diag = "\n".join(errs[:5]) if errs else "\n".join(_ydlp_logs[:5])
        else:
            diag = (
                "extract_info 返回空结果（无异常）。"
                "常见原因：①代理 MITM 导致 SSL 握手失败但被 ignoreerrors 吞掉；"
                "②站点返回空页面；③需要登录 Cookie。建议在「高级选项」粘贴 Cookie 后重试。"
            )
        detail += f"\n\n诊断信息：{diag}"
        raise ResolveError("未获取到视频信息", detail)
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise ResolveError("该链接是一个空的合集", "请粘贴单个视频的播放地址")
        info = entries[0]
    # 多分片视频（搜狐 SohuIE 返回 _type=multi_video，一集切成 N 个 mp4 分片）：
    # 搜狐必须聚合（否则只显示/只下第一片 9 秒片头，见 _sohu_aggregate 注释）；
    # 其他平台的多分片按原逻辑取第一片继续。
    if info.get("_type") == "multi_video" and (_host_of(url) or "").endswith("sohu.com"):
        info = _sohu_aggregate(info)
    elif info.get("_type") == "multi_video":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise ResolveError("该视频无法解析出分片", "请更换链接或稍后重试")
        info = entries[0]
    if info.get("is_live"):
        raise ResolveError("暂不支持下载正在直播的内容", "请等直播结束生成回放后再试")
    if _is_restricted_placeholder(info):
        raise ResolveRestricted(
            "该视频疑似会员 / 付费受限，本工具暂不支持",
            "腾讯等平台的会员专享、付费或地区限制内容需官方客户端或登录态才能获取，"
            "标准下载方式无法解析。请更换为公开可播放的视频链接",
        )
    # 优酷：yt-dlp 的 YoukuIE 只返回单集标题，从网页 <title> 补全整部剧名
    if info and _host_of(url) in ("youku.com", "tudou.com"):
        info = _enrich_youku_series(info, effective_proxy)
    # 多分片的逐片直链只给下载阶段用（体积大、前端无展示价值），别透传到 UI
    if info.get("_vdl_parts"):
        info.pop("_vdl_parts", None)
    return info

# --------------------------------------------------------------------------- #
# 清晰度选项
# --------------------------------------------------------------------------- #

def _tier_tolerance(tier: int) -> int:
    """清晰度档位的高度容差（±3%，至少 4 像素）。

    🔴 非标准高度真实存在：1912x1072（1.85:1 电影裁掉黑边）、1082、1062……
       这些在 yt-dlp 里 height 就是 1072，**既不等于 1080 也不等于 720**。
    若按等值匹配，1080P 这一档根本匹配不到 ⇒ 卡片列不出来（用户选不到），
    下载链也匹配不到。app-dev 从 2026-09 起就在体积估算里用 ±3% 容差
    （见 `_video_size_at` 的「容差匹配」注释），本轮把容差一并提到
    「分档」与「下载链」两处，否则非标准高度的视频会整档消失。
    """
    return max(4, int(tier * 0.03))


def _short_side(f: dict) -> int | None:
    """视频轨的「短边高度」——用户认知里的那个清晰度数字。

    横屏 1920x1080 → 1080（height 就是短边）；竖屏 1080x1920 → 1080（width 才是短边）。

    🔴 短视频（抖音/快手/TikTok/Shorts）大量竖屏，只按 height 分档会两头错：
      ① 卡片：竖屏 1080P 的 height 是 1920，「1080P」这一档匹配不到，
         卡片只能列出 720P 以下，用户根本选不到 1080P；
      ② 下载：`bv*[height<=1080]` 在竖屏上会命中 height=720 的那条
         （720 是 ≤1080 里最大的）⇒ **选 1080P 实得 720P**，跟横屏那个
         「选 1080P 实得 1440P」是同一类病。
    故分档与选择一律按短边，横竖屏共用一套判据。
    """
    h = f.get("height")
    w = f.get("width")
    if not h:
        return None
    if not w:
        return int(h)
    return int(min(int(w), int(h)))


def _info_short_side(info: dict) -> int | None:
    """从 yt-dlp 的 info_dict 取**实际选中**的短边高度。

    DASH 双流下 info 描述的是合并结果，真正的视频轨在 requested_formats 里
    （音轨 height 为 None，不区分就会取到 None）；HLS 混合流则 info 自带
    width/height，直接按短边算即可。
    """
    for f in info.get("requested_formats") or []:
        if isinstance(f, dict) and f.get("vcodec") not in (None, "none"):
            _s = _short_side(f)
            if _s:
                return _s
    return _short_side(info)


# 视频编码偏好：H.264 第一（macOS WKWebView/Safari 不支持 AV1/VP9 解码，
# 选了会「下载成功但播不了」），VP9 次之，AV1 最后。卡片显示的名字也来自这里。
_VIDEO_CODEC_PREF: tuple[tuple[str, str], ...] = (
    ("avc1", "H.264"),
    ("vp9", "VP9"),
    ("av01", "AV1"),
)


def _codec_rank(f: dict) -> int:
    v = str(f.get("vcodec") or "")
    for i, (prefix, _label) in enumerate(_VIDEO_CODEC_PREF):
        if v.startswith(prefix):
            return i
    return len(_VIDEO_CODEC_PREF)


def _codec_label(f: dict) -> str:
    rank = _codec_rank(f)
    if rank < len(_VIDEO_CODEC_PREF):
        return _VIDEO_CODEC_PREF[rank][1]
    return ""


def _video_tracks_at(formats: list[dict], short_h: int) -> list[dict]:
    """该短边档下的视频轨，按「H.264 → VP9 → AV1 → 其他」+ 码率降序排列。

    排序即优先级：卡片取 [0] 作为「实际会下载的那一条」，下载链也按此顺序回落。
    """
    _tol = _tier_tolerance(short_h)
    tracks = []
    for f in formats:
        # 🔴 只排除**显式声明**无视频轨的（vcodec == "none"）。
        #    vcodec 缺失（None / 空）是「未知」，不是「没有视频」—— 自研提取器
        #    （金牌影院 nmwchxx 等）构造 formats 时根本不填 vcodec，老实现用
        #    `in (None, "none")` 把这类条目一并跳过 ⇒ 每一档的 _tracks 都是空 ⇒
        #    build_quality_options 里 `continue` ⇒ **一个分辨率档都列不出来**，
        #    卡片只剩「仅音频 MP3 / WebM / 仅音频 M4A」（2026-10-22 用户实测
        #    金牌影院《少帅》第1集，桌面端 + 网页版同时中招）。
        #    yt-dlp 自身也按这个口径判：None 视为未知仍算视频流，只有 "none" 才算纯音频。
        if str(f.get("vcodec") or "").strip().lower() == "none":
            continue
        _s = _short_side(f)
        if _s and abs(_s - short_h) <= _tol:
            tracks.append(f)
    # 编码优先（与下载链一致）→ 同编码下选**有真实体积**的那条（卡片报价才准）
    # → 再按码率降序。HLS 混合流(96) 与 DASH(137) 同为 avc1，选 137 才报得出
    # 398MB；若反过来选 96（无 filesize）卡片就只能报 0。
    tracks.sort(key=lambda f: (
        _codec_rank(f),
        0 if (f.get("filesize") or f.get("filesize_approx")) else 1,
        -(f.get("tbr") or 0),
    ))
    return tracks


def _audio_tracks(formats: list[dict]) -> list[dict]:
    """纯音轨，按「m4a 优先（合成 mp4 不必重编码）→ 码率降序」排列。"""
    tracks = [
        f for f in formats
        if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
    ]
    tracks.sort(
        key=lambda f: (0 if str(f.get("ext") or "").lower() == "m4a" else 1,
                       -(f.get("abr") or 0))
    )
    return tracks


def _size_of(f: dict | None, duration: float = 0.0) -> int:
    """体积：先取真实值；缺失时按 码率×时长 估算。

    🔴 HLS 混合流（m3u8_native，itag 91-96）yt-dlp **一律不给 filesize /
    filesize_approx** —— 直接读得到 0，卡片上 1080P 就只剩音轨那 14 MiB
    （2026-10-21 线上实测：卡片「1080P · MP4 · H.264 · 0 MiB + 音轨 14 MiB」）。
    按 码率(kbps) × 时长(s) / 8 估虽不精确，但报 0 是彻底没用。
    """
    if not f:
        return 0
    _s = int(f.get("filesize") or f.get("filesize_approx") or 0)
    if _s:
        return _s
    _tbr = float(f.get("tbr") or 0)
    if _tbr and duration:
        return int(_tbr * 1000 * float(duration) / 8)
    return 0


def _is_muxed(f: dict) -> bool:
    """混合流（视频轨自带音频，如 HLS itag 91-96）：落盘即成品，不能再叠音轨。"""
    return f.get("acodec") not in (None, "none")


def _plan_size(v: dict, a: dict | None, duration: float) -> int:
    """该档实际会落盘的体积：混合流不叠音轨，纯视频轨才要叠。"""
    if _is_muxed(v):
        return _size_of(v, duration)
    return _size_of(v, duration) + _size_of(a, duration)


def _best_audio_size(formats: list[dict]) -> int:
    sizes = [
        f.get("filesize") or f.get("filesize_approx") or 0
        for f in formats
        if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
    ]
    return max(sizes, default=0)

def _bucket_height(height: int) -> int:
    """把非标准高度归到最接近的标准档（1080/720/480/360…）。

    🔴 2026-09-22 用户实测踩坑：YouTube 少数视频给的是 **1072 / 714 / 476** 这类
    非整高度（标准档减若干行，常见于 1.79:1 等非 16:9 或音乐长视频）。若直接把它们
    当独立档位：①界面上会出现「1072P」这种怪标签；②更严重的是固定档位表的
    `1080 > 视频最高(1072)` 判断会把 **1080 整档当「超过最高画质」跳过** → 用户看到
    的最高档只有 720P（明明网页播放器里有 1080p）。
    归一到标准档后：标签规整、档位不丢、下载选择器 `height<=1080` 仍能命中 1072 的流。
    """
    for preset, _ in QUALITY_PRESETS:
        if abs(height - preset) <= max(4, int(preset * 0.03)):
            return preset
    return height


def _video_size_at(formats: list[dict], height: int) -> int:
    """该短边档「实际会下载」的视频轨体积 —— 取首选编码那条，不是该档最大的那条。

    🔴 老口径是 `max(该档所有视频轨体积)`。1080P 档里 H.264(137) 通常最大
    （本例 402MB），卡片就显示「约 398 MiB」，而下载链真正选中并落盘的
    可能是 VP9(248, 252MB) ⇒ 显示 398、实得 279，仍是「卡片说得不算数」。
    卡片必须显示**首选组合**的体积，否则用户看到的和拿到的永远对不上。
    """
    tracks = _video_tracks_at(formats, height)
    return _size_of(tracks[0]) if tracks else 0


def build_quality_options(info: dict[str, Any]) -> list[dict[str, Any]]:
    """把 yt-dlp 的 format 列表压缩成用户可选的清晰度档（2026-09-06 优化）。

    变更：去掉首位「最佳画质（自动）」聚合项；改为把解析到的**所有真实清晰度**
    逐档平铺（含抖音等 VPS 平台——不再走单档特例），每档可直接选择。
    只列 formats 中实际存在的高度（视频只有 720 就不再虚构 480/360 假档）。
    """
    formats = [f for f in (info.get("formats") or []) if isinstance(f, dict)]
    # 排除 mhtml 雪碧图/storyboard：其高度（180/90/45/27 等）会被当成真实清晰度
    # 平铺成「180P/90P/45P/27P」怪档（2026-10-01 SABR 实测踩坑）
    formats = [f for f in formats if not str(f.get("protocol") or "").startswith("mhtml")]
    # 一律按**短边**分档（竖屏 1080x1920 的 height 是 1920），
    # 非标准高度（1072/714/476）再由 _bucket_height 归到标准档（2026-09-22 实测）
    _shorts = sorted(
        (s for s in (_short_side(f) for f in formats) if s and 0 < s <= 4320),
        reverse=True,
    )
    heights = sorted({_bucket_height(s) for s in _shorts}, reverse=True)
    _audio_pick = _audio_tracks(formats)
    _a0 = _audio_pick[0] if _audio_pick else None
    _duration = float(info.get("duration") or 0)
    audio_size = _size_of(_a0, _duration)

    # 抖音（VPS 解析）只有单一视频档位 —— 本分支（app-dev）不需要为它开特例：
    # 卡片本身就「按真实短边平铺 + 附加格式」，只有一档就只显示一档，
    # 自然不会平铺出不存在的 480P/360P。相反，若在这里返回 web-dev 那个
    # 「最佳画质（自动）」单档特例，就与本分支「去掉 best 聚合项」的设计冲突。

    # 纯音频内容（喜马拉雅/网易云等无视频轨）：不展示 4K/1080P 等视频画质选项
    # 判据用 _shorts（有没有视频轨）而不是 heights（有没有匹配上标准档）：
    # 只有 900 这种非标准高度的视频不该被误判成纯音频，只是列不出档而已。
    if not _shorts:
        # ⚠️ 「没有 height」≠「没有视频轨」，`formats` 为空更不等于音频。两种真实情形：
        #   ① **直链文件**：probe() 在 _looks_like_direct_file() 短路，**根本没跑 yt-dlp**
        #      ⇒ formats 为 0 条（2026-10-20 实测 mov_bbb.mp4：`formats count: 0`）；
        #   ② yt-dlp 对裸媒体返回 `height=None, vcodec=None, acodec=None`（三项全未知）。
        # 老判据只看 height ⇒ 直链视频被误判成音频，清晰度面板只剩「最佳音质（自动）」，
        # 用户点「开始下载」拿到的却是视频，观感上就是坏的（直链改走服务器任务后才暴露）。
        # 判据改为**显式声明 + 扩展名兜底**：要么「有 format 且全部明确无视频轨、至少一条
        # 明确声明音频编码」，要么「formats 为空但扩展名是音频」—— 才算纯音频；其余一律
        # 按视频给单一「最佳画质」档（格式选择器 `ba/b` 对两者都能兜底）。
        _ext = str(info.get("ext") or "").lower()
        if not _ext:
            _m = re.search(r"\.([a-z0-9]{2,5})(?:\?|#|$|&)",
                           str(info.get("url") or info.get("webpage_url") or ""), re.IGNORECASE)
            _ext = (_m.group(1) if _m else "").lower()
        _audio_url = _ext in {"mp3", "m4a", "aac", "ogg", "oga", "wav", "flac", "opus", "wma"}
        _no_video = all(str(f.get("vcodec") or "none") == "none" for f in formats)
        _has_audio = any(str(f.get("acodec") or "none") != "none" for f in formats)
        _is_audio = (_no_video and _has_audio) if formats else _audio_url
        if not _is_audio:
            return [
                {"key": BEST_KEY, "label": "最佳画质（自动）",
                 "note": "视频+音频自动合并", "approx_size": 0}
            ]

        audio_ex = {str(f.get("ext") or "").lower() for f in formats}
        audio_opts: list[dict[str, Any]] = []
        if "mp3" in audio_ex:
            audio_opts.append({"key": AUDIO_KEY, "label": "仅音频 MP3", "note": "MP3 格式", "approx_size": audio_size})
        if "m4a" in audio_ex:
            audio_opts.append({"key": M4A_KEY, "label": "仅音频 M4A", "note": "M4A 格式", "approx_size": audio_size})
        return [
            {"key": BEST_KEY, "label": "最佳音质（自动）", "note": "自动选择最高音质", "approx_size": audio_size},
        ] + audio_opts

    preset = dict(QUALITY_PRESETS)
    options: list[dict[str, Any]] = []
    for height in heights:
        _tracks = _video_tracks_at(formats, height)
        if not _tracks:
            continue
        _v0 = _tracks[0]
        _codec = _codec_label(_v0)
        options.append(
            {
                "key": str(height),
                "label": preset.get(height, f"{height}P"),
                "note": f"MP4 · {_codec}" if _codec else "MP4",
                # 混合流（HLS 96）自带音轨，不能再叠 audio_size，否则虚高
                "approx_size": _plan_size(_v0, _a0, _duration),
                # 卡片承诺的高度与编码，下载阶段照此精确锁定（说什么就是什么）
                "exact_height": height,
                "codec": _codec,

            }
        )
    options.append(
        {"key": AUDIO_KEY, "label": "仅音频 MP3", "note": "提取音轨", "approx_size": audio_size}
    )
    options.append(
        {"key": WEBM_KEY, "label": "WebM 格式", "note": "体积小·适合网页嵌入", "approx_size": 0}
    )
    options.append(
        {"key": M4A_KEY, "label": "仅音频 M4A", "note": "无损音轨", "approx_size": audio_size}
    )
    return options


def _exact_height_chains(short_h: int) -> list[str]:
    """短边高度**精确锁定**的格式链 —— 只许命中这一档，不升档也不降档。

    🔴 每条链都把高度锁在**窄区间** `[>=lo][<=hi]`（lo/hi 由 ±3% 容差算出），
    而不是单边的 `<=N`。单边上界既能升也能降：无 1080 时它拿 720，403 时
    旧链直接拿 1440；双边区间则把「这一档」和「隔壁档」彻底隔开
    （1080 档 1047~1113，720 档 698~742，互不重叠）。
    留容差是为了容纳 1072 这类非标准高度（见 _tier_tolerance）。

    两条一组是为了横竖屏通吃：横屏 1920x1080 锁 height、竖屏 1080x1920 锁 width。
    组内顺序：先锁 H.264（macOS WKWebView/Safari 解不了 AV1/VP9，
    否则「下载成功但播不了」），再放开编码让 VP9/AV1 顶上。
    """
    _tol = _tier_tolerance(short_h)
    _lo, _hi = short_h - _tol, short_h + _tol
    _h = f"height>={_lo}][height<={_hi}"
    _w = f"width>={_lo}][width<={_hi}"
    return [
        f"bv[{_h}][vcodec^=avc1]+ba[ext=m4a]",
        f"bv[{_w}][vcodec^=avc1]+ba[ext=m4a]",
        f"bv[{_h}][vcodec^=avc1]+ba",
        f"bv[{_w}][vcodec^=avc1]+ba",
        f"bv[{_h}]+ba[ext=m4a]",
        f"bv[{_w}]+ba[ext=m4a]",
        f"bv[{_h}]+ba/bv[{_w}]+ba",
        f"b[{_h}]/b[{_w}]",
    ]



def _format_selector(quality_key: str) -> str:
    if quality_key == BEST_KEY:
        # 优先 H.264(avc1) —— macOS WKWebView / Safari 不支持 AV1 和 VP9 解码，
        # 选了会导致「下载成功但播不了」。[ext=mp4] 不够（YouTube AV1 也是 .mp4），
        # 必须用 [vcodec^=avc1] 锁编码。降级链：H.264≤1080p → H.264任意 → mp4容器 → 兜底
        return (
            "bestvideo[vcodec^=avc1][height<=1080]+bestaudio[ext=m4a]/"
            "bestvideo[vcodec^=avc1]+bestaudio[ext=m4a]/"
            "bestvideo[vcodec^=avc1][height<=1080]+bestaudio/"
            "bestvideo[vcodec^=avc1]+bestaudio/"
            "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/"
            "bestvideo[ext=mp4]+bestaudio/bestvideo+bestaudio/bv*+ba/b"
        )
    if quality_key in (AUDIO_KEY, M4A_KEY):
        return "ba/b"
    if quality_key == WEBM_KEY:
        return "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b"
    height = int(quality_key)
    # 精确锁定该档：卡片承诺 1080P 就只拿 1080P，拿不到就报错，不许换档。
    return "/".join(_exact_height_chains(height))

def _yt_prefer_hls(quality_key: str) -> bool:
    """无 PO Token 时，YouTube 该清晰度档是否优先走 HLS 混合流。

    判据（2026-10-19 实测）：googlevideo 对**无 PO Token 的 DASH 自适应流
    按连接限速**——单连接 ~40~80KB/s，16 连接聚合仅 ~600KB/s，且频繁硬
    阻断直至「网络连接超时」（用户 637MB 的 1080P 任务因此在 35% 失败，
    截图里瞬时 81KB/s、ETA 1:40）。同源 **HLS 混合流（itag 91-96，
    avc1+aac）不限速：单连接实测 1.5MB/s**。
    故 POT 不可用时 ≤1080p 直接要 HLS；>1080p 只有 DASH 一条路（仍会用，
    只是慢），音频/VP9 不受该限速影响、保持原选择器。
    env：VDL_YT_PREFER_HLS=0 可整体关闭（不需要重新发版）。
    """
    if os.environ.get("VDL_YT_PREFER_HLS", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    if quality_key in (AUDIO_KEY, M4A_KEY, WEBM_KEY):
        return False
    if quality_key == BEST_KEY:
        return True  # 「最佳画质」本就封顶 1080p avc1（见 _format_selector）
    try:
        return int(quality_key) <= 1080
    except (TypeError, ValueError):
        return False


def _youtube_hls_selector(quality_key: str) -> str:
    """YouTube HLS 优先的格式选择器；拿不到 HLS 时原样回落 DASH 链。

    protocol^=m3u8 命中 itag 91-96（m3u8_native 混合流）；height 上限与
    请求档一致（BEST_KEY 视同 1080）。注意 HLS 是**音视频混合流**，不需要
    合并音轨，落盘即成品。
    """
    if quality_key == BEST_KEY:
        cap = 1080
    else:
        try:
            cap = int(quality_key)
        except (TypeError, ValueError):
            return _format_selector(quality_key)
    if quality_key == BEST_KEY:
        # 自动档保留 `<=`：720P 的视频也要能下出东西
        return (
            "b[protocol^=m3u8][height<=1080]/"
            "best[protocol^=m3u8][height<=1080]/"
            f"{_format_selector(quality_key)}"
        )
    # 具体档位：HLS 混合流同样锁死这一档（横/竖屏各一条，±3% 容差），
    # 拿不到就落回 DASH 链，不许顺手换个清晰度。
    _tol = _tier_tolerance(cap)
    _lo, _hi = cap - _tol, cap + _tol
    return (
        f"b[protocol^=m3u8][height>={_lo}][height<={_hi}]/"
        f"b[protocol^=m3u8][width>={_lo}][width<={_hi}]/"
        f"{_format_selector(quality_key)}"
    )


def _quality_cap(quality_key: str) -> int:
    """把质量档映射成「最高允许高度」。非数字档（best/audio/webm/m4a）一律 1080。

    BEST_KEY 沿用既有约定：自动档封顶 1080（avc1 兼容性优先，见 _format_selector）。
    """
    try:
        return int(quality_key)
    except (TypeError, ValueError):
        return 1080


def _yt_fallback_chains(quality_key: str) -> list[str]:
    """YouTube 403 降级时依次尝试的格式链，**每一条都受请求档高度上限约束**。

    🔴 2026-10-21 事故（必须保留这条注解，别再把无上限的链加回来）：
    末环曾是 `bv*+ba/best[height<=1080]/best`。当用户选 1080P、而 1080P 的 H.264(DASH)
    被 googlevideo 403 拒绝时，前几环（全是 avc1/137）同样 403，最后落到 `bv*+ba` ——
    它**没有 height 上限**，直接选了 itag 400（AV1 2560×1440）+ 251：
    用户看到卡片「1080P · 约 398 MB」，实际下到 641 MiB 的 1440P，
    既多花 1.6 倍流量，又把 UI 上标着「PRO·原画需会员」的档位白给了出去。
    实测证据：任务 357fc472af3b405b，`downloaded_bytes=671874532`，
    与 `bv*+ba` 选出的 400+251 的 filesize 之和 671874532 完全一致。

    2026-10-21 第二轮：末环再改成**精确锁定** `height=cap`。
    `<=cap` 固然堵死了升档，但还留着降档的口子 —— 1080P 被 403 时会悄悄
    退到 720P，用户以为拿了 1080P，文件却是 720P 的。同样是「说得不算数」。
    从此 403 重试只许**在同档内**换编码（avc1→vp9→av01）、换协议
    （DASH→HLS）、换 client，**清晰度这个数一个像素都不许动**。
    全部拿不到 ⇒ 明确报错让用户重试或改选，绝不代他决定。
    """
    if quality_key == BEST_KEY:
        # 「最佳画质（自动）」语义本就是自动 —— 用户没有指定具体档位，
        # 自动档保留 `<=1080` 才有意义（720P 的视频也能下出东西）。
        _ITAG_HEIGHT = [
            ("299", 1080), ("248", 1080), ("137", 1080), ("136", 720),
            ("135", 480), ("134", 360), ("133", 240), ("160", 144),
        ]
        explicit = [f"{itag}+140" for itag, _h in _ITAG_HEIGHT]
        return [
            "bv*[vcodec^=avc1][height<=1080]+ba[ext=m4a]/b[ext=mp4][height<=1080]",
            "bv*[vcodec^=avc1][height<=1080]+ba/bv*[height<=1080]+ba/b[height<=1080]",
            "/".join(explicit),
            "bv*[height<=1080]+ba/best[height<=1080]/b[height<=1080]",
        ]
    # 具体档位 → 精确锁定。整条链内部已含「换编码 / 换协议 / 横竖屏」的退让，
    # 交给一轮即可；外层再靠 client 轮换重试。
    cap = _quality_cap(quality_key)
    _tol = _tier_tolerance(cap)
    _lo, _hi = cap - _tol, cap + _tol
    return [
        "/".join(_exact_height_chains(cap)),
        f"b[protocol^=m3u8][height>={_lo}][height<={_hi}]/"
        f"b[protocol^=m3u8][width>={_lo}][width<={_hi}]",
    ]


def is_valid_quality(quality_key: str) -> bool:
    # 常量档（最佳/仅音频/WebM/M4A）或任意 1..4320 真实清晰度档（2026-09-06 全档平铺后放开）
    if quality_key in (BEST_KEY, AUDIO_KEY, WEBM_KEY, M4A_KEY):
        return True
    try:
        h = int(quality_key)
    except (TypeError, ValueError):
        return False
    return 0 < h <= 4320

def quality_label(quality_key: str) -> str:
    if quality_key == BEST_KEY:
        return "最佳画质（自动）"
    if quality_key == AUDIO_KEY:
        return "仅音频 MP3"
    if quality_key == WEBM_KEY:
        return "WebM 格式"
    if quality_key == M4A_KEY:
        return "仅音频 M4A"
    return dict(QUALITY_PRESETS).get(int(quality_key), f"{quality_key}P")

# --------------------------------------------------------------------------- #
# 下载执行
# --------------------------------------------------------------------------- #

class _ProgressReporter:
    """把 yt-dlp 的分流进度聚合成单调递增的整体百分比。"""

    def __init__(self, task: DownloadTask, store: TaskStore) -> None:
        self._task = task
        self._store = store
        self._streams: dict[str, tuple[int, int]] = {}
        self._last_progress = 0.0
        self._has_download_step = False
        self._actual_short = 0  # 实际落盘的短边高度，用于完成后核对清晰度

    def __call__(self, payload: dict[str, Any]) -> None:
        if self._task.cancel_requested:
            raise DownloadCanceled()
        if getattr(self._task, "pause_requested", False):
            raise DownloadPaused()
        if payload.get("status") != "downloading":
            return
        self._ensure_title(payload)
        self._capture_actual(payload)
        if not self._has_download_step:
            self._task.add_step("下载音视频", "running", f"已选清晰度：{self._task.quality}")
            self._task.log(f"开始下载：{self._task.quality}")
            self._has_download_step = True
        key = payload.get("filename") or payload.get("tmpfilename") or "stream"
        downloaded = int(payload.get("downloaded_bytes") or 0)
        total = int(payload.get("total_bytes") or payload.get("total_bytes_estimate") or 0)
        self._streams[key] = (downloaded, max(total, downloaded))
        self._push(payload)

    def _capture_actual(self, payload: dict[str, Any]) -> None:
        """首次进度回调就记下实际选中的清晰度，供完成后核对。"""
        if self._actual_short:
            return
        _s = _info_short_side(payload.get("info_dict") or {})
        if _s:
            self._actual_short = _s

    def _actual_note(self) -> str:
        """下载完成文案：把「请求档 vs 实得档」摆到用户眼前。

        精确锁定后两者必然相等；一旦不相等，说明代码里又混进了 `<=` 或 `bv*`，
        宁可让用户看见这句 ⚠️ 去反馈，也不要再静默换档。
        """
        got = self._actual_short
        if not got:
            return "音视频下载完成"
        want = self._task.quality_key
        if want in (BEST_KEY, AUDIO_KEY, M4A_KEY, WEBM_KEY):
            return f"音视频下载完成 · 实际 {got}P"
        if str(got) != str(want):
            # 搜狐这类「按 format_id 映射档位」的平台：站内标称 360P 的源短边只有
            # 268（宽银幕裁切），此时报「清晰度不符」是误报，会吓到用户。
            if getattr(self._task, "_quality_mapped", False):
                return f"音视频下载完成 · 实际 {got}P（按平台档位映射）"
            return f"音视频下载完成 · ⚠️ 请求 {want}P 实得 {got}P（清晰度不符）"
        return f"音视频下载完成 · 实际 {got}P"

    def _ensure_title(self, payload: dict[str, Any]) -> None:
        """首个进度回调就把标题回填，避免任务卡片长时间显示"解析中"。"""
        if self._task.title:
            return
        title = (payload.get("info_dict") or {}).get("title")
        if title:
            self._store.update(self._task.id, title=title)
            self._task.add_step("解析视频信息", "done", f"已获取标题《{title}》")

    def _push(self, payload: dict[str, Any]) -> None:
        done = sum(d for d, _ in self._streams.values())
        total = sum(t for _, t in self._streams.values())
        percent = (done / total * 100) if total else 0.0
        self._last_progress = min(max(self._last_progress, percent), DOWNLOAD_PHASE_CEILING)
        self._store.update(
            self._task.id,
            status="downloading",
            progress=self._last_progress,
            downloaded_bytes=done,
            total_bytes=total,
            speed=float(payload.get("speed") or 0.0),
            eta=int(payload.get("eta") or 0),
        )

    def on_postprocess(self, payload: dict[str, Any]) -> None:
        if payload.get("status") == "started":
            self._task.add_step("下载音视频", "done", self._actual_note())
            self._task.add_step("合并与后处理", "running", "正在合并音视频…")
            self._store.update(self._task.id, status="merging", progress=98.0)

def _download_options(task: DownloadTask, quality_key: str, reporter: _ProgressReporter, *, cookie: str = "", proxy: str = "", format_id: str = "", concurrent_fragments: int = 0, downloader_type: str = "", resume: bool = False) -> dict:
    # YouTube 无 PO Token 时 ≤1080p 优先 HLS 混合流：DASH 自适应流被
    # googlevideo 按连接限速（2026-10-19 实测 ~600KB/s 聚合 + 硬阻断超时），
    # HLS 单连接即 1.5MB/s。判据与回落见 _yt_prefer_hls / _youtube_hls_selector。
    _fmt = _format_selector(quality_key)
    _task_host = _host_of(task.url)
    if (_task_host and _is_youtube_host(_task_host) and _yt_prefer_hls(quality_key)
            and not _pot_usable(proxy or _resolve_proxy(_task_host))):
        _fmt = _youtube_hls_selector(quality_key)
        logger.info("[youtube] 无 PO Token ⇒ %s 档优先 HLS 混合流（避开 DASH 按连接限速）", quality_key)
    options = _base_options(DOWNLOAD_RETRIES, _host_of(task.url), cookie=cookie, proxy=proxy) | {
        "format": _fmt,
        "outtmpl": {"default": f"%(title).{MAX_TITLE_CHARS}s.%(ext)s"},
        "paths": {"home": str(task.workdir)},
        "windowsfilenames": True,
        "concurrent_fragment_downloads": _clamp_concurrency(concurrent_fragments),
        "progress_hooks": [reporter],
        "postprocessor_hooks": [reporter.on_postprocess],
        "overwrites": True,
        # HLS 流优先走 Python 原生下载器，下载阶段不用 ffmpeg（沙盒偶发 SIGXCPU 152 强杀）
        # 仅保留最后的 TS→mp4 remux 调用 ffmpeg（快、低风险）
        "hls_prefer_native": True,
    }
    # CDP 嗅探直链的防盗链 Referer（2026-09-27）：必须显式覆盖 —— 嗅探 URL 的
    # host 是 CDN（如 upos-*.bilivideo.com），_base_options 按 host 生成的
    # Referer 是 CDN origin，B站 CDN 校验只认 https://www.bilibili.com/ 这类
    # 页面 origin，不覆盖必 403。
    if (getattr(task, "referer", "") or "").strip():
        options.setdefault("http_headers", {})["Referer"] = task.referer.strip()
    if _is_netdisk_direct(_task_host):
        # 百度按账号限速，但**请求身份**决定限速档：浏览器身份 ~78 KB/s，
        # 老 PC 客户端的 P2SP 标识 ~145 KB/s（1.5MB 交替 A/B 两轮一致）。
        # 同时禁掉分片并发，配合上面的单连接。
        options.setdefault("http_headers", {})["User-Agent"] = _NETDISK_UA
        options["concurrent_fragment_downloads"] = 1
    # 断点续传：保留 .part 分片的前提下，显式开启 continue 让 yt-dlp 从上次中断处接上。
    # aria2c 分支已在 _build_aria2c_args 内置 --continue=true；此处覆盖原生下载器场景。
    if resume:
        options["continue"] = True
    # 外部下载器：aria2c（需本机已装）。B4（2026-10-02）：
    #   - downloader_type / VDL_DOWNLOADER = "auto"（新默认）→ 有 aria2c 就用、无则静默回退原生
    #     （**Windows 例外**：auto 一律走原生，原因见下）；
    #   - 显式 "aria2c" → 有就用，无则警告回退；显式 "native" → 强制原生。
    _a2 = _aria2c_path()
    _dl_choice = (downloader_type or VDL_DOWNLOADER).strip().lower()
    if _is_netdisk_direct(_task_host):
        # 网盘直链多连接是负优化（实测 4/8/16 连接比单连接更慢，且会被掐断拿不满字节）
        # ⇒ 强制原生单连接，连用户显式选 aria2c 也一起兜住。
        logger.info("[网盘] 直链强制单连接下载（多连接实测更慢）")
        _dl_choice = "native"
    if _dl_choice == "aria2c" and _is_windows():
        # 🔴 Windows 一律不用 aria2c，**包括调用方显式指定**（2026-10-18 用户实测）。
        # 桌面版是 --windowed 的 GUI 进程，Windows 会给它 fork 的 aria2c.exe 新建控制台
        # 黑窗；实测这些窗口里的 aria2c 起不来、还挂着不退出 ⇒ 一开下载就不断弹新窗口。
        # 前端高级选项里仍留着 aria2c 下拉项（历史配置可能已被选中），所以必须在
        # 服务端兜住，而不是只依赖 auto 分支。
        logger.warning("Windows 不支持 aria2c（会弹控制台窗口），自动改用内置下载器")
        _dl_choice = "native"
    if _dl_choice == "auto":
        # 🔴 Windows：auto **不启用** aria2c（2026-10-18 用户实测事故）。
        # 桌面版是 GUI 进程，Windows 会给它 fork 的控制台程序新开一个黑窗；
        # 一个 DASH 任务会分 video/audio 多路，于是「一开下载就弹好几个黑框」，
        # 且实测 aria2c 在这些新控制台里起不来、窗口一直挂着不退出（一直累积）。
        # 已用 win_no_window 全局补 CREATE_NO_WINDOW 从根上压制弹窗，但 Windows 上
        # aria2c 的稳定性还没验证过 ⇒ auto 先一律走 yt-dlp 原生下载器（带
        # http_chunk_size 分块提速，见下方 else 分支），保住「能用」这个底线。
        # 需要 aria2c 时可显式 VDL_DOWNLOADER=aria2c / 请求字段 downloader="aria2c"。
        use_aria2c = _a2 is not None and not _is_windows()
    else:
        use_aria2c = _dl_choice == "aria2c"
    if use_aria2c and _a2:
        options["downloader"] = "aria2c"
        options["downloader_args"] = {"aria2c": _build_aria2c_args(concurrent_fragments)}
        logger.info("使用 aria2c 下载器（并发=%d, 路径=%s）", _clamp_concurrency(concurrent_fragments), _a2)
    elif _dl_choice == "aria2c":
        logger.warning("请求 aria2c 但本机未安装，回退原生下载器（请 brew install aria2 或 apt install aria2）")
    else:
        # 原生下载器提速（B4）：给直链开 Range 分块，concurrent_fragment_downloads 才能把
        # 大文件拆成多段并行拉（否则 http 单连接串行）。HLS 分片自身远小于分块值，不受影响。
        # 紧急回滚开关：VDL_HTTP_CHUNK_MB=0。
        try:
            _chunk_mb = int(os.environ.get("VDL_HTTP_CHUNK_MB", "10") or 10)
        except ValueError:
            _chunk_mb = 10
        if _chunk_mb > 0:
            options["http_chunk_size"] = _chunk_mb * 1024 * 1024
    if _MAX_FILE_BYTES:
        options["max_filesize"] = _MAX_FILE_BYTES
    if quality_key == AUDIO_KEY:
        options["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]
    elif quality_key == M4A_KEY:
        options["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "192"}
        ]
    elif quality_key == WEBM_KEY:
        options["merge_output_format"] = "webm"
    else:
        options["merge_output_format"] = "mp4"
        # 视频 remux 兜底：HLS 原生下载器可能产出 .m3u8/TS 容器（bestv/inke 等
        # 直链 m3u8 平台），flv/f4v 直链同理——统一 ffmpeg -c copy 快速封装成
        # mp4，避免用户拿到扩展名/容器错乱的文件（单文件场景 FFmpegMergerPP
        # 不触发，必须有此 remux 处理器）。
        options.setdefault("postprocessors", []).append(
            {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}
        )
    return options

def _ensure_audio_track(output: Path, info: dict[str, Any], task: DownloadTask) -> Path:
    """成品音轨完整性校验与补救（2026-08-23「部分下载无声」防御）。

    无声两大来源：
    A. DASH 分离下载（YouTube/B站）合并失败 → 成品只有视频轨，info.formats 有独立音频轨
    B. HLS MP2 / FLV MP3 等 mp4 容器不支持的音频编码 → remux -c copy 时被 ffmpeg 丢弃
    补救顺序：A 独立音频轨重新合并 → B 转封装 mkv 保留全轨 → 仍无声则任务日志明确提示。
    ffprobe/ffmpeg 缺失或补救失败均静默降级，绝不阻塞下载成功态。
    """
    def _has_audio(path: Path) -> bool:
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                 "-of", "compact", str(path)],
                capture_output=True, text=True, timeout=25,
            )
            return "codec_type=audio" in (r.stdout or "")
        except Exception:
            return True  # ffprobe 不可用/异常不阻塞，跳过校验

    try:
        if _has_audio(output):
            return output
    except Exception:
        return output

    task.log("检测到成品无音轨，尝试补救…")
    # —— 补救 A：info 提供独立音频轨（DASH 分离下载合并失败场景）——
    try:
        auds = [f for f in (info.get("formats") or []) if isinstance(f, dict)
                and f.get("acodec") and f["acodec"] != "none" and f.get("url")]
        if auds:
            auds.sort(key=lambda f: f.get("abr") or f.get("tbr") or 0, reverse=True)
            au = auds[0]
            aurl = str(au["url"])
            headers = au.get("http_headers") or info.get("http_headers") or {}
            atmp = output.with_name(output.stem + ".aud" + (Path(aurl.split("?")[0]).suffix or ".m4a"))
            hdr_args: list[str] = []
            for k, v in (headers or {}).items():
                hdr_args += ["-H", f"{k}: {v}"]
            dl = subprocess.run(
                ["curl", "-s", "-L", "-m", "120"] + hdr_args + ["-o", str(atmp), aurl],
                capture_output=True, timeout=130,
            )
            if atmp.exists() and atmp.stat().st_size > 1024 and _has_audio(atmp):
                merged = output.with_name(output.stem + ".merged.mp4")
                rr = subprocess.run(
                    ["ffmpeg", "-v", "error", "-y", "-i", str(output), "-i", str(atmp),
                     "-c", "copy", "-map", "0:v:0", "-map", "1:a:0", "-shortest", str(merged)],
                    capture_output=True, timeout=180,
                )
                if merged.exists() and merged.stat().st_size > 1024 and _has_audio(merged):
                    atmp.unlink(missing_ok=True)
                    merged.replace(output)
                    task.log("已重新合并音轨 ✅")
                    return output
            atmp.unlink(missing_ok=True)
    except Exception:
        logger.debug("音轨补救A失败（独立音频轨合并）", exc_info=True)

    # —— 补救 B：转封装 mkv 保留全轨（mp4 不支持的音频编码场景）——
    try:
        mkv = output.with_suffix(".mkv")
        rr = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(output), "-c", "copy", "-map", "0", str(mkv)],
            capture_output=True, timeout=120,
        )
        if mkv.exists() and mkv.stat().st_size > 1024 and _has_audio(mkv):
            output.unlink(missing_ok=True)
            task.log("已转封装 mkv 保留音轨 ✅")
            return mkv
        mkv.unlink(missing_ok=True)
    except Exception:
        logger.debug("音轨补救B失败（转 mkv）", exc_info=True)

    task.log("⚠️ 该视频源无音轨（或合并失败），文件为无声视频，请尝试其他清晰度")
    return output

def _locate_output(info: dict[str, Any], workdir: Path) -> Path:
    """优先用 yt-dlp 回报的路径，兜底扫描工作目录里最大的成品文件。"""
    for entry in info.get("requested_downloads") or []:
        path = entry.get("filepath")
        if path and Path(path).exists():
            return Path(path)
    candidates = [p for p in workdir.glob("*") if p.is_file() and p.suffix != ".part"]
    if not candidates:
        raise ResolveError("下载完成但未找到输出文件", "请重试一次")
    return max(candidates, key=lambda p: p.stat().st_size)

def _write_sidecar(output: Path, task: "DownloadTask", info: dict[str, Any]) -> None:
    """下载完成后在成品旁写一个 .vdlmeta.json，供本地媒体库展示标题/平台/作者/时长。"""
    try:
        meta = {
            "title": info.get("title") or output.stem,
            "platform": task.platform,
            "uploader": info.get("uploader") or info.get("channel") or "",
            "duration": int(info.get("duration") or 0),
            "source_url": info.get("webpage_url") or task.url,
            "thumbnail": info.get("thumbnail") or "",
            "completed_at": int(time.time()),
        }
        sidecar = output.with_name(output.stem + ".vdlmeta.json")
        sidecar.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    except Exception:
        logger.debug("写入元数据侧车失败: %s", output, exc_info=True)

def _retry_matrix(downloader_type: str) -> list[tuple[str, bool]]:
    """A2：可重试失败后的策略矩阵 [(downloader_type, alt_protocol), ...]，首项为原始策略。

    - 策略① 切换下载器：原策略会用 aria2c → 换 "native"；否则本机有 aria2c → 换 "aria2c"；
      本机没有 aria2c 则跳过该策略（避免重复一次无效尝试）。
    - 策略② alt_protocol=True：_run_once 内解析完 formats 后尝试切到 HLS 分片链路。
    返回长度 2~3（原始 + 1~2 个变体），供 run_download 按 attempt 取用。
    """
    matrix: list[tuple[str, bool]] = [(downloader_type, False)]
    choice = (downloader_type or VDL_DOWNLOADER).strip().lower()
    if _is_windows():
        # 🔴 Windows 的失败重试**绝不升级到 aria2c**（2026-10-18 用户实测）。
        # 原逻辑：原生下载失败 → 换 aria2c 再试一次。但 Windows 上 aria2c 会弹控制台
        # 黑窗且起不来，于是「下载一失败就多出几个关不掉的窗口」，失败重试越多窗口越多。
        # Windows 只保留「原生下载器 + 切 HLS 分片链路」两个变体。
        matrix.append((downloader_type, True))
        return matrix
    aria2c_available = _aria2c_path() is not None
    will_use_aria2c = choice == "aria2c" or (choice == "auto" and aria2c_available)
    if will_use_aria2c:
        matrix.append(("native", False))
    elif aria2c_available and downloader_type != "aria2c":
        matrix.append(("aria2c", False))
    matrix.append((downloader_type, True))
    return matrix

class _NetdiskCanceled(Exception):
    """网盘直链下载被用户取消 / 看门狗终止。"""


class _NetdiskPaused(Exception):
    """网盘直链下载被用户暂停（2026-10-26）。

    🔴 与取消的本质区别：**保留 `netdisk.part`**，下次 run_download 发现
    part 存在 → 带 `Range: bytes={size}-` 断点续传、追加写入。
    """


# Content-Type → 扩展名（只在解析阶段没给到带扩展名的文件名时兜底）
_NETDISK_EXT_BY_CT = {
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a",
    "audio/aac": ".aac", "audio/flac": ".flac", "audio/wav": ".wav",
    "video/mp4": ".mp4", "video/x-matroska": ".mkv", "video/webm": ".webm",
    "application/pdf": ".pdf", "application/zip": ".zip",
    "application/x-rar-compressed": ".rar", "application/x-7z-compressed": ".7z",
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "text/plain": ".txt",
}


def _safe_filename(name: str, fallback: str = "网盘文件") -> str:
    """清掉路径分隔符与控制字符，避免落盘越界。"""
    n = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", (name or "").strip())
    n = n.strip(" .")
    return n[:180] or fallback


def _netdisk_output_name(task: DownloadTask, content_type: str, final_url: str) -> str:
    """决定网盘文件的落盘名。

    🔴 优先用解析阶段拿到的**真实文件名**（通常自带扩展名，如 "008.xxx.mp3"）。
    只有它没带扩展名时才按 Content-Type 补一个 —— 绝不能让 yt-dlp 那套
    「按视频猜 .mp4」的逻辑生效，否则 mp3 会被存成 mp4。
    """
    title = _safe_filename(getattr(task, "title", "") or "")
    ctype = (content_type or "").split(";")[0].strip().lower()
    ct_ext = _NETDISK_EXT_BY_CT.get(ctype, "")
    if title:
        tail = title.rsplit(".", 1)[-1].lower() if "." in title else ""
        if tail and 1 <= len(tail) <= 5 and tail.isalnum():
            return title
        return title + ct_ext
    base = ""
    try:
        base = (final_url or task.url).split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    except Exception:  # noqa: BLE001
        base = ""
    base = _safe_filename(base, "网盘文件")
    if ct_ext and not base.lower().endswith(ct_ext):
        return base + ct_ext
    return base


def _netdisk_conn_count(total: int, chunk: int, ua: str = "") -> int:
    """并发连接数（百度直链限速是 per-connection，只能靠叠加连接提速）。

    实测（2026-10-28 夜，439MB / 超会账号 / 4MB 块 / 12s 窗口，curl 矩阵）：
        1 连 0.30 MB/s · 8 连 2.47 · 16 连 5.33 · 24 连 7.45 · 32 连 9.63 · 48 连 14.55
    每连接恒定 ~0.30MB/s，近似线性 ⇒ **连接数是唯一有效杠杆**，账号等级只决定单连接
    基线（本机非会员 0.09~0.15）。

    🔴 并发数必须看**走哪条通道**（2026-10-29 15:07 实测翻车）：UA 协商有时会命中
    浏览器 UA（分享链接换出的 dlink 只认它），那是百度的「网页下载」通道 —— 对并发
    最敏感，24 连跑到第 15 块就开始回 **HTTP 403**；同一台机器、同一时段，`netdisk;P2SP`
    客户端通道 32 连毫无 403。所以浏览器 UA 一律压到 4 连，P2SP 才给 12。
    `VDL_NETDISK_CONN` 可覆盖（设 1 即退回单连接）。并发档再撞 403 也有兜底：
    主循环跑完后缺失的块转**单连接补漏**，且断点保留。
    小文件按块数收敛 —— 每条连都要重握手 + 慢启动，开太多更慢
    （实测 1MB 块 16 连只有 1.51MB/s，4MB 块 16 连 5.33MB/s）。
    """
    blocks = max(1, (total + chunk - 1) // chunk)
    is_client = "netdisk;" in (ua or "").lower()
    ceiling = 12 if is_client else 4
    raw = (os.environ.get("VDL_NETDISK_CONN") or "").strip()
    if raw.isdigit() and int(raw) > 0:
        want = int(raw)
    else:
        want = ceiling if blocks >= ceiling else max(2, blocks)
    return max(1, min(want, blocks, 64))


class _NetdiskHTTPError(RuntimeError):
    def __init__(self, status: int, retry_after: str | None = None):
        super().__init__(f"HTTP {status}")
        self.status = status
        # 403 时百度有时会给 Retry-After，尊重它比自己拍脑袋退避更省事
        self.retry_after = retry_after


def _download_netdisk_serial(task: DownloadTask, store: TaskStore,
                             headers: dict, proxies, total: int, resume_from: int,
                             part: Path) -> tuple[bool, str]:
    """旧版串行实现（单连接 4MB 有界 Range 循环）—— 只在「拿不到总长」时兜底。

    百度直链**必须**有界 Range：不带 Range / `bytes=0-` 开放区间一律 403，
    错误码伪装成 31326「无权限」或 31360「直链过期」，极具误导性。

    返回 (成功与否, content_type)；状态由调用方统一收尾。
    暂停/取消仍用异常上抛，语义与并发版一致。
    """
    import requests

    CHUNK = max(256 * 1024, int(os.environ.get("VDL_NETDISK_CHUNK", str(4 * 1024 * 1024))))
    started = time.time()
    last_push = 0.0
    done = resume_from
    last_ct = ""
    with open(part, "ab" if resume_from else "wb") as fh:
        offset = resume_from
        while True:
            h = dict(headers)
            h["Range"] = f"bytes={offset}-{offset + CHUNK - 1}"
            with requests.get(task.url, headers=h, proxies=proxies, stream=True,
                              timeout=(15, 60), allow_redirects=True) as r:
                last_ct = r.headers.get("Content-Type", "") or last_ct
                if r.status_code == 416:
                    break
                if r.status_code >= 400:
                    raise _NetdiskHTTPError(r.status_code)
                cr = r.headers.get("Content-Range") or ""
                if "/" in cr:
                    tot = cr.rsplit("/", 1)[-1].strip()
                    if tot.isdigit() and int(tot) > 0:
                        total = int(tot)
                        store.update(task.id, total_bytes=total, filesize=total)
                got = 0
                for chunk in r.iter_content(256 * 1024):
                    if not chunk:
                        continue
                    if task.cancel_requested or task.is_finished:
                        raise _NetdiskCanceled()
                    if task.pause_requested:
                        raise _NetdiskPaused()
                    fh.write(chunk)
                    got += len(chunk)
                    done += len(chunk)
                    now = time.time()
                    if now - last_push >= 0.5:
                        last_push = now
                        elapsed = max(now - started, 0.001)
                        speed = (done - resume_from) / elapsed
                        store.update(
                            task.id,
                            downloaded_bytes=done,
                            progress=round(done / total * 100.0, 1) if total else 0.0,
                            speed=round(max(speed, 0.0), 1),
                            eta=int((total - done) / speed) if (total and speed > 0) else 0,
                        )
            if total and done >= total:
                break
            if got < CHUNK or got <= 0:
                break
            offset = done
    return True, last_ct


def _netdisk_sleep(seconds: float) -> None:
    """退避睡眠的唯一出口。

    单独抽一层是为了测试可打桩：403 退避最长会睡 30s，用例里真睡就没法跑了。
    """
    if seconds > 0:
        time.sleep(seconds)


def _download_netdisk_direct(task: DownloadTask, store: TaskStore,
                             cookie: str = "", proxy: str = "") -> None:
    """百度网盘直链 → 下载（**不经过 yt-dlp 解析**）。

    🔴 为什么绕开 yt-dlp：d.pcs.baidu.com 的 dlink 会被 generic 提取器当成媒体页面，
    造成 ① 文件名变成直链路径段、② mp3 被猜成 .mp4、③ 部分直链提取阶段直接失败。

    🔴 2026-10-28 提速（用户：「Mac 能突破 10MB/s，我们为什么不行」）：
    百度对**每一条 TCP 连接**单独限速，实测同一条 dlink 每连恒定 ~0.30MB/s，
    8/16/24/32/48 连呈严格线性（2.47/5.33/7.45/9.63/14.55 MB/s）。
    旧实现是 `while True: 4MB 有界 Range` —— **任一瞬间只有 1 条连接**，
    所以永远卡在 0.09~0.30MB/s；`VDL_NETDISK_CHUNK` 只改块大小、aria2c 分片参数
    在这条链路上根本没被消费。现在改为「1 字节探测拿总长 → N 连并发拉块 →
    位图记已完成块」，断点续传/暂停/取消语义保持不变。

    其余约束照旧：每块必须有界 Range、UA 运行时协商、Cookie 手动带。
    `netdisk.part` 是预分配到最终大小的容器，`netdisk.part.map` 每块一字节记录
    完成状态（多线程乱序写盘，无法再用「文件大小」当进度）。
    """
    import queue  # noqa: PLC0415 — 延迟导入，与本文件其它路径一致
    import requests  # noqa: PLC0415

    workdir = Path(task.workdir) if task.workdir else None
    if workdir is None:
        store.update(task.id, status="failed", error="任务缺少工作目录")
        return
    workdir.mkdir(parents=True, exist_ok=True)

    part = workdir / "netdisk.part"
    bmap = workdir / "netdisk.part.map"
    CHUNK = max(256 * 1024, int(os.environ.get("VDL_NETDISK_CHUNK", str(4 * 1024 * 1024))))
    headers = {"User-Agent": _pick_netdisk_ua(task.url, cookie, proxy)}
    if cookie:
        headers["Cookie"] = cookie
    proxies = {"http": proxy, "https": proxy} if proxy else None

    total = int(getattr(task, "total_bytes", 0) or 0)
    meta = {"ct": "", "url": task.url}

    # ── 1) 1 字节探测：拿总长 + 顺带验证 Range 是否被接受（代价一个 RTT）
    try:
        with requests.get(task.url, headers={**headers, "Range": "bytes=0-0"},
                          proxies=proxies, stream=True, timeout=(15, 30),
                          allow_redirects=True) as r:
            if r.status_code >= 400:
                raise _NetdiskHTTPError(r.status_code)
            meta["ct"] = r.headers.get("Content-Type") or ""
            meta["url"] = r.url or task.url
            cr = r.headers.get("Content-Range") or ""
            if "/" in cr:
                tot = cr.rsplit("/", 1)[-1].strip()
                if tot.isdigit() and int(tot) > 0:
                    total = int(tot)
    except _NetdiskHTTPError as exc:
        msg = str(exc)
        task.add_step("下载文件", "error", msg)
        logger.warning("[netdisk] 探测直链失败 task=%s：%s", task.id, msg)
        store.update(task.id, status="failed", error="网盘下载失败",
                     hint=_user_error_text())
        return
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)[:200]
        task.add_step("下载文件", "error", msg)
        logger.warning("[netdisk] 探测直链异常 task=%s：%s", task.id, msg)
        store.update(task.id, status="failed", error="网盘下载失败",
                     hint=_user_error_text())
        return

    if total:
        store.update(task.id, total_bytes=total, filesize=total)

    # ── 2) 拿不到总长（服务端忽略 Range / 回 200 全量）→ 退回串行实现
    if not total:
        resume_from = part.stat().st_size if part.exists() else 0
        task.add_step("下载文件", "running", "连接中…")
        try:
            ok, ct = _download_netdisk_serial(task, store, headers, proxies, total,
                                              resume_from, part)
        except _NetdiskCanceled:
            part.unlink(missing_ok=True)
            bmap.unlink(missing_ok=True)
            if not task.is_finished:
                store.update(task.id, status="canceled", error="已取消")
            return
        except _NetdiskPaused:
            kept = part.stat().st_size if part.exists() else 0
            task.add_step("下载文件", "pending", "已暂停（点「继续」断点续传）")
            task.log(f"用户暂停网盘下载：已下 {kept} 字节，保留 .part 供续传")
            store.update(task.id, status="paused", speed=0.0, eta=0,
                         downloaded_bytes=kept,
                         progress=round(kept / total * 100.0, 1) if total else 0.0)
            return
        except Exception as exc:  # noqa: BLE001
            part.unlink(missing_ok=True)
            msg = str(exc)[:200]
            task.add_step("下载文件", "error", msg)
            logger.warning("[netdisk] 百度直链下载失败 task=%s：%s", task.id, msg)
            store.update(task.id, status="failed", error="网盘下载失败",
                         hint=_user_error_text())
            return
        out_name = _netdisk_output_name(task, ct or meta["ct"], meta["url"])
        out = workdir / out_name
        try:
            if out.exists():
                out.unlink()
            part.replace(out)
        except OSError as exc:
            store.update(task.id, status="failed", error="保存文件失败",
                         hint=str(exc)[:200])
            return
        size = out.stat().st_size
        task.add_step("下载文件", "done", f"输出文件：{out.name}")
        task.log(f"网盘下载完成：{out.name}（{size} 字节，单连接兜底）")
        store.update(task.id, status="completed", progress=100.0, filepath=out,
                     filename=out.name, filesize=size, downloaded_bytes=size,
                     total_bytes=size, speed=0.0, eta=0)
        return

    # ── 3) 并发分块下载
    nblocks = max(1, (total + CHUNK - 1) // CHUNK)
    done_map = bytearray(nblocks)
    resume_note = ""
    if part.exists() and bmap.exists():
        try:
            saved = bytearray(bmap.read_bytes())
            if len(saved) == nblocks:
                done_map = saved
                resume_note = f"续传：{sum(done_map)}/{nblocks} 块已完成"
        except OSError:
            done_map = bytearray(nblocks)
    elif part.exists():
        # 🔴 旧版（串行）留下的 .part 没有位图 —— 多线程会乱序写盘，光看文件大小
        #    无法判断哪些字节是真下好的，只能整份重下，绝不能拿 st_size 当进度。
        resume_note = "检测到旧版半成品（无分块位图），从头下载"

    done_base = 0
    for i in range(nblocks):
        if done_map[i]:
            done_base += min(CHUNK, total - i * CHUNK)

    try:
        mode = "r+b" if part.exists() else "wb"
        with open(part, mode) as fh:
            if fh.seek(0, os.SEEK_END) != total:
                fh.truncate(total)      # 预分配：NTFS 只登记长度，不真占盘
    except OSError as exc:
        store.update(task.id, status="failed", error="创建临时文件失败",
                     hint=str(exc)[:200])
        return
    try:
        bmap.write_bytes(bytes(done_map))
    except OSError as exc:
        store.update(task.id, status="failed", error="创建分块位图失败",
                     hint=str(exc)[:200])
        return

    q: queue.Queue = queue.Queue()
    for i in range(nblocks):
        if not done_map[i]:
            q.put(i)

    nconn = _netdisk_conn_count(total, CHUNK, headers.get("User-Agent", ""))
    stop = threading.Event()
    lock = threading.Lock()
    done = [done_base]
    last_push = [0.0]
    fatal: list = []
    seen403 = [0]                 # 累计 403 次数（日志与降级判断用）
    cool_until = [0.0]            # 全局冷却到期时间戳：撞到 403 时所有连接一起歇
    started = time.time()

    store.update(task.id, status="downloading", speed=0.0, eta=0,
                 downloaded_bytes=done_base,
                 progress=round(done_base / total * 100.0, 1) if done_base else 0.0)
    task.add_step("下载文件", "running",
                  resume_note or f"并发 {nconn} 连接下载 {nblocks} 块…")

    def _account(nbytes: int) -> None:
        """进度上报（节流到 0.5s 一次，避免多线程高频写 store）。"""
        with lock:
            done[0] += nbytes
            now = time.time()
            if now - last_push[0] < 0.5:
                return
            last_push[0] = now
            speed = (done[0] - done_base) / max(now - started, 0.001)
            store.update(
                task.id,
                downloaded_bytes=done[0],
                progress=round(done[0] / total * 100.0, 1),
                speed=round(max(speed, 0.0), 1),
                eta=int((total - done[0]) / speed) if speed > 0 else 0,
            )

    def _fetch_into(wf, sess, i: int) -> None:
        """拉第 i 块写进 wf（工作句柄），成功则置位图。失败一律抛异常。

        🔴 403 是**可以缓过来的**（百度按连接/时间窗限流，不是权限问题）：这里只
        负责把 403 报上去，重试策略由调用方按「线程池 → 单连接补漏」两级决定。
        """
        st = i * CHUNK
        en = min(st + CHUNK, total) - 1
        want = en - st + 1
        h = dict(headers)
        h["Range"] = f"bytes={st}-{en}"
        with sess.get(task.url, headers=h, proxies=proxies,
                      stream=True, timeout=(15, 30),
                      allow_redirects=True) as r:
            if r.status_code >= 400:
                raise _NetdiskHTTPError(r.status_code,
                                        r.headers.get("Retry-After"))
            if r.status_code == 206:
                meta["ct"] = r.headers.get("Content-Type") or meta["ct"]
                meta["url"] = r.url or meta["url"]
            wf.seek(st)
            got = 0
            for piece in r.iter_content(256 * 1024):
                if not piece:
                    continue
                if task.cancel_requested or task.is_finished or task.pause_requested:
                    stop.set()
                    raise _NetdiskPaused()
                wf.write(piece)
                got += len(piece)
                _account(len(piece))
        if got != want:
            raise RuntimeError(f"块 {i} 长度不符 {got} != {want}")
        wf.flush()
        with lock:
            done_map[i] = 1
            bmap.write_bytes(bytes(done_map))

    def _backoff(attempt: int, retry_after) -> float:
        """403 用更长的退避：百度限流窗口是秒级到分钟级，0.8s 硬重试等于白撞。"""
        base = min(2.0 * (2 ** attempt), 30.0)
        try:
            if retry_after:
                base = max(base, min(float(retry_after), 60.0))
        except (TypeError, ValueError):
            pass
        return base + random.uniform(0, 1.0)

    def _worker() -> None:
        try:
            wf = open(part, "r+b")
        except OSError as exc:
            with lock:
                if not fatal:
                    fatal.append(f"打开临时文件失败：{exc}")
            stop.set()
            return
        # 每条 worker 复用一个 Session：TCP/TLS 握手只做一次。
        # 小文件（几块就下完）全靠这个提速 —— 实测 51MB/13 块不复用连接只有
        # 0.90MB/s，复用后接近连接数应得的并行速率。
        sess = requests.Session()
        sess.trust_env = False
        try:
            while not stop.is_set():
                if task.cancel_requested or task.is_finished or task.pause_requested:
                    stop.set()
                    return
                try:
                    i = q.get_nowait()
                except queue.Empty:
                    return
                err = None
                for attempt in range(4):
                    if stop.is_set():
                        return
                    gap = cool_until[0] - time.time()
                    if gap > 0:
                        _netdisk_sleep(min(gap, 60.0))
                    try:
                        _fetch_into(wf, sess, i)
                        err = None
                        break
                    except _NetdiskPaused:
                        return
                    except _NetdiskHTTPError as exc:
                        err = exc
                        if exc.status == 403:
                            with lock:
                                seen403[0] += 1
                                # 指数式全局冷却：第一批等 3s，持续撞就 6/12/24s
                                cool_until[0] = max(
                                    cool_until[0],
                                    time.time() + min(3.0 * (2 ** attempt), 24.0))
                        elif exc.status == 416:
                            break
                        _netdisk_sleep(_backoff(attempt, exc.retry_after))
                    except Exception as exc:  # noqa: BLE001
                        err = exc
                        _netdisk_sleep(_backoff(attempt, None))
                if err is not None:
                    # 不在这里判死刑：留在位图里，交给收尾阶段的单连接补漏
                    q.put(i)
                    return
        finally:
            try:
                wf.close()
            except OSError:
                pass

    threads = [threading.Thread(target=_worker, daemon=True) for _ in range(nconn)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # ── 3b) 单连接补漏：并发档撞 403 时，一条连接慢慢把剩下的块补齐。
    #        线上实测单连接几乎不触发限流（每连 ~0.30MB/s），宁可慢也不能缺块。
    missing = [i for i in range(nblocks) if not done_map[i]]
    if missing and not stop.is_set():
        task.add_step("下载文件", "running",
                      f"并发受限，改为单连接补齐剩余 {len(missing)} 块…")
        task.log(f"有 {len(missing)} 块在并发阶段未下完（403 {seen403[0]} 次），"
                 f"转单连接补漏，共 {len(missing)} 块")
        try:
            wf2 = open(part, "r+b")
        except OSError as exc:
            with lock:
                if not fatal:
                    fatal.append(f"打开临时文件失败：{exc}")
            wf2 = None
        if wf2 is not None:
            sess2 = requests.Session()
            sess2.trust_env = False
            try:
                for i in missing:
                    if stop.is_set() or task.cancel_requested or task.is_finished \
                            or task.pause_requested:
                        break
                    err = None
                    for attempt in range(6):
                        try:
                            _fetch_into(wf2, sess2, i)
                            err = None
                            break
                        except _NetdiskPaused:
                            break
                        except _NetdiskHTTPError as exc:
                            err = exc
                            if exc.status == 403:
                                with lock:
                                    seen403[0] += 1
                            _netdisk_sleep(_backoff(attempt + 1, exc.retry_after))
                        except Exception as exc:  # noqa: BLE001
                            err = exc
                            _netdisk_sleep(_backoff(attempt + 1, None))
                    if err is not None:
                        with lock:
                            if not fatal:
                                fatal.append(f"块 {i} 补漏失败：{err}")
                        break
            finally:
                try:
                    wf2.close()
                except OSError:
                    pass

    # ── 4) 收尾：取消 / 暂停 / 失败 / 完成
    if task.cancel_requested or task.is_finished:
        part.unlink(missing_ok=True)
        bmap.unlink(missing_ok=True)
        if not task.is_finished:
            store.update(task.id, status="canceled", error="已取消")
        return
    if task.pause_requested:
        kept = sum(done_map)
        kept_bytes = 0
        for i in range(nblocks):
            if done_map[i]:
                kept_bytes += min(CHUNK, total - i * CHUNK)
        task.add_step("下载文件", "pending", "已暂停（点「继续」断点续传）")
        task.log(f"用户暂停网盘下载：已下 {kept_bytes} 字节（{kept}/{nblocks} 块），"
                 f"保留 .part + 位图供续传")
        store.update(task.id, status="paused", speed=0.0, eta=0,
                     downloaded_bytes=kept_bytes,
                     progress=round(kept_bytes / total * 100.0, 1))
        return
    if fatal or any(v == 0 for v in done_map):
        msg = fatal[0] if fatal else "存在未完成分块"
        kept_bytes = 0
        for i in range(nblocks):
            if done_map[i]:
                kept_bytes += min(CHUNK, total - i * CHUNK)
        # 🔴 不再删 .part/位图：一次 403 就把 361MB 的 17% 清掉太亏，
        #    留着「点重试从断点续」比从头下快得多（且续传时同样只补缺块）。
        task.add_step("下载文件", "error", str(msg)[:200])
        logger.warning("[netdisk] 百度直链下载中断 task=%s：%s（403 共 %d 次，"
                       "已下 %d/%d 块，保留断点）",
                       task.id, msg, seen403[0], sum(done_map), nblocks)
        store.update(task.id, status="failed", error="网盘下载中断",
                     hint=f"已下载 {kept_bytes/1048576:.0f}MB 保留在断点里，"
                          f"点「重试」会从断点续传（百度接口返回异常，可能触发风控，"
                          f"稍等几分钟再试更稳）",
                     downloaded_bytes=kept_bytes,
                     progress=round(kept_bytes / total * 100.0, 1) if total else 0.0)
        return

    out_name = _netdisk_output_name(task, meta["ct"], meta["url"])
    out = workdir / out_name
    try:
        if out.exists():
            out.unlink()
        part.replace(out)
        bmap.unlink(missing_ok=True)
    except OSError as exc:
        store.update(task.id, status="failed", error="保存文件失败", hint=str(exc)[:200])
        return

    size = out.stat().st_size
    elapsed = max(time.time() - started, 0.001)
    avg = (size - done_base) / elapsed / 1048576
    task.add_step("下载文件", "done", f"输出文件：{out.name}")
    task.log(f"网盘下载完成：{out.name}（{size} 字节，{nconn} 连接并发，均速 {avg:.2f}MB/s）")
    store.update(task.id, status="completed", progress=100.0, filepath=out,
                 filename=out.name, filesize=size, downloaded_bytes=size,
                 total_bytes=size, speed=0.0, eta=0)

def _force_land_paused(task: DownloadTask, store: TaskStore) -> None:
    """暂停标志长期未被进度钩子消费时，由主等待循环就地落 paused。

    背景（2026-10-27 用户实测）：暂停/取消都只在 `_ProgressReporter`（yt-dlp 进度钩子）
    里被消费；合并与后处理、以及 socket 停滞阶段**一条钩子都不发**，暂停标志无人消费 ⇒
    UI 永久停在「暂停中…」，且取消按钮当时被前端一并隐藏，用户两条路都走不了。

    孤儿下载线程可能还活着（停滞的 socket 会在 socket_timeout 后醒来写终态）：
    这里自增 `_run_gen`，`_run_once` 退出时的代际守卫会拦下它的终态写入，
    避免把用户已暂停的任务又改写成 completed/failed。
    """
    task._run_gen = getattr(task, "_run_gen", 0) + 1
    task.pause_requested = False
    task.add_step("下载音视频", "done", "已暂停（可继续下载）")
    task.log("下载正处于无进度回调阶段，暂停已就地生效（点「继续」可续传）")
    store.update(task.id, status="paused", speed=0.0, eta=0)


def run_download(task: DownloadTask, store: TaskStore, quality_key: str, cookie: str = "", proxy: str = "", max_retries: int = 0, format_id: str = "", concurrent_fragments: int = 0, downloader_type: str = "", resume: bool = False) -> None:
    """在后台线程执行，全部异常都写回任务状态，不向外抛。

    max_retries=N 时，对网络/超时/连接类等「可重试」错误最多再试 N 次（指数退避）。
    重试在 worker 线程内循环进行，不会额外占用并发槽；会员受限 / 链接失效等不可重试
    错误会直接以 failed 结束，避免无效重试浪费带宽。
    A2（2026-10-02）：即使 max_retries=0，可重试失败也会按 _retry_matrix 策略矩阵
    额外尝试至多 2 次（切换下载器 → 切换 HLS 分片链路），不是原样重试。

    健壮性：内置「停滞看门狗 + 整体硬超时」，防止站点/CDN 假死让任务永久挂起、
    占满并发槽拖垮后续所有下载（典型如 m3u8 流慢速 trickle 不触发 socket_timeout）。
    """
    # 网盘直链（2026-10-24）：跳过 yt-dlp 解析，按文件直接下载。
    # 走 yt-dlp 会把标题换成直链路径段、扩展名猜成 .mp4（mp3 被存成 mp4），
    # 少数情况还会在 generic 提取阶段直接失败。
    if _is_netdisk_direct(task.url):
        _download_netdisk_direct(task, store,
                                 cookie=(cookie or task.cookie or ""),
                                 proxy=(proxy or task.proxy or ""))
        return

    # 运行代际：每次监督（首次下载 / resume 重提）自增。_run_once 在入口捕获自己的代际，
    # 退出时若代际已变（被「就地暂停」或新一轮取代）就不再写终态，防孤儿线程覆盖。
    task._run_gen = getattr(task, "_run_gen", 0) + 1

    stop = threading.Event()
    last = {"bytes": 0, "disk": 0, "ts": time.time()}
    _workdir = Path(task.workdir) if task.workdir else None

    def _workdir_bytes() -> int:
        """工作目录里最大文件体积——m3u8_native 等协议 yt-dlp 进度钩子只在整段下完才触发，
        不足以作为「还在跑」的信号；用磁盘上文件实际增长作兜底。"""
        if not _workdir or not _workdir.is_dir():
            return 0
        try:
            return max((p.stat().st_size for p in _workdir.iterdir() if p.is_file()), default=0)
        except OSError:
            return 0

    def _watchdog() -> None:
        """下载中但 N 秒无字节增量 → 判定停滞，置取消标记让进度回调抛出终止。
        信号：①yt-dlp 进度钩子报告的 downloaded_bytes ②工作目录里最大文件体积
        （覆盖 m3u8_native/分段合并等无进度钩子场景）"""
        while not stop.is_set() and not task.is_finished:
            time.sleep(WATCHDOG_POLL)
            if task.status != "downloading":
                continue
            cur_disk = _workdir_bytes()
            if cur_disk > last["disk"]:
                last["disk"] = cur_disk
                last["ts"] = time.time()
            if task.downloaded_bytes > last["bytes"]:
                last["bytes"] = task.downloaded_bytes
                last["ts"] = time.time()
            elif cur_disk > last["disk"]:
                pass  # 上一分支已更新时间
            elif time.time() - last["ts"] > DOWNLOAD_STALL_TIMEOUT:
                task.add_step("下载音视频", "error", f"停滞 {DOWNLOAD_STALL_TIMEOUT}s，已自动终止")
                task.log(f"下载停滞超过 {DOWNLOAD_STALL_TIMEOUT}s，自动终止")
                task.cancel_reason = "stall"   # A3：与用户取消区分，终态给可行动错误
                task.cancel_requested = True
                return

    wd = threading.Thread(target=_watchdog, name=f"wd-{task.id}", daemon=True)
    wd.start()
    try:
        # A2 通用失败矩阵（2026-10-02）：可重试失败后不是原样重试，而是换策略——
        #   策略① 切换下载器（aria2c ↔ 原生）：不同连接/TLS 栈，绕开单一下载器兼容性问题；
        #   策略② 改用 HLS 分片链路（_switch_to_hls）：直链被风控/403 时常能通过。
        # 最多额外 2 次尝试；YouTube 的 client 降级链在 _run_once 内部独立处理，互不影响。
        _matrix = _retry_matrix(downloader_type)
        for attempt in range(1, max(max_retries + 1, min(len(_matrix), 3)) + 1):
            _dt, _alt = _matrix[min(attempt - 1, len(_matrix) - 1)]
            if attempt > 1:
                if _dt != downloader_type:
                    task.log(f"重试策略：切换下载器为 {_dt or '原生'}")
                if _alt:
                    task.log("重试策略：本次优先尝试 HLS 分片链路")
                # 重试前把状态拨回排队，让前端进度条归零、状态显示「重试中」
                store.update(
                    task.id, status="pending", error="", hint="", progress=0.0,
                    downloaded_bytes=0, total_bytes=0, speed=0.0, eta=0,
                )
                last["bytes"] = 0
                last["ts"] = time.time()
            task.cancel_reason = ""   # 新一轮尝试：清掉上一轮的停滞/超时标记
            # 每次尝试独立代际：上一轮若留了孤儿线程（停滞/硬超时强制终态后它仍活着），
            # 它醒来时不得再写终态覆盖本轮结果。
            task._run_gen = getattr(task, "_run_gen", 0) + 1
            # 实际下载放到子线程，主线程带「整体硬超时」等待，避免解析/下载任意阶段无限挂起
            th = threading.Thread(
                target=_run_once, args=(task, store, quality_key, cookie, proxy, format_id, concurrent_fragments, _dt, resume, _alt),
                name=f"dl-{task.id}-{attempt}", daemon=True,
            )
            th.start()
            # B3（2026-10-02）：硬上限到点时若任务仍在推进（最近 DOWNLOAD_STALL_TIMEOUT 内
            # 有字节增量，last["ts"] 由看门狗维护），不杀任务、继续等——只杀真正停滞/假死的。
            # 超长视频（几小时直播回放、4K 大片）此前会被 7200s 硬上限「进度正常也被处决」。
            while True:
                # 实测 F2（2026-10-11）：原先一次性 join(整体硬上限) 会干等最多 7200s。
                # 期间若看门狗已置 cancel_requested（下载线程卡在无进度回调的阶段，
                # 标志长期不被消费），任务既不落终态、DELETE 也清不掉、还白占并发槽。
                # 改为短切片 join：一旦发现取消/停滞标志立即落终态并跳出。
                _wait_left = float(DOWNLOAD_HARD_TIMEOUT)
                _early = False
                _pause_stuck = False
                _pause_since = time.time() if (
                    getattr(task, "pause_requested", False) and task.status == "pausing") else 0.0
                while _wait_left > 0 and th.is_alive():
                    _slice = min(WATCHDOG_POLL, _wait_left)
                    th.join(timeout=_slice)
                    _wait_left -= _slice
                    if not th.is_alive():
                        break
                    if task.cancel_requested and not task.is_finished:
                        _early = True
                        break
                    # 暂停兜底（2026-10-27）：取消有上面的 cancel_requested 分支兜底，
                    # 暂停此前没有 —— 而暂停标志同样只在进度钩子里被消费，合并/后处理/
                    # socket 停滞阶段没有任何钩子，任务会永久停在 pausing。宽限
                    # PAUSE_FORCE_LAND_SECONDS 秒仍没落地就就地落 paused。
                    if getattr(task, "pause_requested", False) and task.status == "pausing":
                        if not _pause_since:
                            _pause_since = time.time()
                        elif time.time() - _pause_since >= PAUSE_FORCE_LAND_SECONDS:
                            _pause_stuck = True
                            break
                    else:
                        _pause_since = 0.0
                if not th.is_alive():
                    break
                if _early:
                    # 看门狗/用户已判定终止：用与子线程同一份逻辑落终态，不再干等硬超时。
                    _finalize_terminated(task, store)
                    break
                if _pause_stuck:
                    _force_land_paused(task, store)
                    break
                if task.status == "downloading" and (time.time() - last["ts"]) <= DOWNLOAD_STALL_TIMEOUT:
                    task.log(
                        f"下载满 {DOWNLOAD_HARD_TIMEOUT}s 但仍在推进（未见停滞），自动延长等待"
                    )
                    continue
                task.add_step("下载音视频", "error", f"超过硬上限 {DOWNLOAD_HARD_TIMEOUT}s")
                task.log(f"下载超过整体硬上限 {DOWNLOAD_HARD_TIMEOUT}s，强制结束")
                task.cancel_reason = "timeout"   # A3：与用户取消区分
                task.cancel_requested = True
                store.update(
                    task.id, status="failed", error="下载超时",
                    hint="站点响应过慢或连接不稳定，请稍后重试或更换清晰度/代理",
                )
                break
            t = store.get(task.id)
            if t is None:
                return
            if t.status != "failed":
                return  # completed / canceled -> 停止
            _total_attempts = max(max_retries + 1, min(len(_matrix), 3))
            if attempt >= _total_attempts:
                return
            if not _is_retryable(t.error):
                return
            time.sleep(min(2 ** attempt, 30))  # 指数退避，最多 30s
    finally:
        stop.set()
        # 2026-10-20：终态回写「这次到底下载成功没有」——内部重试的中间失败不报，
        # 只在整条任务收敛后报一次（成功＝completed 且成品文件真实存在且非空）。
        _note_usage_result_by_task(task)

def _mark_step_error(task: DownloadTask, detail: str) -> None:
    """根据当前进行中的步骤，把对应步骤标记为失败。"""
    if any(s.get("name") == "合并与后处理" and s.get("status") == "running" for s in task.steps):
        task.add_step("合并与后处理", "error", detail)
    elif any(s.get("name") == "下载音视频" and s.get("status") in ("running", "done") for s in task.steps):
        task.add_step("下载音视频", "error", detail)
    else:
        task.add_step("解析视频信息", "error", detail)

def _finalize_terminated(task: DownloadTask, store: TaskStore) -> None:
    """把「看门狗停滞 / 整体硬超时 / 用户取消」按 cancel_reason 统一落成终态。

    实测 F2（2026-10-11）：终态原先只在下载子线程的 `except DownloadCanceled` 里写。
    但 `cancel_requested` 只在**进度回调**与若干重试循环边界被消费——若子线程正阻塞在
    **无回调**阶段（解析元数据 / 取流 URL / 连接假死），该标志长时间不被读取，任务便以
    「步骤已写『已自动终止』、整体状态仍 downloading」的形态僵持到整体硬超时
    （DOWNLOAD_HARD_TIMEOUT=7200s），期间 DELETE 清不掉、白占并发槽。

    抽出本函数后，主等待循环一旦看到 `cancel_requested` 也能立即落终态，不必干等。
    **幂等**：重复调用产出同一终态（子线程随后真正退出时再调一次也不会写歪）。
    """
    _reason = getattr(task, "cancel_reason", "") or ""
    if _reason == "stall":
        _mark_step_error(task, f"下载停滞超过 {DOWNLOAD_STALL_TIMEOUT}s，已自动终止")
        store.update(
            task.id, status="failed", error="下载停滞，已自动终止",
            hint=f"连接超过 {DOWNLOAD_STALL_TIMEOUT}s 没有任何进展（站点/网络假死）。"
                 "点「继续下载」可从断点续传，或更换清晰度/代理后重试",
            category="stalled", resumable=_has_partial(task.workdir),
        )
    elif _reason == "timeout":
        _mark_step_error(task, "下载超时，已强制结束")
        store.update(
            task.id, status="failed", error="下载超时",
            hint="站点响应过慢或连接不稳定，请稍后重试或更换清晰度/代理",
            category="timeout", resumable=_has_partial(task.workdir),
        )
    else:
        _mark_step_error(task, "用户已取消")
        # 断点续传：取消时【不清除】工作目录里的 .part 分片，仅当确实残留部分文件时标记可续传。
        resumable = _has_partial(task.workdir)
        store.update(
            task.id, status="canceled", error="已取消下载",
            progress=task.progress, downloaded_bytes=task.downloaded_bytes,
            resumable=resumable,
        )
        if not resumable:
            store.clear_files(task.id)
    # 2026-10-20：停滞/超时/用户取消都不算「保存到本机」，把失败结果补回账本
    # （幂等：run_download 的 finally 再报一次也不会写歪）
    _note_usage_result_by_task(task)

def _usage_result_ok(task: DownloadTask) -> tuple[bool, int]:
    """任务是否算「下载成功」——用户口径：下载完成且成品真实保存在本机。

    只看 status == completed 不够：成品可能被同时清掉/写空（磁盘满、后处理失败）。
    这里额外要求文件此刻存在且 > 0 字节，避免把「空文件」误报成成功。
    """
    try:
        if task.status != "completed":
            return False, 0
        fp = getattr(task, "filepath", None)
        if not fp:
            return False, 0
        p = Path(fp)
        if not p.exists():
            return False, 0
        sz = p.stat().st_size
        return (sz > 0), (sz if sz > 0 else 0)
    except OSError:
        return False, 0


def _note_usage_result(task: DownloadTask, ok: bool, size: int = 0) -> None:
    """把「这次下载到底有没有成功」回写到用量账本（2026-10-20）。

    为什么需要：配额是**下单时**就扣的（那时还不知道能不能下成），所以后台
    「使用详情」原先只能看到「下载任务 3」，分不清「下了 3 个都成功」还是
    「点了 3 次全失败」。这里在任务终态把结果按 ref 补回去，管理员就能看到
    每条的真实成败（成功标准＝下载完成、文件已保存到本机）。

    幂等：同一任务只报一次（内部重试的中间失败不报，只报最终终态）。
    绝不抛异常 —— 记账问题不能影响下载本身。
    """
    if getattr(task, "_usage_reported", False):
        return
    ref = str(getattr(task, "usage_ref", "") or "")
    store = getattr(task, "_usage_store", None)
    if not ref or store is None:
        return
    task._usage_reported = True
    try:
        store.mark_usage_result(ref, bool(ok), resource="download",
                                size=int(size or 0))
    except Exception:  # noqa: BLE001
        pass


def _note_usage_result_by_task(task: DownloadTask) -> None:
    """按任务终态自动判定成功/失败并回写（终态兜底用）。"""
    ok, sz = _usage_result_ok(task)
    _note_usage_result(task, ok, sz)


def _format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    value = float(n)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024
        if value < 1024:
            return f"{value:.2f} {unit}"
    return f"{value:.2f} PB"

def _run_once(task: DownloadTask, store: TaskStore, quality_key: str, cookie: str = "", proxy: str = "", format_id: str = "", concurrent_fragments: int = 0, downloader_type: str = "", resume: bool = False, alt_protocol: bool = False) -> None:
    """执行一次下载：先解析元数据，再进入实际下载。

    把 extract_info(..., download=False) 与 process_info(info) 拆成两阶段，
    让「解析视频信息」步骤能快速收敛，且下载阶段一旦卡住就能被看门狗识别。
    A2（2026-10-02）：alt_protocol=True 时解析后尝试把格式选择切到 HLS 分片链路
    （重试策略②，见 _switch_to_hls / _retry_matrix）。
    """
    _my_gen = getattr(task, "_run_gen", 0)

    def _gen_stale() -> bool:
        """孤儿守卫：代际已变（被就地暂停 / 新一轮尝试 / resume 取代）时禁止再写终态。"""
        return getattr(task, "_run_gen", 0) != _my_gen

    effective_proxy = task.proxy or _resolve_proxy(_host_of(task.url) or "")
    task.url = _normalize_share_url(task.url, proxy=effective_proxy)
    reporter = _ProgressReporter(task, store)
    task.add_step("排队等待", "done", "已开始执行")
    task.add_step("解析视频信息", "running", f"正在解析：{task.url[:120]}…")
    info: dict[str, Any] = {}
    # 提前初始化，避免 _download_options 自身抛错时下面 except 引用 _dl_opts 触发
    # UnboundLocalError（会被兜底 except 吞掉，丢失错误分类）
    _dl_opts: dict = {}
    try:
        _dl_opts = _download_options(task, quality_key, reporter, cookie=cookie, proxy=proxy, format_id=format_id, concurrent_fragments=concurrent_fragments, downloader_type=downloader_type, resume=resume)
        # YouTube 免 Cookie 路径：下载阶段也注入 PO Token 上下文——process_info
        # 对部分格式会重新取 URL，无 pot 的请求在数据中心 IP 下被 CDN 403
        # （实测 YouTube 无 Cookie 下载卡 29.93% 后 403）。与解析阶段保持一致会话。
        _task_host = _host_of(task.url)
        _yd_vd = ""   # 解析/下载共用的 visitor_data（PO Token 会话），403 降级链也要带上
        if _task_host and _is_youtube_host(_task_host) and not (cookie or "").strip():
            try:
                _yd_vd = _fetch_youtube_visitor_data(effective_proxy) or ""
                if _yd_vd:
                    _ya = _dl_opts.setdefault("extractor_args", {}).setdefault("youtube", {})
                    _ya.setdefault("visitor_data", [_yd_vd])
                    _ya["fetch_pot"] = ["always"]
            except Exception:
                logger.debug("YouTube 下载前获取 visitor_data 失败", exc_info=True)
        # B站 经国内代理回源时，yt-dlp 原生 urllib 读取页面偶发 IncompleteRead；
        # 用 requests 预下载视频页 HTML 并注入 extractor，提高连接稳定性。
        _task_host = _host_of(task.url)
        # 直播模块已下线（2026-08-22）：下载链路同样拦截
        if _task_host and any(_lb in _task_host for _lb in ("douyu.com", "inke.cn", "cc.163.com")):
            raise ResolveError(
                "该直播平台已暂时下线",
                "直播模块（斗鱼 / 映客 / 网易CC）已于 2026-08-22 下线，恢复时间待定。",
            )
        if _task_host and ("bilibili.com" in _task_host or "b23.tv" in _task_host):
            # patch 必须带最终生效的 Cookie（含公共池自动注入），而不是用户原始输入
            _patch_bilibili_webpage_download(
                proxy=effective_proxy,
                cookie=(_dl_opts.get("http_headers") or {}).get("Cookie", cookie),
                ua=(_dl_opts.get("http_headers") or {}).get("User-Agent"),
            )
        # 搜狐：解析阶段必须去掉 format selector —— 搜狐格式没有 vcodec 标记、
        # 短边又远离标称档位（640x268 标称 360P），带 selector 会在**解析**这一步
        # 就抛 "Requested format is not available"，连信息都拿不到。
        # 档位由 _sohu_pick_format_id 在下载阶段逐片映射。
        _extract_opts = _dl_opts
        if (_task_host or "").endswith("sohu.com"):
            _extract_opts = {k: v for k, v in _dl_opts.items() if k != "format"}
            _extract_opts["noplaylist"] = False
        with _YoutubeDL(_extract_opts) as ydl:
            # 阶段 1：只解析元数据，不下载
            if _is_douyin_host(_task_host):
                # 抖音：yt-dlp 提取器已失效，直接用 VPS 解析出的真实音视频轨 URL
                # 传用户登录态：匿名解析会被 Argus 风控拦截（2026-10-27 实测）
                info = _douyin_info(task.url, cookie=cookie)
            elif "ixigua.com" in (_task_host or ""):
                # 西瓜：原生 IxiguaIE + 浏览器 Cookie（与 probe 同一路径）
                info = _ixigua_info(task.url, cookie=cookie, proxy=effective_proxy)
            elif "dazn.com" in (_task_host or ""):
                # DAZN：DRM+地区封锁，明确提示（与 probe 一致）
                raise ResolveError(
                    "DAZN 暂不支持解析",
                    "DAZN 为付费体育流媒体：内容受 Widevine DRM 保护（合规不破解），"
                    "且中国 IP / 数据中心 IP 均被 403 地区封锁。",
                )
            elif "rumble.com" in (_task_host or ""):
                # Rumble：Cloudflare 反爬，专用浏览器头接口解析直链
                info = _rumble_info(task.url)
            elif "tubitv.com" in (_task_host or ""):
                # Tubi：免费 AVOD，专用页面解析 HLS 直链（支持住宅代理）
                info = _tubi_info(task.url, "", effective_proxy)
            elif _is_kuaishou_host(_task_host):
                # 快手：同上，VPS 解析出合并 mp4 直链
                info = _kuaishou_info(task.url)
            elif "douyu.com" in (_task_host or ""):
                # 斗鱼：yt-dlp 旧提取器失效，VPS Playwright 监听流
                info = _douyu_info(task.url)
            elif "yangshipin.cn" in (_task_host or ""):
                # 央视频：播放地址需 playvinfo JSONP 签名，VPS Playwright 解析
                info = _yangshipin_info(task.url)
            elif "1905.com" in (_task_host or ""):
                # 1905：详情页反爬，VPS Playwright 解析
                info = _m1905_info(task.url)
            elif "fun.tv" in (_task_host or ""):
                # 风行：播放地址需接口签名，VPS Playwright 解析
                info = _funshion_info(task.url)
            elif "bestv.com.cn" in (_task_host or ""):
                # 百视TV：wasm 签名，VPS Playwright 解析出 m3u8 直链
                info = _bestv_info(task.url)
            elif _is_nmw_host(_task_host):
                # 金牌影院：自签 API（SHA1(MD5)）拿多清晰度 m3u8
                info = _nmw_info(task.url, proxy=effective_proxy)
            elif ("hongguoduanju.com" in (_task_host or "")) or ("novelquickapp.com" in (_task_host or "")):
                # 红果短剧：字节 CDN 流，VPS Playwright 点开播放器捕获真流直链
                info = _hongguo_info(task.url)
            elif "inke.cn" in (_task_host or ""):
                # 映客直播/回放：公开接口构造 m3u8，VPS worker 解析
                info = _inke_info(task.url)
            elif "cc.163.com" in (_task_host or ""):
                # 网易CC直播：vapi 公开接口拿签名 FLV 直链，VPS worker 解析
                info = _cc_info(task.url)
            elif _is_weibo_host(_task_host):
                # 微博：同上，VPS 解析出合并 mp4 直链
                info = _weibo_info(task.url)
            elif _is_iqiyi_host(_task_host):
                # 爱奇艺：分享页 JS-only，VPS 解析 m3u8；worker 未配置且非分享页时回退 yt-dlp
                info = _iqiyi_info(task.url, cookie=cookie)
                if info is None:
                    info = ydl.extract_info(task.url, download=False) or {}
            elif ("weixin.qq.com" in (_task_host or "")) or ("channels.weixin.qq.com" in (_task_host or "")):
                # 微信视频号：必须微信登录态，用户自带 Cookie 优先，共享池兜底
                info = _finder_info(task.url, cookie=cookie)
            elif _is_youtube_host(_task_host):
                # YouTube：自动降级（免 Cookie + PO Token → Cookie 源自动切换）
                info = _resolve_youtube(task.url, user_cookie=cookie, proxy=effective_proxy)
            else:
                try:
                    info = ydl.extract_info(task.url, download=False) or {}
                except (DownloadError, ExtractorError) as _exc:
                    # B站 页面风控（412/403，2026-08 B站对非浏览器 TLS 栈返回 412 验证页）
                    # 或网络错误 → 下载阶段同样走 API 兜底（api.bilibili.com 不过页面风控）
                    _low = str(_exc).lower()
                    if _task_host and ("bilibili.com" in _task_host or "b23.tv" in _task_host) and (
                        "412" in _low or "403" in _low or "forbidden" in _low
                        or "precondition" in _low or "incompleteread" in _low
                        or "connection" in _low
                    ):
                        task.log("B站 页面被风控拦截，改用 API 直连解析…")
                        info = _bilibili_api_extract(
                            task.url,
                            proxy=effective_proxy,
                            cookie=(_dl_opts.get("http_headers") or {}).get("Cookie", cookie),
                        ) or {}
                    else:
                        raise
            # 搜狐剧集：SohuIE 返回 multi_video（一集切成 N 片），必须聚合后再
            # 交给 _sohu_download_parts 逐片下载 + concat，否则只会下到第一片
            # 9 秒片头（2026-10-25 用户实测「搜狐解析器要重写」的根因）。
            if _sohu_is_multi(info) and (_host_of(task.url) or "").endswith("sohu.com"):
                info = _sohu_aggregate(info)
            if info.get("webpage_url"):
                task.source_url = info["webpage_url"]
            if info.get("title"):
                store.update(task.id, title=info["title"])
                task.add_step("解析视频信息", "done", f"已获取标题《{info['title']}》")
            else:
                task.add_step("解析视频信息", "done", "未获取到标题")

            # 直链 m3u8 协议归一化：bestv/inke/douyu 等 worker 返回的 info dict
            # protocol=https + m3u8 URL，yt-dlp determine_protocol 会沿用 dict 里
            # 已有的 protocol（不重新看 URL）→ 用 HttpFD 把 HLS 播放清单文本当文件
            # 下载。统一按 URL 修正为 m3u8_native（走 HLS 下载器分段拉流出 mp4，
            # 与 _iqiyi_info 的成熟路径一致）。
            # ⚠️ ext 保持 mp4（勿改成 m3u8）：否则 outtmpl 产出 .m3u8 扩展名的文件
            # （内容实为 TS 流），用户拿到 .m3u8 会误以为下载失败。
            if _is_hls_url(info.get("url") or ""):
                _proto = (info.get("protocol") or "").split("+")[0].lower()
                if _proto in ("http", "https", ""):
                    info["protocol"] = "m3u8_native"
                    if not info.get("ext") or info["ext"] in ("mp4", "m3u8"):
                        info["ext"] = "mp4"

            # A2 重试策略②：改用 HLS 分片链路（仅当 formats 里真有 HLS 视频轨且当前
            # 选中的不是 HLS 时才会真正切换；没有可用 HLS 则保持原选择按原样重试）
            if alt_protocol:
                _sw = _switch_to_hls(info, quality_key)
                if _sw:
                    task.log("重试策略：本次改用 HLS 分片链路下载")
                    task.add_step("下载音视频", "running", "切换 HLS 分片链路重试…")
                else:
                    task.log("重试策略：无可用 HLS 视频轨，按原方式重试")

            # 阶段 2：真正开始下载；先把状态置为 downloading，看门狗才能生效
            # 开下之前先预检：体积上限 / 磁盘余量不够直接给人话错误（B1，2026-10-02），
            # 别让任务闷头下到 90% 才 ENOSPC，或被 max-filesize 静默跳过。
            _preflight_space_check(task, info)
            task.add_step("下载音视频", "running", f"已选清晰度：{task.quality}")
            task.log(f"开始下载：{task.quality}")
            store.update(task.id, status="downloading", progress=0.0)

            # 记录本次 yt-dlp 实际选中的格式 ID（用于 403 降级时向用户说明原因）
            _sel_parts: list[str] = []
            if info.get("requested_formats"):
                _sel_parts = [str(f.get("format_id", "?")) for f in info["requested_formats"]]
            elif info.get("format_id"):
                _sel_parts = [str(info["format_id"])]
            _sel_fmt = "+".join(_sel_parts) or "未知"

            # B站 API 兜底 info 固定选了 best（720P 等），按用户选择的清晰度重建
            # requested_formats，否则用户选 480P/360P 实际也会下成 720P。
            if info.get("extractor") == "bilibili" and info.get("requested_formats"):
                _rebuild_requested_formats(info, quality_key)

            # YouTube 403 自动降级：部分 client 的格式 ID
            # （如 AV1 400/39x、部分 H.264 298/18）URL 被 Google CDN 拒绝，
            # 捕获后自动换 client 与已知可用格式重试，用户无感知。
            try:
                if info.get("_vdl_parts"):
                    # 搜狐多分片：逐段下载 + ffmpeg concat（进度由 reporter 按文件名累加）
                    _sohu_download_parts(task, info, _dl_opts, reporter, quality_key)
                else:
                    ydl.process_info(info)
            except (DownloadError, ExtractorError) as _exc:
                _err_str = str(_exc)
                _is_403 = "403" in _err_str or "Forbidden" in _err_str
                _yt_host = _host_of(task.url)
                _is_yt = _yt_host and ("youtube.com" in _yt_host or "youtu.be" in _yt_host)
                if _is_403 and _is_yt:
                    task.log(
                        f"YouTube 格式 {_sel_fmt} 的下载地址被 CDN 拒绝(403，"
                        f"URL 绑定的出口 IP / 签名不匹配)，自动降级重试…"
                    )
                    task.add_step(
                        "下载音视频", "running",
                        f"格式 {_sel_fmt} 被 YouTube 拒绝，自动切换兼容格式…",
                    )
                    # 多轮降级：依次尝试不同格式链，优先 H.264(avc1) 编码
                    # （最不易被 CDN 拒绝），再放宽到 VP9/AV1、降低分辨率，
                    # 但**始终不许超过用户选的清晰度**。每一轮独立 try，直到成功或穷尽所有链。
                    _fb_base = _download_options(
                        task, quality_key, reporter, cookie=cookie, proxy=proxy,
                        format_id=format_id, concurrent_fragments=concurrent_fragments,
                        downloader_type=downloader_type, resume=resume,
                    )
                    # 全部受请求档封顶（末环曾是无上限的 `bv*+ba`，会把 1080P 请求
                    # 静默升级成 1440P/4K —— 2026-10-21 实事故，见 _yt_fallback_chains）
                    _fallback_chains = _yt_fallback_chains(quality_key)
                    # 2026-08-22 实测（task logs 确认）：数据中心 IP 下
                    #   ios    → Requested format is not available（SABR 无格式）
                    #   tv     → The page needs to be reloaded
                    #   android → ✅ 直连 mp4 对云 IP 放行，成功
                    #   tv_embedded → 解析 OK 但下载分片 403
                    # 2026-09-21 复测（yt-dlp 2026.08.19 + bgutil 2.0.0，数据中心 IP）：
                    #   android / tv_embedded → 分片一律 403（bgutil 的 gvs PO Token
                    #   按 client 生成，与 android 不匹配；tv_embedded 已被 yt-dlp 移除）
                    #   web_safari → ✅ 与 PO Token 同 client，实测可正常下载
                    # 2026-09-26 复测（用户 VPN 出口 + yt-dlp 2026.08.19，SABR 新政后）：
                    #   web_safari/mweb/web/tv → 解析即抛 "Requested format is not available"
                    #   不传 player_client（yt-dlp 默认客户端链）→ ✅ 31 个格式解析成功
                    # 故把「默认客户端」排到第一位；"(default)" 表示不覆盖 player_client。
                    _yt_clients = ["(default)", "web_safari", "android", "ios", "tv"]
                    _done = False
                    for _client in _yt_clients:
                        if task.cancel_requested:
                            task.log("用户取消下载，停止 YouTube 降级尝试")
                            break
                        for _chain in _fallback_chains:
                            if task.cancel_requested:
                                break
                            try:
                                _fb_opts = dict(_fb_base)
                                _fb_opts["format"] = _chain
                                _ya = _fb_opts.setdefault("extractor_args", {}).setdefault("youtube", {})
                                if _client != "(default)":
                                    _ya["player_client"] = [_client]
                                # 免 Cookie 时保持 PO Token 会话（A1，2026-10-02）：
                                # _fb_base 是新构建的，会丢掉首试注入的 visitor_data——
                                # 没有同一 visitor 会话，pot 与会话不匹配照样 403。
                                if not (cookie or "").strip():
                                    _ya.setdefault("fetch_pot", ["always"])
                                    if _yd_vd:
                                        _ya["visitor_data"] = [_yd_vd]
                                with _YoutubeDL(_fb_opts) as _ydl2:
                                    info = _ydl2.extract_info(task.url, download=False) or info
                                    if info.get("title"):
                                        store.update(task.id, title=info["title"])
                                    _ydl2.process_info(info)
                                _done = True
                                task.log(f"已用 client={_client} 格式链 {_chain} 完成下载")
                                break
                            except (DownloadError, ExtractorError) as _e2:
                                _e2_str = str(_e2)
                                if "403" in _e2_str or "Forbidden" in _e2_str:
                                    task.log(f"client={_client} 链 {_chain} 仍被拒绝，继续…")
                                    continue
                                # 非 403 错误（格式不存在等）换下一个 client/链
                                task.log(f"client={_client} 链 {_chain} 失败({_e2_str[:60]})，继续…")
                                continue
                        if _done:
                            break
                    if not _done:
                        # 所有格式链都 403：极可能是代理出口 IP 不一致
                        _eff = proxy or _resolve_proxy(_yt_host)
                        _proxy_note = (
                            "（当前生效代理：%s；双击 .app 不继承终端代理，"
                            "请确认 Clash/V2Ray 已开启「系统代理」或 TUN 模式。"
                            "若本机 IP 被 YouTube 限流，可配置 VDL_PROXY_YT 环境变量或"
                            " ~/.video-downloader/proxy.json 的 youtube 键，让 YouTube 独立走代理）"
                            % (_eff or "无，直连")
                        )
                        task.log("YouTube 所有兼容格式均被 CDN 拒绝，疑似代理出口 IP 不匹配" + _proxy_note)
                        raise DownloadError(
                            "YouTube 下载被 CDN 全面拒绝(403)：请检查代理设置后重试" + _proxy_note
                        )
                else:
                    raise

            output = _locate_output(info, task.workdir or Path("."))
            # 无声防御（2026-08-23）：成品音轨校验 + 补救（纯音频任务跳过）
            if task.quality_key not in (AUDIO_KEY, M4A_KEY):
                output = _ensure_audio_track(output, info, task)
            _write_sidecar(output, task, info)
    except DownloadPaused:
        if _gen_stale():
            return
        task.add_step("下载音视频", "done", "已暂停（可继续下载）")
        task.log("用户暂停下载")
        store.update(task.id, status="paused",
                     progress=task.progress, downloaded_bytes=task.downloaded_bytes)
        # 保留 .part 文件，不清除——后续继续时 yt-dlp 断点续传
    except DownloadCanceled:
        if _gen_stale():
            return
        # A3（2026-10-02）：看门狗停滞 / 硬超时的系统终止此前伪装成「用户已取消」，
        # 用户看到 canceled「已取消下载」一头雾水。按 cancel_reason 分流终态。
        # 实测 F2（2026-10-11）：逻辑抽到 `_finalize_terminated`，与主等待循环共用同一份，
        # 避免「子线程阻塞时代码路径根本走不到这里」导致任务僵在 downloading。
        _finalize_terminated(task, store)
    except (UnsupportedError, GeoRestrictedError, ExtractorError, DownloadError) as exc:
        if _gen_stale():
            return
        err = _friendly_error(exc, _build_diag_context(task.url, cookie=cookie, proxy=proxy, options=_dl_opts))
        _mark_step_error(task, err.message)
        # 下载中断类失败（网络抖动/限速假死）往往残留部分分片，标记可续传
        store.update(task.id, status="failed", error=err.message, hint=err.hint,
                     category=getattr(err, "category", None) or "unknown",
                     resumable=_has_partial(task.workdir))
    except (OSError, ResolveError) as exc:
        if _gen_stale():
            return
        message = getattr(exc, "message", None) or "下载过程中出现错误"
        # OSError 多为网络层错误（DNS/连接/超时），归为 network 便于前端给「重试」建议；
        # ResolveError 自带 category（如优酷 -3007 的 cookie_required），优先采用
        cat = getattr(exc, "category", None)
        if not cat:
            cat = "network" if isinstance(exc, OSError) else "unknown"
        if isinstance(exc, OSError) and ("no space left" in str(exc).lower() or getattr(exc, "errno", None) == 28):
            # 下载中途磁盘满：预检（_preflight_space_check）拦不到的缺口（如 HLS 体积未知）
            message = "磁盘空间不足，下载中断"
            cat = "disk_full"
            task.log(f"ENOSPC：{exc}")
        _mark_step_error(task, message)
        store.update(task.id, status="failed", error=message, hint=_clean_message(str(exc)),
                     category=cat, resumable=_has_partial(task.workdir))
    except Exception as exc:  # noqa: BLE001 - 兜底，保证任务状态一定收敛
        if _gen_stale():
            return
        logger.exception("下载任务 %s 未预期失败", task.id)
        _mark_step_error(task, "未预期错误")
        store.update(task.id, status="failed", error="下载失败", hint=_clean_message(str(exc)),
                     category="unknown", resumable=_has_partial(task.workdir))
    else:
        if task.cancel_requested or task.is_finished or _gen_stale():
            # 已被看门狗/硬超时/用户终止（或已被就地暂停、新一轮接手），不再写完成态
            return
        # 可选：下载完成后提取文案（口播/简介），失败不影响下载完成态
        if task.extract_mode:
            _run_extraction(task, store, output, info, cookie, proxy)
        task.add_step("合并与后处理", "done", f"输出文件：{output.name}")
        task.add_step("下载完成", "done", f"文件大小：{_format_bytes(output.stat().st_size)}")
        task.log(f"下载完成：{output.name}")
        store.update(
            task.id,
            status="completed",
            progress=100.0,
            filepath=output,
            filename=output.name,
            filesize=output.stat().st_size,
            speed=0.0,
            eta=0,
        )

def run_remote_download(task: DownloadTask, store: TaskStore, quality_key: str = "", cookie: str = "",
                        proxy: str = "", max_retries: int = 0, format_id: str = "",
                        concurrent_fragments: int = 0, downloader_type: str = "", resume: bool = False) -> None:
    """海外站点下载：把任务整个交给对端（海外）节点执行，再把成品回传本机。

    背景：桌面端跑在国内、本机没有出网链路时，YouTube 等海外站直连必然解析/下载超时。
    前端 baseFor() 已经会把海外链接转发给对端，但那依赖 /api/nodes 请求成功
    （跨境链路实测约 20% 概率失败），一旦失败前端拿不到 peer 就退回本机 → 超时。
    本函数是**后端兜底**：只要声明了 VDL_PEER_ENDPOINT，海外平台一律交给对端执行，
    对端不可达才报错。本机因此完全不需要翻墙链路。

    签名与 run_download 保持一致，供 scheduler 直接替换。
    """
    import re as _re
    import tempfile as _tempfile
    from urllib.parse import unquote as _unquote
    try:
        import requests as _rq_mod
    except Exception:  # noqa: BLE001
        _rq_mod = None
    _rq = None
    if _rq_mod is not None:
        # 换成 Session 并关闭 trust_env：桌面端进程常继承 Clash/系统代理，
        # 若不禁用，访问自家节点会被代理误拦（与 _call_vps_worker 同理）。
        # 绑定回 _rq 后下面所有 _rq.post/get/head 自动复用同一会话（回传大文件也更快）。
        _rq = _rq_mod.Session()
        _rq.trust_env = False

    peer = (os.environ.get("VDL_PEER_ENDPOINT") or "").strip().rstrip("/")
    if not peer or _rq is None:
        store.update(task.id, status="failed", error="海外节点不可用",
                     hint="未配置海外节点（VDL_PEER_ENDPOINT），无法下载海外视频",
                     category="network")
        _note_usage_result(task, False)   # 2026-10-20：失败结果补回账本
        return

    def _fail(err: str, hint: str) -> None:
        store.update(task.id, status="failed", error=err, hint=_clean_message(hint)[:300],
                     category="network")
        _note_usage_result(task, False)   # 2026-10-20：失败结果补回账本

    task.add_step("提交到海外节点", "running", peer)
    try:
        resp = _rq.post(peer + "/api/download",
                        json={"url": task.url, "quality": quality_key or BEST_KEY,
                              # 🔴 必须把登录态带给对端（cookie 参数 ← create_download 已注入
                              # 本机浏览器解密结果）：写死空串会让对端误报「需要登录 Cookie」。
                              "title": task.title or "", "cookie": cookie or task.cookie or "", "proxy": "",
                              "format_id": format_id or "",
                              "extract_script": task.extract_mode or ""},
                        timeout=60)
        if resp.status_code != 200:
            raise RuntimeError("HTTP %s" % resp.status_code)
        cloud_id = str((resp.json() or {}).get("task_id") or "")
    except Exception as exc:  # noqa: BLE001
        _fail("提交到海外节点失败", str(exc))
        return
    if not cloud_id:
        _fail("海外节点未返回任务 ID", "")
        return

    task.log("已提交到海外节点：%s" % cloud_id)
    store.update(task.id, status="downloading", progress=0.0)

    # 轮询对端进度。⚠️ 对端任务完成后详情接口可能返回空（保留策略会清理），
    # 因此不能只依赖状态接口：状态不可读时改用 HEAD 探测成品文件是否就绪。
    file_url = "%s/api/tasks/%s/file" % (peer, cloud_id)
    status_url = "%s/api/tasks/%s" % (peer, cloud_id)
    ready = False
    for _ in range(600):                      # 最长约 30 分钟
        if task.cancel_requested or task.is_finished:
            try:
                _rq.post("%s/api/tasks/%s/cancel" % (peer, cloud_id), timeout=15)
            except Exception:  # noqa: BLE001
                pass
            return
        st: dict[str, Any] = {}
        try:
            r = _rq.get(status_url, timeout=25)
            if r.status_code == 200:
                st = r.json() or {}
        except Exception:  # noqa: BLE001
            st = {}
        status = str(st.get("status") or "")
        if status == "completed":
            ready = True
            break
        if status in ("failed", "error", "canceled"):
            _fail("海外节点下载失败", str(st.get("error") or st.get("hint") or status))
            return
        if status:
            _title = str(st.get("title") or "")
            if _title and not task.title:
                store.update(task.id, title=_title)
            try:
                store.update(task.id, status="downloading",
                             progress=min(99.0, float(st.get("progress") or 0)))
            except Exception:  # noqa: BLE001
                pass
        else:
            # 状态接口不可用 → 直接探测成品文件是否就绪
            try:
                h = _rq.head(file_url, timeout=25, allow_redirects=True)
                if h.status_code == 200 and int(h.headers.get("Content-Length") or 0) > 0:
                    ready = True
                    break
            except Exception:  # noqa: BLE001
                pass
        time.sleep(3)

    if not ready:
        _fail("海外节点下载超时", "30 分钟内未完成")
        return

    # ---- 回传成品到本机 ----
    workdir = Path(task.workdir) if task.workdir else None
    if workdir is None:
        workdir = Path(_tempfile.mkdtemp(prefix="vdl_remote_"))
        task.workdir = workdir
    workdir.mkdir(parents=True, exist_ok=True)

    def _safe_name(raw: str) -> str:
        raw = (raw or "").strip().strip('"')
        # Content-Disposition 里可能是 filename*=utf-8''xxx（Starlette 用小写），
        # 大小写不敏感地剥掉这个编码前缀，否则文件名会变成 utf-8''xxx.mp4 这种垃圾名。
        raw = _re.sub(r"^utf-8''", "", raw, flags=_re.I)
        raw = raw.replace("\\", "_").replace("/", "_").replace(":", "_")
        for ch in ('*', '?', '"', '<', '>', '|'):
            raw = raw.replace(ch, "_")
        return raw or "video.mp4"

    out = workdir / _safe_name(task.title or "video") 
    if not out.suffix:
        out = out.with_suffix(".mp4")

    task.add_step("回传文件到本机", "running", "")
    # ⚠️ 跨境回传不能走「一条长流」：实测 84MB 的文件在 33MB 处被对端/CDN 掐断，
    # 报 IncompleteRead(33087479 bytes read, 51372525 more expected)，整单直接失败——
    # 前面几十 MB 全白下。改成 **分片 Range 下载**：每片独立请求、独立超时、失败只重试该片，
    # 已下的片写盘即固化，链路抖动最多拖慢、不会整单作废。
    CHUNK = 4 * 1024 * 1024

    def _name_from_headers(resp) -> None:
        nonlocal out
        cd = resp.headers.get("Content-Disposition") or ""
        m = _re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd, flags=_re.I)
        if m:
            nm = _safe_name(_unquote(m.group(1)))
            if nm:
                out = workdir / nm

    wrote_bytes = [0]   # 真正写入的字节数（预分配后 st_size 恒等于 total，完成度只能靠它判断）
    try:
        # 探测请求：只取 1 字节，顺带拿到总长度（Content-Range: bytes 0-0/TOTAL）与真实文件名
        r0 = _rq.get(file_url, headers={"Range": "bytes=0-0"}, timeout=(15, 90))
        if r0.status_code not in (200, 206):
            raise RuntimeError("HTTP %s" % r0.status_code)
        _name_from_headers(r0)
        total = 0
        mm = _re.search(r"/(\d+)\s*$", (r0.headers.get("Content-Range") or "").strip())
        if mm:
            total = int(mm.group(1))
        if not total:
            total = int(r0.headers.get("Content-Length") or 0)
        if r0.status_code == 206 and total > 0:
            # 支持 Range 且拿到总长 → **并行分片**回传。
            # 实测跨境单连接只有 100~900KB/s 且会被中途掐断；多连接并行能把带宽吃满，
            # 单片失败只重试该片，已落盘的片不回退。
            import threading as _threading
            from concurrent.futures import ThreadPoolExecutor

            with out.open("wb") as _fh:
                _fh.truncate(total)          # 预分配，各线程按偏移写入不会互相覆盖
            _wlock = _threading.Lock()
            ranges = [(s, min(s + CHUNK, total) - 1) for s in range(0, total, CHUNK)]
            # 并发度：跨境链路单连接吞吐低且波动大（实测 0.1~1MB/s），
            # 8 并发能把总量顶到链路天花板；小文件 2 并发足够，别浪费建连开销。
            workers = 8 if total > 8 * 1024 * 1024 else 2

            def _fetch(rng) -> int:
                s, e = rng
                want = e - s + 1
                last_err = ""
                # requests.Session 非线程安全 → 每个线程独立建会话
                sess = _rq_mod.Session() if _rq_mod is not None else None
                if sess is not None:
                    sess.trust_env = False
                for attempt in range(4):
                    if task.cancel_requested:
                        return 0
                    try:
                        rr = (sess or _rq).get(file_url,
                                               headers={"Range": "bytes=%d-%d" % (s, e)},
                                               timeout=(15, 120))
                        if rr.status_code == 206:
                            data = rr.content or b""
                            if not data:
                                last_err = "空响应"
                            elif len(data) != want:
                                last_err = "长度不符(%d/%d)" % (len(data), want)
                            else:
                                with _wlock:
                                    with out.open("r+b") as fh:
                                        fh.seek(s)
                                        fh.write(data)
                                return len(data)
                        else:
                            last_err = "HTTP %s" % rr.status_code
                    except Exception as exc:  # noqa: BLE001
                        last_err = str(exc)
                    time.sleep(min(2 + attempt * 3, 10))
                raise RuntimeError("分片 %d-%d 回传失败：%s" % (s, e, last_err[:120]))

            got_total = 0
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = [ex.submit(_fetch, r) for r in ranges]
                for fu in futs:
                    got_total += fu.result()   # 任一片最终失败 → 在此抛出
                    wrote_bytes[0] = got_total
                    if total:
                        store.update(task.id,
                                     progress=min(99.0, got_total * 100.0 / total))
        else:
            # 对端不支持 Range / 拿不到总长 → 退化成一次性整包（老行为）
            _n = 0
            with out.open("wb") as fh:
                for chunk in r0.iter_content(chunk_size=512 * 1024):
                    if not chunk:
                        continue
                    if task.cancel_requested:
                        return
                    fh.write(chunk)
                    _n += len(chunk)
            wrote_bytes[0] = _n
    except Exception as exc:  # noqa: BLE001
        _fail("回传文件失败", str(exc))
        return

    if not out.exists() or out.stat().st_size <= 0:
        _fail("回传文件为空", "")
        return
    # 长度校验：分片写盘是按偏移 seek + 预分配的，缺片时文件大小**仍是满的**（空洞），
    # 所以只能按「实际写入字节数」判定；宁可报错也不要把打不开的文件当用户成果。
    _expect = total or 0
    _wrote = (wrote_bytes[0] if _expect else out.stat().st_size)
    if _expect and _wrote != _expect:
        _fail("回传文件不完整",
              "期望 %s，实际写入 %s（分片有缺失）" % (_format_bytes(_expect), _format_bytes(_wrote)))
        return

    task.add_step("下载完成", "done", "文件大小：%s" % _format_bytes(out.stat().st_size))
    task.log("海外节点下载完成：%s" % out.name)
    store.update(task.id, status="completed", progress=100.0, filepath=out,
                 filename=out.name, filesize=out.stat().st_size, speed=0.0, eta=0)
    # 2026-10-20：海外回传落盘成功 = 这次下载真的成功（成品已在本机）
    _note_usage_result_by_task(task)


def _run_extraction(task: DownloadTask, store: TaskStore, output: Path, info: dict[str, Any],
                    cookie: str = "", proxy: str = "", mode: str | None = None) -> None:
    """在后台线程里执行文案提取，结果写回任务。任何失败都降级处理，不影响下载完成态。

    mode 为 None 时沿用 task.extract_mode；info 为 None 时（重提取场景）改用 task.source_url。
    """
    mode = mode or task.extract_mode
    if mode not in ("spoken", "description", "both"):
        return
    source_url = (info.get("webpage_url") if info else None) or task.source_url or task.url
    task.add_step("提取文案", "running", "正在提取文案…")
    store.update(task.id, extract_status="running")
    workdir = task.workdir

    def progress_cb(stage: str, detail: str) -> None:
        task.add_step("提取文案", "running", detail)

    try:
        from extract_text import extract_all
        result = extract_all(
            str(output), source_url=source_url, cookie=cookie, proxy=proxy,
            mode=mode, workdir=workdir, progress_cb=progress_cb,
        )
        task.extracted_text = result
        task.extract_status = "done"
        task.add_step("提取文案", "done", "文案提取完成")
        store.update(task.id, extracted_text=result, extract_status="done")
    except Exception as exc:  # noqa: BLE001 - 文案提取失败绝不能拖垮下载任务
        logger.exception("提取文案失败 task=%s", task.id)
        err = {"error": str(exc)[:300]}
        task.extracted_text = err
        task.extract_status = "error"
        task.add_step("提取文案", "error", f"文案提取失败：{str(exc)[:120]}")
        store.update(task.id, extracted_text=err, extract_status="error")

def _is_retryable(error: str) -> bool:
    """判断失败原因是否值得自动重试：网络/超时/连接/代理/临时服务端错误可重试，
    会员受限、链接失效等应直接失败，避免无效刷带宽。"""
    if not error:
        return True
    lowered = error.lower()
    keywords = (
        "超时", "timeout", "连接", "connection", "网络", "network", "resolve",
        "代理", "proxy", "ssl", "reset", "refused", "unreachable", "中断",
        "interrupted", "503", "502", "500", "429", "temporary", "temp",
        "ffmpeg", "m3u8", "hls",  # HLS 合并偶发，ffmpeg 偶发退出 → 自动重试
    )
    return any(k in lowered for k in keywords)

def _is_hls_url(url: str) -> bool:
    """粗略判断是否为 HLS 播放清单地址。"""
    return bool(url) and (".m3u8" in url or url.rstrip().endswith(".m3u8"))

def _detect_play_url(info: dict[str, Any]) -> tuple[str | None, bool]:
    """返回适合「在线观看」的播放地址与是否为 HLS。

    HLS 优先（腾讯等站原生就是 m3u8 流，浏览器可经后端代理播放）；
    否则退回 MP4 直链。返回 (url, is_hls)。
    """
    formats = [f for f in (info.get("formats") or []) if isinstance(f, dict)]
    # 1) 从 formats 里挑分辨率最高的 HLS 流
    cands: list[tuple[int, str]] = []
    for f in formats:
        u = f.get("url") or f.get("manifest_url") or ""
        if not u:
            continue
        proto = (f.get("protocol") or "").split("+")[0].lower()
        is_hls = proto in ("m3u8", "m3u8_native") or _is_hls_url(u)
        if is_hls:
            cands.append((int(f.get("height") or 0), u))
    if cands:
        cands.sort(key=lambda x: x[0], reverse=True)
        return cands[0][1], True
    # 2) 合并 info 本身的 url 若是 HLS
    u = info.get("url") or ""
    if _is_hls_url(u):
        return u, True
    # 3) 普通 MP4 直链（info.direct 标记）
    du = _detect_direct_url(info)
    if du:
        return du, False
    # 3b) 从 formats 中挑最高分辨率非 HLS 直链（覆盖 info.direct=False 的站点）
    #     优先 H.264(avc1) —— macOS WKWebView 不支持 AV1/VP9，选了会导致黑屏
    prog_cands: list[tuple[int, int, int, str]] = []  # (is_avc1, height, tbr, url)
    for f in formats:
        u = f.get("url") or ""
        if not u:
            continue
        proto = (f.get("protocol") or "").split("+")[0].lower()
        if proto in ("m3u8", "m3u8_native") or _is_hls_url(u):
            continue
        h = int(f.get("height") or 0)
        if h:
            tbr = float(f.get("tbr") or 0) or 0.0
            vc = (f.get("vcodec") or "").lower()
            is_avc1 = 1 if ("avc1" in vc or "h264" in vc or "avc" in vc) else 0
            prog_cands.append((is_avc1, h, tbr, u))
    if prog_cands:
        # 先按是否 H.264 降序（H.264 优先），同编码按 height 降序，同 height 按 tbr 降序
        prog_cands.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
        return prog_cands[0][3], False
    # 3c) 兜底：无 height 的渐进式直链（BitChute 等 _check_format 只返回 url+filesize，
    #     没有 height/protocol 字段，会全部漏过上面 3b 的高度分桶）
    for f in formats:
        u = f.get("url") or ""
        if not u:
            continue
        proto = (f.get("protocol") or "").split("+")[0].lower()
        if proto in ("m3u8", "m3u8_native") or _is_hls_url(u):
            continue
        return u, False
    return None, False

def build_watch_options(info: dict[str, Any]) -> list[dict[str, Any]]:
    """为「在线观看」生成可选清晰度列表（每个清晰度对应一个可直接播放的地址）。

    - 按清晰度（height）去重：同一分辨率下平台常给出多个 format（不同码率/音轨/CDN），
      只保留码率最高、可播放的一个 url，避免下拉出现重复项；
    - 同时覆盖两类可播源：HLS 直播清单（m3u8）与渐进式直链（MP4/WebM，可在 <video> 直接播）；
    - 所有视频清晰度共用同一 HLS url 时，合并为「自动（源站自适应）」；
    - 既无 HLS 又有 MP4 直链时，补一个 MP4 直链选项。
    保证返回的每个 url 都能直接交给后端 /api/stream/proxy 代理播放。
    """
    formats = [f for f in (info.get("formats") or []) if isinstance(f, dict)]
    by_height: dict[int, dict[str, Any]] = {}   # height -> HLS 最佳可播放项
    prog: dict[int, dict[str, Any]] = {}         # height -> 渐进式直链最佳项
    audio: dict[str, Any] | None = None          # 纯音频 HLS（无 height）
    prog_unknown: dict[str, Any] | None = None   # 无 height 的渐进式直链兜底
    for f in formats:
        u = (f.get("url") or f.get("manifest_url") or "").strip()
        if not u:
            continue
        proto = (f.get("protocol") or "").split("+")[0].lower()
        is_hls = proto in ("m3u8", "m3u8_native") or _is_hls_url(u)
        height = int(f.get("height") or 0)
        note = (f.get("format_note") or "").strip()
        fid = f.get("format_id") or ""
        tbr = float(f.get("tbr") or 0) or 0.0
        item = {"url": u, "note": note, "fid": fid, "tbr": tbr}
        if is_hls:
            if height:
                cur = by_height.get(height)
                if cur is None or tbr > cur["tbr"]:
                    by_height[height] = item
            elif audio is None:
                audio = item
        else:
            # 渐进式直链：放宽过滤——只要有 url + height 就纳入观看选项。
            # 很多第三方提取器 ext/proto 字段不规范；播放失败由前端 onerror 兜底。
            # 同分辨率优先 H.264(avc1) —— WKWebView 不支持 AV1/VP9
            if height:
                cur = prog.get(height)
                vc = (f.get("vcodec") or "").lower()
                is_avc1 = ("avc1" in vc or "h264" in vc or "avc" in vc)
                cur_vc = (cur.get("vcodec") or "") if cur else ""
                cur_is_avc1 = ("avc1" in cur_vc or "h264" in cur_vc or "avc" in cur_vc) if cur else False
                # 优先 H.264；同编码选高码率
                if cur is None or (is_avc1 and not cur_is_avc1) or (is_avc1 == cur_is_avc1 and tbr > cur["tbr"]):
                    item["_vcodec"] = vc  # 保留编码信息供调试
                    prog[height] = item
            else:
                # 无 height 直链（BitChute 等）：保留第一个作兜底
                if prog_unknown is None:
                    prog_unknown = item

    # 合并：同清晰度优先 HLS（自适应更好），无 HLS 才用渐进式直链
    merged: dict[int, tuple[bool, dict[str, Any]]] = {}
    for h, v in by_height.items():
        merged[h] = (True, v)          # (is_hls, item)
    for h, v in prog.items():
        merged.setdefault(h, (False, v))

    opts: list[dict[str, Any]] = []
    video_urls = {v["url"] for v in by_height.values()}
    if len(video_urls) == 1 and by_height:
        # 所有视频清晰度共用同一清单：源站按带宽自适应，无需手动选
        url = next(iter(video_urls))
        tag = f"{max(by_height)}P"
        opts.append({"key": "auto", "label": f"自动（源站自适应） · {tag}",
                     "url": url, "format_id": "", "is_hls": True})
    else:
        for height in sorted(merged, reverse=True):
            is_hls, v = merged[height]
            label = f"{height}P"
            note = v["note"]
            if note and str(height) not in note and note.lower() not in ("hls", "m3u8"):
                label = f"{label} · {note}"
            if not is_hls:
                label += " · MP4"
            opts.append({
                "key": str(height),
                "label": label,
                "url": v["url"],
                "format_id": v["fid"],
                "is_hls": is_hls,
            })

    # 无视频流但有纯音频 HLS 时，单列一个音频选项
    if not opts and audio:
        opts.append({"key": "audio", "label": audio["note"] or "音频",
                     "url": audio["url"], "format_id": "", "is_hls": True})

    # 无 height 的渐进式直链兜底（BitChute 等 _check_format 只给 url+filesize）
    if not opts and prog_unknown:
        _pu_url = prog_unknown["url"]
        _pu_ext = (Path(urlparse(_pu_url).path).suffix or "").lstrip(".").lower()
        _pu_label = f"直链 · {_pu_ext.upper()}" if _pu_ext else "直链"
        opts.append({"key": "direct", "label": _pu_label,
                     "url": _pu_url, "format_id": "", "is_hls": False})

    if not opts:
        # HLS 源兜底：部分平台只设 info.url 为 m3u8（不填充 formats），
        # 如 Rumble 的 hls-vod 路径——此时给出「自动（HLS）」观看选项
        _iu = info.get("url") or ""
        if _is_hls_url(_iu):
            opts.append({"key": "auto", "label": "自动（HLS）",
                         "url": _iu, "format_id": "", "is_hls": True})
            return opts
        du = _detect_direct_url(info)
        if du:
            # 按真实扩展名标注标签（f4v/flv 直链不该标成 "MP4 直链"）
            _du_ext = (Path(urlparse(du).path).suffix or "").lstrip(".").lower()
            _du_ext = _du_ext or (info.get("ext") or "").lower()
            _du_label = {"flv": "FLV 直链", "f4v": "FLV 直链",
                         "m3u8": "HLS 直播流", "mp4": "MP4 直链"}.get(_du_ext, "直链")
            opts.append({"key": _du_ext or "mp4", "label": _du_label,
                         "url": du, "format_id": "", "is_hls": _du_ext == "m3u8"})
    return opts

def _extract_page_title(html: str) -> str:
    """从优酷页面 HTML 抽取剧名候选标题。

    优先 <title>；回退 <meta property="og:title">；再回退 JSON-LD 的 name/title。
    """
    if not html:
        return ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if m:
        t = m.group(1).strip()
        if t:
            return t
    m = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']',
        html, re.IGNORECASE | re.DOTALL,
    )
    if m:
        t = m.group(1).strip()
        if t:
            return t
    for lm in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.IGNORECASE | re.DOTALL,
    ):
        try:
            data = json.loads(lm.group(1))
        except Exception:
            continue
        name = data.get("name") or data.get("title")
        if isinstance(name, str) and name.strip():
            return name.strip()
        items = data.get("itemListElement")
        if isinstance(items, list) and items:
            n = items[0].get("name")
            if isinstance(n, str) and n.strip():
                return n.strip()
    return ""

def _parse_series_from_title(raw: str, title: str, is_show_page: bool = False) -> str:
    """从优酷网页标题里切出整部剧名。

    raw 形如：
      "神墓 辰南觉醒 第1话 我自远古来-动漫-高清完整正版视频在线观看-优酷"
    末尾 "-优酷"（可能带"视频"）之前还夹着站点描述，需整体剥离后再定位单集标题。

    is_show_page=True 时表示这是 show_page 总页（标题只有剧名、无单集标题），
    直接取首个描述分隔符前的内容作为剧名。
    """
    if not raw:
        return ""
    body = re.split(r"[-_|｜]\s*优酷", raw)[0].strip()
    if is_show_page:
        # 总页标题形如 "神墓 辰南觉醒-动漫-高清完整正版视频在线观看"，
        # 剧名在首个 "-" 之前。
        return re.split(r"[-_|｜]", body)[0].strip()
    if not title:
        return ""
    series = ""
    # 策略1：网页标题里能直接定位 yt-dlp 的单集标题，取它之前的部分作为剧名
    if title in body:
        series = body[: body.index(title)].strip(" -_｜|")
    # 策略2：兜底——单集标题与网页略有出入时，按"第X话/集"把剧名切出来
    if not series:
        mm = re.match(r"^(.*?)[\s\-_]+第\s*\d+\s*[话集]", body)
        if mm:
            series = mm.group(1).strip(" -_｜|")
    return series

def _enrich_youku_series(info: dict[str, Any], proxy: str = "") -> dict[str, Any]:
    """优酷剧集：yt-dlp 只返回单集标题，从网页 <title> 提取整部剧名补到 info['series']。

    优酷播放页 HTML 的 title 通常是：
        "神墓 辰南觉醒 第1话 我自远古来-动漫-高清完整正版视频在线观看-优酷"
    而 yt-dlp 返回的 info['title'] 只有：
        "第1话 我自远古来"
    用网页 title 减去单集标题，即可得到 "神墓 辰南觉醒" 作为 series。

    抓取走 VDL_PROXY_CN（海外部署经国内代理回源）；失败时记录 _series_source 诊断，
    便于线上排查（Railway 抓优酷页可能被风控返回非视频页）。
    """
    title = (info.get("title") or "").strip()
    if not title:
        info["_series_source"] = "no_title"
        return info
    # 只对明显是"第X话/集"的单集标题做补全，避免普通短视频也走一次请求
    if not re.search(r"^(?:第\s*\d+\s*[话集]|\d+\s*[话集])", title):
        info["_series_source"] = "not_episode"
        return info

    webpage_url = info.get("webpage_url") or ""
    host = _host_of(webpage_url)
    if not webpage_url or not (host.endswith("youku.com") or host.endswith("tudou.com")):
        info["_series_source"] = "not_youku"
        return info

    try:
        import requests
    except Exception:
        info["_series_source"] = "no_requests"
        return info

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://v.youku.com/",
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    timeout = int(os.environ.get("VDL_YOUKU_TITLE_TIMEOUT", "15"))

    # 候选页面：v_show 单集页优先；URL 带 show id（s=）时追加 show_page 总页兜底
    candidates = [webpage_url]
    m_show = re.search(r"[?&]s=([0-9a-f]{12,})", webpage_url)
    if m_show:
        candidates.append(f"https://www.youku.com/show_page/id_{m_show.group(1)}.html")

    raw = ""
    parsed_series = ""
    last_err = ""
    for attempt in range(3):
        for u in candidates:
            try:
                resp = requests.get(
                    u, headers=headers, proxies=proxies,
                    timeout=timeout, allow_redirects=True,
                )
                resp.raise_for_status()
                candidate_raw = _extract_page_title(resp.text)
                if not candidate_raw:
                    continue
                # 只有真正从候选标题里解析出剧名才提前结束；
                # 否则（如被风控返回纯站点名"优酷"）继续尝试下一个候选页。
                is_show_page = "show_page" in u
                series_candidate = _parse_series_from_title(
                    candidate_raw, title, is_show_page=is_show_page
                )
                if series_candidate:
                    parsed_series = series_candidate
                    raw = candidate_raw
                    break
                # 没解析出剧名但拿到了标题，留作诊断候选（不覆盖已成功解析的）
                if not raw:
                    raw = candidate_raw
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {str(exc)[:100]}"
        if parsed_series:
            break
        if attempt < 2:
            time.sleep(1)

    if not parsed_series:
        info["_series_source"] = (
            f"fetch_failed:{last_err or 'empty'}" if not raw else "parse_failed"
        )
        logger.warning(
            "[youku series] no series parsed (raw=%r, err=%s)", raw, last_err
        )
        return info

    info["series"] = parsed_series
    info["_series_source"] = "web_title"
    logger.info("[youku series] extracted series=%r from webpage title", parsed_series)
    return info

def _combine_series_title(info: dict[str, Any]) -> str:
    """组合剧集名 + 单集标题，避免前端只显示"第X话 XXX"。

    yt-dlp 对剧集/动漫/综艺通常把整部剧名放在 series/alt_title，单集标题放在 title。
    如果两者都存在且 title 不含剧名，则拼接为"剧名 - 单集标题"。
    """
    title = (info.get("title") or "").strip()
    series = (info.get("series") or "").strip()
    alt = (info.get("alt_title") or "").strip()
    # 优先用 series；没有则用 alt_title
    show = series or alt
    if show and title:
        # 避免重复拼接（有些平台 title 里已经带了剧名前缀）
        if title.startswith(show) or show in title:
            return title
        return f"{show} - {title}"
    return title or show or "未命名视频"

def direct_headers(info: dict[str, Any]) -> dict[str, str]:
    """直链下载所需的最小请求头（仅 UA / Referer / Origin 白名单，不含 Cookie）。

    ★ 为什么要暴露到前端（2026-09-22 实测踩坑）：字节系 CDN（抖音/快手/微博/红果…）
      校验 Referer，缺失一律 403 openresty。而 **Referer 是浏览器的 forbidden
      header**，`<a href>` / fetch / XHR 都无法设置它 —— 所以「把直链丢给浏览器/
      WKWebView 下载」对这些平台天然不成立（桌面版还会把整个应用界面导航到 403 页）。
      桌面版改由本机原生桥按这些头拉流写盘；此处把 headers 一并下发。
    """
    raw = info.get("http_headers") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key in ("User-Agent", "Referer", "Origin"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = _clean_header_value(value)
    return out

def summarize(info: dict[str, Any]) -> dict[str, Any]:
    """抽取前端需要的字段。"""
    play_url, is_hls = _detect_play_url(info)
    _dh = direct_headers(info)
    return {
        "title": _combine_series_title(info),
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": int(info.get("duration") or 0),
        "thumbnail": info.get("thumbnail") or "",
        "view_count": info.get("view_count") or 0,
        "webpage_url": info.get("webpage_url") or "",
        "extractor": info.get("extractor_key") or "",
        "direct_url": _detect_direct_url(info),
        # 直链下载必须的防盗链头（UA/Referer）；needs_referer=True 代表浏览器侧
        # 无法直连（Referer 是 forbidden header）→ 桌面端走原生桥、web 端回落服务器下载。
        "direct_headers": _dh,
        "direct_needs_referer": bool(_dh.get("Referer") or _dh.get("referer")),
        "play_url": play_url,
        "is_hls": is_hls,
        "is_live": bool(info.get("is_live")),
        "watch_options": build_watch_options(info),
        # 诊断字段（仅供排查优酷剧集剧名补全是否生效，前端忽略即可）
        "_series_source": info.get("_series_source", ""),
    }
