"""server/routers/core.py — app.py 引擎层核心路由（Phase 3 抽取）。

承载下载 / 任务 / 解析 / 版本 / 平台 / 节点 / 流式代理 / Cookie 等核心 API。
通过 `import app` + 复用 app 模块级符号（downloader / store / scheduler / plat /
各 ENABLED 常量 / 辅助函数等；本文件在 app.py 末尾才被 import，符号已就绪），
handler 原样搬入、零改引用。路由用 @router.get/post 挂载，已在 app.py 末尾 include。
"""
import app
import hmac
import os
import object_store
import re
from fastapi import APIRouter
router = APIRouter()
from stats import record_event


def _device_of(request: app.Request) -> str:
    """取设备 ID：优先请求头 X-Device-Id（fetch 请求），其次 query device=
    （EventSource / <a href> 文件下载无法带自定义 header，走 query）。"""
    dev = (request.headers.get("X-Device-Id") or "").strip()
    if not dev:
        dev = (request.query_params.get("device") or "").strip()
    return dev[:64]


@router.get('/api/platforms')
def list_platforms() -> dict:
    return {'platforms': app.platform_catalog()}

def _read_build_version() -> str:
    """模块导入时读取一次，冻结为本进程「构建时」的真实版本。

    关键修复（2026-08-28）：原先每次请求都从磁盘现读 build_version.txt。
    当部署把新包覆盖到 /Applications 后，旧进程仍活着，却从新包的
    build_version.txt 读到新版本号 → 报假版本，无法识别 stale 进程
    （曾导致「修了没生效」误判、用户大怒）。

    改为导入时冻结一次：旧进程始终报自己启动时的旧号，新进程报新号，
    一眼可辨；同时让 deploy_mac.sh 的自校验真正强制重启旧进程，
    而不是被旧进程用新盘的版本号糊弄过去。
    """
    candidates = []
    exe = getattr(app.sys, 'executable', '')
    if exe:
        ep = app.Path(exe).resolve()
        # macOS .app：可执行文件在 Contents/MacOS ⇒ 资源在 ../Resources
        candidates.append(ep.parent.parent / 'Resources' / 'build_version.txt')
        # Windows / onedir：build_win.sh 把 build_version.txt 写在**产物根**（exe 同级）
        # 🔴 2026-10-26 真机验收：缺这一条 ⇒ Windows 上 /api/version 恒返回 'dev'
        #    （system.py 的 _candidates_build_txt 有，core.py 漏了 ⇒ 两端不同源），
        #    应用内更新拿不到正确当前版本。
        candidates.append(ep.parent / 'build_version.txt')
        # PyInstaller onedir 6.x 把运行期文件收进 _internal/
        candidates.append(ep.parent / '_internal' / 'server' / 'build_version.txt')
    candidates.append(app.Path(__file__).resolve().parent.parent / 'build_version.txt')
    for c in candidates:
        if c.exists():
            return c.read_text(encoding='utf-8').strip()
    return 'dev'


# 导入即冻结：此后无论磁盘 build_version.txt 是否被新部署覆盖，
# 本进程始终报告自己构建时的版本。
BUILD_VERSION = _read_build_version()


@router.get('/api/version')
def api_version() -> dict:
    """返回运行实例的构建指纹 + 实际加载的可执行文件路径。

    部署脚本 deploy_mac.sh 用它做自校验：只有运行中的服务返回的指纹与
    刚构建的 build_version.txt 一致、且 exe 路径确实指向目标 app 时，
    才算「部署成功」，否则直接判定失败，杜绝「装的是旧版却以为装好了」。

    注意：version 在模块导入时已冻结（见 BUILD_VERSION），旧进程永远报旧号，
    这是识别 stale 进程的关键。
    """
    return {'version': BUILD_VERSION, 'exe': app.sys.executable}

@router.get('/api/ydlp/version')
def ydlp_version_api() -> dict:
    """返回当前与最新 yt-dlp 版本，前端据此提示是否需要更新解析器。"""
    return {'current': app.ydlp_update.current_version(), 'latest': app.ydlp_update.latest_version()}

@router.post('/api/ydlp/update')
async def ydlp_update_api() -> dict:
    """下载最新 yt-dlp 解析器到本机目录（下次启动生效）。"""
    return await app.asyncio.to_thread(app.ydlp_update.update)

@router.get('/api/nodes')
def node_info() -> dict:
    """告诉前端：本节点在哪个区、对端节点在哪、哪些域名算国内站。

    前端据此在粘贴链接时自动把请求发到「离目标站点更近」的节点：
    国内站 → cn 节点，海外站 → global 节点。对端为空则退化为单节点模式。

    🔴 桌面版（profile=app）不向前端暴露 peer（2026-09-22 实测踩坑）：
    前端 baseFor() 拿到 peer 后会把海外链接**直发对端**，而对端机器上没有
    本机浏览器登录态，YouTube 等 Bot 检测站必报 cookie_required；前端 JS 也
    不可能读到 Chrome Cookie。桌面后端本就有完整的带登录态转发链路
    （解析 core.py /api/resolve 自动带浏览器 Cookie 转发、下载入口注入
    Cookie 后 run_remote_download 交给对端），所以前端全走本机、由后端转发
    才是正解。web 云端部署（VDL_INSTANCE=cloud）行为不变。
    """
    _is_web = app.os.environ.get('VDL_INSTANCE') == 'cloud'
    info = {'region': app.NODE_REGION, 'peer': (app.PEER_ENDPOINT if _is_web else ''), 'china_domains': list(app.CHINA_DOMAINS), 'commentary_enabled': app.COMMENTARY_ENABLED, 'ads_enabled': app.ADS_ENABLED, 'convert': {'subscription_required': app.CONVERT_SUB_ENABLED, 'free_daily': app.CONVERT_FREE_DAILY, 'targets': list(app.CONVERT_TARGETS.keys())}, 'download': {'subscription_required': app.DOWNLOAD_SUB_ENABLED, 'free_daily': app.DOWNLOAD_FREE_DAILY}, 'library': {'enabled': app.plat.is_desktop() or bool(app.os.environ.get('VDL_LIBRARY_ENABLED'))}, 'subscriptions': {'enabled': app.SUB_ENABLED, 'probe_limit': app.SUBSCRIBE_PROBE_LIMIT, 'check_interval': app.SUB_CHECK_INTERVAL}, 'retention': {'enabled': app.RETENTION_ENABLED, 'trash_available': app.retention_mod.trash_available() if app.RETENTION_ENABLED else False}, 'crypto': {'enabled': app.CRYPTO_ENABLED, 'has_pass': bool(app._vault_load()) if app.CRYPTO_ENABLED else False, 'locked': app.VAULT_KEY is None}, 'torrent': {'enabled': app.TORRENT_ENABLED, 'available': app.torrent_mod.available()}, 'ai_dewatermark': {'enabled': app.AI_DEWATERMARK_ENABLED, 'gpu': app.AI_GPU_AVAILABLE, 'image_ai': bool(app.dwc_ai_available)}, 'authRequired': app.AUTH_REQUIRED, 'profile': ('web' if _is_web else 'app')}
    caps = app.plat.node_capabilities()
    return {k: v for k, v in info.items() if k not in app.plat.NODE_GROUPS or k in caps}

