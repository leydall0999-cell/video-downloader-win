# 国内站回源代理 · 实操手册

> 场景：video-downloader 部署在**海外**（Railway / Fly.io 等），但想下 **B站 / 抖音 / 腾讯** 等国内站。
> 这些站在海外 IP 会被地理围栏拦截（解析/下载报「该地区不可播放」）。
> 解决方案：在**物理位于国内**的机器上跑一个正向代理，Railway 用 `VDL_PROXY_CN` 指向它，
> 让国内站的请求从国内 IP 出去。

代码侧早已支持，无需改代码——只差一个「公网可达的国内出口」。本仓库附带的
[`cn_proxy.py`](./cn_proxy.py) 是一个**零依赖**（仅 Python 标准库）的 HTTP/HTTPS 正向代理，
可直接跑在国内机器上充当这个出口。

---

## 一、代理本身（国内机器上跑）

```bash
# 国内机器（有公网 IP 的 VPS，或你这台在国内、经隧道暴露的 Mac）
python3 cn_proxy.py 18888        # 监听 0.0.0.0:18888
```

- 支持 HTTP(GET/POST) 与 HTTPS(`CONNECT` 隧道) 两种转发。
- 仅用标准库，无需 `pip install`。
- 生产环境**务必加认证 / 限制来源 IP**（见第四节），裸奔代理会被扫爆。

---

## 二、三种「让 Railway 够得着」的方式（按推荐度）

### ✅ A. 国内 VPS（最稳，推荐）
买阿里云/腾讯云轻量（国内地域，约 ¥60–100/年），公网 IP 直连，无需隧道。
`cn_proxy.py` 已内置 Basic 鉴权（设 `CN_PROXY_AUTH` 环境变量即生效），公网暴露**必须开**。

**一键脚本（推荐）**：仓库自带 `setup_vps.sh`，在 VPS 上 root 跑一条命令即可完成下面 1–5 步：

```bash
# 把脚本弄到 VPS（任选其一）：
#   a) 本地 scp：  scp setup_vps.sh root@<VPS公网IP>:~/
#   b) VPS 上直接拉： curl -fsSL https://raw.githubusercontent.com/leydall0999-cell/video-downloader-web/main/setup_vps.sh -o setup_vps.sh

# 在 VPS 上执行（--auth 即代理账号密码，Railway 要用同一对）：
sudo bash setup_vps.sh --auth 你的user:你的强密码 --port 18888
# 脚本会自动：装 python3 → 放 cn_proxy.py 到 /opt/vdl-proxy → 写 systemd 单元(含 CN_PROXY_AUTH)
#            → 开机自启+崩溃重启 → 自检鉴权(无密码407/带密码200) → 放行防火墙
#            → 末尾打印 Railway 要填的 VDL_PROXY_CN
```

想手动一步步来，见下方分步说明。

**1) 上传脚本到 VPS**
```bash
mkdir -p /opt/vdl-proxy
scp cn_proxy.py root@<VPS公网IP>:/opt/vdl-proxy/cn_proxy.py
```

**2) 前台试跑 + 自检（确认鉴权生效）**
```bash
# 在 VPS 上
CN_PROXY_AUTH=你的user:你的强密码 python3 /opt/vdl-proxy/cn_proxy.py 18888
# 日志出现：listening on 0.0.0.0:18888 (auth=on, user=你的user)

# 另开窗口自检：
curl -s -x http://你的user:你的强密码@127.0.0.1:18888 http://www.bilibili.com -o /dev/null -w "%{http_code}\n"
# 期望 200/301（转发成功）；不带密码应返回 407
curl -s -x http://127.0.0.1:18888 http://www.bilibili.com -o /dev/null -w "%{http_code}\n"
# 期望 407（鉴权拦截）
```

**3) systemd 常驻（开机自启、崩溃重启）**
仓库附带 `cn_proxy.service` 示例：
```bash
scp cn_proxy.service root@<VPS公网IP>:/etc/systemd/system/cn_proxy.service
# 在 VPS 上编辑该文件，改 CN_PROXY_AUTH 与脚本路径，然后：
systemctl daemon-reload
systemctl enable --now cn_proxy
systemctl status cn_proxy        # 应 active (running)
```

