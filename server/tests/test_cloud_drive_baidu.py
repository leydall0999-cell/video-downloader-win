"""百度网盘接入单元测试（新接口四步，离线 mock）。

不依赖真实网络/真实账号：用 FakeSession 替换 requests.Session，验证
- URL 分类（短链 / share/init / 未知 / 提取码）
- 分享页 yunData 提取（对象字面量 + 点赋值两种页面形态）
- 新四步串联：verify → tplconfig → list → sharedownload，产出 dlink
- **sharedownload 必须 form 编码**（改回 json= 会让百度返回 errno=2 —— 回归防护）
- 无登录态 → need_login 标记；有登录态 → cookie 随文件下发
- 错误语义：提取码错（errno=-12）/ 空分享 / 只有文件夹 / 个人路径未支持
- 路由层：登录态取用顺序（缓存 → 手动 BDUSS）、need_credential 三态
"""
from __future__ import annotations

import json
import logging
import sys
import urllib.parse
from pathlib import Path
from unittest import mock

import pytest

# 让 server/ 在 import 路径中（与现有 test_*.py 约定一致：以 server 为 cwd 运行）
SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from cloud_drives import baidu  # noqa: E402


# ——————————————————————————————————————————————————————————————
# 固定样本
# ——————————————————————————————————————————————————————————————

SURL = "PsAzAEtERXiTYumhEUG0Bw"          # 去前导 1
RAW = "1" + SURL
SHARE_URL = f"https://pan.baidu.com/s/{RAW}"
PWD = "c4c0"
UK = "1100294005954"
SHARE_ID = "14483302138"
FS_ID = "1117595361416981"
SIGN = "0991edd7c421232aaf9e2cdaf60547e2a223e854"
TS = "1792762672"
SEKEY = "IqeYR1pPIMpILYG1OqMtSe8Aaxskii29IfBro7i2qsc="
DLINK = ("https://d.pcs.baidu.com/file/0edddf313med10c49a01286c8cb08b1c"
         f"?fid={UK}-250528-{FS_ID}&dstime=1792762690")

RECORDS = [{"fs_id": FS_ID, "server_filename": "001.17岁-刘德华.mp3",
            "size": "9610033", "isdir": "0"}]

# 页面形态 1：对象字面量（当前真实页面；注意页面里另有个占位的 uk:'0'）
PAGE_LITERAL = (
    '<html><script>window.yunData = {"share_uk":"%s","shareid":"%s",'
    '"uk":"0","sign":"","timestamp":""};</script></html>' % (UK, SHARE_ID)
)
# 页面形态 2：点赋值（老页面 / 历史 mock）
PAGE_DOT = (
    "<html><script>"
    "window.yunData.sign = '%s';"
    "window.yunData.shareid = '%s';"
    "window.yunData.uk = '%s';"
    "window.yunData.timestamp = '%s';"
    "</script></html>" % (SIGN, SHARE_ID, UK, TS)
)


class _Resp:
    """假响应。status_code/content 供 _share_page_missing 判定死链用（默认 200 = 正常页）。"""

    def __init__(self, payload=None, text="", status_code=200):
        self._payload = payload if payload is not None else {}
        self.text = text
        self.status_code = status_code
        self.content = (text or "").encode("utf-8")

    def json(self):
        return self._payload