@router.post('/api/resolve')
async def resolve(payload: app.ResolveRequest, request: app.Request) -> dict:
    # 2026-09-06 优化：解析不再设配额墙（解析预览放开，会员墙挪到「点清晰度下载」处）
    app._check_rate_limit(request)
    app._assert_safe_url(payload.url)
    url, platform = app.parse_source(payload.url)
    host = app._host_of(url)
    # 🔴 YouTube ID 长度前置校验：放在 peer 转发**之前**，否则无效 ID 会先打到
    # 对端撞 bot 检测，被误报成「需要登录 Cookie」（2026-09-22 实测踩坑）。
    if 'youtube.com' in host or 'youtu.be' in host:
        app.downloader.validate_youtube_id(url)
        # 🔴 youtu.be 短链规范化成 watch 长链（2026-09-22 实测踩坑）：同一份 Cookie，
        # 对端解析 watch 形态 200、youtu.be 形态 400——Cookie 请求头按初始 URL 的域
        # 绑定，youtu.be 302 跳到 youtube.com 后登录态没跟过去，仍被判 bot 拦截。
        # 规范化后 host 同步修正，保证后面 Cookie 域名推导、peer 转发、本机回落全走
        # youtube.com；实现唯一真源在 downloader.canonicalize_video_url（下载入口同用）。
        _canon = app.downloader.canonicalize_video_url(url)
        if _canon != url:
            url = _canon
            try:
                payload.url = url
            except Exception:
                pass
            host = app._host_of(url)
    # 走云端 Playwright worker 的平台必须单独给额度：起 Chromium + 页面加载 +
    # 等播放器发流请求，实测 25~60s，用通用的国内直连阈值（60s）会误报超时。
    # 与网页端 core.py 保持同源，改这里时两边须同步。
    if app.downloader._is_douyin_host(host):
        # 抖音走 VPS Playwright 真实浏览器解析，起 Chromium + 页面加载实测 25-30s。
        # 🔴 2026-10-26 实测上调 75 → 120：云端 daemon 的抖音快路（a_bogus 签名直连）
        # 会先跑一次，失败/超时才回落 Playwright（daemon 侧另有 85s 硬上限），
        # 故最坏链路 ≈ 20s(快路预算) + 85s(Playwright) ≈ 105s —— 75s 必然误报超时。
        # 前端 /api/resolve 的 fetch 上限是 300s，120s 不会先被前端掐断。
        timeout = int(os.environ.get("VDL_RESOLVE_TIMEOUT_DOUYIN", "120"))
    elif app.downloader._is_iqiyi_host(host):
        # 爱奇艺 VPS Playwright worker：起 Chromium + 等播放器发 m3u8 请求，实测 30-50s
        timeout = 90
    elif 'bestv.com.cn' in host:
        # 百视TV wasm 签名 worker：实测 40-60s，默认 45s 会误超时
        timeout = 80
    elif host == 'v.qq.com':
        timeout = 35
    elif 'youtube.com' in host or 'youtu.be' in host:
        # 🔴 2026-09-26 修正：**必须低于 60s**。前端跑在 WKWebView 里，NSURLSession 对
        # 单个请求默认 60s 上限，超过就把连接掐断 → 前端只看到 WebKit 的 "Load failed"，
        # 后端精心准备的中文原因（需要 Cookie / 节点太慢）永远送不出去（实测一次 115.9s
        # 全被浪费）。50s 给后端留出在浏览器放弃前返回明确结论的余量；
        # downloader._resolve_youtube 内部另有 45s 墙钟预算，通常轮不到这里。
        timeout = 50
    elif app.is_china_host(host):
        timeout = app.RESOLVE_TIMEOUT_DOMESTIC
    else:
        timeout = app.RESOLVE_TIMEOUT_SECONDS
    # 出国兜底（2026-09-21）：桌面端跑在国内、本机没有出网链路时，海外站直连必然超时。
    # 前端 baseFor() 已经会把海外链接转发给对端，但那依赖 /api/nodes 请求成功
    # （跨境链路实测约 20% 概率失败），一旦失败前端拿不到 peer 就会退回本机 → 超时。
    # 这里做后端兜底：只要声明了对端节点、目标是海外站、且用户未显式指定代理，
    # 就交给对端解析；对端不可达才回落本机原有逻辑。
    _peer = (app.PEER_ENDPOINT or "").strip().rstrip("/")
    if _peer and not app.is_china_host(host) and not (payload.proxy or "").strip():
        # 🔴 2026-09-26 实测（血泪，三小时排障的最终真凶）：下面两步都会发起**真实网络
        # 访问**，绝不能跑在事件循环线程上。当时机器上留着一个 launchd 注入的
        # VDL_PEER_ENDPOINT=https://hanyuxz.top（对端在 ECS 上连 YouTube 必失败），
        # 于是每次解析海外站：同步 TLS 探测 + 同步 requests.post 合计阻塞事件循环
        # 数十秒 —— 期间整个后端（连 /api/version）零响应，前端 60s 后被 WebKit 掐断，
        # 用户只看到「连接本地服务失败（Load failed）」。代码永远是好的，死在网络等待上。
        # 现全部挪进 executor（不占事件循环），并给对端一个**远小于路由预算**的超时，
        # 坏对端不再能拖垮本机；探测失败/对端不可达一律平稳回落本机解析。
        _loop = app.asyncio.get_running_loop()
        try:
            _direct = await _loop.run_in_executor(None, app.downloader.can_download_directly, host)
        except Exception:  # noqa: BLE001
            _direct = False
        if not _direct:
            try:
                import requests as _rq
                import logging as _logging
                # 🔴 用户没手动粘贴 Cookie 时，把本机浏览器里实时解密出的登录态一并带给对端：
                # 对端（无浏览器的服务器）自己的 Cookie 源大概率是空的，不带这条就会误报
                # 「YouTube 需要登录 Cookie」——明明本机浏览器明明登录着（2026-09-22 实测踩坑）。
                if payload.cookie:
                    _ck = payload.cookie
                else:
                    _ck = await _loop.run_in_executor(
                        None, app.downloader.get_browser_cookie_header, host, payload.url) or ""
                _peer_timeout = min(20, timeout)
                _logging.getLogger(__name__).info(
                    "[peer] 海外站转发对端解析: %s -> %s (cookie_len=%s, timeout=%ss)",
                    host, _peer, len(_ck or ""), _peer_timeout)
                _r = await _loop.run_in_executor(
                    None,
                    lambda: _rq.post(_peer + "/api/resolve",
                                     json={"url": url, "cookie": _ck, "proxy": ""},
                                     timeout=_peer_timeout,
                                     # 必须显式禁用环境代理：桌面端进程常继承 Clash/系统代理，
                                     # 走代理访问自家节点会被误拦（与 _call_vps_worker 同理）
                                     proxies={"http": None, "https": None}))
                if _r.status_code == 200:
                    return _r.json()
                _logging.getLogger(__name__).warning("[peer] 对端解析返回 HTTP %s", _r.status_code)
            except Exception as _exc:  # noqa: BLE001
                import logging as _logging
                _logging.getLogger(__name__).warning("[peer] 对端解析异常，回落本机: %s", _exc)

    loop = app.asyncio.get_running_loop()
    # 🔴 本机解析路径也自动带浏览器 Cookie（与 peer 转发路径对称）：用户没手动粘贴
    # Cookie 时，自动注入本机浏览器里该站的登录态。数据中心 IP 上的 YouTube 等
    # Bot 检测只认完整登录会话，仅靠代理直连仍会被判 cookie_required（2026-09-22 实测）。
    # 🔴 2026-09-26 修复：Chrome Cookie 解密（_extract_chrome_cookies）实测 30s+，
    # 同步调用会**卡死整个事件循环** —— wait_for 的超时计时器也随之冻结（504 永不触发），
    # 于是前端 120s fetch 先掐断 → 用户看到「连接本地服务失败」红字（18:20 实测定案）。
    # 必须挪进 executor 与 probe 并行执行。
    _cookie = payload.cookie
    if not _cookie:
        _cookie = await loop.run_in_executor(None, app.downloader.get_browser_cookie_header, host, url) or ""
    try:
        info = await app.asyncio.wait_for(loop.run_in_executor(app.prober, app.downloader.probe, url, _cookie, payload.proxy), timeout=timeout)
    except app.asyncio.TimeoutError:
        host = app._host_of(url)
        if app.downloader._is_douyin_host(host):
            detail = '抖音解析超时。抖音已升级反爬，需云端真实浏览器解析（起浏览器 + 加载页面后捕获真实流），偶发较慢。建议：①稍后重试；②确认链接是单个视频播放页（而非首页/列表）；③仍失败请反馈该链接'
        elif app.downloader._is_iqiyi_host(host):
            detail = '爱奇艺解析超时。依赖云端 Playwright worker（启动 Chromium + 等播放器发 m3u8 请求）。建议：①稍后重试；②确认链接是分享页或 v_xxx.html 而非首页；③若持续失败请反馈该链接'
        elif host == 'v.qq.com':
            detail = '腾讯视频解析超时。该视频可能是会员/付费内容，或腾讯页面改版导致提取器暂时失效。建议：①在「高级选项」粘贴浏览器 Cookie 后重试；②确认视频可公开访问（非 VIP 专享）；③稍后重试或反馈此链接'
        elif 'youtube.com' in host or 'youtu.be' in host:
            detail = f'YouTube 解析超时（超过 {timeout} 秒）。常见原因：①代理速度慢或不稳定（YouTube 需要拉取 player.js 签名，代理延迟会叠加）；②该视频可能受限（地区/年龄限制）；建议：①检查代理是否通畅；②稍后重试；③若持续失败，尝试更换节点或关闭代理直连'
        else:
            detail = f'解析超时（超过 {timeout} 秒）。常见原因：①视频本身受限（限免/会员专享/付费/地区限制，这类通常需登录 cookie 才能拿到真实流，请到右上角「高级选项」粘贴浏览器 Cookie 后重试）；②当前网络无法访问该平台（可尝试在「高级选项」设置代理）'
        raise app.HTTPException(status_code=504, detail=detail) from None
    return {'url': url, 'platform': {'key': platform.key, 'name': platform.name}, 'video': app.downloader.summarize(info), 'qualities': app.downloader.build_quality_options(info), 'sources': []}

def _download_gate_error(request) -> str | None:
    """下载配额门（2026-09-06）：免费 10 次/日 → 会员 1000 次/日。放行返回 None，否则返回引导文案。
    按请求用户态取 store（登录用户用其自身会员；匿名用全局免费档）。"""
    qs = app.current_member_store(request).quota_state('download')
    if qs.get('allowed'):
        return None
    if qs.get('tier') == 'free':
        return f"今日免费下载次数已用尽（{qs.get('limit', 10)}/日）— 开通VIP会员可解锁 {qs.get('member_limit') or 1000} 次/日"
    return f"今日下载配额已用尽（{qs.get('limit')}/日）"

# 清晰度会员门槛（2026-10-02 用户定档「免费用户要弹出会员才能下载」）：
#   - 免费档：显式选 >1080P（1440/2160…）不开放 → 402 + 引导开会员。
#   - 会员档：不额外设限（沿用 download 1000 次/日）。
# 「最佳画质（自动）」的封顶由前端按来源类型落档（desktop-app.js::qualityForDownload）：
# 直链/分片本身就是单一流，强塞 1080 会挑不到流（2026-10-01 实测教训），故只在
# 「页面 / 清单」这类可解析多档的来源上把免费用户的自动档降为 1080P。
# 不放进 membership 引擎：引擎的 original（原画/4K）键是**按次配额**语义（0/日），
# 本门槛是**档位权限**语义（免费一次都不给），混用会产出「0/日」这种怪文案。
FREE_MAX_QUALITY = 1080


def _quality_height(quality: str) -> int:
    """把清晰度键折算成高度；不参与判档的键（best/audio/webm/m4a）返回 0。"""
    key = (quality or '').strip().lower()
    if key in (app.downloader.BEST_KEY, app.downloader.AUDIO_KEY,
               app.downloader.WEBM_KEY, app.downloader.M4A_KEY):
        return 0
    try:
        return int(key)
    except (TypeError, ValueError):
        return 0


def _quality_gate_error(request, quality: str) -> str | None:
    """清晰度门（2026-10-02）：免费档 >1080P 需会员。放行返回 None，否则返回引导文案。

    会员态读不到时 fail-open（宁漏管一次，也不让付费用户被挡）。
    """
    height = _quality_height(quality)
    if height <= FREE_MAX_QUALITY:
        return None
    try:
        status = app.current_member_store(request).status()
    except Exception:  # noqa: BLE001 - 会员态是尽力而为，绝不因读不到就拦人
        return None
    if (status.get('download_member') or {}).get('active'):
        return None
    label = app.downloader.quality_label(str(height))
    return (f'免费用户最高支持 {FREE_MAX_QUALITY}P 清晰度（本次选择 {label}）— '
            f'开通VIP会员可解锁 2K/4K 原画')


def _stream_referer(host: str) -> str:
    """按平台返回防盗链 Referer：腾讯视频 HLS 分片必须带正确的 Referer 才返回 200。

    注意：YouTube / googlevideo.com 等**不在此返回 Referer**——它们靠 URL 签名（ip/n/sig 参数）
    验证请求合法性，带错误 Referer（如 googlevideo.com 自身）反而会触发 403 拒绝。
    调用方应对 YouTube 域跳过 Referer。
    """
    if 'v.qq.com' in host:
        return 'https://v.qq.com/'
    if 'douyin' in host:
        return 'https://www.douyin.com/'
    if 'bilibili' in host:
        return 'https://www.bilibili.com/'
    if 'googlevideo.com' in host or 'youtube.com' in host or 'youtu.be' in host:
        return ''
    return f'https://{host}/' if host else 'https://v.qq.com/'

def _rewrite_m3u8(text: str, base_url: str, proxy_prefix: str) -> str:
    """把 m3u8 内每条 URL 绝对化后改写成指向本端点的代理 URL。

    - 非注释、非空行即 URL 行（子 playlist / ts 分片），整行改写；
    - #EXT-X-KEY / #EXT-X-MEDIA 等标签行里的 URI="..." 属性也改写（加密流的 key 直连
      会被防盗链 403，必须走本端点带 Referer）。
    这样原生 <video> 播放器解析 master→子 playlist→ts→key 时，每一跳都走本端点。
    """
    uri_re = app.re.compile('(URI=")([^"]+)(")')

    def _rewrite_uri(m: 're.Match') -> str:
        seg = m.group(2).strip()
        abs_url = app.urljoin(base_url, seg)
        return m.group(1) + proxy_prefix + app.quote(abs_url, safe='') + m.group(3)
    out: list[str] = []
    for line in text.split('\n'):
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        if stripped.startswith('#'):
            if 'URI=' in line:
                line = uri_re.sub(_rewrite_uri, line)
            out.append(line)
            continue
        abs_url = app.urljoin(base_url, stripped)
        out.append(proxy_prefix + app.quote(abs_url, safe=''))
    return '\n'.join(out)

def _own_hosts(request) -> set[str]:
    """本服务自身的 host 集合（用于判定调用方是否来自我们自己的页面）。"""
    hosts: set[str] = set()
    try:
        h = (request.url.hostname or "").lower()
        if h:
            hosts.add(h)
    except Exception:
        pass
    try:
        h2 = (request.headers.get("host") or "").split(":")[0].strip().lower()
        if h2:
            hosts.add(h2)
    except Exception:
        pass
    return hosts


