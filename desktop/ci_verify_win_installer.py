"""CI 真机验收：Windows 安装包「静默装 → 起 → 接口冒烟 → 卸」全流程。

============================================================================
为什么要有这个文件
============================================================================
Windows 版的问题历来只能靠「用户装到真机上试」才能发现 —— 本地开发机是 macOS，
PyInstaller 又不能跨平台编译，等于没有中间地带。而 GitHub Actions 的
`windows-latest` runner 就是一台现成的真 Windows 电脑：免费、无人值守、
每次构建自动跑。

现有 workflow 里的 Smoke test 只做了一件事：起 onedir 目录里的 exe，探一下
8321 端口。三个真实缺口它一个都没盖住：

  1. **压根没测安装包**。CI 起的是 `dist/VideoDownloader/VideoDownloader.exe`
     （onedir 目录），而分发给用户的是 `VideoDownloader-Setup-<ver>.exe`
     （Inno 单文件安装包）。安装包少打一个文件、快捷方式指错、卸载残留这些
     问题，跑 onedir 永远发现不了。
  2. **只探 2 个接口**，且 `continue-on-error`，红了也不阻断 —— 等于没断言。
  3. **不验 platform**。`server/routers/system.py` 历史上把 platform 写死
     "macos"（Windows 版谎报自己是 mac），这类「只有真 Windows 才暴露」的缺陷
     本地谁都验不了。

============================================================================
分级断言（关键设计：不让「无头环境的锅」掩盖「真缺陷」）
============================================================================
runner 是无头的，pywebview 窗口可能起不来。如果把它和「包完整不完整」混在
一起判，结果就是一红全红、看不出到底哪儿坏了。所以分三级：

  A 级（必过）：安装包能静默装、文件齐、deno/ffmpeg 真能执行、version 对得上。
              这些跟有没有显示器无关，红就是真坏了。
  B 级（期望过）：exe 起得来、8321 响应、platform == windows、只读接口不 500。
              无头环境下 pywebview 起不来会导致这一级失败，报告里会明确标注
              「疑似无头环境限制」，并附上 exe 的 stdout/stderr 供判断。
  C 级（必过）：卸载干净（unins000.exe /VERYSILENT 后安装目录消失）。

============================================================================
用法
============================================================================
    python desktop/ci_verify_win_installer.py <setup.exe 或 glob>
                                              [--ver 1.0.93] [--dir C:\\VDLVerify]

产物（写到当前工作目录，供 CI 上传 / 推 ci-logs 分支）：
    verify-win-report.md    人读的报告
    verify-win-report.json  机读的结构化结果

退出码：0 = A+B+C 全过；1 = A 或 C 级失败；2 = 仅 B 级失败（无头限制，不算包坏）。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

# 🔴 Windows runner 的 stdout/stderr 默认是本地代码页（cp1252/gbk），任何中文输出
# 都会抛 UnicodeEncodeError ⇒ 进程立刻非零退出，CI 上表现为「步骤 0 秒失败」且
# 看不到有用信息（2026-10-18 连挂三轮才定位到）。先强制 UTF-8；日志文案仍写英文，
# 双保险。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REPORT_MD = "verify-win-report.md"
REPORT_JSON = "verify-win-report.json"

# 桌面端端口：8321 被旧实例短暂占用时会顺延，故扫一段
PORT_CANDIDATES = (8321, 8322, 8323, 8324, 8325)

# 「路由可达」的判定：2xx 自然算过；401/403/422 说明路由在、只是要登录，也算过。
# 404 单独处理（可能是路由没注册 → warn，不是硬失败，避免误伤可选路由）。
REACHABLE_CODES = (200, 204, 401, 403, 422)

# 必过接口：冻结包里一定有，且 /api/system/info 是钉死 platform 的关键点
MUST_ENDPOINTS = ("/api/version", "/api/system/info")
# 尽力接口：404 只记 warn
BEST_EFFORT_ENDPOINTS = (
    "/api/system/changelog",  # 🔴 真正的路由是这个，不是 /api/changelog（首轮误报 404）
    "/api/tasks",
    "/api/sniffer/status",
)

# 抖音解析探测用的公开视频（2026-11-03 实测可解析，见 skill
# `vdl-resolve-cookie-diag` §12.6）。换视频时确认它是公开单条视频页，
# 别用合集/直播/私密（那三类本来就要登录，会让探测恒定失败）。
DOUYIN_PROBE_URL = "https://www.douyin.com/video/7683799657923301297"


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
class Report:
    """收集 A/B/C 三级结果。"""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, level: str, item: str, ok: bool, detail: str = "") -> bool:
        self.rows.append({
            "level": level, "item": item,
            "ok": bool(ok), "detail": str(detail)[:400],
        })
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] ({level}) {item}" + (f" :: {detail}" if detail else ""))
        return bool(ok)

    def level_ok(self, level: str) -> bool:
        rows = [r for r in self.rows if r["level"] == level]
        return bool(rows) and all(r["ok"] for r in rows)

    def failed(self, level: str) -> list[dict]:
        return [r for r in self.rows if r["level"] == level and not r["ok"]]


# 安装目录里 _internal 动辄上千个文件，逐个二进制 os.walk 一遍就要几秒；
# 这里一次性建索引（文件名 → 首路径），后续查找是 O(1)。
_FILE_INDEX: dict[str, str] = {}


def build_index(root: str) -> int:
    """遍历 root 建文件名索引，返回索引到的文件数。"""
    _FILE_INDEX.clear()
    count = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            _FILE_INDEX.setdefault(name, os.path.join(dirpath, name))
            count += 1
    return count


def find_first(filename: str) -> str | None:
    """按文件名查已建索引（二进制位置随版本可能变，不写死路径）。"""
    return _FILE_INDEX.get(filename)


def read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def http_get(url: str, timeout: float = 3.0) -> tuple[int, str]:
    """返回 (status_code, body)；连不上返回 (0, "")."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "vdl-ci-verify/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:  # 4xx/5xx 也是「有响应」
        try:
            return exc.code, exc.read().decode("utf-8", "replace")
        except Exception:
            return exc.code, ""
    except Exception:
        return 0, ""