class _FakeSession:
    """替身 requests.Session：记录调用，按 URL 回放固定响应。"""

    def __init__(self, records=None, page=PAGE_LITERAL,
                 list_errno=0, verify_errno=0, dl_errno=0, dl_list=None,
                 fm_info=None, fm_errno=0, dir_map=None,
                 own_fm_info=None, own_dir=None):
        self.headers = {}
        self.cookies = {}
        self.calls: list[tuple] = []
        self._records = RECORDS if records is None else records
        self._page = page
        self._list_errno = list_errno
        self._verify_errno = verify_errno
        self._dl_errno = dl_errno
        self._dl_list = dl_list
        # filemetas 是新主路径（2026-10-24）。默认 info=[] 表示「没给直链」，
        # 让老用例继续走 sharedownload 回退，保持既有断言不变。
        self._fm_info = fm_info
        self._fm_errno = fm_errno
        # 文件夹分享：{目录路径: [子项]}。None = 不支持子目录（返回 errno=2）
        self._dir_map = dir_map
        # 🔴 个人网盘（2026-10-28）：filemetas 用 target=（路径）而不是 fsids=，
        #    必须按这一点区分，否则分享侧的 filemetas 会串到个人网盘用例里。
        self._own_fm_info = own_fm_info
        self._own_dir = own_dir

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self._dispatch("GET", url, kw)

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self._dispatch("POST", url, kw)

    def _dispatch(self, method, url, kw):
        # 个人网盘那几条接口用 params= 传参（分享链路是拼在 URL 里的）。
        # 替身必须把它们并进 URL，否则按 dir= / target= 分支的断言永远拿到默认值。
        params = kw.get("params")
        if params:
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
        if "/share/verify" in url:
            if self._verify_errno != 0:
                return _Resp({"errno": self._verify_errno})
            self.cookies["BDCLND"] = urllib.parse.quote(SEKEY)
            return _Resp({"errno": 0, "randsk": urllib.parse.quote(SEKEY)})
        if "/share/tplconfig" in url:
            return _Resp({"errno": 0, "data": {"sign": SIGN, "timestamp": TS}})
        if "/share/list" in url:
            if self._list_errno != 0:
                return _Resp({"errno": self._list_errno})
            # 子目录列举：必定带 dir= 参数（且斜杠也被编码）。根目录用 shorturl+root=1。
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            dpath = (qs.get("dir") or [""])[0]
            if dpath:
                if self._dir_map is None:
                    return _Resp({"errno": 2})       # 与真实行为一致：参数不对报 2
                return _Resp({"errno": 0, "list": self._dir_map.get(dpath, []),
                              "uk": UK, "share_id": SHARE_ID})
            return _Resp({"errno": 0, "list": self._records,
                          "uk": UK, "share_id": SHARE_ID})
        if "/api/filemetas" in url:
            # 个人网盘用 target=（路径），分享用 fsids=（文件 id）——必须分开回放
            if "target=" in url:
                if self._own_fm_info is None:
                    return _Resp({"errno": 0, "info": []})
                return _Resp({"errno": 0, "info": self._own_fm_info})
            if self._fm_errno != 0:
                return _Resp({"errno": self._fm_errno})
            return _Resp({"errno": 0, "info": self._fm_info or []})
        if "/api/search" in url:
            # 2026-10-30 实测：该接口对云端多个账号一律回 errno=2（换 bdstoken /
            # 去 recursion / 用完整文件名当 wd 都试过），所以替身也照实回 2 ——
            # 谁再想退回「search 找同名文件」这条老路，测试会立刻红。
            return _Resp({"errno": 2, "list": []})
        if "/api/list" in url:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            dpath = (qs.get("dir") or ["/"])[0]
            return _Resp({"errno": 0, "list": (self._own_dir or {}).get(dpath, [])})
        if "/api/sharedownload" in url:
            if self._dl_errno != 0:
                return _Resp({"errno": self._dl_errno})
            if self._dl_list is not None:
                return _Resp({"errno": 0, "list": self._dl_list})
            fids = json.loads((kw.get("data") or {}).get("fid_list") or "[]")
            return _Resp({"errno": 0,
                          "list": [{"fs_id": f, "dlink": DLINK} for f in fids]})
        if url.startswith("https://pan.baidu.com/s/"):
            return _Resp({}, self._page)
        return _Resp({})


def _patch_session(**kw):
    """patch baidu.requests.Session，返回 (patcher, 实际被用到的 session)。

    ⚠️ 所有 Session() 调用复用**同一个替身**：2026-10-24 起，resolve_share 会在
    「换直链」这一步额外新建一个带登录态的会话（匿名会话拿到的是过期的游客直链），
    若每次都造新的替身，断言只能看到后一个空对象的 calls。
    """
    box = {}

    def _factory():
        s = box.get("s")
        if s is None:
            s = _FakeSession(**kw)
            box["s"] = s
        return s

    p = mock.patch.object(baidu.requests, "Session", side_effect=_factory)
    return p, box


# ——————————————————————————————————————————————————————————————
# URL 分类
# ——————————————————————————————————————————————————————————————

def test_parse_share_short():
    r = baidu.parse_baidu_url(f"https://pan.baidu.com/s/{RAW}")
    assert r["kind"] == "share"
    assert r["surl"] == SURL          # 去前导 1（verify/list 用）
    assert r["raw"] == RAW            # 带前导 1（tplconfig 用）


def test_parse_share_init_with_pwd():
    r = baidu.parse_baidu_url(f"https://pan.baidu.com/share/init?surl={SURL}&pwd={PWD}")
    assert r["kind"] == "share"
    assert r["surl"] == SURL
    assert r["pwd"] == PWD


def test_parse_personal_path():
    r = baidu.parse_baidu_url("https://pan.baidu.com/disk/main#/all?path=%2Fdocs%2Fdemo.mp4")
    assert r["kind"] == "personal"
    assert r["path"] == "/docs/demo.mp4"


def test_parse_unknown():
    assert baidu.parse_baidu_url("https://example.com/foo").get("kind") == "unknown"


# ——————————————————————————————————————————————————————————————
# 分享页 yunData 提取（两种页面形态）
# ——————————————————————————————————————————————————————————————

def test_yun_data_literal_form():
    d = baidu._extract_yun_data(PAGE_LITERAL)
    assert str(d.get("uk")) == UK          # 取 share_uk，忽略占位 uk:'0'
    assert str(d.get("shareid")) == SHARE_ID


def test_yun_data_dot_assign_form():
    d = baidu._extract_yun_data(PAGE_DOT)
    assert str(d.get("uk")) == UK
    assert str(d.get("shareid")) == SHARE_ID
    assert str(d.get("timestamp")) == TS


# ——————————————————————————————————————————————————————————————
# 四步主流程
# ——————————————————————————————————————————————————————————————

