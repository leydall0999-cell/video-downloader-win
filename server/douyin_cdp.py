#!/usr/bin/env python3
"""抖音本地 CDP 解析（桌面端破 Argus 第二层 UIFID 风控）。

原理（2026-11-02 实弹验证通过）：
  抖音 PC 网页对匿名解析启用 Argus 风控（Layer1 登录态缺失 + Layer2 UIFID 设备指纹）。
  服务器侧（VPS Playwright / HTTP a_bogus）拿不到与登录态绑定的设备指纹，永远卡在
  Layer2「Uifid Not Found」。破局唯一活路 = 桌面端**系统真实 Chrome**（指纹完全真实）
  + 用户真实登录态 + CDP 监听播放器发出的 douyinvod 真实流地址。

本模块：
  - 启动/复用系统真实 Chrome（独立 profile `~/.video-downloader/douyin-cdp-profile` +
    独立调试端口，避免与 cdp_sniffer 的 cdp-profile 抢锁/抢端口）。
  - 解析抖音时：在 Chrome 里打开视频页 → CDP 监听 Network.responseReceived →
    捕获 douyinvod 的视频/音轨直链 → 构造成 downloader 兼容的 info dict。
  - 提供「扫码登录」入口：打开抖音登录页（展示二维码），用户在手机抖音扫码；
    登录态写进独立 profile，跨解析会话与 App 重启持久化。
  - 纯标准库 WebSocket 客户端（无 websockets 第三方依赖，适配 onefile 打包）。

⚠️ 仅桌面端（mac/win）可用；web 版（VDL_INSTANCE=cloud）无系统 Chrome，走降级引导。
"""
from __future__ import annotations

import base64
import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.parse

# ---------------------------------------------------------------------------
# 常量 / 路径
# ---------------------------------------------------------------------------
def _default_chrome() -> str:
    if sys.platform == "darwin":
        return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if sys.platform == "win32":
        candidates = [
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%PROGRAMFILES(x86)%\Google\Chrome\Application\chrome.exe"),
        ]
        for p in candidates:
            if p and os.path.exists(p):
                return p
        return "chrome.exe"
    # Linux（开发/测试场景）
    for p in ("/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"):
        if os.path.exists(p):
            return p
    return "google-chrome"


PORT = int(os.environ.get("VDL_DOUYIN_CDP_PORT", "9341"))
PROFILE = os.path.expanduser(
    os.environ.get("VDL_DOUYIN_CDP_PROFILE", "~/.video-downloader/douyin-cdp-profile"))
PROFILE = os.path.expanduser(PROFILE)
CHROME = os.environ.get("VDL_CHROME_BIN") or _default_chrome()
DOUYIN_HOME = "https://www.douyin.com/"

# 登录态判定用的 Cookie 名（命中任一即视为已登录）
# 🔴 不要把 odin_tt 放进来（2026-11-03 实测误判）：odin_tt / ttwid / UIFID / passport_csrf_token
# 都是**设备级** Cookie，未登录也存在 —— 放进判定会出现「显示已登录，一解析就要登录」的假阳性。
# 只有下面这些是扫码登录成功后才下发的会话 Cookie。
_LOGIN_COOKIES = ("sid_tt", "sessionid", "sessionid_ss", "sid_guard", "uid_tt")

_LOCK = threading.RLock()   # 可重入：resolve() 内部会再调 is_logged_in()
_PROC = {"proc": None}
# 附加启动参数（诊断/实验用，例如 ["--headless=new"]；正常运行为空）
_EXTRA_FLAGS: list[str] = []


