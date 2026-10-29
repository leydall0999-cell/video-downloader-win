#!/usr/bin/env bash
# 离线测试运行器：在沙盒内验证 VideoDownloader 后端，不依赖任何外部网络。
#
# 覆盖：
#   1. test_app_smoke.py              —— FastAPI TestClient 无头冒烟核心路由
#   2. test_static_undefined_names.py —— 静态守卫：全量扫描 server/ 的「未定义名」
#                                        （py_compile 查不到、只在运行时炸的漏 import）
#   3. test_downloader_url_parsing.py —— 下载器链接解析/归一化（B站短链、追踪参数）
#   3. test_matting_core.py           —— 抠图前处理/选区/边缘柔化 纯函数
#   4. test_dewatermark_core.py       —— 去水印选区归一化/mask 合成/瓦片羽化权重
#   3b. test_matting_forward.py       —— 抠图 AI 推理前向链路（假 session 注入，不下载权重）
#   4b. test_dewatermark_forward.py   —— 去水印 AI 推理前向链路（假 session 注入，不下载权重）
#   5. test_convert_guards.py         —— 转换入口守卫（分片 id/排序、本地路径白名单）
#   6. test_commentary_routes.py      —— 解说路由层（试听路径守卫、sidecar 推导、kind 白名单、
#                                         解说词字数/时长预检（防原速渲染静默截断旁白））
#   7. test_codec_utils.py            —— LGPL 编码器选择（H.264/HEVC 降级链、GPL 红线）
#   8. test_convert_pipeline.py       —— 转码管线决策 + 参数构造 + 真实转码端到端
#   9. test_ffmpeg_tools.py           —— 媒体加工（裁剪注入防护、抽音频/封面/铃声/去水印）
#  10. test_selfupdate.py             —— 自更新链路（全量 sha/校验闸门、增量版本门槛、
#                                        套用后的权限保持、发布脚本排序守卫）
#  11. test_sr.py                    —— 高清修复（档位/倍率校验、AI 输入上限、
#                                        轮询端点免限流、CoreML 不得指定计算单元）
#
# 注：7~9 含调用真实 ffmpeg 的端到端用例（合成素材，无需网络）；
#     若构建机没有 ffmpeg，这些用例会打印「⚠️ 跳过」而非失败。
#
# ✅ 2026-10-14 已清零：曾因「当脚本跑 ⇒ 断言静默跳过」而长期假绿的 10 个 pytest 用例，
#    已逐个分诊修好并全部切到 run_pytest_root（下面按成因归类，改动都有 git 史可查）：
#      ① 用例过期（代码是有意演进，断言没跟）——更新断言到新语义：
#          test_dewatermark_layout.py (.dw-compare 改 max-height:60vh 自适应)
#          test_ffmpeg_tools_unit.py (压缩弃 libx264/-crf；去水印 delogo→LGPL 模糊链)
#          test_narration_preview.py (TTS 桩缺 2026-09-18 新增的 provider 参数)
#          test_sidebar_widen.py (.wrap 800→1400；侧栏改版后 tab 清单/缩进变化)
#          test_commentary_routes.py (_require_task → _require_owned_task + 旁路日配额)
#      ② 未 mock 新上的闸门——补打桩：
#          test_dewatermark.py (AI 回落探测 + 真实像素算法，fake 库撑不住)
#          test_upload_src_filename.py (上传积分闸门 → 402 MEMBER_QUOTA)
#      ③ 真问题/环境假象——分别处理：
#          test_login_gate.py   → **代码 bug 已修**：门禁豁免 suffix 写 "/cancel"（带斜杠）
#                                  却用 rsplit("/",1)[-1]（不带斜杠）比 ⇒ 豁免整段死代码
#          test_web_contract.py → **声明 bug 已修**：f304571 移除网盘功能集群后，
#                                  platform_model.NODE_GROUPS 仍留 cloud/archive 两个幽灵组
#          test_admin_superuser.py → **测试环境假象**：TestClient 的 client.host 是
#                                  "testclient"（非回环），而 P1-2 安全审计后公网不 bootstrap
#                                  ⇒ 首用户拿不到超管。已在 fixture 里模拟本机回环
#                                  （回环判定本身由 server/tests/test_local_trust_p3_1.py 守卫）
#    ⚠️ 教训：pytest 用例被 run_one/run_root 当脚本跑时，断言从不执行、恒"通过"。
#       新增 pytest 用例必须走 run_pytest_root，登记后要验证断言真的跑了。
#
# 退出码非 0 表示有测试失败（可在 build_mac.sh 末尾调用以阻断坏构建）。
set -u

# 登录门禁（2026-09-26）：生产默认开启——能力型 POST 端点（解析/下载/转换/抠图/去水印/字幕/
# 解说/订阅等）未登录一律 401 + NO_AUTH，见 server/app.py 的 _login_gate。
# 本运行器里这批脚本测试的是「功能内部逻辑」，都直接打端点且不模拟登录态，
# 因此整体关掉门禁（保持它们原本的语义）。门禁自身的拦截/放行由
# tests/test_login_gate.py 单独打开开关验证。
export VDL_LOGIN_GATE=0

# ⚠️ 数据目录隔离（2026-09-30 事故后必设）：
# credential_store 的 Keychain 是**系统级**的，只能靠 VDL_DATA_DIR 这个开关来
# 判断「当前是不是测试环境」并切到 .test 命名空间。本运行器此前只设了
# VDL_LOGIN_GATE，没有 VDL_DATA_DIR → 跑全量套件时测试桩 token（"tok-1"）
# 把真实账号 15014313254 的 Keychain 条目覆盖了，线上登录态直接 BAD_TOKEN。
# 所以这一行不是可选的：没有它，跑一次全量测试就会弄坏真实登录凭据。
export VDL_DATA_DIR="${VDL_DATA_DIR:-/tmp/vdl_offline_data}"
mkdir -p "$VDL_DATA_DIR" 2>/dev/null

# ⚠️ 云端套餐/免费额度「读」隔离（2026-10-13 接续待办 #2 后必设）：
# 免费额度口径（free_quota.mode）上云后，free_quota_mode() 在本机未设时会回读
# 授权中心 `/api/license/plans` 的 free_quota 字段（复用 cloud_plan_overrides 的
# 同一条缓存通道）。若本机恰好配了授权中心基址、且线上把口径设成了 off，
# 断言「本机语义」的用例（如 test_membership 的免费 10 次/日、test_free_quota_mode_*）
# 就会被线上状态污染成不确定。离线套件必须与云端解耦，故统一关掉「读云端」。
# （只关「读」：push_* 下发走 _license_api 直连，不受此开关影响。）
export VDL_PLANS_CLOUD=0

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVER="$REPO/server"

# 优先用项目 build venv（含 requests / fastapi / httpx），否则回退 managed python
# Windows 构建机（build_win.sh）venv 在 .build_venv/Scripts/，与 mac 的 bin/ 并列候选。
PY=""
for cand in "$REPO/.build_venv/bin/python" "$REPO/.build_venv/Scripts/python.exe" "/Users/suixindelang/.workbuddy/binaries/python/versions/3.13.12/bin/python3"; do
  if [ -x "$cand" ]; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
  echo "❌ 找不到可用的 python（需 requests + fastapi + httpx）"
  exit 1
fi

echo "▶ 使用解释器: $PY"
echo "▶ 运行离线测试（无外部网络依赖）..."
cd "$SERVER" || exit 1

PASS=0
FAIL=0
# 失败套件名清单（2026-10-22）：只有计数没有名字时，偶发失败无法定位。
FAILED_LIST=""

run_one() {
  local f="$1"
  echo ""
  echo "=== $f ==="
  if "$PY" "tests/$f" 2>&1; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1)); FAILED_LIST="$FAILED_LIST $f"
  fi
}