**4) 防火墙放行（只放 Railway 出口更稳）**
```bash
# 腾讯云/阿里云控制台：安全组入站放行 TCP 18888，来源建议限 Railway 出口 IP
# 或 VPS 本机 firewalld：
firewall-cmd --permanent --add-port=18888/tcp && firewall-cmd --reload
```

**5) Railway 加环境变量并重新部署**
```
VDL_PROXY_CN=http://你的user:你的强密码@<VPS公网IP>:18888
```
（README 里也给了 `gogost/gost` Docker 版，二选一；两者都用 `user:pass` 鉴权，格式一致。）

### ⚠️ B. 本机隧道（零月费，但需绑卡 + 本机常开）
免费隧道服务**几乎都不透传 `CONNECT`**（HTTP 代理访问 HTTPS 目标必须用它），实测结论：

| 工具 | 结论 |
| --- | --- |
| `ngrok http 8899` | ❌ HTTP 隧道不透传 CONNECT，当代理用会被立即拒绝 |
| `ngrok tcp 8899` | ✅ 原始 TCP 转发，可用；但**免费版需绑信用卡**（不扣费）|
| `cloudflared tunnel --url http://localhost:8899` | ❌ 同上，不透传 CONNECT |
| `localhost.run` SSH 反向隧道 | ❌ 免费无密钥档 TCP 转发地址不明确 / 不可用 |

**ngrok tcp 可用路径**（本机需常开、ngrok 进程不能断）：
```bash
ngrok tcp 8899          # 得到 tcp://x.tcp.ngrok.io:PORT
```
Railway 设：
```
VDL_PROXY_CN=http://x.tcp.ngrok.io:PORT
```

> 本机 Mac 在国内、直连 B站 正常时，`VDL_PROXY_CN` 指向本机 `127.0.0.1:8899` 即可本地验证
> （无需隧道）。要上 Railway，必须换成公网可达地址。

### ✅ C. 你已有国内 socks5/http 出口
直接 Railway 设：
```
VDL_PROXY_CN=http://user:pass@host:port
# 或 socks5://user:pass@host:port
```

---

## 三、已验证（本机实测）

- 本机服务设 `VDL_PROXY_CN=http://127.0.0.1:8899` 后，解析 B站 → 代理日志确认真实透传：
  ```
  [cn_proxy] #1 CONNECT www.bilibili.com:443
  [cn_proxy] #2 CONNECT api.bilibili.com:443
  ```
- 真实下载 B站 mp4（20.5MB）成功，「路由分流 + 代理」整条链 100% 可用。
- 海外站（YouTube）仍走 `VDL_PROXY`/系统代理，与国内站互不串。

→ **代码 + 代理链路已证明可用，只差「公网可达的国内出口」这一步。**

---

## 四、安全提醒（重要）

- 公网暴露的代理是**开放中继**，务必加固（建议叠加）：
  1. **加代理认证**（首选）：`cn_proxy.py` 已内置——设 `CN_PROXY_AUTH=user:pass` 环境变量即要求所有请求（含 CONNECT）带 Basic 鉴权，否则返回 407；gost 版用 `user:pass@` 同理。Railway 的 `VDL_PROXY_CN` 写成 `http://user:pass@host:port` 即可。
  2. **限制来源 IP**（更稳）：VPS 安全组入站只放行 Railway 出口 IP，或本机 firewalld 限制来源。
  3. 仅用随机隧道 URL（ngrok/cloudflared，URL 即密码，但不稳定）。
- 该代理**仅供本服务回源国内站点**，不要对外提供跨境代理服务。
- 未设 `CN_PROXY_AUTH` 时 `cn_proxy.py` 不鉴权，仅可用于「已限制来源 IP / 随机隧道 URL」场景，切勿公网裸奔。

---

## 五、验证上线

Railway 加好 `VDL_PROXY_CN` 并重新部署后，硬刷新站点，贴一个 B站链接解析：
- 不再报「该地区不可播放」即成功；
- 仍报地区限制 → 检查 `VDL_PROXY_CN` 是否公网可达、代理是否在国内机器上运行、端口是否放行。
