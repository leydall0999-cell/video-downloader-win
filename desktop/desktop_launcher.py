"""视频工坊桌面版启动器（由 PyInstaller 打包为 .app / .exe）。

职责：
1. 定位捆绑的资源（ffmpeg / server 代码 / 前端静态文件 / yt-dlp 插件）
2. 自动挑选空闲端口（默认 8321，被占用时顺延，避免启动即崩溃）
3. 后台启动 FastAPI 服务（uvicorn）
4. 在原生窗口中打开 Web UI（优先 pywebview，回退浏览器）

普通用户双击 .app / .exe 即可，无需安装 Python、ffmpeg 或任何依赖。
退出：关掉窗口即可。
"""
import os
import sys
import time
import socket
import signal
import subprocess
import threading
from pathlib import Path

# 🔴 2026-10-18：import webview（连带 pythonnet / .NET runtime 初始化）必须尽早。
#    曾出现：后端重模块（fastapi/uvicorn/PIL/cv2/onnxruntime 等）在子线程 import 完成、
#    服务就绪后，主线程才 import webview → 部分 Windows 机器上 GUI 线程创建窗口
#    **静默失败**：进程活着、API 200、WebView2 渲染进程也在跑，但 EnumWindows
#    找不到该进程的任何窗口（用户看到"程序打不开/白屏"）。提前到进程干净期加载，
#    避开与后端 DLL/线程环境的相互干扰。失败不致命：下方 webview 分支有兜底。
try:
    import webview as _webview
except Exception:  # pragma: no cover - 打包缺件时走浏览器回落
    _webview = None

try:
    import fcntl as _fcntl  # macOS / Linux
except ImportError:  # pragma: no cover - Windows 走 .exe 单实例
    _fcntl = None

# ---- 定位资源目录 ----
if getattr(sys, "frozen", False):
    _exe = Path(sys.executable)
    _macos_dir = _exe.parent
    _resources = (_macos_dir / ".." / "Resources").resolve()
    if getattr(sys, "_MEIPASS", None):
        BASE = Path(sys._MEIPASS)
    elif (_resources / "web").exists():
        BASE = _resources
    else:
        BASE = _macos_dir
else:
    BASE = Path(__file__).resolve().parent.parent

SERVER_DIR = BASE / "server"
WEB_DIR = BASE / "web"
PLUGINS_DIR = BASE / "yt_dlp_plugins"
# 提前把 server 目录注入路径，供 _detect_commentary 直接 import commentary_locate
sys.path.insert(0, str(SERVER_DIR))

# 配音引擎热补丁层必须先于任何 import edge_tts 生效。
# 解说 worker 是「重入自身」的同一进程，所以这一行同时覆盖主进程与 worker：
# 巡查发现 edge-tts 被微软作废 → 新版下载到用户目录 → 下次启动即用新版，不必重打包。
try:
    import tts_guard as _tts_guard
    _tts_guard.bootstrap()
except Exception:
    _tts_guard = None


def _detect_ffmpeg() -> str | None:
    exe = ".exe" if sys.platform == "win32" else ""
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / "bin" / f"ffmpeg{exe}")
    candidates.append(BASE / "bin" / f"ffmpeg{exe}")
    for c in candidates:
        if c.exists():
            return str(c)
    return None

def _venv_python(root: Path) -> Path:
    """跨平台返回某 venv 的解释器路径（Win: .venv\\Scripts\\python.exe / POSIX: .venv/bin/python）。"""
    if sys.platform == "win32":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"

def _detect_commentary() -> tuple[str | None, str | None]:
    """定位 commentary-pipeline 目录及其解释器；找不到时返回 (None, None)。

    统一走 server/commentary_locate.locate_commentary()（含包内捆绑候选）。
    包内捆绑模式不暴露 python（由 worker 重入自身处理，见 #198），仅返回目录。
    """
    from commentary_locate import locate_commentary
    loc = locate_commentary()
    if not loc:
        return None, None
    return str(loc.root), (None if loc.bundled else loc.python)

_ff = _detect_ffmpeg()
if _ff:
    os.environ["VDL_FFMPEG_BIN"] = _ff              # app.py 自己用
    os.environ["FFMPEG_LOCATION"] = str(Path(_ff).parent)  # yt-dlp 找 ffmpeg 用
    os.environ["PATH"] = str(Path(_ff).parent) + os.pathsep + os.environ.get("PATH", "")  # 兜底
    # 同目录探测 ffprobe（Windows 上为 ffprobe.exe），注入给 app.py 用
    _fp = Path(_ff).with_name("ffprobe" + (".exe" if sys.platform == "win32" else ""))
    if _fp.exists():
        os.environ["VDL_FFPROBE_BIN"] = str(_fp)

_c_dir, _c_py = _detect_commentary()
if _c_dir:
    os.environ["VDL_COMMENTARY_DIR"] = _c_dir
if _c_py:
    os.environ["VDL_COMMENTARY_PYTHON"] = _c_py

# ---- 从用户配置文件读取 VDL_* 代理设置（让打包后的 .app 也能用国内/海外出口，无需改系统代理）----
def _load_external_config() -> dict:
    """读取用户可写配置目录下的 config.json，把其中的 VDL_PROXY_CN / VDL_PROXY 等注入 os.environ。

    用途：用户租了国内 VPS 或买了付费国内节点后，把代理地址写进配置文件，.app 双击即可生效，
    不必去改 macOS 系统代理，也不必重打包。downloader._resolve_proxy 在每次请求时读取这些变量。

    另支持 VDL_PORT 固定端口——打包后的 .app 双击即可固定本地服务端口，便于调试与规避端口冲突。

    优先级：运行时已存在的环境变量(launchd/shell 注入) > 配置文件 > _resolve_proxy 自动检测系统代理。
    """
    import json

    candidates = []
    if sys.platform == "darwin":
        candidates.append(Path.home() / "Library" / "Application Support" / "VideoDownloader" / "config.json")
    candidates.append(Path.home() / ".config" / "videodownloader" / "config.json")
    candidates.append(BASE / "config.json")  # 便携 / 调试用

    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text("utf-8"))
        except Exception as e:  # 坏 JSON 不致命，跳过并报警
            print(f"[VDL] 配置文件损坏已忽略 {path}: {e}", file=sys.stderr)
            continue
        if not isinstance(data, dict):
            continue
        applied = []
        for key in (
            "VDL_PROXY_CN",
            "VDL_PROXY",
            "VDL_MAX_FILE_MB",
            "VDL_PORT",
        ):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                if key not in os.environ:        # 显式环境变量优先于配置文件
                    os.environ[key] = val.strip()
                    applied.append(f"{key}=<set>")
                else:
                    applied.append(f"{key}(env-override)")
        if applied:
            print(f"[VDL] 已从 {path} 应用配置: {', '.join(applied)}", file=sys.stderr)
        return data  # 命中第一个存在的配置文件即止
    return {}

_load_external_config()

if PLUGINS_DIR.exists():
    sys.path.insert(0, str(BASE))

# ---- 单二进制双角色：解说管线 worker 重入 ----
def _app_data_dir() -> Path:
    """跨平台返回本应用的可写数据目录（input/output/work 重定向到这里）。"""
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", str(Path.home() / ".vdl")))
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "VideoDownloader"
    return Path.home() / ".local" / "share" / "videodownloader"

def _rebind_std_streams() -> None:
    """Windows windowed(.exe 无控制台)下，重绑标准流到 nul，避免 ffmpeg/print 写已关闭 fd 崩溃。"""
    try:
        dn = open(os.devnull, "w")
        os.dup2(dn.fileno(), 1)
        os.dup2(dn.fileno(), 2)
        if sys.stdin is not None:
            os.dup2(dn.fileno(), 0)
    except Exception:
        pass