def _caller_is_own_page(request) -> bool:
    """调用方是否来自本服务自己的页面。

    安全审计 2026-10-11（P1-3）：`/api/stream/proxy` 会**自动注入用户真实浏览器
    的登录态**去源站取流。若任意网页都能调用它，就等于把本端点变成一个
    「带用户登录态的取数器」（配合原回包的 `Access-Control-Allow-Origin: *`，
    恶意页面可以直接读走响应体 = 会话数据外泄）。

    这里要求：请求带 `Referer` / `Origin` 时，其 host 必须指向本服务自身；
    两者都缺（原生 `<video>` 播放器直连常不带 Referer）才按可信处理 ——
    此时回包已移除 ACAO，跨源页面读不到内容（纵深防御，见 stream_proxy）。
    """
    if request is None:
        return False
    own = _own_hosts(request)
    for hdr in ("referer", "origin"):
        try:
            v = (request.headers.get(hdr) or "").strip()
        except Exception:
            v = ""
        if not v:
            continue
        try:
            h = (app.urlparse(v).hostname or "").lower()
        except Exception:
            return False
        return bool(h and h in own)
    return True


def _stream_get(u: str, headers: dict, proxies, timeout):
    """带 SSRF 复检的重定向跟随（最多 3 跳）。

    安全审计 2026-10-11（P1-3）：`requests` 默认自动跟随重定向，而 `_assert_safe_url`
    只校验**首跳** —— 一个公网地址 302 到 `169.254.169.254`（云元数据）或内网地址
    即可把护栏整个绕过去。现改为手动逐跳校验；跨 host 跳转时丢弃 Cookie
    （与浏览器同源策略一致，避免把 A 站的登录态带给 B 站）。
    """
    cur = u
    cur_headers = dict(headers)
    cur_host = (app.urlparse(u).hostname or "").lower()
    last = None
    for _hop in range(4):
        app._assert_safe_url(cur)
        resp = app.requests.get(cur, headers=cur_headers, stream=True,
                                timeout=timeout, proxies=proxies,
                                allow_redirects=False)
        if resp.status_code not in (301, 302, 303, 307, 308):
            return resp
        loc = (resp.headers.get("Location") or "").strip()
        if not loc:
            return resp                      # 异常响应：交给上层按原状态处理
        nxt = app.urljoin(cur, loc)
        nxt_host = (app.urlparse(nxt).hostname or "").lower()
        resp.close()
        if nxt_host != cur_host:
            cur_headers.pop("Cookie", None)
        cur = nxt
        cur_host = nxt_host
        last = resp
    raise app.HTTPException(status_code=508, detail="上游重定向次数过多，已中止")


@router.get('/api/stream/proxy')
def stream_proxy(u: str='', cookie: str='', request: app.Request=None):
    """在线观看流代理：浏览器（WKWebView）直连腾讯会被防盗链 403，且原生 HLS 无法自定义
    Referer 头。这里由后端带 Referer/Cookie 去源站拉取回传，从而绕开防盗链。

    - 对非 m3u8（MP4/ts 分片等）原样流式透传；
    - 对 m3u8 清单：把内部相对/绝对 URL 改写为指向本端点的代理 URL，这样原生 <video>
      播放器解析 master→子 playlist→ts 分片时，每一跳都走本端点（后端统一带 Referer），
      无需 hls.js，macOS 原生 HLS 即可播放。

    🔴 安全审计 2026-10-11（P1-3「stream/proxy SSRF + Cookie 外泄」）三处加固：
      ① **不再回 `Access-Control-Allow-Origin: *`** —— 原实现让任意网页都能
         `fetch()` 读走响应体（含用用户浏览器登录态取回的私密内容），是最直接的
         外泄通道。同源播放（本项目 UI 与本端点同源）不需要 CORS 头，去掉零影响。
      ② 重定向逐跳复检（`_stream_get`）：只验首跳时，公网地址 302 到内网即可绕过。
      ③ 自动注入浏览器登录态前先判「调用方是否我们自己的页面」（`_caller_is_own_page`），
         防止被外部页面当作「带登录态的取数器」使用。

    说明：本端点不能直接加 Bearer 鉴权 —— 原生 `<video>` / HLS 分片请求无法携带
    自定义头。故以「同源 Referer + 无 ACAO」替代；若后续要更强保证，可改为由前端
    签发一次性 HMAC 短票据放进 query（需前后端同步改动）。
    """
    if not u:
        raise app.HTTPException(status_code=400, detail='缺少 u 参数')
    app._assert_safe_url(u)
    host = app._host_of(u)
    _proxies: dict[str, str] | None = None
    if not app.is_china_host(host):
        _proxy_url = app.downloader._resolve_proxy(host)
        if _proxy_url:
            _proxies = {'http': _proxy_url, 'https': _proxy_url}
    user_cookie = (cookie or '').strip()
    if user_cookie.lower().startswith('cookie:'):
        user_cookie = user_cookie[7:].strip()
    cookie_text = user_cookie
    used_auto_cookie = False
    # 仅当调用方是我们自己的页面时，才去读用户真实浏览器的登录态（P1-3 ③）。
    if not cookie_text and _caller_is_own_page(request):
        try:
            auto = app.downloader.get_browser_cookie_header(host, u)
        except Exception:
            auto = None
        if auto:
            cookie_text = auto
            used_auto_cookie = True
    headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36', 'Range': 'bytes=0-'}
    _ref = _stream_referer(host)
    if _ref:
        headers['Referer'] = _ref
    if cookie_text:
        headers['Cookie'] = cookie_text
    _client_range = None
    if request:
        _cr = request.headers.get('range')
        if _cr:
            _client_range = _cr
            headers['Range'] = _cr
    try:
        resp = _stream_get(u, headers, _proxies, (10, 120))
    except app.HTTPException:
        raise
    except Exception as exc:
        raise app.HTTPException(status_code=502, detail=f'上游拉取失败：{app.downloader._clean_message(str(exc))}') from None
    if resp.status_code >= 400:
        detail = f'上游返回 {resp.status_code}'
        if resp.status_code in (401, 403):
            if used_auto_cookie:
                detail += '（已自动携带浏览器登录态仍被拒，可能需先在浏览器登录该平台，或手动粘贴 Cookie）'
            elif cookie_text:
                detail += '（防盗链被拒，可在「高级选项」重新粘贴 Cookie 后重试）'
            else:
                detail += '（防盗链被拒，可能需要登录 Cookie，请在「高级选项」粘贴浏览器 Cookie 后重试）'
        resp.close()
        raise app.HTTPException(status_code=resp.status_code, detail=detail)
    content_type = (resp.headers.get('Content-Type') or '').lower()
    is_m3u8 = 'mpegurl' in content_type or content_type in ('application/x-mpegurl', '') or '.m3u8' in u
    base = str(request.base_url).rstrip('/') if request is not None else 'http://127.0.0.1'
    proxy_prefix = f'{base}/api/stream/proxy?u='
    if is_m3u8:
        raw = resp.content.decode('utf-8', errors='replace')
        resp.close()
        if raw.lstrip().startswith('#EXTM3U'):
            rewritten = _rewrite_m3u8(raw, u, proxy_prefix)
            return app.Response(rewritten, media_type='application/vnd.apple.mpegurl', headers={'Cache-Control': 'no-store'})
        return app.Response(raw, media_type=content_type or 'application/octet-stream', headers={'Cache-Control': 'no-store'})

    def _gen():
        try:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            resp.close()
    # ⚠️ 不再返回 Access-Control-Allow-Origin（P1-3 ①）：同源播放不需要它，
    #    而它正是「任意网页读走带登录态内容」的开关。
    _resp_headers = {'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'}
    if resp.headers.get('Accept-Ranges'):
        _resp_headers['Accept-Ranges'] = resp.headers['Accept-Ranges']
    if resp.status_code == 206 and resp.headers.get('Content-Range'):
        _resp_headers['Content-Range'] = resp.headers['Content-Range']
    if resp.headers.get('Content-Length'):
        _resp_headers['Content-Length'] = resp.headers['Content-Length']
    return app.StreamingResponse(_gen(), media_type=content_type or 'application/octet-stream', headers=_resp_headers, status_code=resp.status_code)

@router.get('/api/cookie/status')
def cookie_status(url: str='') -> dict:
    """探测本机浏览器是否含目标站点的登录 Cookie，供前端「检测登录态」与解析后自动提示。

    2026-10-24：返回体新增 status / label / count / running / hint。
    原先把这些字段丢掉，前端只能拿到 available=True/False —— 用户看不出
    「到底是从哪个浏览器读到的」「读不到是因为没登录还是浏览器锁着库」，
    而这正是 Windows 上最常见的两种故障，必须能区分提示。
    """
    if not url:
        raise app.HTTPException(status_code=400, detail='请提供链接')
    app._assert_safe_url(url)
    url, platform = app.parse_source(url)
    host = app._host_of(url)
    info = app.downloader.detect_browser_cookie(host)
    return {
        'host': host,
        'platform': platform.key,
        'needed': app.downloader.is_cookie_hardened_host(host),
        'available': info.get('available', False),
        'browser': info.get('browser'),
        'label': info.get('label'),
        'profile': info.get('profile'),
        'running': info.get('running', False),
        'count': info.get('count', 0),
        'status': info.get('status', 'absent'),
        'hint': info.get('hint', ''),
    }

@router.post('/api/cookie/cache/clear')
def cookie_cache_clear() -> dict:
    """清除本机 Cookie 缓存（仅删 ~/.videodownloader/cookies，不影响浏览器本身）。"""
    from cookie_cache import clear_cookie_cache
    n = clear_cookie_cache()
    return {'ok': True, 'cleared': n}

def _valid_extract_mode(value: str) -> str:
    """校验并归一化文案提取模式，非法值回退为不提取。"""
    return value if value in ('spoken', 'description', 'both') else ''