def test_resolve_share_full_flow():
    p, box = _patch_session()
    with p:
        files = baidu.resolve_share(SHARE_URL, login_cookie="BDUSS=abc", pwd=PWD)
    s = box["s"]
    assert len(files) == 1
    f = files[0]
    assert f.name == "001.17岁-刘德华.mp3"
    assert f.size == 9610033
    assert f.dlink == DLINK
    assert f.cookie == "BDUSS=abc"
    assert f.need_login is False
    # 四步都走到了，且顺序正确
    steps = [u for _, u, _ in s.calls]
    assert any("/share/verify" in u for u in steps)
    assert any("/share/tplconfig" in u for u in steps)
    assert any("/share/list" in u for u in steps)
    assert any("/api/sharedownload" in u for u in steps)


def test_sharedownload_uses_form_encoding():
    """回归防护：sharedownload 必须 form 编码（data=），用 json= 会被百度判 errno=2。"""
    p, box = _patch_session()
    with p:
        baidu.resolve_share(SHARE_URL, login_cookie="BDUSS=abc", pwd=PWD)
    s = box["s"]
    dl_calls = [kw for m, u, kw in s.calls if "/api/sharedownload" in u]
    assert dl_calls, "sharedownload 未被调用"
    kw = dl_calls[0]
    assert "data" in kw and "json" not in kw
    body = kw["data"]
    assert body["product"] == "share"
    assert body["uk"] == UK
    assert body["primaryid"] == SHARE_ID
    assert json.loads(body["fid_list"]) == [int(FS_ID)]
    assert json.loads(body["extra"])["sekey"] == SEKEY


def test_verify_seeds_sekey_into_session():
    """verify 的 randsk 会被 URL-encode；代码要 unquote 后再当 sekey 用。"""
    p, box = _patch_session()
    with p:
        baidu.resolve_share(SHARE_URL, login_cookie="BDUSS=abc", pwd=PWD)
    s = box["s"]
    assert s.cookies.get("BDCLND") == urllib.parse.quote(SEKEY)
    dl = [kw for m, u, kw in s.calls if "/api/sharedownload" in u][0]
    assert json.loads(dl["data"]["extra"])["sekey"] == SEKEY   # 未带 %3D 尾巴


def test_no_login_marks_need_login():
    p, _ = _patch_session()
    with p:
        files = baidu.resolve_share(SHARE_URL, login_cookie="", pwd=PWD)
    assert len(files) == 1
    assert files[0].need_login is True
    assert files[0].cookie == ""
    # 直链本身仍然拿到了（解析匿名可过，只是下载要登录态）
    assert files[0].dlink == DLINK


def test_anonymous_encrypted_dlink_returns_list_only():
    """2026-10-24 实测回归：匿名换直链时百度返回加密字符串。

    旧代码对 list 直接 item.get(...) → AttributeError，前端只看到
    「'str' object has no attribute 'get'」。现在的契约：
      · 不抛异常；返回文件清单（dlink 为空）+ need_login=True，
        前端据此把下载按钮置灰并提示填 BDUSS。
    """
    p, _ = _patch_session(dl_list="ENCRYPTED_BASE64_BLOB==")
    with p:
        files = baidu.resolve_share(SHARE_URL, login_cookie="", pwd=PWD)
    assert len(files) == 1
    assert files[0].dlink == ""            # 拿不到直链
    assert files[0].need_login is True     # 明确告知缺登录态
    assert files[0].name == "001.17岁-刘德华.mp3"


def test_logged_in_encrypted_dlink_raises_actionable_error():
    """带登录态仍拿不到直链 = 真异常，必须是人话（不再崩成 AttributeError）。

    2026-10-27 文案定档（乐懂）：上屏一律 `baidu.USER_ERROR_TEXT`，真实原因
    （密文字节数 / 账号等级 / errno）只进日志。所以这里断言统一文案；
    根因日志由 test_all_accounts_cipher_raises_member_hint 覆盖。
    """
    p, _ = _patch_session(dl_list="ENCRYPTED_BASE64_BLOB==")
    with p:
        with pytest.raises(ValueError) as ei:
            baidu.resolve_share(SHARE_URL, login_cookie="BDUSS=abc", pwd=PWD)
    assert str(ei.value) == baidu.USER_ERROR_TEXT


def test_sharedownload_is_primary_and_filemetas_falls_back():
    """主路径 = /api/sharedownload；filemetas 只在 sharedownload 给密文时兜底。

    2026-10-27 语义再变（推翻 10-24 的「filemetas 优先」）：同一链接 × 三个
    池内账号实测，filemetas 对分享文件普遍回 `errno=0, info=[]`（与账号等级无关），
    而 sharedownload 对**会员账号**回明文数组含真直链、对普通账号回 AES 密文串。
    所以顺序改回 sharedownload 主路径，filemetas 留作兜底（成本只有一次请求）。
    """
    p, box = _patch_session(dl_list="ENCRYPTED_BASE64_BLOB==",
                            fm_info=[{"fs_id": int(FS_ID), "dlink": DLINK}])
    with p:
        files = baidu.resolve_share(SHARE_URL, login_cookie="BDUSS=abc", pwd=PWD)
    s = box["s"]
    assert files[0].dlink == DLINK, "sharedownload 给密文时应退到 filemetas 拿明文链"
    urls = [u for _, u, _ in s.calls]
    assert any("/api/sharedownload" in u for u in urls)
    assert any("/api/filemetas" in u for u in urls)
    # 顺序也要锁：先 sharedownload，密文才轮到 filemetas
    i_dl = next(i for i, u in enumerate(urls) if "/api/sharedownload" in u)
    i_fm = next(i for i, u in enumerate(urls) if "/api/filemetas" in u)
    assert i_dl < i_fm, "应先试 sharedownload，再退 filemetas"
    fm = [u for u in urls if "/api/filemetas" in u][0]
    assert "dlink=1" in fm and "type=dlink" in fm