# ---------------------------------------------------------------------------
# 纯标准库 WebSocket 客户端（仅处理 CDP 文本帧 + ping/pong 回复）
# ---------------------------------------------------------------------------
class _WSClient:
    def __init__(self, ws_url: str):
        assert ws_url.startswith("ws://"), ws_url
        rest = ws_url[5:]
        host, _, path = rest.partition("/")
        self.host = host
        self.port = PORT
        if ":" in host:
            self.host, p = host.split(":")
            self.port = int(p)
        self.path = "/" + path
        self.sock = socket.create_connection((self.host, self.port), timeout=15)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
               % (self.path, self.host, self.port, key))
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("handshake: connection closed")
            buf += chunk
        if b"101" not in buf.split(b"\r\n\r\n", 1)[0]:
            raise RuntimeError("handshake failed: %r" % buf[:160])

    def _recvn(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            c = self.sock.recv(n - len(data))
            if not c:
                raise RuntimeError("WS connection closed")
            data += c
        return data

    def send_text(self, data: str) -> None:
        payload = data.encode("utf-8")
        mask = os.urandom(4)
        h = bytes([0x81])
        ln = len(payload)
        if ln < 126:
            h += bytes([0x80 | ln])
        elif ln < 65536:
            h += bytes([0x80 | 126]) + struct.pack(">H", ln)
        else:
            h += bytes([0x80 | 127]) + struct.pack(">Q", ln)
        self.sock.sendall(h + mask + bytes(payload[i] ^ mask[i % 4] for i in range(ln)))

    def _send_pong(self, payload: bytes) -> None:
        mask = os.urandom(4)
        h = bytes([0x8A])
        ln = len(payload)
        if ln < 126:
            h += bytes([0x80 | ln])
        else:
            h += bytes([0x80 | 126]) + struct.pack(">H", ln)
        self.sock.sendall(h + mask + bytes(payload[i] ^ mask[i % 4] for i in range(ln)))

    def recv_frame(self):
        b = self._recvn(2)
        opcode = b[0] & 0x0F
        ln = b[1] & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self._recvn(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._recvn(8))[0]
        payload = self._recvn(ln)
        return opcode, payload

    def recv_msg(self):
        """读取一条非控制消息；遇 ping 立即回 pong（关键：否则 Chrome reset）。"""
        while True:
            op, pl = self.recv_frame()
            if op == 0x8:
                raise RuntimeError("WS closed by peer")
            if op == 0x9:  # ping
                self._send_pong(pl)
                continue
            if op == 0xA:  # pong
                continue
            return op, pl.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# HTTP 辅助
# ---------------------------------------------------------------------------
# 🔴 本地 CDP 端点必须绕开系统/环境代理：App 若在带 HTTP_PROXY 的环境里被拉起
# （终端、启动器、CI 都可能），urllib 默认会把 http://127.0.0.1:9341 也发给代理，
# 代理再回 502/超时 —— 表现为「Chrome 起不来 / 建 tab 失败 / 登录态读不出来」，
# 而实际 Chrome 好好的。
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_json(url: str, timeout: int = 10):
    with _LOCAL_OPENER.open(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _chrome_running() -> bool:
    try:
        _http_json("http://127.0.0.1:%d/json/version" % PORT, timeout=2)
        return True
    except Exception:
        return False


def _page_targets() -> list[dict]:
    """当前 Chrome 里的页面标签（type == 'page'）。空 = 无窗口僵尸。"""
    try:
        return [t for t in _http_json("http://127.0.0.1:%d/json/list" % PORT, timeout=3)
                if t.get("type") == "page"]
    except Exception:
        return []


def _kill_chrome() -> None:
    """关掉本 profile 的 Chrome（先走 CDP，再兜底按命令行杀）。

    macOS 上 Chrome 关掉所有窗口后进程会继续驻留（Dock 里那个点），
    CDP target 变成 0 个，既建不了 tab 也不会自动退出。这种"僵尸"必须先清掉。
    """
    try:
        ws = _WSClient(_browser_ws())
        ws.send_text(json.dumps({"id": 1, "method": "Browser.close"}))
        try:
            ws.sock.close()
        except Exception:
            pass
    except Exception:
        pass
    for _ in range(12):
        if not _chrome_running():
            break
        time.sleep(0.5)
    if _chrome_running():
        try:
            subprocess.run(["pkill", "-f", "--user-data-dir=%s" % PROFILE],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        for _ in range(10):
            if not _chrome_running():
                break
            time.sleep(0.5)
    _PROC["proc"] = None


# 窗口位置：解析时把窗口挪到屏幕外（用户无感），扫码登录时挪回屏幕内。
# 🔴 2026-11-03 用户实测报「点解析为什么会打开浏览器」——CDP 解析必须真的跑一个 Chrome，
# 但没必要让用户看见它。窗口位置控制在「新建 Chrome 时」决定，已运行的不动
# （否则登录页轮询 login-status 会把二维码窗口一起挪走）。
_HIDDEN_POS = "-3000,60"   # 越界值交给 macOS 钳制（实际会停在露 40px 的位置）
_VISIBLE_POS = "120,80"
_WIN_W, _WIN_H = 1100, 820


def launch_chrome(open_url: str | None = None, visible: bool = False) -> subprocess.Popen:
    """启动或复用系统 Chrome（独立 profile + 独立端口）。返回进程对象。

    visible=False（默认）= 后台静默：窗口放到屏幕外，只做解析，不打扰用户。
    visible=True = 扫码登录：窗口放回屏幕内。
    仅在需要**新建** Chrome 时生效；已运行实例不改动其窗口（见 _set_window_visible）。
    """
    if _chrome_running():
        if _page_targets():
            return _PROC["proc"]
        # 有端口没窗口 ⇒ 僵尸，关掉重开（否则建 tab 拿不到 webSocket 地址）
        _kill_chrome()
    os.makedirs(PROFILE, exist_ok=True)
    args = [
        CHROME, "--remote-debugging-port=%d" % PORT,
        "--user-data-dir=%s" % PROFILE, "--no-first-run",
        "--no-default-browser-check", "--no-sandbox",
        "--disable-dev-shm-usage", "--disable-gpu",
        "--window-size=%d,%d" % (_WIN_W, _WIN_H),
        # 静默模式先贴屏幕左沿起窗（macOS 会钳制坐标，实际露约 40px 窄边）
        "--window-position=%s" % (_VISIBLE_POS if visible else _HIDDEN_POS),
    ] + list(_EXTRA_FLAGS)
    if open_url:
        args.append(open_url)
    else:
        # 必须带一个初始页面：否则 Chrome 可能走"恢复上次会话"，上次窗口已关时
        # 就起来一个没有窗口的进程（CDP target=0），建 tab 直接失败。
        args.append("about:blank")
    proc = subprocess.Popen(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    for _ in range(50):
        try:
            _http_json("http://127.0.0.1:%d/json/version" % PORT, timeout=2)
            _PROC["proc"] = proc
            # 会话级登录 Cookie 已随上次退出被清掉 → 启动后先灌回快照，
            # 否则用户每次重启 App 都得重新扫码。
            try:
                _restore_cookie_snapshot()
            except Exception:
                pass
            if not visible and "--headless=new" not in _EXTRA_FLAGS:
                # 有头 + 静默：起来就在屏幕左沿待着，别在用户眼前弹窗
                try:
                    _set_window_visible(False)
                except Exception:
                    pass
            return proc
        except Exception:
            if proc.poll() is not None:
                raise RuntimeError("Chrome 启动失败，退出码 %s" % proc.poll())
            time.sleep(0.5)
    raise RuntimeError("Chrome 未在预期时间内就绪")


def _browser_ws() -> str:
    return _http_json("http://127.0.0.1:%d/json/version" % PORT, timeout=3)["webSocketDebuggerUrl"]


def _cdp_browser_call(method: str, params: dict | None = None,
                      timeout: float = 5.0) -> dict:
    """Browser 域单次调用（连接浏览器级 WS，收一条同名 id 的响应就返回）。"""
    ws = None
    try:
        ws = _WSClient(_browser_ws())
        ws.send_text(json.dumps({"id": 1, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                _, raw = ws.recv_msg()
            except RuntimeError:
                break
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            if m.get("id") == 1:
                if m.get("error"):
                    return {}
                return m.get("result") or {}
    except Exception:
        return {}
    finally:
        try:
            if ws is not None:
                ws.sock.close()
        except Exception:
            pass
    return {}


def _set_window_visible(visible: bool) -> bool:
    """把 Chrome 主窗口挪到屏幕外沿（解析静默）/ 移回屏幕内（扫码可见）。

    🔴 2026-11-03 实测三条硬约束，别随手改：
      1. 不能"最小化"：窗口 minimized ⇒ 页面 visibilityState=hidden ⇒ 抖音播放器
         不初始化，整个解析 56s 抓不到任何 douyinvod 流（有端口、无流）。
      2. 不能 left=-3200 一放了之：macOS 钳制坐标，窗口至少留 ~40px 在屏内
         （1100 宽时 left 最低 -1060）。
      3. 窗口尺寸必须保持正常（1100x800）：抖音按视口大小选码率，窗口缩到很小
         只能拿到低清晰度流。
    所以静默 = 保持 normal 状态 + 贴屏幕最左沿（只露一条 40px 边，不抢焦点）。
    """
    targets = _page_targets()
    if not targets:
        return False
    tid = targets[0].get("id") or ""
    if not tid:
        return False
    res = _cdp_browser_call("Browser.getWindowForTarget", {"targetId": tid})
    wid = res.get("windowId")
    if not wid:
        return False
    bounds = res.get("bounds") or {}
    state = (bounds.get("windowState") or "normal").lower()
    left = bounds.get("left") or 0
    try:
        if visible:
            if not (state == "normal" and left > -1000):
                if state != "normal":
                    _cdp_browser_call("Browser.setWindowBounds",
                                      {"windowId": wid, "bounds": {"windowState": "normal"}})
                _cdp_browser_call("Browser.setWindowBounds", {
                    "windowId": wid,
                    "bounds": {"left": 120, "top": 80, "width": _WIN_W, "height": _WIN_H}})
            if sys.platform == "darwin":
                # 必须显式 activate：Chrome 进程在后台时窗口不会获得焦点，
                # 用户会以为"点了登录没反应"（二维码藏在别的应用后面）
                subprocess.run(["osascript", "-e",
                                'tell application "Google Chrome" to activate'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        # ---- 静默：normal + 贴最左沿（坐标越界由系统钳制，别指望真出屏）----
        if state != "normal":
            _cdp_browser_call("Browser.setWindowBounds",
                              {"windowId": wid, "bounds": {"windowState": "normal"}})
        if state == "normal" and left < -900 and (bounds.get("width") or 0) >= _WIN_W - 8:
            return True
        _cdp_browser_call("Browser.setWindowBounds", {
            "windowId": wid,
            "bounds": {"left": -3000, "top": 60, "width": _WIN_W, "height": _WIN_H}})
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Cookie 快照（🔴 2026-11-03 实测坑）
# 抖音的登录 Cookie（sid_tt / sessionid_ss / sid_guard / odin_tt）是**会话级**的：
# Chrome 正常退出时会把它们从 Cookies 库里清掉（除非开"继续上次会话"），结果就是
# 用户扫一次码，App 重启后又变未登录，得反复扫码。profile 里只剩 UIFID/ttwid 这类
# 设备 Cookie（所以"看起来有抖音 cookie"其实没登录）。
# 解法：登录态一出现就 CDP 导出快照落盘，下次启动灌回浏览器再导航。
# ---------------------------------------------------------------------------
_COOKIE_SNAPSHOT = os.path.join(PROFILE, "douyin_cookies.json")
_LOGIN_COOKIE_SET = set(_LOGIN_COOKIES)
_SNAP_SIG = [None]


def _douyin_cookies(cookies) -> list[dict]:
    """从 CDP cookie 列表里挑出抖音域，转成 Storage.setCookies 可回灌的形状。"""
    out: list[dict] = []
    for c in cookies or []:
        dom = (c.get("domain") or "").lower()
        if "douyin.com" not in dom:
            continue
        item = {
            "name": c.get("name") or "",
            "value": c.get("value") or "",
            "domain": c.get("domain") or ".douyin.com",
            "path": c.get("path") or "/",
            "secure": bool(c.get("secure")),
            "httpOnly": bool(c.get("httpOnly")),
            "sameSite": c.get("sameSite") or "Lax",
        }
        exp = c.get("expires")
        if isinstance(exp, (int, float)) and exp > 0:
            item["expires"] = exp
        out.append(item)
    return out


def _browser_cookies() -> list[dict]:
    """浏览器级取全部 Cookie（含刚登录写入的会话 Cookie）。失败返回空表。"""
    ws = None
    try:
        ws = _WSClient(_browser_ws())
        ws.send_text(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                _, raw = ws.recv_msg()
            except RuntimeError:
                break
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            if m.get("id") == 1:
                return (m.get("result") or {}).get("cookies") or []
    except Exception:
        return []
    finally:
        try:
            if ws is not None:
                ws.sock.close()
        except Exception:
            pass
    return []


def _save_cookie_snapshot(cookies=None) -> int:
    """把当前抖音 Cookie 落盘（0600）。不含登录 Cookie 就不写，避免存一堆无用的设备 Cookie。"""
    ck = _douyin_cookies(cookies if cookies is not None else _browser_cookies())
    if not ck or not any(c["name"] in _LOGIN_COOKIE_SET for c in ck):
        return 0
    sig = (len(ck), sum(len(c["value"]) for c in ck))
    if _SNAP_SIG[0] == sig and os.path.exists(_COOKIE_SNAPSHOT):
        return len(ck)
    tmp = _COOKIE_SNAPSHOT + ".tmp.%d" % os.getpid()
    try:
        os.makedirs(PROFILE, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"saved_at": time.time(), "cookies": ck}, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, _COOKIE_SNAPSHOT)
        _SNAP_SIG[0] = sig
        return len(ck)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        return 0


def _restore_cookie_snapshot() -> int:
    """把上次落盘的 Cookie 灌回浏览器（每个 Chrome 生命周期灌一次）。"""
    if not os.path.exists(_COOKIE_SNAPSHOT):
        return 0
    try:
        with open(_COOKIE_SNAPSHOT, encoding="utf-8") as f:
            ck = (json.load(f) or {}).get("cookies") or []
    except Exception:
        return 0
    if not ck:
        return 0
    # 老/坏快照（只有设备 Cookie、没有真正登录 Cookie）不要回灌，否则会一直假阳性已登录
    if not any(c.get("name") in _LOGIN_COOKIE_SET for c in ck):
        return 0
    ws = None
    try:
        ws = _WSClient(_browser_ws())
        ws.send_text(json.dumps({"id": 1, "method": "Storage.setCookies",
                                 "params": {"cookies": ck}}))
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                _, raw = ws.recv_msg()
            except RuntimeError:
                break
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            if m.get("id") == 1:
                if m.get("error"):
                    return 0
                _SNAP_SIG[0] = (len(ck), sum(len(c.get("value") or "") for c in ck))
                return len(ck)
    except Exception:
        return 0
    finally:
        try:
            if ws is not None:
                ws.sock.close()
        except Exception:
            pass
    return 0


def _headless_enabled() -> bool:
    """解析默认走无头 Chrome（零窗口，用户完全无感）。

    🔴 2026-11-03 实测 `--headless=new` + 同一 profile/cookie 快照可以正常抓到
    douyinvod 流（9.9s），所以没必要让用户看窗口。万一某台机器上被抖音拦，
    设 VDL_DOUYIN_HEADLESS=0 可退回「有头但贴屏幕左沿」，或由 _ensure_mode 自动回退。
    """
    return os.environ.get("VDL_DOUYIN_HEADLESS", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _instance_is_headless() -> bool | None:
    """当前 9341 端口上的实例是否无头。无实例返回 None。"""
    try:
        v = _http_json("http://127.0.0.1:%d/json/version" % PORT, timeout=2)
    except Exception:
        return None
    ua = v.get("User-Agent") or ""
    # 页面内 navigator.userAgent 在 headless=new 里与有头一致，
    # 但浏览器进程的 UA（/json/version）仍带 HeadlessChrome —— 这是唯一可靠信号
    return "HeadlessChrome" in ua


def _ensure_mode(headless: bool) -> None:
    """确保 Chrome 实例形态正确：解析=headless（无窗口）/ 登录=有头（看得见二维码）。

    形态不符时先落 Cookie 快照再重启（抖音登录 Cookie 是会话级的，关了就没）。
    """
    want = ["--headless=new"] if headless else []
    cur = _instance_is_headless()
    if cur is not None and cur == headless and _page_targets():
        _EXTRA_FLAGS[:] = want
        return
    if cur is not None:
        try:
            _save_cookie_snapshot()
        except Exception:
            pass
        _kill_chrome()
    _EXTRA_FLAGS[:] = want
    launch_chrome(visible=not headless)


def _create_target(url: str, background: bool = False) -> str:
    """在 Chrome 里新开一个 tab（target），返回 targetId。

    当前没有任何页面时用新窗口建（否则建出来的是"无窗口后台 tab"，
    拿不到可挂接的 webSocket 地址）。

    background=True：后台建 tab 不激活窗口 —— 🔴 实测 Chrome 建前台 tab 会
    自动把最小化的窗口恢复出来，导致"解析时浏览器弹出来"（用户 2026-11-03 反馈）。
    """
    no_page = not _page_targets()
    bws = _WSClient(_browser_ws())
    params = {"url": url, "newWindow": no_page}
    if background and not no_page:
        params["background"] = True
    bws.send_text(json.dumps({"id": 1, "method": "Target.createTarget",
                              "params": params}))
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            _, raw = bws.recv_msg()
        except RuntimeError:
            break
        try:
            m = json.loads(raw)
        except ValueError:
            continue
        if m.get("id") == 1 and m.get("result", {}).get("targetId"):
            return m["result"]["targetId"]
    raise RuntimeError("创建抖音 tab 失败")


def _target_ws(target_id: str) -> str | None:
    try:
        targets = _http_json("http://127.0.0.1:%d/json/list" % PORT, timeout=3)
    except Exception:
        return None
    for t in targets:
        if t.get("id") == target_id:
            return t.get("webSocketDebuggerUrl")
    return None


def _close_target(target_id: str) -> None:
    try:
        bws = _WSClient(_browser_ws())
        bws.send_text(json.dumps({"id": 1, "method": "Target.closeTarget",
                                  "params": {"targetId": target_id}}))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 登录态
# ---------------------------------------------------------------------------
def is_logged_in() -> bool:
    """检测独立 profile 里是否已登录抖音（命中登录 Cookie 即视为已登录）。"""
    with _LOCK:
        return _is_logged_in_unlocked()


def _storage_get_cookies() -> list[dict]:
    """浏览器级读 Cookie（不新建标签页，轮询开销极小）。"""
    ws = None
    try:
        ws = _WSClient(_browser_ws())
        ws.send_text(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                _, raw = ws.recv_msg()
            except RuntimeError:
                break
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            if m.get("id") == 1:
                return (m.get("result") or {}).get("cookies") or []
    except Exception:
        return []
    finally:
        try:
            if ws is not None:
                ws.sock.close()
        except Exception:
            pass
    return []


def _has_login_cookie(cookies) -> bool:
    for c in cookies or []:
        if "douyin.com" in (c.get("domain") or "").lower() and c.get("name") in _LOGIN_COOKIE_SET:
            return True
    return False


def _is_logged_in_unlocked() -> bool:
    try:
        launch_chrome()
    except Exception:
        return False
    cookies = _storage_get_cookies()
    if _has_login_cookie(cookies):
        # 登录态一出现就落快照（会话 Cookie 退出即失，见 _COOKIE_SNAPSHOT 说明）
        try:
            _save_cookie_snapshot(cookies)
        except Exception:
            pass
        return True
    # 🔴 Chrome 可能是上一轮 / 别的进程启动的：这种路径没有走"启动后灌回快照"，
    # 会话级登录 Cookie 就不在浏览器里（表现为快照明明有、界面却说未登录）。
    # 这里按需补灌一次再判，把"每次用之前都得扫码"彻底断掉。
    try:
        if _restore_cookie_snapshot() and _has_login_cookie(_storage_get_cookies()):
            return True
    except Exception:
        pass
    return False


def login_status() -> bool:
    """登录页轮询专用：检测到登录成功后自动收起可见的登录窗口。

    扫码必须用有头实例（用户要看到二维码），而解析用无头实例。扫完还留着浏览器
    窗口会让用户以为"每次解析都开浏览器"，所以这里检测到登录态就把窗口关掉，
    下一次解析会自动起无头实例。
    """
    ok = is_logged_in()
    if ok and _instance_is_headless() is False:
        try:
            _save_cookie_snapshot()   # 会话级 Cookie，关窗前必须落盘
        except Exception:
            pass
        try:
            close_chrome()
        except Exception:
            pass
    return ok


def open_login_page() -> dict:
    """打开抖音登录页（展示二维码），用户在手机抖音 App 扫码登录。"""
    with _LOCK:
        # 解析用的是无头实例（看不见窗口）⇒ 登录必须先切成有头可见实例
        _ensure_mode(headless=False)
        _set_window_visible(True)
        # 已经有抖音页就复用，别每次点按钮都叠一个新 tab
        existing = next((t for t in _page_targets()
                         if "douyin.com" in (t.get("url") or "")), None)
        if existing:
            target_id = existing.get("id") or ""
            _bring_to_front(target_id)
        else:
            target_id = _create_target(DOUYIN_HOME)
            _bring_to_front(target_id)
        return {
            "ok": True,
            "hint": "已在系统 Chrome 打开抖音登录页，请用手机抖音 App 扫码登录；"
                    "登录成功后即可直接粘贴抖音链接解析下载。",
            "target_id": target_id,
        }


def _bring_to_front(target_id: str) -> None:
    """把既有的抖音 tab 提到最前（用户点「登录抖音」时要能一眼看到二维码）。"""
    if not target_id:
        return
    ws = None
    try:
        ws_url = _target_ws(target_id)
        if not ws_url:
            return
        ws = _WSClient(ws_url)
        ws.send_text(json.dumps({"id": 1, "method": "Page.bringToFront"}))
        time.sleep(0.2)
    except Exception:
        pass
    finally:
        try:
            if ws is not None:
                ws.sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 核心：解析抖音 → 捕获 douyinvod 直链
# ---------------------------------------------------------------------------
def _classify_douyinvod(url: str) -> str | None:
    """返回 'video' / 'audio' / 'combined' / None（非抖音流）。"""
    if "douyinvod.com" not in url:
        return None
    u = url.lower()
    if "media-audio" in u:
        return "audio"
    if "media-video" in u:
        return "video"
    # 形如 v11-weba.douyinvod.com/.../xxx.mp4 且不含 media- 前缀：通常是混合轨
    if u.endswith(".mp4") or "video" in u:
        return "combined"
    return "video"


def resolve(url: str, timeout: int = 90) -> dict:
    """本地 Chrome + CDP 捕获抖音视频/音轨直链，返回与 downloader._douyin_info
    同构的 data dict：{video_url, audio_url, video_has_audio, title, ...}。

    未登录 / 未捕获到流 → 抛 ResolveError（调用方负责转译前端文案）。
    """
    with _LOCK:
        return _resolve_unlocked(url, timeout)


def _resolve_unlocked(url: str, timeout: int = 90) -> dict:
    from downloader import ResolveError

    if not os.path.exists(CHROME):
        raise ResolveError(
            "本地未找到 Chrome 浏览器",
            "抖音解析依赖系统 Chrome 捕获真实视频流。请安装 Google Chrome 后重试。",
            category="douyin_no_chrome")
    if not is_logged_in():
        raise ResolveError(
            "请先登录抖音",
            "抖音已升级反爬（Argus 设备指纹风控），需在系统 Chrome 里登录抖音账号才能解析。"
            "请点击「登录抖音」按钮，用手机抖音扫码登录后重试。",
            category="douyin_login_required")

    headless = _headless_enabled()
    try:
        _ensure_mode(headless=headless)
        return _capture(url, timeout)
    except ResolveError as e:
        # 兜底：无头实例被抖音识别/拦截（极少见）时，退回有头可见实例再抓一次。
        # 只对「能连上但没抓到流」重试，登录态/没装 Chrome 这类错误重试也没意义。
        if headless and getattr(e, "category", "") in ("douyin_login_stale", "parse_failed"):
            print("[douyin_cdp] 无头未抓到流(%s)，回退有头重试" % e.category, flush=True)
            try:
                _ensure_mode(headless=False)
                return _capture(url, timeout)
            except Exception:
                raise e from None
        raise


def _capture(url: str, timeout: int = 90) -> dict:
    """在已就绪的 Chrome 实例里开 tab、导航、抓 douyinvod 流。形态由 _ensure_mode 决定。"""
    from downloader import ResolveError

    target_id = _create_target(url)
    ws_url = None
    for _ in range(30):
        ws_url = _target_ws(target_id)
        if ws_url:
            break
        time.sleep(0.3)
    if not ws_url:
        raise ResolveError("抖音解析失败", "无法挂接 Chrome 标签页", category="parse_failed")

    videos: list[str] = []
    audios: list[str] = []
    combined: list[str] = []
    title = "抖音视频"
    captured_at = 0.0
    _mid = [10]

    def _send(method, params=None):
        _mid[0] += 1
        _send_txt = json.dumps({"id": _mid[0], "method": method,
                                **({"params": params} if params else {})})
        try:
            ws.send_text(_send_txt)
        except Exception:
            pass

    try:
        import urllib.parse as _up
        ws = _WSClient(ws_url)
        _send("Page.enable")
        _send("Network.enable")
        _send("Runtime.enable")
        # 确保导航到目标（createTarget 已带 url，但稳妥起见再 navigate 一次）
        _send("Page.navigate", {"url": url})
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                _, raw = ws.recv_msg()
            except RuntimeError as e:
                raise ResolveError("抖音解析失败", "Chrome 连接中断：%s" % e,
                                  category="parse_failed") from None
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            method = m.get("method") or ""
            if method == "Network.responseReceived":
                resp = m.get("params", {}).get("response", {})
                rurl = resp.get("url", "")
                kind = _classify_douyinvod(rurl)
                if kind == "video" and rurl not in videos:
                    videos.append(rurl)
                    captured_at = time.time()
                    print("[douyin_cdp] 捕获视频流[%d]" % len(videos), flush=True)
                elif kind == "audio" and rurl not in audios:
                    audios.append(rurl)
                    captured_at = time.time()
                    print("[douyin_cdp] 捕获音轨流[%d]" % len(audios), flush=True)
                elif kind == "combined" and rurl not in combined:
                    combined.append(rurl)
                    captured_at = time.time()
                    print("[douyin_cdp] 捕获混合流[%d]" % len(combined), flush=True)
                # 顺手抓标题
                if not videos and not audios and len(combined) == 0:
                    pass
            elif method == "Page.frameNavigated":
                # 标题在 DOM 里，简单用 frame 的 title 兜底（不够准，下游会覆盖）
                pass
            # 同时抓 document title 通过 Runtime 太重，跳过；标题后续用 URL 兜底
            # 一旦拿到「视频+音频」或「混合轨」，稳定 3s 收尾再退（确保两轨都抓全）
            if captured_at and (time.time() - captured_at > 3):
                if (videos and audios) or combined:
                    break
        # 抓页面标题做文件名（解析完成后再补一刀，不阻塞主流程）
        try:
            _title_id = _mid[0] + 1
            _send("Runtime.evaluate",
                  {"expression": "document.title || ''", "returnByValue": True})
            _tdead = time.time() + 3
            while time.time() < _tdead:
                try:
                    _, raw = ws.recv_msg()
                except RuntimeError:
                    break
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                if m.get("id") == _title_id:
                    val = ((m.get("result") or {}).get("result") or {}).get("value")
                    if isinstance(val, str):
                        _t = val.strip()
                        _t = re.sub(r'\s*[-_|]\s*抖音.*$', '', _t).strip()
                        if _t:
                            title = _t[:120]
                    break
        except Exception:
            pass
    finally:
        try:
            ws.sock.close()
        except Exception:
            pass
        _close_target(target_id)

    # 抓流成功后顺手刷新登录 Cookie 快照（登录 Cookie 可能刚被抖音续期）
    try:
        _save_cookie_snapshot()
    except Exception:
        pass

    # 优先混合轨（单直链），其次 视频+音频 分离轨
    if combined:
        return {
            "video_url": combined[0],
            "audio_url": "",
            "video_has_audio": True,
            "video_id": "",
            "title": title,
            "duration": None,
            "thumbnail": "",
            "webpage_url": url,
            "height": 720, "width": 1280,
        }
    if videos and audios:
        return {
            "video_url": videos[0],
            "audio_url": audios[0],
            "video_has_audio": False,
            "video_id": "",
            "title": title,
            "duration": None,
            "thumbnail": "",
            "webpage_url": url,
            "height": 720, "width": 1280,
        }
    # 只有视频（极少，可能音频流还没发出来）：当作混合轨兜底
    if videos:
        return {
            "video_url": videos[0],
            "audio_url": "",
            "video_has_audio": True,
            "video_id": "",
            "title": title,
            "duration": None,
            "thumbnail": "",
            "webpage_url": url,
            "height": 720, "width": 1280,
        }
    # 🔴 已登录却抓不到流：最常见是"会话在服务端已过期"（本地 Cookie 还在，界面显示已登录，
    # 但抖音把我们当未登录给登录墙）。这种情况必须引导重新扫码，否则用户会卡在
    # "永远显示已登录、永远解析失败"的死角里。另可能是私密/已删除/合集/直播页。
    raise ResolveError(
        "抖音没抓到视频流",
        "登录状态可能已过期（本地仍显示已登录，但抖音已不认）。请点「去登录抖音」"
        "重新扫码后重试；若链接是私密/已删除/合集/直播页，请换单个视频播放页的链接。",
        category="douyin_login_stale")


def close_chrome() -> None:
    """退出本地 Chrome（App 退出时调用，释放调试端口）。

    跨进程安全：Chrome 可能由上一个 App 进程启动（本进程没有其 Popen 句柄），
    故优先用 CDP `Browser.close`（同一 profile/端口，任何进程都能关），再退本地句柄。
    """
    proc = _PROC.get("proc")
    # 0) 关之前先落 Cookie 快照：抖音登录 Cookie 是会话级的，Chrome 一退就被清掉，
    #    不存快照 = 用户下次还得重新扫码。
    if _chrome_running():
        try:
            _save_cookie_snapshot()
        except Exception:
            pass
    # 1) CDP Browser.close：跨进程生效
    if _chrome_running():
        try:
            ws = _WSClient(_browser_ws())
            ws.send_text(json.dumps({"id": 1, "method": "Browser.close"}))
            time.sleep(0.5)
        except Exception:
            pass
    # 2) 本地句柄兜底（本进程启动的情况）
    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
    _PROC["proc"] = None