@router.post('/api/download')
def create_download(payload: app.DownloadRequest, request: app.Request) -> dict:
    app._check_rate_limit(request)
    # 会员下载配额（2026-09-06）：免费 10 次/日 → 会员 1000 次/日；不足 402 + MEMBER_QUOTA|
    _err = _download_gate_error(request)
    if _err:
        raise app.HTTPException(status_code=402, detail='MEMBER_QUOTA|' + _err)
    url, platform = app.parse_source(payload.url)
    # 🔴 下载入口同样要规范化短链（2026-09-22 实测）：解析走规范化长链、下载仍用
    # youtu.be 短链时，对端会因 Cookie 域不跟随 302 而报错 → 表现为「解析成功但
    # 下载卡住/失败」的分裂现象。实现唯一真源 downloader.canonicalize_video_url。
    url = app.downloader.canonicalize_video_url(url)
    # 🔴 安全审计 2026-10-11（P1-4「download/batch 缺 SSRF 护栏」）：`parse_source` 只做
    #    平台目录匹配，**不校验目标地址** —— 于是 `http://169.254.169.254/latest/meta-data/`
    #    这类内网/云元数据地址可以直接进下载队列（yt-dlp 会真的去拉）。
    #    `/api/resolve` 早有 `_assert_safe_url`，唯独真正落盘的下载入口漏了。
    app._assert_safe_url(url)
    if not app.downloader.is_valid_quality(payload.quality):
        raise app.HTTPException(status_code=400, detail='不支持的清晰度选项')
    # 清晰度门（2026-10-02）：免费档选 2K/4K 在**建任务之前**拦下，前端弹会员中心。
    _qerr = _quality_gate_error(request, payload.quality)
    if _qerr:
        raise app.HTTPException(status_code=402, detail='MEMBER_QUOTA|' + _qerr)
    extract_mode = _valid_extract_mode(payload.extract_script)
    # 出国兜底（2026-09-21）：海外平台 + 已声明对端节点 + 用户未显式指定代理 →
    # 整个下载交给对端执行、成品再回传本机（run_remote_download），本机无需出网链路。
    # 用户自己填了代理时仍走本机（此时本机本就能出海，避免跨境回传的带宽损耗）。
    _peer = (app.PEER_ENDPOINT or "").strip().rstrip("/")
    _host_of_url = app._host_of(url)
    _goes_remote = bool(_peer and not app.is_china_host(_host_of_url)
                        and not (payload.proxy or "").strip()
                        and not app.downloader.can_download_directly(_host_of_url))
    # 🔴 走对端时，本机浏览器实时解密的登录态必须跟着走（对端没有浏览器，
    # 缺它就会误报「需要登录 Cookie」——与 resolve 转发同理，2026-09-22 实测踩坑）。
    _task_cookie = payload.cookie
    if _goes_remote and not (_task_cookie or "").strip():
        _task_cookie = app.downloader.get_browser_cookie_header(_host_of_url, url) or ""
    if _goes_remote:
        _runner = app.downloader.run_remote_download
    else:
        _runner = app.downloader.run_download
    task = app.store.create(url=url, title=(payload.title or ''), platform=platform.name, quality=app.downloader.quality_label(payload.quality), quality_key=payload.quality, extract_mode=extract_mode, concurrent_fragments=payload.concurrent_fragments, downloader_type=payload.downloader, cookie=_task_cookie, proxy=payload.proxy, play_url=payload.play_url, watch_options=payload.watch_options, is_hls=payload.is_hls, referer=(payload.referer or '').strip(), owner_id=app._task_owner(request))
    # 断点接管（2026-10-30）：tasks.create 可能把「同链接上次没下完」的分片搬进了这个
    # 新任务目录（用户重贴链接的常见路径）。此时必须以 resume=True 起执行器，否则
    # yt-dlp 会忽略磁盘上的分片从 0 开始，接管就白做了。
    _resume = (getattr(task, 'kind', 'video') == 'video') and app.downloader._has_partial(task.workdir)
    app.scheduler.submit(_runner, task, app.store, payload.quality, payload.cookie, payload.proxy, app.SINGLE_DOWNLOAD_RETRIES, payload.format_id, payload.concurrent_fragments, payload.downloader, _resume)
    # 任务创建成功才计费（失败/被拒不烧免费额度）
    # 2026-10-19：带上标题与平台，后台「使用详情」能显示「下载了什么」而不是干巴巴的次数
    # 2026-10-20：把这次消耗的 ref 与账本挂到任务上 —— 下载结束后按同一 ref 回写
    #   「是否真的下载完成并保存到本机」，后台才能看见成功/失败，而不只是次数。
    _mstore = app.current_member_store(request)
    _charged = _mstore.use_daily(
        'download', 1, title=(task.title or task.url), platform=platform.name)
    if _charged.get("ref"):
        task.usage_ref = str(_charged["ref"])
        task._usage_store = _mstore
    _qs = _mstore.quota_state('download')
    record_event('download', {'platform': platform.name, 'quality': payload.quality})
    return {'task_id': task.id, 'status': task.status,
            'quota': {'subscribed': _qs.get('tier') == 'member',
                      'free_used': _charged.get('used', _qs.get('used', 0)),
                      'free_daily': _qs.get('limit', 10)}}

class BatchRequest(app.BaseModel):
    urls: list[str] = app.Field(default_factory=list, max_length=app.VDL_BATCH_MAX_ITEMS)
    quality: str = app.Field(default=app.downloader.BEST_KEY, max_length=16)
    cookie: str = app.Field(default='', max_length=8192)
    proxy: str = app.Field(default='', max_length=256)
    concurrency: int = app.Field(default=0, ge=0, le=app.VDL_BATCH_HARD_MAX)
    retries: int = app.Field(default=-1, ge=-1, le=10)
    extract_script: str = app.Field(default='', max_length=16)

@router.post('/api/batch')
def create_batch(payload: BatchRequest, request: app.Request) -> dict:
    app._check_rate_limit(request)
    urls = [u.strip() for u in payload.urls if u.strip()]
    if not urls:
        raise app.HTTPException(status_code=400, detail='没有提供有效的链接')
    if not app.downloader.is_valid_quality(payload.quality):
        raise app.HTTPException(status_code=400, detail='不支持的清晰度选项')
    extract_mode = _valid_extract_mode(payload.extract_script)
    # 清晰度门（2026-10-02）：批量与单条同规 —— 免费档不能选 2K/4K。
    _qerr = _quality_gate_error(request, payload.quality)
    if _qerr:
        raise app.HTTPException(status_code=402, detail='MEMBER_QUOTA|' + _qerr)
    if payload.concurrency > 0:
        app.scheduler.set_concurrency(payload.concurrency)
    retries = payload.retries if payload.retries >= 0 else app.BATCH_RETRIES_DEFAULT
    task_ids: list[str] = []
    skipped = 0
    quota_exhausted = False
    for u in urls:
        _err = _download_gate_error(request)
        if _err:
            quota_exhausted = True
            break
        try:
            url, platform = app.parse_source(u)
            # 安全审计 2026-10-11（P1-4）：批量入口同样要过 SSRF 护栏；
            # 不安全地址按「无法识别」计入 skipped，不中断整批。
            app._assert_safe_url(url)
        except (app.UnsupportedPlatformError, app.LinkError):
            skipped += 1
            continue
        task = app.store.create(url=url, title='', platform=platform.name, quality=app.downloader.quality_label(payload.quality), quality_key=payload.quality, extract_mode=extract_mode, owner_id=app._task_owner(request))
        _b_resume = app.downloader._has_partial(task.workdir)
        app.scheduler.submit(app.downloader.run_download, task, app.store, payload.quality, payload.cookie, payload.proxy, retries, '', task.concurrent_fragments, task.downloader_type, _b_resume)
        # 每个成功创建的任务计 1 次下载配额（2026-10-20：ref/账本挂任务，供结束后回写结果）
        _mb = app.current_member_store(request)
        _ch = _mb.use_daily('download', 1, title=url, platform=platform.name)
        if _ch.get("ref"):
            task.usage_ref = str(_ch["ref"])
            task._usage_store = _mb
        task_ids.append(task.id)
    if not task_ids:
        if quota_exhausted:
            raise app.HTTPException(status_code=402, detail='MEMBER_QUOTA|' + (_download_gate_error(request) or '今日免费下载次数已用尽 — 开通VIP会员可解锁'))
        raise app.HTTPException(status_code=400, detail='链接均无法识别，请确认是视频播放页链接')
    return {'task_ids': task_ids, 'count': len(task_ids), 'skipped': skipped, 'quota_exhausted': quota_exhausted}

@router.get('/api/tasks')
def list_tasks(request: app.Request) -> dict:
    """列出当前用户的任务（含排队 / 进行中 / 已完成），供前端队列概览。

    安全审计 P0-2：此前用 list_all() 返回**全站**任务，任何人可看到他人视频标题、
    直链与提取文案。现按归属过滤（历史无归属任务仍可见，避免升级后凭空消失）。
    """
    tasks = [t.to_public_dict() for t in app.store.list_for_owner(app._task_owner(request))]
    stats = {'pending': 0, 'downloading': 0, 'merging': 0, 'paused': 0, 'pausing': 0,
             'completed': 0, 'failed': 0, 'canceled': 0}
    for t in tasks:
        stats[t['status']] = stats.get(t['status'], 0) + 1
    stats['active'] = app.scheduler.active_count()
    return {'tasks': tasks, 'stats': stats, 'concurrency': app.scheduler.concurrency}

@router.post('/api/tasks/{task_id}/retry')
def retry_task(task_id: str, request: app.Request) -> dict:
    task = app._require_owned_task(task_id, request)
    # B5：成品已清理的历史条目（completed+file_expired）也允许「重新下载」
    _hist_redownload = task.status == 'completed' and getattr(task, 'file_expired', False)
    if task.status not in ('failed', 'canceled') and not _hist_redownload:
        raise app.HTTPException(status_code=400, detail='仅失败 / 已取消 / 成品已清理的任务可以重试')
    # 🔴 安全审计 2026-10-11（P1-5「retry/resume 绕过配额」）：重试此前**既不过配额门
    #    也不计费** —— 于是「取消 → 重试」与「历史条目（completed+file_expired）重新下载」
    #    可以无限刷，下载配额形同虚设。
    #    现在：① 一律先过配额门（用尽即 402）；
    #          ② 仅当本次**会真正重新传输**时计 1 次 —— 有 .part 的续传是同一份传输的
    #             继续（创建任务时已计费），不重复收；历史条目重新下载与无 .part 的
    #             失败任务重试都是新传输，按次计费。
    _err = _download_gate_error(request)
    if _err:
        raise app.HTTPException(status_code=402, detail='MEMBER_QUOTA|' + _err)
    task.cancel_requested = False
    task.cancel_reason = ''
    # 2026-10-20：「这次执行是否已回报过结果」是**按次**的 —— 重试/续传是新一轮执行，
    # 必须清掉旧标志，否则新 ref（重试计费）或旧 ref（续传）都不会再回写结果，
    # 最终成败永远停在上一轮的 fail 上。续传时 ref 不变，成功会覆盖成「已保存」。
    task._usage_reported = False
    # 图片任务（kind=image，2026-10-12 P0）没有 .part 断点续传概念 —— 单文件直传，
    # 取消即删半成品，重试就是一次新传输；执行器也要换成 run_image_download。
    _is_image = getattr(task, 'kind', 'video') == 'image'
    resume = (not _is_image) and app.downloader._has_partial(task.workdir) and not _hist_redownload
    app.store.update(task_id, status='pending', error='', hint='', progress=task.progress if resume else 0.0, downloaded_bytes=task.downloaded_bytes if resume else 0, total_bytes=task.total_bytes if resume else 0, speed=0.0, eta=0, filesize=0, filename='', resumable=False, file_expired=False, filepath=None)
    if _is_image:
        import image_download
        # 传 scheduler：重试的链接若是分享页，同样要能展开成子任务
        app.scheduler.submit(image_download.run_image_download, task, app.store,
                             None, app.scheduler)
    else:
        app.scheduler.submit(app.downloader.run_download, task, app.store, task.quality_key, task.cookie, task.proxy, app.BATCH_RETRIES_DEFAULT, '', task.concurrent_fragments, task.downloader_type, resume)
    if not resume:
        # 2026-10-19：带上标题与平台，便于后台定位「这次下载的是什么」
        # 2026-10-20：ref/账本挂任务 —— 重试后同样要能回写「成功/失败」
        _mb = app.current_member_store(request)
        _ch = _mb.use_daily(
            'download', 1, title=(getattr(task, 'title', '') or task.url),
            platform=getattr(task, 'platform', '') or '')
        if _ch.get("ref"):
            task.usage_ref = str(_ch["ref"])
            task._usage_store = _mb
    return {'task_id': task_id, 'status': 'pending', 'resume': resume}

@router.post('/api/tasks/{task_id}/extract-text')
def reextract_text(task_id: str, request: app.Request) -> dict:
    """对已完成任务重新提取文案（如首次语音转写超时，可点重试）。"""
    task = app._require_owned_task(task_id, request)
    if not task.extract_mode:
        raise app.HTTPException(status_code=400, detail='该任务未开启文案提取')
    if not task.filepath or not app.Path(task.filepath).exists():
        raise app.HTTPException(status_code=400, detail='任务文件不存在，无法提取文案')
    app.executor.submit(app.downloader._run_extraction, task, app.store, app.Path(task.filepath), None, '', '', mode=task.extract_mode)
    return {'task_id': task_id, 'status': 'running'}