def test_dlink_headers_use_non_browser_ua():
    """直链请求头必须用非浏览器 UA，且不带 Referer。

    实测：带浏览器 UA（Chrome/Safari）一律 403 `error_code:31326 user is not authorized`；
    官方客户端 UA / 非浏览器 UA 才能 206。Referer 同样会触发 403。
    """
    p, _ = _patch_session()
    with p:
        files = baidu.resolve_share(SHARE_URL, login_cookie="BDUSS=abc", pwd=PWD)
    h = files[0].headers
    assert "Referer" not in h
    assert "Mozilla" not in h["User-Agent"]
    assert h["User-Agent"] == baidu.DLINK_UA


def test_anonymous_search_cookie_is_not_login():
    """只有百度搜索匿名 Cookie（无 BDUSS）时不能算已登录。

    实测：这类 Cookie 多半来自「百度搜索」页，任何访客都有，传给网盘接口
    照样拿不到直链；当成已登录会把「请填 BDUSS」这条可读提示盖成莫名错误。
    """
    assert baidu._has_baidu_login("BAIDUID=abc; PSTM=1") is False
    assert baidu._has_baidu_login("BDUSS=xyz") is True
    p, _ = _patch_session(dl_list="ENCRYPTED_BASE64_BLOB==")
    with p:
        files = baidu.resolve_share(SHARE_URL, login_cookie="BAIDUID=abc; PSTM=1", pwd=PWD)
    assert files[0].need_login is True


def test_dlink_list_with_non_dict_items_is_skipped():
    """list 里夹带非 dict 条目时跳过而不是崩（防御性）。"""
    p, _ = _patch_session(dl_list=["junk", {"fs_id": FS_ID, "dlink": DLINK}])
    with p:
        files = baidu.resolve_share(SHARE_URL, login_cookie="BDUSS=abc", pwd=PWD)
    assert len(files) == 1
    assert files[0].dlink == DLINK


def test_public_share_without_pwd_skips_verify():
    p, box = _patch_session()
    with p:
        baidu.resolve_share(f"https://pan.baidu.com/s/{RAW}", login_cookie="BDUSS=a")
    s = box["s"]
    assert not any("/share/verify" in u for _, u, _ in s.calls)


# ——————————————————————————————————————————————————————————————
# 错误语义
# ——————————————————————————————————————————————————————————————

def test_wrong_pwd_raises_with_errno():
    p, _ = _patch_session(verify_errno=-12)
    with p:
        with pytest.raises(ValueError) as ei:
            baidu.resolve_share(SHARE_URL, login_cookie="BDUSS=a", pwd="0000")
    assert "-12" in str(ei.value)


def test_empty_share_raises():
    p, _ = _patch_session(records=[])
    with p:
        with pytest.raises(ValueError) as ei:
            baidu.resolve_share(SHARE_URL, login_cookie="BDUSS=a", pwd=PWD)
    assert "空" in str(ei.value) or "失效" in str(ei.value)


def test_dir_only_share_raises():
    """只有空文件夹 ⇒ 明确报「没有可下载的文件」，不能说解析成功给个空列表。"""
    p, _ = _patch_session(records=[{"fs_id": "1", "server_filename": "d",
                                    "size": "0", "isdir": "1",
                                    "path": "/d"}],
                          dir_map={"/d": []})
    with p:
        with pytest.raises(ValueError) as ei:
            baidu.resolve_share(SHARE_URL, login_cookie="BDUSS=a", pwd=PWD)
    assert "文件夹" in str(ei.value)


# ——————————————————————————————————————————————————————————————
# 文件夹分享：递归展开（2026-10-27 新增能力）
# ——————————————————————————————————————————————————————————————

FOLDER_ROOT = [{"fs_id": "1001", "server_filename": "小说", "isdir": 1,
                "size": "0", "path": "/我的资源/小说"}]
FOLDER_MAP = {
    "/我的资源/小说": [
        {"fs_id": "2001", "server_filename": "甲.txt", "isdir": 0,
         "size": "100", "path": "/我的资源/小说/甲.txt"},
        {"fs_id": "2002", "server_filename": "言情", "isdir": 1,
         "size": "0", "path": "/我的资源/小说/言情"},
    ],
    "/我的资源/小说/言情": [
        {"fs_id": "3001", "server_filename": "乙.txt", "isdir": 0,
         "size": "200", "path": "/我的资源/小说/言情/乙.txt"},
    ],
}


