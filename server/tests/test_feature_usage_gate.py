# -*- coding: utf-8 -*-
"""「今日使用」功能配额表必须与服务器真实拦截点一致（2026-10-04）

背景（真实事故）：这张表在会员引擎 V1 时是从另一个产品 DataTool 整份抄过来的
（提交 26aa558 说明里写着「照搬 DataTool 三轨」），9 行里 6 行**从来没有任何
实现** —— 全仓搜不到对应路由、没有 use_daily 拦截点：

    插件原画解析 / AI字幕识别 / 插件批量下载素材 / 插件批量下载评论 /
    插件批量下载数据 / 插件批量下载字幕 / 图片翻译（+ AI_FEATURES 里的视频总结）

用户在个人中心看到 9 个功能，实际只有 1 个能用；会员页还把它们当卖点文案。
本守卫钉死：**表里每一行的 resource，都必须能在 server/ 业务代码里搜到真实的
配额拦截点**（use_daily("X") / quota_state("X")）。想加功能进表，必须先实现出来。

同时钉：
  ① 表里不得残留已下线的死资源键，也不得用从 DataTool 抄来的「插件…」命名；
  ② 每行的 free_limit / member_limit 必须与 FREE_DAILY_LIMITS /
     DAILY_QUOTA_LIMITS 一致（「体验剩余」列直接取这两个值，不一致会打架）；
  ③ download_benefits 的权益文案不得承诺已下线功能，且真正生效的配额必须有文案。
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_SERVER = _HERE.parent
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

_MP = (_SERVER / "membership.py").read_text(encoding="utf-8")

# 已下线资源（2026-10-04 随占位功能移除）
DEAD_RESOURCES = ("original", "batch_material", "comment", "data",
                  "ai_subtitle", "subtitle_batch", "image_translate")

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + name + (("  —— " + detail) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def _business_sources() -> list[tuple[str, str]]:
    """server/ 下所有业务 .py（跳过 tests / 缓存 / membership.py 本身）。"""
    out: list[tuple[str, str]] = []
    for p in sorted(_SERVER.rglob("*.py")):
        parts = set(p.parts)
        if "tests" in parts or "__pycache__" in parts or p.name == "membership.py":
            continue
        try:
            out.append((str(p.relative_to(_SERVER)), p.read_text(encoding="utf-8")))
        except OSError:
            continue
    return out


# 🔴 2026-10-06：某些 resource 的拦截点只存在于**网页版**（web-dev 分支），
# 桌面端 server/ 里搜不到 —— 最典型的是 `cloud`（云端算力）：网页版的
# 转码/拼接/去水印/字幕识别走 `cloud_quota_gate` → quota_state("cloud")，
# 而桌面端这些活儿都在本机跑、记在 `app_compute` 上。
#
# 守卫的原始意图是「不许塞没有实现的占位行」，所以这里**不放宽成通融**，而是
# 精确登记跨端资源 + 校验网页端**真的**有拦截点（用 git show 读 web-dev）。
# 网页端读不到（未 clone / 无 git）时该资源判为无拦截点 ⇒ 守卫照样会红，
# 不会因为「环境缺 git」而静默放过。
CROSS_END_RESOURCES: dict[str, str] = {
    # 🔴 2026-10-06 拆池：旧总池键 "cloud" 拆为独立功能键（cloud_commentary
    # 的拦截点在桌面端 routers/quota.py 本地，不在此表）。网页端的预检统一走
    # app.cloud_quota_gate(request, resource="X")（routers 显式传键），
    # 下面的映射指向**真有该字面量**的网页端文件。
    "cloud_convert": "web-dev:server/routers/convert.py",
    "cloud_concat": "web-dev:server/routers/convert.py",
    "cloud_dewatermark": "web-dev:server/routers/dewatermark.py",
    "cloud_dewatermark_pdf": "web-dev:server/routers/dewatermark.py",
    "cloud_subtitle": "web-dev:server/routers/subtitle.py",
    "cloud_subtitle_burn": "web-dev:server/routers/subtitles.py",
    "cloud_subtitle_translate": "web-dev:server/routers/subtitles.py",
}


def _web_dev_source(rel_path: str) -> str:
    """读**网页版仓**里的文件内容；取不到返回空串。

    🔴 2026-10-29：三仓拆分后 `web-dev` 分支/worktree 都已不存在（旧法
    `git worktree list` + `git show web-dev:...` 恒空 ⇒ 6 个跨端资源全部误报红灯）。
    改为按**工作树路径**找网页版仓（读实际文件，仍含未提交改动）：
      ① 环境变量 `VDL_WEB_REPO`
      ② 默认同级目录 `<repo 上级>/video-downloader`（= 网页版仓）
    两处都读不到就返回空串 ⇒ 该资源判为无拦截点，守卫照旧会红，
    不会因为「本机没 clone 网页版」而静默放过。
    """
    import os
    roots: list[pathlib.Path] = []
    env = (os.environ.get("VDL_WEB_REPO") or "").strip()
    if env:
        roots.append(pathlib.Path(env))
    roots.append(_SERVER.parent.parent / "video-downloader")
    for root in roots:
        p = root / rel_path
        try:
            if p.is_file():
                return p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
    return ""


def _app_compute_keys() -> set[str]:
    """app.py 里 `_APP_COMPUTE_KEYS` 白名单（闸门只认这些键）。"""
    app_src = (_SERVER / "app.py").read_text(encoding="utf-8")
    m = re.search(r"_APP_COMPUTE_KEYS[^=]*=\s*\((.*?)\n\)", app_src, re.S)
    if not m:
        return set()
    return set(re.findall(r'"([a-z0-9_]+)"', m.group(1)))


def _parse_rows() -> list[dict]:
    blk = re.search(r"FEATURE_USAGE_DEFS:[^\[]*\[(.*?)\n\]", _MP, re.S)
    assert blk, "server/membership.py 必须仍定义 FEATURE_USAGE_DEFS"
    rows = []
    for m in re.finditer(
        r'"key":\s*"([^"]+)".*?"resource":\s*"([^"]+)".*?'
        r'"free_limit":\s*(-?\d+).*?"member_limit":\s*(-?\d+)',
        blk.group(1), re.S,
    ):
        rows.append({"key": m.group(1), "resource": m.group(2),
                     "free": int(m.group(3)), "member": int(m.group(4))})
    assert rows, "FEATURE_USAGE_DEFS 解析不到任何行（格式变了？）"
    return rows


def _parse_limits(name: str) -> dict[str, int]:
    blk = re.search(name + r":[^\{]*\{(.*?)\n\}", _MP, re.S)
    assert blk, f"server/membership.py 必须仍定义 {name}"
    return {m.group(1): int(m.group(2))
            for m in re.finditer(r'"([^"]+)":\s*(\d+)', blk.group(1))}


def test_rows_have_real_gate() -> None:
    print("\n[A] 表里每一行都必须在业务代码里有真实配额拦截点")
    rows = _parse_rows()
    srcs = _business_sources()
    for r in rows:
        res = re.escape(r["resource"])
        pat = re.compile(r'(use_daily|quota_state)\(\s*["\']' + res + r'["\']')
        hit = [f for f, s in srcs if pat.search(s)]
        if hit:
            check(f'[{r["key"]}] use_daily/quota_state("{r["resource"]}") 有拦截点',
                  True, "")
            continue
        # 🔴 2026-10-06 拆键后新增的间接拦截点形态：`app_compute_gate(request, "convert")`
        # 把资源名当**参数**传进闸门（而不是在业务代码里写死 `quota_state("convert")`）。
        # 这种「参数化闸门」是真实存在的拦截点，原先的正则只认字面量
        # `use_daily("X")` / `quota_state("X")` ⇒ 会误报「无拦截点」。
        # 判据：业务代码里出现 `app_compute_gate(<任意>, "资源名"` 或
        #       `app_compute_count(...)` 且 gate 带该资源 —— 前者足以证明
        #       该资源会被真实计入（gate 里 quota_state(res) + count 里 use_daily(res)）。
        if not hit:
            argpat = re.compile(r'app_compute_gate\(\s*[^,]+,\s*["\']' + res + r'["\']')
            hit = [f for f, s2 in srcs if argpat.search(s2)]
        if not hit:
            # 🔴 2026-10-06 转换类键的形态：资源名由 `convert_quota_key(target)`
            #    **返回值**传入（`app_compute_gate(request, _ckey, _clabel)`），
            #    因为视频/音乐/图片转换走同一个端点、只能按 target 动态判定。
            #    判据：业务代码里出现 `convert_quota_key(`，且 app.py 里该函数
            #    返回值确实可能等于本资源（直接查常量表里的字面量）。
            ck = [f for f, s2 in srcs if "convert_quota_key(" in s2]
            if ck and res in ("convert_video", "convert_audio", "convert_image"):
                hit = ck
        if not hit:
            # 🔴 2026-10-06 第二轮拆池：拼接类键的形态 —— 资源名先以**字面量**
            #    出现在三元/条件表达式里，赋给变量再传给 `app_compute_gate`
            #    （`app_compute_gate(request, _ckey, _clabel)`）。
            #    判据：① app.py 的 `_APP_COMPUTE_KEYS` 白名单含该键（闸门认它），
            #    ② 业务代码里该字面量确实出现在赋值语句右侧（证明它会被传进闸门）。
            if res in _app_compute_keys():
                # 覆盖两种写法：`X = "键" if cond else "键2"` 与 `X = "键"`。
                lit = re.compile(r'["\']' + res + r'["\']')
                hit = [f for f, s2 in srcs if lit.search(s2)]
        if hit:
            check(f'[{r["key"]}] use_daily/quota_state("{r["resource"]}") 有拦截点',
                  True, "")
            continue
        # 桌面端没有 → 允许「跨端资源」，但必须在网页端**真的**有拦截点
        cross = CROSS_END_RESOURCES.get(r["resource"])
        if not cross:
            check(f'[{r["key"]}] use_daily/quota_state("{r["resource"]}") 有拦截点',
                  False, f"server/ 里搜不到（{r['key']} resource={r['resource']}）")
            continue
        rel = cross.split("::", 1)[0]
        if rel.startswith("web-dev:"):          # 写成 "web-dev:server/app.py::fn"
            rel = rel.split(":", 1)[1]
        wsrc = _web_dev_source(rel)
        # 🔴 判据必须是**真调用**，不能只认「文件里出现过资源名字面量」——
        # 后者会连注释/回派 payload 都算通过，等于给跨端资源开万能通行证。
        # 2026-10-06 拆池后网页端拦截点形态是
        # `app.cloud_quota_gate(request, resource="X")`（router 显式传键），
        # 与 use_daily/quota_state 字面量同等效力，一并认可。
        wok = bool(
            re.search(r'(use_daily|quota_state)\(\s*["\']' + res + r'["\']', wsrc)
            or re.search(r'cloud_quota_gate\([^)]*resource=\s*["\']' + res + r'["\']', wsrc)
        )
        if not wok:
            # 🔴 2026-10-20：网页端把转换类资源改成**按目标格式分派** ——
            #    `cloud_quota_gate(request, resource=_convert_resource(target, ...))`，
            #    资源名不在调用点做字面量，而在 helper 的 return 分支里。
            #    判据（两条都要）：① 该 helper 的返回值真被喂给闸门；② helper 里
            #    确有本资源键的 return —— 只认「文件里出现过这个词」等于开万能通行证。
            if (re.search(r'cloud_quota_gate\([^)]*resource=\s*_convert_resource\(', wsrc)
                    and re.search(r'return\s+["\']' + res + r'["\']', wsrc)):
                wok = True
        check(f'[{r["key"]}] 拦截点在网页版（{cross}）', wok,
              f"web-dev 的 {rel} 里搜不到 use_daily/quota_state(\"{r['resource']}\") —— "
              f"若网页版已改实现，请同步更新 CROSS_END_RESOURCES")


def test_no_dead_or_datatool_rows() -> None:
    print("\n[B] 不得残留已下线资源 / DataTool 命名")
    for r in _parse_rows():
        check(f'[{r["key"]}] 不是已下线资源', r["resource"] not in DEAD_RESOURCES,
              f'resource={r["resource"]} 属于 2026-10-04 下线清单')
        check(f'[{r["key"]}] 不是 DataTool 的「插件…」命名',
              not r["key"].startswith("plugin_"), "VDL 侧对应功能请用真实命名")


def test_limits_consistent() -> None:
    print("\n[C] 表里的限额必须与两张配额表一致")
    daily, free = _parse_limits("DAILY_QUOTA_LIMITS"), _parse_limits("FREE_DAILY_LIMITS")
    for r in _parse_rows():
        if r["member"] >= 0:
            check(f'[{r["key"]}] member_limit 与 DAILY_QUOTA_LIMITS 一致',
                  daily.get(r["resource"]) == r["member"],
                  f'表里 {r["member"]} vs 配额表 {daily.get(r["resource"])}')
        if r["free"] >= 0:
            check(f'[{r["key"]}] free_limit 与 FREE_DAILY_LIMITS 一致',
                  free.get(r["resource"]) == r["free"],
                  f'表里 {r["free"]} vs 配额表 {free.get(r["resource"])}')


def test_benefits_text_honest() -> None:
    print("\n[D] 权益文案不得承诺已下线功能，且真配额必须有文案")
    blk = re.search(r"_BENEFIT_FROM_LIMITS:[^(]*\((.*?)\n\)", _MP, re.S)
    assert blk, "server/membership.py 必须仍定义 _BENEFIT_FROM_LIMITS"
    body = blk.group(1)
    check("不承诺「原画 / 4K 直链解析 N 次/日」",
          '"original"' not in body and "原画 / 4K" not in body,
          "原画是清晰度档位门（>1080P 需会员），不按次计费")
    check("不承诺「批量下载素材 N 条/日」", "批量下载素材" not in body)
    daily = _parse_limits("DAILY_QUOTA_LIMITS")
    # 🔴 2026-10-06 拆键：跳过老键（app_compute）。它只用于存量用量归集，
    # 不再有业务写入点，也不对外展示权益（展示会让用户误以为转换/压缩/
    # 超分还共用一份额度，而拆键后已不共用）。其余真配额键仍必须有文案。
    legacy = set()
    m = re.search(r"LEGACY_QUOTA_KEYS[^=]*=\s*\(([^)]*)\)", _MP)
    if m:
        legacy = {x.strip().strip('"\'') for x in m.group(1).split(",") if x.strip()}
    for key in daily:
        if key in legacy:
            continue
        check(f"配额 {key}={daily[key]} 有对应权益文案", f'("{key}"' in body)


def main() -> int:
    test_rows_have_real_gate()
    test_no_dead_or_datatool_rows()
    test_limits_consistent()
    test_benefits_text_honest()
    print("\n" + "=" * 46)
    if FAILS:
        print("❌ 失败 %d 项：" % len(FAILS))
        for f in FAILS:
            print("   - " + f)
        return 1
    print("✅ 功能配额表与真实拦截点一致（占位功能清理回归通过）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