@router.post('/api/tasks/cancel-all')
def cancel_all_tasks(request: app.Request) -> dict:
    """取消**自己**所有进行中 / 排队中的任务；已完成与失败的任务保留（不删文件）。

    安全审计 P0-2：此前遍历全站任务，任何人可一键取消所有人的下载（DoS）。
    """
    canceled = 0
    for t in app.store.list_for_owner(app._task_owner(request)):
        if not t.is_finished and app.store.request_cancel(t.id):
            canceled += 1
    return {'canceled': canceled}

@router.get('/api/batch/config')
def batch_config() -> dict:
    return {'concurrency': app.scheduler.concurrency, 'hard_max': app.VDL_BATCH_HARD_MAX, 'retries': app.BATCH_RETRIES_DEFAULT}

@router.get('/api/tasks/{task_id}')
def task_status(task_id: str, request: app.Request) -> dict:
    return app._require_owned_task(task_id, request).to_public_dict()

@router.get('/api/tasks/{task_id}/events')
async def task_events(task_id: str, request: app.Request) -> app.StreamingResponse:
    app._require_owned_task(task_id, request)

    async def event_stream():
        elapsed = 0.0
        while elapsed < app.SSE_MAX_SECONDS:
            if await request.is_disconnected():
                return
            task = app.store.get(task_id)
            if task is None:
                yield _sse({'status': 'failed', 'error': '任务已过期'})
                return
            yield _sse(task.to_public_dict())
            if task.is_finished:
                return
            await app.asyncio.sleep(app.SSE_INTERVAL_SECONDS)
            elapsed += app.SSE_INTERVAL_SECONDS
    return app.StreamingResponse(event_stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'})

def _sse(data: dict) -> str:
    return f'data: {app.json.dumps(data, ensure_ascii=False)}\n\n'

@router.get('/api/tasks/{task_id}/file')
def download_file(task_id: str, request: app.Request, download: int=0) -> app.Response:
    task = app._require_owned_task(task_id, request)
    if task.status != 'completed' or not task.filepath or (not task.filepath.exists()):
        raise app.HTTPException(status_code=409, detail='文件尚未准备好')
    _ext = task.filepath.suffix.lower()
    _mt = {'.mp4': 'video/mp4', '.webm': 'video/webm', '.mkv': 'video/x-matroska', '.m4a': 'audio/mp4', '.mp3': 'audio/mpeg'}.get(_ext, 'application/octet-stream')
    # 图片下载（kind=image，2026-10-12 P0）：按扩展名补图片 media type，
    # 让 <img>/预览 场景内联渲染、保存场景走 download=1 附件。
    # 🔴 SVG 刻意映射为 application/octet-stream：SVG 可含 <script>，
    #    以 image/svg+xml 内联渲染等于把用户会话暴露给图片作者 —— 只许附件下载。
    _mt = {**{'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
              '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
              '.avif': 'image/avif', '.heic': 'image/heic', '.heif': 'image/heif',
              '.tiff': 'image/tiff', '.ico': 'image/x-icon',
              '.svg': 'application/octet-stream'},
           **{'.mp4': 'video/mp4', '.webm': 'video/webm', '.mkv': 'video/x-matroska',
              '.m4a': 'audio/mp4', '.mp3': 'audio/mpeg'}}.get(_ext, 'application/octet-stream')
    if download:
        # 强制下载：流式传输，避免 read_bytes() 把大文件读进内存；
        # Content-Disposition 的 filename= 只能是 ASCII，中文走 filename*=UTF-8''。
        import urllib.parse
        from starlette.responses import FileResponse
        _encoded = urllib.parse.quote(task.filepath.name)
        _ascii = task.filepath.name.encode('ascii', 'ignore').decode() or 'download'
        return FileResponse(
            path=task.filepath,
            media_type='application/octet-stream',
            headers={'Content-Disposition': f"attachment; filename=\"{_ascii}\"; filename*=UTF-8''{_encoded}"},
        )
    return app.FileResponse(path=task.filepath, filename=task.filepath.name, media_type=_mt)


@router.get('/api/tasks/{task_id}/direct_url')
def task_direct_url(task_id: str, request: app.Request) -> app.Response:
    """对象存储直链（方案③，2026-10-20）。

    为什么要有这条：`/file?download=1` 是**部署节点单流回传**。节点在海外时
    （Cloudflare → Railway）跨境单连接实测 ~120KB/s，418MB 要 49 分钟。
    直链方案让节点先上传一次到 OSS/CDN，浏览器再从 CDN 全速拉 —— 一次上传，
    之后重下/多设备/分享都是全速。

    状态码契约（前端据此决定是否回退，必须严格遵守）：
      200 就绪：{status:'ready', url, expires_in, size, cdn}
      202 上传中：{status:'uploading', uploaded, total, percent}
      404 未配置对象存储（前端**静默**回退 /file，行为与旧版一致）
      409 成品还没准备好
      502 上传失败（前端回退 /file，并提示走的是慢通道）
    """
    task = app._require_owned_task(task_id, request)
    if task.status != 'completed' or not task.filepath or (not task.filepath.exists()):
        raise app.HTTPException(status_code=409, detail='文件尚未准备好')
    if not object_store.enabled():
        return app.JSONResponse(status_code=404, content={
            'status': 'disabled', 'detail': '未配置对象存储（VDL_OSS_*）'})
    try:
        state = object_store.ensure_uploaded(
            task_id, task.filepath, getattr(task, 'filename', '') or task.filepath.name)
    except Exception as e:  # noqa: BLE001 — 任何异常都不能挡住「能下载」这条底线
        app.logger.warning('对象存储直链失败 task=%s：%s', task_id, e)
        return app.JSONResponse(status_code=502, content={
            'status': 'failed', 'error': str(e)[:200]})
    status = state.get('status')
    if status == 'ready':
        return state
    if status == 'uploading':
        return app.JSONResponse(status_code=202, content=state)
    if status == 'disabled':
        return app.JSONResponse(status_code=404, content=state)
    return app.JSONResponse(status_code=502, content=state)


@router.delete('/api/tasks/{task_id}')
def cancel_task(task_id: str, request: app.Request) -> dict:
    """进行中的任务 → 请求取消并保留记录；已结束的任务 → 连同文件一起清理。"""
    task = app._require_owned_task(task_id, request)
    if task.is_finished:
        app.store.remove(task_id)
        return {'task_id': task_id, 'canceled': False, 'removed': True}
    return {'task_id': task_id, 'canceled': app.store.request_cancel(task_id), 'removed': False}

@router.post('/api/tasks/{task_id}/pause')
def pause_task(task_id: str, request: app.Request) -> dict:
    """暂停正在下载的任务——保留 .part 文件，后续可断点续传。

    安全审计 2026-10-11（P2-19）：`pausing` 是「暂停信号已发出、尚未被进度回调
    消费」的中间态（例如正卡在合并阶段）。它必须可被重复点击、也必须可被 resume
    接管，否则任务会永久停在 `pausing`。
    """
    task = app._require_owned_task(task_id, request)
    if task.is_finished:
        return {'task_id': task_id, 'paused': False, 'message': '任务已结束，无法暂停'}
    # 图片任务（kind=image）：单文件直传、无 .part 续传，暂停没有恢复语义 ——
    # 若照样置 pausing，没有任何执行器消费该标志，任务会永久卡在中间态（P2-19 同款坑）。
    if getattr(task, 'kind', 'video') == 'image':
        return {'task_id': task_id, 'paused': False,
                'message': '图片下载不支持暂停，可取消后重新下载'}
    if task.status in ('paused', 'pausing'):
        return {'task_id': task_id, 'paused': True, 'message': '已暂停'}
    task.pause_requested = True
    task.add_step('下载音视频', 'pending', '正在暂停…')
    app.store.update(task.id, status='pausing')
    return {'task_id': task_id, 'paused': True}

@router.post('/api/tasks/{task_id}/resume')
def resume_task(task_id: str, request: app.Request) -> dict:
    """继续被暂停的下载——yt-dlp 自动从已下载的 .part 文件断点续传。

    安全审计 2026-10-11（P2-19）：同时接受 `pausing` —— 合并阶段的暂停信号不会被
    进度回调消费，任务可能长期停在 `pausing`；此时必须允许「继续」把它拉回来
    （否则用户既等不到暂停、也点不动继续）。
    """
    task = app._require_owned_task(task_id, request)
    if task.status not in ('paused', 'pausing'):
        return {'task_id': task_id, 'resumed': False, 'message': '任务未处于暂停状态'}
    # 图片任务永远不会进入 paused/pausing（pause 入口已挡），这里兜底拒绝，
    # 避免把图片任务错交给 yt-dlp 续传逻辑。
    if getattr(task, 'kind', 'video') == 'image':
        return {'task_id': task_id, 'resumed': False, 'message': '图片下载不支持暂停/继续'}
    # 🔴 安全审计 2026-10-11（P1-5）：恢复同样要过配额门 —— 否则「暂停 → 恢复」
    #    是一条完全免闸的循环通道（配额用尽也能继续下载）。这里**不重复计费**：
    #    任务创建时已计过 1 次，恢复只是同一份传输的继续（yt-dlp 走 .part 续传）。
    _err = _download_gate_error(request)
    if _err:
        raise app.HTTPException(status_code=402, detail='MEMBER_QUOTA|' + _err)
    task.pause_requested = False
    task.add_step('下载音视频', 'running', '继续下载…')
    task.log('用户继续下载（断点续传）')
    app.store.update(task.id, status='downloading')
    app.scheduler.submit(app.downloader.run_download, task, app.store, task.quality_key, '', '', app.SINGLE_DOWNLOAD_RETRIES)
    return {'task_id': task_id, 'resumed': True}


# --------------------------------------------------------------------------- #
# 运维可观测性（App 内看板）：本地错误日志 + 本机代理拉取 ECS 网站数据
# --------------------------------------------------------------------------- #
# 设计要点（防「放 App 撑爆」）：
#   - 网站访客 / 错误事件**全部在 ECS**，App 只做「带密钥代理 + 前端展示」，
#     不落盘、不缓存全量；每次拉取 ECS 现解析 nginx 日志约 0.05s，返回几 KB。
#   - 本地仅记 App 自身的前端 JS 报错 + 服务端未捕获异常到 .ops_events.log
#     （2MB×3 轮转的 JSON 行），规模极小。
_OPS_ADMIN_KEY_FILE = app.Path.home() / ".video-downloader" / "ops_admin_key"
_OPS_ECS_BASE = (app.os.environ.get("VDL_OPS_ECS_BASE") or "http://8.138.223.3:8888").strip()
_OPS_LOCAL_LOG = app.Path.home() / ".video-downloader" / ".ops_events.log"
_OPS_MAX_BYTES = 2 * 1024 * 1024


def _ops_range_start(range_key: str):
    """看板时间窗 key → CST 时区起始 datetime；非法/空返回 None（不过滤，兼容旧调用）。

    day=今天 00:00 CST / 3d=近三天 / week=近七天 / month=近三十天。
    """
    from datetime import datetime, timedelta, timezone
    if not range_key or range_key not in ("day", "3d", "week", "month"):
        return None
    _tz = timezone(timedelta(hours=8))
    now = datetime.now(_tz)
    if range_key == "3d":
        return now - timedelta(days=3)
    if range_key == "week":
        return now - timedelta(days=7)
    if range_key == "month":
        return now - timedelta(days=30)
    # day：今天 00:00 CST
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


_OPS_RANGE_LABELS = {"day": "每天", "3d": "三天", "week": "每周", "month": "每月", "": "全部"}


def _ops_keychain_key() -> str:
    """从 macOS 钥匙串读取运维（超级管理员）密钥（service=vdl-ops-admin）。无则返回空。"""
    try:
        import subprocess
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "vdl-ops-admin", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return ""


def _ops_admin_key() -> str:
    """运维（超级管理员）密钥优先级：环境变量 > 本机钥匙串 > 本机文件 > 空。

    安全约束：二进制内**不内置**任何可用密钥。网页控制台由用户手输密钥；App 侧从
    本机钥匙串读取（设置里贴一次即可），外人拿到安装包也无法拿到密钥，拉 ECS 数据
    一律 403。ECS 侧通过 VDL_ADMIN_KEY 环境变量持有同一把超级管理员密钥。
    """
    env = (app.os.environ.get("VDL_ADMIN_KEY") or "").strip()
    if env:
        return env
    kc = _ops_keychain_key()
    if kc:
        return kc
    try:
        if _OPS_ADMIN_KEY_FILE.exists():
            k = _OPS_ADMIN_KEY_FILE.read_text(encoding="utf-8").strip()
            if k:
                return k
    except Exception:
        pass
    return ""


def _is_local_request(request) -> bool:
    """桌面端本机 WebView 经 127.0.0.1 访问，视为可信。

    安全审计 2026-10-11：
      · P1-6：此前把 `host == ""`（拿不到来源地址）也算本机 —— 「无法确定来源」被
        当成「可信」，是最不该有的默认。现要求**显式**回环地址。
      · P3-1：改用共享真源 `app._is_local_direct` —— 单看 `client.host` 会被
        `X-Forwarded-For` 伪造（uvicorn 的 proxy-headers 会用它重写 client），
        故还要求**未携带任何代理头**。本机直连的 WebView 不带代理头。
    """
    return app._is_local_direct(request)


def _require_local(request):
    """App 内看板代理/页面仅限本机 WebView 访问，防远端滥用。"""
    if not _is_local_request(request):
        raise app.HTTPException(status_code=403, detail="仅限本机访问")


def _ops_requester_is_admin(request) -> bool:
    """登录账号级超管门禁：当前请求必须携带有效登录 token 且该账号 is_admin=True。

    bearer token 即 user_id（与 /api/auth/me 同源判定，auth_store.user_is_admin）。
    机器级钥匙串密钥只是第二因子——**换了普通账号登录，有密钥也进不去**。
    """
    try:
        from auth_store import token_from_header, user_is_admin
        uid = token_from_header(request.headers.get("Authorization"))
        return bool(uid and user_is_admin(uid))
    except Exception:
        return False


def _require_admin(request):
    """运维端点鉴权：本机放行；远端必须带正确 X-Admin-Key。

    安全审计 2026-10-11（P1-6）：此前判定写的是 `key == _ops_admin_key()`，
    而服务端**未配置任何密钥**时 `_ops_admin_key()` 返回 `""` ——
    于是远端请求只要不带 `X-Admin-Key`（或带空值）就满足 `"" == ""`，
    运维端点（事件日志 / 诊断包导出 / 授权中心代理）**完全无鉴权**。
    现在：期望密钥为空时直接拒绝（fail-closed），并改用恒定时间比较。
    """
    if _is_local_request(request):
        return
    expected = _ops_admin_key()
    if not expected:
        raise app.HTTPException(
            status_code=503,
            detail="运维密钥未配置，已拒绝访问（请设置 VDL_ADMIN_KEY 或写入本机钥匙串 vdl-ops-admin）",
        )
    key = (request.headers.get("X-Admin-Key") or "").strip()
    if key and hmac.compare_digest(key, expected):
        return
    raise app.HTTPException(status_code=403, detail="需要管理员密钥（X-Admin-Key）")


def _ops_append_local(level: str, message: str, extra: dict = None) -> None:
    """把一条本地事件追加到 .ops_events.log（JSON 行，轮转）；任何异常静默。"""
    try:
        _OPS_LOCAL_LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": int(app.time.time()), "level": level, "message": str(message)[:3000]}
        if extra:
            rec["extra"] = extra
        line = app.json.dumps(rec, ensure_ascii=False)
        if _OPS_LOCAL_LOG.exists() and _OPS_LOCAL_LOG.stat().st_size > _OPS_MAX_BYTES:
            # 简单轮转：保留后 60%（避免一次性读全量）
            try:
                _txt = _OPS_LOCAL_LOG.read_text(encoding="utf-8", errors="replace")
                _lines = [l for l in _txt.splitlines() if l.strip()][-3000:]
                _OPS_LOCAL_LOG.write_text("\n".join(_lines) + "\n", encoding="utf-8")
            except Exception:
                pass
        with _OPS_LOCAL_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


@router.post('/api/client-error')
def client_error(payload: dict, request: app.Request):
    """前端 JS 运行期错误上报（window.onerror / unhandledrejection）。

    让 App 自身前端报错也能在服务端 .ops_events.log 查到，闭环「用户报问题我们看不到记录」。
    """
    app._check_rate_limit(request)
    msg = (payload.get("message") or "").strip()[:2000]
    if not msg:
        raise app.HTTPException(status_code=400, detail="缺少 message")
    extra = {
        "stack": (payload.get("stack") or "")[:3000],
        "url": (payload.get("url") or "")[:500],
        "line": payload.get("line"),
        "col": payload.get("col"),
        "level": payload.get("level", "error"),
    }
    _ops_append_local("error", msg, extra)
    return {"ok": True}


@router.get('/api/admin/events')
def admin_events(request: app.Request, limit: int = 200, level: str = "", range: str = ""):
    """读取本地结构化事件日志（App 自身错误，运维视图）。本机免密钥，远端需 X-Admin-Key。
    range 时间窗同 /api/admin/visits；空=全部（兼容 diagnostic 旧调用）。"""
    _require_admin(request)
    from datetime import datetime, timezone, timedelta
    threshold = _ops_range_start(range)
    items = []
    if _OPS_LOCAL_LOG.exists():
        try:
            for ln in _OPS_LOCAL_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = app.json.loads(ln)
                except Exception:
                    continue
                if level and rec.get("level") != level:
                    continue
                if threshold is not None:
                    ts = rec.get("ts")
                    if ts is not None:
                        try:
                            _dt = datetime.fromtimestamp(ts, timezone(timedelta(hours=8)))
                            if _dt < threshold:
                                continue
                        except Exception:
                            pass
                items.append(rec)
        except Exception:
            pass
    items = items[-max(1, min(int(limit), 2000)):]
    return {"count": len(items), "events": items, "range": range or "all", "range_label": _OPS_RANGE_LABELS.get(range, "全部")}


def _collect_local_diag() -> dict:
    """聚合 App 本机诊断文件（launch.log）。

    注：功能统计文件由 stats 模块独占写入，本诊断包只读 launch.log +
    本地错误日志，避免与功能模块共享同一配置文件字面量触发隔离检测冲突。
    """
    out: dict = {}
    launch = app.Path.home() / ".vdl_launch.log"
    if launch.exists():
        try:
            txt = launch.read_text(encoding="utf-8", errors="replace")
            out["launch_log_tail"] = "\n".join(txt.splitlines()[-120:])
            out["launch_log_size"] = launch.stat().st_size
        except Exception:
            pass
    return out


@router.get('/api/diagnostic')
def diagnostic(request: app.Request):
    """导出诊断信息包：版本指纹 + 近期事件 + 本机日志/统计。本机免密钥，远端需 X-Admin-Key。"""
    _require_admin(request)
    data: dict = {"generated_at": int(app.time.time())}
    try:
        data["version"] = api_version()
    except Exception as e:
        data["version"] = {"error": str(e)}
    try:
        data["recent_events"] = admin_events(request, limit=50).get("events", [])
    except Exception:
        data["recent_events"] = []
    data["local_diag"] = _collect_local_diag()
    return app.JSONResponse(content=data)


_NGINX_ACCESS_LOG = "/var/log/nginx/access.log"
_STATIC_SUFFIX = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".ico",
                  ".woff", ".woff2", ".ttf", ".svg", ".map", ".json")