def test_folder_share_expands_subdirs():
    """根节点是文件夹时递归展开，文件带相对目录（同名文件才分得清）。"""
    p, _ = _patch_session(records=FOLDER_ROOT, dir_map=FOLDER_MAP)
    with p:
        files = baidu.resolve_share(SHARE_URL, login_cookie="BDUSS=a", pwd=PWD)
    by_name = {f.name: f for f in files}
    assert set(by_name) == {"甲.txt", "乙.txt"}, "两个文件都要被展开出来"
    assert by_name["甲.txt"].path == "小说/", "一级目录的相对路径"
    assert by_name["乙.txt"].path == "小说/言情/", "二级目录的相对路径"
    assert by_name["乙.txt"].size == 200


def test_folder_share_respects_limits():
    """目录数/文件数上限必须生效，不能因为巨型目录把一次解析拖死。"""
    assert baidu._SHARE_MAX_FILES >= 50, "上限别设得太小，正常分享要能一次列完"
    assert baidu._SHARE_MAX_DEPTH >= 2, "至少要能进两层（分享/分类/文件）"


def test_subdir_list_uses_encoded_dir_and_no_root():
    """子目录列举的参数形态是硬要求（2026-10-27 实测），改回去必 errno=2：
    必须带 uk+shareid+sekey+is_from_web，dir 要整体编码（斜杠也编码），
    且**不能**带 shorturl / root=1。"""
    p, sess = _patch_session(records=FOLDER_ROOT, dir_map=FOLDER_MAP)
    with p:
        baidu.resolve_share(SHARE_URL, login_cookie="BDUSS=a", pwd=PWD)
    fake = sess["s"]                      # box 里存的是实际用到的替身
    sub_urls = [c[1] for c in fake.calls
                if "/share/list" in c[1] and "dir=" in c[1]]
    assert sub_urls, "应该发生子目录列举请求"
    u = sub_urls[0]
    assert f"uk={UK}" in u and f"shareid={SHARE_ID}" in u
    assert "is_from_web=true" in u
    assert "sekey=" in u
    assert "shorturl=" not in u, "带 shorturl 会让 dir 失效"
    assert "root=1" not in u, "带 root=1 会强制回根目录"
    # 斜杠必须编码成 %2F，否则百度按参数分隔处理
    dir_part = u.split("dir=")[1].split("&")[0]
    assert "%2F" in dir_part, f"dir 里的斜杠要编码：{dir_part}"


def test_dlink_failure_raises():
    """取直链失败要带 errno 语义抛上来（errno=2 = 参数/编码不对）。"""
    p, _ = _patch_session(dl_errno=2)
    with p:
        with pytest.raises(ValueError) as ei:
            baidu.resolve_share(SHARE_URL, login_cookie="BDUSS=a", pwd=PWD)
    assert "errno=2" in str(ei.value)


def test_personal_path_without_login_is_unified_text():
    """个人网盘没有登录态 —— 上屏统一文案，真实原因进日志（不打网络）。"""
    with pytest.raises(ValueError) as ei:
        baidu.resolve_personal("/docs/demo.mp4", "")
    assert str(ei.value) == baidu.USER_ERROR_TEXT


def test_personal_path_returns_plaintext_dlink():
    """个人网盘单文件 → 明文 dlink（2026-10-28 起真正实现，原先只是空壳）。

    这是非超会账号唯一的取链路径：分享接口回密文，但自己名下文件给明文。
    """
    own = [{"path": "/影视/demo.mp4", "size": "378978423", "dlink": DLINK}]
    p, _ = _patch_session(own_fm_info=own)
    with p:
        files = baidu.resolve_personal("/影视/demo.mp4", "BDUSS=a")
    assert len(files) == 1
    assert files[0].name == "demo.mp4"
    assert files[0].size == 378978423
    assert files[0].dlink == DLINK
    assert files[0].need_login is False


def test_personal_path_folder_expands_to_files():
    """目录 → 递归展开成文件列表（每个都带明文 dlink）。"""
    name = "001.17岁-刘德华.mp3"
    own_dir = {"/音乐": [{"fs_id": FS_ID, "server_filename": name, "size": "9610033",
                          "isdir": 0, "path": f"/音乐/{name}"}]}
    own = [{"path": f"/音乐/{name}", "size": "9610033", "dlink": DLINK}]
    p, _ = _patch_session(own_fm_info=own, own_dir=own_dir)
    with p:
        files = baidu.resolve_personal("/音乐", "BDUSS=a")
    assert [f.name for f in files] == [name]
    assert files[0].dlink == DLINK
    assert files[0].size == 9610033


