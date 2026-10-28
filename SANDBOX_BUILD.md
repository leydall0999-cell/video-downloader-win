# 沙盒约束与离线验证工作流

本项目在 WorkBuddy 沙盒中开发/构建，但**分发与运行在用户本机（macOS）**。
沙盒到外部网络有部分限制，曾导致反复「发 dmg → 用户真机试 → 出问题 → 再发」的循环。
本文档沉淀约束与对策，确保**整个项目在沙盒内可验证、可自测**，不再依赖用户当测试小白鼠。

---

## 一、沙盒网络约束（实测）

Bash 环境有代理，仅放行部分域名：

| 目标 | 沙盒内 | 说明 |
|------|--------|------|
| `api.github.com` | ✅ 可达 | gh CLI 的 API 操作可行 |
| GitHub releases CDN | ✅ 可达 | 构建时能 `curl` 下载 baiduPCS-Go / aria2c |
| 百度 `getqrcode` | ✅ 可达 | 能真实调通，返回二维码 |
| `github.com` 主站 git 协议 | ❌ 被拦 | `git push/clone` 到 github.com 不可行 |
| 百度 `passport channel/unicast` | ⚠️ 长轮询超时 | 扫码确认的轮询接口连不通 |
| 其他外部 API（yt-dlp 解析等） | ⚠️ 不稳定 | 不应在沙盒里依赖 |

**对策**：
- 构建期外部依赖 → 改为**本地预打包**（二进制在构建脚本里下载并装入用户目录），不依赖运行时联网。
- 测试期外部 API → 用**本地 Mock** 模拟（见第三节），严格按已抓到的真实响应格式。

---

## 二、git 推送

- 远程是 SSH：`git@github.com:leydall0999-cell/video-downloader-win.git`（2026-10-28 三仓拆分：桌面端在 `-win` 仓，网页版在 `-web` 仓，旧 `video-downloader` 只作历史）
- 沙盒内 `git push` 到 github.com **不可行**（代理拦 443 git 协议）。
- **推送请在用户本机执行**（本机网络可访问 github.com，用 SSH 推绕开代理拦截）。
- 沙盒内可行：gh CLI 的 `api` 类操作（走 `api.github.com`）。

---

## 三、离线验证工作流（核心）

任何改动后，先在沙盒内跑这套，**全过才发 dmg**：

```
cd server && bash tests/run_offline_tests.sh
```

包含：

1. **`tests/test_baidu_qr_offline.py`**
   本地起 `http.server` 模拟百度 passport（`getqrcode` + `unicast` JSONP），
   跑通 生成二维码 → 轮询(待扫/已扫/已确认) → 提取 BDUSS → 调 `baidu_pcs.login` 全链路。
   覆盖三种场景：BDUSS 经 Set-Cookie 下发 / 响应体兜底提取 / 失效 sign 安全返回。

2. **`tests/test_app_smoke.py`**
   用 `FastAPI TestClient` 无头 import 整个 `app`，对**所有 `/api/pcs/*` 路由**发请求，
   确认路由已装配、返回合法 JSON、无 500。后端函数用 `unittest.mock` 隔离，
   完全不碰网络/二进制。另含一个真实 `baidu_pcs.status()` 调用（仅查本机二进制路径，不联网）。

**退出码非 0 = 有失败用例。**

### 已接入构建

`desktop/build_mac.sh` 末尾新增「构建自验证」：
- 检查 baiduPCS-Go 二进制已安装到用户目录且可执行
- 检查 `baidu_pcs.py` / `baidu_qr.py` 已打进 `.app`
- 运行上面的离线测试套件
- **任一失败 → `exit 1` 拦截发布**，不会产出「看起来成功实则坏掉」的 dmg

---

## 四、新增外部依赖 / 接口时的规范

1. 任何「需要联网才能验证」的功能，**必须配一个本地 Mock + 离线测试**，放进 `server/tests/`。
2. 不在沙盒里依赖用户真机试错；真机只用于确认「百度真实协议细节差异」这一类沙盒无法覆盖的点。
3. Mock 的响应格式要**严格匹配已抓到的真实响应**（先 `dangerouslyDisableSandbox` 探通再写 Mock），避免 Mock 与真实行为偏离。

---

## 五、已解决的 macOS 打包坑（防回归）

| 坑 | 根因 | 对策 |
|----|------|------|
| 改了代码构建后模块没加载 | `build_mac.sh` 会 `rm -f *.spec`，hiddenimports 写在 spec 里被删 | hiddenimports / `--collect-all` 全部放 **pyinstaller 命令行参数** |
| 二进制没打包进 app | 只 `--add-binary` 但未确认路径 | 构建时 `find` 确认 `BaiduPCS-Go` 在 `Frameworks/bin/` |
| 运行时 `Errno 13 Permission denied` | macOS 禁止 `.app` 执行包内非原始签名二进制 | 构建时把二进制**预装到 `~/.video-downloader/baidupcs/bin/`**，运行时只从该路径执行 |
| zip 嵌套路径找不到二进制 | GitHub release 的 zip 内层还有一层同名目录 | `_find_existing_binary()` 增加 `bin/BaiduPCS-Go/BaiduPCS-Go` 嵌套查找 |
| 登录误判成功 | baiduPCS-Go 失败时仍返回退出码 0 | `login()` 扫输出里的失败标记（错误代码/errno/系统繁忙/密码错误…），不只信退出码 |

---

## 六、本地手动验证（可选）

想在不发 dmg 的情况下快速验后端：

```bash
cd server
../.build_venv/bin/python -c "
from fastapi.testclient import TestClient
import app
c = TestClient(app.app)
print(c.get('/api/pcs/status').json())
"
```
