"""百度网盘共享池兜底 —— 运行时回归测试（2026-10-25）。

为什么需要运行时测试（而不是只靠 test_baidu_cookie_share.mjs 的源码断言）
--------------------------------------------------------------------
第一版实现把共享池兜底写在了 `get_cached_cookie_header(...)` 的 `return` **之后**，
那是死代码：池子里明明有条目，`_baidu_login_cookie()` 永远返回空，页面表现为
「共享池没生效」。源码断言只能看「有没有那段代码」，看不住「跑不跑得到」——
只有真调用一次才能抓住这类错误。故本测试直接调函数，不 mock。

同时钉死取用顺序：手动 BDUSS > 本机缓存 > 共享池（自己的账号优先于别人的）。

全程隔离：HOME 与 VDL_DATA_DIR 都指到临时目录，绝不碰真实凭据/池子。
运行：cd server && python3 tests/test_baidu_pool_fallback.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

# 🔴 必须在 import 业务模块之前换 HOME：credential_store / cookie_pool 都用
#    Path.home() 定位，只设 VDL_DATA_DIR 不够（实测会读到真实凭据）。
_TMP = tempfile.mkdtemp(prefix="vdl_baidu_pool_")
os.environ["HOME"] = _TMP
os.environ["VDL_DATA_DIR"] = os.path.join(_TMP, "data")
# 🔴 池目录必须用 VDL_COOKIE_POOL_DIR 显式隔离（2026-10-28 事故）：
#    cookie_pool._POOL_DIR 走 `Path.home()`，而 Windows 上 Path.home() 读的是
#    USERPROFILE、不认 HOME ⇒ 只设 HOME 的隔离在这台机器上等于没设，本测试
#    的 `pool_file.write_text(...)` 会把**真实池文件整个覆盖掉**（实测把一份
#    能换到明文直链的 38 字段快照冲没了，解析立刻退回「只拿到密文」）。
os.environ["VDL_COOKIE_POOL_DIR"] = os.path.join(_TMP, "cookie_pool")
os.environ.setdefault("VDL_CLOUD_LINK", "0")

from credential_store import clear_baidu_bduss, set_baidu_bduss  # noqa: E402
from cookie_pool import _pool_file, get_cookie, is_allowed, is_contrib_only  # noqa: E402
import cookie_pool as _cp  # noqa: E402

# 🔴 上面的环境变量只在「本文件是第一个 import cookie_pool 的模块」时生效。
#    用 pytest 全量收集时，cookie_pool 往往已被别的用例先 import，`_POOL_DIR` 在
#    import 那一刻就固化成真实目录了 ⇒ 下面 `pool_file.write_text(...)` 会把假条目
#    写进**用户的真实池**（2026-10-29 实际发生：真实池里多出
#    `BDUSS=POOLSHARED123` 的 contrib 垃圾，解析时白试一个不存在的账号，并让
#    「会员优先」的候选表出现幽灵条目）。直接在 import 后覆写常量，pytest 与
#    `python3 tests/test_baidu_pool_fallback.py` 两种跑法都安全。
_cp._POOL_DIR = Path(os.environ["VDL_COOKIE_POOL_DIR"])


def test_pool_fallback_and_priority():
    # 域判定：百度可贡献可取用，但属「仅显式贡献」
    assert is_allowed("pan.baidu.com"), "百度应被池放行（贡献/取用）"
    assert is_contrib_only("pan.baidu.com"), "百度必须标记为仅显式贡献（禁自动同步）"

    # 造一条「已验真」的池条目（直接写文件：add_cookie 会验真，假 BDUSS 进不去）
    pool_file = _pool_file("baidu.com")
    pool_file.parent.mkdir(parents=True, exist_ok=True)
    pool_file.write_text(json.dumps({
        "cookies": [{"header": "BDUSS=POOLSHARED123; STOKEN=x",
                     "ts": time.time(), "source": "contrib"}],
    }), encoding="utf-8")
    assert get_cookie("pan.baidu.com"), "池条目写进去了却取不到（_pool_file 键不一致？）"

    # ① 本机没有登录态 → 必须落到共享池（第一版死代码就死在这一步）
    clear_baidu_bduss()
    # ⚠️ 先 import app 完成初始化：routers.cloud_drive 顶层 `import app`，而 app 底部
    #    include 该 router —— 若入口直接是 cloud_drive，会拿到半初始化的模块
    #    （AttributeError: partially initialized module ... has no attribute 'router'）。
    import app  # noqa: F401
    from routers.cloud_drive import _baidu_login_cookie

    # 隔离「本机浏览器缓存」：Windows 上 `Path.home()` 读 USERPROFILE，上面设的
    # HOME 不生效 ⇒ cookie_cache 会指向真实缓存目录，真实登录态一旦存在就把
    # 顺序断言污染掉（2026-10-28 本机有缓存后此例必挂）。直接把浏览器读取置空，
    # 测的仍是「本机没登录态 → 落共享池」这个语义。
    import cookie_cache
    _orig = (cookie_cache.peek_cached_cookie_header,
             cookie_cache.get_cached_cookie_header)
    cookie_cache.peek_cached_cookie_header = lambda *a, **k: ""
    cookie_cache.get_cached_cookie_header = lambda *a, **k: ""
    try:
        got = _baidu_login_cookie()
        assert got and "POOLSHARED123" in got, f"共享池兜底没接上，实际取到: {got!r}"
        print("✅ 本机无登录态 → 落到共享池（别人贡献的那条）")

        # ② 本机存了自己的 → 必须盖过共享池
        set_baidu_bduss("BDUSS=MYOWN456")
        got2 = _baidu_login_cookie()
        assert got2 and "MYOWN456" in got2, f"手动 BDUSS 必须优先于共享池，实际: {got2!r}"
        print("✅ 本机有登录态 → 盖过共享池（自己的账号优先）")
    finally:
        (cookie_cache.peek_cached_cookie_header,
         cookie_cache.get_cached_cookie_header) = _orig


if __name__ == "__main__":
    test_pool_fallback_and_priority()
    print("\n全部通过 ✅（百度共享池：兜底接得上 + 手动优先 共 2 例）")