def _nginx_log_paths(max_files: int = 40) -> list:
    """当前 + 轮转的历史访问日志路径，**按时间从旧到新**排列。

    🔴 2026-10-03：nginx 每天轮转（access.log 只有当天，昨天的在 access.log.1，
    更早是 .gz）。原来只读 access.log，于是「三天/每周/每月」拿到的都是当天数据
    —— 四个范围看起来一模一样，用户以为筛选没生效。
    """
    import pathlib as _pl

    base = _pl.Path(_NGINX_ACCESS_LOG)
    older = []
    idx = 1
    while idx <= max_files:
        gz = base.with_name(base.name + f".{idx}.gz")
        plain = base.with_name(base.name + f".{idx}")
        if gz.exists():
            older.append(gz)
        elif plain.exists():
            older.append(plain)
        else:
            break
        idx += 1
    older.reverse()          # 最旧在前
    return older + ([base] if base.exists() else [])


def _read_nginx_log_lines(per_file_cap: int = 20000) -> list:
    """读全部访问日志行（.gz 自动解压），每文件只取末尾若干行防爆内存。"""
    import gzip as _gz

    out: list = []
    for p in _nginx_log_paths():
        try:
            if p.suffix == ".gz":
                with _gz.open(p, "rt", encoding="utf-8", errors="replace") as f:
                    out.extend(f.readlines()[-per_file_cap:])
            else:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    out.extend(f.readlines()[-per_file_cap:])
        except Exception:
            continue          # 单个文件读不动就跳过，不影响其它
    return out


# 机器/脚本 UA 特征（我们自己 App 的轮询、脚本、扫描器等）
_MACHINE_UA_MARKS = (
    "python-requests", "python-urllib", "curl/", "wget", "Go-http-client",
    "okhttp", "Java/", "l9explore", "axios", "node-fetch", "PostmanRuntime",
    "python-http-client", "aiohttp", "scrapy", "HeadlessChrome", "bot",
)

# 🔴 2026-10-03 追加：把「机器 UA」再拆成「我们自己桌面 App」与「其它客户端」。
# 桌面 App 走 license_client(requests)/urllib 打授权、任务、Cookie 等接口；
# 剩下的 curl/扫描器/未知 UA 归 other，别混进 App 的账里。
_APP_UA_MARKS = (
    "python-requests", "python-urllib", "python-http-client", "VideoDownloader",
    "okhttp", "aiohttp",
)


def _is_machine_ua(ua: str) -> bool:
    """UA 是否属于机器/脚本（不是真人浏览器）。"""
    s = str(ua or "")
    return any(mark in s for mark in _MACHINE_UA_MARKS)


def _is_app_ua(ua: str) -> bool:
    """UA 是否为我们自己的桌面 App（与其它脚本/扫描器区分开）。"""
    s = str(ua or "")
    return any(mark in s for mark in _APP_UA_MARKS)


def _is_page_path(path: str) -> bool:
    """是否「页面级」请求：排除接口、网关、内部路径与静态资源。"""
    p = str(path or "")
    if not p or p.startswith("/api/") or p.startswith("/gw/") or p.startswith("/internal"):
        return False
    if p.endswith(_STATIC_SUFFIX):
        return False
    return True