def test_share_reuses_own_netdisk_when_ciphertext(caplog):
    """分享只给密文，但自己网盘里有同名同大小文件 → 复用它的明文直链。

    这是「非超会账号也能下分享」的核心兜底（只读：不转存、不占额度）。
    """
    name = RECORDS[0]["server_filename"]
    own_path = f"/已存/{name}"
    p, _ = _patch_session(
        dl_list="ENCRYPTED_BASE64_BLOB==",          # 分享侧：密文
        fm_info=[],                                  # 分享侧 filemetas 也不给
        # 2026-10-30：发现「找自己网盘里的同名文件」不能用 /api/search（实测该接口
        # 对部分账号一律 errno=2，等于永不命中），已改为 /api/list 递归建索引，
        # 所以这里改成按目录回放。
        own_dir={"/": [{"server_filename": name, "size": "9610033",
                        "isdir": 0, "path": own_path}]},
        own_fm_info=[{"path": own_path, "size": "9610033", "dlink": DLINK}],
    )
    with caplog.at_level(logging.INFO, logger="cloud_drives.baidu"):
        with p:
            files = baidu.resolve_share(SHARE_URL, login_cookie="BDUSS=a", pwd=PWD)
    assert files and files[0].dlink == DLINK
    assert files[0].need_login is False
    assert "自己网盘已有同名文件" in caplog.text


def test_share_reuse_finds_file_in_subdirectory():
    """文件在子目录深处也要能复用。

    2026-10-30：旧的 search 方案在云端账号上一律 errno=2，等于永不命中；改走
    /api/list 递归后必须能翻到子目录里 —— 这条守的就是「别只扫根目录」。
    """
    name = RECORDS[0]["server_filename"]
    own_path = f"/深层/目录/{name}"
    p, _ = _patch_session(
        dl_list="ENCRYPTED_BASE64_BLOB==",
        fm_info=[],
        own_dir={
            "/": [{"server_filename": "深层", "isdir": 1, "path": "/深层"}],
            "/深层": [{"server_filename": "目录", "isdir": 1, "path": "/深层/目录"}],
            "/深层/目录": [{"server_filename": name, "size": "9610033",
                            "isdir": 0, "path": own_path}],
        },
        own_fm_info=[{"path": own_path, "size": "9610033", "dlink": DLINK}],
    )
    with p:
        files = baidu.resolve_share(SHARE_URL, login_cookie="BDUSS=a", pwd=PWD)
    assert files and files[0].dlink == DLINK
    assert files[0].need_login is False


def test_share_reuse_requires_matching_size():
    """同名但大小不同 ⇒ 不是同一个文件，不许复用（防止下错片）。"""
    name = RECORDS[0]["server_filename"]
    p, _ = _patch_session(
        dl_list="ENCRYPTED_BASE64_BLOB==",
        fm_info=[],
        own_dir={"/": [{"server_filename": name, "size": "42",     # 大小对不上
                        "isdir": 0, "path": f"/别处/{name}"}]},
        own_fm_info=[{"path": f"/别处/{name}", "size": "42", "dlink": DLINK}],
    )
    with p:
        with pytest.raises(baidu.NeedMemberLogin) as ei:
            baidu.resolve_share(SHARE_URL, login_cookie="BDUSS=a", pwd=PWD)
    assert str(ei.value) == baidu.USER_ERROR_TEXT


def test_errno_message_printed_once():
    """回归：errno 只能出现一次。

    曾把 `_errno_msg()` 的结果再套一层 "（errno=%s）"，线上日志打成
    「参数错误（errno=2）（errno=2）」（2026-10-27 修）。
    """
    msg = baidu._errno_msg(2, "获取下载直链失败")
    assert msg.count("errno=") == 1
    assert msg == "参数错误（errno=2）"


def test_resolve_dispatch_and_unknown():
    p, _ = _patch_session()
    with p:
        files = baidu.resolve(SHARE_URL, "BDUSS=abc", PWD)
    assert len(files) == 1
    with pytest.raises(ValueError):
        baidu.resolve("https://example.com/x", "", "")


def test_fallback_to_page_when_list_lacks_ids():
    """list 没给 uk/share_id 时，要能从分享页 yunData 兜底取到。"""
    class _NoIdsSession(_FakeSession):
        def _dispatch(self, method, url, kw):
            if "/share/list" in url:
                return _Resp({"errno": 0, "list": RECORDS})
            return super()._dispatch(method, url, kw)

    box = {}

    def factory():
        s = _NoIdsSession(page=PAGE_LITERAL)
        box["s"] = s
        return s

    with mock.patch.object(baidu.requests, "Session", side_effect=factory):
        files = baidu.resolve_share(SHARE_URL, login_cookie="BDUSS=a", pwd=PWD)
    assert len(files) == 1
    dl = [kw for m, u, kw in box["s"].calls if "/api/sharedownload" in u][0]["data"]
    assert dl["uk"] == UK and dl["primaryid"] == SHARE_ID


# ——————————————————————————————————————————————————————————————
# 路由层
# ——————————————————————————————————————————————————————————————

def _router():
    import app as _app  # noqa: F401  （确保路由已挂载到 app）
    from routers import cloud_drive as rt
    return rt