def _run_commentary_worker(argv: list[str]) -> int:
    """以 --vdl-commentary-worker 重入自身时，把主程序当作解说管线 worker 运行。

    依赖随包内置（config 已砍 torch），无需外部 Python / pip；脚本与模型在包内只读资源目录，
    input/output/work 重定向到可写目录(COMMENTARY_WORK_ROOT)。详见 server/commentary_locate.py。
    """
    import multiprocessing
    multiprocessing.freeze_support()  # PyInstaller 子进程兼容（保险）

    # 保险丝：防止递归重入（worker 不应再拉起 worker）
    depth = int(os.environ.get("VDL_WORKER_DEPTH", "0") or "0")
    if depth > 0:
        print("[worker] 检测到重入保险丝(VDL_WORKER_DEPTH>0)，拒绝二次重入")
        return 1
    os.environ["VDL_WORKER_DEPTH"] = str(depth + 1)

    from commentary_locate import locate_commentary
    loc = locate_commentary()
    if loc is None or not loc.bundled:
        print("[worker] 未找到包内捆绑的解说管线，无法以 worker 模式运行")
        return 1

    # 注入工作环境：模型/脚本在包内(只读)，工作目录重定向到可写位置
    os.environ["VDL_COMMENTARY_BUNDLED"] = "1"
    os.environ.setdefault("COMMENTARY_BASE", str(loc.root))
    _model_dir = os.path.join(str(loc.root), "models", "whisper-base")
    if os.path.isdir(_model_dir):
        os.environ["COMMENTARY_MODEL_DIR"] = _model_dir
    _work_root = _app_data_dir() / "commentary"
    os.environ["COMMENTARY_WORK_ROOT"] = str(_work_root)

    # 🔴 2026-10-20 Windows 实测缺陷：管线默认 provider 是 qwen3tts（本机 7871 克隆服务，
    #    依赖 torch —— 桌面版为了体积早把 torch 砍掉了，Windows 上必然起不来）。
    #    于是每次都先撞一次本地服务、打印一行 ⛔ 红色告警，再回退 edge-tts：
    #    用户看到「本轮成片旁白不是你的克隆音色」就以为功能坏了。
    #    这里在**用户没显式配置**时把默认值定为 edge（云端微软音色，开箱即用）。
    #    用户若在设置里选了克隆音色，VDL_TTS_PROVIDER 已存在，不会被覆盖。
    if sys.platform == "win32" and not (os.environ.get("VDL_TTS_PROVIDER") or "").strip():
        os.environ["VDL_TTS_PROVIDER"] = "edge"

    # Windows windowed 无控制台，重绑标准流避免崩溃。
    # 🔴 2026-10-19：**仅当确实没有可用的 stdout 时**才重绑。
    #    历史实现无条件 dup2 到 nul，会覆盖掉父进程用 PIPE 建立的 fd 1 ——
    #    于是 worker 的所有进度日志（转写/解说词/配音/渲染）全部丢弃，
    #    前端 6 个步骤永远停在「准备」，用户看到的就是「点了没反应」。
    #    sys.stdout is None 才是「GUI 双击、无控制台」的可靠判据；
    #    作为子进程被 Popen 拉起时 sys.stdout 是有效管道，必须保持原样。
    if sys.platform == "win32" and sys.stdout is None:
        _rebind_std_streams()

    # 🔴 2026-10-20 Windows 实测缺陷：worker 的 stdout 是父进程 PIPE，但编码仍按
    #    **系统 locale(cp936/GBK)** 初始化（PYTHONIOENCODING 在冻结包里没生效）。
    #    管线 llm_script.py 会打印 `估算¥0.0123`，`¥`(U+00A5) 在 GBK 里无映射
    #    ⇒ UnicodeEncodeError ⇒ 整条解说链在「生成 AI 解说词」直接失败。
    #    这里把所有标准流强制重设为 UTF-8 且 errors=replace：
    #    宁可个别字符变 `?`，也绝不允许日志把业务打死。
    for _name in ("stdout", "stderr"):
        _st = getattr(sys, _name, None)
        _rec = getattr(_st, "reconfigure", None)
        if _rec is None:
            continue
        try:
            _rec(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    # 把包内管线根注入 sys.path，使 import process 命中包内版本（process.py 自行接管 scripts/）
    sys.path.insert(0, str(loc.root))
    try:
        import process
    except Exception as exc:
        print(f"[worker] 导入包内 process 失败: {exc}")
        return 1

    # 去掉哨兵后交给 process.main（其内部直接读 sys.argv）
    # argv[0] 是 sys.executable，需跳过——否则 argparse 会把它当 video positional，
    # 真正传入的 in_file 会变成第二个 positional 报「unrecognized arguments」。
    worker_argv = [a for a in argv[1:] if a != "--vdl-commentary-worker"]
    sys.argv = ["process.py", *worker_argv]
    code = 0
    try:
        process.main()
    except SystemExit as e:
        code = int(e.code) if isinstance(e.code, int) else 0
    except BaseException:  # noqa: BLE001
        # 🔴 2026-10-20 Windows 实测缺陷：管线内部抛异常时（如 llm_script 打印 `¥` 撞 GBK），
        #    异常冒到 PyInstaller 窗口版顶层后**进程不退出**：34s CPU 后彻底僵住，
        #    但仍持有父进程建立的 stdout 管道写端 ⇒ 父进程 `for line in proc.stdout`
        #    永远等不到 EOF ⇒ job 永远 running、无 error、无日志，用户看到「点了没反应」。
        #    这里兜住一切异常：把 traceback 打进管道（父进程据此显示失败原因），
        #    再以非零码退出，保证父进程一定能拿到退出码并把任务标 failed。
        import traceback

        traceback.print_exc()
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass
        code = 1
    # 强制退出：不留任何非守护线程/子进程句柄把管道撑住。
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    os._exit(code)
    return code  # pragma: no cover

def _find_free_port(start: int = 8321, tries: int = 80) -> int:
    """挑一个真空闲端口。

    🔴 2026-10-17：connect_ex 探测 → 真 bind 探测。原因：Windows 的 Hyper-V/WSL
    会「静默保留」整段 TCP 端口（netsh int ipv4 show excludedportrange 可见，
    但 netstat 看不到占用者）——保留段内 connect_ex 连不上、被误判为「空闲」，
    选中后 uvicorn bind 必失败（WSAEINUSE），白耗一轮重试。真 bind 才是权威判据。
    bind 后立即 close，与随后 uvicorn 的 bind 之间有极小竞态，由
    _run_server_with_retry 的顺延兜底（_update_active_port 会同步全局端口）。
    """
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start

def _update_active_port(p: int, lock_file=None) -> None:
    """绑定成功后回写实际端口到全局变量与单实例锁文件，
    保证原生窗口、connect 轮询、VdlApi 保存对话框全部指向真实监听端口。"""
    global PORT, API_URL, URL
    PORT = p
    API_URL = f"http://{HOST}:{PORT}"
    URL = f"http://{HOST}:{PORT}"
    if lock_file is not None:
        try:
            lock_file.seek(0)
            cur = lock_file.read().strip()
            parts = cur.split()
            pid = parts[0] if parts else os.getpid()
            # 🔴 2026-10-19：版本 tag 含空格（"v1.0.76 · 构建 xxx @ MM-DD HH:MM"），
            # 必须 join 后半段。旧代码取 parts[2] 会把 tag 退化成 "v1.0.76"，
            # 导致接管判定 obuild != self_build 永真（同版本也判为旧版 → 误杀），
            # 且混构实测断言读不到「构建」字样。与 _read_lock / 互斥体分支同源修复。
            build = " ".join(parts[2:]) if len(parts) >= 3 else ""
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.write(f"{pid} {PORT} {build}\n")
            lock_file.flush()
        except Exception:
            pass

def _ensure_stdio() -> None:
    """Windows 冻结 GUI 程序（无控制台）里 sys.stdout/sys.stderr 是 None，必须补掉。

    历史事故（2026-10-17，用户 Windows 装机实测）：安装后双击打不开，WebView2
    窗口能起但页面 127.0.0.1 拒绝连接。日志根因：PyInstaller windowed 冻结程序
    不分配控制台 ⇒ sys.stdout is None ⇒ uvicorn 日志格式化器初始化时调用
    sys.stdout.isatty() 抛 AttributeError ⇒ 后端线程秒崩 ⇒ 白等 240s 后开一个
    必然连不上的窗口。CI 冒烟测试从 bash 启动 exe（继承 pipe，stdout 非 None），
    因此抓不到；Mac app bundle 的 bootloader 会把 stdout 重定向为有效对象也不中招。
    这里把 None 换成 devnull，让 uvicorn / logging / traceback 等一切依赖
    sys.std* 的代码恢复正常；有控制台时（Mac / 开发态 / CI）本函数直接返回。
    """
    if sys.stdout is not None and sys.stderr is not None and sys.stdin is not None:
        # 🔴 2026-11-03 真机 CI 实测补的一层：**不是 None 也会炸**。
        # Windows 上父进程用 PIPE 起我们（CI 验收脚本把 stdout 重定向到文件、
        # 启动器/守护进程也可能这么做）时，sys.stdout 是**cp1252 编码的管道**，
        # 任何中文 print 都抛 UnicodeEncodeError，表现为接口 500
        # （实测 `/api/resolve` 解析抖音 500 charmap can't encode）。
        # 强制成 UTF-8 + errors=replace：写不出就替换，永不抛异常。
        for _name in ("stdout", "stderr"):
            _st = getattr(sys, _name, None)
            try:
                _st.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        return
    try:
        import io as _io

        def _mk(mode: str):
            return _io.TextIOWrapper(open(os.devnull, mode), encoding="utf-8",
                                     errors="replace")

        if sys.stdin is None:
            sys.stdin = _mk("r")
        if sys.stdout is None:
            sys.stdout = _mk("w")
        if sys.stderr is None:
            sys.stderr = _mk("w")
    except Exception:
        pass


def _is_port_in_use(e: OSError) -> bool:
    """只有『端口真的被占用』才值得顺延重试。其余 OSError（如 ENOENT 文件缺失、
    EACCES 权限不足）在任一候选端口都会以完全相同的方式失败，重试只是把同一个
    错误重复 12 遍并掩盖真凶（典型如 `[Errno 2] No such file or directory`：
    那是 uvicorn 加载 app 阶段抛的，跟端口毫无关系）。"""
    import errno as _errno
    if e.errno in (_errno.EADDRINUSE, 10048):  # Linux=98 / Windows WSAEADDRINUSE=10048
        return True
    msg = str(e).lower()
    return "address already in use" in msg or "10048" in msg


def _run_server_with_retry(start_port: int, host: str, app_dir: str, lock_file=None) -> None:
    """启动 uvicorn；若首端口绑定失败（EADDRINUSE 等），自动顺延到下一端口重试，
    最多尝试 12 个端口，彻底消除“kill 旧实例后立刻启动新实例”的端口竞态导致的假启动。"""
    _ensure_stdio()  # 无控制台环境先补 sys.std*，否则 uvicorn 初始化即崩（见函数注释）
    import uvicorn
    max_off = 12
    last_err = None
    for off in range(max_off):
        p = start_port + off
        try:
            _update_active_port(p, lock_file)
            _launch_log(f"尝试在端口 {p} 启动后端服务...")
            uvicorn.run("app:app", app_dir=app_dir, host=host, port=p, log_level="info")
            return
        except OSError as e:
            last_err = e
            # 🔴 2026-10-26 修复：过去只要 OSError 就当「端口被占」顺延 12 个端口，
            #    结果 `[Errno 2] No such file or directory` 这类根本不是端口问题的
            #    ENOENT 被误判，12 个端口同一秒全失败、真凶藏死。现在先判是否真·端口
            #    被占；不是的话把完整堆栈写进启动日志并**立即抛出**，不再无脑顺延。
            try:
                import traceback as _tb
                _detail = _tb.format_exc()
            except Exception:  # noqa: BLE001
                _detail = "(traceback unavailable)"
            if _is_port_in_use(e):
                _launch_log(f"端口 {p} 被占用（{e}），自动重试下一端口 {p+1}")
                continue
            _launch_log(
                f"端口 {p} 启动失败（非端口问题，{type(e).__name__}: {e}），"
                f"停止重试并上报真实错误\n{_detail}"
            )
            raise
        except SystemExit as e:
            # 冻结版 uvicorn 在 bind 失败时抛 SystemExit(非0)，需当作可重试的绑定失败；
            # 仅 code==0 视为干净退出，不再重试。
            code = getattr(e, "code", 1) or 1
            if code == 0:
                return
            last_err = e
            _launch_log(f"端口 {p} 启动进程异常退出(code={code})，自动重试下一端口 {p+1}")
            continue
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if "address already in use" in msg or "bind" in msg:
                last_err = e
                _launch_log(f"端口 {p} 启动失败（{e}），自动重试下一端口 {p+1}")
                continue
            raise
    _launch_log(f"所有候选端口({start_port}..{start_port+max_off-1})均绑定失败，后端无法启动: {last_err}")

def _logged_run_server(start_port: int, host: str, app_dir: str, lock_file=None) -> None:
    """包装 _run_server_with_retry：任何未捕获异常都写进启动日志再死。

    历史教训（2026-09-15）：Dock 启动（open -a）时后端线程曾静默崩溃，日志只留
    「服务器启动超时」，但 nohup 直跑一切正常，两者唯一差别是运行方式 → 排查成本
    极高。没有这段包装，下次还得靠猜。
    """
    try:
        _run_server_with_retry(start_port, host, app_dir, lock_file)
    except BaseException as exc:  # noqa: BLE001
        import traceback as _tb
        _launch_log("后端线程异常终止: %s: %s\n%s"
                    % (type(exc).__name__, exc, _tb.format_exc()))


_env_port = (os.environ.get("VDL_PORT") or "").strip()
PORT = int(_env_port) if _env_port else _find_free_port()
HOST = "127.0.0.1"
API_URL = f"http://{HOST}:{PORT}"          # 后端 FastAPI 地址（API 调用用这个）
# 使用 http:// 模式加载（相对路径天然工作，CSS/JS/图片无跨协议问题）。
# 通过 PyObjC 配置 WKWebViewConfiguration 绕过系统代理/PAC/VPN 网络扩展(NE)，
# 解决 Karing 等 NE 在网卡层劫持 WKWebView 导致的 "Load failed"。
URL = f"http://{HOST}:{PORT}"

# Dock Quit / Cmd+Q 退出标记：AppDelegate.applicationShouldTerminate_ 被调用时置 True，
# 使窗口 closing 拦截放行；红叉（windowShouldClose）不置此标记 → 仍最小化返回桌面。
_app_terminating = False


def _origin_of(url: str) -> str:
    """取 URL 的源（scheme://host:port），无 scheme/host 时返回空串。"""
    import urllib.parse as _up
    try:
        parts = _up.urlsplit((url or "").strip())
        if not parts.scheme or not parts.netloc:
            return ""
        return f"{parts.scheme}://{parts.netloc}".lower()
    except Exception:
        return ""


def _window_url_is_foreign(current_url: str, app_url: str) -> bool:
    """窗口当前地址是否「已离开应用」→ 需要导回应用首页。

    纯函数（便于离线单测）。判定规则：
      · 空地址 / about:blank / data: → **不算离开**（启动期与内联下载的正常中间态，
        否则 load_url 会在启动瞬间反复触发）；
      · 与应用不同源（外链、file://、blob:、API 绝对地址）→ 算离开；
      · 同源的任何路径（/、/index.html、/ops-board…）→ 不算离开。
    """
    cur = (current_url or "").strip()
    if not cur or cur.lower().startswith("about:") or cur.lower().startswith("data:"):
        return False
    app_origin = _origin_of(app_url)
    if not app_origin:
        return False                      # 应用地址异常时宁可不动作，也不误伤
    cur_origin = _origin_of(cur)
    if not cur_origin:
        return True# blob: / file: 之类没有 netloc 的 → 一定不是应用页面
    return cur_origin != app_origin


def _choose_save_path_via_osascript(prompt: str, suggested: str, log_path: str = "") -> str:
    """★ 兜底路径（面板会是**英文**）：直接调 osascript 的 `choose file name`。

    弹 macOS 原生「保存文件」面板，返回用户选定的绝对路径；取消返回 "CANCELLED"。

    为什么用 osascript 子进程而不是 pywebview/NSSavePanel：桌面壳的主线程跑在
    pywebview 的 run loop 上，从 JS 同步桥调用里弹 NSSavePanel 会把 run loop 卡死。
    另起 osascript 进程则完全不碰主线程。

    中文提示词/默认名必须写进**临时 .applescript 文件（UTF-8）**再交给 osascript 执行，
    经 argv 传中文会被错误解码导致面板根本不弹（历史踩坑）。

    返回 "CANCELLED" 与 "ERROR: ..." 是约定值，前端据此区分「用户主动取消」与「真失败」。
    """
    import json
    import tempfile
    import subprocess

    def _log(msg: str) -> None:
        if not log_path:
            return
        try:
            import datetime as _dt
            with open(log_path, "a") as f:
                f.write(f"[{_dt.datetime.now().isoformat()}] {msg}\n")
        except Exception:
            pass

    script = (
        f'set p to choose file name with prompt "{prompt}" '
        f'default name {json.dumps(suggested, ensure_ascii=False)} '
        'default location (path to downloads folder)\n'
        'POSIX path of p'
    )
    scpt = ""
    try:
        fd, scpt = tempfile.mkstemp(suffix=".applescript")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(script)
        r = subprocess.run(
            ["osascript", scpt],
            capture_output=True, text=True, timeout=600,
            env={**os.environ, "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"},
        )
        if r.returncode == 0 and r.stdout.strip():
            chosen = r.stdout.strip()
            _log(f"choose-save-path -> {chosen}")
            return chosen
        # 用户点了「取消」，osascript 退出码为 1 且 stderr 形如 "User canceled."。
        _log(f"choose-save-path cancelled rc={r.returncode} err={r.stderr.strip()!r}")
        return "CANCELLED"
    except Exception as exc:  # noqa: BLE001 — osascript 缺失/异常 → 交给调用方兜底
        _log(f"choose-save-path exception: {exc!r}")
        return ""
    finally:
        if scpt:
            try:
                os.remove(scpt)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 原生「保存到哪里」面板（中文本地化版）
#
# ★ 2026-09-22 用户报「面板/替换提示是英文，改中文」。根因实测（非推断）：
#   系统语言确实是中文（AppleLanguages = zh-Hans-CN），但 CLI 形态的 `osascript`
#   自身**没有中文本地化资源**，它弹出来的面板一律走英文 —— 连文件夹都显示成
#   "Downloads" 而不是「下载」。三种常见偏方**实测全部无效**：
#     ① env LANG/LC_ALL=zh_CN.UTF-8   ② osascript -AppleLanguages '(zh-Hans-CN)'
#     ③ 伪造 __CFBundleIdentifier + defaults 写 AppleLanguages
#   唯一有效：把同一段 AppleScript 用 `osacompile` 编成**真正的 .app 程序**，
#   并在 Info.plist 声明中文优先（CFBundleDevelopmentRegion=zh-Hans +
#   CFBundleLocalizations），再用 `open -W` 启动它 —— 面板立即全中文
#   （存储为 / 标签 / 位置: 下载 / 取消 / 存储，已实测截图核对）。
#
# 入参/结果用文件交换：`open --args` 传不进 applet 的 `on run argv`（实测无效），
# 所以用   <数据目录>/in.txt  = 随机数 \n 提示语 \n 默认文件名
#          <数据目录>/out.txt = 随机数 \n 选定路径|CANCELLED
# 随机数用来识别「这一次」的结果，避免读到上一次的残留文件而被误判。
# applet 只在首次（或版本升级）编译一次，之后一直复用。
# ---------------------------------------------------------------------------
_SAVE_PANEL_APPLET_SRC = r'''on run
	set basePath to (POSIX path of (path to home folder)) & ".video-downloader/save_panel/"
	set inPath to basePath & "in.txt"
	set outPath to basePath & "out.txt"
	set theNonce to "0"
	set thePrompt to "Save file to"
	set theName to "untitled"
	try
		set rawText to (do shell script "cat " & quoted form of inPath)
		set AppleScript's text item delimiters to linefeed
		set theParts to text items of rawText
		if (count of theParts) is less than 3 then
			set AppleScript's text item delimiters to return
			set theParts to text items of rawText
		end if
		if (count of theParts) is greater than or equal to 3 then
			set theNonce to item 1 of theParts
			set thePrompt to item 2 of theParts
			set theName to item 3 of theParts
		end if
		set AppleScript's text item delimiters to ""
	end try
	set theResult to "CANCELLED"
	-- ★ 2026-09-24：applet 现在是 LSUIElement 后台程序（不占 Dock、不占切换器），
	--   agent 程序不会自动激活，面板可能弹在别的窗口后面。先 activate 顶到最前。
	--   （对普通前台程序，activate 本就是默认行为，无副作用。）
	try
		activate
	end try
	try
		set chosenFile to choose file name with prompt thePrompt default name theName default location (path to downloads folder)
		set theResult to POSIX path of chosenFile
	end try
	do shell script "echo " & quoted form of theNonce & " > " & quoted form of outPath & "; echo " & quoted form of theResult & " >> " & quoted form of outPath
end run
'''

_SAVE_PANEL_APPLET_CACHE = {"path": None}

# ★ 2026-09-24：applet「形态」版本号。凡改动 Info.plist 侧行为（图标 / 本地化 / LSUIElement）
#   都要 bump 一次 —— 指纹里带上它，老用户机器上的 applet 才会被重编（否则改了不生效）。
_SAVE_PANEL_APPLET_VER = "2026-09-24-lsuielement"


def _save_panel_dir() -> str:
    """保存面板的入参/结果/applet 数据目录（`~/.video-downloader/save_panel`）。"""
    d = os.path.join(os.path.expanduser("~"), ".video-downloader", "save_panel")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:  # noqa: BLE001
        return ""
    return d


def _save_panel_icon_bytes() -> bytes:
    """读 App 正式图标（desktop/icon.icns），用于替换 osacompile 默认卷曲纸图标。

    查找顺序：源码态 `desktop/icon.icns`（与本文件同目录）→ 打包态
    `VideoDownloader.app/Contents/Resources/icon.icns`（sys.executable 同级的
    Resources）。都找不到返回 b""（applet 保持默认图标，功能不受影响）。

    背景（2026-09-22 用户截图）：osacompile 编译的 applet 用 AppleScript 默认
    「卷曲纸卷」图标，弹保存面板时 Dock/切换器里冒出陌生图标，用户截图来问
    「这是什么」。换成正式 App 图标后可辨识。
    """
    candidates = []
    try:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.icns"))
    except Exception:
        pass
    try:
        candidates.append(os.path.join(os.path.dirname(sys.executable), "..", "Resources", "icon.icns"))
    except Exception:
        pass
    for cand in candidates:
        try:
            with open(cand, "rb") as fh:
                data = fh.read()
            if data:
                return data
        except Exception:
            continue
    return b""


def _ensure_save_panel_applet(log_path: str = "") -> str:
    """确保存在「中文本地化」的保存面板 applet，返回其绝对路径；不可用返回 ""。

    失败（没有 osacompile / 无写权限 / 编译报错）时返回 ""，调用方会回落到
    osascript 那条老路 —— 功能一样，只是面板文案是英文。
    """
    import subprocess
    import shutil
    import hashlib

    cached = _SAVE_PANEL_APPLET_CACHE.get("path")
    if cached and os.path.isdir(cached):
        return cached

    d = _save_panel_dir()
    if not d:
        return ""
    app = os.path.join(d, "SavePanel.app")
    info = os.path.join(app, "Contents", "Info.plist")
    ver_file = os.path.join(d, "SavePanel.version")
    # ★ 按源码指纹判断是否需要重编：只按「plist 里有 zh-Hans」判断的话，
    #   以后改了 _SAVE_PANEL_APPLET_SRC，老用户会一直用旧 applet（改了不生效）。
    #   指纹同时纳入图标内容 —— 图标换版也要重编落盘（2026-09-22）。
    icon_bytes = _save_panel_icon_bytes()
    icon_sig = hashlib.sha1(icon_bytes).hexdigest()[:16] if icon_bytes else "noicon"
    sig = hashlib.sha1((hashlib.sha1(_SAVE_PANEL_APPLET_SRC.encode("utf-8")).hexdigest()
                        + icon_sig + _SAVE_PANEL_APPLET_VER).encode("utf-8")).hexdigest()[:16]

    if os.path.isfile(info) and os.path.isfile(ver_file):
        try:
            with open(ver_file, "r", encoding="utf-8", errors="ignore") as fh:
                same_ver = fh.read().strip() == sig
            with open(info, "r", encoding="utf-8", errors="ignore") as fh:
                plist_old = fh.read()
            # 除了中文优先级，还要确认「后台形态」已写进 plist：09-22 版 applet 没有
            # LSUIElement，会占着 Dock 一个磁贴（用户截图问「为什么多一个一样的图标」）。
            same_loc = ("zh-Hans" in plist_old) and ("LSUIElement" in plist_old)
            if same_ver and same_loc:
                _SAVE_PANEL_APPLET_CACHE["path"] = app
                return app
        except Exception:  # noqa: BLE001
            pass

    src = os.path.join(d, "SavePanel.applescript")
    try:
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(_SAVE_PANEL_APPLET_SRC)
        shutil.rmtree(app, ignore_errors=True)
        r = subprocess.run(
            ["osacompile", "-o", app, src],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0 or not os.path.isfile(info):
            return ""
        # ★ 用 App 正式图标覆盖 osacompile 的默认「卷曲纸」图标（2026-09-22）。
        #   不换的话弹面板时 Dock/切换器里出现陌生图标，用户不知道那是什么。
        if icon_bytes:
            try:
                with open(os.path.join(app, "Contents", "Resources", "applet.icns"), "wb") as fh:
                    fh.write(icon_bytes)
                # bump mtime 促使 LaunchServices/Dock 重读图标缓存
                subprocess.run(["touch", app], capture_output=True, timeout=10)
            except Exception:
                pass
        # ★ 关键一步：声明中文优先。不写这两项，系统面板仍按英文渲染。
        # ★ 追加 LSUIElement：让 applet 成为「后台程序」（agent）—— 面板照常弹、
        #   文案照旧中文，但**不再出现在 Dock 和 Cmd-Tab 里**。
        #   背景（2026-09-24 用户截图）：09-22 把 applet 图标/CFBundleName 都改成了
        #   「视频工坊」，于是弹保存面板时 Dock 里多出一个**一模一样的图标**（还带运行小圆点），
        #   看起来像 App 被双开。lsappinfo 实测该进程 type="Foreground" → 确有 Dock 磁贴。
        for args in (
            ["-replace", "CFBundleDevelopmentRegion", "-string", "zh-Hans"],
            ["-insert", "CFBundleLocalizations", "-json", '["zh-Hans","en"]'],
            ["-replace", "CFBundleName", "-string", "视频工坊"],
            ["-insert", "LSUIElement", "-bool", "true"],
        ):
            try:
                subprocess.run(["plutil"] + args + [info], capture_output=True, text=True, timeout=30)
            except Exception:  # noqa: BLE001
                pass
        try:
            with open(info, "r", encoding="utf-8", errors="ignore") as fh:
                _plist_after = fh.read()
            if "zh-Hans" not in _plist_after:
                return ""
            if "LSUIElement" not in _plist_after:
                return ""   # 没写进去就当不可用，回落 osascript（宁可英文面板，也不要多一个 Dock 图标）
        except Exception:  # noqa: BLE001
            return ""
        try:
            with open(ver_file, "w", encoding="utf-8") as fh:
                fh.write(sig)
        except Exception:  # noqa: BLE001
            pass
        _SAVE_PANEL_APPLET_CACHE["path"] = app
        return app
    except Exception:  # noqa: BLE001
        return ""


def _kill_save_panel_applet(app: str) -> None:
    """杀掉我们自己的保存面板 applet 进程（只匹配该 applet 的可执行路径，绝不误伤 App 本体）。

    用途：① 上一次面板还挂着（用户点红点关窗 / 面板被切到后台）时，新一次保存不应再叠一个
    面板；② 看门狗超时兜底，避免 applet 永久僵住。
    """
    import subprocess
    try:
        exe = os.path.join(app, "Contents", "MacOS", "applet")
        subprocess.run(["pkill", "-f", exe], capture_output=True, timeout=10)
    except Exception:  # noqa: BLE001
        pass


def _choose_save_path_via_applet(prompt: str, suggested: str, log_path: str = "",
                                 timeout_s: float = 1800.0) -> str:
    """走中文本地化 applet 弹面板：返回选定路径 / "CANCELLED"；机制不可用返回 ""。

    ⚠️ 只有「applet 压根不可用」（编译失败 / 目录不可写）才返回 "" 让调用方回落英文面板；
    applet 一旦成功弹出来，任何拿不到结果的情况都按 **"CANCELLED"** 处理 —— 否则会紧接着
    再弹一个英文 osascript 面板，用户看到的是「取消了一个又冒出一个」。
    `timeout_s` 是面板等待上限（看门狗），超时即杀掉 applet 并按取消处理。
    """
    import subprocess
    import time
    import secrets

    app = _ensure_save_panel_applet(log_path)
    if not app:
        return ""
    d = _save_panel_dir()
    if not d:
        return ""
    in_path = os.path.join(d, "in.txt")
    out_path = os.path.join(d, "out.txt")
    nonce = f"{int(time.time() * 1000)}-{secrets.token_hex(4)}"
    # 先清掉上一次遗留的 applet：面板没关/关窗后挂住时，Dock（老版本）或后台会残一个，
    # 而且两个面板叠着也没法用 —— 本次保存为准。
    _kill_save_panel_applet(app)
    try:
        os.remove(out_path)  # 清掉上一次的残留，只认本次随机数
    except OSError:
        pass
    try:
        with open(in_path, "w", encoding="utf-8") as fh:
            fh.write(f"{nonce}\n{prompt}\n{suggested}\n")
    except Exception:  # noqa: BLE001
        return ""
    try:
        # -W：等 applet 退出（= 用户关掉面板）再返回，天然同步。
        # 用 Popen + 看门狗而不是 run(timeout=…)：run 超时会抛异常、连通 returncode 都拿不到，
        # 老的兜底路径会再弹一个英文面板。这里超时就**杀 applet + 按取消**，干净利落。
        proc = subprocess.Popen(["open", "-W", app], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, text=True)
        remained = float(timeout_s)
        while proc.poll() is None and remained > 0:
            time.sleep(0.2)
            remained -= 0.2
        if proc.poll() is None:
            _kill_save_panel_applet(app)
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
            return "CANCELLED"   # 超时不回落（避免第二个英文面板）
    except Exception:  # noqa: BLE001
        return ""                # open 都起不来 → 交给调用方回落
    if proc.returncode != 0:
        # applet 被我们自己杀掉（上一次遗留/看门狗）或异常退出 → 按取消处理，不回落。
        return "CANCELLED"
    try:
        with open(out_path, "r", encoding="utf-8") as fh:
            parts = fh.read().splitlines()
    except Exception:  # noqa: BLE001
        return "CANCELLED"
    if len(parts) < 2 or parts[0].strip() != nonce:
        return "CANCELLED"  # 结果不是本次的（被后续请求顶掉）→ 当作用户取消，不回落
    res = parts[1].strip()
    if not res or res == "CANCELLED":
        return "CANCELLED"
    return res


def _choose_save_path(prompt: str, suggested: str, log_path: str = "") -> str:
    """弹系统「保存文件」面板，返回用户选定的绝对路径；取消返回 "CANCELLED"。

    两级：先走**中文本地化 applet**（面板全中文）；applet 不可用时回落
    `osascript`（功能一致，但面板文案是英文）。
    返回 "" 表示面板机制整体不可用，由调用方决定兜底位置。
    """
    if sys.platform == "win32":
        # 2026-10-20：Windows 上 applet/osascript 两条路都不存在，原实现恒返回 ""，
        # 8 个保存入口因此从不弹面板（静默落到「下载」，用户以为没反应）。补原生另存为。
        return _windows_choose_save_path(prompt, suggested, log_path)
    got = _choose_save_path_via_applet(prompt, suggested, log_path)
    if got:
        return got
    return _choose_save_path_via_osascript(prompt, suggested, log_path)


def _download_url_to_path(url: str, dest: str, *, headers: dict | None = None,
                          timeout=(10, 600)) -> str:
    """把 url 流式下载写入 dest，返回最终写入的绝对路径字符串。

    行为约定（与桥接方法的用户预期一致）：
      · 先写同目录 `xxx.part`，成功后 `os.replace` 原子改名 —— 中途失败不会在用户
        选定的位置留下半截文件名（那比失败本身更让人困惑）；
      · 同名不覆盖：`片子.mp4` 已存在则写 `片子(1).mp4`（macOS 访达同款习惯）；
      · 自动创建所选目录（用户可能在保存面板里直接输入一个新文件夹名）。

    抽成**模块级纯函数**（不弹面板、不碰 UI）是为了能离线测：给夹具 HTTP 服务器打
    真实请求即可覆盖「落盘 / 同名 / 报错不留残根」，见 tests/test_desktop_bridge.py。
    """
    import os as _os
    import requests
    from pathlib import Path

    target = Path(dest).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():                       # 同名不覆盖，与访达行为一致
        stem, suf = target.stem, target.suffix
        i = 1
        while target.exists():
            target = target.parent / f"{stem}({i}){suf}"
            i += 1
    partial = target.with_name(target.name + ".part")
    try:
        resp = requests.get(url, headers=headers or {}, timeout=timeout, stream=True)
        resp.raise_for_status()
        with open(partial, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=256 * 1024):
                if chunk:
                    fh.write(chunk)
        _os.replace(partial, target)
    except Exception:
        try:
            _os.remove(partial)
        except OSError:
            pass
        raise
    return str(target)


class VdlApi:
    """暴露给前端 JS 的桥接 API（仅 pywebview 桌面模式生效）。

    🔴 2026-10-12 更正一条被误信很久的结论：此前这里写着「桌面版无法通过
      <a download> 触发系统保存框（WebKit 限制）」。**不准确** —— pywebview 的
      cocoa 后端是有下载委托的（`DownloadDelegate.download_decideDestinationUsing
      Response_…` 会弹 NSSavePanel），但两条分支都要求
      `webview.settings['ALLOW_DOWNLOADS']` 为真，而它**默认 False**、本项目从未开启。
      于是 `<a download>` 在桌面壳里是**静默吞掉**（不报错、不弹框、不落盘），
      用户以为保存了却什么都没有（2026-10-12 用户报「图片埋在一堆文件夹里找不到」）。
    因此「让用户把文件拿到手」的操作一律走本类的方法：面板 + 写盘 + 回报真实路径。
    """

    def open_external(self, url: str) -> str:
        """在系统默认浏览器中打开外部 URL（如第三方授权页）。

        pywebview 的 WKWebView 不支持 window.open() 弹窗（会被静默拦截），
        因此外部授权改由 Python 调系统浏览器打开。返回 "OK" 或 "ERROR: ..."。
        """
        import webbrowser
        try:
            webbrowser.open(url)
            return "OK"
        except Exception as exc:  # 把错误回传前端展示
            return f"ERROR: {exc}"

    def copy_to_clipboard(self, text: str) -> str:
        """把文本写入系统剪贴板（桌面版原生桥）。

        为什么必须走原生桥（2026-10-01 用户实测「复制链接点了没反应」）：
        pywebview 的 WKWebView 里 `navigator.clipboard` 常为 undefined，
        `document.execCommand('copy')` 在新版 WebKit 也已被禁用 —— 网页式复制
        在桌面壳里是**静默失效**（不报错、不生效），与 blob `<a download>` 被吞
        是同一类坑（铁律：桌面壳里「让用户拿东西」的操作一律走 pywebview 原生桥）。
        返回 "OK" 或 "ERROR: ..."。
        """
        import subprocess
        try:
            payload = "" if text is None else str(text)
            if sys.platform == "win32":
                p = subprocess.run(["clip"], input=payload.encode("utf-16le"),
                                   timeout=5, shell=True)
                return "OK" if p.returncode == 0 else f"ERROR: clip rc={p.returncode}"
            if sys.platform == "darwin":
                # pbcopy 是 macOS 自带，无第三方依赖
                p = subprocess.run(["pbcopy"], input=payload.encode("utf-8"), timeout=5)
                return "OK" if p.returncode == 0 else f"ERROR: pbcopy rc={p.returncode}"
            # Linux：xclip / xsel 二选一
            for cmd in (["xclip", "-selection", "clipboard"],
                        ["xsel", "--clipboard", "--input"]):
                try:
                    subprocess.run(cmd, input=payload.encode("utf-8"), timeout=5, check=True)
                    return "OK"
                except FileNotFoundError:
                    continue
            return "ERROR: 缺少剪贴板工具（xclip/xsel）"
        except Exception as exc:
            return f"ERROR: {exc}"

    def read_clipboard(self) -> str:
        """读取系统剪贴板里的文本（桌面版原生桥）。

        与 copy_to_clipboard 对称：桌面壳 WKWebView 里 `navigator.clipboard.readText()`
        在没有用户手势/权限时会被拒（WebKit 对 readText 比 writeText 更严），所以剪贴板
        **读取**同样一律走原生桥。用途：用户「复制视频链接 → 切回 App → 一键下载」
        （见 web/js/desktop-app.js 的剪贴板识别）。

        返回剪贴板文本；为空或读取失败一律返回 ""（调用方只关心「有没有可用文本」）。
        故意**不**返回 "ERROR: ..." —— 调用方拿返回值当链接文本判断，错误前缀会被
        当成一个诡异的 URL 弹提示条。
        """
        import subprocess
        try:
            if sys.platform == "win32":
                # 🔴 2026-10-18 用户实测事故：前端每 2 秒轮询一次剪贴板，而这里原来每次
                # 都 `subprocess.run(["powershell", ...])`。桌面版是 --windowed 的 GUI
                # 进程，Windows 会给它 fork 出来的控制台程序新建窗口 ⇒ 用户只打开了一次
                # App，屏幕就每隔一两秒闪一下、窗口不停自己弹（看着像中毒）。
                # 改走 Win32 API 直读（零进程、零窗口）；只有 API 不可用时才回退 powershell，
                # 且回退分支显式带 CREATE_NO_WINDOW 双保险。
                try:
                    import win_clipboard as _wcb
                    _txt = _wcb.read_text()
                except Exception:
                    _txt = None
                if _txt is not None:
                    return _txt
                p = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                    capture_output=True, timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if p.returncode != 0:
                    return ""
                return p.stdout.decode("utf-8", errors="replace")
            if sys.platform == "darwin":
                # pbpaste 是 macOS 自带，无第三方依赖（与 pbcopy 对称）
                p = subprocess.run(["pbpaste"], capture_output=True, timeout=5)
                if p.returncode != 0:
                    return ""
                return p.stdout.decode("utf-8", errors="replace")
            # Linux：xclip / xsel 二选一
            for cmd in (["xclip", "-selection", "clipboard", "-o"],
                        ["xsel", "--clipboard", "--output"]):
                try:
                    p = subprocess.run(cmd, capture_output=True, timeout=5, check=True)
                    return p.stdout.decode("utf-8", errors="replace")
                except FileNotFoundError:
                    continue
            return ""
        except Exception:
            return ""

    def choose_extension_dir(self) -> str:
        """弹原生「选择文件夹」面板，让用户指定浏览器扩展的加载目录（零点击自动更新用）。

        为什么单独走一条桥而不是复用 choose_folder()：那个面板的提示语写死成
        「选择剪映草稿导出目录」，用在扩展目录上会让用户以为点错了。

        实现与 choose_folder() 同源（osascript 子进程）：把面板隔离在主线程之外 ——
        直接在非主线程里动 NSWindow 会触发
        `NSWindow drag regions should only be invalidated on the Main Thread!` SIGTRAP，
        整个桌面 App 卡死。

        返回所选目录绝对路径；用户取消 / 非 macOS / 失败 → 空串（调用方按取消处理）。
        """
        if sys.platform == "win32":
            # 2026-10-20 修复：原先这里直接 `return ""`，Windows 上点「选择扩展目录」
            # 完全不弹框、无提示 —— 与「添加文件」是同一批 osascript-only 缺陷
            return _windows_choose_folder(
                "请选择浏览器扩展的加载目录（就是含 manifest.json 的那个文件夹）")
        if sys.platform != "darwin":
            return ""
        try:
            import subprocess
            script = ('POSIX path of (choose folder with prompt '
                      '"请选择浏览器扩展的加载目录（就是含 manifest.json 的那个文件夹）")')
            p = subprocess.run(["osascript", "-e", script],
                               capture_output=True, timeout=300)
            if p.returncode == 0:
                out = p.stdout.decode("utf-8", "replace").strip()
                if out and not out.startswith("execution error"):
                    return out.rstrip("/")
            return ""
        except Exception:
            return ""

    def choose_folder(self) -> str:
        """弹出系统文件夹选择框，返回所选目录绝对路径；用户取消或失败返回空串。

        ★ 2026-08-28 续33 关键修复：原先用 tkinter.Tk() / filedialog.askdirectory，
        但 pywebview 的 JS bridge 在 macOS cocoa 后端**未必在 main thread** 同步调
        这条链 → 在 non-main-thread 触 `TkMacOSXMakeRealWindowExist` →
        `NSWindow drag regions should only be invalidated on the Main Thread!`
        SIGTRAP → macOS 弹 force quit「应用没响应」+ 主循环饿死，整个桌面 app 卡死。

        改为走 pyobjc 自己的 NSOpenPanel（cocoa 主线程原生 panel），100% 不走 Tk。
        """
        if sys.platform == "win32":
            # 2026-10-20：Windows 无 osascript，原先必落到 `return ""` → 选目录静默无反应。
            # 走 Shell 原生 SHBrowseForFolder（同样是「不进 Tk 主线程」的思路）
            return _windows_choose_folder("选择剪映草稿导出目录")
        try:
            import os as _os
            # AppleScript 弹「选择文件夹」原生 panel（cocoa 主线程，纯 macOS API）
            # 跨线程最稳：osascript 子进程隔离 NSWindow 主线程约束
            script = (
                'set theFolder to choose folder with prompt "选择剪映草稿导出目录"\n'
                'return POSIX path of theFolder\n'
            )
            try:
                proc = _os.popen(f"osascript -e '{script.replace(chr(10), "' -e '").replace("'", chr(39))}' 2>&1")
                out = proc.read().strip()
                if out and not out.startswith("execution error"):
                    return out.rstrip('/')
            except Exception:
                pass

            # osascript 兜底失败 → 落到纯手工输入（前端已有 fallback 回退）
            return ""
        except Exception as exc:
            return f"ERROR: {exc}"

    def choose_files(self, kind: str = "media") -> list[str] | str:
        """弹出系统多文件选择框，返回所选文件绝对路径列表；用户取消或失败返回空串。

        kind 决定可选扩展名与对话框标题（★ 2026-09-11 修复「高效压缩/图片转换里图片是灰的、选不了」）：
          - "media"（默认）：视频 + 音频（桥接/转码/字幕等原有场景，保持行为不变）
          - "image"        ：图片（图片转换视图，含 bmp/tif/gif）
          - "video"        ：纯视频（高清修复的视频档，2026-09-12 新增；
                             不含音频扩展名，避免用户选到 mp3 才被后端拒绝）
          - "any"          ：视频 + 音频 + png/jpg/jpeg/webp（高效压缩视图；
                             刻意不含 heic/avif/bmp/tif/gif，后端压缩不支持，避免「选得到却报错」）
        前端未传参时按 "media" 处理，向后兼容旧调用。

        ★ 2026-08-28 续33 关键修复：移除 tkinter.Tk() 调用（见 choose_folder 注释）。
        同样改用 osascript `choose file with multiple selections allowed` 子进程弹窗，
        不依赖 Tk，跨线程 100% 安全。

        ★ 2026-08-28 续45 关键修复：原版用 "\\\\n".join(...) 把多语句压成单行再走
        osascript -e，osascript 收不到真换行 → 整个脚本解析失败 → 前端 chooseFiles
        返回空数组 → 「添加文件」按钮无任何反应。改为 choose_folder 同款：每条语句
        单独 -e 喂入（真换行由 shell 拆 -e 参数实现）。
        """
        try:
            import subprocess as _sp
            _MEDIA_EXTS = [
                "mp4", "mov", "mkv", "webm", "avi", "flv", "ts", "wmv",
                "mpeg", "mpg", "3gp", "ogv", "m4v",
                "mp3", "m4a", "aac", "wav", "flac", "ogg", "opus",
            ]
            # 图片转换视图（UPLOAD_IMAGE_EXTS 等同）：Pillow 原生可开的位图
            _IMAGE_EXTS = [
                "png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff", "gif",
            ]
            # 高效压缩视图 only 吃 png/jpg/jpeg/webp（与 server/routers/compress.py 一致）；
            # 故意不含 heic/avif/bmp/tif/gif —— 后端不处理，放进来会变成「选得到但报不支持」。
            _COMPRESS_IMAGE_EXTS = ["png", "jpg", "jpeg", "webp"]
            _VIDEO_EXTS = [
                "mp4", "mov", "mkv", "webm", "avi", "flv", "wmv", "m4v",
            ]
            _KINDS = {
                "media": (_MEDIA_EXTS, "选择要桥接的视频/音频文件（可多选）"),
                "image": (_IMAGE_EXTS, "选择图片文件（可多选）"),
                "video": (_VIDEO_EXTS, "选择要增强的视频文件（可多选）"),
                "any": (_MEDIA_EXTS + _COMPRESS_IMAGE_EXTS, "选择视频/音频/图片文件（可多选）"),
            }
            exts, prompt = _KINDS.get(str(kind or "media").strip().lower(), _KINDS["media"])

            # 🔴 2026-10-20 Windows 实测缺陷修复：Windows 上不存在 osascript，
            #    原实现直接落到 `subprocess.run(["osascript", ...])` → FileNotFoundError
            #    → 被下面 `except Exception: pass` 吞掉 → 返回 "" → 前端空数组 →
            #    六个视图的「添加文件」按钮点了**毫无反应**。改走 Win32 原生对话框。
            if sys.platform == "win32":
                return _windows_choose_files(exts, prompt, multiple=True)

            # AppleScript 列表必须用引号+逗号+空格：`{"mp4", "mov"}`。
            # `of type` 是 choose file 的子句，必须与 choose file 同一行。
            ext_list = "{" + ", ".join(f'"{e}"' for e in exts) + "}"
            # AppleScript 限制：choose file ... of type ... with ... 必须同一行；
            # 后续 repeat / return 可独立 -e 喂入。
            script_lines = [
                f'set theFiles to choose file of type {ext_list} with multiple selections allowed with prompt "{prompt}"',
                'set out to ""',
                'repeat with f in theFiles',
                '  set out to out & POSIX path of f & linefeed',
                'end repeat',
                'return out',
            ]
            # 每条语句独立 -e（与 choose_folder 同款），osascript 才会按真换行解析
            cmd = ["osascript"]
            for line in script_lines:
                cmd += ["-e", line]
            try:
                proc = _sp.run(cmd, capture_output=True, text=True, timeout=120)
                out = (proc.stdout or "").strip()
                if out and "execution error" not in out and "User canceled" not in out:
                    # osascript 多选用 linefeed 分隔，单选返回单行
                    paths = [line.strip() for line in out.split("\n") if line.strip()]
                    if len(paths) == 1:
                        return paths[0]
                    return paths
            except Exception:
                pass

            # 用户取消 或 失败 → 返回空串让前端走手动输入 fallback
            return ""
        except Exception as exc:
            return f"ERROR: {exc}"

    def pick_voice_sample(self) -> str:
        """选择「我的音色」参考音频：弹系统文件选择框，返回绝对路径；取消返回空串。

        与 choose_folder / choose_files 同套机制（osascript 子进程弹原生 panel），
        刻意不用 tkinter：bridge 未必跑在 main thread，Tk 会触发
        `NSWindow ... Main Thread` 约束直接 SIGTRAP，把整个 App 卡死。
        单选 + 限定音频类型（克隆样本只可能是音频）。
        """
        try:
            import subprocess as _sp
            exts = ["wav", "mp3", "m4a", "flac", "aac", "ogg", "opus", "mp4"]
            # 同一类缺陷（2026-10-20）：Windows 无 osascript → 原实现必然返回空串，
            # 「选择音色样本」点了没反应。走 Win32 原生单选对话框。
            if sys.platform == "win32":
                picked = _windows_choose_files(
                    exts, "选择你的音色样本（念一句话的录音，3~15 秒最佳）", multiple=False
                )
                if isinstance(picked, list):
                    return picked[0] if picked else ""
                return picked
            ext_list = "{" + ", ".join(f'"{e}"' for e in exts) + "}"
            script_lines = [
                f'set theFile to choose file of type {ext_list} with prompt '
                f'"选择你的音色样本（念一句话的录音，3~15 秒最佳）"',
                "return POSIX path of theFile",
            ]
            cmd = ["osascript"]
            for line in script_lines:
                cmd += ["-e", line]
            proc = _sp.run(cmd, capture_output=True, text=True, timeout=180)
            out = (proc.stdout or "").strip()
            if out and "execution error" not in out and "User canceled" not in out:
                return out
            return ""
        except Exception as exc:
            return f"ERROR: {exc}"

    def start_indextts_mlx(self) -> dict:
        """一键开启本地语音克隆（IndexTTS-MLX）。

        普通用户无需理解「端口/服务」等概念：本方法自动在常见位置寻找语音克隆包，
        启动其本地服务，并返回大白话结果。找不到包或启动失败时也给出可操作的提示，
        不会抛出未捕获异常（前端会友好展示 msg）。
        """
        import os
        import subprocess

        pack_dirs = [
            os.path.expanduser("~/Downloads/IndexTTS-MLX-1.5-Pack"),
            os.path.expanduser("~/Desktop/IndexTTS-MLX-1.5-Pack"),
            os.path.expanduser("~/Documents/IndexTTS-MLX-1.5-Pack"),
            os.path.expanduser("~/Applications/IndexTTS-MLX-1.5-Pack"),
            "/Applications/IndexTTS-MLX-1.5-Pack",
            os.path.expanduser("~/IndexTTS-MLX-1.5-Pack"),
        ]
        pack = next((d for d in pack_dirs if os.path.isdir(d)), None)
        if not pack:
            return {
                "ok": False,
                "msg": "还没装「语音克隆」包。请把 IndexTTS-MLX-1.5-Pack 解压到「下载」文件夹，再点一次此按钮即可。",
            }

        # 兼容不同解压结构：优先在包根目录找启动脚本，再递归向下找一层
        launch_scripts = ["app.py", "launch.py", "demo.py", "server.py", "start.py", "main.py"]
        target = None
        for s in launch_scripts:
            p = os.path.join(pack, s)
            if os.path.isfile(p):
                target = p
                break
        if target is None:
            for root, dirs, files in os.walk(pack):
                if "node_modules" in root or ".git" in root:
                    continue
                for f in files:
                    if f in launch_scripts:
                        target = os.path.join(root, f)
                        break
                if target:
                    break
        if target is None:
            return {
                "ok": False,
                "msg": "在「语音克隆」文件夹里没找到启动脚本，请确认解压完整（不要只解压了子文件夹）。",
            }

        # 选解释器：优先用包内 venv，否则退回系统 python3
        target_dir = os.path.dirname(target)
        py_cands = [
            os.path.join(pack, "venv", "bin", "python"),
            os.path.join(target_dir, "venv", "bin", "python"),
            os.path.join(pack, "venv", "Scripts", "python.exe"),
            "python3",
            "python",
        ]
        py = None
        for c in py_cands:
            if c in ("python3", "python"):
                py = c
                break
            if os.path.isfile(c):
                py = c
                break
        if py is None:
            py = "python3"

        try:
            log_path = os.path.expanduser("~/.vdl_indextts.log")
            with open(log_path, "w") as lf:
                subprocess.Popen(
                    [py, target],
                    cwd=target_dir,
                    stdout=lf,
                    stderr=lf,
                    start_new_session=True,
                )
            return {
                "ok": True,
                "msg": "已开始启动，约 10–30 秒后在「配音引擎」下拉里会显示「已就绪，可直接用」。",
            }
        except Exception as exc:  # 把错误回传前端展示
            return {"ok": False, "msg": f"启动失败：{exc}"}

    def start_qwen3tts(self) -> dict:
        """选中「Qwen3-TTS 本地语音克隆」引擎时由前端自动调用：拉起本机 7871 服务。

        与 IndexTTS-MLX 同思路：在常见位置找启动脚本与 venv，起服务，返回大白话结果。
        找不到/启动失败时给可操作提示，不抛未捕获异常。
        """
        import os
        import socket
        import subprocess

        dev_root = os.path.expanduser("~/WorkBuddy/问问题/commentary-pipeline")
        c_dir = os.environ.get("VDL_COMMENTARY_DIR", "")
        script_cands = [
            os.path.join(str(BASE), "commentary", "scripts", "start_qwen3tts_server.py"),
            os.path.join(c_dir, "scripts", "start_qwen3tts_server.py") if c_dir else None,
            os.path.join(dev_root, "scripts", "start_qwen3tts_server.py"),
        ]
        script = next((s for s in script_cands if s and os.path.isfile(s)), None)
        if not script:
            return {
                "ok": False,
                "msg": "没找到 Qwen3-TTS 启动脚本（应在 commentary/scripts/start_qwen3tts_server.py）。请确认解说管线已安装。",
            }

        # 🔴 优先 MLX venv（.venv_qwen3tts_mlx）：Apple GPU 直跑，实测 RTF≈1.05；
        #    旧 torch venv 在 macOS 13 无 MPS，纯 CPU 一句 3 字都要跑几分钟。
        #    两者都放候选里，脚本自身 --engine auto 也会在 torch venv 里 execv 切到 MLX。
        #
        # ★ 2026-10-20：venv 目录布局随平台不同 —— POSIX 是 `bin/python`，
        #   Windows 是 `Scripts\python.exe`。原实现只列 bin/python，Windows 上即使
        #   用户真装了环境也必然走到「没找到 Python 环境」分支，引擎永远起不来。
        _bindir = "Scripts" if sys.platform == "win32" else "bin"
        _pyexe = "python.exe" if sys.platform == "win32" else "python"

        def _venv(dirpath: str) -> str:
            return os.path.join(dirpath, _bindir, _pyexe)

        venv_cands = [
            _venv(os.path.join(os.path.dirname(script), ".venv_qwen3tts_mlx")),
            _venv(os.path.join(os.path.dirname(script), ".venv_qwen3tts")),
            _venv(os.path.join(dev_root, "scripts", ".venv_qwen3tts_mlx")),
            _venv(os.path.join(dev_root, "scripts", ".venv_qwen3tts")),
            _venv(os.path.join(c_dir, "scripts", ".venv_qwen3tts_mlx")) if c_dir else None,
            _venv(os.path.join(c_dir, "scripts", ".venv_qwen3tts")) if c_dir else None,
            _venv(os.path.expanduser("~/.video-downloader/venvs/qwen3tts_mlx")),
            _venv(os.path.expanduser("~/.video-downloader/venvs/qwen3tts")),
        ]
        py = next((p for p in venv_cands if p and os.path.isfile(p)), None)
        if not py:
            return {
                "ok": False,
                "msg": "没找到 Qwen3-TTS 的 Python 环境（.venv_qwen3tts_mlx / .venv_qwen3tts）。"
                       "请先安装本地克隆环境。",
            }

        # 已在运行则直接返回就绪，不重复起
        try:
            with socket.create_connection(("127.0.0.1", 7871), timeout=1):
                return {"ok": True, "msg": "Qwen3-TTS 服务已在运行，可直接使用。"}
        except Exception:
            pass

        try:
            # macOS 用 ~/Library/Logs；Windows 没有这个约定（会建出个空的 Library\Logs），
            # 按 Windows 习惯放 %LOCALAPPDATA%\VideoDownloader\logs，便于用户/支持排查
            if sys.platform == "win32":
                _log_dir = os.path.join(
                    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local"),
                    "VideoDownloader", "logs")
            else:
                _log_dir = os.path.expanduser("~/Library/Logs")
            log_path = os.path.join(_log_dir, "qwen3tts_server.log")
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "a") as lf:
                subprocess.Popen(
                    [py, script, "--host", "127.0.0.1", "--port", "7871"],
                    stdout=lf,
                    stderr=lf,
                    start_new_session=True,
                )
            return {
                "ok": True,
                "msg": "已开始启动 Qwen3-TTS 服务，首次加载权重约 30–70 秒（MLX 走 Apple GPU）。"
                       "状态变绿（已就绪）后再开始渲染；即使提前点渲染，本轮也会等到服务就绪，"
                       "不会再静默回退 edge。",
            }
        except Exception as exc:
            return {"ok": False, "msg": f"启动失败：{exc}"}

    def stop_qwen3tts(self) -> dict:
        """切走「Qwen3-TTS 本地语音克隆」引擎时由前端自动调用：停掉本机 7871 服务，省内存。"""
        import subprocess
        if sys.platform == "win32":
            # 2026-10-20：Windows 无 pkill，原实现必然抛 FileNotFoundError →
            # 返回「停止失败」且服务继续占内存。改为按端口精确定位再 taskkill。
            return _windows_stop_port_listener(7871, "Qwen3-TTS")
        try:
            subprocess.run(["pkill", "-f", "start_qwen3tts_server.py"], check=False)
            return {"ok": True, "msg": "已停止 Qwen3-TTS 本地服务（切换其他引擎时自动释放）。"}
        except Exception as exc:
            return {"ok": False, "msg": f"停止失败：{exc}"}

    def save_commentary_file(self, job_id: str, filename: str) -> str:
        import requests
        from pathlib import Path
        url = f"http://{HOST}:{PORT}/api/commentary/{job_id}/file"
        downloads = Path.home() / "Downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        name = filename or "解说成片.mp4"
        dest = downloads / name
        # 避免覆盖已有文件
        if dest.exists():
            stem, suf = dest.stem, dest.suffix
            i = 1
            while dest.exists():
                dest = downloads / f"{stem}({i}){suf}"
                i += 1
        try:
            r = requests.get(url, timeout=(10, 600))
            r.raise_for_status()
            dest.write_bytes(r.content)
        except Exception as exc:  # 把错误回传前端展示
            return f"ERROR: {exc}"
        return str(dest)

    def save_direct_url(self, url: str, filename: str, referer: str = "", ua: str = "") -> str:
        """把「直链视频」保存到用户**自选的位置**（桌面版原生直存）。

        流程：先弹系统「保存」面板（默认「下载」文件夹 + 视频标题），用户选定路径后由
        Python 带 Referer/UA 拉流写盘；用户点「取消」返回 "CANCELLED"（前端只提示不报错）。

        ★ 2026-09-22 修复「直接保存到本机 = 应用界面变成 403 页」：
          旧实现是前端 `<a href=直链 download>`，而桌面壳是 WKWebView —— 点击会把
          **整个应用界面导航**到该 URL；抖音/快手等字节系 CDN 校验 Referer，浏览器
          拿不到（Referer 是 forbidden header），于是返回 403 openresty 页面，
          应用界面直接消失，用户只能重开 App。
          现在改由本方法在 Python 侧带 Referer/UA 拉流写盘：既不导航 WebView，
          又满足防盗链（后端下载同源，仍不经过外网服务器）。

        ★ 2026-09-22 用户要求「也要弹出来可选择保存的位置弹窗」：落盘前先过
          `_choose_save_path()`（osascript 原生保存面板），不再固定塞进「下载」。

        referer/ua 由后端 resolve 下发的 video.direct_headers 提供；缺省时按
        「无防盗链的裸文件直链」处理（只发一个普通浏览器 UA）。
        返回保存的绝对路径；取消返回 "CANCELLED"；失败返回 "ERROR: ..."
        （前端据此自动回落服务器下载）。
        """
        import os as _os
        import re as _re
        import requests
        from pathlib import Path

        url = (url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return "ERROR: 无效的直链地址"
        # 文件名清洗：去掉路径分隔符与文件系统非法字符，兜底一个扩展名
        name = (filename or "视频").strip() or "视频"
        name = _re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", name).strip(" .") or "视频"
        if not _re.search(r"\.[A-Za-z0-9]{2,5}$", name):
            name += ".mp4"
        if len(name) > 120:  # 超长文件名部分文件系统会失败
            stem, dot, suf = name.rpartition(".")
            name = (stem[:110] + dot + suf) if dot else name[:120]

        downloads = Path.home() / "Downloads"
        try:
            downloads.mkdir(parents=True, exist_ok=True)
        except Exception:
            downloads = Path.home()

        # ★ 2026-09-22 用户要求：直存也要先弹系统「保存到哪里」面板（与去水印/文案/二维码
        #   保存一致），而不是一声不吭塞进「下载」文件夹。默认位置仍是「下载」，
        #   默认名是解析出来的视频标题，用户直接回车＝老行为（零学习成本）。
        chosen = _choose_save_path("保存视频到", name, "/tmp/vdl_direct_save.log")
        if chosen == "CANCELLED":
            return "CANCELLED"
        dest = Path(chosen) if chosen else (downloads / name)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            return f"ERROR: 无法写入所选目录（{exc}）"
        if dest.exists():  # 不覆盖已有文件
            stem, suf = dest.stem, dest.suffix
            i = 1
            while dest.exists():
                dest = dest.parent / f"{stem}({i}){suf}"
                i += 1

        headers = {
            "User-Agent": (ua or "").strip() or (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        }
        if (referer or "").strip():
            headers["Referer"] = referer.strip()

        tmp = dest.with_name(dest.name + ".part")
        try:
            with requests.get(url, headers=headers, stream=True, timeout=(15, 600)) as r:
                if r.status_code >= 400:
                    return f"ERROR: 源站返回 HTTP {r.status_code}（防盗链或链接已过期）"
                written = 0
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=256 * 1024):
                        if chunk:
                            fh.write(chunk)
                            written += len(chunk)
            if written <= 0:
                try:
                    _os.remove(tmp)
                except OSError:
                    pass
                return "ERROR: 源站返回空文件"
            _os.replace(tmp, dest)
        except Exception as exc:  # 把错误回传前端展示，并让前端回落服务器下载
            try:
                if tmp.exists():
                    _os.remove(tmp)
            except OSError:
                pass
            return f"ERROR: {exc}"
        return str(dest)

    def save_task_file(self, task_id: str, suggested_name: str = "",
                       device: str = "", token: str = "") -> str:
        """把某个下载任务（视频 / 图片）的成品另存到**用户自选的位置**（原生桥）。

        为什么必须走原生桥（2026-10-12 用户实测「保存到本机点了没反应，文件埋在
        一堆哈希文件夹里，我上哪找去」）：
          旧实现是前端 `<a href=/api/tasks/{id}/file download>`。桌面壳是 pywebview
          的 WKWebView，`<a download>` 想弹系统保存面板必须开
          `webview.settings['ALLOW_DOWNLOADS']`（默认 False，本项目从未开启）——
          于是点击被**静默吞掉**：不报错、不弹框、不落盘。用户以为保存了，其实
          唯一的一份文件还在 `~/Downloads/VideoDownloader/<任务号>/` 里，极难找。
        本方法复用 `_choose_save_path()`（去水印 / 文案 / 二维码 / 解说成片同款原生
        面板，默认位置「下载」且预填好文件名），用户选定后由 Python 从本机后端拉文件
        写入 —— 用户明确知道文件去了哪，返回的绝对路径还会显示在界面上。

        参数：device/token 用于通过后端归属校验（登录用 token，未登录用设备标识；
        两者都带最稳）。返回保存的绝对路径；取消返回 "CANCELLED"；失败返回 "ERROR: ..."。
        """
        import re as _re
        import urllib.parse as _urlparse
        import datetime as _dt
        from pathlib import Path

        def _log(msg: str) -> None:
            try:
                with open("/tmp/vdl_task_save.log", "a") as f:
                    f.write(f"[{_dt.datetime.now().isoformat()}] {msg}\n")
            except Exception:
                pass

        tid = str(task_id or "").strip()
        # 任务 id 只可能是十六进制短串：白名单校验顺带堵住把它拼进 URL 路径的口子
        if not _re.fullmatch(r"[A-Za-z0-9_-]{4,64}", tid):
            return "ERROR: 无效的任务编号"
        suggested = _re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", str(suggested_name or "")).strip(" .")
        if not suggested:
            suggested = "下载文件"
        if len(suggested) > 120:              # 超长名部分文件系统会失败
            stem, dot, suf = suggested.rpartition(".")
            suggested = (stem[:110] + dot + suf) if dot else suggested[:120]

        downloads = Path.home() / "Downloads"
        try:
            downloads.mkdir(parents=True, exist_ok=True)
        except Exception:
            downloads = Path.home()

        try:
            chosen = _choose_save_path("保存到", suggested, "/tmp/vdl_task_save.log")
        except Exception as exc:  # noqa: BLE001 - 面板异常不该让「保存」变成崩溃
            _log(f"choose-save-path exception: {exc!r}")
            chosen = ""
        if chosen == "CANCELLED":
            return "CANCELLED"
        dest = Path(chosen) if chosen else (downloads / suggested)

        url = f"http://{HOST}:{PORT}/api/tasks/{tid}/file?download=1"
        if device:
            url += "&device=" + _urlparse.quote(str(device))
        headers = {}
        if device:
            headers["X-Device-Id"] = str(device)
        if token:
            headers["Authorization"] = "Bearer " + str(token)
        try:
            saved = _download_url_to_path(url, str(dest), headers=headers)
        except Exception as exc:  # noqa: BLE001 - 把原因交回前端展示
            _log(f"write error: {exc!r}")
            return f"ERROR: {exc}"
        _log(f"saved -> {saved}")
        return saved

    def reveal_path(self, path: str) -> str:
        """在系统文件管理器里定位并选中该文件（访达 / 资源管理器 / xdg）。

        配合 `save_task_file` 使用：用户在提示里点「在访达中显示」就能直接看到文件
        ——「文件到底在哪」不该靠用户自己在一堆目录里翻。返回 "OK" 或 "ERROR: ..."。
        """
        import os as _os
        import subprocess
        p = _os.path.expanduser(str(path or "").strip())
        if not p or not _os.path.exists(p):
            return "ERROR: 文件不存在（可能已被移动或删除）"
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-R", p])
            elif sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", _os.path.normpath(p)])
            else:
                subprocess.Popen(["xdg-open", _os.path.dirname(p) or "."])
            return "OK"
        except Exception as exc:  # noqa: BLE001
            return f"ERROR: {exc}"

    def save_dw_file_dialog(self, job_id: str, kind: str, suggested_name: str) -> str:
        """弹出系统保存面板让用户自选去水印结果位置（桌面版原生下载）。

        与 save_commentary_file_dialog 同一套机制：用 osascript `choose file name`
        弹原生窗口，避免 pywebview 主线程 run loop 阻塞（NSSavePanel 在主线程被同步
        JS 调用卡死）。取消返回 "CANCELLED"；osascript 不可用时退化为存「下载」文件夹。
        """
        import json
        import os
        import tempfile
        import requests
        import subprocess
        import datetime as _dt
        import threading as _th
        from pathlib import Path

        if kind not in ("image", "pdf", "video"):
            return "ERROR: 未知的去水印类型"
        url = f"http://{HOST}:{PORT}/api/dw/{kind}/{job_id}/file"
        # 默认建议文件名：图片→png / PDF→pdf / 视频→mp4（缺失或用户未给时兜底）
        if suggested_name:
            suggested = suggested_name
        elif kind == "image":
            suggested = "dewatered.png"
        elif kind == "pdf":
            suggested = "dewatered.pdf"
        else:
            suggested = "dewatered.mp4"
        downloads = Path.home() / "Downloads"
        try:
            downloads.mkdir(parents=True, exist_ok=True)
        except Exception:
            downloads = Path.home()

        def _log(msg):
            try:
                with open("/tmp/vdl_dw_save.log", "a") as f:
                    f.write(f"[{_dt.datetime.now().isoformat()}] {msg}\n")
            except Exception:
                pass

        dest = None
        try:
            name_json = json.dumps(suggested, ensure_ascii=False)
            prompt = "保存去水印视频" if kind == "video" else ("保存去水印结果" if kind == "image" else "保存去水印 PDF")
            # ★ 统一走 `_choose_save_path`（中文本地化面板；不可用时内部自动回落 osascript）
            chosen = _choose_save_path(prompt, suggested, "/tmp/vdl_dw_save.log")
            if chosen == "CANCELLED":
                return "CANCELLED"
            dest = chosen or None
            _log(f"chose: {dest!r}")
        except Exception as e:
            _log(f"choose-save-path exception: {e!r}")
            dest = None

        if not dest:
            # 兜底：面板机制整体不可用时存到下载文件夹（符合默认行为）。
            dest = str(downloads / suggested)

        target = Path(dest)
        try:
            resp = requests.get(url, timeout=(10, 600))
            resp.raise_for_status()
            target.parent.mkdir(parents=True, exist_ok=True)
            # 避免覆盖已有文件
            if target.exists():
                stem, suf = target.stem, target.suffix
                i = 1
                while target.exists():
                    target = target.parent / f"{stem}({i}){suf}"
                    i += 1
            target.write_bytes(resp.content)
        except Exception as exc:
            _log(f"write error: {exc!r}")
            return f"ERROR: {exc}"
        _log(f"saved -> {target}")
        return str(target)

    def save_text_file_dialog(self, text: str, suggested_name: str) -> str:
        """弹出系统保存面板，把传入的文案文本直接写成 .txt 文件（桌面版原生保存）。

        与 save_dw_file_dialog 同套机制：用 osascript `choose file name` 子进程弹原生
        窗口，绕开 pywebview 主线程 run loop 阻塞。内容来自 JS 直接传入，不再走后端下载。
        取消返回 "CANCELLED"；osascript 不可用时退化为存「下载」文件夹（默认行为）。
        """
        import json
        import os
        import tempfile
        import subprocess
        import datetime as _dt
        from pathlib import Path

        suggested = (suggested_name or "提取文案.txt").strip() or "提取文案.txt"
        # 🔴 2026-09-22 缺陷修复：此前无条件 `not endswith('.txt') 就追加 .txt`，
        #    把「下载 SRT 字幕」传来的 xxx.srt 强改成 xxx.srt.txt —— 用户拿到
        #    双后缀文件，拖进播放器/剪映识别不了字幕。正确规则：只要文件名已带
        #    任意后缀就原样保留，仅对「无后缀」的裸名兜底补 .txt。
        if not os.path.splitext(suggested)[1]:
            suggested += ".txt"
        downloads = Path.home() / "Downloads"
        try:
            downloads.mkdir(parents=True, exist_ok=True)
        except Exception:
            downloads = Path.home()

        def _log(msg):
            try:
                with open("/tmp/vdl_text_save.log", "a") as f:
                    f.write(f"[{_dt.datetime.now().isoformat()}] {msg}\n")
            except Exception:
                pass

        dest = None
        try:
            name_json = json.dumps(suggested, ensure_ascii=False)
            prompt = "保存提取文案为文本"
            # ★ 统一走 `_choose_save_path`（中文本地化面板；不可用时内部自动回落 osascript）
            chosen = _choose_save_path(prompt, suggested, "/tmp/vdl_text_save.log")
            if chosen == "CANCELLED":
                return "CANCELLED"
            dest = chosen or None
            _log(f"chose: {dest!r}")
        except Exception as e:
            _log(f"choose-save-path exception: {e!r}")
            dest = None

        if not dest:
            # 兜底：面板机制整体不可用时存到下载文件夹（符合默认行为）。
            dest = str(downloads / suggested)

        target = Path(dest)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # 避免覆盖已有文件
            if target.exists():
                stem, suf = target.stem, target.suffix
                i = 1
                while target.exists():
                    target = target.parent / f"{stem}({i}){suf}"
                    i += 1
            target.write_text(text or "", encoding="utf-8")
        except Exception as exc:
            _log(f"write error: {exc!r}")
            return f"ERROR: {exc}"
        _log(f"saved -> {target}")
        return str(target)

    def save_qr_image_dialog(self, data_url: str, suggested_name: str) -> str:
        """把「扫码分享」的二维码 PNG 存到用户选定位置（桌面版原生保存）。

        为什么必须走原生桥：**WKWebView 不支持 `<a download>` 的 blob 下载**——
        在 App 里点「保存二维码」不会弹任何保存框，而是把**主框架导航**到 blob: 图片，
        整个 App 界面被替换成一张二维码、只能重启（用户 2026-09-21 报的
        「点击保存二维码有问题」正是此现象，已在真机复现取证）。
        所以前端把已经取到的 PNG 转成 data URL 传进来，这里解 base64 直接落盘。

        与 save_text_file_dialog / save_matting_file_dialog 同套机制：
        弹系统原生保存窗口（中文本地化 applet；不可用时内部回落 osascript），绕开 pywebview 主线程
        run loop 阻塞。取消返回 "CANCELLED"；osascript 不可用时退化为存「下载」文件夹。
        """
        import base64
        import binascii
        import json
        import os
        import tempfile
        import subprocess
        import datetime as _dt
        from pathlib import Path

        def _log(msg):
            try:
                with open("/tmp/vdl_qr_save.log", "a") as f:
                    f.write(f"[{_dt.datetime.now().isoformat()}] {msg}\n")
            except Exception:
                pass

        raw = (data_url or "").strip()
        if raw.startswith("data:"):
            # data:image/png;base64,XXXX → 取逗号后的负载
            comma = raw.find(",")
            raw = raw[comma + 1:] if comma >= 0 else ""
        raw = "".join(raw.split())  # 去掉换行/空格，防止桥接传输插入的空白
        try:
            blob = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            _log(f"base64 decode failed: {exc!r}")
            return f"ERROR: 二维码数据解析失败（{exc}）"
        if not blob.startswith(b"\x89PNG\r\n\x1a\n"):
            _log(f"not a png, head={blob[:8]!r} len={len(blob)}")
            return "ERROR: 二维码数据不是有效 PNG"

        suggested = (suggested_name or "分享二维码.png").strip() or "分享二维码.png"
        if not suggested.lower().endswith(".png"):
            suggested += ".png"
        downloads = Path.home() / "Downloads"
        try:
            downloads.mkdir(parents=True, exist_ok=True)
        except Exception:
            downloads = Path.home()
        _log(f"enter save_qr_image_dialog suggested={suggested!r} bytes={len(blob)}")

        dest = None
        try:
            name_json = json.dumps(suggested, ensure_ascii=False)
            # ★ 统一走 `_choose_save_path`（中文本地化面板；不可用时内部自动回落 osascript）
            chosen = _choose_save_path("保存二维码", suggested, "/tmp/vdl_qr_save.log")
            if chosen == "CANCELLED":
                return "CANCELLED"
            dest = chosen or None
            _log(f"chose: {dest!r}")
        except Exception as e:
            _log(f"choose-save-path exception: {e!r}")
            dest = None

        if not dest:
            dest = str(downloads / suggested)

        target = Path(dest)
        try:
            if target.suffix.lower() != ".png":
                target = target.with_suffix(target.suffix + ".png")
            target.parent.mkdir(parents=True, exist_ok=True)
            # 避免覆盖已有文件
            if target.exists():
                stem, suf = target.stem, target.suffix
                i = 1
                while target.exists():
                    target = target.parent / f"{stem}({i}){suf}"
                    i += 1
            target.write_bytes(blob)
        except Exception as exc:
            _log(f"write error: {exc!r}")
            return f"ERROR: {exc}"
        _log(f"saved -> {target} ({target.stat().st_size} bytes)")
        return str(target)

    def save_commentary_file_dialog(self, cid: str, suggested_name: str) -> str:
        """弹出系统保存面板（默认目录=下载文件夹、预填文件名），用户可改位置/重命名。

        实现：用 osascript `choose file name` 子进程弹原生窗口。它不受 pywebview
        主线程 run loop 阻塞影响（之前的 NSSavePanel 在主线程被同步 JS 调用卡住，
        导致面板永远弹不出、后台线程永久挂起）。取消返回 "CANCELLED"；若 osascript
        不可用则退化为直接保存到「下载」文件夹（即默认行为）。
        """
        import json
        import os
        import tempfile
        import requests
        import subprocess
        import datetime as _dt
        import threading as _th
        from pathlib import Path

        def _log(msg):
            try:
                with open("/tmp/vdl_save.log", "a") as f:
                    f.write(f"[{_dt.datetime.now().isoformat()}] {msg}\n")
            except Exception:
                pass

        url = f"http://{HOST}:{PORT}/api/commentary/{cid}/file"
        suggested = suggested_name or "解说成片.mp4"
        downloads = Path.home() / "Downloads"
        try:
            downloads.mkdir(parents=True, exist_ok=True)
        except Exception:
            downloads = Path.home()
        _log(f"enter thread={_th.current_thread().name} main={_th.current_thread() is _th.main_thread()} suggested={suggested!r}")

        dest = None
        # 主路径：osascript choose file name —— 真正的原生保存窗口，
        # 默认位置=下载文件夹、预填文件名，用户可改位置/重命名。任意线程可用。
        # 关键：必须写入临时 .applescript 文件（UTF-8）再 `osascript <file>` 执行，
        # 不能走 `osascript -e <脚本>` —— 中文经 argv 传给 osascript 时会被错误解码，
        # 触发「syntax error: 预期是引号，却找到未知的记号」导致面板不弹。
        try:
            name_json = json.dumps(suggested, ensure_ascii=False)
            # ★ 统一走 `_choose_save_path`（中文本地化面板；不可用时内部自动回落 osascript）
            chosen = _choose_save_path("保存解说成片", suggested, "/tmp/vdl_save.log")
            if chosen == "CANCELLED":
                return "CANCELLED"
            dest = chosen or None
            _log(f"chose: {dest!r}")
        except Exception as e:
            _log(f"choose-save-path exception: {e!r}")
            dest = None

        if not dest:
            # 兜底：面板机制整体不可用时直接存到下载文件夹（符合默认行为）。
            _log("fallback -> ~/Downloads copy")
            dest = str(downloads / suggested)

        target = Path(dest)
        try:
            resp = requests.get(url, timeout=(10, 600))
            resp.raise_for_status()
            target.parent.mkdir(parents=True, exist_ok=True)
            # 避免覆盖已有文件
            if target.exists():
                stem, suf = target.stem, target.suffix
                i = 1
                while target.exists():
                    target = target.parent / f"{stem}({i}){suf}"
                    i += 1
            target.write_bytes(resp.content)
        except Exception as exc:
            _log(f"write error: {exc!r}")
            return f"ERROR: {exc}"
        _log(f"saved -> {target}")
        return str(target)

    def save_matting_file_dialog(self, job_id: str, suggested_name: str) -> str:
        """弹出系统保存面板让用户自选「一键抠图」透明 PNG 位置（桌面版原生下载）。

        与 save_commentary_file_dialog / save_dw_file_dialog 同套机制：
        弹系统原生保存窗口（中文本地化 applet；不可用时内部回落 osascript），绕开 pywebview 主线程
        run loop 阻塞。取消返回 "CANCELLED"；osascript 不可用时退化为存「下载」文件夹。
        """
        import json
        import os
        import tempfile
        import requests
        import subprocess
        import datetime as _dt
        from pathlib import Path

        def _log(msg):
            try:
                with open("/tmp/vdl_matting_save.log", "a") as f:
                    f.write(f"[{_dt.datetime.now().isoformat()}] {msg}\n")
            except Exception:
                pass

        url = f"http://{HOST}:{PORT}/api/matting/image/{job_id}/file"
        suggested = suggested_name or "matting.png"
        downloads = Path.home() / "Downloads"
        try:
            downloads.mkdir(parents=True, exist_ok=True)
        except Exception:
            downloads = Path.home()
        _log(f"enter save_matting_file_dialog job_id={job_id} suggested={suggested!r}")

        dest = None
        try:
            name_json = json.dumps(suggested, ensure_ascii=False)
            # ★ 统一走 `_choose_save_path`（中文本地化面板；不可用时内部自动回落 osascript）
            chosen = _choose_save_path("保存抠图结果", suggested, "/tmp/vdl_matting_save.log")
            if chosen == "CANCELLED":
                return "CANCELLED"
            dest = chosen or None
            _log(f"chose: {dest!r}")
        except Exception as e:
            _log(f"choose-save-path exception: {e!r}")
            dest = None

        if not dest:
            dest = str(downloads / suggested)

        target = Path(dest)
        try:
            resp = requests.get(url, timeout=(10, 600))
            resp.raise_for_status()
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                stem, suf = target.stem, target.suffix
                i = 1
                while target.exists():
                    target = target.parent / f"{stem}({i}){suf}"
                    i += 1
            target.write_bytes(resp.content)
        except Exception as exc:
            _log(f"write error: {exc!r}")
            return f"ERROR: {exc}"
        _log(f"saved -> {target}")
        return str(target)

    def save_convert_file_dialog(self, job_id: str, suggested_name: str) -> str:
        """弹出系统保存面板让用户自选「格式转换 / 高效压缩」结果位置（桌面版原生下载）。

        同时服务两条链路：转换/桥接任务（app.CONVERT_JOBS）与压缩任务
        （routers.compress.COMPRESS_JOBS），按 job_id 依次查两个注册表。

        与 save_commentary_file_dialog / save_dw_file_dialog 同套机制：
        弹系统原生保存窗口（中文本地化 applet；不可用时内部回落 osascript），绕开 pywebview 主线程
        run loop 阻塞（NSSavePanel 在主线程被同步 JS 调用卡死的问题）。取消返回
        "CANCELLED"；osascript 不可用时退化为存「下载」文件夹。
        """
        import json
        import os
        import tempfile
        import requests
        import subprocess
        import datetime as _dt
        import threading as _th
        from pathlib import Path

        def _log(msg):
            try:
                with open("/tmp/vdl_convert_save.log", "a") as f:
                    f.write(f"[{_dt.datetime.now().isoformat()}] {msg}\n")
            except Exception:
                pass

        _log(f"enter save_convert_file_dialog thread={_th.current_thread().name} job_id={job_id} suggested={suggested_name!r}")

        # 进程内直接读任务注册表的 out_path —— launcher 与 FastAPI 服务在同一进程
        # （uvicorn.run 跑在 daemon 线程），共享同一份模块内存。这样彻底绕开之前的
        # `requests.get(http://127.0.0.1:PORT/api/convert/.../file)` 路径：launcher 端不携带
        # X-Device-Id header 也不带 device= query，会被设备隔离校验判 404（2026-08-28 实测）。
        # ⚠️ 两个注册表都要查（2026-09-11）：转换/桥接在 app.CONVERT_JOBS，压缩在
        # routers.compress.COMPRESS_JOBS。只查前者会让「高效压缩」的下载按钮报
        # 「任务不存在或已过期」。
        src_path = None
        is_compress = False
        try:
            import app as _vdl_app
            registries = []
            if hasattr(_vdl_app, "CONVERT_JOBS"):
                registries.append(("CONVERT_JOBS", _vdl_app.CONVERT_JOBS))
            try:
                from routers.compress import COMPRESS_JOBS as _CJOBS
                registries.append(("COMPRESS_JOBS", _CJOBS))
            except Exception as _e:
                _log(f"import COMPRESS_JOBS failed: {_e!r}")
            try:
                from routers.sr import SR_JOBS as _SJOBS
                registries.append(("SR_JOBS", _SJOBS))
            except Exception as _e:
                _log(f"import SR_JOBS failed: {_e!r}")
            for _name, _reg in registries:
                job = _reg.get(job_id) if isinstance(_reg, dict) else None
                if job and job.get("out_path"):
                    src_path = job.get("out_path")
                    is_compress = (_name == "COMPRESS_JOBS")
                    _log(f"in-memory hit registry={_name} status={job.get('status')} out={src_path!r}")
                    break
        except Exception as e:
            _log(f"in-memory read error: {e!r}")
            return f"ERROR: 读任务失败：{e}"

        if not src_path:
            return f"ERROR: 任务不存在或已过期（job_id={job_id}）"

        src_path = str(src_path)
        if not Path(src_path).exists():
            return f"ERROR: 产物文件已丢失（{src_path}）"

        suggested = (suggested_name or "").strip() or Path(src_path).name
        downloads = Path.home() / "Downloads"
        try:
            downloads.mkdir(parents=True, exist_ok=True)
        except Exception:
            downloads = Path.home()

        dest = None
        try:
            name_json = json.dumps(suggested, ensure_ascii=False)
            _prompt = "保存压缩结果" if is_compress else "保存转码结果"
            # ★ 统一走 `_choose_save_path`（中文本地化面板；不可用时内部自动回落 osascript）
            chosen = _choose_save_path(_prompt, suggested, "/tmp/vdl_convert_save.log")
            if chosen == "CANCELLED":
                return "CANCELLED"
            dest = chosen or None
            _log(f"chose: {dest!r}")
        except Exception as e:
            _log(f"choose-save-path exception: {e!r}")
            dest = None

        if not dest:
            _log("fallback -> ~/Downloads copy")
            dest = str(downloads / suggested)

        target = Path(dest)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                stem, suf = target.stem, target.suffix
                i = 1
                while target.exists():
                    target = target.parent / f"{stem}({i}){suf}"
                    i += 1
            # 直接从已落盘的产物拷到目标；同进程内 read + copy，不走 HTTP，零 404 风险
            import shutil as _sh
            _sh.copyfile(src_path, target)
        except Exception as exc:
            _log(f"write error: {exc!r}")
            return f"ERROR: {exc}"
        _log(f"saved -> {target}")
        return str(target)

    # ── 窗口关闭 / 退出 行为区分（macOS 原生窗口）──
    # 设计：
    #   * 点窗口红叉 / 页面 X  → 仅「返回桌面」(最小化窗口，软件继续在后台运行)
    #   * 只有在窗口内显式点击「退出」按钮 → 真正退出整程序
    # 这样避免用户误触红叉把整个软件关掉。
    def hide_to_desktop(self) -> None:
        """返回桌面：最小化窗口，软件继续后台运行（点页面 X / 窗口红叉走这条）。"""
        try:
            if getattr(self, "window", None) is not None:
                self.window.minimize()
        except Exception:
            pass

    def quit_app(self) -> None:
        """真正退出软件（仅当用户在窗口内显式点击「退出」按钮时调用）。"""
        global _quitting
        _quitting = True  # 标记正在退出，让 closing 拦截器放行
        os._exit(0)       # 强制退出（不经过 closing 事件循环）

    def trigger_update(self, version: str, token: str = None) -> dict:
        """原生 bridge：在 Python 端（不经过 WKWebView 的 fetch）向后端发起更新。
        后端立即返回 job_id，前端再轮询 update_status 获取进度。彻底规避 WebKit 偶发拦截。
        token：登录态 Bearer token（前端从 localStorage 取出后透传），满足后端 _require_user 鉴权；
        不传则后端返回 NO_AUTH，更新无法启动。"""
        try:
            import json as _json
            import urllib.request as _urllib
            url = f"http://{HOST}:{PORT}/api/system/update"
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = "Bearer " + str(token)
            req = _urllib.Request(
                url,
                data=_json.dumps({"version": version}).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with _urllib.urlopen(req, timeout=60) as resp:
                return _json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return {"ok": False, "error": "触发更新失败：%s" % e}

    def update_status(self, job_id: str) -> dict:
        """原生 bridge：轮询更新任务进度（短 GET，不经过 WebKit fetch）。"""
        try:
            import json as _json
            import urllib.request as _urllib
            url = f"http://{HOST}:{PORT}/api/system/update/status?job={job_id}"
            with _urllib.urlopen(url, timeout=10) as resp:
                return _json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return {"ok": False, "error": "查询更新状态失败：%s" % e}

def _handle_exit(*_args) -> None:
    os._exit(0)

def _setup_win_job_object() -> None:
    """Windows 作业对象（Job Object）：主进程入"杀子作业"，进程树全灭。

    背景（2026-10-18 22:55 实测）：用户机器上残留 9 个孤儿 aria2c + 6 个孤儿
    msedgewebview2——VDL 主进程被 taskkill /F、崩溃或被安装器关停后，后端线程
    subprocess 出来的子进程无人认领继续跑：8 个 aria2c 抢同一个做种任务的
    控制文件（有损坏风险）、~250MB 内存泄漏，还挡住安装程序更新文件（Inno
    Setup 弹"以下应用程序正在使用将由安装程序更新的文件"列出一堆 aria2c）。

    后端是主进程内的**线程**（daemon=True），线程随主进程死，但它 spawn 的
    子进程不会——Python subprocess 不建立进程树关联。
    正解：CreateJobObjectW + KILL_ON_JOB_CLOSE。主进程入动作业后所有后代
    自动入作业；主进程退出（含 crash / taskkill /F / 被安装器关停）时内核
    关闭作业句柄 → 作业内全部进程被级联终结，从机制上杜绝孤儿。

    BREAKAWAY_OK：放行应用内更新的安装器子进程破例脱离作业（见
    server/routers/system.py 的更新流程：安装器要活得比本进程久，否则
    /CLOSEAPPLICATIONS 关停本进程 → 作业关闭 → 安装器被级联杀掉，更新烂尾）。
    注意：DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP **不能**脱离作业，
    安装器侧必须配对 CREATE_BREAKAWAY_FROM_JOB 才生效。

    失败不致命（Win7 嵌套作业限制 / 进程已被别的作业持有等）：记日志继续跑。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes as _ct
        from ctypes import wintypes as _w
        _k = _ct.windll.kernel32

        class _JOBOBJECT_BASIC_LIMIT_INFORMATION(_ct.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", _w.LARGE_INTEGER),
                ("PerJobUserTimeLimit", _w.LARGE_INTEGER),
                ("LimitFlags", _w.DWORD),
                ("MinimumWorkingSetSize", _ct.c_size_t),
                ("MaximumWorkingSetSize", _ct.c_size_t),
                ("ActiveProcessLimit", _w.DWORD),
                ("Affinity", _ct.c_size_t),
                ("PriorityClass", _w.DWORD),
                ("SchedulingClass", _w.DWORD),
            ]

        class _IO_COUNTERS(_ct.Structure):
            _fields_ = [
                ("ReadOperationCount", _ct.c_ulonglong),
                ("WriteOperationCount", _ct.c_ulonglong),
                ("OtherOperationCount", _ct.c_ulonglong),
                ("ReadTransferCount", _ct.c_ulonglong),
                ("WriteTransferCount", _ct.c_ulonglong),
                ("OtherTransferCount", _ct.c_ulonglong),
            ]

        class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(_ct.Structure):
            _fields_ = [
                ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", _ct.c_size_t),
                ("JobMemoryLimit", _ct.c_size_t),
                ("PeakProcessMemoryUsed", _ct.c_size_t),
                ("PeakJobMemoryUsed", _ct.c_size_t),
            ]

        _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
        _JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
        _JobObjectExtendedLimitInformation = 9

        # 🔴 ctypes 坑（2026-10-18 实测 err=6）：GetCurrentProcess 返回伪句柄
        #    0xFFFFFFFF，restype 默认 32 位 c_int → Win64 传参高 32 位是垃圾 →
        #    AssignProcessToJobObject 报 ERROR_INVALID_HANDLE。HANDLE 类函数
        #    必须显式声明 restype/argtypes，让伪句柄零扩展成完整 64 位。
        _k.CreateJobObjectW.restype = _w.HANDLE
        _k.AssignProcessToJobObject.argtypes = [_w.HANDLE, _w.HANDLE]
        _k.AssignProcessToJobObject.restype = _w.BOOL
        _k.SetInformationJobObject.argtypes = [_w.HANDLE, _ct.c_int, _ct.c_void_p, _w.DWORD]
        _k.SetInformationJobObject.restype = _w.BOOL
        _k.GetCurrentProcess.restype = _w.HANDLE

        _hJob = _k.CreateJobObjectW(None, None)
        if not _hJob:
            raise OSError(f"CreateJobObjectW 失败: err={_k.GetLastError()}")
        _info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        _info.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_BREAKAWAY_OK
        )
        if not _k.SetInformationJobObject(
            _hJob, _JobObjectExtendedLimitInformation,
            _ct.byref(_info), _ct.sizeof(_info),
        ):
            _err = _k.GetLastError()
            _k.CloseHandle(_hJob)
            raise OSError(f"SetInformationJobObject 失败: err={_err}")
        if not _k.AssignProcessToJobObject(_hJob, _k.GetCurrentProcess()):
            _err = _k.GetLastError()
            _k.CloseHandle(_hJob)
            raise OSError(f"AssignProcessToJobObject 失败: err={_err}")
        # 句柄故意不关：进程存活期持有；进程退出时内核自动关 → 触发级联终结。
        globals()["_VDL_WIN_JOB_HANDLE"] = _hJob
        _launch_log("Windows 作业对象已就位（KILL_ON_JOB_CLOSE）：主进程退出将级联终结 aria2c/ffmpeg/WebView2 等全部子进程")
    except Exception as _e:
        _launch_log(f"Windows 作业对象设置失败（非致命，仍按旧行为运行）: {_e!r}")

def _pid_alive(pid: int) -> bool:
    """进程存活判定。

    🔴 2026-10-19 修复：Windows 上必须用 OpenProcess+GetExitCodeProcess，
    不能用 os.kill(pid, 0)——PyInstaller windowed 冻结进程（GUI 子系统、
    无控制台）里 os.kill(pid,0) 对活进程抛 OSError(EBADF/ERROR_INVALID_HANDLE)
    （实测：同一活进程，console python 报 OK，冻结 exe 报句柄无效），
    导致接管判定 _pid_alive=False → 旧版实例漏杀 → 新版不接管。
    顺带解决僵尸误报：进程已死但别处持有句柄时，GetExitCodeProcess
    返回退出码（非 STILL_ACTIVE），而 os.kill 的 OpenProcess 对僵尸仍成功。

    🔴 2026-10-26 补充（「双击出 4 个窗口」根因之一）：win32 分支出错时
    **绝不能**掉到下面的 os.kill(pid, 0) —— CPython 在 Windows 上把非 CTRL
    信号一律交给 TerminateProcess 执行，sig=0 不是「探测」而是**把对方杀掉**
    （退出码 0）。⇒ win32 路径任何异常一律判定「不存活」，不回落 os.kill。
    """
    if sys.platform == "win32":
        try:
            import ctypes as _ct
            from ctypes import wintypes as _w
            _k = _ct.windll.kernel32
            _k.OpenProcess.restype = _w.HANDLE
            _k.OpenProcess.argtypes = [_w.DWORD, _w.BOOL, _w.DWORD]
            _h = _k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
            if not _h:
                return False
            try:
                _code = _w.DWORD()
                if _k.GetExitCodeProcess(_h, _ct.byref(_code)):
                    return _code.value == 259  # STILL_ACTIVE
                return False
            finally:
                _k.CloseHandle(_h)
        except Exception:
            # 不回落到 os.kill —— Windows 上 os.kill(pid, 0) == TerminateProcess(pid)
            return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True

def _pid_is_vdl(pid: int) -> bool:
    """Windows：用 tasklist 校验 pid 对应进程确为本程序。

    版本接管要 taskkill 掉锁文件里的 pid——若该 pid 早已死亡并被系统分配给
    无关进程（PID 复用），误杀会干掉用户的其他程序（浏览器等）。
    校验失败（tasklist 异常）时返回 True 保持旧行为，不阻断正常接管。
    """
    if sys.platform != "win32":
        return True
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/NH", "/FO", "CSV"],
            capture_output=True, timeout=5,
        )
        text = (out.stdout or b"").decode("utf-8", "ignore")
        return "VideoDownloader" in text
    except Exception:
        return True

def _read_self_build_version() -> str:
    """读取当前构建指纹（build_mac.sh / build_win.sh 写入 build_version.txt）。

    用于启动时的版本自检——若已有实例的版本比当前构建旧，则自动接管，
    彻底避免「双击后仍在跑旧版」导致的反复调试浪费。

    🔴 2026-10-18 修复：Windows 冻结构建（PyInstaller）下 BASE = sys._MEIPASS
    （即 _internal/），而 build_version.txt 由安装器放在 exe 同级目录，
    _read_self_build_version() 永远返回 "" → 锁文件只有 2 字段 →
    版本比对恒不成立 → 「旧版在跑、新版双击」时新版只会激活旧窗口再退出
    （实测：新 exe 启动 4s 后 rc=0，用户装的新版被旧版堵死）。
    现按顺序探测：BASE → exe 同级目录（PyInstaller 安装布局）。
    """
    _candidates = [BASE / "build_version.txt"]
    if getattr(sys, "frozen", False):
        try:
            _candidates.append(Path(sys.executable).resolve().parent / "build_version.txt")
        except Exception:
            pass
    for _p in _candidates:
        try:
            if _p.exists():
                return _p.read_text("utf-8").strip()
        except Exception:
            pass
    return ""

def _kill_process_tree(pid: int, timeout: float = 3.0) -> bool:
    """向目标进程发 SIGTERM，等待其退出；超时则 SIGKILL。返回是否成功终止。"""
    import time as _t
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return not _pid_alive(pid)
    _deadline = _t.monotonic() + timeout
    while _t.monotonic() < _deadline:
        if not _pid_alive(pid):
            return True
        _t.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    return not _pid_alive(pid)

def _activate_existing_window() -> None:
    """重复启动时把已有 视频工坊 窗口提到最前（macOS / Windows）。

    提窗优先级（**绝不自动打开浏览器**，用户明确：App 就是 App，不跳网页）：
      Windows：EnumWindows 按 pid / 标题找窗口 → ShowWindow(SW_RESTORE)
          → AttachThreadInput + SetForegroundWindow（绕过前台限制）；
      macOS：AppKit `NSRunningApplication.activateWithOptions_` —— 官方"把另一实例带到
         前台"API，**无需任何「辅助功能」权限**；
      2. osascript System Events `set frontmost` —— 需要「辅助功能」权限，未授权会失败；
      3. 以上都失败只写日志，保持现状（提窗失败不跳浏览器）。
    修复背景：旧版无条件先开浏览器，导致已开着 app 再双击时每次都乱弹网页。
    """
    pid = None
    try:
        lp = Path.home() / ".vdl_instance.lock"
        if lp.exists():
            parts = _read_lock_text(lp).strip().split()
            if len(parts) >= 1 and parts[0].isdigit():
                pid = int(parts[0])
    except Exception:
        pass

    raised = False
    # 1) AppKit 无权限提窗（首选）
    if sys.platform == "darwin" and pid:
        try:
            from AppKit import NSRunningApplication, NSApplicationActivateAllWindows
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if app is not None:
                try:
                    # 组合 AllWindows|IgnoringOtherApps：把实例所有窗口带到前台
                    from AppKit import NSApplicationActivateIgnoringOtherApps
                    raised = bool(app.activateWithOptions_(
                        NSApplicationActivateAllWindows | NSApplicationActivateIgnoringOtherApps))
                except Exception:
                    try:
                        raised = bool(app.activateWithOptions_(NSApplicationActivateAllWindows))
                    except Exception:
                        raised = bool(app.activate())
        except Exception:
            raised = False

    # 2) Windows：EnumWindows 找已有实例窗口 → SW_RESTORE + SetForegroundWindow
    #    （SetForegroundWindow 有前台限制，标准解法：AttachThreadInput 贴到前台线程）
    if not raised and sys.platform == "win32":
        try:
            import ctypes as _ct
            from ctypes import wintypes as _w
            _u32 = _ct.windll.user32
            _k32 = _ct.windll.kernel32
            _WNDENUMPROC = _ct.WINFUNCTYPE(_w.BOOL, _w.HWND, _w.LPARAM)
            _u32.EnumWindows.argtypes = [_WNDENUMPROC, _w.LPARAM]
            _u32.GetWindowThreadProcessId.argtypes = [_w.HWND, _w.LPDWORD]
            _u32.IsWindowVisible.argtypes = [_w.HWND]
            _u32.GetWindowTextW.argtypes = [_w.HWND, _w.LPWSTR, _w.INT]
            _u32.ShowWindow.argtypes = [_w.HWND, _w.INT]
            _u32.SetForegroundWindow.argtypes = [_w.HWND]
            _u32.AttachThreadInput.argtypes = [_w.DWORD, _w.DWORD, _w.BOOL]
            found = []

            def _cb(hwnd, _l):
                if not _u32.IsWindowVisible(hwnd):
                    return True
                tpid = _w.DWORD()
                _u32.GetWindowThreadProcessId(hwnd, _ct.byref(tpid))
                if pid and tpid.value == pid:
                    found.append(hwnd)  # lock 文件记录的旧实例 pid，精确匹配
                    return True
                buf = _ct.create_unicode_buffer(256)
                if _u32.GetWindowTextW(hwnd, buf, 256) > 0:
                    t = buf.value
                    if "视频工坊" in t or "VideoDownloader" in t:
                        found.append(hwnd)  # pid 未知时按标题兜底
                return True

            _u32.EnumWindows(_WNDENUMPROC(_cb), 0)
            for _hw in found:
                _u32.ShowWindow(_hw, 9)  # SW_RESTORE：最小化态先恢复
            if found:
                _cur_tid = _k32.GetCurrentThreadId()
                _fg = _u32.GetForegroundWindow()
                _fg_tid = _u32.GetWindowThreadProcessId(_fg, None) if _fg else 0
                if _fg_tid and _fg_tid != _cur_tid:
                    _u32.AttachThreadInput(_fg_tid, _cur_tid, True)
                    _u32.SetForegroundWindow(found[0])
                    _u32.AttachThreadInput(_fg_tid, _cur_tid, False)
                else:
                    _u32.SetForegroundWindow(found[0])
                raised = True
                _launch_log(f"win32 提窗成功（hwnd={found[0]}，共 {len(found)} 个窗口）")
        except Exception as _e:
            _launch_log(f"win32 提窗异常：{_e!r}")
            raised = False

    # 3) osascript System Events 提窗（需要「辅助功能」权限，失败静默降级）
    if not raised and sys.platform == "darwin":
        try:
            r = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to set frontmost of '
                 '(every process whose name contains "视频工坊" or name contains "VideoDownloader") to true'],
                check=False, capture_output=True, timeout=3,
            )
            raised = (r.returncode == 0)
        except Exception:
            raised = False

    # 2.5) Windows 提窗（2026-10-26 单实例锁配套）：EnumWindows 模糊匹配标题，
    #      找到就 SW_RESTORE + SetForegroundWindow。标题可能带后缀，所以不能用
    #      FindWindowW 的精确匹配。
    if not raised and sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            hits = []

            _EnumProc = ctypes.WINFUNCTYPE(
                wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            def _on_window(hwnd, _lparam):
                if user32.IsWindowVisible(hwnd):
                    n = user32.GetWindowTextLengthW(hwnd)
                    if n > 0:
                        buf = ctypes.create_unicode_buffer(n + 1)
                        user32.GetWindowTextW(hwnd, buf, n + 1)
                        text = buf.value
                        if ("视频工坊" in text) or ("VideoDownloader" in text):
                            hits.append(hwnd)
                return True

            user32.EnumWindows(_EnumProc(_on_window), 0)
            for hwnd in hits:
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            if hits:
                user32.SetForegroundWindow(hits[0])
                raised = True
        except Exception:
            raised = False

    # 3) 提窗失败：只写日志，绝不自动打开浏览器（用户明确：App 就是 App，不跳网页）
    if not raised:
        _launch_log("提窗失败（AppKit/osascript 均未生效），不打开浏览器，保持现状")

_LAUNCH_LOG = Path.home() / ".vdl_launch.log"

# PATH 兜底：从 Dock/Finder 启动时 PATH 只有 /usr/bin:/bin:/usr/sbin:/sbin，
# homebrew (/opt/homebrew/bin) 下的 ffmpeg/ffprobe/yt-dlp 会「存在但找不到」，
# 进而让依赖外部工具的初始化在 GUI 启动与终端启动下行为不一致。
VDL_PATH_BOOTSTRAP = True
_extra_paths = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
for _p in reversed(_extra_paths):
    if _p not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = _p + os.pathsep + os.environ.get("PATH", "")

def _launch_log(msg: str) -> None:
    """把启动关键节点写入 ~/.vdl_launch.log，便于「双击打不开」时定位。"""
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(_LAUNCH_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def _read_lock_text(path) -> str:
    """读单实例锁文件文本，utf-8 优先、GBK(mbcs) 回退。

    🔴 2026-10-19：锁文件的写入方曾是 `open(path, "a+")`——**不指定编码就用
    进程 locale 默认编码**。实测同一台机器：系统 Python 默认 utf-8，而
    PyInstaller windowed 冻结进程默认 cp936（GBK）→ 冻结 exe 写出的
    「v1.0.76 · 构建 xxx」是 GBK 字节（\\xa1\\xa4 / \\xb9\\xb9\\xbd\\xa8），
    任何按 utf-8 读的外部组件/测试直接 UnicodeDecodeError，读成空锁。
    现在写入统一显式 utf-8；这里保留 GBK 回退，只为兼容存量 GBK 锁。
    """
    try:
        data = Path(path).read_bytes()
    except Exception:
        return ""
    for enc in ("utf-8", "mbcs"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


def _release_lock(f, path) -> None:
    try:
        if _fcntl is not None:
            _fcntl.flock(f, _fcntl.LOCK_UN)
        f.close()
    except Exception:
        pass
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass

def _show_error_dialog(msg: str) -> None:
    """macOS 下用系统弹窗显示错误（不依赖 Python 包）。失败静默忽略。"""
    if sys.platform != "darwin":
        return
    try:
        safe = msg.replace('"', "'").replace("\\", "")
        # 仅取前两行做弹窗，详情留给日志，避免 AppleScript 超长报错
        short = "\\n".join(safe.splitlines()[:2])
        subprocess.run(
            ["osascript", "-e",
             f'display dialog "{short}" with title "视频工坊" '
             f'buttons {{"确定"}} default button "确定"'],
            check=False, capture_output=True, timeout=3,
        )
    except Exception:
        pass

def _parse_openfile_result(raw: str) -> list[str]:
    """解析 GetOpenFileNameW 写回的缓冲内容。

    Windows 多选对话框不返回数组，而是把结果写进同一块缓冲，格式二选一：
      · 只选了一个：        "C:\\dir\\a.mp4\\0"
      · 选了多个：          "C:\\dir\\0a.mp4\\0b.mov\\0\\0"（目录 + 各文件名，双 NUL 结尾）
    漏了这层解析就会出现「明明选了文件，列表里还是没有」——表现和「点了没反应」一样。
    """
    parts = raw.split("\0")
    while parts and parts[-1] == "":
        parts.pop()
    if not parts:
        return []
    if len(parts) == 1:
        return [parts[0]]
    folder = parts[0]
    return [os.path.join(folder, name) for name in parts[1:]]


_OFN_STRUCT_CACHE = None


def _openfilenamew_struct():
    """返回缓存的 OPENFILENAMEW Structure 类（x64 = 152 字节）。

    「打开」与「另存为」用的是同一个 Win32 结构，只靠 Flags / lpstrDefExt 区分。
    抽成工厂是为了让两条路径共用一份字段定义 —— 字段少一个就会读写错位，
    表现为「对话框能弹但返回值乱码或崩溃」，这种错极难从现象反推。
    """
    global _OFN_STRUCT_CACHE
    if _OFN_STRUCT_CACHE is not None:
        return _OFN_STRUCT_CACHE

    import ctypes
    from ctypes import wintypes

    class _OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD),
            ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE),
            ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR),
            ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD),
            ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD),
            ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD),
            ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR),
            ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD),
            ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR),
            ("lCustData", wintypes.LPARAM),
            ("lpfnHook", wintypes.LPVOID),
            ("lpTemplateName", wintypes.LPCWSTR),
            ("pvReserved", wintypes.LPVOID),
            ("dwReserved", wintypes.DWORD),
            ("FlagsEx", wintypes.DWORD),
        ]

    _OFN_STRUCT_CACHE = _OPENFILENAMEW
    return _OPENFILENAMEW


def _windows_choose_folder(prompt: str = "请选择文件夹") -> str:
    """Windows 原生「选择文件夹」对话框（ctypes 直调 shell32.SHBrowseForFolderW）。

    🔴 2026-10-20 同类缺陷：`choose_folder` / `choose_extension_dir` 历史实现同样
       只有 osascript，Windows 上恒返回空串 —— 点「选择目录」完全不弹框、无提示。
       影响「剪映草稿导出目录」「浏览器扩展加载目录」两处。

    为什么不复用 tkinter.filedialog.askdirectory：launcher 有「非主线程不许开 Tk」
    的硬约束（macOS 上真触发过 NSWindow SIGTRAP 卡死整个 App），Windows 侧也不
    保证 bridge 线程能起 Tk mainloop —— 直接调 Win32 是唯一不赌的实现。

    返回：所选目录绝对路径（不带尾部分隔符）/ 用户取消 "" / 失败 "ERROR: ..."。
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception as exc:  # pragma: no cover
        return f"ERROR: {exc}"

    BIF_RETURNONLYFSDIRS = 0x0001
    BIF_EDITBOX = 0x0010
    BIF_NEWDIALOGSTYLE = 0x0040

    class _BROWSEINFOW(ctypes.Structure):
        _fields_ = [
            ("hwndOwner", wintypes.HWND),
            ("pidlRoot", wintypes.LPVOID),
            ("pszDisplayName", wintypes.LPWSTR),
            ("lpszTitle", wintypes.LPCWSTR),
            ("ulFlags", wintypes.UINT),
            ("lpfn", wintypes.LPVOID),
            ("lParam", wintypes.LPARAM),
            ("iImage", ctypes.c_int),
        ]

    try:
        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(_BROWSEINFOW)]
        shell32.SHBrowseForFolderW.restype = wintypes.LPVOID
        shell32.SHGetPathFromIDListW.argtypes = [wintypes.LPVOID, wintypes.LPWSTR]
        shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
        ole32.CoTaskMemFree.argtypes = [wintypes.LPVOID]
    except Exception as exc:
        return f"ERROR: {exc}"

    display = ctypes.create_unicode_buffer(260)
    bi = _BROWSEINFOW()
    try:
        bi.hwndOwner = ctypes.windll.user32.GetForegroundWindow()
    except Exception:
        bi.hwndOwner = None
    bi.pidlRoot = None
    bi.pszDisplayName = ctypes.cast(display, wintypes.LPWSTR)
    bi.lpszTitle = prompt or "请选择文件夹"
    # EDITBOX 让用户能直接粘贴路径 —— 深层目录用鼠标一层层点太慢
    bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_EDITBOX | BIF_NEWDIALOGSTYLE

    # shell 对话框内部走 COM，bridge 线程未必初始化过 → 显式 STA（已初始化返回 S_FALSE，无害）
    try:
        ole32.CoInitializeEx(None, 0x2)
    except Exception:
        pass

    try:
        pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
    except Exception as exc:
        return f"ERROR: {exc}"

    if not pidl:
        return ""                      # 用户点了取消 / 关闭
    try:
        path = ctypes.create_unicode_buffer(4096)
        if not shell32.SHGetPathFromIDListW(pidl, path):
            return ""
        return (path.value or "").rstrip("\\")
    finally:
        try:
            ole32.CoTaskMemFree(pidl)  # pidl 由 shell 分配，必须归还，否则每次选目录泄漏
        except Exception:
            pass


def _windows_choose_save_path(prompt: str, suggested: str, log_path: str = "") -> str:
    """Windows 原生「另存为」对话框（ctypes 直调 comdlg32.GetSaveFileNameW）。

    🔴 2026-10-20 同类缺陷：`_choose_save_path` 的两级实现（applet + osascript）都是
       macOS 专属，Windows 上恒返回 ""。8 个保存入口（去水印/文案/二维码/解说/
       抠像/转换/任务文件/直链）因此**从不弹面板**，静默落到「下载」文件夹 ——
       能保存但用户以为「没反应」（找不到文件放哪了）。

    返回约定与 macOS 侧完全一致：绝对路径 / "CANCELLED" / ""（机制不可用）。
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception as exc:  # pragma: no cover
        return f"ERROR: {exc}"

    def _log(msg: str) -> None:
        if not log_path:
            return
        try:
            import datetime as _dt
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"[{_dt.datetime.now().isoformat()}] {msg}\n")
        except Exception:
            pass

    OFN_OVERWRITEPROMPT = 0x00000002
    OFN_NOCHANGEDIR = 0x00000008
    OFN_PATHMUSTEXIST = 0x00000800
    OFN_EXPLORER = 0x00080000

    _OPENFILENAMEW = _openfilenamew_struct()

    # 建议名清洗：Win32 对话框收到含路径分隔符的 lpstrFile 会跑偏
    name = (suggested or "文件").replace("\\", "_").replace("/", "_").strip() or "文件"
    buf_size = 4096
    buf = ctypes.create_unicode_buffer(buf_size)
    buf.value = name

    # 过滤器留空 = 不过滤类型（用户已在前端选定输出格式，再筛一次只会挡住自己）
    ofn = _OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(_OPENFILENAMEW)
    try:
        ofn.hwndOwner = ctypes.windll.user32.GetForegroundWindow()
    except Exception:
        ofn.hwndOwner = None
    ofn.lpstrFilter = None
    ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
    ofn.nMaxFile = buf_size
    ofn.lpstrTitle = prompt or "保存文件"
    ofn.lpstrInitialDir = None  # 交给系统记住上次位置，比硬塞「下载」更符合习惯
    # 有扩展名就预填默认扩展：用户敲「片子」回车也能得到「片子.mp4」而不是无后缀文件
    ext = ""
    if "." in name:
        tail = name.rsplit(".", 1)[-1]
        if 2 <= len(tail) <= 5 and tail.isalnum():
            ext = tail
    ofn.lpstrDefExt = ext or None
    ofn.Flags = OFN_EXPLORER | OFN_PATHMUSTEXIST | OFN_NOCHANGEDIR | OFN_OVERWRITEPROMPT

    try:
        ctypes.windll.ole32.CoInitializeEx(None, 0x2)
    except Exception:
        pass

    try:
        ok = ctypes.windll.comdlg32.GetSaveFileNameW(ctypes.byref(ofn))
    except Exception as exc:
        _log(f"win32 save-dialog exception: {exc!r}")
        return f"ERROR: {exc}"

    if not ok:
        try:
            err = int(ctypes.windll.comdlg32.CommDlgExtendedError())
        except Exception:
            err = 0
        if err == 0:
            _log("win32 save-dialog cancelled")
            return "CANCELLED"
        _log(f"win32 save-dialog failed code=0x{err:04X}")
        return f"ERROR: 保存对话框失败 code=0x{err:04X}"

    chosen = (buf.value or "").strip()
    _log(f"win32 save-dialog -> {chosen}")
    return chosen


def _parse_netstat_listening_pids(raw: str, port: int) -> set[int]:
    """从 `netstat -ano` 输出里挑出「正在 LISTEN 指定端口」的 PID 集合。

    行格式（列宽随语言/版本变化，必须按空白切分而不是定宽截取）：
        TCP    127.0.0.1:7871      0.0.0.0:0      LISTENING     12345
        TCP    [::1]:7871          [::]:0         LISTENING     12345

    只认 LISTENING：ESTABLISHED / TIME_WAIT 里也有同一端口和 PID，
    但那些是**连接**不是**监听**——按它们杀会误伤客户端。
    """
    pids: set[int] = set()
    for line in (raw or "").splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        local, state, pid = parts[1], parts[3].upper(), parts[4]
        if state != "LISTENING":
            continue
        # IPv6 是 [::1]:7871 → 同样按「最后一个冒号之后」取端口
        if local.rsplit(":", 1)[-1] == str(port) and pid.isdigit() and pid != "0":
            pids.add(int(pid))
    return pids


def _windows_stop_port_listener(port: int, label: str = "") -> dict:
    """结束占用指定 TCP 端口的监听进程（Windows），返回 {"ok", "msg"}。

    🔴 2026-10-20：`stop_qwen3tts` 原实现是 `pkill -f start_qwen3tts_server.py`，
       Windows 上没有 pkill → FileNotFoundError → 返回「停止失败」，而服务仍在跑、
       继续占着内存（Qwen3-TTS 权重按 GB 算）。用户切回 edge-tts 后本地服务白跑。

    为什么**按端口**而不是按镜像名/命令行：
      · `python.exe` 在这台机器上有一堆（后端自己也叫 python），按名字杀必然误伤宿主进程；
      · 该服务就是监听固定端口，端口是唯一、精确且不会误杀的判据。

    为什么用 `netstat -ano` 而不是 wmic：
      wmic 在 Win11 24H2 起默认不预装（已弃用），调它会直接「找不到命令」；
      netstat 是 in-box 组件，所有版本都在。

    返回：
      {"ok": True,  "msg": "已停止…"}          —— 成功杀掉
      {"ok": True,  "msg": "当前未在运行…"}    —— 端口无人监听（本来就没起）
      {"ok": False, "msg": "…"}                —— 查询或结束失败
    """
    import subprocess

    name = f"{label} " if label else ""
    # CREATE_NO_WINDOW：桌面 App 内不要为每次查询闪一个黑色控制台窗口
    no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, timeout=15,
                             creationflags=no_win)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "msg": f"停止{name}服务失败：无法查询端口占用（{exc}）"}

    # ⚠️ 必须按字节读再宽松解码，不能用 text=True：
    #    中文 Windows 的 netstat 表头是 GBK（CP936），`text=True` 默认按 UTF-8 解 →
    #    subprocess 的 reader 线程抛 UnicodeDecodeError → stdout 直接变空 →
    #    永远判定「未在运行」，停止功能**静默失效**（实测踩到，2026-10-20）。
    #    我们只需要 ASCII 部分（TCP / LISTENING / 数字 PID），丢几个表头汉字无所谓。
    raw = (out.stdout or b"").decode("utf-8", "replace")
    pids = _parse_netstat_listening_pids(raw, port)

    if not pids:
        return {"ok": True, "msg": f"{name}本地服务当前未在运行，无需停止。"}

    killed: list[int] = []
    failed: list[int] = []
    for pid in sorted(pids):
        try:
            # 同上：中文 Windows 的 taskkill 回显是 GBK，text=True 会解码炸掉
            r = subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=15,
                               creationflags=no_win)
            (killed if r.returncode == 0 else failed).append(pid)
        except Exception:  # noqa: BLE001
            failed.append(pid)

    if failed and not killed:
        return {"ok": False,
                "msg": f"停止{name}服务失败（PID {', '.join(map(str, failed))}），"
                       f"可能权限不足，请在任务管理器手动结束。"}
    if failed:
        return {"ok": False,
                "msg": f"已停止部分{name}进程（PID {', '.join(map(str, killed))}），"
                       f"但 PID {', '.join(map(str, failed))} 未能停止，请手动结束。"}
    return {"ok": True,
            "msg": f"已停止{name}本地服务（PID {', '.join(map(str, killed))}），内存已释放。"}


def _windows_choose_files(exts: list[str], prompt: str, *, multiple: bool = True) -> list[str] | str:
    """Windows 原生文件选择框（ctypes 直调 comdlg32.GetOpenFileNameW，零依赖、不碰 tkinter）。

    🔴 2026-10-20 Windows 实测缺陷（用户报「点击添加文件没反应」）：
       `choose_files` / `choose_folder` / `pick_voice_sample` 历史实现**只有 macOS 的
       `osascript` 子进程**（desktop_launcher 里 6 处真调用全是 osascript）。
       Windows 上 `osascript` 根本不存在 → `subprocess.run` 抛 FileNotFoundError →
       被外层 `except Exception: pass` 吞掉 → `return ""` →
       前端 `desktop.chooseFiles()` 的 `norm("")` 得到 `[]` →
       调用点 `if (list && list.length)` 不成立 → **按钮点了完全没反应**：
       不报错、不提示、不弹框，用户只能反复点（转码/音乐/图片/压缩/合并/高清修复
       六个视图的「添加文件」全部同一条链路，全部失效）。

       修法：Windows 直接走 Win32 通用对话框，返回真实绝对路径数组 ——
       这样「跳过上传、直接读本地文件转码」的原生优势在 Windows 上才真正成立。

    参数：
      exts     —— 允许的扩展名（不带点），如 ["mp4", "mov"]；空表示不过滤
      prompt   —— 对话框标题
      multiple —— 是否允许多选

    返回：
      路径列表（多选）/ 只选一个时也可能是单元素列表；用户取消返回 ""；
      失败返回 "ERROR: ..."（与 osascript 分支的返回约定保持一致）。
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception as exc:  # pragma: no cover - 理论上不可能
        return f"ERROR: {exc}"

    OFN_READONLY = 0x00000001
    OFN_NOCHANGEDIR = 0x00000008
    OFN_ALLOWMULTISELECT = 0x00000200
    OFN_PATHMUSTEXIST = 0x00000800
    OFN_FILEMUSTEXIST = 0x00001000
    OFN_EXPLORER = 0x00080000

    # 「打开」与「另存为」共用同一份字段定义，见 _openfilenamew_struct 的说明
    _OPENFILENAMEW = _openfilenamew_struct()

    # 过滤器串格式：显示名 \0 模式 \0 ... \0 \0（结尾双 NUL，漏了会退化成不过滤）
    groups: list[str] = []
    patterns: list[str] = []
    for e in exts or []:
        e = str(e).strip().lstrip(".")
        if e:
            patterns.append(f"*.{e}")
    if patterns:
        groups.append(f"支持的文件 ({';'.join(patterns)})")
        groups.append(";".join(patterns))
    groups.append("所有文件 (*.*)")
    groups.append("*.*")
    filter_str = "\0".join(groups) + "\0\0"

    # 多选时 Windows 会把「目录 + 若干文件名」写进同一缓冲，10 个长路径也能塞爆 4KB
    buf_size = 65536 if multiple else 4096
    buf = ctypes.create_unicode_buffer(buf_size)

    ofn = _OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(_OPENFILENAMEW)
    try:
        ofn.hwndOwner = ctypes.windll.user32.GetForegroundWindow()
    except Exception:
        ofn.hwndOwner = None
    ofn.lpstrFilter = filter_str
    ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
    ofn.nMaxFile = buf_size
    ofn.lpstrTitle = prompt or "选择文件"
    ofn.Flags = OFN_EXPLORER | OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_NOCHANGEDIR | OFN_READONLY
    if multiple:
        ofn.Flags |= OFN_ALLOWMULTISELECT

    # 通用对话框内部走 COM，bridge 调用线程未必初始化过 → 显式 STA（已初始化返回 S_FALSE，无害）
    try:
        ctypes.windll.ole32.CoInitializeEx(None, 0x2)
    except Exception:
        pass

    try:
        ok = ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn))
    except Exception as exc:
        return f"ERROR: {exc}"

    if not ok:
        try:
            err = int(ctypes.windll.comdlg32.CommDlgExtendedError())
        except Exception:
            err = 0
        # err == 0 表示用户点了取消 / 关闭；非 0 才是真故障
        return "" if err == 0 else f"ERROR: 文件选择框失败 code=0x{err:04X}"

    # 缓冲内容 → 路径数组（单选用完整路径，多选是「目录 + 若干文件名」，见解析函数注释）
    paths = _parse_openfile_result(buf[:])
    # 与 osascript 分支保持同一约定：没选到东西一律返回空串（前端 norm 会转成 []）
    return paths if paths else ""


def _show_windows_message_box(msg: str, title: str = "视频工坊") -> bool:
    """Windows 原生 MessageBox 弹窗（ctypes 直调 user32，不依赖 tkinter / 任何 Python 包）。

    用于启动拦截等「必须让用户看到」的场景；失败静默返回 False（调用方自行兜底写日志）。
    """
    try:
        import ctypes
        MB_ICONINFORMATION = 0x00000040
        MB_SETFOREGROUND = 0x00010000
        MB_TOPMOST = 0x00040000
        ctypes.windll.user32.MessageBoxW(
            None, msg, title,
            MB_ICONINFORMATION | MB_SETFOREGROUND | MB_TOPMOST,
        )
        return True
    except Exception:
        return False


def _windows_version_major():
    """返回 Windows 主版本号；非 Windows / 取不到版本信息返回 None。

    对照：Win7=6.1 / Win8=6.2 / Win8.1=6.3 / Win10=10.0 / Win11=10.0（build≥22000，
    主版本号与 Win10 相同，一律放行）。抽成纯函数供启动检查与离线测试共用。
    """
    if sys.platform != "win32":
        return None
    getver = getattr(sys, "getwindowsversion", None)
    if getver is None:
        return None
    try:
        return int(getver().major)
    except Exception:
        return None


def _check_windows_version() -> None:
    """Win7 / Win8 / Win8.1 启动拦截（2026-10-17 上线）。

    背景：用户问「目前支持 win10/11，其他 windows 能不能也支持」——结论是不能，且不是
    改代码能绕过的，上游三处硬约束：① CPython 3.13 官方只支持 Windows 8.1+（Win7 要锁
    Python 3.8）；② Edge WebView2 Runtime 109 是最后一个支持 Win7/8/8.1 的版本
    （2023-01 起新 SDK 不再支持，且再无安全更新）；③ deno 2.x / onnxruntime 等原生依赖
    均要求 Win10+。与其让用户装完打不开再来问，不如启动即弹窗引导。

    行为：检测到主版本 <10 → 弹窗告知「请升级 Win10/11 或先用网页版」后退出。
    非 Windows 平台、取不到版本信息、或设置了 VDL_ALLOW_OLD_WINDOWS=1 均放行（不误伤）。
    """
    if str(os.environ.get("VDL_ALLOW_OLD_WINDOWS") or "").strip().lower() in ("1", "true", "yes", "on"):
        return
    major = _windows_version_major()
    if major is None or major >= 10:
        return
    _launch_log(f"不受支持的 Windows 版本（major={major}），阻止启动并引导用户")
    _show_windows_message_box(
        "视频工坊 不支持 Windows 7 / 8。\n"
        "\n"
        "这些系统过于老旧（微软已停止支持），程序无法稳定运行。\n"
        "\n"
        "您有两个选择：\n"
        "1. 升级到 Windows 10 或更高版本，重新安装使用；\n"
        "2. 暂不升级也可用：浏览器打开网页版 hanyuxz.top"
    )
    sys.exit(0)


def _browser_fallback(server_thread) -> None:
    """原生窗口不可用时的兜底：**不再自动打开浏览器**（用户明确：App 就是 App，不跳网页）。

    仅弹窗告知用户 + 写日志，并保持本地服务继续运行，等用户决定（可手动访问或退出）。
    """
    _launch_log("原生窗口不可用，不自动跳转浏览器（用户要求），仅提示并保持服务运行")
    _show_error_dialog(
        "视频工坊 无法打开原生窗口。\n"
        "请把 ~/.vdl_launch.log 内容发给开发者排查。"
    )
    try:
        server_thread.join()
    except Exception:
        pass

_WIN_MUTEX_NAME = "Local\\VideoDownloader_SingleInstance_Mutex"

class _WindowsMutexHandle:
    """占位句柄：Windows 单实例由内核互斥锁保证，无需文件锁。

    main() 把返回值当「成功拿到锁」的信号（None=失败→激活旧窗退出），
    所以这里必须返回一个非 None 对象。互斥锁生命周期随进程，崩溃/被杀
    由内核自动释放，不存在僵尸锁问题。
    """

def _ensure_single_instance_windows():
    """Windows 单实例：内核命名互斥锁（CreateMutexW），微信/Chrome 同款做法。

    修复 2026-10-26 用户真机「双击出 4 个窗口」：旧方案在 Windows 上
    ① 无 fcntl（文件锁互斥缺失）；② os.kill(pid,0) 判活实为杀进程；
    ③ 锁写在端口绑定前 + 判活依赖端口绑定 ⇒ 冷启动空窗期内连点全放行。
    互斥锁由内核保证原子性，从根上堵死以上三条，与端口/锁文件时序无关。
    """
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW(None, False, _WIN_MUTEX_NAME)
    ERROR_ALREADY_EXISTS = 183
    if k32.GetLastError() == ERROR_ALREADY_EXISTS:
        _launch_log("检测到已有实例（Windows 互斥锁命中），激活其窗口并退出（保持单实例）")
        _activate_existing_window()
        sys.exit(0)
    _launch_log("成为 singleton（Windows 互斥锁持有）")
    return _WindowsMutexHandle()

def _ensure_single_instance():
    # Windows：互斥锁主防线，一票定音，不进入下方文件锁/端口判定逻辑
    # （那套逻辑在 Windows 上有上述三条硬伤，留着只会帮倒忙）。
    if sys.platform == "win32":
        return _ensure_single_instance_windows()
    """同一用户只保留一个 GUI 实例，且确保运行的是最新构建版本。

    健壮化（修复「双击打不开 / 闪退」根因）：
    - 用「端口是否被绑定」判定实例是否真的存活，替代 os.kill(pid,0) 的 PID 复用误判；
    - 抢锁前不再用 open('w') 截断锁文件（旧逻辑会清空持有者已写的 PID，造成僵尸锁）；
    - 仅在确认自己是 singleton 后才 seek(0)+truncate 重写锁；进程退出时 atexit 清理。
    - 锁文件格式：`PID PORT BUILD_VERSION`
   

    锁文件格式：`PID PORT BUILD_VERSION`
    - 拿到锁（无别的实例）→ 写自己信息，返回锁 handle，正常启动
    - 拿不到锁（有别实例）→
        * 旧实例版本 == 当前版本 → 激活窗口并退出（保持单实例，避免重复窗口）
        * 旧实例版本 ≠ 当前版本（旧版在跑、新版双击）→ **自动终止旧实例**，
          自己成为唯一实例（彻底解决「双击后仍在跑旧版」导致反复调试浪费的问题）
    """
    lock_path = Path.home() / ".vdl_instance.lock"
    self_build = _read_self_build_version()

    # ── 🔴 2026-10-18 修复：Windows 单实例失效（连点图标开出 N 个窗口）──
    # 根因：下方锁文件依赖 `fcntl.flock`，而 fcntl 是 POSIX 专属，Windows 上
    # `_fcntl is None` → 锁文件只是"君子协议"。多进程同时启动时全部 open("a+")
    # seek+truncate+write 成功，每个都自认 singleton；再加上"后端端口绑定前"
    # 有几秒竞态窗口（端口未绑 → 僵尸锁清理不删 → 激活分支被跳过），
    # 用户快速双击 3 次就开 3 个窗口（实测 8 进程 10 窗口）。
    # 正解：Windows 命名互斥体（内核级，跨进程原子）。
    _win_mutex_ok = False  # 持有互斥体 = 已由内核保证无其他 VDL 实例
    if sys.platform == "win32":
        import ctypes as _ct
        from ctypes import wintypes as _w
        _k = _ct.windll.kernel32
        _k.CreateMutexW.restype = _w.HANDLE
        _k.CreateMutexW.argtypes = [_w.LPVOID, _w.BOOL, _w.LPWSTR]
        _k.WaitForSingleObject.restype = _w.DWORD
        _k.WaitForSingleObject.argtypes = [_w.HANDLE, _w.DWORD]
        _MUTEX_NAME = "Local\\VideoGongfang_SingleInstance_Mutex"
        _WAIT_OBJECT_0, _WAIT_ABANDONED = 0, 0x80
        _k.SetLastError(0)
        _mutex = _k.CreateMutexW(None, False, _MUTEX_NAME)
        # 对象是否已存在：修复前的旧构建只创建互斥体但不持有所有权
        # （bInitialOwner=False + 从不 Wait），对象因旧进程句柄而存在且空闲——
        # 混构场景（旧版在跑 + 新版启动）必须靠锁文件/端口分支兜底接管。
        _mutex_preexisted = bool(_mutex) and (_k.GetLastError() == 183)
        if _mutex:
            # 🔴 判据必须用 WaitForSingleObject(0ms)，不能用 CreateMutexW 的
            # ERROR_ALREADY_EXISTS(183)：183 只说明"互斥体对象存在"，不说明
            # "被持有"——本进程第一次 CreateMutexW 自己就开着一个非拥有句柄，
            # 对象会因此续命。2026-10-18 实测：接管流程里旧实例已被 taskkill
            # 杀掉，重抢循环却因 183 永远为真，5s 抢不回 → 新版激活退出打不开。
            # WaitForSingleObject 返回值才是权威：
            #   0    WAIT_OBJECT_0   → 获得所有权，本进程是唯一实例
            #   0x80 WAIT_ABANDONED  → 前主崩溃/被杀未 ReleaseMutex，wait 方照样获得所有权
            #   258  WAIT_TIMEOUT    → 确有活实例持有
            _wait = _k.WaitForSingleObject(_mutex, 0)
            if _wait in (_WAIT_OBJECT_0, _WAIT_ABANDONED):
                # 句柄不关（进程存活期持有），防 GC：挂到模块级全局。
                globals()["_VDL_SINGLE_INSTANCE_MUTEX"] = _mutex
                # 对象是本次新建 → 之前绝无实例创建过它，跳过端口分支；
                # 对象已存在却空闲 → 可能是旧构建在跑（只创建不持有所有权），
                # 不设 _win_mutex_ok，交给下方锁文件/端口分支兜底判定。
                if not _mutex_preexisted:
                    _win_mutex_ok = True
            else:
                # 互斥体被持有 = 已有实例在跑。**不能直接激活退出**（2026-10-18 回归
                # 修复）：旧代码在这里无条件 sys.exit(0)，导致「旧版在跑、新版双击」
                # 时新版永远抢不了班——下面基于锁文件的版本接管逻辑成了死代码
                # （实测：新 exe 启动 4s 后 rc=0 退出，旧构建继续占着窗口和端口，
                # 用户装的新版被旧版堵死）。正确流程：读锁文件判版本——
                #   * 旧版本且 pid 存活 → 杀旧实例（整棵树）→ 等退出 → 重抢互斥体
                #     → 成为 singleton 继续启动；
                #   * 同版本 / 版本未知 / pid 已死 → 激活窗口并退出（保持单实例）。
                try:
                    _raw = _read_lock_text(lock_path)
                except Exception:
                    _raw = ""
                _parts = _raw.strip().split()
                _opid = int(_parts[0]) if _parts and _parts[0].isdigit() else None
                # 版本 tag 含空格（"v1.0.76 · 构建 xxx @ MM-DD HH:MM"），必须 join 后半段，
                # 只取 _parts[2] 会退化成 "v1.0.76" ——同版本实例也比对不相等 → 误杀。
                _obuild = " ".join(_parts[2:]) if len(_parts) >= 3 else ""
                # 接管判定（2026-10-18 修正）：
                #  * 知道自己版本（self_build 非空）才有资格判新旧；
                #  * 对方版本为空 = 旧构建写的 2 字段锁（Windows 下 BASE 指向
                #    _internal 导致读不到 build_version.txt 的历史 bug，已修）→ 视为旧版；
                #  * 对方版本不同 = 旧版在跑 → 一律接管。
                if self_build and _opid and _pid_alive(_opid) and (not _obuild or _obuild != self_build):
                    _launch_log(f"检测到旧版本实例({_obuild or '版本未知(旧构建)'})持有互斥体，自动接管并关闭")
                    # taskkill /F /T：连子树一起杀（旧构建没有作业对象保护，
                    # 只杀主进程会留下 aria2c/WebView2 孤儿——正是本轮修的 bug）。
                    # _kill_process_tree 走的是 POSIX 信号语义，Windows 上不可靠。
                    # 杀前先校验 pid 确是本程序，防 PID 复用误杀无关进程。
                    if not _pid_is_vdl(_opid):
                        _launch_log(f"锁文件 pid={_opid} 已被无关进程复用（PID 复用），放弃接管")
                        _activate_existing_window()
                        sys.exit(0)
                    try:
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(_opid)],
                                       capture_output=True, timeout=10)
                    except Exception as _te:
                        _launch_log(f"taskkill 旧实例异常（继续尝试重抢互斥体）: {_te!r}")
                    # 重抢互斥体：复用已打开的同一个句柄 + WaitForSingleObject(0)。
                    # 旧实例退出后内核释放所有权；最多等 5s
                    # （竞态下可能被第三个实例抢走 → 激活退出）
                    _acquired = False
                    for _ in range(50):
                        if _k.WaitForSingleObject(_mutex, 0) in (_WAIT_OBJECT_0, _WAIT_ABANDONED):
                            _acquired = True
                            break
                        time.sleep(0.1)
                    if _acquired:
                        globals()["_VDL_SINGLE_INSTANCE_MUTEX"] = _mutex
                        _win_mutex_ok = True
                        _launch_log("已接管旧版本实例并重新获得互斥体，继续启动")
                        # 不退出：继续往下走（僵尸锁清理 + 写锁），成为唯一实例
                    else:
                        _launch_log("重抢互斥体失败（可能被其他实例抢走），激活其窗口并退出")
                        _activate_existing_window()
                        sys.exit(0)
                else:
                    _launch_log("检测到已在运行的同版本实例（Windows 互斥体），激活其窗口并退出")
                    _activate_existing_window()
                    sys.exit(0)

    def _read_lock():
        try:
            parts = _read_lock_text(lock_path).strip().split()
        except Exception:
            return None, None, ""
        pid = int(parts[0]) if parts and parts[0].isdigit() else None
        port = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else None
        build = " ".join(parts[2:]) if len(parts) >= 3 else ""
        return pid, port, build

    def _port_bound(port):
        if not port:
            return False
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                return s.connect_ex(("127.0.0.1", port)) == 0
        except OSError:
            return False

    # 清理僵尸锁：锁存在，但对应端口未绑定且 PID 已死 → 直接删除
    try:
        if lock_path.exists():
            opid, oport, _ = _read_lock()
            if not _port_bound(oport) and not (opid and _pid_alive(opid)):
                try:
                    lock_path.unlink()
                except OSError:
                    pass
    except Exception:
        pass

    opid, oport, obuild = _read_lock() if lock_path.exists() else (None, None, "")

    # 端口被占用 = 真的有别的实例在提供本地服务（PID 复用也不会误判）
    # 🔴 2026-10-19 修复：本分支【不再】用 _win_mutex_ok 把守。互斥体是
    # 2026-10-18 才引入的——从更早的【无互斥体版本】升级时，新版 CreateMutexW
    # 创建的是全新对象（preexisted=False → _win_mutex_ok=True），旧版实例
    # 不会被互斥体覆盖到；若这里跳过端口分支，旧版继续占着端口和窗口，
    # 新版 uvicorn 只能另选端口起第二个实例——正是「新版装完打不开」。
    # 锁文件 + 端口 + pid + 版本才是跨版本权威判据；互斥体只对同代构建有效。
    # 走到这里的其他路径（持锁无事发生 / 互斥体已存在却空闲）同样安全：
    # 锁 pid 是活 VDL 且版本不同 → 接管；同版本 → 激活退出；否则继续启动。
    if _port_bound(oport):
        _lock_pid_is_vdl = bool(opid) and _pid_alive(opid) and _pid_is_vdl(opid)
        if self_build and _lock_pid_is_vdl and (not obuild or obuild != self_build):
            _launch_log(f"检测到旧版本实例({obuild or '版本未知(旧构建)'})在端口 {oport} 运行，自动接管并关闭")
            if sys.platform == "win32":
                # taskkill /F /T：连子树一起杀（Windows 上 POSIX 信号语义不可靠）
                try:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(opid)],
                                   capture_output=True, timeout=10)
                except Exception as _te:
                    _launch_log(f"taskkill 旧实例异常（继续等端口释放）: {_te!r}")
            else:
                _kill_process_tree(opid)
            for _ in range(50):  # 最多等 5s 让端口释放
                if not _port_bound(oport):
                    break
                time.sleep(0.1)
        elif _lock_pid_is_vdl:
            _launch_log("检测到已在运行的同版本实例，激活其窗口并退出（保持单实例）")
            _activate_existing_window()
            sys.exit(0)
        # else：端口被无关进程占用，或锁文件已僵尸 → 无视，继续启动（自有端口另选）

    # ── 自己是唯一实例：拿文件锁（不提前截断，避免清空持有者已写内容）──
    try:
        f = open(lock_path, "a+", encoding="utf-8")  # 🔴 显式 utf-8：不指定就用进程
        # locale 默认编码——冻结进程是 cp936、开发态是 utf-8，读写会错位
        if _fcntl is not None:
            _fcntl.flock(f, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        f.seek(0)
        f.truncate()
        f.write(f"{os.getpid()} {PORT} {self_build}\n")
        f.flush()
    except Exception:
        try:
            f.close()
        except Exception:
            pass
        f = None
    if f is not None:
        try:
            import atexit
            atexit.register(_release_lock, f, lock_path)
        except Exception:
            pass
        _launch_log(f"成为 singleton，已写锁 (pid={os.getpid()} port={PORT} build={self_build})")
    return f

def main() -> None:
    # 进程树保卫（必须最先）：主进程入 Windows"杀子作业"，之后 spawn 的
    # aria2c/ffmpeg/deno/WebView2 渲染进程全部入作业；主进程一旦退出
    # （含 crash / taskkill /F / 被安装器关停）内核级联终结整棵树，杜绝孤儿。
    _setup_win_job_object()

    # Win7 / Win8 / Win8.1 启动拦截：上游（Python 3.13 / WebView2 / 原生依赖）已整体不
    # 支持这些系统，安装包无法稳定运行。放在最前面 fail-fast——装完打不开再来问，
    # 不如启动即弹窗引导（详见 _check_windows_version 注释）。
    _check_windows_version()

    # 无控制台环境（Windows 双击启动）补 sys.std*，防 uvicorn 等库踩 None 崩溃
    _ensure_stdio()

    import uvicorn

    signal.signal(signal.SIGTERM, _handle_exit)
    signal.signal(signal.SIGINT, _handle_exit)

    # 主进程也注入 COMMENTARY_WORK_ROOT，让 _commentary_root 始终走用户可写目录，
    # 不污染包内的 Contents/Resources/commentary 与 Contents/Frameworks/commentary
    os.environ.setdefault("COMMENTARY_WORK_ROOT", str(_app_data_dir() / "commentary"))

    # 单实例：已有窗口则激活并退出，绝不创建第二个页面
    _launch_log(f"main 启动 (pid={os.getpid()} port={PORT} frozen={getattr(sys, 'frozen', False)})")
    _lock = _ensure_single_instance()
    if _lock is None:
        _activate_existing_window()
        sys.exit(0)

    # 后台启动 FastAPI 服务（带 bind 失败自动顺延端口的兜底重试）
    _app_dir = str(BASE) if getattr(sys, "frozen", False) else str(SERVER_DIR)
    # 后端线程异常必须留痕：从 Dock/Finder（open -a）启动时 stdout/stderr 不进任何
    # 日志文件，线程一旦抛非 OSError 异常就**静默死亡**——表现为「窗口开了但后端
    # 永远不监听」，且 ~/.vdl_launch.log 只留一句「服务器启动超时」，无从下手。
    # 这里统一兜住并写入启动日志。
    server_thread = threading.Thread(
        target=_logged_run_server,
        args=(PORT, HOST, _app_dir, _lock),
        daemon=True,
    )
    # 🔍 诊断（2026-09-22）：后端线程曾卡死在 import 阶段的 open()（系统日志无从下手），
    # 让 faulthandler 每 30s 把全部线程的 Python 栈打到启动日志，卡在哪一行一目了然。
    try:
        import faulthandler as _fh
        _dump_path = Path.home() / ".vdl_pydump.log"
        _dump_fh = open(_dump_path, "w")
        _fh.dump_traceback_later(30, repeat=True, file=_dump_fh)
    except Exception:
        _dump_fh = None
    server_thread.start()

    # 等服务器就绪（窗口 240s：见下方 2026-09-15 实测）。daemon 线程不 join，
    # 即便超时 main 返回，server 线程仍随 main 进程存活直到用户主动退出/系统清理。
    #
    # 2026-09-15 实测（换包后首次启动）：热启动 2s，但**冷启动 97s** —— 661MB
    # 包刚换进 /Applications，磁盘缓存全冷 + ad-hoc 签名校验，后端 97s 才监听。
    # 旧窗口 60s 一到就放行 → 窗口比后端先开，用户看到加载失败页（且不会自动重试），
    # 而事实上后端再等 37s 就好了。放宽到 240s：就绪立即 break，热启动体感不变；
    # 冷启动多等一会儿但**打开就能用**。真慢到 240s 还没起，行为与旧版一致（不更差）。
    _READY_WAIT_SEC = 240
    _ready = False
    for _ in range(int(_READY_WAIT_SEC / 0.2)):
        if not server_thread.is_alive():
            # 后端线程已退出（异常栈已由 _logged_run_server 写进启动日志）——立即失败，
            # 不再傻等满 240s 才让用户看到错误窗口（2026-10-17 实测：Windows 用户
            # 装机后打不开，对着「127.0.0.1 拒绝连接」干等 4 分钟，体验极差）。
            _launch_log("后端线程已退出且服务从未就绪，提前结束等待")
            break
        try:
            with socket.create_connection((HOST, PORT), timeout=0.5):
                _ready = True
                break
        except OSError:
            time.sleep(0.2)
    if not _ready:
        _launch_log(f"服务器未就绪（{_READY_WAIT_SEC}s 内未监听 {HOST}:{PORT}），弹窗告知用户")
        # 注意：不能 server_thread.join()——会让 main 阻塞直到 thread 退出，
        # 而 thread 可能因 cookie_pool 等慢 init 永远不结束，整个 app 卡死。
        # daemon=True 表明 main 退出时 thread 一起死，但用户不主动退就没人杀 main。
        # 这里只警告，不阻塞 main。
        # 🔴 2026-10-17：GUI（双击图标）启动时 stdout 不可见，只 print 等于静默失败——
        # 用户唯一看到的就是 WebView2 的「拒绝连接」错误页，不知道去哪找日志。
        # 必须弹窗指明日志路径（Windows: MessageBoxW；macOS: osascript 各自生效）。
        _show_windows_message_box(
            "视频工坊 的本地服务启动失败。\n"
            "\n"
            "请把日志文件发给我们排查：\n"
            "%USERPROFILE%\\.vdl_launch.log\n"
            "（按 Win+R，粘贴该路径回车即可打开）"
        )
        _show_error_dialog(
            "视频工坊 的本地服务启动失败。\n"
            "请把 ~/.vdl_launch.log 内容发给开发者排查。"
        )
        print(f"服务器启动较慢（>{_READY_WAIT_SEC}s），请稍候或手动访问 {URL}")

    _launch_log("后端服务就绪，准备打开界面")

    # ── ★ 2026-08-28 续33 关键护城河：拦截任何 lazy tkinter 初始化 ──
    # 根因：pywebview 的 JS bridge 在 macOS cocoa 后端 **未必在 main thread** 同步调 →
    # 任何依赖 Tk 创建窗口的代码（tkinter / PIL.ImageTk）在 non-main-thread 调 Tk() →
    # `TkMacOSXMakeRealWindowExist` → `NSWindow drag regions should only be invalidated
    # on the Main Thread!` SIGTRAP trap → macOS 弹 force quit「应用没响应」+ 主循环饿死。
    # 修复：把 tkinter / _tkinter 在 sys.modules 里替换为 stub，让任何 lazy 调用立即报
    # RuntimeError，绝不触发 Tk lib init。本 app 自有桌面 dialog（osascript/NSOpenPanel），
    # 不需要 Tk 跨平台回退。
    try:
        import sys as _sys
        class _TkGuard:
            """所有 tkinter API 调到这里抛 RuntimeError，绝不触发 Tk lib init。"""
            __slots__ = ()
            def __getattr__(self, name):
                raise RuntimeError(
                    f"[vdl-guard] tkinter.{name} 已被禁用（macOS main-thread-only trap 修复）。"
                    " 用 app 内置的 osascript / NSOpenPanel 桌面 dialog 替代。"
                )
        if _sys.modules.get('tkinter') is None:
            _sys.modules['tkinter'] = _TkGuard()
        if _sys.modules.get('_tkinter') is None:
            _sys.modules['_tkinter'] = _TkGuard()
        # 让 PIL 探测到 tkinter 不可用（features['tkinter'] = False），禁用 ImageTk 后端
        try:
            from PIL import features as _pf
            _pf.pilinfo  # noqa: B018
        except Exception:
            pass
        _launch_log("已激活 tkinter 主线程护栏（续33 修复）")
    except Exception as _mpe:
        _launch_log("tkinter 护栏注册失败（非致命）: " + repr(_mpe))

    # 开窗口 → 优先原生窗口（pywebview），回退浏览器
    webview = _webview
    if webview is None:
        _launch_log("未捆绑 webview 模块，无法打开原生窗口（不跳浏览器，仅提示）")
        _browser_fallback(server_thread)
        return

    try:
        # ── macOS 退出语义修复：区分「红叉(Cmd+W)=返回桌面」与「Dock Quit(Cmd+Q)=彻底退出」──
        # pywebview 6.x 把两者都导向同一个 closing 事件；其 cocoa 后端中：
        #   * 红叉 / Cmd+W      → WindowDelegate.windowShouldClose_ → closing
        #   * Dock Quit / Cmd+Q → AppDelegate.applicationShouldTerminate_ → 同样经 closing
        # 由于我们让 closing 返回 False 实现「红叉=最小化」，会连带把 Dock Quit 也取消，
        # 导致右键 Quit 退不出来。修复：拦截 applicationShouldTerminate_（仅 Dock Quit 走这条），
        # 置 _app_terminating 标志并放行；红叉仍走 windowShouldClose → closing 返回 False 最小化。
        if sys.platform == "darwin":
            try:
                import Foundation as _Foundation
                import webview.platforms.cocoa as _cocoa
                _AD = _cocoa.BrowserView.AppDelegate

                def _patch_app_should_terminate(self, app):
                    global _app_terminating
                    _app_terminating = True  # 来自 Dock Quit / Cmd+Q
                    return _Foundation.YES    # 始终允许退出

                _AD.applicationShouldTerminate_ = _patch_app_should_terminate
            except Exception as _e:
                print(
                    f"[VDL] 无法 patch applicationShouldTerminate（Dock Quit 可能退不出）: {_e}",
                    file=sys.stderr,
                )

        # ── 放行 WebView 麦克风（2026-09-19）：「我的音色 → ⏺ 直接录制」靠 WKWebView 的
        #    getUserMedia。macOS 12+ 采集前必须由 WKUIDelegate 的
        #    webView:requestMediaCapturePermissionForOrigin:initiatedByFrame:type:decisionHandler:
        #    给出决定；pywebview 没实现它，而 WKWebView 自己没有权限弹窗 →
        #    请求会一直挂着不落定（页面侧表现＝点录制没反应）。这里补上并直接 grant，
        #    系统 TCC 仍会照常弹一次「视频工坊想访问麦克风」，用户拒绝则前端给出中文指引。
        #    ⚠️ 签名必须显式写全（v@: + 每个实参一个编码，type 是 NSUInteger→Q，block→@?）：
        #       classAddMethods 校验的实参个数 = 签名里 self/_cmd 之后的个数，少写一个即
        #       BadPrototypeError，多写一个则 block 参数传不进来（拿不到 handler 必崩）。
        if sys.platform == "darwin":
            try:
                import objc as _objc
                import webview.platforms.cocoa as _cocoa_mic
                _mic_sel = (
                    b"webView:requestMediaCapturePermissionForOrigin:"
                    b"initiatedByFrame:type:decisionHandler:"
                )

                def _vdl_allow_media_capture(self, _wv, _origin, _frame, _mtype, handler):
                    """放行麦克风/摄像头采集（仅本机页面；TCC 会再问用户一次）。"""
                    try:
                        handler(1)   # WKPermissionDecisionGrant
                    except Exception:
                        pass

                _objc.classAddMethods(
                    _cocoa_mic.BrowserView.BrowserDelegate,
                    [_objc.selector(_vdl_allow_media_capture, selector=_mic_sel,
                                    signature=b"v@:@@@Q@?")],
                )
                _launch_log("已放行 WebView 媒体采集权限（我的音色·直接录制）")
            except Exception as _mic_err:
                _launch_log(f"无法放行 WebView 麦克风（「直接录制」将不可用）: {_mic_err!r}")

        api = VdlApi()

        # ---- 窗口尺寸/位置记忆（2026-09-17：用户要求按上次手动调整后的宽高打开） ----
        # 存 _app_data_dir()/window_frame.json；无文件 → 按屏幕可用区 94% 计算。
        _win_frame_file = _app_data_dir() / "window_frame.json"

        def _logical_workarea():
            """屏幕**可用区**的逻辑像素（已除 DPI 缩放）：(宽, 高, 原点x, 原点y)。

            🔴 2026-10-28（用户反馈「页面变这么小了、界面都变形了」）：
               此前默认写死 1280x800 逻辑。但 125% DPI 的 1920x1080 屏逻辑只有
               1536x864，1280x800 仅比 web 的 901px 断点宽一点点 → 界面被压进紧凑
               布局（侧栏变窄、卡片两列、字号显小），用户体感就是「变形」。
               改成按屏幕算，任何 DPI 下都占屏 ~94%，不再靠猜固定数字。
            🔴 同时改用 SPI_GETWORKAREA 而不是整屏：可用区已扣掉任务栏，否则
               92% 高 + 居中会压到任务栏下面。
            """
            try:
                if sys.platform == "win32":
                    import ctypes as _ct

                    class _R(_ct.Structure):
                        _fields_ = [("left", _ct.c_long), ("top", _ct.c_long),
                                    ("right", _ct.c_long), ("bottom", _ct.c_long)]

                    _u32 = _ct.WinDLL("user32")
                    _g32 = _ct.WinDLL("gdi32")
                    r = _R()
                    ok = _u32.SystemParametersInfoW(0x0030, 0, _ct.byref(r), 0)  # SPI_GETWORKAREA
                    dc = _u32.GetDC(0)
                    dpi = _g32.GetDeviceCaps(dc, 88) or 96
                    _u32.ReleaseDC(0, dc)
                    k = 96.0 / dpi
                    if ok and r.right > r.left and r.bottom > r.top:
                        return (int((r.right - r.left) * k), int((r.bottom - r.top) * k),
                                int(r.left * k), int(r.top * k))
                    pw = _u32.GetSystemMetrics(0) or 1920
                    ph = _u32.GetSystemMetrics(1) or 1080
                    return int(pw * k), int(ph * k), 0, 0
            except Exception:
                pass
            return 1280, 800, 0, 0

        _scr_w, _scr_h, _org_x, _org_y = _logical_workarea()
        # 默认尺寸：占屏 94% 宽 / 92% 高（可用区已扣任务栏），带绝对下限
        _def_w = max(1120, min(int(_scr_w * 0.94), 2560))
        _def_h = max(740, min(int(_scr_h * 0.92), 1600))
        # 🔴 2026-10-28：默认位置改为**居中**明确算出来，不再交给 pywebview 猜。
        #    实测传 x=None,y=None 时窗口落在 (224,224)，1803x992 物理的窗口右下角
        #    直接出屏（1920x1080 上右溢 107px、下溢 136px），用户看到界面被切边。
        _def_x = _org_x + max(0, (_scr_w - _def_w) // 2)
        _def_y = _org_y + max(0, (_scr_h - _def_h) // 2)
        _launch_log(
            f"屏幕可用区 {_scr_w}x{_scr_h} @{_org_x},{_org_y} → 默认窗口 {_def_w}x{_def_h} @{_def_x},{_def_y}")

        def _load_win_frame():
            import json as _json
            try:
                d = _json.loads(_win_frame_file.read_text(encoding="utf-8"))
                # 🔴 2026-10-28：记忆值「过小」视为无效历史脏数据，直接回默认尺寸。
                #    实测本机存过 {"width":960,"height":640}（正是旧 min_size），于是每次
                #    启动都被钉在最小窗口，用户侧表现为「界面变小、排版变形」。
                _raw_w = int(d.get("width", _def_w) or 0)
                _raw_h = int(d.get("height", _def_h) or 0)
                if _raw_w <= 0 or _raw_h <= 0 or _raw_w < 1120 or _raw_h < 740:
                    _launch_log(
                        f"记忆窗口尺寸过小({_raw_w}x{_raw_h})，已忽略 → 用默认 {_def_w}x{_def_h}")
                    return _def_w, _def_h, _def_x, _def_y
                # 🔴 2026-10-19：最小宽高保证宽于 web 的 901px 断点，否则侧边栏被隐藏、
                #    顶部 pills 换行 → 排版错乱（与 Mac 版对齐）。
                # 🔴 2026-10-28：下限从 960x640 抬到 1120x740 —— 960 逻辑在 125% DPI 下
                #    等于物理 1200x800，正是用户截图里「小得变形」的那个尺寸。
                w = max(1120, min(3840, _raw_w))
                h = max(740, min(2160, _raw_h))
                # 记忆值可能来自换屏前（旧屏更大）：超出当前屏幕就按当前屏幕上限收回来
                w = min(w, max(1120, int(_scr_w * 0.98)))
                h = min(h, max(740, int(_scr_h * 0.96)))
                x = d.get("x"); y = d.get("y")
                x = int(x) if isinstance(x, (int, float)) else None
                y = int(y) if isinstance(y, (int, float)) else None
                # 🔴 2026-10-18 修复：最小化/屏幕外的坐标曾被 _save_win_frame 打包存下
                #    （实测 {"width":159,"height":27,"x":-25600,"y":-25600}），
                #    导致窗口每次启动都落在虚拟桌面外，用户完全看不到内容
                #    （任务栏缩略图却正常 → "小图能看到、点开白屏"）。
                #    坐标必须落在虚拟桌面内，否则按首次运行居中处理。
                if x is not None and y is not None and sys.platform == "win32":
                    try:
                        import ctypes as _ct
                        _u32 = _ct.WinDLL("user32")
                        vx = _u32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
                        vy = _u32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
                        vw = _u32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
                        vh = _u32.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN
                        if not (vx <= x <= vx + vw - 200 and vy <= y <= vy + vh - 200):
                            x, y = None, None
                    except Exception:
                        x, y = None, None
                # 🔴 2026-10-28：位置一律**收回可用区内**（记忆值可能是换屏前存的，
                #    也可能本来就是 pywebview 猜出来的出屏坐标）。宽高已按当前屏幕
                #    收过一次，这里只保证「整窗可见」。
                if x is None or y is None:
                    x, y = _def_x, _def_y
                x = min(max(x, _org_x), max(_org_x, _org_x + _scr_w - w))
                y = min(max(y, _org_y), max(_org_y, _org_y + _scr_h - h))
                return w, h, x, y
            except Exception:
                return _def_w, _def_h, _def_x, _def_y

        _win_w, _win_h, _win_x, _win_y = _load_win_frame()
        _launch_log(f"窗口请求尺寸 {_win_w}x{_win_h} @ {_win_x},{_win_y}")
        window = webview.create_window(
            title="视频工坊",
            url=URL,
            width=_win_w,
            height=_win_h,
            x=_win_x,
            y=_win_y,
            min_size=(1120, 740),
            text_select=True,
            js_api=api,
        )

        # 🔴 2026-10-28 兜底：实测有机器上 pywebview 会把窗口直接顶到 min_size
        #    （请求 1880x1010 却得到 1200x800 = 960x640 逻辑，界面被压成紧凑布局）。
        #    这里在窗口就绪后复测一次，明显小于请求值就强制 resize 回目标尺寸。
        def _ensure_window_on_screen(tag=""):
            """把窗口拉回可用区内（尺寸也一起收）。实测默认落点 (224,224) 会让
            1803x992 的窗口右下角出屏。全部按本进程坐标系（DPI 未感知即虚拟化坐标）
            操作，与 _logical_workarea 同一套口径。"""
            try:
                import ctypes as _ct

                class _R(_ct.Structure):
                    _fields_ = [("left", _ct.c_long), ("top", _ct.c_long),
                                ("right", _ct.c_long), ("bottom", _ct.c_long)]

                _u32 = _ct.WinDLL("user32")
                hwnd = _u32.FindWindowW(None, "视频工坊")
                if not hwnd:
                    return
                r = _R()
                _u32.GetWindowRect(hwnd, _ct.byref(r))
                w, h = r.right - r.left, r.bottom - r.top
                wa = _R()
                _u32.SystemParametersInfoW(0x0030, 0, _ct.byref(wa), 0)
                nx = min(max(r.left, wa.left), max(wa.left, wa.right - w))
                ny = min(max(r.top, wa.top), max(wa.top, wa.bottom - h))
                if (nx, ny) != (r.left, r.top):
                    # SWP_NOSIZE=0x1 | SWP_NOZORDER=0x4
                    _u32.SetWindowPos(hwnd, 0, nx, ny, 0, 0, 0x0001 | 0x0004)
                    _launch_log(
                        f"窗口位置出屏({r.left},{r.top})，已拉回 ({nx},{ny})，"
                        f"尺寸 {w}x{h}，可用区 {wa.left},{wa.top}-{wa.right},{wa.bottom}{tag}")
                else:
                    _launch_log(
                        f"窗口位置正常 ({r.left},{r.top})，尺寸 {w}x{h}，"
                        f"可用区 {wa.left},{wa.top}-{wa.right},{wa.bottom}{tag}")
            except Exception as _e:  # noqa: BLE001
                _launch_log(f"窗口位置校验失败：{_e!r}")

        def _enforce_window_size():
            for _ in range(20):
                time.sleep(0.4)
                try:
                    cw, ch = int(window.width), int(window.height)
                except Exception:
                    continue
                if cw <= 0 or ch <= 0:
                    continue
                if cw >= _win_w * 0.94 and ch >= _win_h * 0.94:
                    _launch_log(f"窗口尺寸校验通过：{cw}x{ch}")
                    _ensure_window_on_screen()
                    return
                try:
                    window.resize(_win_w, _win_h)
                    _launch_log(f"窗口尺寸被压到 {cw}x{ch}，已强制 resize → {_win_w}x{_win_h}")
                    time.sleep(0.5)
                    _ensure_window_on_screen("（resize 后）")
                    return
                except Exception as _e:  # noqa: BLE001
                    _launch_log(f"强制 resize 失败：{_e!r}")

        threading.Thread(target=_enforce_window_size, daemon=True,
                         name="vdl-window-size").start()

        def _save_win_frame(*_args):
            """resized/moved 时持久化窗口框架（失败静默，不影响主流程）。"""
            import json as _json
            try:
                _w = int(window.width); _h = int(window.height)
                _x = int(window.x); _y = int(window.y)
                # 🔴 2026-10-18 修复：最小化时 pywebview/WinForms 返回怪异值
                #    （实测 159x27 @ (-25600,-25600)），旧代码照单全收 → 写坏
                #    frame 文件 → 下次启动窗口永久屏幕外。低于 min_size 或负坐标
                #    一律视为"非正常展示态"，跳过保存。
                # 🔴 2026-10-28：下限 960x640 → 1120x740。
                #    真凶复盘：旧下限恰好等于 min_size，于是「窗口被顶到最小」那一次的
                #    尺寸被当成正常值存了下来（Roaming\window_frame.json = 960x640），
                #    之后每次启动都读它 → 125% DPI 下物理仅 1200x800，web 断点 901 刚过，
                #    界面被压成紧凑布局。用户看到的就是「页面变这么小、都变形了」。
                if _w < 1120 or _h < 740 or _x < 0 or _y < 0:
                    return
                _win_frame_file.parent.mkdir(parents=True, exist_ok=True)
                _json.dump(
                    {
                        "width": _w,
                        "height": _h,
                        "x": _x,
                        "y": _y,
                    },
                    open(_win_frame_file, "w", encoding="utf-8"),
                )
            except Exception:
                pass

        window.events.resized += _save_win_frame
        try:
            window.events.moved += _save_win_frame  # 老版本 pywebview 无 moved 时忽略
        except AttributeError:
            pass

        # 把 window 引用交给桥接 API，供「返回桌面」/「退出」按钮调用
        api.window = window

        # 🔴 2026-10-19 修复「点击卡死 / 未响应 / 所有 API 超时」根因：
        #    pywebview 在窗口 before_load 时会递归枚举 js_api（本对象 VdlApi）的全部属性，
        #    生成 JS 桥的 function 列表。VdlApi.window 是 pywebview.Window，其 .native 是
        #    WinForms WebView2 控件——对象图里存在 Rect.Bounds.Empty → Rect.Empty → … 的
        #    **无限递归**，外加上千个 COM 属性枚举。一次 get_functions 会长时间占用 GIL
        #    ⇒ uvicorn 事件循环被饿死（/api/version 等全部超时）、主线程消息循环卡死
        #    （标题栏显示“未响应”）。
        #    设 _serializable=False 让 pywebview 直接跳过该属性（Python 侧
        #    self.window.minimize() 等方法仍可用），彻底切断递归链。前端只调用 api.<方法>()，
        #    从不访问 api.window，故对 JS 桥无影响。
        try:
            api.window._serializable = False
        except Exception:
            pass

        # 窗口行为说明（macOS 原生窗口）：
        #   - 点窗口红叉 / Cmd+W  → closing 拦截 → 最小化到 Dock（返回桌面，软件常驻）
        #   - 前端「返回桌面」按钮 → api.hide_to_desktop() → window.minimize()（最小化常驻）
        #   - 前端「退出」按钮    → api.quit_app() → os._exit(0) 强制退出
        #   - 顶部菜单 Cmd+Q / Dock 右键 Quit → 彻底退出（经 AppDelegate.applicationShouldTerminate_）
        # ── 窗口关闭 vs 退出软件（macOS 原生窗口）──
        # 全局标志：quit_app() 设 _quitting；Dock Quit/Cmd+Q 经 applicationShouldTerminate_ 设 _app_terminating；
        # 二者任一为真时 closing 拦截器放行真正退出，否则红叉最小化（返回桌面）。
        _quitting = False

        def _on_closing(*_args):
            """拦截窗口关闭事件：
            - 点红叉 / Cmd+W → 最小化到 Dock（返回桌面，软件常驻）
            - Dock Quit / Cmd+Q（_app_terminating）或 显式「退出」按钮（_quitting）→ 放行退出
            """
            if _quitting or _app_terminating:
                return True  # 正在退出，放行
            try:
                window.minimize()
            except Exception:
                pass
            return False  # 取消关闭，用最小化代替（返回桌面）

        window.events.closing += _on_closing

        # ── 窗口自愈：主框架被导航到应用外就导回（2026-10-12 用户报「窗口里是一整页乱码 JSON」）──
        # 成因是一类**已复发两次**的老问题：WKWebView 会把主框架导航到非 UI 资源。
        #   2026-09-21「点保存二维码」导航到 blob: 图片（那次只修了那一个路径）；
        #   2026-10-12 窗口停在 `{"detail": "…乱码…"}` —— 某条链接回退走了
        #   `window.open(url)`，API 地址的 JSON 错误被当文档渲染，且 JSON 无 charset
        #   被按 GBK 解码 → 满屏乱码，用户既看不懂也回不去，只能重启 App。
        # 这里做**兜底**：不管哪条路径把窗口带走，每次加载完成都核对一次地址，
        # 不是应用自己的源就导回应用根 —— 覆盖全部路径，而不是逐个补 fallback。
        _last_recover = 0.0

        def _on_loaded(*_args):
            nonlocal _last_recover
            try:
                try:
                    cur = window.get_current_url() or ""
                except Exception:
                    cur = ""
                if not _window_url_is_foreign(cur, URL):
                    return
                now = time.time()
                if now - _last_recover < 3.0:      # 防抖：load_url 失败时别反复导回
                    return
                _last_recover = now
                _launch_log(f"窗口被导航到应用外，已导回应用首页：{cur[:160]}")
                window.load_url(URL)
            except Exception:
                pass

        try:
            window.events.loaded += _on_loaded
        except Exception:
            pass

        # Windows 端退出 webview 后清理资源
        _launch_log("原生窗口已创建，启动 webview 主循环")
        # ── 2026-10-18 窗口就绪 watchdog（Windows）──
        # 背景：GUI 线程创建窗口是**静默**的。失败时进程活着、后端 API 200、
        # WebView2 渲染进程也在跑，但 EnumWindows 找不到本进程任何窗口 ——
        # 用户侧即"程序打不开/白屏"，且日志无任何异常，无从下手。
        # 探针实验（pywebview 6.2.1 + WebView2 155 全部通过）无法复现，
        # 故这里不做根因猜测，直接做**可观测 + 保底**：
        #   1. 轮询本进程可见顶层窗口（>300x200），出现即记录并退出；
        #   2. 25s 未见 → dump 全部线程栈到 ~/.vdl_window_timeout.log +
        #      回落浏览器（后端在跑，用户至少能完整使用，不至于干瞪眼）。
        if sys.platform == "win32":
            def _watch_window_ready():
                import ctypes as _ct
                from ctypes import wintypes as _wt
                _u32 = _ct.WinDLL("user32", use_last_error=True)
                _me = os.getpid()
                _found = []

                @_ct.WINFUNCTYPE(_ct.c_bool, _wt.HWND, _wt.LPARAM)
                def _cb(h, _p):
                    pid = _wt.DWORD()
                    _u32.GetWindowThreadProcessId(h, _ct.byref(pid))
                    if pid.value == _me and _u32.IsWindowVisible(h):
                        r = _wt.RECT()
                        _u32.GetWindowRect(h, _ct.byref(r))
                        if (r.right - r.left) > 300 and (r.bottom - r.top) > 200:
                            _found.append((r.left, r.top, r.right, r.bottom))
                    return True

                deadline = time.time() + 25.0
                while time.time() < deadline and not _found:
                    _found.clear()
                    try:
                        _u32.EnumWindows(_cb, 0)
                    except Exception:
                        return
                    if _found:
                        _launch_log(f"窗口就绪 watchdog：已确认可见窗口 {_found[0]}")
                        return
                    time.sleep(1.5)
                if not _found:
                    _launch_log("窗口就绪 watchdog：25s 未见可见窗口，dump 线程栈并回落浏览器")
                    try:
                        import faulthandler as _fh
                        _fh.dump_traceback(
                            file=open(Path.home() / ".vdl_window_timeout.log", "w"))
                    except Exception:
                        pass
                    _browser_fallback(server_thread)

            threading.Thread(target=_watch_window_ready, daemon=True,
                             name="vdl-window-watchdog").start()
        # 🔴 登录态持久化修复（2026-09-30）：pywebview 默认 private_mode=True，
        #   其 cocoa 后端在每次启动时 removeDataOfTypes(全部网站数据) —— 把 localStorage
        #   （含登录 token vdl_auth_token）连同 cookie 一并清空。后果：桌面 App 彻底退出
        #   （Cmd+Q / Dock Quit → os._exit）重开后必须重新登录。改为 private_mode=False
        #   后走 defaultDataStore（持久化），localStorage 跨启动保留 → 正常退出/重开自动
        #   保持登录态；仅在用户主动「退出账户」(logoutAccount 清掉 token) 时才需重登。
        webview.start(private_mode=False)
        _launch_log("webview 主循环结束，正常退出")
        os._exit(0)
    except Exception as _wv_err:  # pywebview 运行期异常（如 cocoa 初始化失败）→ 绝不静默闪退
        _launch_log(f"pywebview 运行异常({type(_wv_err).__name__})，无法打开原生窗口: {_wv_err!r}")
        _browser_fallback(server_thread)
        return

if __name__ == "__main__":
    if "--vdl-commentary-worker" in sys.argv:
        # 单二进制双角色：自身重入为解说管线 worker（依赖随包内置，无需外部 Python）
        raise SystemExit(_run_commentary_worker(sys.argv))
    main()