@router.get("/api/admin/visits")
def admin_visits(request: app.Request, limit: int = 100, range: str = ""):
    """网站访客汇总：解析 nginx access.log（仅 ECS 有；本机无 nginx 时优雅返回空）。

    range 时间窗：day=今天 / 3d=近三天 / week=近七天 / month=近三十天；空=全部（兼容旧调用）。
    桌面 App 看网站数据请走 /api/app/ops-visits（代理 ECS），本端点用于 ECS 本机直查。
    """
    _require_admin(request)
    from datetime import datetime, timedelta, timezone
    threshold = _ops_range_start(range)
    import re as _re
    from collections import Counter
    line_re = _re.compile(
        r'^(?P<ip>\S+) \S+ \S+ \[(?P<t>[^\]]+)\] "(?P<m>\S+) (?P<p>\S+) [^"]*" '
        r'(?P<s>\d{3}) (?P<sz>\S+) "(?P<ref>[^"]*)" "(?P<ua>[^"]*)"'
    )
    total = 0
    ips = set()
    by_status = Counter()
    by_method = Counter()
    # 真人访客口径（2026-10-03）：原口径统计**所有** HTTP 请求，其中大部分是
    # 接口调用 + 我们自己 App 的轮询，且独立 IP 里混着机主自己的浏览器与机器 IP
    # —— 看数字无法判断「今天来了几个真人」。这里另算一套：浏览器 UA + 页面级路径。
    human_pv = 0
    human_uv = set()
    human_ips = set()
    human_pages = Counter()
    machine_requests = 0
    # 按客户端拆分（2026-10-03）：桌面 App / 网页页面 / 网页接口轮询 / 其它客户端
    app_reqs = 0
    app_ips = set()
    web_api_reqs = 0
    web_api_ips = set()
    other_reqs = 0
    other_ips = set()
    top_paths = Counter()
    top_ips = Counter()
    recent = []
    try:
        raw_lines = _read_nginx_log_lines()
    except Exception as e:
        return {"error": f"无法读取访问日志（本机非服务器，无 nginx）：{e}", "total": 0,
                "source": "local-desktop-no-nginx"}
    if not raw_lines:
        return {"error": "访问日志为空或不可读（本机非服务器，无 nginx）", "total": 0,
                "source": "local-desktop-no-nginx"}
    for ln in raw_lines:
        m = line_re.search(ln)
        if not m:
            continue
        if threshold is not None:
            try:
                _dt = datetime.strptime(m.group("t"), "%d/%b/%Y:%H:%M:%S %z")
                if _dt < threshold:
                    continue
            except Exception:
                pass
        total += 1
        ip = m.group("ip")
        path = m.group("p")
        status = m.group("s")
        ips.add(ip)
        by_status[status] += 1
        by_method[m.group("m")] += 1
        top_ips[ip] += 1
        if not path.lower().endswith(_STATIC_SUFFIX):
            top_paths[path] += 1
        recent.append({
            "t": m.group("t"), "ip": ip, "m": m.group("m"),
            "p": path, "s": int(status), "ua": m.group("ua")[:140],
        })
        # ---- 真人访客口径 + 按客户端拆分 ----
        ua = m.group("ua") or ""
        is_browser = ("Mozilla" in ua) and not _is_machine_ua(ua)
        if _is_app_ua(ua):
            # 桌面 App（我们的 python 客户端）：几乎全是接口轮询
            app_reqs += 1
            app_ips.add(ip)
            machine_requests += 1
        elif is_browser:
            if _is_page_path(path):
                human_pv += 1
                human_ips.add(ip)
                human_uv.add((ip, hash(ua) & 0xFFFF))
                human_pages[path] += 1
            else:
                # 浏览器打的接口/静态资源 = 网页版前端的轮询
                web_api_reqs += 1
                web_api_ips.add(ip)
                machine_requests += 1
        else:
            other_reqs += 1
            other_ips.add(ip)
            machine_requests += 1
    return {
        "total": total, "unique_ips": len(ips),
        "by_status": dict(by_status.most_common()),
        "by_method": dict(by_method.most_common()),
        "top_paths": [{"path": k, "count": v} for k, v in top_paths.most_common(20)],
        "top_ips": [{"ip": k, "count": v} for k, v in top_ips.most_common(15)],
        "recent": recent[-max(1, min(int(limit), 200)):],
        "source": _NGINX_ACCESS_LOG,
        "log_files": len(_nginx_log_paths()),
        "human": {
            "pv": human_pv,
            "uv": len(human_uv),
            "ips": len(human_ips),
            "machine_requests": machine_requests,
            "top_pages": [{"path": k, "count": v} for k, v in human_pages.most_common(10)],
            "note": "真人访客 = 浏览器 UA 且访问页面级路径；已排除 /api /gw /internal 接口、静态资源与机器请求（App 轮询/健康检查/脚本）",
        },
        "by_client": {
            "desktop_app": {"requests": app_reqs, "ips": len(app_ips)},
            "web_page": {"requests": human_pv, "uv": len(human_uv), "ips": len(human_ips)},
            "web_api": {"requests": web_api_reqs, "ips": len(web_api_ips)},
            "other": {"requests": other_reqs, "ips": len(other_ips)},
            "note": "桌面 App=我们的 python 客户端轮询；网页=浏览器 UA（页面 / 接口轮询分开）；其它=curl、扫描器、未知 UA",
        },
        "range": range or "all",
        "range_label": _OPS_RANGE_LABELS.get(range, "全部"),
    }


@router.get("/ops")
def ops_console(request: app.Request):
    """运维控制台页面（web 版用）。本机 WebView 加载 /ops-board 即可看板。"""
    html_path = app.WEB_DIR / "ops" / "index.html"
    try:
        html = html_path.read_text(encoding="utf-8")
    except Exception as e:
        return app.Response(f"<h1>运维控制台页面缺失</h1><p>{e}</p>", media_type="text/html", status_code=500)
    return app.Response(html, media_type="text/html")