def test_router_resolve_returns_files():
    rt = _router()
    fake = [baidu.CloudFile(name="x.mp4", size=10, dlink=DLINK,
                            cookie="BDUSS=z", headers={"User-Agent": "ua"})]
    with mock.patch.object(rt, "_baidu_login_cookie", return_value="BDUSS=z"), \
         mock.patch.object(rt.baidu, "resolve", return_value=fake):
        out = rt.cloud_drive_resolve(rt.CloudDriveResolveReq(url=SHARE_URL))
    assert out["ok"] is True
    assert out["platform"] == "baidupan"
    assert out["files"][0]["name"] == "x.mp4"
    assert out["need_credential"] is False


def test_router_resolve_third_state_parsed_but_needs_login():
    """解析成功但没登录态 ⇒ ok=True 且 need_credential=True（下载会 403）。"""
    rt = _router()
    fake = [baidu.CloudFile(name="x.mp4", size=10, dlink=DLINK, need_login=True)]
    with mock.patch.object(rt, "_baidu_login_cookie", return_value=""), \
         mock.patch.object(rt.baidu, "resolve", return_value=fake):
        out = rt.cloud_drive_resolve(rt.CloudDriveResolveReq(url=SHARE_URL))
    assert out["ok"] is True
    assert out["need_credential"] is True
    assert out["need_login"] is True


def test_router_resolve_error_flags_credential():
    rt = _router()
    with mock.patch.object(rt, "_baidu_login_cookies", return_value=[]), \
         mock.patch.object(rt.baidu, "resolve",
                           side_effect=ValueError("不是有效的百度网盘分享链接")):
        out = rt.cloud_drive_resolve(rt.CloudDriveResolveReq(url="https://pan.baidu.com/x"))
    assert out["ok"] is False
    assert out["need_credential"] is True


def test_login_cookie_prefers_stored_over_cache():
    """手动保存的 BDUSS 优先于本机缓存（2026-10-24 改序）。

    原因：百度限速按账号等级，用户为了跑满带宽会去网盘页填**会员账号**的 BDUSS；
    若缓存优先，会被本机 Chrome 里那个非会员账号盖掉 ⇒ 表现成「填了没反应」。
    显式指定的账号必须赢过自动抓取的缓存。
    """
    rt = _router()
    with mock.patch("cookie_cache.peek_cached_cookie_header",
                    return_value="BDUSS=from-cache"), \
         mock.patch.object(rt, "get_baidu_bduss", return_value="BDUSS=stored"):
        assert rt._baidu_login_cookie() == "BDUSS=stored"


def test_login_cookie_uses_cache_when_no_stored():
    """没手动保存时仍用本机缓存（装了扩展的用户零配置）。"""
    rt = _router()
    with mock.patch("cookie_cache.peek_cached_cookie_header",
                    return_value="BDUSS=from-cache"), \
         mock.patch.object(rt, "get_baidu_bduss", return_value=""):
        assert rt._baidu_login_cookie() == "BDUSS=from-cache"


def test_login_cookie_falls_back_to_stored():
    """缓存没有时用手动保存的 BDUSS（patch 必须打在路由模块的引用上，
    `from credential_store import get_baidu_bduss` 是值绑定，打 credential_store 无效）。"""
    rt = _router()
    with mock.patch("cookie_cache.peek_cached_cookie_header", return_value=None), \
         mock.patch.object(rt, "get_baidu_bduss", return_value="BDUSS=stored"):
        assert rt._baidu_login_cookie() == "BDUSS=stored"


def test_router_credential_accepts_bare_bduss():
    rt = _router()
    with mock.patch.object(rt, "set_baidu_bduss", return_value="keychain") as m:
        out = rt.cloud_drive_set_credential(rt.CloudDriveCredentialReq(bduss="abc123"))
    assert out["ok"] is True
    m.assert_called_once_with("abc123")


def test_router_credential_rejects_empty():
    rt = _router()
    out = rt.cloud_drive_set_credential(rt.CloudDriveCredentialReq(bduss="  "))
    assert out["ok"] is False


def test_resolve_share_retries_next_account_when_cipher():
    """普通号（密文）→ 自动换会员号重试，不许直接甩给用户。

    2026-10-27 线上真因：百度只对**会员账号**回明文直链。池里同时有会员号和
    普通号时，只取最新一条会随机踩到普通号 ⇒ 用户看到「百度这次没返回可用的
    下载直链（接口返回了加密数据）」。契约：按候选顺序逐个试，第一个给出明文
    直链的胜出，且下载用的就是它。
    """
    p, box = _patch_session(fm_info=[])
    tried: list = []
    real = baidu.share_dlinks

    def fake_dl(s, sign, ts, uk, share_id, ids, sekey, q):
        tried.append(1)
        if len(tried) == 1:
            raise baidu.NeedMemberLogin("普通账号只回密文")
        return {str(FS_ID): DLINK}

    with p, mock.patch.object(baidu, "share_dlinks", side_effect=fake_dl):
        files = baidu.resolve_share(SHARE_URL, login_cookie="BDUSS=vip_first",
                                    pwd=PWD,
                                    extra_cookies=["BDUSS=plain_second"])
    assert len(tried) == 2, "第一个账号拿不到明文链时必须换下一个"
    assert files[0].dlink == DLINK
    assert "plain_second" in (files[0].cookie or ""), "下载要沿用拿到直链的那个账号"
    assert real is baidu.share_dlinks