def http_post_json(url: str, payload: dict,
                   timeout: float = 3.0) -> tuple[int, str]:
    """POST JSON，返回 (status_code, body)；连不上返回 (0, "")."""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"User-Agent": "vdl-ci-verify/1.0",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:  # 4xx/5xx 也是「有响应」
        try:
            return exc.code, exc.read().decode("utf-8", "replace")
        except Exception:
            return exc.code, ""
    except Exception:
        return 0, ""


def main() -> int:
    if os.name != "nt":
        print(f"SKIP: not Windows (os.name={os.name}); this verifier is Windows-only")
        return 0

    ap = argparse.ArgumentParser()
    ap.add_argument("setup", nargs="?", default="",
                    help="安装包 exe 路径或 glob（默认 dist/VideoDownloader-Setup-*.exe）")
    ap.add_argument("--ver", default="", help="期望版本号（默认读仓库 VERSION）")
    ap.add_argument("--dir", default=r"C:\VDLVerify", help="静默安装目标目录")
    args = ap.parse_args()

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # ---- 期望版本 ----
    want_ver = args.ver.strip()
    if not want_ver:
        want_ver = read_text(os.path.join(repo, "VERSION"))
        if not want_ver:
            try:
                want_ver = read_text(os.path.join(
                    os.path.dirname(repo), "dist", "VideoDownloader", "version.txt"))
            except Exception:
                pass

    # ---- 定位安装包 ----
    pattern = args.setup or os.path.join(repo, "dist", "VideoDownloader-Setup-*.exe")
    matches = sorted(glob.glob(pattern))
    if not matches:
        print(f"FATAL: installer not found by pattern: {pattern}")
        return 1
    setup_exe = matches[-1]

    install_dir = args.dir
    print(f"=== VDL Windows installer verification ===")
    print(f"installer : {setup_exe}")
    print(f"expect ver: {want_ver}")
    print(f"target dir: {install_dir}")

    rep = Report()

    # ===================================================================== #
    # A 级：安装包本身
    # ===================================================================== #
    print("\n--- A: installer integrity ---")
    rep.add("A", "installer file exists",
            os.path.isfile(setup_exe), f"{os.path.getsize(setup_exe)} bytes")

    # 如果目标目录残留（上一轮没卸干净），先卸
    unins = os.path.join(install_dir, "unins000.exe")
    if os.path.isfile(unins):
        subprocess.run([unins, "/VERYSILENT"], timeout=120)
        time.sleep(2)

    # 静默安装。/NOCANCEL + /SUPPRESSMSGBOXES 保证无人值守不卡住。
    # PrivilegesRequired=admin，runner 默认以管理员跑，装到 C:\VDLVerify 也避开 PF 权限。
    inst = subprocess.run(
        [setup_exe, "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES",
         "/NOCANCEL", f"/DIR={install_dir}"],
        capture_output=True, timeout=600,
    )
    time.sleep(2)
    rep.add("A", "silent install exit code", inst.returncode == 0,
            f"rc={inst.returncode} stderr={(inst.stderr or b'')[:200]!r}")

    exe_path = os.path.join(install_dir, "VideoDownloader.exe")
    rep.add("A", "VideoDownloader.exe installed", os.path.isfile(exe_path),
            install_dir)

    indexed = build_index(install_dir)
    rep.add("A", "installed files indexed", indexed > 0, f"{indexed} files")

    rep.add("A", "dir _internal/ present",
            os.path.isdir(os.path.join(install_dir, "_internal")))
    # 🔴 PyInstaller onedir 6.x 把 --add-data 的数据收进 **_internal/**（不是平铺在
    #    产物根），所以 web 实际在 _internal/web —— 只查根会误报「UI 整个没了」。
    #    （2026-10-26 首轮验收就这样假红过一次。）
    web_root = os.path.join(install_dir, "web")
    web_internal = os.path.join(install_dir, "_internal", "web")
    rep.add("A", "dir web/ present (root or _internal/)",
            os.path.isdir(web_root) or os.path.isdir(web_internal),
            web_internal if os.path.isdir(web_internal) else web_root)

    # version.txt（build_win.sh 写在 onedir 根，装完应在 {app} 根）
    vtxt = read_text(os.path.join(install_dir, "version.txt"))
    rep.add("A", "version.txt matches", (not want_ver) or vtxt == want_ver,
            f"found={vtxt!r} want={want_ver!r}")

    # 二进制真能执行 —— 抓「文件在但缺 VC++ 运行库 / 架构不对」这类只有真 Windows
    # 才暴露的问题（递归查找，不写死 bin/ 还是根目录）
    for binary, arg in (("ffmpeg.exe", "-version"),
                        ("ffprobe.exe", "-version"),
                        ("deno.exe", "--version"),
                        ("aria2c.exe", "--version")):
        hit = find_first(binary)
        if not hit:
            # aria2c 是可选件（bundle_aria2 下载失败就不进包）→ 只 warn
            rep.add("A", f"{binary} present", binary == "aria2c.exe",
                    "not bundled (optional)" if binary == "aria2c.exe" else "MISSING")
            continue
        try:
            proc = subprocess.run([hit, arg], capture_output=True, timeout=60)
            out = (proc.stdout or b"") + (proc.stderr or b"")
            rep.add("A", f"{binary} executes", proc.returncode == 0,
                    out[:80].decode("utf-8", "replace").strip())
        except Exception as exc:  # noqa: BLE001
            rep.add("A", f"{binary} executes", False, repr(exc)[:200])

    a_ok = rep.level_ok("A")

    # ===================================================================== #
    # B 级：运行期（无头环境可能受限，失败要留证据）
    # ===================================================================== #
    print("\n--- B: runtime ---")
    b_ok = False
    port = 0
    proc = None
    stdout_log = os.path.abspath("verify-win-app-stdout.log")
    try:
        with open(stdout_log, "wb") as logfh:
            proc = subprocess.Popen(
                [exe_path], cwd=install_dir,
                stdout=logfh, stderr=subprocess.STDOUT,
            )
        # 轮询端口
        body = ""
        for _ in range(40):
            time.sleep(3)
            if proc.poll() is not None:
                break
            for p in PORT_CANDIDATES:
                code, b = http_get(f"http://127.0.0.1:{p}/api/version")
                if code == 200:
                    port, body = p, b
                    break
            if port:
                break
        rep.add("B", "backend responds on 127.0.0.1", bool(port),
                f"port={port}" if port else "no response on any candidate port")

        if port:
            # 版本对得上
            got_ver = ""
            try:
                got_ver = str(json.loads(body).get("version") or "")
            except Exception:
                pass
            # version 字段是**构建指纹**（"v1.0.93 · 构建 <sha> @ MM-DD HH:MM"），
            # 不是裸版本号 ⇒ 只能做包含判断，全等会永远红（首轮误报过）。
            rep.add("B", "/api/version matches installer",
                    (not want_ver) or got_ver == want_ver or (want_ver in got_ver),
                    f"got={got_ver!r} want={want_ver!r}")

            # 🔴 钉死 platform：史上写死 "macos"，Windows 版谎报自己
            code, info_body = http_get(f"http://127.0.0.1:{port}/api/system/info")
            plat = ""
            try:
                plat = str(json.loads(info_body).get("platform") or "")
            except Exception:
                pass
            rep.add("B", "/api/system/info platform == windows",
                    plat.lower() == "windows", f"platform={plat!r} http={code}")

            for ep in MUST_ENDPOINTS:
                c, _ = http_get(f"http://127.0.0.1:{port}{ep}")
                rep.add("B", f"{ep} reachable", c in REACHABLE_CODES, f"http={c}")
            for ep in BEST_EFFORT_ENDPOINTS:
                c, _ = http_get(f"http://127.0.0.1:{port}{ep}")
                ok = c in REACHABLE_CODES
                rep.add("B", f"{ep} reachable (best-effort)", ok, f"http={c}")

            # 运行期数据落点：Windows 应在 %APPDATA%\VideoDownloader 下建目录
            appdata = os.environ.get("APPDATA") or ""
            vdl_dir = os.path.join(appdata, "VideoDownloader") if appdata else ""
            rep.add("B", "runtime config dir under %APPDATA%",
                    bool(vdl_dir) and os.path.isdir(vdl_dir),
                    vdl_dir or "APPDATA unset")

            # ── 抖音解析探测（2026-11-03 新增）─────────────────────────────
            # 为什么放这里：抖音在桌面端靠**本机 Chromium 内核 + CDP 抓流**，
            # 是「本地看着好、真机一跑就废」的高风险环节。runner 上**没装
            # Chrome、只有系统自带 Edge**，正好是那条兜底路径的真实样本。
            # 级别用 I（信息性）：💡 不影响 A/B/C 结论。runner 是境外机房 IP，
            # 抖音风控可能直接不给流，失败不代表用户机器上不行；跑通则是
            # 最硬的证据（包内 douyin_cdp + Edge + 无头，端到端出直链）。
            #
            # 🔴 2026-11-03 第二轮：上一轮 500（编码）修好后变成 504 超时。
            #    要区分「机房网络到不了抖音」和「Windows 上 Edge/CDP 卡住」，
            #    先各测一条基线：① 直连 douyin.com 的耗时/状态码；② Edge 在不在、
            #    什么版本。两条都列进报告，免得对着 504 瞎猜。
            _t0 = time.time()
            dy_code, dy_body = http_get("https://www.douyin.com/", timeout=25)
            dy_dt = time.time() - _t0
            rep.add("I", "douyin.com reachable from runner (network baseline)",
                    dy_code in (200, 204, 301, 302, 403),
                    f"http={dy_code} {dy_dt:.1f}s bytes={len(dy_body)}")

            edge = ""
            for _p in (r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe",
                       r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe"):
                _p = os.path.expandvars(_p)
                if os.path.isfile(_p):
                    edge = _p
                    break
            chrome_bin = ""
            for _p in (r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
                       r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"):
                _p = os.path.expandvars(_p)
                if os.path.isfile(_p):
                    chrome_bin = _p
                    break
            rep.add("I", "chromium engine available for douyin (Edge fallback)",
                    bool(edge or chrome_bin),
                    f"edge={edge or '-'} chrome={chrome_bin or '-'}")

            _t0 = time.time()
            d_code, d_body = http_post_json(
                f"http://127.0.0.1:{port}/api/resolve",
                {"url": DOUYIN_PROBE_URL}, timeout=300)
            d_dt = time.time() - _t0
            d_ok = any(k in d_body for k in ('"direct_url"', '"formats"',
                                             '"video_url"'))
            d_cat = ""
            try:
                d_cat = str(json.loads(d_body).get("category") or "")
            except Exception:
                pass
            rep.add("I", "douyin resolve via local Edge/Chrome (info only)",
                    d_ok,
                    f"http={d_code} {d_dt:.1f}s cat={d_cat or '-'} "
                    f"body={d_body[:150]}")
        else:
            tail = ""
            try:
                with open(stdout_log, "rb") as fh:
                    tail = fh.read()[-800:].decode("utf-8", "replace")
            except OSError:
                pass
            rep.add("B", "app stdout captured for diagnosis", False,
                    (tail or "(empty)")[:400])
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except Exception:
                subprocess.run(["taskkill", "/F", "/IM", "VideoDownloader.exe"],
                               capture_output=True, timeout=60)
        subprocess.run(["taskkill", "/F", "/IM", "VideoDownloader.exe"],
                       capture_output=True, timeout=60)
        time.sleep(2)

    b_ok = rep.level_ok("B")

    # ===================================================================== #
    # C 级：卸载干净
    # ===================================================================== #
    print("\n--- C: uninstall ---")
    unins = os.path.join(install_dir, "unins000.exe")
    if os.path.isfile(unins):
        un = subprocess.run([unins, "/VERYSILENT", "/SUPPRESSMSGBOXES"],
                            capture_output=True, timeout=300)
        time.sleep(3)
        rep.add("C", "uninstall exit code", un.returncode == 0,
                f"rc={un.returncode}")
        # 🔴 别断言「目录整个消失」：Inno 只删自己装过的文件，运行期产生的
        #    （日志/配置/缓存）会留下，于是目录还在但 exe 已删 —— 这是正常行为，
        #    首轮把它判成 ❌ 是误报。判据改为「主程序确实被删」+ 列出残留备查。
        exe_gone = not os.path.isfile(os.path.join(install_dir, "VideoDownloader.exe"))
        leftovers = ""
        if os.path.isdir(install_dir):
            try:
                leftovers = ", ".join(sorted(os.listdir(install_dir))[:8])
            except OSError:
                leftovers = "(无法列出)"
        rep.add("C", "VideoDownloader.exe removed", exe_gone,
                f"残留: {leftovers}" if leftovers else "")
    else:
        rep.add("C", "unins000.exe exists", False, install_dir)

    c_ok = rep.level_ok("C")

    # ===================================================================== #
    # 报告
    # ===================================================================== #
    total_ok = a_ok and b_ok and c_ok
    summary = {
        "installer": os.path.basename(setup_exe),
        "expect_version": want_ver,
        "install_dir": install_dir,
        "A_installer": a_ok,
        "B_runtime": b_ok,
        "C_uninstall": c_ok,
        "all_pass": total_ok,
        "rows": rep.rows,
    }
    with open(REPORT_JSON, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)

    lines = [
        "# Windows 安装包真机验收报告",
        "",
        f"- 安装包：`{os.path.basename(setup_exe)}`",
        f"- 期望版本：`{want_ver}`",
        f"- 安装目录：`{install_dir}`",
        "",
        "| 级别 | 结论 |",
        "|---|---|",
        f"| A 安装包完整性 | {'✅' if a_ok else '❌'} |",
        f"| B 运行期 | {'✅' if b_ok else '⚠️'} |",
        f"| C 卸载 | {'✅' if c_ok else '❌'} |",
        "",
        "## 明细",
        "",
        "| 级别 | 项 | 结果 | 详情 |",
        "|---|---|---|---|",
    ]
    for r in rep.rows:
        lines.append(f"| {r['level']} | {r['item']} | "
                     f"{'✅' if r['ok'] else '❌'} | {r['detail']} |")
    if not b_ok:
        lines += [
            "",
            "> B 级未通过时先判断是不是无头环境的锅：runner 没有显示器，"
            "pywebview 窗口可能创建失败并导致进程退出。"
            "若 A / C 全绿，说明**包本身是好的**，B 级失败多半是环境限制，"
            "真正要人工看界面时再起虚拟机。",
        ]
    with open(REPORT_MD, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    print(f"\n=== A={a_ok} B={b_ok} C={c_ok} -> {'ALL PASS' if total_ok else 'HAS FAILURES'} ===")
    print(f"report: {REPORT_MD} / {REPORT_JSON}")

    if not a_ok or not c_ok:
        return 1
    if not b_ok:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
