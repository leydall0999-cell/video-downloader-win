#!/usr/bin/env python
"""网盘直链「按文件直下」守卫测试（2026-10-24）

用户实测报错：「视频解析失败 — [generic] 3eab43fb6m1795...」
任务记录里暴露的真实症状：
  - filename 变成 `3eab43fb6m17959086004425e3138a2d.mp4`（直链路径段 + 错误后缀）
  - 真实文件名「008.爱的故事-孙耀威.mp3」丢了，mp3 被存成 mp4

根因：网盘 dlink 仍被交给 yt-dlp，generic 提取器把直链当媒体页处理。
修法：`run_download` 开头对网盘直链短路，走 `_download_netdisk_direct`
（单连接 + P2SP UA + 手动 Cookie + 用解析阶段的真实文件名落盘）。

本文件是纯离线守卫，不发任何网络请求。
"""
import os
import sys
import unittest

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

os.environ.setdefault("VDL_DATA_DIR", "/tmp/vdl_test_netdisk_direct")

import downloader as D  # noqa: E402
from tasks import DownloadTask  # noqa: E402

DLINK = "https://d.pcs.baidu.com/file/3eab43fb6m17959086004425e3138a2d?fid=1&sign=X"


def _task(title: str, url: str = DLINK) -> DownloadTask:
    return DownloadTask(id="t1", url=url, title=title, platform="百度网盘",
                        quality="最佳画质（自动）")


def _src() -> str:
    with open(os.path.join(SERVER_DIR, "downloader.py"), encoding="utf-8") as f:
        return f.read()


class TestOutputName(unittest.TestCase):
    def test_keeps_real_name_with_ext(self):
        """解析阶段带来的真实文件名（含扩展名）必须原样保留。"""
        t = _task("008.爱的故事-孙耀威.mp3")
        self.assertEqual(D._netdisk_output_name(t, "audio/mpeg", DLINK),
                         "008.爱的故事-孙耀威.mp3")

    def test_mp3_not_turned_into_mp4(self):
        """回归核心：mp3 绝不能被改成 .mp4。"""
        t = _task("008.爱的故事-孙耀威.mp3")
        self.assertFalse(D._netdisk_output_name(t, "audio/mpeg", DLINK).endswith(".mp4"))

    def test_adds_ext_from_content_type_when_missing(self):
        t = _task("008.爱的故事-孙耀威")
        self.assertEqual(D._netdisk_output_name(t, "audio/mpeg; charset=utf-8", DLINK),
                         "008.爱的故事-孙耀威.mp3")

    def test_no_title_falls_back_to_url_and_ct(self):
        t = _task("")
        name = D._netdisk_output_name(t, "application/pdf", DLINK)
        self.assertTrue(name.endswith(".pdf"), name)
        self.assertNotIn("?", name)

    def test_strips_path_separators(self):
        """标题里带路径分隔符时不能越界写到别处。"""
        t = _task("../../etc/passwd.mp3")
        name = D._netdisk_output_name(t, "audio/mpeg", DLINK)
        self.assertNotIn("/", name)
        self.assertNotIn("\\", name)
        self.assertTrue(name.endswith(".mp3"))

    def test_unknown_ext_is_completed_by_ct(self):
        """标题里那个"扩展名"其实是乱码段（如 .3eab43fb6m）时，按 CT 补正确的。"""
        t = _task("3eab43fb6m17959086004425e3138a2d")
        name = D._netdisk_output_name(t, "audio/mpeg", DLINK)
        self.assertTrue(name.endswith(".mp3"), name)