def test_all_accounts_cipher_raises_member_hint(caplog):
    """全池都拿不到明文 → 上屏统一文案，**根因进日志**（运维监控要能定位）。

    2026-10-27 定档：前端不再解释「会员 / 加密数据」，一律 USER_ERROR_TEXT；
    但「试了几个号、是否全部密文」这类真实原因必须落日志，否则线上无从排查。
    """
    p, _ = _patch_session(dl_list="ENCRYPTED_BASE64_BLOB==")
    with caplog.at_level(logging.WARNING, logger="cloud_drives.baidu"):
        with p:
            with pytest.raises(baidu.NeedMemberLogin) as ei:
                baidu.resolve_share(SHARE_URL, login_cookie="BDUSS=plain",
                                    pwd=PWD, extra_cookies=["BDUSS=plain2"])
    msg = str(ei.value)
    assert msg == baidu.USER_ERROR_TEXT
    assert " object has no attribute " not in msg
    # 根因必须能从日志里查到
    assert "已试 2 个百度账号" in caplog.text
    assert "密文" in caplog.text


def test_cookie_candidates_dedupes_by_bduss_and_drops_anonymous():
    out = baidu._cookie_candidates("BAIDUID=anon; PSTM=1", ["BDUSS=A; x=1", "BDUSS=A; y=2", "BDUSS=B"])
    assert out == ["BDUSS=A; x=1", "BDUSS=B"]


def test_cookie_candidates_keeps_same_bduss_distinct_stoken():
    """同一 BDUSS 的不同 STOKEN 快照必须都留（2026-10-28 真 bug 守卫）。

    实测（那个什么都笑的女孩英国童话故事.mp4，同一账号）：
      · STOKEN=cc795c…（缓存快照，38 字段）→ sharedownload 回明文直链；
      · STOKEN=8e6dc3…（浏览器扩展同步，32 字段）→ 同一链接回 4248 字节密文。
    只按 BDUSS 去重会裁掉能用的那条 ⇒ 上屏统一文案，看起来像「分享失效」。
    """
    out = baidu._cookie_candidates(
        "BDUSS=A; STOKEN=s1; PANPSC=p",
        ["BDUSS=A; STOKEN=s2", "BDUSS=A; STOKEN=s1; PANPSC=p"],
    )
    assert out == ["BDUSS=A; STOKEN=s1; PANPSC=p", "BDUSS=A; STOKEN=s2"]


def test_router_login_cookies_keeps_same_bduss_distinct_stoken():
    """上报路径同样不能把同 BDUSS 的另一 STOKEN 快照吃掉，且字段多的排前。"""
    rt = _router()
    import cookie_pool

    full = "BDUSS=SAME; STOKEN=aaa; PANPSC=1; pcsett=1; P1=1"
    partial = "BDUSS=SAME; STOKEN=bbb"
    with mock.patch.object(rt, "get_baidu_bduss", return_value=""), \
         mock.patch("cookie_cache.peek_cached_cookie_header", return_value=""), \
         mock.patch("cookie_cache.get_cached_cookie_header", return_value=""), \
         mock.patch.object(rt, "_baidu_cloud_cookies", return_value=[]), \
         mock.patch.object(cookie_pool, "get_cookies", return_value=[partial, full]):
        got = rt._baidu_login_cookies()
    assert len(got) == 2, got
    assert got[0] == full          # 字段多的优先，少一次失败往返
    assert got[1] == partial


def test_router_login_cookies_appends_cloud_after_local():
    """云端共享池只做兜底：本机候选全部排在前面（自己的账号/会员带宽优先）。

    2026-10-28 打通云端回读后，必须守住这个次序 —— 否则别人贡献的号（哪怕字段
    更多）会抢在自己账号前面，出现「填了自己的会员号，实际走的是别人的免费号」。
    """
    rt = _router()
    import cookie_pool

    local_full = "BDUSS=L1; STOKEN=a; P1=1; P2=1"
    cloud_only = "BDUSS=C1; STOKEN=b; P1=1; P2=1; P3=1; P4=1; P5=1; P6=1"
    with mock.patch.object(rt, "get_baidu_bduss", return_value="BDUSS=L1"), \
         mock.patch("cookie_cache.peek_cached_cookie_header", return_value=""), \
         mock.patch("cookie_cache.get_cached_cookie_header", return_value=""), \
         mock.patch.object(rt, "_baidu_cloud_cookies", return_value=[cloud_only]), \
         mock.patch.object(cookie_pool, "get_cookies", return_value=[local_full]):
        got = rt._baidu_login_cookies()
    assert got[0] == local_full, f"本机候选必须排最前，实际: {got[0]!r}"
    assert cloud_only in got, "云端候选应被追加（兜底）"
    # 手动保存的裸 BDUSS（无 STOKEN）与完整快照是不同会话键，两条都留 ——
    # 裸 BDUSS 换直链成功率低但成本也低，留着当最后一张牌。
    assert got[1] == "BDUSS=L1"
    assert len(got) == 3