_OPS_KEY_GATE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>超级管理员验证 · 视频工坊</title>
<style>
  :root{--bg:#0f1320;--panel:#171c2e;--line:#2a3350;--txt:#e7ecf5;--muted:#9aa6c2;--accent:#4f8cff;--green:#3ecf8e;--red:#ff6b6b}
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:var(--bg);color:var(--txt);font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:28px;width:min(420px,92vw)}
  h1{font-size:17px;margin:0 0 6px}
  p.desc{color:var(--muted);font-size:13px;margin:0 0 16px}
  input{width:100%;background:#0b0f1a;border:1px solid var(--line);border-radius:8px;color:var(--txt);padding:10px 12px;font-size:14px;outline:none}
  input:focus{border-color:var(--accent)}
  button{margin-top:12px;width:100%;background:var(--accent);border:none;border-radius:8px;color:#fff;padding:10px;font-size:14px;cursor:pointer}
  button:disabled{opacity:.6;cursor:default}
  .hint{margin-top:12px;font-size:12.5px;color:var(--muted);min-height:18px}
  .hint.err{color:var(--red)} .hint.ok{color:var(--green)}
</style>
</head>
<body>
  <div class="card">
    <h1>🔒 运维看板 · 仅超级管理员可见</h1>
    <p class="desc">请粘贴超级管理员密钥。密钥会先在服务器实测校验，通过后保存到本机钥匙串，之后无需重复输入。</p>
    <input id="keyInput" type="password" placeholder="粘贴超级管理员密钥" autocomplete="off">
    <button id="saveBtn">验证并进入看板</button>
    <div class="hint" id="hint"></div>
  </div>
<script>
const $=s=>document.querySelector(s);
$('#saveBtn').addEventListener('click',async()=>{
  const key=($('#keyInput').value||'').trim();
  const h=$('#hint');
  if(!key){h.textContent='请先粘贴密钥';h.className='hint err';return;}
  $('#saveBtn').disabled=true;h.textContent='正在校验密钥…';h.className='hint';
  try{
    const v=await fetch('/api/app/ops-key-verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key})});
    const vd=await v.json();
    if(v.status===403){h.textContent='仅超级管理员账号登录后才可验证：请回到主界面用超级管理员账号登录后再试';h.className='hint err';return;}
    if(!(v.ok&&vd.ok)){h.textContent='密钥无效：服务器拒绝了这把密钥（HTTP '+(vd.status||v.status)+'）';h.className='hint err';return;}
    const s=await fetch('/api/app/ops-key',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key})});
    const sd=await s.json();
    if(s.ok&&sd.ok){h.textContent='✅ 校验通过，已保存到本机钥匙串，正在进入看板…';h.className='hint ok';setTimeout(()=>location.reload(),700);}
    else{h.textContent='密钥有效但保存失败：'+(sd.detail||('HTTP '+s.status));h.className='hint err';}
  }catch(e){h.textContent='校验失败：'+(e&&e.message?e.message:e);h.className='hint err';}
  finally{$('#saveBtn').disabled=false;}
});
$('#keyInput').addEventListener('keydown',e=>{if(e.key==='Enter')$('#saveBtn').click();});
// 账号级门禁：非超级管理员登录时隐藏密钥输入，只提示换账号。
(async()=>{
  try{
    const tok=localStorage.getItem('vdl_auth_token')||'';
    const r=await fetch('/api/auth/me',{headers:tok?{'Authorization':'Bearer '+tok}:{}});
    const me=await r.json();
    if(!(me&&me.ok&&me.is_admin)){
      $('#keyInput').style.display='none';$('#saveBtn').style.display='none';
      const h=$('#hint');h.textContent='当前登录的不是超级管理员账号：请回到主界面「账号」中切换为超级管理员账号，再连点版本号 5 次进入';
      document.querySelector('.desc').textContent='此页面仅对超级管理员账号开放。';
    }
  }catch(_){}
})();
</script>
</body>
</html>"""


@router.get("/ops-board")
def ops_board(request: app.Request):
    """App 内运维看板页面：WKWebView 同源加载本机 /ops-board，fetch /api/app/*。

    超级管理员门禁：本机未配置运维密钥时只返回密钥验证页（不含看板与任何数据）；
    配置后才能进入看板。数据接口 /api/app/ops-* 仍独立校验密钥，双保险。
    """
    _require_local(request)
    if not _ops_admin_key():
        return app.Response(_OPS_KEY_GATE_HTML, media_type="text/html")
    html_path = app.WEB_DIR / "ops" / "board.html"
    try:
        html = html_path.read_text(encoding="utf-8")
    except Exception as e:
        return app.Response(f"<h1>运维看板页面缺失</h1><p>{e}</p>", media_type="text/html", status_code=500)
    return app.Response(html, media_type="text/html")


@router.get("/api/app/ops-visits")
def app_ops_visits(request: app.Request, limit: int = 100, range: str = ""):
    """App 内看板代理：本机放行，带密钥去 ECS 拉「网站访客」汇总并转发前端。

    数据仍在 ECS（nginx 日志），App 不落盘、不缓存——纯代理展示，不会撑爆 App。
    """
    _require_local(request)
    if not _ops_requester_is_admin(request):
        raise app.HTTPException(status_code=403, detail="仅管理员账号登录后可用（管理员为只读）")
    if not _ops_admin_key():
        raise app.HTTPException(status_code=401, detail="未配置运维密钥：请在「关于本应用」设置中填写超级管理员密钥")
    try:
        resp = app.requests.get(
            f"{_OPS_ECS_BASE}/api/admin/visits",
            params={"limit": max(1, min(int(limit), 200)), "range": range},
            headers={"X-Admin-Key": _ops_admin_key()},
            timeout=15,
        )
        return app.JSONResponse(content=resp.json(), status_code=resp.status_code)
    except Exception as e:
        raise app.HTTPException(status_code=502, detail=f"拉取 ECS 访客数据失败：{e}")


@router.get("/api/app/ops-events")
def app_ops_events(request: app.Request, limit: int = 200, level: str = "", range: str = ""):
    """App 内看板代理：本机放行，带密钥去 ECS 拉「错误/异常事件」并转发前端。"""
    _require_local(request)
    if not _ops_requester_is_admin(request):
        raise app.HTTPException(status_code=403, detail="仅管理员账号登录后可用（管理员为只读）")
    if not _ops_admin_key():
        raise app.HTTPException(status_code=401, detail="未配置运维密钥：请在「关于本应用」设置中填写超级管理员密钥")
    try:
        resp = app.requests.get(
            f"{_OPS_ECS_BASE}/api/admin/events",
            params={"limit": max(1, min(int(limit), 2000)), "level": level, "range": range},
            headers={"X-Admin-Key": _ops_admin_key()},
            timeout=15,
        )
        return app.JSONResponse(content=resp.json(), status_code=resp.status_code)
    except Exception as e:
        raise app.HTTPException(status_code=502, detail=f"拉取 ECS 事件数据失败：{e}")


@router.get("/api/app/ops-key-status")
def app_ops_key_status(request: app.Request):
    """本机查看是否已配置运维密钥（不返回明文）。"""
    _require_local(request)
    return {"configured": bool(_ops_admin_key())}


@router.post("/api/app/ops-key")
def app_ops_key_save(payload: dict, request: app.Request):
    """本机保存运维（超级管理员）密钥到 macOS 钥匙串（不进二进制、不写明文文件）。

    仅本机 WebView 可调用；且**当前登录账号必须是超级管理员**（is_admin）才允许写入；
    Keychain 首次访问可能弹授权，允许一次即可。
    """
    _require_local(request)
    if not _ops_requester_is_admin(request):
        raise app.HTTPException(status_code=403, detail="仅超级管理员账号登录后可配置密钥")
    key = (payload.get("key") or "").strip()
    if not key:
        raise app.HTTPException(status_code=400, detail="密钥为空")
    try:
        import subprocess
        subprocess.run(["security", "delete-generic-password", "-s", "vdl-ops-admin"],
                       capture_output=True, text=True, timeout=5)
        r = subprocess.run(["security", "add-generic-password", "-a", "vdl", "-s", "vdl-ops-admin", "-w", key],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or "security 写入失败")
    except Exception as e:
        raise app.HTTPException(status_code=500, detail=f"保存密钥到钥匙串失败：{e}")
    return {"ok": True, "stored": "keychain"}


@router.post("/api/app/ops-key-verify")
def app_ops_key_verify(payload: dict, request: app.Request):
    """校验一把密钥是否为有效超级管理员密钥（拿去 ECS 实测一次，不落盘）。仅本机可调。

    验证页「验证并进入看板」用：先确认密钥有效，再允许保存进钥匙串，防止存进废钥匙。
    非超级管理员账号调用一律 403——普通用户即使拿到密钥也验证不了。
    """
    _require_local(request)
    if not _ops_requester_is_admin(request):
        raise app.HTTPException(status_code=403, detail="仅管理员账号登录后可用（管理员为只读）")
    key = (payload.get("key") or "").strip()
    if not key:
        raise app.HTTPException(status_code=400, detail="密钥为空")
    try:
        resp = app.requests.get(
            f"{_OPS_ECS_BASE}/api/admin/events",
            params={"limit": 1},
            headers={"X-Admin-Key": key},
            timeout=10,
        )
        return {"ok": resp.status_code == 200, "status": resp.status_code}
    except Exception as e:
        raise app.HTTPException(status_code=502, detail=f"无法连接校验服务：{e}")


# ── 授权中心异常告警（2026-09-25 超管实时监控）──────────────────────────────── #
# 链路：App 前端(admin 登录) → 本机 /api/app/license-alerts（is_admin 门禁）
#       → ECS worker /api/license-alerts（X-Admin-Key）→ 本机 8902 license alerts。
# 告警在授权中心生成：大额充值 / 连刷 / 卡密爆破 / 负余额（见 deploy/license_server.py）。

@router.get("/api/app/license-alerts")
def app_license_alerts(request: app.Request, since: float = 0.0,
                       unseen_only: bool = False, limit: int = 100):
    """拉取授权中心异常告警（超管账号 + 本机双门禁）。前端 60s 轮询用。"""
    _require_local(request)
    if not _ops_requester_is_admin(request):
        raise app.HTTPException(status_code=403, detail="仅管理员账号登录后可用（管理员为只读）")
    try:
        resp = app.requests.get(
            f"{_OPS_ECS_BASE}/api/license-alerts",
            params={"since": since, "unseen_only": "true" if unseen_only else "false",
                    "limit": max(1, min(int(limit), 200))},
            headers={"X-Admin-Key": _ops_admin_key()},
            timeout=12,
        )
        return app.JSONResponse(content=resp.json(), status_code=resp.status_code)
    except Exception as e:
        raise app.HTTPException(status_code=502, detail=f"拉取授权中心告警失败：{e}")


@router.post("/api/app/license-alerts/ack")
def app_license_alerts_ack(payload: dict, request: app.Request):
    """确认（已读）告警。ids 为空数组 = 全部确认。"""
    _require_local(request)
    if not _ops_requester_is_admin(request):
        raise app.HTTPException(status_code=403, detail="仅管理员账号登录后可用（管理员为只读）")
    try:
        resp = app.requests.post(
            f"{_OPS_ECS_BASE}/api/license-alerts/ack",
            json={"ids": list(payload.get("ids") or [])},
            headers={"X-Admin-Key": _ops_admin_key()},
            timeout=12,
        )
        return app.JSONResponse(content=resp.json(), status_code=resp.status_code)
    except Exception as e:
        raise app.HTTPException(status_code=502, detail=f"确认告警失败：{e}")


@router.post("/api/app/license-alerts/notify")
def app_license_alerts_notify(payload: dict, request: app.Request):
    """macOS 系统通知桥：前端轮询发现新告警时调用，弹系统级通知（免解锁也可见）。"""
    _require_local(request)
    if not _ops_requester_is_admin(request):
        raise app.HTTPException(status_code=403, detail="仅管理员账号登录后可用（管理员为只读）")
    import subprocess
    title = str(payload.get("title") or "视频工坊·异常告警")[:60]
    body = str(payload.get("body") or "")[:200]
    try:
        script = f'display notification "{body}" with title "{title}" sound name "Glass"'
        r = subprocess.run(["osascript", "-e", script], capture_output=True,
                           text=True, timeout=6)
        return {"ok": r.returncode == 0, "err": (r.stderr or "").strip()[:200]}
    except Exception as e:
        return {"ok": False, "err": str(e)[:200]}


# ── 反破解风控（2026-10-14 第 1 批纯观测）──────────────────────────────────── #
# 链路：看板(admin 登录) → 本机 /api/app/license-risk/*（is_admin 门禁）
#       → ECS worker /api/license-risk/*（X-Admin-Key）→ 本机 8902 license risk_*。
# 🔴 本批【只读+人工抹平】，不含任何自动响应；封禁仍走管理端 ban（永远人工）。

@router.get("/api/app/license-risk/top")
def app_license_risk_top(request: app.Request, limit: int = 50, min_score: int = 1):
    """风险最高的账号列表（看板「风险复核队列」数据源）。"""
    _require_local(request)
    if not _ops_requester_is_admin(request):
        raise app.HTTPException(status_code=403, detail="仅管理员账号登录后可用（管理员为只读）")
    try:
        resp = app.requests.get(
            f"{_OPS_ECS_BASE}/api/license-risk/top",
            params={"limit": max(1, min(int(limit), 200)),
                    "min_score": max(1, int(min_score))},
            headers={"X-Admin-Key": _ops_admin_key()},
            timeout=20,
        )
        return app.JSONResponse(content=resp.json(), status_code=resp.status_code)
    except Exception as e:
        raise app.HTTPException(status_code=502, detail=f"拉取风险队列失败：{e}")


@router.post("/api/app/license-risk/query")
def app_license_risk_query(payload: dict, request: app.Request):
    """单账号风险画像（命中信号明细）。"""
    _require_local(request)
    if not _ops_requester_is_admin(request):
        raise app.HTTPException(status_code=403, detail="仅管理员账号登录后可用（管理员为只读）")
    try:
        resp = app.requests.post(
            f"{_OPS_ECS_BASE}/api/license-risk/query",
            json={"email": str(payload.get("email") or "")},
            headers={"X-Admin-Key": _ops_admin_key()},
            timeout=15,
        )
        return app.JSONResponse(content=resp.json(), status_code=resp.status_code)
    except Exception as e:
        raise app.HTTPException(status_code=502, detail=f"查询风险画像失败：{e}")


@router.post("/api/app/license-risk/clear")
def app_license_risk_clear(payload: dict, request: app.Request):
    """人工抹平（确认误报）：清空该账号风险摘要，并写 risk_clear 审计事件。"""
    _require_local(request)
    if not _ops_requester_is_admin(request):
        raise app.HTTPException(status_code=403, detail="仅管理员账号登录后可用（管理员为只读）")
    try:
        resp = app.requests.post(
            f"{_OPS_ECS_BASE}/api/license-risk/clear",
            json={"email": str(payload.get("email") or ""),
                  "note": str(payload.get("note") or "")},
            headers={"X-Admin-Key": _ops_admin_key()},
            timeout=15,
        )
        return app.JSONResponse(content=resp.json(), status_code=resp.status_code)
    except Exception as e:
        raise app.HTTPException(status_code=502, detail=f"清除风险失败：{e}")


# 查账区间（2026-10-09 用户要求「入账可查 1 天/3 天/7 天/1 个月/半年/1 年」）：
# 默认 7 天（与授权中心自扫窗口一致），上限 400 天（前端最大 365 = 近 1 年）。
# 🔴 与 ECS worker `server/routers/core.py`、授权中心 `deploy/license_server.py`
#    的 RECON_DAYS_MAX **必须同值**，否则大区间被上游先截断且不报错。
_RECON_DAYS_DEFAULT = 7
_RECON_DAYS_MAX = 400


@router.post("/api/app/license-recon")
def app_license_recon(payload: dict, request: app.Request):
    """每日入账/充值对账报告（2026-09-26 资金核对，超管专用）。

    链路：App 前端(看板) → 本机 /api/app/license-recon（is_admin 门禁）
          → ECS worker /api/license-recon（X-Admin-Key）→ 本机 8902 license recon。
    差异（收款未发货/发货未收款/金额不符）在授权中心自动告警，本接口按需拉报告。

    `days` = 查账区间天数（前端给 1/3/7/30/180/365）。🔴 上限 `_RECON_DAYS_MAX` 与
    ECS worker 的 `core.py`、授权中心 `deploy/license_server.py` 的 `RECON_DAYS_MAX`
    是**三处同值**：任一处没放开，大区间都会被静默截断成 60 天且不报错。
    """
    _require_local(request)
    if not _ops_requester_is_admin(request):
        raise app.HTTPException(status_code=403, detail="仅管理员账号登录后可用（管理员为只读）")
    try:
        days = int(payload.get("days") or _RECON_DAYS_DEFAULT)
    except (TypeError, ValueError):
        days = _RECON_DAYS_DEFAULT
    try:
        resp = app.requests.post(
            f"{_OPS_ECS_BASE}/api/license-recon",
            json={"days": max(1, min(days, _RECON_DAYS_MAX))},
            headers={"X-Admin-Key": _ops_admin_key()},
            timeout=15,
        )
        return app.JSONResponse(content=resp.json(), status_code=resp.status_code)
    except Exception as e:
        raise app.HTTPException(status_code=502, detail=f"对账服务不可达：{e}")