class TestSourcing(unittest.TestCase):
    def test_run_download_short_circuits_netdisk(self):
        """run_download 必须在进入 yt-dlp 之前对网盘直链短路。"""
        src = _src()
        idx = src.find("def run_download(")
        self.assertNotEqual(idx, -1)
        head = src[idx:idx + 1200]
        self.assertIn("_is_netdisk_direct(task.url)", head,
                      "run_download 没有网盘直链短路，会继续走 yt-dlp 解析")
        self.assertIn("_download_netdisk_direct(", head)

    def test_direct_download_headers(self):
        """直链下载的三个硬要求（2026-10-24 实测，缺一即 403）：

        1) 必须带 Range —— 裸 GET 会被判"无权限/过期"（伪错误码 31326/31360）；
        2) UA 必须**运行时协商** —— 百度按「账号 + UA」分桶：白天 SVIP 账号
           浏览器 UA 206 / P2SP UA 403，晚上非会员账号正好相反。写死任何一个
           UA 都会在另一类账号上 403 31326，所以不能断言「必须用某个常量」，
           只能断言「不能写死」：UA 由 _pick_netdisk_ua 探测后填入；
        3) 必须带登录态 Cookie。
        """
        src = _src()
        idx = src.find("def _download_netdisk_direct(")
        body = src[idx:idx + 3000]
        self.assertIn('"Range": "bytes=0-0"', body,
                      "探测请求必须带 1 字节有界 Range（百度对开放区间一律 403）")
        full = src[idx:src.find("\ndef ", idx + 10)]   # 分块 Range 在本函数中后段
        self.assertIn('h["Range"] = f"bytes={st}-{en}"', full,
                      "每个分块必须带自己的有界 Range，否则百度 403")
        self.assertIn("_pick_netdisk_ua(", body, "UA 必须运行时协商，不能写死")
        self.assertNotIn('"User-Agent": _NETDISK_UA', body, "写死 P2SP UA 会在部分账号上 403")
        self.assertNotIn('"User-Agent": _NETDISK_DOWNLOAD_UA', body,
                         "写死浏览器 UA 会在另一类账号上 403 —— 两个常量都只能作为候选")
        self.assertIn('headers["Cookie"] = cookie', body)
        # 单连接：不允许出现并发下载器参数
        self.assertNotIn("concurrent_fragments", body)

    def test_netdisk_ua_candidates_cover_both_identities(self):
        """两个 UA 候选都必须在表里，否则协商只会在同一类账号上打转。"""
        self.assertEqual(
            set(D._NETDISK_UA_CANDIDATES), {D._NETDISK_DOWNLOAD_UA, D._NETDISK_UA}
        )
    def test_pause_is_consumed_and_part_kept(self):
        """2026-10-26 用户实测：点「暂停」还在下载、下完才停。

        三个硬要求：
        1) 下载循环必须消费 pause_requested（否则暂停永远不落地）；
        2) 暂停走 _NetdiskPaused 且**保留 part**（区别于取消的 unlink）；
        3) 续传必须 206 才追加（"ab"），200 全量回退覆盖重下。
        """
        src = _src()
        idx = src.find("def _download_netdisk_direct(")
        end = src.find("def run_download(")
        body = src[idx:end]
        self.assertIn("task.pause_requested", body,
                      "下载循环没消费 pause_requested，暂停永远不会生效")
        # 2026-10-28 并发化后：暂停在 worker 循环里消费，收尾落 paused 并**保留** part+位图
        _p = body.find("if task.pause_requested:")
        self.assertNotEqual(_p, -1, "并发路径收尾缺 pause_requested 分支")
        _tail = body[_p:body.find("if fatal or any(", _p)]
        self.assertNotIn("part.unlink", _tail,
                         "暂停分支不得删除 part（那是取消分支的行为）")
        self.assertIn('status="paused"', _tail)
        # 串行兜底（拿不到总长时）仍用异常上抛，续传必须 206 追加、200 覆盖重下
        _j = src.find("def _download_netdisk_serial(")
        ser = src[_j:src.find("\ndef ", _j + 10)]
        self.assertIn("raise _NetdiskPaused()", ser)
        self.assertIn('"ab" if resume_from else "wb"', ser,
                      "续传必须 206 才追加，200 全量必须覆盖重下")

    def test_cancel_covers_paused_state(self):
        """暂停中的任务没有线程在消费标志，request_cancel 必须就地置终态。"""
        with open(os.path.join(SERVER_DIR, "tasks.py"), encoding="utf-8") as f:
            tsrc = f.read()
        self.assertIn('task.status in ("pending", "paused")', tsrc,
                      "request_cancel 必须同时覆盖 pending 与 paused")


if __name__ == "__main__":
    unittest.main(verbosity=2)