# --- Node 解释器（前端结构守卫用；与 web-dev 侧同一套办法）---
# 找不到就只跳过那几条，不让「本机没装 node」把整份套件染红。
NODE=""
for cand in /Users/suixindelang/.workbuddy/binaries/node/versions/*/bin/node \
            "$(command -v node 2>/dev/null)"; do
  if [ -n "$cand" ] && [ -x "$cand" ]; then NODE="$cand"; break; fi
done

run_node() {
  local f="$1"
  echo ""
  echo "=== $f (node) ==="
  if [ -z "$NODE" ]; then
    echo "⏭ 跳过：未找到 node 解释器（前端结构守卫需要 node）"
    return 0
  fi
  if "$NODE" "tests/$f" 2>&1; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1)); FAILED_LIST="$FAILED_LIST $f"
  fi
}

run_one test_app_smoke.py
run_one test_static_undefined_names.py
run_one test_membership.py
run_one test_member_quota_e2e.py
run_one test_matting_member.py
# 3a. test_matting_credit_gate.py —— 云端抠图「积分不足」必须不升级 + 有提示（A+B 段）
run_one test_matting_credit_gate.py
run_one test_subtitle_quota.py
run_one test_subtitle_preview.py
run_one test_subtitle_audio.py
run_one test_subtitle_music_fallback.py
run_one test_subtitle_lyrics.py
run_one test_sensevoice_lazy_model.py    # SenseVoice 模型懒加载：下载/验签/解压/降级/接线（2026-10-15 安装包瘦身 228MB）
run_one test_quality_options.py
run_one test_quality_member_gate.py          # 清晰度会员门槛：免费档 2K/4K → 402 引导开会员（2026-10-02 用户「免费用户 1080 以上要弹会员」）
run_one test_quality_member_ui_wiring.py     # 清晰度门槛三端接线：后端两入口都挂门 + 前端弹会员中心（不弹＝用户不知道怎么解锁）
run_one test_yt_pot_starved.py
run_one test_youtube_hls_prefer.py         # YouTube 无 POT ⇒ ≤1080p 优先 HLS 混合流（2026-10-19 用户「下载速度慢」）：DASH 无 POT 被 googlevideo 按连接限速（16 连接聚合 ~600KB/s + 硬阻断超时），HLS 单连接 1.5MB/s；含 _pot_usable 代理感知判定 + VDL_YT_JS_RUNTIME=off 开关
run_one test_yt_fallback_cap.py            # 403 降级链不许超过用户选的清晰度（2026-10-21：选 1080P 显示 398MB、实际下到 641MiB 的 1440P AV1，末环 bv*+ba 无 height 上限）
run_one test_bundle_binaries.py            # 发布包必备二进制闸门（2026-10-21：deno/ffmpeg/ffprobe 缺任一项 ⇒ 拦发布；判据含 --version 冒烟执行，不许用体积阈值——真包 ffmpeg 是 434KB 共享链接版，体积卡会假拦）
run_one test_quality_exact.py              # 「选什么清晰度就下什么清晰度」：下载/降级/HLS 三处选择器锁在 [n±3%] 窄区间（上下界成对）、分档按短边（竖屏 1080x1920 不能整档消失）、自动档保留 <=1080
run_one test_downloader_url_parsing.py
run_one test_matting_core.py
run_one test_dewatermark_core.py
run_one test_idphoto.py            # 证件照（2026-10-10 竞品对标水印云）：合成构图 / 底色 / 排版 + 前端接线 + 路由挂载
run_one test_matting_forward.py
run_one test_dewatermark_forward.py
run_one test_dewatermark_diffusion.py
#   4c. test_dewatermark_routes.py     —— 去水印路由层（扩散档）：capability 暴露 / diffusion 503 / engine·model 白名单 400
run_one test_dewatermark_routes.py
#   3c. test_matting_engines_forward.py —— 其余本地 ONNX 引擎前向（modnet/isnet/birefnet-matting/portrait）
#   3d. test_matting_chroma_key.py      —— chroma-key 纯算法前向（背景透明/主体不透明/选区硬边界）
#   3e. test_matting_sam_forward.py     —— sam-matting 前向（假 _sam_session 注入 enc/dec）
run_one test_matting_engines_forward.py
run_one test_matting_chroma_key.py
run_one test_matting_sam_forward.py
run_one test_convert_guards.py
run_one test_commentary_routes.py
#   6a. test_commentary_range_trim.py —— 解说「正剧范围 → 实际输入」裁剪判据（2026-09-20 用户实测）：
#                                        「正剧开始」留空 + 「片尾开始」15 分钟 → 成片仍是 46 分钟整片
#                                        （ts=0 被 `0.0 < ts < te` 静默漏掉）。含 fold 与裁剪判据必须一致
#                                        的不变量 + 「调用方不许再抄一份判据」的静态守卫
run_one test_commentary_range_trim.py
#   6b. test_clone_env.py             —— 本地语音克隆「按需安装」环境判定（候选优先级 / 半成品驳回 /
#                                        磁盘不足拦截 / 非 Apple Silicon 拒绝 / 全程不得改进程级 HF_ENDPOINT）
run_one test_clone_env.py
run_one test_codec_utils.py
run_one test_convert_pipeline.py
run_one test_ffmpeg_tools.py
run_one test_compress.py
run_one test_desktop_bridge.py
run_one test_desktop_copy_wiring.py            # 桌面壳「复制链接」：原生剪贴板桥优先 + 失败不静默（2026-10-01）
run_one test_clip_autodetect_wiring.py         # 剪贴板自动识别一键下载：原生桥读取 + 提示条接线（2026-10-02 用户「复制链接后粘贴下载」）
run_one test_sniffer_ext_ping.py            # 嗅探「扩展心跳」三态：未连/在线/过期离线（2026-10-01 面板简化配套）
run_one test_sniffer_ext_push.py            # 嗅探「扩展自动直推」：入库/去重/kind_hint/脏数据（2026-10-01 自动嗅探配套）
run_one test_ext_version_cmp.py             # 扩展版本比对守卫：只在「内置比已装新」时提示升级（2026-10-01 防降级）
run_one test_sniffer_quality_wiring.py      # 嗅探链路清晰度透传：后端白名单 + 桌面端用它 + 两端都有下拉（2026-10-01 用户「没法选择分辨率」）
run_one test_sniffer_badge_hide_wiring.py   # 嗅探入口徽标**常显**（2026-10-23 用户「嗅探是隐藏了吗」；此前扩展在线即自动收起，今改为一律显示，条件隐藏不得加回）
run_one test_extension_autoupdate_wiring.py # 扩展零点击自动更新：目录识别/安全同步（拒写他人目录·只增不删）+ 心跳回传 reload_to + 扩展自重载（2026-10-02 用户「扩展程序更新怎么办」）
run_one test_download_wiring.py
#  11b. test_login_gate_placement.py —— 登录门禁「挂载位置」守卫（2026-09-27 用户实测：
#                                       点「解析链接」就弹登录框 = 门禁挂错层）。
#                                       锁住「前置步骤（解析/选文件）不弹，执行动作才弹」，
#                                       并要求门禁表 id 在 index.html 真实存在、后端名单同步、
#                                       提示文案不被 openAuthModal 清空
run_one test_login_gate_placement.py
#  11c. test_page_link_wiring.py —— 「生成网页 → 在线链接」接线守卫（2026-09-30 用户实测：
#                                       「为什么别人打不开」＝ 结果区只给本机路径，用户把它当分享链接发出去）。
#                                       锁住两端：分享节点必须把 .html 分享渲染成页面原件；
#                                       前端必须给在线链接 + 二维码，且上传请求带登录 token。
run_one test_page_link_wiring.py
#  11d. test_home_entries.py —— 首页「热门功能快捷入口」接线守卫（2026-09-30「快捷入口继续完善」）：
#                              首页 12 张卡片改 5 组 17 张，补齐生成二维码/生成网页/音乐转换/
#                              图片转换/视频音频桥接。守卫钉住「卡片 data-view 必须是侧栏真实视图名」
#                              （拼错=点了没反应）与「侧栏主功能必须都在首页有入口」（防加了功能忘了首页）。
run_one test_home_entries.py
#  11e. test_update_notes_version.py —— 「关于」页更新内容的版本对齐守卫（2026-10-07 用户实测反馈）：
#                               本机 1.0.43 却显示「v1.0.39 更新内容」。根因是前端兜底判据写成
#                               「版本号不相等」（源比本机旧时把旧版本当新版本），且
#                               `_cachedUpdateNotes()` 是死代码。守卫钉住：判定必须比大小
#                               （`_verNewer`）、标题版本与内容必须同源、找不到条目就隐藏。
run_one test_update_notes_version.py
run_one test_sr.py
run_one test_selfupdate.py
#  11f. test_win_inapp_update.py —— Windows 应用内更新链路（2026-10-16）：
#                               此前一刀切禁用 Windows 更新（PLATFORM != macos 即拦），
#                               根因是清单 url 指向 macOS 的 .app.zip。现 Windows 走独立
#                               清单 latest-win.json + Inno 静默安装。守卫钉住：清单按平台
#                               分流（防拿到 mac 包）、验签同一条 fail-closed 通道、安装链路
#                               fail-closed（sha 不符/缺字段绝不启动且删下载物）、
#                               静默安装参数齐全且脱离父进程、三条源码棘轮。
run_one test_win_inapp_update.py
run_one test_llm_local_priority.py
run_one test_quota.py
#  任务落盘持久化（2026-10-02 B2）：重启后未完成任务恢复为可续传、completed 只在
#  成品文件仍在时恢复、remove 同步删状态。此前任务表纯内存，重启全丢。
run_one test_task_persistence.py
#  下载预检与 PO Token 会话保持（2026-10-02 B1/A1）：体积估算/上限 20GB/磁盘预检、
#  YouTube 403 降级链必须带回 visitor_data（丢会话=照样 403）。
run_one test_download_preflight.py
#  下载失败矩阵重试与 aria2c 默认化（2026-10-02 A2/B4）：可重试失败换策略重试
#  （切换下载器→切 HLS 链路，最多额外 2 次）、auto 默认启用 aria2c、原生路径
#  http_chunk_size 分块并行、硬上限到点仍在推进则延长等待（B3）。
run_one test_download_matrix.py
#  可行动化报错 / YouTube 代理分流 / 完成任务历史（2026-10-02 A3/A4/B5）：
#  停滞与硬超时不再伪装成「用户取消」；VDL_PROXY_YT/proxy.json 只分流 YouTube 域；
#  到期完成任务降级为历史条目（保留 50 条元数据、可重新下载）。
run_one test_batch3_history.py
#  积分口径一致性 + 停滞看门狗终态（2026-10-11 全功能实测发现）：
#  F1 给非活跃账号充 AI 池 → 「账面显示 N、扣费报当前 0」（拒绝 + 不同步云端）；
#  F2 看门狗停滞只写步骤不落终态 → 任务僵在 downloading 最长 2h、DELETE 清不掉
#     （抽 _finalize_terminated，主等待循环短切片 join + 取消即中断）；
#  F3 设备锁定时 credits_total 未随 AI 池置零重算。
run_one test_credit_and_stall_consistency.py
run_one test_proxy_settings.py
#  1/3/7 天下载会员档 + 套餐价格单一真源（2026-09-30）：超管在后台改价/改天数后，
#  展示（/api/member/plans 真身 store.plans()）/ 下单金额 / 发放天数三处必须同步。
#  此前下单读硬编码 PAY_PLANS、发货读硬编码 DOWNLOAD_PLANS —— 改了等于没改。
run_one test_member_plans_source.py
#  会员 store 决议隔离（2026-10-04，Fix C）：
#  current_member_store 回退全局账本前必须校验归属（gstore.meta.account.email ==
#  当前账号注册邮箱），否则会读成上一个登录账号的会员/积分（跨账号泄漏，真实事故：
#  桌面显示 400/2030 实为上一账号 15014313254 的全局账本）。钉死「旁观账号看不到
#  他人权益」+「同账号兜底仍生效」+「_email_of 映射正确」+ 变异确认（去掉归属校验
#  立即复现泄漏，证明测试能抓住回退）。
run_one test_member_store_isolation.py
run_one test_llm_config_save.py
run_one test_vision_managed_config.py
run_one test_gateway_config.py
run_one test_quota_refund.py
# 跨端云端额度守卫（per-resource 记账 / 中心权威 / fail-open / Keychain 影子 /
# 旧数据迁移 / daily_auto per-resource）—— 🔴 此前**漏登记**，17 项从未进过
# 构建自验证，等于这批财务守卫只在手动单跑时生效。
run_one test_quota_cross_end.py
# 🔴 2026-10-19：这两行原来写在这里，但 `run_pytest_root` 到第 1099 行才定义 ⇒ 执行到
#    这里只会打印 "command not found"，**两个用例从未真正跑过**（等于漏登记）。
#    它们是纯 pytest 写法（无 __main__），用 run_one 当脚本跑会「零断言恒通过」，
#    所以只能在函数定义之后用 run_pytest_root 登记 —— 见文件末尾。
# test_license_base.py / test_membership_device.py → 见末尾 run_pytest_root 段
run_one test_pagetool.py
run_one test_commentary_output_naming.py
run_one test_commentary_precheck.py
run_one test_subtitle_band.py
run_one test_llm_truncation_retry.py
#  12. test_engine_isolation.py      —— 跨功能「共享工具」隔离性：抠图/去水印/扩散 各自独立
#                                       session 缓存与模型全局；VDL_MODELS_DIR 下模型目录必须一致
#                                       （同模型不下载两份）；字幕不得在请求期污染进程级 HF 端点
run_one test_engine_isolation.py
#  13. test_users_store_concurrency.py —— 账号表并发写安全：所有写入口必须整段持锁
#                                        （注册/改密/注销/提权/头像/后台禁用/后台重置/超管引导），
#                                        并发注册不丢号、同名只成功一次、跨进程锁生效、
#                                        写盘用唯一临时名（固定名会写出半截 JSON）
run_one test_users_store_concurrency.py
#  14. test_config_atomic_write.py  —— 配置/状态文件的「原子落盘 + 读改写串行」：
#                                       固定名临时文件并发下会互相截断（有可复现反证）、
#                                       quota.json 跨进程读写会「额度复原」；
#                                       含全仓 AST 棘轮（不得再出现固定名 .tmp）
run_one test_config_atomic_write.py
#  15. test_auth_reset_code_leak.py  —— 🔴 账号接管防护：dev 模式不得把验证码回传给公网调用方
#                                        （缺 smtp.json 时生产会自动落到 dev，原实现＝公网可改任意账号密码）；
#                                        dev+本机仍回传（桌面调试不退化）、dev+公网为 None、
#                                        拿不到码即改不动密码、未知账号不回传、smtp 不回传
run_one test_auth_reset_code_leak.py
#  15b. test_update_manifest_signature.py —— 🔴 P0-1 自更新清单 Ed25519 验签守卫：
#                                        篡改清单被拒 / 错密钥被拒 / 缺签名被拒 / 未配置公钥 fail-closed /
#                                        验签通过才放行。此前未登记 ⇒ P0-1 无回归网，必须接进门禁。
run_one test_update_manifest_signature.py
#  16. test_voice_sample_record.py —— 「我的音色 · 直接录制」落盘守卫：
#                                      WAV 头时长解析、空/过大/格式/太短/过长 五类拒绝、
#                                      非 WAV 回落前端报的时长、文件 0600 目录 0700、
#                                      只保留最近 5 段且不碰配置目录里的其它文件
run_one test_voice_sample_record.py
#  voice_studio F-05 守卫：TTS/转写 计费按用户归属（current_member_store(request)）
#  + tts/transcribe 跨源校验（拒绝外部站点）—— 防止写死全局账本/被跨源调用
run_one test_voice_studio_hardening_f05.py
#  17. test_commentary_style_intensity.py —— 解说「风格强度」链路（2026-09-21 新增）：
#                                        CommentaryRequest.style_intensity 字段必须存在（漏加则
#                                        router 的 payload.style_intensity 在 JSON 主路径 AttributeError，
#                                        pyflakes 查不到这类字段缺失）；CLI 按强度产出
#                                        `--style <s> --style-intensity <n>`；★AST 棘轮：
#                                        routers 里每处 payload.X 都必须是该函数绑定模型里的真实字段
run_one test_commentary_style_intensity.py
#  18. test_share_history.py        —— 扫码分享「我的分享 / 删除 / 有效期」（2026-09-21 新增）：
#                                        历史落盘（去重/新在前/上限裁剪/坏文件容错）、
#                                        删除端点**只允许删本机历史里的 sid**（否则成任意删除代理）、
#                                        节点删失败必须保留记录、通道根推导（direct 只能上传，
#                                        删除/探活须用源站根）、上传须透传 X-Expire 且成功后落历史
run_one test_share_history.py
#  18b. test_share_quota_keys.py    —— 分享两子功能并入账号级每日配额（2026-10-20 新增）：
#                                        src 分派（pagegen→share_page / 其余→share_qr）、
#                                        闸门放行/429/fail-open、**路径校验之后才计数**
#                                        （第一版曾把闸门放在校验之前，与自己的注释相反）、
#                                        会员中心功能表必须出现「生成二维码/生成网页」两行
run_one test_share_quota_keys.py
#  19. test_login_error_message.py  —— 登录提示不得张冠李戴 + 手机号账号的云端建号（2026-09-22 新增）：
#                                      本机账号表决定「账号是否存在」（本机区分 BAD_PASSWORD /
#                                      NO_ACCOUNT，公网仍模糊防枚举）、手机号老账号自愈只在
#                                      本机密码正确时补建、云端的账号主键口径与 App 一致
#                                      ⚠️ 之前漏挂在清单里 —— 加了新测试文件必须在这里登记，
#                                     否则构建门禁根本不跑它（只写文件＝没门禁）
run_one test_login_error_message.py
#  20. test_cloud_account.py        —— 账号制客户端（2026-09-22）：
#                                      同一笔云端购买不得重复落户（幂等）、登出/被挤掉不清已购权益
run_one test_cloud_account.py
#  21. test_password_sync.py        —— 🔴 两端账号库的密码一致性（2026-09-22 新增）：
#                                      账号其实是两套库（本机 auth_store 管登录与门禁 / 云端授权中心
#                                      管会员与设备位），各存一份密码哈希 —— 任何一侧单独改密都会
#                                      分叉成「这台能登、换台说密码错」。锁死三条收敛路径：
#                                      ①改密/重置后推云端（fail-open，云端失败不回滚本机）
#                                      ②云端本无此账号不算失败 ③下次登录时自愈（须本机密码对
#                                      **且**持该账号的云端 token，防越权改他人密码）
run_one test_password_sync.py
#  22a. test_cloud_account_unified.py —— 全端账号统一（2026-10-16 用户「每个端数据要都
#                                      统一到一起」：Mac/Windows 桌面 + 网页版 cn/hk 四端
#                                      账号/权益/设备/is_admin 收敛到云端授权中心，超管后台
#                                      可见全部端）。钉住：①is_admin 上云（快照/列表带出、
#                                      set-admin 写了就生效）②存量搬迁 import-account 哈希
#                                      可移植（原密码直接能登）且不覆盖云端已有账号 ③超管
#                                      admin_token 改密通道（持令可改/错令拒/原密码通道回归）
#                                      ④list_users 云端为主+本机兜底+重叠去重+云端故障退化
#                                      ⑤路由双写 helper（标识解析/失败只出 notice/空标识不调云端）
run_one test_cloud_account_unified.py
#  22c. test_user_data_cloud.py      —— 用户信息类数据云备份（2026-10-16 用户「用户数据
#                                      应该放云端吧，万一本机数据丢失呢」）。口径：**用户
#                                      信息类**（设置/收藏/历史 + 平台登录态 Cookie）上云，
#                                      **不含功能产物**（成片/图片/解说）。钉住：①账号隔离
#                                      ②资料包与面板元数据都不含登录态明文 ③换机还原逐字节
#                                      一致 ④无密钥只同步设置、不抹云端备份 ⑤错密码拒绝上传
#                                      （云端逐字节不变 + 不污染本机密钥缓存）⑥内容未变不推
#                                      ⑦本机路径字段不进包 ⑧畸形文件名不落盘
run_one test_user_data_cloud.py
#  22b. test_system_changelog.py     —— 「关于本应用」的内置更新日志（2026-09-26 新增）：
#                                      不再依赖线上更新源（线上长期停在旧版会让面板只显示
#                                      旧版说明）：结构与版本降序、仓库 VERSION 必须在日志里
#                                      命中（漏写则退回兜底文案）、未命中时优雅降级、
#                                      端点免登录可用（未登录也要能看更新内容）
run_one test_system_changelog.py
#  23. test_youtube_player_client.py —— YouTube player_client 选择（2026-09-26 写成，2026-09-27 补登记）：
#                                      不得再给 YouTube 强制 player_client（SABR 后全部失效）、
#                                      下载兜底链首位必须是 "(default)"、
#                                      带 Cookie 失败要能剥 Cookie 裸重试
#                                      ⚠️ 之前漏挂在清单里 —— 只写文件＝没门禁
run_one test_youtube_player_client.py
#  24. test_youtube_cookie_sources.py —— YouTube Cookie 源顺序 + 登录态体检（2026-09-27 新增，
#                                       对标竞品 DataTool 的会话自持机制）：
#                                      ①登录态判据只认 SID/__Secure-*PSID/LOGIN_INFO；
#                                      ②自动源缺登录态字段即跳过、显式源(user/env)永不跳过；
#                                      ③候选顺序必须是 user > env > **browser** > cache > pool
#                                       （实时解密浏览器必须早于 30 天 TTL 的缓存：Google 的
#                                       SIDCC/__Secure-*PSIDTS 是滚动的，快照落后即被判未登录，
#                                       而「有缓存」会永远挡在新鲜值前面 → 死循环）
run_one test_youtube_cookie_sources.py
#  25. test_youtube_js_challenge.py —— YouTube JS 挑战（nsig）求解链 + 打包依赖（2026-09-27 新增，
#                                     对标竞品 DataTool 的第二半机制：自带 deno + yt-dlp-ejs）：
#                                     ①_find_js_runtime_binary 按 VDL_JS_RUNTIME > _MEIPASS/bin >
#                                       venv/bin > sysconfig scripts > PATH 顺序探测 deno/node/bun/quickjs；
#                                     ②_js_challenge_options 只下发 yt_dlp.globals 真正支持的 runtime
#                                       （不支持的绝不硬塞，否则 options 校验直接抛错）；
#                                     ③_base_options 只给 YouTube 挂 js_runtimes/remote_components，
#                                       其它站点（B 站等）行为不得被改变；
#                                     ④requirements 必须含 yt-dlp-ejs + deno，两个 build 脚本必须把
#                                       deno 显式 --add-binary 进包（venv 脚本目录不会自动分发）
run_one test_youtube_js_challenge.py

#  26. test_direct_url_hls.py —— m3u8 不得被当作「可直接下载的文件」（2026-09-27 修复）：
#                                 `.m3u8` 曾在 _DIRECT_EXT_RE 白名单里，导致 `probe()` 在
#                                 `_looks_like_direct_file()` 处短路跳过 yt-dlp，`direct_url`
#                                 直接等于那个清单地址 → 前端把播放列表当文件存下来，
#                                 用户拿到几百字节的废文本。现在清单一律走 HLS 合成路径；
#                                 单个 `.ts` 仍是可直取的完整 MPEG-TS，不得误伤。
run_one test_direct_url_hls.py

#  27. test_sniffer_extension_ingest.py —— MV3 扩展回传链路（2026-09-27，★12）：
#      /api/sniffer/send 接受 cookie/source（extension 标记）、picked 出队含全字段、
#      items 展示区不受污染、cookie 截断 8192（对齐 DownloadRequest.max_length）、
#      缺 url 400、缺省回落 manual（悬浮球兜底语义不回归）
run_one test_sniffer_extension_ingest.py

#  28. test_upload_endpoint_same_origin.py —— 上传端点必须同源（2026-09-29，★13）：
#      UC_UPLOAD_ENDPOINTS 只许 location.origin。曾有的第二项（已停服的 Railway 云上
#      备份）会让每个奇数下标分片先撞死主机；非同源端点还会把分片劈到两份存储上，
#      finish 必报「分片不完整」。同时钉住「非 JSON 的 413 = 网关拦截」的区分文案。
run_one test_upload_endpoint_same_origin.py

#  29. test_payment_core.py —— 支付内核（2026-09-30）：套餐价表 / 下单返回 PENDING+二维码 /
#      未付款查询仍 PENDING 且不发放权益 / simulate_paid 翻转 PAID 且 grant 回调收到正确
#      plan_code / 未知套餐拒绝 / 真实通道未接入时 NotImplementedError / 订单可持久化复查。
run_one test_payment_core.py

#  30. test_payment_router.py —— 支付 REST 层（2026-09-30，★仓库健康）：app.py 无条件
#      include routers/payment.py，故该文件与四个 /api/cloud/pay/* 路由必须在位，
#      否则全新 clone 直接 ImportError 起不来。同时钉死订单目录走 _base_dir()
#      （~/.video-downloader + VDL_DATA_DIR 隔离），不得回落 ~/.videodownloader
#      （无短横线，是 cookie/cloud_sync 的历史目录，写错不报错但会污染家目录）。
run_one test_payment_router.py

#  30b. test_pay_order_mode.py —— VPS 支付服务下单产品分流（2026-10-08）：本应用
#       无当面付资质，_place_order 必须 auto 先试当面付、权限不足自动回退
#       page/wap，全败抛错（绝不静默返回空二维码让用户扫不出来）。deploy/ 此前
#       零测试 → 与 test_static_undefined_names.py 互补，构成 deploy/ 回归网。
run_one test_pay_order_mode.py

#  30c. test_payment_xunhu.py —— 虎皮椒（xunhupay）聚合通道（2026-10-09）：签名必须按
#       官方 PHP `generate_xh_hash` 取**原值**（不 urlencode）、空值/hash 不参与；
#       下单必须带官方必填 `time` + `nonce_str`（缺则网关报「缺少参数appid,time,hash
#       或他们的值不合法」）；`url_qrcode` 要还原成 `weixin://` 原生码、取不到时
#       回落收银台 URL 而**不得**返回空码。
#       🔴 本文件此前**从未登记**（等于零执行），且断言写的是「urlencode 版」错误口径 ——
#       这就是两个缺陷溜进线上的原因。改 `_xunhu_sign` / `_place_xunhu` 后必须跑它。
run_one test_payment_xunhu.py
# 2026-10-09「点购买没反应」回归守卫：本机网关 /api/pay/* 必须带**云端令牌**转发 VPS，
# 且前端 PAY_API_BASE 默认不得退回直连公网（直连 = 无令牌 = 必然 401）。
run_one test_pay_gateway.py
# 2026-10-09 价格同源守卫：支付服务 deploy/pay_server.py 的「有效价/可购性」必须与
# App 侧 membership.plan_sales_state 逐条一致（含秒杀窗口、下架、活动窗口、售罄）——
# 防「前端展示价 ≠ 实收价」再次分叉（曾发生 3 天档展示 ¥2.99 / 实收 ¥4.90）。
run_one test_pay_price_source.py

#  31. test_credential_store.py —— 账号 token 凭据存储（2026-09-30，P2 加固）：
#      token 不再以明文落在 ~/.video-downloader/membership*.json（拷走文件即可
#      冒充用户调云端）。钉死三点：Keychain 存取往返、磁盘 JSON 无明文但内存态
#      仍有 token（否则 6 处 acc.get("token") 会集体掉登录）、重新加载能注回。
#      测试用一次性 Keychain 账号并在结尾清理；数据目录走 VDL_DATA_DIR 隔离。
run_one test_credential_store.py

#  32. test_engine_idle.py —— 引擎空闲自动卸载（2026-09-30，方案 A）：
#      空闲超时默认 180s 释放模型权重；开关关闭时不释放；释放后能重建；
#      配置走 VDL_DATA_DIR 隔离（绝不写用户家目录）。
run_one test_engine_idle.py

#  33. test_support_image.py —— 客服「发图片 + 搜历史消息」（2026-10-03）：
#      图片解码白名单/大小上限/魔数校验、附件路径防穿越、历史消息搜索
#      （超管搜全部 / 用户只搜自己的、按时间倒序、带定位下标）。
#      附件必须落 VDL_DATA_DIR（绝不写 Downloads —— TCC 会重新索要授权并永久阻塞）。
run_one test_support_image.py

echo ""
echo "=== extension/tests/test_sniff_core.js（MV3 扩展判定核心，node） ==="
NODE_BIN=""
for cand in /Users/suixindelang/.workbuddy/binaries/node/versions/22.22.2-3/bin/node "$(command -v node 2>/dev/null)"; do
  if [ -n "$cand" ] && [ -x "$cand" ]; then NODE_BIN="$cand"; break; fi
done
if [ -n "$NODE_BIN" ]; then
  if "$NODE_BIN" "$REPO/extension/tests/test_sniff_core.js"; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1)); FAILED_LIST="$FAILED_LIST $f"
  fi
else
  echo "⚠️ 跳过（无 node）"
fi

echo ""
echo "=== extension/tests/test_background_page_scope.js（扩展「只保存当前页」，node + chrome 桩） ==="
if [ -n "$NODE_BIN" ]; then
  if "$NODE_BIN" "$REPO/extension/tests/test_background_page_scope.js"; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1)); FAILED_LIST="$FAILED_LIST $f"
  fi
else
  echo "⚠️ 跳过（无 node）"
fi

echo ""
echo "=== extension/tests/test_pagewatch.js（页面侧哨兵：SPA 换页不丢事件，node + chrome 桩） ==="
# 2026-10-01 补登记：该文件随扩展 1.0.40 一起写就，但当时漏挂进运行器（只写文件＝没门禁）。
if [ -n "$NODE_BIN" ]; then
  if "$NODE_BIN" "$REPO/extension/tests/test_pagewatch.js"; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1)); FAILED_LIST="$FAILED_LIST $f"
  fi
else
  echo "⚠️ 跳过（无 node）"
fi

echo ""
echo "=== extension/tests/test_popup_video_page.js（视频页空状态必须有「解析并下载/复制链接」，node） ==="
# 2026-10-01 用户反馈「这里也要可以操作」：钉住 popup 空状态的两个操作按钮 + 复制失败不静默。
if [ -n "$NODE_BIN" ]; then
  if "$NODE_BIN" "$REPO/extension/tests/test_popup_video_page.js"; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1)); FAILED_LIST="$FAILED_LIST $f"
  fi
else
  echo "⚠️ 跳过（无 node）"
fi

echo ""
echo "=== extension/tests/test_ext_autoreload.js（零点击自动更新：只在真更新时自重载，防死循环/防降级，node + chrome 桩） ==="
# 2026-10-02 用户问「扩展程序更新怎么办」→ 心跳回传 reload_to + 扩展 chrome.runtime.reload()。
# 三个致命分支都在「不该动」的一侧（同版本/旧版本/同目标重复），故用行为测试而非源码断言。
if [ -n "$NODE_BIN" ]; then
  if "$NODE_BIN" "$REPO/extension/tests/test_ext_autoreload.js"; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1)); FAILED_LIST="$FAILED_LIST $f"
  fi
else
  echo "⚠️ 跳过（无 node）"
fi

echo ""
echo "=== extension/tests/test_popup_quality_state.js（清晰度「回显 = 实际发送」，node + DOM/chrome 桩） ==="
# 2026-10-02 用户截图：下拉选的 2K 1440P，回显却写「最佳画质（自动）」，实际也按 best 下载。
# 根因是 render(st) 的 `state = st` 抹掉了 quality（快照里没有该字段）。要害全在「两次渲染
# 之间」，源码断言 grep 不出来，故写成行为测试：改选后必须三处一致，且再渲染一次不许丢。
if [ -n "$NODE_BIN" ]; then
  if "$NODE_BIN" "$REPO/extension/tests/test_popup_quality_state.js"; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1)); FAILED_LIST="$FAILED_LIST $f"
  fi
else
  echo "⚠️ 跳过（无 node）"
fi

echo ""
echo "=== extension/tests/test_cookie_harvest.js（登录态采集：拼头/登录判据/推送契约，node） ==="
# 2026-10-18：扩展新增「登录态同步」——按站用 chrome.cookies 拉全量登录态，交给本机
# App（私密）或云端公共池（共享）。要害都在「拼出来的 Cookie 头能不能直接用」：
# 同名重复/过期/空值任何一条没滤掉，服务端验真就 400，用户看到的是「同步失败」
# 而不是「Cookie 无效」；登录判据若用 cna/buvid3 这类游客字段，会把没登录的站
# 显示成已登录，点同步必被拒。故写成行为测试（chrome.cookies 用桩）。
if [ -n "$NODE_BIN" ]; then
  if "$NODE_BIN" "$REPO/extension/tests/test_cookie_harvest.js"; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1)); FAILED_LIST="$FAILED_LIST $f"
  fi
else
  echo "⚠️ 跳过（无 node）"
fi

echo ""
echo "=== extension/tests/test_cookie_zone.js（登录态面板：按钮闸门/白名单/缺权限文案，node + DOM 桩） ==="
# 三个真实用户会踩的点：① 扫到已登录却按钮仍灰（以为坏了）/ 没勾同意就能贡献到云端
# （共享语义被绕过）；② 微博/爱奇艺不在云端白名单，不能让用户点了吃 400；
# ③ manifest 加 cookies 权限后 Chrome 会停用扩展，文案必须指向「重新启用」，
# 而不是显示成「没有已登录的平台」（那会逼用户白重登一遍）。
if [ -n "$NODE_BIN" ]; then
  if "$NODE_BIN" "$REPO/extension/tests/test_cookie_zone.js"; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1)); FAILED_LIST="$FAILED_LIST $f"
  fi
else
  echo "⚠️ 跳过（无 node）"
fi

echo ""
echo "=== extension/tests/test_cookie_autopush.js（默认自动共享：默认勾选/30 分钟节流/取消即停，node + DOM 桩） ==="
# 产品策略＝默认自动推云端公共池（与桌面 App 每 30 分钟自动推一致）。三条要害：
# ① popup.html 的 ckAgree 必须默认带 checked（否则「默认共享」名不副实）；
# ② 30 分钟节流（popup 每次打开都是新实例，不节流会每次开面板都重推，撞云端每域限流）；
# ③ 用户取消勾选后既不推、也不写「上次推送时间」（写了会白压 30 分钟，用户以为坏了）。
if [ -n "$NODE_BIN" ]; then
  if "$NODE_BIN" "$REPO/extension/tests/test_cookie_autopush.js"; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1)); FAILED_LIST="$FAILED_LIST $f"
  fi
else
  echo "⚠️ 跳过（无 node）"
fi

echo ""
echo "=== extension/tests/test_page_title_lag.js（嗅探标题滞后：SPA 换页「地址先到、标题后到」必须补推，node + chrome 桩） ==="
# 2026-10-02 用户实测「app上嗅探到的比视频慢一步，最新嗅探到的是上一个视频，
# 想要看的当前视频的嗅探结果得打开另一个视频」。根因：SPA 换页那一刻 Chrome 只给得到
# changeInfo.url，tab.title 还是**上一页**的；只推那一拍 → 条目带旧标题，且同页 5 分钟
# 冷却把重推挡死。要害全在「两拍之间的状态」，源码 grep 不出来，故跑真实 background.js
# 做行为测试（服务端配套见 server/tests/test_sniffer_ext_push.py 的标题覆盖用例）。
if [ -n "$NODE_BIN" ]; then
  if "$NODE_BIN" "$REPO/extension/tests/test_page_title_lag.js"; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1)); FAILED_LIST="$FAILED_LIST $f"
  fi
else
  echo "⚠️ 跳过（无 node）"
fi

echo ""
echo "=== extension/tests/test_sniff_meta.js（嗅探条目元信息：标题/时长/大小，node + chrome 桩） ==="
# 2026-10-02 用户「这里把嗅探到的视频信息也加上，比如：标题、时长、大小」。
# 三样来源各不相同：标题=tab.title；时长=页面侧 <video>.duration（只有 pagewatch.js 读得到，
# SABR 站后台既抓不到媒体流也没 Content-Duration 头）；大小=响应头 Content-Length
# （sniff-core.pickHeaders 早已解析，但必须**传进 store.add**、再**带进推送载荷**才有用）。
# 要害在「时长是后到值」：refreshPageTitle 的去重口径必须连 duration 一起比，
# 否则「标题同、时长刚拿到」的补推会被当成重复丢掉 —— 列表里的时长永远空着。
if [ -n "$NODE_BIN" ]; then
  if "$NODE_BIN" "$REPO/extension/tests/test_sniff_meta.js"; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1)); FAILED_LIST="$FAILED_LIST $f"
  fi
else
  echo "⚠️ 跳过（无 node）"
fi

echo ""
# 两个**用户实测事故**的回归守卫（此前未登记，且沙箱符号腐化导致长期失效，
# 2026-10-06 修：产品给上传/轮询请求加 Authorization 头时用 authBearerToken()，
# 沙箱未提供 → eval 直接 ReferenceError，断言"轮询 0 次"长期假红/假绿）
echo "=== server/tests/test_concat_flow.mjs（拼接面板状态机：防重复结果/停表/清源片段） ==="
if "$NODE_BIN" "$SERVER/tests/test_concat_flow.mjs"; then
  PASS=$((PASS+1))
else
  FAIL=$((FAIL+1))
fi

echo "=== server/tests/test_convert_poll_lifecycle.mjs（音乐转换轮询不被上传期停表） ==="
if "$NODE_BIN" "$SERVER/tests/test_convert_poll_lifecycle.mjs"; then
  PASS=$((PASS+1))
else
  FAIL=$((FAIL+1))
fi

echo "=== server/tests/test_platform_count_label.mjs（平台数对外口径：两端统一 116→100+） ==="
if [ -n "$NODE_BIN" ]; then
  if "$NODE_BIN" "$SERVER/tests/test_platform_count_label.mjs"; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1)); FAILED_LIST="$FAILED_LIST $f"
  fi
else
  echo "⚠️ 跳过（无 node）"
fi

# 🔴 2026-10-05 修正：这里原本是「汇总 + 失败即 exit 1」，导致**后面的守卫全都没跑过**
#    （34~40 号 + 我新加的 41/42 号注册在后面，永远执行不到；表现为「登记了但日志里
#    找不到该用例」）。改为：中途只快照计数，继续往下跑，末尾统一汇总。
MID_PASS=$PASS
MID_FAIL=$FAIL
echo "── 前半段完成：通过 ${MID_PASS:-0} / 失败 ${MID_FAIL:-0}，继续跑后半段 ──"

#  34. test_membership_benefits.py —— 下载会员权益与配额表一致性（2026-10-03）：
#      权益文案改为从 DAILY_QUOTA_LIMITS + FEATURE_USAGE_DEFS 自动生成，
#      守卫钉住「有配额必有文案」+「下载会员不送积分」，以后加配额不会再漏文案。
run_one test_membership_benefits.py

#  35. test_plan_sales_state.py —— 档位售卖状态（2026-10-03）：
#      模式（普通/秒杀/限量/活动）、秒杀价与窗口、限量份数、上下架与活动时间；
#      激活入口强制校验（售罄/未开始/已结束/下架拒发），限量档发放后 sold 自动 +1。
run_one test_plan_sales_state.py

#  36. test_admin_user_usage.py —— 后台「用户使用详情」聚合（2026-10-03）：
#      _usage_bundle 聚合结构（按天倒序/只留>0/激活历史倒序）、store 读失败降级不崩、
#      路由挂载与 require_admin 门禁。daily_usage 里的 date 字段非数值，必须跳过。
run_one test_admin_user_usage.py

# 2026-10-22 权限拆两级（超管唯一 / 管理员只读）：
#   test_admin_roles.py —— 角色判定：名单优先、名单为空兜底最早注册的 is_admin、
#     名单配多个只认第一个（超管唯一性硬保证）、非 admin 角色为空、未知 uid 不崩。
#   test_admin_roles_api.py —— 闸门是否真装上：管理员调写接口 403（不是 401）、
#     超管放行、普通用户 401；并静态扫描 16 个写接口全部 require_super、
#     11 个只读接口仍 require_admin（管理员要能看），漏一个即后门。
#   test_admin_roles_e2e.py —— 真起 FastAPI 应用 + 真登录拿 token：
#     管理员 GET 200 / POST 403（禁用·提权·改 SMTP·重置密码·删 Cookie）、
#     普通用户 401、超管写权限完好、超管不可被取消。
run_one test_admin_roles.py
run_one test_admin_roles_api.py
run_one test_admin_roles_e2e.py
run_one test_admin_promote_effect.py   # 提权「点了没反应」的根因：手机号账号的标识解析必须命中（否则云端同步被跳过 + 云端权威回写把本机打回）＋权限文案两级口径（2026-10-22）

#  37. test_plan_mkt_matrix.py —— 营销面板「模式×分组」联动（2026-10-03）：
#      直接解析 web/app.js 的 PLAN_MODES / _MKT_BY_MODE / data-mkt 三处命名做交叉核对，
#      并模拟 4 模式 × 3 分组显隐矩阵。防止「模式 value 与分组标识不同名」
#      （limited vs stock）导致选了该模式却什么都不显示。
run_one test_plan_mkt_matrix.py

#  38. test_member_countdown.py —— 会员卡秒杀/活动倒计时（2026-10-03）：
#      用户「秒杀卡片上做个倒计时」。四层核对：后端 state 字段齐 → 前端模板/选择器/
#      定时器生命周期接线 → _fmtCountdown 真实行为（抽出来交给 node 跑）→
#      回归钉 _memberMsg/_memberCard 函数体完整（防「整块被替换成签名行」事故）。
run_one test_member_countdown.py

#  38.5 test_plan_flash_price_guard.py —— 后台「秒杀价必须低于原价」保存前校验（2026-10-17）：
#      真实事故：1 天档「价格¥」与「秒杀价¥」都填成 0.1 → 窗口内无价差（不划线、无秒杀观感，
#      用户以为没生效）、窗口结束后价格回原价（而原价=秒杀价）→ 看起来「秒杀永远不结束」。
#      前台倒计时/角标本身是对的（见 38），根因是配置，故在保存前拦一道。
#      两层核对：静态接线（校验块位置/判定/文案/三张表/拦截分支释放闸门）+
#      node 实跑校验块真实判定（低于原价放行；等于/高于原价、缺时间、结束已过、原价 0 均拦截；
#      普通/限量模式不误伤）。变异 3 处全红才算守住。
run_one test_plan_flash_price_guard.py

#  39. test_plan_sale_table.py —— 档位快速上下架的表名解析（2026-10-03）：
#      真实事故「点下架报 未知套餐类别：dl」—— 前端分段 id(dl/ai/cp) 被当服务端表名提交。
#      三层核对：后端 _norm_plan_table 别名归一 → 前端两处模板 data-table 接线 +
#      前后端映射表一致性 → 真接口 E2E（旧命名必须成功；传错表名不得写错表）。
run_one test_plan_sale_table.py

#  40. test_feature_usage_gate.py —— 「今日使用」功能配额表 vs 真实拦截点（2026-10-04）：
#      真实事故：会员引擎 V1「照搬 DataTool」抄来 9 行功能配额，其中 6 行从未实现
#      （插件原画解析/AI字幕识别/插件批量下载素材·评论·数据·字幕/图片翻译），
#      用户在个人中心看到 9 个功能实际只有 1 个能用，会员页还拿它们做卖点。
#      本守卫要求：每行 resource 都必须在 server/ 里搜到真实 use_daily/quota_state
#      拦截点；不得残留已下线资源或 DataTool 的「插件…」命名；限额须与
#      DAILY/FREE_DAILY_QUOTA_LIMITS 一致；权益文案不得承诺已下线功能。
run_one test_feature_usage_gate.py

#  41. test_ai_credential_delivery.py —— AI 凭据「后台下发、用户不填」全链路（2026-10-05）：
#      产品定档「所有 Key 都由后台配好，用户不需要自己填」，但这条链路曾是**界面改完、
#      链路没通**：① 火山/视觉的受管层只能读不能写（管理员无法下发，换机即丢）；
#      ② app.js 拿**已隐藏**的 Key 输入框 value 判「是否已配置」——值恒为空，
#      管理员配好 Key 后用户仍看到「未配置」；③ admin.py::_gateway_base 硬编码家目录，
#      绕过 VDL_HOME 与三级优先级。
#      本守卫钉：三家都有受管写入函数、受管层覆盖用户文件、状态接口不回明文、
#      前端不再用隐藏框值做就绪判断、6 个 Key 框全在 hidden 容器内、
#      后台四个下发接口都在且都要超管、面板 modules 改为探测式就绪标记。
#      ⚠️ 需带 venv 跑（app.py 顶层 import requests）。
run_one test_ai_credential_delivery.py

#  42. test_ai_credit_costs.py —— AI 积分成本表 + 计费覆盖（2026-10-05 用户定档）：
#      「云端 + 本机重算力都计入积分」+「每次消耗多少积分要能在后台配」。
#      本次盘点的核心发现：全仓 24 个模型调用点，**只有 1 处**真在扣积分
#      （routers/matting.py）。解说（最贵、6 个 LLM/VLM 调用点）完全免费；
#      🔴 云端抠图「本地预检失败自动升级」绕过计费（fc 只来自前端表单，升级时仍 False）
#      ⇒ 反复提交烂图即可白嫖火山算力；AI 去水印 LaMa 连日配额都没有。
#      本守卫钉：A 每项有真实调用点/说明 · B 每个调用点都接了计费（含两处回调记账）
#      · C 未登记 op 必须告警不静默 · D 覆盖层优先级与恢复默认 · E 后台表不含凭据。
#      ⚠️ 需带 venv 跑（app.py 顶层 import requests）。
run_one test_ai_credit_costs.py

#  42b. test_credit_price_display.py —— 积分单价显示 + 匿名归属（2026-10-12）：
#      个人中心「积分单价」列必须显示生效价（credit_cost(op)，跟随后台覆盖层），
#      纯积分功能（抠图/视频去水印/声音克隆）必须出现在使用统计表；
#      tasks/subscriptions list_for_owner 匿名请求一律空（web-dev 同源漏洞修复）。
run_one test_credit_price_display.py

#  42c. test_commentary_duration_pricing.py —— 视频解说「按时间计费」（2026-10-12 用户定档
#       「按时间半个小时50积分」）：此前一律按次收 40，8 分钟短片与 2 小时长片同价 ——
#       长片真实成本（转写+脚本+修复重试）是短片十几倍，等于短片用户在补贴长片。
#       本守卫钉：① 折算向上取整（不足半小时按半小时，时长未知按 1 个单位，绝不算出 0 分）；
#       ② 总价 = 每半小时单价 × 单位数且随时长单调不降；③ 真实扣费必须把 total 传进
#       gate_message（漏了就静默退回按次收）；④ 个人中心显示「50 积分/半小时」
#       且「每日次数」列仍用「次」（两列不混用）。
run_one test_commentary_duration_pricing.py

#  42d. test_commentary_fail_no_charge.py —— 解说「失败不扣积分」（2026-10-23 事故修复）。
#       事故：解说在转写阶段 1 秒崩溃（包内 av 19.0.1 与 faster-whisper 1.2.1 不兼容），
#       但 AI 积分已在**开跑前**的预检里真扣掉，失败路径只退云端免费次数、没有退积分
#       通道 ⇒ 用户两次重试白扣 320 积分、一个成片都没拿到。
#       本守卫钉（行为级，不靠字符串）：① 预检只读——跑完一分不动、被拒也不动；
#       ② 预检回传账单（op/cost/vision_cost）；③ 只有产出成功才落账，且落账额
#       = 解说 + 画面理解；④ 同一个 job 重复落账不重复扣；⑤ 失败任务一分不扣；
#       ⑥ 空账单不误扣；⑦ can_afford(total) 与 gate_message(cost) 同口径。
run_one test_commentary_fail_no_charge.py

#  43. test_ai_free_trial.py —— 免费用户「首次体验」（2026-10-05 用户定档：
#      **每个功能各免一次**，口径可在后台改 per_op / once / off）。
#      背景：上一轮把 AI 积分墙补齐后，积分池为 0 的免费用户**第一次点任何 AI 功能
#      就撞 402**，连好不好用都没机会判断，漏斗第一步就断了。
#      本守卫钉：默认口径为免费用户 each-op 一次 · 首次放行不扣分且第二次必拦
#      （文案要说「已用过」，不能只丢「积分不足」）· 名额必须落盘（内存态 = 重启白薅）
#      · 有积分时不烧名额 · once/exclude/max_cost/会员豁免四种变体 · status() 暴露余量
#      · 后台 GET/POST 带策略且脏值当场 400 · 所有扣费入口都过 gate_message 收口。
#      6 处变异全红（含「名额不落盘」这一处最隐蔽的）。
#      ⚠️ 需带 venv 跑（app.py 顶层 import requests）。
run_one test_ai_free_trial.py

#  42. test_free_quota_admin.py —— 后台「免费额度」栏（2026-10-06）：
#      覆盖生效与字段级合并、脏值收敛、has_cost_gate 与真实代码一致、
#      4 个解说入口都传 vision、免费额度与 AI 积分成本同区块左右分栏、
#      **配额表每个键都必须出现在后台页面**（防再次漏项，如当时的 cloud）。
run_one test_free_quota_admin.py

#  42b. test_quota_display_e2e.py —— 配额显示同源端到端（2026-10-07 用户「改了不生效」复测）：
#      走真实 HTTP 链路（POST /api/admin/config/plans → GET /api/account/profile），
#      钉死「显示层 feature_usage_status 必须读 effective_daily_limits，与放行层 quota_state 同源」，
#      覆盖免费档 / 会员档 / 真拦截三处。函数级守卫 test_free_quota_admin 已覆盖保存侧，本例补读取侧。
run_one test_quota_display_e2e.py

#  43. test_web_quota_single_source.py —— 网页端「双配额」守卫（2026-10-06）：
#      网页端历史上存在两套每日配额 —— 旧的按 IP 计数（开源版遗留，换 IP 即重置）
#      与现行账号级（cloud_quota_gate + hk 回派 cn 为权威）。两者同时开会**双重
#      计数**。本守卫钉住：旧 IP 墙开关默认 false、账号级墙真接了配额引擎、
#      hk 回派链路完整。线上 cn/hk 实测均未开启旧墙（走代码默认）。
run_one test_web_quota_single_source.py

#  44. test_update_progress.py —— 自更新「实时进度」守卫（2026-10-08 用户反馈
#      「更新没有实时进度一直在10%」）：进度此前是硬编码三跳（downloading=10 →
#      applying=80 → ready=100），_http_download 里算好的百分比被 `_ = int(...)`
#      直接丢弃 ⇒ 472MB 全量包下载全程停在 10%。本守卫钉三层：
#      ① _http_download 真按分片回调且单调到 100%；② _prepare_update 把下载进度
#      映射到 [10,80]（下载完恰好 80）；③ _run_update 把回调接到 job 状态上 ⇒
#      /api/system/update/status 的 progress 必须随时间变化。
run_one test_update_progress.py

#  45. test_admin_recon_tab.py —— 后台「每日对账」独立成 tab + 日期精确到分
#      （2026-10-09 用户截图要求）。① 对账原先压在运维监控页最底部，访客/事件/告警
#      三块占两屏，钱账要滚到底才看得到 ⇒ 提为同级独立 tab（排在运维监控右侧）；
#      ② 对账明细日期只到「天」，逐条核对时对不上发货日志 ⇒ 授权中心 4 处 detail 全改
#      `_bj_dt`（YYYY-MM-DD HH:MM，北京时间），但**账期汇总键与告警去重键必须仍用
#      `_bj_day`** —— 改过头会把同一天的差异拆成多条告警、汇总表裂成多行。
#      8 处变异全红（含「只改一处明细」「app.js 侧容器 id 改名」这两处最隐蔽的）。
run_one test_admin_recon_tab.py

# 对账 grant_no_pay 归并守卫（2026-10-09）：用户截图「每日对账 里两条一模一样的
# 红字」—— 两笔探针发货相隔 13 秒，但明细只到分 ⇒ 渲染成同一个 HH:MM，看不出是
# 两笔、也看不出时间跨度。修法：按 (账号,档位,账期日) 归并成一行 + 明细带总笔数，
# 多笔时给**到秒**的时刻区间，单笔保持分钟级；day_rows 差异计数随之改为「异常次数」
# （与 paid_no_grant 口径一致）。
# ⚠️ 归并键是**天**不是**分钟**：同一天相隔几小时的两笔必须仍是一行（A2 用例守着，
#    变异 M9 把键换成 _bj_dt 会红）。
# 9 处变异全红。
run_one test_recon_grant_aggregate.py

# 反破解风控「风险评分」守卫（2026-10-14 第 1 批「纯观测」）：完整方案见
#   WorkBuddy/反破解风控方案_异常检测_自动降级_人工复核_2026-10-14.md。
#   本批只算分、记事件、交看板复核，**不挂任何自动响应**。钉住六信号 + 分数/档位边界 +
#   自清性（不累加不沉淀）+ 弱信号不可单独跨档 + 豁免 + risk_top/risk_clear，以及三条红线：
#   ① IP 永不进风险路径（六信号签名不含 ip ⇒ 杜绝 CGNAT 一锁一片）；② 档位集合不含 ban
#   （自动永久封禁永远不做）；③ 纯观测——risk_eval 返回值无任何自动动作字段。
#   配套变异测试 server/tests/mutation_risk_eval.py：10 项注入缺陷必须变红。
run_one test_risk_eval.py

# 46. test_profile_site_row.py —— 「关于」页「官方网站」入口（2026-10-09 用户要求）。
#      两个必须钉死的坑：① 桌面壳是 WKWebView，**会静默拦截 `window.open`** —— 若
#      有人把点击改回 window.open，网页版正常、桌面端点下去毫无反应且不报错，故
#      必须走 `_openExternalUrl()`（原生桥优先）；② 不能用 `<a href>` 承载，
#      桌面壳里锚点会尝试把**主框架**导航到外网（项目已有同类事故：二维码 blob）。
#      另钉 HTML/JS 两处 URL 常量一致、图标与箭头结构、样式规则为**真规则行**
#      （注释掉后全文仍含选择器，靠 contains 会假绿）。
#      17 处变异全红（含「注释掉 -ic/-label/-arrow 规则」「改回 window.open」）。
run_one test_profile_site_row.py

# 47. test_placeholder_contrast.mjs —— 输入框 placeholder 灰度统一（2026-10-09 用户反馈
#      「会话 Cookie 框字体颜色太深」）。实测根因：Chrome 的 UA 默认 placeholder 是深灰
#      #757575，而页面里**只有主输入框 #urlInput 被显式调浅**成 #a2acbd ⇒ 其余框（含 Cookie
#      会话框）都吃默认深灰，WKWebView 默认又偏浅，两引擎深浅还各不相同。修法是加一条
#      全局兜底 `input::placeholder, textarea::placeholder { color: #a2acbd; opacity: 1 }`。
#      守卫钉死：① 兜底须同时覆盖 input 与 textarea（Cookie 框本身是 textarea）；
#      ② 兜底色必须与 #urlInput 基准**同值**（同源约束，防只改一处又错开）；
#      ③ 兜底（0-1-1）必须排在 .auth-input::placeholder（0-1-1）之前，否则同权重后写者胜、
#         会悄悄改掉登录框既有的 #b8bec5；④ 任何 ::placeholder 都不得再出现 #757575，
#         且 .adv-textarea/.adv-input 不得自挂色（会覆盖兜底）；⑤ 兄弟树须同步（防两树分叉）。
#      11 处变异全红。
run_node test_placeholder_contrast.mjs

# 48. test_howto_guide.mjs —— 每个功能的「怎么用」操作引导（2026-10-09 用户要求
#      「给每个功能加上操作引导」）。18 个功能入口（含去水印 4 个子面板）各一条
#      原生 <details class="howto"> 折叠条，首次进入该功能时由 switchView 里的
#      howtoAutoOpen() 自动展开一次，已读记录存 localStorage.vdl_howto_seen。
#      守卫钉死：① 18 条齐全且 key 不重复、每条默认收起（写死 open 就等于没有"首次"）；
#      ② 每条必须落在自己所属的视图/子面板区间内（挪到别的页 = 串台）；
#      ③ 引导里用「」标注的每个控件名，都必须在 index.html / app.js 里真实存在
#         —— 且**出处库要先剥掉引导条自身**，否则引导里写什么库里就有什么，断言自证成真；
#      ④ JS 断言同样要剥注释后再比对（注释里也写着 d.closest('[hidden]') 之类字样）。
#      18 处变异全红，含「把按钮名改成不存在的控件」「引导整块挪去别的功能页」
#      「只在注释里保留 closest([hidden])」三个自证/定位类用例。
run_node test_howto_guide.mjs

# 50. test_commentary_source_restore.mjs —— 解说页「切走再回来，原视频没了」（2026-10-24
#      用户报障）：loadCommentary() 历来无条件清预览（2026-09-18 为修黑屏残留覆层），
#      从没做过恢复 ⇒ 侧栏切走再点回、或点「刷新」，选中/拖入的源视频就成空占位。
#      守卫钉死：① 清前必须捞走 pick 与 File 对象；② 恢复必须排在 refreshComSource()
#      之后（选项是那一刻才重建的，提前恢复会静默失败）；③ 媒体库下拉与本地文件两个
#      入口都要记账，取消选择要清账；④ 真刷新后本地文件拿不回来必须明示重选；
#      ⑤ loadedmetadata→comFeatherDetect 自动探测链必须还在（羽化绿条靠它回来）。
run_node test_commentary_source_restore.mjs

# 51. test_baidu_cookie_share.mjs —— 百度网盘登录态进共享池（2026-10-25 用户定档
#      「加入共享池，给其他用户一起用」）。BDUSS = 完整网盘账号权限，敏感度远高于
#      视频平台 Cookie，所以只接受**显式贡献**：
#      ① 百度进 _CONTRIB_ONLY_DOMAINS，被 _sync_domains_to_cloud 与
#         /api/cookie/sync/from-local 双双排除（否则等于替所有用户决定共享账号）；
#      ② 贡献只走网盘页「贡献到共享池」按钮（带确认弹窗），用的是手动保存的 BDUSS；
#      ③ 贡献前真问百度 uinfo 验真，过期/被踢直接拒；
#      ④ 取用顺序：手动 BDUSS > 本机缓存 > 浏览器解密 > 共享池（自己的优先）。
run_node test_baidu_cookie_share.mjs

# 52. test_baidu_pool_fallback.py —— 百度共享池兜底的**运行时**测试（2026-10-25）。
#      源码守卫（上一条）只能看「有没有那段代码」，看不住「跑不跑得到」：第一版
#      把兜底写在本机缓存 return 之后成了死代码，池子里有条目却取不到。
#      本测试直接调 _baidu_login_cookie()：无本机登录态必须落到共享池，
#      存了手动 BDUSS 必须盖过共享池。HOME/VDL_DATA_DIR 全隔离，不碰真实凭据。
run_one test_baidu_pool_fallback.py

# 53. test_cloud_drive_auto_contrib.py —— 浏览器/缓存里的百度登录态**自动**入共享池
#      （2026-10-25 用户定档：用户自己填的入口已隐藏，浏览器里本来有就自动取、
#      自动共享）。钉住：手动那份仍优先且不被自动共享；缓存/浏览器命中 ⇒ 入本地池
#      + 推云端两步都做；共享池故障不影响本次取用；共享池兜底仍排最后。
run_one test_cloud_drive_auto_contrib.py

# 49. test_task_ownership_p0_2.py —— P0-2 下载任务/订阅源 越权隔离（安全审计 2026-10-11）：
#      此前 `_require_task` 只按 id 取、`/api/tasks` 直接 `store.list_all()` 返回**全站**任务，
#      攻击者拿到/枚举到 task_id 即可读标题、拉成片、取消/暂停他人任务；订阅源同理。
#      本守卫钉死：① 服务端 uid 优先于客户端可伪造的 X-Device-Id；② 跨设备/跨账号一律 404
#      （不返回 403 防枚举）；③ 历史遗留（owner_id 为空）保持可见，避免老用户历史凭空消失；
#      ④ list_for_owner 只返回本人 + 遗留；⑤ 借他人 task_id 反查片名不得泄漏标题。
run_one test_task_ownership_p0_2.py

# 50. test_token_revocation_p0_5.py —— 注销/禁用/改密 必须撤销已签发 token（安全审计 2026-10-11）：
#      `verify_token` 原先只验 HMAC+exp ⇒ 配合 10 年 TTL，任何一次 token 泄漏都是**永久后门**：
#      用户改密/注销/被后台禁用后，攻击者手里的 bearer 照旧畅通。本守卫钉死：
#      ① payload 增加 token_version，改密/注销/禁用/删除时 +1，旧 token 立即失效；
#      ② 账号不存在/disabled/deleted_at → 一律拒（fail-closed）；
#      ③ 禁用账号不得再登录换新 token；④ 「解禁本正常账号」不得无故踢人（幂等）；
#      ⑤ 旧格式 token（无版本号）在状态未变时仍可用 —— 升级不把在线用户集体下线；
#      ⑥ TTL 可经 VDL_TOKEN_TTL_SECONDS 收紧，非法值回落默认。
run_one test_token_revocation_p0_5.py

# 51. test_mock_pay_gate_p0_3.py —— 旧 mock 支付路径不得免费发权益（安全审计 2026-10-11）：
#      `_provider()` 默认 mock，`simulate_paid` 唯一守卫是 isinstance(MockProvider) ⇒
#      任何能访问端口的人 POST 自己的 order_id 即可白拿会员/积分。本守卫钉死：
#      ① 默认（未设 VDL_PAY_MOCK）一律 404；② 开关打开但非本机回环仍拒（缺一不可）；
#      ③ 权益必须发给**订单归属账号**而非回调请求者；④ 订单缺 user_id 时拒绝发放；
#      ⑤ activate(order_id=...) 订单级幂等，重复回调不叠加会员时长。
run_one test_mock_pay_gate_p0_3.py

# 52. test_cloud_quota_refund_p0_4.py —— 云额度不得被用户自行回退（安全审计 2026-10-11）：
#      `cloud_quota_impl` 是两端共用的账号级免费云端额度记账（终身 3 次/日 1 次，
#      单次真实大模型调用 ¥0.04~0.19）。旧实现两个口子：① 负值守卫写 `and`
#      ⇒ 只传一个负数即绕过，`max(0, used+n)` 把已用数回退 = 自行刷满额度；
#      ② `refund=True` 由客户端直接传入即生效 ⇒ 消费→退还→再消费无限套利。
#      本守卫钉死：① 任一负值 400（or 而非 and）；② 客户端 refund 一律 403；
#      ③ 退还必须带 event_id 且一次性（重复 409）、不得退成负数；
#      ④ 扣减返回事件号；⑤ 每次变更写审计流水（来源/事件号/金额/IP）。
run_one test_cloud_quota_refund_p0_4.py

# 53. test_p1_core_hardening.py —— core.py 四项加固（安全审计 2026-10-11 P1）：
#      P1-6 admin 空密钥放行（`key == _ops_admin_key()` 在未配置时 ""=="" 即放行，
#           运维端点等于无鉴权；且 `_is_local_request` 把 host=="" 也算本机）
#      P1-3 stream/proxy SSRF + Cookie 外泄（回包 ACAO:* 让任意网页读走带登录态内容；
#           只验首跳导致 302→内网可绕过；任意 URL 都自动注入浏览器 Cookie）
#      P1-4 download/batch 缺 SSRF 护栏（parse_source 只做平台匹配，内网地址可入队）
#      P1-5 retry/resume 绕过配额（既不过配额门也不计费 ⇒ 取消→重试无限刷）
run_one test_p1_core_hardening.py

# 54. test_p1_identity_and_vault.py —— 回调幂等 / 超管 bootstrap / 保险箱明文 / 云账号身份：
#      P1-1 支付回调重复发权益（pay_server 只判 PAID 且 _grant 在锁外；授权中心 grant
#           无订单级幂等 ⇒ 并发/重试通知重复发货，会员天数积分翻倍）
#      P1-2 超管 bootstrap 抢占（公网全新实例「第一个注册者」自动成为超管）
#      P1-7 保险箱明文持久化（.vdlenc 解密到 DOWNLOAD_DIR/.vault_tmp 后从不删除，
#           且 scan_library/_resolve_safe 都不排除 ⇒ 明文可被列出并下载）
#      P1-8 云账号全局身份（/api/cloud/* 不校验调用者；_cloud_token 回落全局 token
#           不校验归属 ⇒ 越权借用他人账号身份）
run_one test_p1_identity_and_vault.py

# 56. test_p2_hardening.py —— 🟡/🟢 级发现（安全审计 2026-10-11 P2 批次）：
#      P2-14 分享 upload_path 任意绝对路径外传（~/.ssh/id_rsa 可传成公网短链）
#      P2-15 文件变网页 build 未鉴权、未限路径（盲读 + 同目录写产物）
#      P2-16 aria2 JSON-RPC 无 rpc-secret 且开 rpc-allow-origin-all（任意写文件）
#      P2-17 CF 隧道下 nginx $remote_addr=127.0.0.1 ⇒ 全站限流被整体豁免
#      P2-18 purge_expired 把「下载超过 TTL 的进行中任务」就地删除；降级不复核状态
#      P2-19 pause 置非法态 pausing（不在 TaskStatus），卡死后不可续不可控
#      P2-21 证件照 make/render 与文件变网页未进登录门禁
run_one test_p2_hardening.py

# 57. test_esc_escaping.mjs —— 前端 `esc()` 真转义守卫（P2-22）：
#      app.js 里两处 `const esc` 原是纯字符串化的 **no-op**，却用在 200+ 处 innerHTML
#      模板里（客服会话正文/图片地址、后台告警 detail/email、对账明细、事件日志、
#      访客 path/message…）⇒ 用户↔客服↔后台互相注入的存储型 XSS。
#      守卫行为求值每一处 esc 定义（& < > " ' 全转义）、反回归 no-op 形态、
#      并锁住「搜索过滤处不得用 esc」与「调用点数量不得塌陷」。
run_node test_esc_escaping.mjs

# 58. test_local_trust_p3_1.py —— 「本机直连」判定不可伪造（验证轮新发现，P3-1）：
#      uvicorn 默认开 `--proxy-headers`（Procfile 未关），`scope["client"]` 已被
#      X-Forwarded-For 重写；uvicorn 0.52 取「从右往左第一个不受信 host，全受信则回退最左」
#      ⇒ 链路里存在环回跳时（CF 命名隧道下 nginx 的 $remote_addr 恰为 127.0.0.1），
#      客户端自填 `X-Forwarded-For: 127.0.0.1` 即可让 client.host 变成 127.0.0.1。
#      受影响分支全是高危：运维端点免密钥（_require_admin）、dev 模式回传重置验证码
#      （账号接管）、公网首用户自动提权、读本机浏览器登录态上报公共池、改全局代理配置。
#      修复：统一走 `app._is_local_direct`（环回 **且** 未携带任何代理头）。
run_one test_local_trust_p3_1.py

# 59. test_subscription_ssrf_p3_2.py —— 订阅源 SSRF 护栏（验证轮新发现，P3-2）：
#      `/api/subscriptions` 的 add_subscription 在 parse_source 之后、probe_channel
#      （yt-dlp extract_flat，真实发起 HTTP）之前，补 `_assert_safe_url`，与 /api/download、
#      /api/resolve 口径一致；否则任意登录用户可让本机请求 169.254.169.254 云元数据等内网地址
#      （parse_source 仅做「功能白名单」，对非已知平台回退 _generic_platform 并不拦截内部地址）。
run_one test_subscription_ssrf_p3_2.py

# 59b. test_subscription_download_quota.py —— 订阅追更也计「下载」额度（2026-10-20 新增）：
#      「使用统计」里「视频下载 / 订阅追更」两行 resource 同为 download（共用一份额度），
#      但 _run_subscription_check 此前**一条都没计** ⇒ 免费档 10 次/日可被订阅完全绕过，
#      且订阅任务无 usage_ref、不进「使用详情」。守卫：每条扣 1 次 + 挂 ref；额度用尽即停
#      且剩余不进基线（下轮重试）；账本异常 fail-open；u:/d: 归属还原；会员 1000/日不误拦。
run_one test_subscription_download_quota.py

# 55. tests/test_crypto_routes.py（仓库根，非 server/tests）—— 保险箱路由集成（TestClient）：
#      加密→原件移回收站→库识别加密项→**encfile 播放内容与原文一致**→解密还原→锁定 423→禁用 404。
#      ⚠️ 安全审计 2026-10-11（P1-7）把解密播放从「落盘临时文件」改成「内存流式解密 + Range」后，
#      这条「播放内容一致」的断言就是该改动的回归网，故补登记进运行器（此前从未被跑过）。
echo ""
echo "=== tests/test_crypto_routes.py（保险箱路由集成） ==="
if VDL_CRYPTO_ENABLED=1 VDL_VAULT_CONFIG="$VDL_DATA_DIR/vault_route_test.json" \
   "$PY" "$REPO/tests/test_crypto_routes.py" 2>&1; then
  PASS=$((PASS+1))
else
  FAIL=$((FAIL+1))
fi

# tests/test_download_watchdog.py（仓库根，pytest）—— 下载停滞看门狗 + 整体硬超时端到端：
#  用「下到一半永久卡死、进度回调再也不来」的假 _run_once，验证 run_download 不会永久挂起、
#  并发槽会被释放。⚠️ 此前**从未登记进运行器**：A2（2026-10-02）给 run_download 加第 10 个
#  位置参 alt_protocol 时漏改本文件的假 _run_once，两用例从此恒红却无人发现（离线运行器不跑它）。
#  2026-10-11 全功能实测发现并修好，登记进来防再次腐化。
echo ""
echo "=== tests/test_download_watchdog.py（看门狗/硬超时，pytest） ==="
if env -u HTTP_PROXY -u HTTPS_PROXY "$PY" -m pytest "$REPO/tests/test_download_watchdog.py" -q 2>&1; then
  PASS=$((PASS+1))
else
  FAIL=$((FAIL+1))
fi

# 60. test_image_download.py —— 图片直链下载（2026-10-12 P0）全链路回归：
#     server/image_download.py 新增的「URL → SSRF-safe 流式落盘 → 落终态」管线 +
#     /api/image/download 路由 + tasks.py 的 kind 字段 + core.py 的 kind 分流
#     （重试换执行器 / 暂停拒绝 / /file 媒体类型）。35 项断言覆盖：
#     魔数识别（13 格式）、scheme 拒绝清单、SSRF（入口+重定向每一跳）、
#     体积上限（CL 预检+流式）、协作式取消、停滞看门狗、文件名清洗/去重、
#     执行器成功必须落 filepath（曾漏：用户看得到完成却取不到文件）、
#     HTTP 端到端（含 /file 取回、SVG 强制附件下载）、kind 持久化 round-trip、
#     以及 routers/image.py / routers/core.py / app.py / 前端 mode-switch 的接线棘轮。
#     配套变异测试 tests/mutation_image_download.py：8 项注入缺陷必须变红。
run_one test_image_download.py

# 61. test_launcher_window_guard.py —— 桌面窗口「被导航到应用外就导回」护栏：
#      WKWebView 会把主框架导航到非 UI 资源（2026-09-21 保存二维码 → blob: 图片；
#      2026-10-12 用户实机看到整页乱码 JSON）。launcher 在 window.events.loaded 上核对
#      窗口地址，不是应用自己的源就 load_url 导回。判定是纯函数，本文件离线验证。
run_one test_launcher_window_guard.py

# 61.5 test_win_version_guard.py —— Win7/8/8.1 启动拦截（2026-10-17 用户问「其他
#       windows 能不能也支持」）：上游三处硬约束（Python 3.13 需 Win8.1+ /
#       WebView2 Runtime 109 是最后支持版 / 原生依赖均要 Win10+）⇒ 不支持也不建议。
#       拦住启动即弹窗引导（升级 Win10/11 或用网页版 hanyuxz.top），Win10/11 与非
#       Windows 平台不误伤；main() 必须在起服务线程之前调用（fail-fast）。
run_one test_win_version_guard.py

# 61.6 test_launcher_boot_feedback.py —— 启动「静默失败」加固（2026-10-17 用户 Windows
#       装机后打不开，唯一现象是 WebView2 窗口「127.0.0.1 拒绝连接」）：① _find_free_port
#       从 connect_ex 探测改真 bind 探测（Windows Hyper-V 保留段会让 connect 误判空闲）；
#       ② ready 轮询循环内检测 server_thread.is_alive()，后端线程已死立即 break，
#       不再傻等 240s；③ 未就绪时弹窗指明 %USERPROFILE%\.vdl_launch.log
#       （GUI 双击启动 stdout 不可见，只 print = 静默失败）。
run_one test_launcher_boot_feedback.py

# 61.7 test_win_console_stdio_guard.py —— Windows 双击打不开的真根因（2026-10-17
#       用户装机实测）：PyInstaller windowed 冻结程序不分配控制台 ⇒ sys.stdout is None
#       ⇒ uvicorn 日志格式化器 isatty() 抛 AttributeError ⇒ 后端线程秒崩。CI 冒烟从
#       bash 起 exe（stdout 是 pipe 非 None）抓不到、Mac bootloader 重定向 stdout 也不
#       中招，只有 Windows 双击复现。守卫：子进程真跑复现（None⇒崩）+ 修复
#       （_ensure_stdio 补 devnull⇒Config 可构造）+ 不误伤 + 接线棘轮。
#       ⚠️ 若 uvicorn 升级自修了 None-stdout，A 组「复现」会红——按用例过期删 A 组即可。
run_one test_win_console_stdio_guard.py

# 61.8 test_win_path_layout.py —— Windows 配置目录布局守卫（2026-10-26）。
#       **在 macOS 上模拟 Windows 冻结版**，不需要虚拟机、不需要 CI、秒级反馈：
#       伪造 sys.platform=win32 / sys.frozen / APPDATA 三件套（它们都是运行时求值），
#       断言业务配置落到 %APPDATA%\VideoDownloader 而非 ~/.video-downloader。
#       另有一条扫描守卫：任何定义 _config_dir 且用 Path.home() 的模块都必须处理
#       win32，防止新增配置模块时漏加分支（那种缺陷在 mac 上永远测不出来，只会
#       表现为「Windows 用户改设置不生效」）。
#       已知例外 gateway_config（唯一不带 Windows 分支）已登记在测试文件顶部。
#       变异验证：破坏 vision_config 的 win32 分支 ⇒ 本文件 2 项变红。
run_one test_win_path_layout.py

# 61.9 test_win_ffmpeg_shim_guard.py —— Windows 打包「ffmpeg shim」陷阱守卫（2026-10-26）。
#       真机验收连挂两轮的元凶：CI 上 PATH 的 ffmpeg 是 Chocolatey **shim**（靠相对路径
#       ..\lib\ffmpeg\tools\... 找真身），拷进包后相对路径失效 ⇒ 守门实跑 --version 拦下
#       ⇒ 整轮中止、安装包产不出来（user「做 Windows 版老是卡住」的真因）。
#       第一版修复仍被拦：排除规则写成小写 chocolatey，实际路径是**大写 Chocolatey**。
#       本守卫**不是文本 grep**：把 build_win.sh 的 _resolve_real_bin 原样抽出来，
#       在假目录树上真跑（大写 Chocolatey + bin/ 放 shim + lib/ 放真身），断言解析到真身。
#       变异验证：摘掉 lib 分支 + 排除改回大小写敏感 ⇒ 4 项变红（复现 CI 日志里的 shim 路径）。
run_one test_win_ffmpeg_shim_guard.py

# 61.10 test_win_version_identity.py —— Windows 上 /api/version 不得回落 'dev'（2026-10-26）。
#       core.py::BUILD_VERSION 是 /api/version 唯一来源，候选路径原本只写 macOS 布局
#       （<exe>/../../Resources/...），Windows onedir 下变成 C:\Resources ⇒ 恒 'dev'
#       ⇒ 应用内更新拿不到当前版本。同仓 system.py **有** exe 同级候选 ⇒ 两端不同源、极难察觉。
#       用 ast 抽出 _read_build_version 真跑（假 sys.executable + 真目录树），
#       覆盖 macOS / onedir 根 / onedir 6.x(_internal/server) 三种布局。
#       变异验证：删掉 `ep.parent / 'build_version.txt'` ⇒ Windows 用例变红且 got='dev'（与真机一致）。
run_one test_win_version_identity.py

# 62. test_cloud_matting_gateway.py —— 云端抠图改走「本产品服务端」（2026-10-13 定档
#     「钥匙不出保险柜」）：网关接入时必须走网关且出站请求不含任何火山凭据；
#     未接入时保留本机直连；就绪判定与凭据来源（server/local/none）语义正确；
#     网关服务自身的新端点鉴权有效、响应不回显凭据。
#     配套变异测试 tests/mutation_cloud_matting_gateway.py：10 项注入缺陷必须变红。
run_one test_cloud_matting_gateway.py

# 63.5 test_free_quota_mode_and_stats.py —— 免费额度口径（mode on/off）+ 安装/插件
#     统计的离线守卫（2026-10-13）。覆盖：effective_daily_limits 的 mode 执行与脏值
#     回退、save_plan_overrides 清洗、GET credit-costs 下发、心跳设备画像接线、
#     授权中心 stat_aggregate/_attach_device（AST 抽取纯函数执行）。
run_one test_free_quota_mode_and_stats.py

# 63.6 test_free_quota_mode_cloud.py —— 免费额度口径（free_quota.mode）上云的离线守卫
#     （2026-10-13 接续待办 #2）。覆盖：授权中心 free_quota_impl 的 mode 三态语义
#     （设置/清除/不传）与非法值 400、plans_payload 带出 free_quota、客户端
#     free_quota_set_remote 只发提供了的键（不传 ≠ 传 null）、口径优先级
#     本机 > 云端 > 默认 on、push_free_quota_to_cloud 一起下发 mode 并刷缓存。
#     授权中心函数按 AST 抽取执行（不动真实 state）。
run_one test_free_quota_mode_cloud.py

# 63.7 test_extension_browser_precision.py —— 扩展「浏览器判定精确化」的离线守卫
#     （2026-10-13 接续待办 #4）。原按扩展加载目录路径推断（默认受管目录中性 ⇒ 恒
#     chromium；Windows 反斜杠匹配不上）。现优先用扩展请求自带的 UA 判定，路径降为兜底。
#     钉住：UA→各浏览器识别与顺序敏感、Windows 路径能认出 chrome、中性目录返回空、
#     _device_meta 优先级（UA > 目录 > 不瞎填）、授权中心白名单放开新浏览器。
run_one test_extension_browser_precision.py

# 63.8 test_update_server_counting.py —— App 内更新下载计数（8765 分发服务，2026-10-13 接续待办 #5）：
#     deploy/update_server.py 在分发包/补丁的同时计入 downloads.json 的 app_update bucket
#     （与官网下载分开计口径，中心 stat_query 同机直读合并）。钉住：只计 .zip/.delta、
#     只认 App 的 VDL-Update/ UA、HEAD 预检不计数、路径穿越/缺文件 404、by_day 90 天上限。
run_one test_update_server_counting.py

# 63.9 test_nmw_extractor.py —— 金牌影院（nmwchxx.com）解析器守卫（2026-10-14 建，
#     2026-10-21 扩容：域名族 nmw* 识别 + 四种链接写法全兼容 + API 域名跟随源站）。
#     平台条目 + CHINA_DOMAINS、probe/任务段两处分发、签名公式
#     sign=SHA1(MD5("排序参数&key=<key>&t=<毫秒>")) 逐位锁定、标题/清晰度/best 装配
#     （AST 抽函数 + sys.modules 注入假 requests/uuid，离线跑，不发真实请求）。
run_one test_nmw_extractor.py
run_one test_cookie_pool_nmw_family.py     # 金牌影院一族 Cookie 池链路：hardened 双域名/白名单派生/验真派发按域/测试 URL（2026-10-21 收编 chrqj.com 时发现只认老域名）

# 63.10 test_resolve_demand.py —— 解析需求登记守卫（2026-10-17，Downie 机制本土化
#     第一环）：解析失败（白名单空白站点/未知域名）自动落库 → 管理端「解析需求榜」
#     按「站点×周」聚合（2026-10-23 起；原按天，重试免疫）→ 每月挑 Top 写专用提取器。
#     钉住：字段/60s 同 host 节流/聚合口径（含 days_count 兼容别名）/坏行跳过/
#     2MB 截断真生效（截断曾漏写回，守卫抓出）。
run_one test_resolve_demand.py

# 63.11 test_member_notice.py —— 会员中心公告守卫（2026-10-18 用户要求）：会员中心
#     套餐卡上方一条可手动编辑的公告，真源在授权中心、随 /api/license/plans 下发。
#     钉住：云端关闭⇒空公告（不凭空造文案）/enabled=False /空文本⇒隐藏 /plans() 带
#     notice /下发失败不抛 / 前端 textContent 渲染（公告不当 HTML 解释）。
run_one test_member_notice.py

# 63. test_release_audit.py —— 发布包「机密数据」核查工具的离线守卫（2026-10-13 用户要求
#     「发布的安装包绝对不含机密数据」，并要求写入档案）。七类：明文凭据 / 私钥 / 服务端
#     凭据文件 / 用户数据 / 内部信息 / 打包垃圾 / 我方明文源码；另含三个**真实误报**的回归钉子
#     （base64 里的假 AKIA、公开 CA 包 cacert.pem、二进制版本号 10.11.12.13）。
#     工具本体 server/tools/audit_release_secrets.py 已接入 build_mac.sh 的构建自验证（命中即拦发布）。
#     配套变异测试 tests/mutation_release_audit.py：6 项注入缺陷必须变红。
run_one test_release_audit.py
run_one test_retention_library_minimal.py   # I7：app 时效清理/媒体库路径护栏最小回归（零覆盖补盲）


# ---------------------------------------------------------------------------
#  根 tests/ 大收编（2026-10-14，F-13/I2 app 侧终章）：49 个未登记根测试盲测
#  （90s/个，镜像门禁环境）后 48 个纳入门禁：45 个直过 + 2 个修复
#  （test_title_propagation 适配 _commentary_title ①显式title优先/端点改
#  _require_owned_task；test_download_file_cjk 适配「匿名请求不 touch 任务」）
#  + test_upload_title_fallback（pytest 模块级 skip 设计，缺真实产物即跳）。
#  test_kuaishou_e2e.py 设计内自跳过（需 VDL_RUN_KS_E2E=1 真网络）不纳入。
#  每个 run_root 用独立 VDL_DATA_DIR，防 credential_store/账号表串扰。
# ---------------------------------------------------------------------------
run_root() {
  local f="$1"
  echo ""
  echo "=== (root) $f ==="
  # 🔴 2026-10-14：VDL_DATA_DIR 必须每次运行都**全新**（mktemp -d），不能用固定
  # /tmp/vdl_approot_<test>——固定目录会把上一次运行的消耗型状态（免费体验/配额/
  # 账号表）带进来，第二次起必红（test_title_propagation 就这样 402 过）。
  local d
  d=$(mktemp -d "/tmp/vdl_approot_XXXXXX")
  if PYTHONPATH="$SERVER:$REPO/tests" VDL_LOGIN_GATE=0 VDL_PLANS_CLOUD=0 \
        VDL_DATA_DIR="$d" "$PY" "$REPO/tests/$f" 2>&1; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1)); FAILED_LIST="$FAILED_LIST $f"
  fi
  /bin/rm -rf "$d"
}

run_pytest_root() {
  local f="$1" path=""
  echo ""
  echo "=== (root pytest) $f ==="
  # 🔴 2026-10-14：用例同时存在于 $REPO/tests/ 与 $SERVER/tests/ 两处，
  #    原实现写死 "$REPO/tests/$f" ⇒ server/tests/ 的用例会「文件不存在」被误判 FAIL。
  for cand in "$REPO/tests/$f" "$SERVER/tests/$f"; do
    if [ -f "$cand" ]; then path="$cand"; break; fi
  done
  if [ -z "$path" ]; then
    echo "❌ 找不到用例文件：$f"
    FAIL=$((FAIL+1)); return
  fi
  local d
  d=$(mktemp -d "/tmp/vdl_approot_XXXXXX")
  if PYTHONPATH="$SERVER:$REPO/tests" VDL_LOGIN_GATE=0 VDL_PLANS_CLOUD=0 \
        VDL_DATA_DIR="$d" env -u HTTP_PROXY -u HTTPS_PROXY "$PY" -m pytest "$path" -q 2>&1; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1)); FAILED_LIST="$FAILED_LIST $f"
  fi
  /bin/rm -rf "$d"
}
run_pytest_root test_admin_superuser.py
run_pytest_root test_auth_routes.py
run_root test_bilibili_cookie_fix.py
run_root test_bilibili_url_normalize.py
run_pytest_root test_cdp_sniffer.py
run_pytest_root test_chrqj_extractor.py
# 搜狐多分片聚合：SohuIE 一集剧切成 N 个 mp4（part1 常只有几秒片头），原实现只取
# entries[0] ⇒ 用户下到 9 秒残片；且 multi_video 整个丢给 yt-dlp 也不会合并
# （outtmpl 相同会后片覆盖前片）。钉住聚合判据 + 档位映射（纯函数，不联网）。
run_pytest_root test_sohu_multipart.py
# 全平台路由/解析修掉的三处问题（2026-10-25 实测）：西瓜视频不再改写成抖音
# （ID 不互通，改写只会白等 30s+ 并盖掉真实原因）；西瓜无 Cookie 直接抛
# cookie_required；网易云单曲不再走 weibo /mv/detail。
run_pytest_root test_platform_routing_fixes.py
# 小红书短链展开 + Windows 浏览器 Cookie 兜底：_expand_xhs_short 必须能从
# 「落在 /login?redirectPath=…」的展开结果里解出笔记页，否则桌面 UA 下短链
# 必被误判「无法展开」，还会给用户一句错误的「任何下载工具都无法解析」。
run_pytest_root test_xhs_short_link.py
# 网盘（百度）解析：新接口四步 verify→tplconfig→list→sharedownload
# （sharedownload 必须 form 编码 —— 改回 json= 百度会 errno=2，本用例是回归防护）、
# 登录态三态、路由层登录态取用顺序（2026-10-23 接入）
run_pytest_root test_cloud_drive_baidu.py
# 网盘下载「前端接线」守卫：HTML id / switchView 互斥 / 三端点 / 下载带 cookie
# （少任何一处都会变成「点了没反应」或「解析成功却下载失败」，后端测试发现不了）
run_pytest_root test_clouddrive_ui_wiring.py
# 网盘直链下载策略：强制单连接 + 客户端身份 UA（2026-10-24 实测：百度按账号总带宽
# 限速，4/8/16 连接比单连接更慢且会被掐断；P2SP 身份 ~145 KB/s vs 浏览器 ~78 KB/s）
run_pytest_root test_netdisk_download_options.py
# 网盘直链必须「按文件直下」：走 yt-dlp 会把标题换成直链路径段、mp3 存成 mp4
run_pytest_root test_netdisk_direct_download.py
# 撞百度瞬时风控（403）后的自动断点续传：只认「seen403>0」的风控中断才冷却重试，
# 链接失效/磁盘满绝不重试（2026-10-29）
run_pytest_root test_netdisk_auto_resume.py
# 会员 Cookie 优先：候选排序主键从「字段数」改成「会员 → 字段数」（2026-10-29 根因修复）
run_pytest_root test_baidu_member_priority.py
# aria2c 默认 --enable-cookie=false，会静默吞掉 --header 传进来的 Cookie ⇒ 需要登录态的源 403
run_pytest_root test_aria2_cookie_args.py
run_pytest_root test_commentary_config.py
run_pytest_root test_commentary_routes.py
run_pytest_root test_commentary_stash.py
run_pytest_root test_commentary_work_purge.py
run_pytest_root test_cookie_cache.py
# browser_cookies：「自动读取浏览器登录态」的唯一入口。它静默失败时上层只看到
# 「未检测到登录态」——用户以为是自己没登录，实际是解密/枚举/挑 profile 出了问题。
# 这类故障没有异常、没有日志，只能靠用例把每一环的契约钉死（离线，不碰真实浏览器数据）。
run_pytest_root test_browser_cookies.py
run_pytest_root test_harvest_baidu_browser_cookies.py # 多 profile 浏览器抓取：逐个 profile 全量入池（2026-10-29）
run_root test_cookie_pool_bilibili.py
run_pytest_root test_cookie_sync_ratelimit.py
run_root test_crypto_unit.py
run_pytest_root test_device_id.py
run_pytest_root test_dewatermark.py
run_pytest_root test_dewatermark_layout.py
run_pytest_root test_dewatermark_selection.py
run_root test_download_file_cjk.py
run_pytest_root test_download_phases.py
run_pytest_root test_enginebadge_visibility.py
run_pytest_root test_extract_text_unit.py
run_pytest_root test_ffmpeg_tools_unit.py
run_root test_friendly_error_403.py
run_pytest_root test_intro_outro_mode.py
run_pytest_root test_kuaishou_extractor.py
# 🔴 2026-10-14 修：这两个是 pytest 用例（无 __main__），run_root 当普通脚本跑
#    ⇒ 断言从不执行、恒"通过"（test_fallback_match_legacy_note 曾因此在 HEAD 上
#    红了很久却从未暴露）。改走 run_pytest_root 后断言才真正运行。
run_pytest_root test_license_recon.py
run_pytest_root test_license_server.py
run_pytest_root test_login_gate.py
run_pytest_root test_merged_modules_verify.py
run_pytest_root test_narration_preview.py
run_pytest_root test_process_queue.py
run_pytest_root test_proxy_cn_routing.py
run_pytest_root test_router_isolation.py
run_root test_share_url_normalize.py
run_pytest_root test_sidebar_widen.py
run_pytest_root test_tasks.py
run_root test_torrent_routes.py
run_pytest_root test_torrent_unit.py
run_pytest_root test_upload_convert.py
run_pytest_root test_upload_src_filename.py
run_pytest_root test_web_contract.py
run_root test_ydlp_update.py
run_pytest_root test_youku_card_removed.py
run_pytest_root test_youtube_player_skip.py
run_pytest_root test_title_propagation.py
run_pytest_root test_upload_title_fallback.py
run_pytest_root test_extension_package_head.py # 扩展包下载端点 HEAD 预检：HEAD 空 body 但 Content-Length=GET 真实体积（2026-10-14 接续待办 #5）
# ↓↓↓ Windows 桌面端三连（2026-10-18 统一补登记；此前只在本机单跑、未进套件）
run_pytest_root test_win_no_window.py # 子进程不弹黑窗：Popen 默认补 CREATE_NO_WINDOW + Windows 上 auto 不启用 aria2c（事故：一开下载连弹好几个 aria2c 黑框）
run_pytest_root test_win_version_guard.py # Win7/8 启动即拦截并弹窗引导网页版，别装完打不开且无提示
run_pytest_root test_launcher_boot_feedback.py # 后端起不来必须弹窗告知并指日志路径，禁止静默失败
run_pytest_root test_win_console_stdio_guard.py # 无控制台时 sys.stdout is None ⇒ uvicorn 配置日志即崩（Windows 双击打不开的真因）

#  test_admin_usage_cloud_display.py —— 后台「用户使用详情」必须看得见云端用量
#    （2026-10-18）：网页版账号的消耗记在授权中心 authority.usage，不进本机
#    usage_history；旧版判空只读本机 ⇒ 云端明明用过也报「没有任何使用记录」。
#    用 node 实跑产品代码段 + 静态断言双保险（杜绝「grep 到字符串就算过」）。
run_one test_admin_usage_cloud_display.py # 云端用量必须渲染，判空必须算云端

#  test_license_usage_history.py —— 授权中心「账号级用量近一年按天历史」（2026-10-18）
#    日切时把前一天归档进 user["usage_history"]（保留 366 天），usage_impl 一并下发；
#    桌面 App 与网页版共用同一份账号级 daily，所以两端用量都进这份历史。
#    边界：只对上线之后发生的用量生效，过去被覆盖的日期无法补录。
run_one test_license_usage_history.py

#  test_cookie_pool_identity.py —— 公共 Cookie 池的身份去重与入池验真（2026-10-18）
#    B 站池 1905 条其实是同 1 个账号的 1905 次快照，其中 1902 条是未登录游客态，
#    而 get_cookie 取「最新一条」⇒ 解析恒 412 只能靠 yt-dlp 兜底。
#    本用例守住三件事：未登录不得入池 / 同账号只留最新 N 条 / 剔除时按会话验一次
#    而不是逐条打 1905 次请求（后者必然触发风控）。
run_pytest_root test_cookie_pool_identity.py # 身份去重 + 入池验真 + 会话级验真预算

#  test_cookie_browser_push.py —— 扩展登录态推送接口 + 本机缓存根域回落（2026-10-18）
#    ① 扩展（extension/cookie-harvest.js）采集到登录态后 POST /api/cookie/browser-push，
#       写进 ~/.videodownloader/cookies —— 比 App 自己解密浏览器 Cookie 库更准
#       （扩展读的是用户此刻正在用的那个 profile）。
#    ② 只允许本机直连 + 扩展/本机来源：外部站点若能调它，就能借用户浏览器把伪造
#       Cookie 写进本机缓存，下次解析带着它们去请求目标站。
#    ③ 扩展按根域推（bilibili.com）、解析按具体域取（www./api.）⇒ 缓存必须能按根域
#       回落，否则扩展同步等于白做；但精确 host 的缓存必须优先（含 host-only cookie）。
run_pytest_root test_cookie_browser_push.py # 登录态推送闸门 + 根域回落

#  test_admin_cookies_panel.py —— 超管后台「Cookie 池」页（2026-10-17）
#    ① 页面看的是「云端公共池」（网页版真在用的那个），不是本机空池；云端不可达才回落
#       本机并标注来源，绝不冒充成功。
#    ② 三态口径：valid / invalid / unknown（无探针≠无效），静态旁证只下 invalid 结论，
#       避免把游客态当「还能用」、也避免把没验过的当「不能删」。
#    ③ 同一账号多份快照，验真结论要回写「同会话全部 entry_id」，否则页面按最新一份
#       展示时命中不到缓存，永远显示「未校验」。
#    ④ 注入必须先验真再入池（无效拒收）、多行粘贴逐行校验防 CRLF 头注入；
#       云端入口只暴露脱敏信息，且必须带 VDL_COOKIE_SYNC_TOKEN。
run_pytest_root test_admin_cookies_panel.py # 后台 Cookie 池：概览/校验/注入/删除/云端代理

#  test_cloud_pool_readback.py —— 桌面端回读云端池（2026-10-25）
#    ① 本机池 miss → 回读云端 get：命中/未命中/旧版云端 400/不可达 都不抛；
#    ② 命中 5 分钟 TTL 缓存、未命中负缓存 1 分钟、不可达全局冷却 60s；
#    ③ _pooled_cookie 本机优先（本地命中绝不调云端）；禁环境代理（防 407）；
#    ④ 配置优先级 env > cloud_sync.json；服务端 _do_get 白名单/子域回落/ckey 仅优酷。
run_pytest_root test_cloud_pool_readback.py

#  test_usage_details_ledger.py —— 用量明细台账（2026-10-19）
#    ① use_daily(title/platform) 把「下载了什么」写进 daily_details，供后台定位；
#    ② 无标题不记、每资源最多 50 条、标题/平台截断（防无限增长）；
#    ③ 跨天日切时明细随计数一起归档进 usage_history，并清空当日明细；
#    ④ 上云钩子必须把标题/平台带进 items（授权中心按天归档 details）。
run_pytest_root test_usage_details_ledger.py

#  test_usage_result_flag.py —— 每次下载「成功了吗」（2026-10-20 用户要求）
#    ① 后台「使用详情」要能看见每条是否下载成功；成功＝下载完成、成品存到本机；
#    ② use_daily 返回 ref 并落到明细；任务终态按 ref 回写 r=1/0（幂等、只报最终态）；
#    ③ 授权中心 n=0 + r 只更新结果、绝不重复计数，跨天归档后也能补上；
#    ④ 结果缺失显示「未上报」，绝不把老数据/中断当成功或失败。
run_pytest_root test_usage_result_flag.py

#  补登此前「写了 run_pytest_root 但函数尚未定义」的两个用例（2026-10-19）：它们在
#  文件前段曾被调用，实际只打了 command not found，从未进过构建自验证。
run_pytest_root test_license_base.py # 授权中心 base url：云同步配置/环境变量/兜底优先级
run_pytest_root test_membership_device.py # 设备绑定：激活记账/换机不锁/弱指纹不锁/吊销

# 64. test_cloud_data_card_hidden.mjs —— 会员中心「云端资料」卡片隐藏（2026-10-20 用户要求，
#      与网页版同步）：只掐展示入口，元素不删（app.js $() 取节点）、文案照常渲染
#      （恢复显示不用重测），登录自动同步 /api/cloud/sync、/api/userdata/status、
#      /api/userdata/sync 三条后台链路一个都不能少。开关在 app.js 的 CLOUD_DATA_CARD_VISIBLE。
run_node test_cloud_data_card_hidden.mjs

# 65. test_cookie_sync_auto.py —— 登录态「自动」同步的两个失点（2026-10-21 用户
#      「浏览器登录了快手，怎么没自动获取」）：
#      ① from-local 只写本机池却自称「上报到公共池」⇒ 加 cloud 字段如实回报，
#         且云端失败不得把本机成功判成失败；
#      ② 上云原本只有 30min 定时器、每轮要逐域实时解密（14 站 16.7s）⇒ 新增
#         /api/cookie/sync/auto：先比对浏览器 Cookies 库 mtime/size，没变化毫秒
#         skipped、有变化才推送；推送全失败不落盘签名（下轮必须能重试）。
#      ③ 拿到新鲜 header 时刷本机快照：30 天 TTL 的旧快照优先级高于本机池，
#         不刷它就等于「同步成功但依旧未登录」。
run_one test_cookie_sync_auto.py

# 分享域 / 极速版域 → 主站登录域（2026-10-21）：chenzhongtech→kuaishou、
# iesdouyin→douyin、b23.tv→bilibili。这些域自身只有设备指纹（did/didv、ttwid），
# 不换域取 Cookie = 匿名解析。含「主站不得被误映射」和 B站 CDN 回归。
run_one test_cookie_share_host_map.py

# 解说渲染「绝不慢放」守卫（2026-10-26）：用户实拍成片突然慢动作，根因是
# 渲染预检检出「旁白字数超片段时长预算」就自动放弃原速（--no-original-speed
# ⇒ setpts 慢放对齐）。用户拍板：画面绝不慢放、解说保持完整 —— 渲染层原速
# 模式本就窗口跟随旁白（原速多播几秒/放不下整段跳过），恒原速即可。
# 本守卫钉死：预检恒 True、渲染链路无降速模式、app.py 慢放开关默认关。
run_one test_commentary_never_slowdown.py

echo ""
echo "========================================="
echo "  通过: $PASS   失败: $FAIL"
if [ -n "$FAILED_LIST" ]; then
  echo "  失败套件：$FAILED_LIST"
fi
echo "========================================="
if [ "$FAIL" -gt 0 ]; then
  echo "❌ 存在失败用例，构建不应发布"
  exit 1
fi
echo "✅ 全部离线测试通过"
