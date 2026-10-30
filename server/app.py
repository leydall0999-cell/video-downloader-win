"""视频下载站 · FastAPI 后端入口。

路由一览：
    GET    /                      前端页面
    GET    /api/platforms         支持的平台清单
    POST   /api/resolve           解析链接，返回视频信息与可选清晰度
    POST   /api/download          创建下载任务
    GET    /api/tasks/{id}        查询任务状态（轮询兜底）
    GET    /api/tasks/{id}/events SSE 实时进度
    GET    /api/tasks/{id}/file   下载已完成的文件
    DELETE /api/tasks/{id}        取消任务并清理临时文件
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import ipaddress
import json
import socket
import logging
import os
import re
import sys
import shutil
import subprocess
import threading
import urllib.request
import requests  # 解说 worker HTTP 模式客户端（VDL_COMMENTARY_MODE=http 时用到）
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import atomic_io
import engine_idle
import platform_model as plat

from codec_utils import h264_args as _h264_args, hevc_args as _hevc_args, \
    audio_encode_args as _audio_encode_args

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import File as _FastAPIFile, Form, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from urllib.parse import quote, urljoin, urlparse

# Windows：让本进程启动的所有子进程（aria2c / ffmpeg / deno…）不弹控制台黑窗。
# 桌面版是 GUI 程序（无控制台），Windows 会给它启动的控制台子程序新建一个黑框窗口；
# 必须在**任何子进程启动之前**装上，所以放在这里（import downloader 之前）。
import win_no_window
win_no_window.install()

# 在 import downloader（间接 import yt_dlp）之前，先把本机可能已下载的新版 yt-dlp
# 插入 sys.path 最前，实现「解析器自动更新」无需重新打包。
import ydlp_update
ydlp_update.bootstrap()

# 同理：解说配音走 edge-tts，微软会周期性作废旧版（403 / NoAudioReceived）。
# 在**任何 import edge_tts 之前**把用户目录里的热补丁层插到 sys.path 最前，
# 这样「自动巡查发现失效 → 下载新版 → 下次启动即生效」，不必重新打包发版。
import tts_guard
tts_guard.bootstrap()

import downloader
import library as library_mod
import subtitles as subtitles_mod
import subscriptions as subs_mod
import ffmpeg_tools as fftools
import retention as retention_mod
import crypto_vault as crypto_mod
import process_queue as pq_mod
import torrent as torrent_mod
from batch import BatchScheduler
from platforms import CHINA_DOMAINS, LinkError, UnsupportedPlatformError, is_china_host, parse_source, platform_catalog
from tasks import TaskStore, TASK_ID_LENGTH
from llm_config import (inject_llm_env, get_llm_config, save_llm_config, detect_ollama,
                        PROVIDER_PRESETS, DEFAULT_PROVIDER,
                        list_local_models, local_runtime_status, local_models_dir,
                        load_user_config_raw, managed_status, mask_key)
from vision_config import (
    inject_vision_env, get_vision_config, save_vision_config,
    VISION_PROVIDER_PRESETS, VISION_DEFAULT_PROVIDER, platform_status,
    load_user_config_raw as load_vision_config_raw,
    managed_status as vision_managed_status,
    mask_key as vision_mask_key,
)
import gateway_config
from gateway_config import (
    get_gateway_config, gateway_status, mask_token,
    upstream_models, resolve_model, cloud_env as gateway_cloud_env,
)

# 暴露给 routers 访问的共享内核（同时确保 PyInstaller 静态分析能收进 gateway_config；
# llm_config 里是函数内 import，分析器看不见，未显式引用会被漏打进包）
get_gateway_config = get_gateway_config
gateway_status = gateway_status
mask_token = mask_token
upstream_models = upstream_models
resolve_model = resolve_model
gateway_cloud_env = gateway_cloud_env

# 暴露给 routers/vision.py 访问的共享内核（镜像 PROVIDER_PRESETS 等）
VISION_PROVIDER_PRESETS = VISION_PROVIDER_PRESETS
VISION_DEFAULT_PROVIDER = VISION_DEFAULT_PROVIDER
get_vision_config = get_vision_config
save_vision_config = save_vision_config
platform_status = platform_status
load_vision_config_raw = load_vision_config_raw
vision_managed_status = vision_managed_status
vision_mask_key = vision_mask_key
from commentary_config import get_voice_sample, inject_commentary_env

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("vdl")


def _attach_file_logging():
    """root logger 额外写一份到磁盘，返回日志文件路径（失败返回 None）。

    🔴 2026-10-27：打包版是 console=False（runw.exe），desktop_launcher._ensure_stdio()
       把 sys.stdout/stderr 换成 devnull —— basicConfig 的 StreamHandler 写进去
       等于**扔掉**。而这轮把网盘失败的用户可见文案统一成一句话后，真实原因
       （errno / 账号等级 / 密文字节数 / 浏览器锁库）只剩 logger 这一个出口，
       不落盘就等于线上无从排查。所以这里补文件日志（8MB × 轮转 3 份）。
    任何异常都吞掉：日志落盘失败绝不能影响启动。
    """
    try:
        from logging.handlers import RotatingFileHandler

        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        log_dir = Path(base) / "VideoDownloader" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "vdl.log"
        fh = RotatingFileHandler(
            str(path), maxBytes=8 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        fh.setLevel(logging.INFO)
        logging.getLogger().addHandler(fh)
        return path
    except Exception:  # noqa: BLE001 - 日志自身失败不能拖垮启动
        return None


LOG_FILE = _attach_file_logging()

if getattr(sys, "frozen", False):
    # PyInstaller 打包后资源位置：
    #   - 单文件模式：解压到 sys._MEIPASS
    #   - macOS .app（单文件夹）：数据文件在 Contents/Resources
    #   - 其它单文件夹模式：与可执行文件同目录
    _exe = sys.executable
    _macos_dir = os.path.dirname(_exe)
    _resources = os.path.normpath(os.path.join(_macos_dir, "..", "Resources"))
    if getattr(sys, "_MEIPASS", None):
        _frozen_base = Path(sys._MEIPASS)
    elif os.path.exists(os.path.join(_resources, "web")):
        _frozen_base = Path(_resources)
    else:
        _frozen_base = Path(_macos_dir)
    BASE_DIR = _frozen_base
    WEB_DIR = _frozen_base / "web"
    # 下载产物放在用户目录，避免写入只读的 .app 包内
    DOWNLOAD_DIR = Path.home() / "Downloads" / "VideoDownloader"
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
    WEB_DIR = BASE_DIR / "web"
    DOWNLOAD_DIR = BASE_DIR / "downloads"

MAX_CONCURRENT_DOWNLOADS = 3
MAX_CONCURRENT_PROBES = 8
# 批量下载相关环境变量（桌面版万能下载器重点能力）：
#   VDL_BATCH_HARD_MAX   线程池硬上限（实际并发不超过它，默认 8）
#   VDL_BATCH_RETRIES    失败自动重试次数（默认 2，指数退避最多 30s）
#   VDL_BATCH_MAX_ITEMS  单次批量最多链接数（默认 50，防误粘贴巨量链接打爆）
VDL_BATCH_HARD_MAX = int(os.environ.get("VDL_BATCH_HARD_MAX", "8") or 8)
BATCH_RETRIES_DEFAULT = int(os.environ.get("VDL_BATCH_RETRIES", "2") or 2)
VDL_BATCH_MAX_ITEMS = int(os.environ.get("VDL_BATCH_MAX_ITEMS", "50") or 50)
SINGLE_DOWNLOAD_RETRIES = 0  # 单条下载默认不自动重试（失败让用户手动点重试）；批量才默认重试

# ---- 格式转换（增值能力）：对已下载文件做 ffmpeg 转码，异步 job ----
def _safe_mkdir(p: Path) -> None:
    """建立目录但绝不让 TCC/权限问题打断 import。

    ~/Downloads 属 TCC 保护目录；新构建首次运行时若用户点了「不允许」，
    mkdir 会抛 PermissionError，import 直接崩 → 后端静默死亡。目录建不出来
    也要让应用先起来，真正写文件时再把错误暴露到 UI（可操作、可提示）。
    """
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

CONVERT_DIR = DOWNLOAD_DIR / "conversions"
_safe_mkdir(CONVERT_DIR)
CONVERT_JOBS: dict[str, dict] = {}
CONVERT_LOCK = threading.Lock()
FFMPEG_BIN = os.environ.get("VDL_FFMPEG_BIN") or shutil.which("ffmpeg") or ("/opt/homebrew/bin/ffmpeg" if sys.platform == "darwin" else "")
# 允许的目标格式 -> ffmpeg 参数；resolution 可选 original/1080/720/480
# 视频容器按容器选型（avi 用 mp3 音轨、wmv 用原生 msmpeg4/wmav2、mpeg 用 mpeg2video/mp2、hevc 用 libx265）；
# 音频容器按编码选型（aac/opus/wma/mp2 均自带编码器）。
# 注意：ogv/ogg/oga 因捆绑 ffmpeg 缺 libvorbis/libtheora 不支持，故不纳入。
#
# 速度档说明（2026-08-28 实测 1080p 60s）：
#   libx264 ultrafast + -threads 0：约 4s（veryfast 的 2.3x）
#   libx264 veryfast + -threads 0：约 9s（基线）
#   libx264 medium  + -threads 0：约 22s
# ultrafast 加 -crf 28 控制码率，避免文件因快速编码变成 3x 大小。
# 默认走 ultrafast 满足"批量转换" 的速度诉求；要高质量请用 medium（保留 fast 兜底路径）。
# LGPL 构建不含 libx264 / libx265（均属 GPL 库），改由 codec_utils 选择
# VideoToolbox 硬件编码优先、libopenh264（BSD）软编回退。macOS 硬编比原
# libx264 ultrafast 更快，故速度档继续沿用偏快的 "fast"。
_H264_FAST = _h264_args(FFMPEG_BIN, "fast")
_HEVC_FAST = _hevc_args(FFMPEG_BIN, "fast")
CONVERT_TARGETS = {
    # ---- 视频容器（H.264 走 VideoToolbox 硬编；-threads 0 对硬编无效但无害，保留兼容）----
    "mp4":   _H264_FAST + ["-threads", "0", "-c:a", "aac", "-movflags", "+faststart"],
    "mov":   _H264_FAST + ["-threads", "0", "-c:a", "aac"],
    "mkv":   _H264_FAST + ["-threads", "0", "-c:a", "aac"],
    "webm":  ["-c:v", "libvpx-vp9", "-threads", "0", "-c:a", "libopus", "-b:v", "1M"],
    "avi":   _H264_FAST + ["-threads", "0", "-c:a", "libmp3lame"],
    "flv":   _H264_FAST + ["-threads", "0", "-c:a", "aac"],
    "ts":    _H264_FAST + ["-threads", "0", "-c:a", "aac"],
    "m4v":   _H264_FAST + ["-threads", "0", "-c:a", "aac"],
    "wmv":   ["-c:v", "msmpeg4", "-threads", "0", "-b:v", "4M", "-c:a", "wmav2"],
    "3gp":   _H264_FAST + ["-threads", "0", "-c:a", "aac"],
    "mpeg":  ["-c:v", "mpeg2video", "-threads", "0", "-b:v", "4M", "-c:a", "mp2"],
    "hevc":  _HEVC_FAST + ["-threads", "0", "-c:a", "aac", "-tag:v", "hvc1"],
    # ---- 音频容器（解码多线程收益小，默认单线程）----
    "mp3":   ["-vn", "-c:a", "libmp3lame", "-q:a", "4"],
    "m4a":   ["-vn", "-c:a", "aac", "-b:a", "192k"],
    "wav":   ["-vn", "-c:a", "pcm_s16le"],
    "flac":  ["-vn", "-c:a", "flac"],
    "aac":   ["-vn", "-c:a", "aac", "-b:a", "192k"],
    "opus":  ["-vn", "-c:a", "libopus", "-b:a", "128k"],
    "wma":   ["-vn", "-c:a", "wmav2", "-b:a", "192k"],
    "mp2":   ["-vn", "-c:a", "mp2", "-b:a", "192k"],
    # ---- 动图 ----
    "gif":   ["-t", "5", "-vf", "fps=10,scale=480:-1:flags=lanczos"],
    # ---- 图片（单帧；jpg/webp 质量档由 _image_encode_args 覆盖）----
    "png":   [],
    "jpg":   ["-q:v", "5"],
    "jpeg":  ["-q:v", "5"],
    "webp":  ["-q:v", "80"],
    "bmp":   [],
    "tiff":  [],
}
# 输出文件后缀（hevc 编码进 mp4 容器，其余与键同名）
CONVERT_EXT = {
    "mp4": "mp4", "mov": "mov", "mkv": "mkv", "webm": "webm",
    "avi": "avi", "flv": "flv", "ts": "ts", "m4v": "m4v",
    "wmv": "wmv", "3gp": "3gp", "mpeg": "mpeg", "hevc": "mp4",
    "mp3": "mp3", "m4a": "m4a", "wav": "wav", "flac": "flac",
    "aac": "aac", "opus": "opus", "wma": "wma", "mp2": "mp2",
    "gif": "gif",
    "png": "png", "jpg": "jpg", "jpeg": "jpg",
    "webp": "webp", "bmp": "bmp", "tiff": "tiff",
}
# 纯音频目标（跳过视频滤镜/缩放/旋转分支）
CONVERT_AUDIO = {"mp3", "m4a", "wav", "flac", "aac", "opus", "wma", "mp2"}
# 纯图片目标（图片转换 tab；单帧输出，无音轨）
CONVERT_IMAGE = {"png", "jpg", "jpeg", "webp", "bmp", "tiff"}
# 不支持 alpha 透明的图片目标（转码时需白底合成，避免透明区变黑）
IMAGE_NO_ALPHA = {"jpg", "jpeg", "bmp"}

# 音频目标支持的音质档位（音乐转换用；无损格式忽略码率）
_AUDIO_BITRATE_PRESETS = {"320k", "256k", "192k", "128k", "96k"}

# 音频编码器的固定前缀（无损格式不在此列，直接用 CONVERT_TARGETS）
_AUDIO_ENC_PREFIX = {
    "mp3": ["-vn", "-c:a", "libmp3lame"],
    "m4a": ["-vn", "-c:a", "aac"],
    "aac": ["-vn", "-c:a", "aac"],
    "opus": ["-vn", "-c:a", "libopus"],
    "wma": ["-vn", "-c:a", "wmav2"],
    "mp2": ["-vn", "-c:a", "mp2"],
}


def _audio_encode_args(target: str, audio_bitrate: str) -> list:
    """构造音频目标的编码参数（支持音质档位）。

    返回空列表表示无有效档位，调用方应回退到 CONVERT_TARGETS[target]。
    无损格式（wav/flac）忽略码率，直接返回固定表。
    """
    if target in ("wav", "flac"):
        return CONVERT_TARGETS[target]
    br = (audio_bitrate or "").strip().lower()
    if not br:
        return []
    enc = _AUDIO_ENC_PREFIX.get(target)
    if not enc:
        return []
    # 码率规范化：支持 "320k" / "320" / "2M"
    try:
        if br.endswith("m"):
            kbps = int(float(br[:-1]) * 1000)
        elif br.endswith("k"):
            kbps = int(br[:-1])
        else:
            kbps = int(br)
    except (ValueError, TypeError):
        return []
    kbps = max(32, min(512, kbps))
    return enc + ["-b:a", f"{kbps}k"]


def _image_encode_args(target: str, quality: int = 0, resize: int = 0,
                       flatten_alpha: bool = True) -> list:
    """构造图片目标的 ffmpeg 参数（图片转换 tab 用）。

    - quality：0-100 质量档，仅 jpg(mjpeg -q:v 2~31 反向映射) 与 webp(libwebp 直接用) 生效；
      png/bmp/tiff 无损，忽略。
    - resize：长边像素（>0 时较长边缩到该值、短边等比 -1），0=保持原尺寸。
    - flatten_alpha：jpg/bmp 不支持透明，把 RGBA 白底合成（drawbox 白底 + overlay），
      避免透明区默认变黑；png/webp/tiff 保留 alpha。
    返回插入到输出路径之前的参数列表（含 -vf / -filter_complex）。
    """
    target = "jpg" if target == "jpeg" else target
    try:
        quality = int(quality or 0)
    except (TypeError, ValueError):
        quality = 0
    try:
        resize = int(resize or 0)
    except (TypeError, ValueError):
        resize = 0
    args: list = []
    scale_expr = ""
    if resize > 0:
        # 长边缩放（只缩不放）：横图缩宽、竖图缩高，min() 保证原图更小时保持原尺寸
        scale_expr = (f"scale='if(gt(iw,ih),min({resize},iw),-1)':'if(gt(ih,iw),min({resize},ih),-1)'")
    if target in IMAGE_NO_ALPHA and flatten_alpha:
        # 白底合成（filter_complex 带 label，不能用 -vf 链）：scale 后 split 出
        # 背景 drawbox 填白，再把带 alpha 的前景 overlay 回去
        graph = ("[0:v]" + (scale_expr + "," if scale_expr else "")
                 + "format=rgba,split=2[fg][bg];"
                 "[bg]drawbox=t=fill:c=white[bgw];"
                 "[bgw][fg]overlay=format=auto[out]")
        args += ["-filter_complex", graph, "-map", "[out]"]
    elif scale_expr:
        args += ["-vf", scale_expr]
    if target == "jpg":
        if quality > 0:
            # UI 质量 100→最好，mjpeg -q:v 1(最好)~31(最差)：线性反向映射，钳到 [2,31]
            qscale = max(2, min(31, round(31 - (quality / 100.0) * 30)))
            args += ["-q:v", str(qscale)]
    # 图片目标必须只产出一帧（即本函数注释所称的「单帧输出」）。若源其实是多帧
    # 视频（桌面端原生文件选择器不受 accept="image/*" 约束，可能被放进视频），
    # 缺少此参数会让 ffmpeg 试图把多帧写进同一个文件而整体报错：第一帧虽已落盘，
    # 任务状态却是 failed，用户反而拿不到产物。单帧图片源加与不加完全等效。
    args += ["-frames:v", "1"]
    return args


def _convert_image_webp(src: str, out: "Path", quality: int = 0, resize: int = 0) -> None:
    """WebP 转码（图片转换 tab）：捆绑的 LGPL ffmpeg 没有 libwebp 编码器，
    用 Pillow（MIT-CMU 许可，可商用，已随包捆绑）直接编码。保留 alpha 透明。"""
    from PIL import Image
    img = Image.open(src)
    try:
        resize = int(resize or 0)
    except (TypeError, ValueError):
        resize = 0
    if resize > 0 and max(img.size) > resize:
        scale = resize / float(max(img.size))   # 只缩不放
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                         Image.LANCZOS)
    try:
        quality = int(quality or 0)
    except (TypeError, ValueError):
        quality = 0
    img.save(out, "WEBP", quality=max(1, min(100, quality or 80)), method=4)

# ---- 本地视频上传转码（需求文档模块一）：接收上传文件直接转码，复用上面的 ffmpeg 管线 ----
UPLOAD_TMP = DOWNLOAD_DIR / "uploads"
_safe_mkdir(UPLOAD_TMP)
# 上传文件大小上限（字节），默认 2GB，可用 VDL_UPLOAD_MAX_BYTES 覆盖
UPLOAD_MAX_BYTES = int(os.environ.get("VDL_UPLOAD_MAX_BYTES") or 2_000_000_000)
# 单个分片大小上限（字节），前端大文件用 64MB 分片、常规 32MB；超过即 413 拒绝。
# 注意：convert.py 的 upload_chunk 路由引用此常量，缺省会令所有分片上传 500（含音乐 tab / 视频转换 tab 的网页上传路径）。
UPLOAD_CHUNK_MAX = 64 * 1024 * 1024
# 允许上传的视频后缀白名单
UPLOAD_VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".m4v", ".ts", ".wmv", ".mpeg", ".mpg"}
# 允许上传的音频后缀白名单（桥接/合并模块复用分片上传 mode=store）
UPLOAD_AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".oga", ".wma"}
# 允许上传的图片后缀白名单（图片转换 tab；.gif 同时可作图片输入）
UPLOAD_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}

# ---- PDF / 图片去水印（需求文档模块二）：接收上传图片/PDF 做去水印，依赖 cv2/fitz（缺则降级） ----
DW_DIR = DOWNLOAD_DIR / "dewatermark"
_safe_mkdir(DW_DIR)
# ---- 本地视频字幕提取（faster-whisper ASR，MIT）：SRT/TXT 产物目录 ----
SUBTITLE_DIR = DOWNLOAD_DIR / "subtitles"
_safe_mkdir(SUBTITLE_DIR)
DW_JOBS: dict[str, dict] = {}
DW_LOCK = threading.Lock()

# ---- 双节点分流：国内节点直连国内站，海外节点直连海外站，前端按链接域名自动选 ---- #
# VDL_REGION: 本节点所在区域，"cn"=国内 / "global"=海外（默认海外）
# VDL_PEER_ENDPOINT: 对端节点的完整地址，如 https://cn.example.com（留空=单节点模式）
# VDL_ALLOW_ORIGINS: 允许跨域访问本节点 API 的来源，逗号分隔；"*" 表示全部
NODE_REGION = (os.environ.get("VDL_REGION", "global").strip().lower() or "global")
PEER_ENDPOINT = os.environ.get("VDL_PEER_ENDPOINT", "").strip().rstrip("/")
_allow_raw = os.environ.get("VDL_ALLOW_ORIGINS", "").strip()
ALLOW_ORIGINS = [o.strip().rstrip("/") for o in _allow_raw.split(",") if o.strip()] or ([PEER_ENDPOINT] if PEER_ENDPOINT else [])
# 海外站（走代理）：2026-09-26 从 40 提到 90 —— 此前 40s 因事件循环被 Chrome Cookie
# 同步解密卡死而从未真正生效（计时器被冻结）；Cookie 挪进 executor 并行后超时恢复
# 真实作用，YouTube 代理链路实测 25~50s（player.js 拉取慢时更久），40s 会开始误杀。
# 仍需 < 前端 fetch 默认 120s（web/app.js request()），否则前端先掐断报「连接本地服务失败」。
RESOLVE_TIMEOUT_SECONDS = int(os.environ.get("VDL_RESOLVE_TIMEOUT_OVERSEAS", "90"))
# 国内站：腾讯/优酷/B站等直连本可很快，但抖音/快手/微博/爱奇艺等 14 个平台
# 依赖云端 Playwright worker（经隧道起 Chromium 做浏览器级解析，实测 25~50s），
# 原先的 20s 会稳定误报「解析超时」→ 对齐网页端取 60s，可用环境变量覆盖。
RESOLVE_TIMEOUT_DOMESTIC = int(os.environ.get("VDL_RESOLVE_TIMEOUT_DOMESTIC", "60"))
SSE_INTERVAL_SECONDS = 0.5
SSE_MAX_SECONDS = 60 * 30
CLEANUP_INTERVAL_SECONDS = 600

# ---- 自动解说（增值功能）：松耦合桥接用户现成的 commentary-pipeline ----
# 复用 commentary-pipeline/process.py 整条管线（转写→配音→出片），本服务只负责
# 把下载好的视频喂进它的 input/、等成片回传，绝不重写解说逻辑。
# 默认关闭：需显式开启 + 配置管线目录，且解说 worker 应在独立机器/进程跑，别和下载抢 CPU。
#   VDL_COMMENTARY_ENABLED=true                      启用
#   VDL_COMMENTARY_DIR=/path/to/commentary-pipeline  管线项目根目录（需有 process.py + input/ + output/）
#   VDL_COMMENTARY_PYTHON=/path/to/python            跑 process.py 的解释器（需装 faster_whisper 等依赖）
#   VDL_COMMENTARY_VOICE=zh-CN-XiaoxiaoNeural        默认配音嗓音
_COMMENTARY_EXPLICIT = "VDL_COMMENTARY_ENABLED" in os.environ
COMMENTARY_ENABLED = os.environ.get("VDL_COMMENTARY_ENABLED", "false").strip().lower() == "true"
# 桌面版自动探测：没显式设 COMMENTARY_ENABLED 时，若管线可用则自动开启。
# 仅 frozen(打包版)生效——dev/在线版须显式开启，避免 import 即改写默认行为。
# 统一走 commentary_locate.locate_commentary()（含包内捆绑候选，践行自包含铁律）。
if not _COMMENTARY_EXPLICIT and plat.is_desktop():
    from commentary_locate import locate_commentary
    _loc = locate_commentary()
    if _loc is not None:
        COMMENTARY_ENABLED = True
        if "VDL_COMMENTARY_DIR" not in os.environ:
            os.environ["VDL_COMMENTARY_DIR"] = str(_loc.root)
        if "VDL_COMMENTARY_MODE" not in os.environ:
            # 包内捆绑走 worker 重入(#198 实现)；外部/显式走 local 子进程
            os.environ["VDL_COMMENTARY_MODE"] = "bundled" if _loc.bundled else "local"

# 解说运行环境探测已集中到 _CommentaryRuntime（见下方 _commentary_work_dir 之前的定义），
# 模块加载时一次性解析解释器与工具链，并暴露清晰的诊断信息，避免打包后路径截断导致静默失败。

# 广告位开关：默认关闭。下载站属广告平台高风险类目，默认不挂广告，
# 待流量稳定、确定接入合规广告源后再开。前端据此决定是否渲染广告位容器。
ADS_ENABLED = os.environ.get("VDL_ADS_ENABLED", "false").strip().lower() == "true"
# ---- 格式转换订阅开关（增值能力变现，默认关闭以保持开源全免费体验）----
# 部署者设 VDL_CONVERT_REQUIRE_SUB=true 并填 VDL_CONVERT_SUB_KEY 后进入「订阅墙」模式：
#   免费用户每日限 VDL_CONVERT_FREE_DAILY 次（按客户端 IP 计），超出需订阅；
#   请求头携带正确 X-Subscription-Key 的用户不限次。
# 两个开关任一为空/未设 → 视为未启用订阅墙，所有人免费无限（开源默认）。
CONVERT_REQUIRE_SUB = os.environ.get("VDL_CONVERT_REQUIRE_SUB", "false").strip().lower() == "true"
CONVERT_SUB_KEY = os.environ.get("VDL_CONVERT_SUB_KEY", "").strip()
CONVERT_FREE_DAILY = int(os.environ.get("VDL_CONVERT_FREE_DAILY", "3") or 3)
CONVERT_SUB_ENABLED = CONVERT_REQUIRE_SUB and bool(CONVERT_SUB_KEY)
_convert_quota: dict[str, dict] = {}      # ip -> {"date": "YYYY-MM-DD", "count": int}
_convert_quota_lock = threading.Lock()
# ---- 下载订阅开关（整体 freemium：免费每日限次、订阅无限；默认关闭保持全免费）----
# 与格式转换共用同一把订阅密钥 VDL_CONVERT_SUB_KEY（一个订阅解锁全部增值能力）。
# 单独开关 VDL_DOWNLOAD_REQUIRE_SUB 控制「是否对下载也启用限次」；
# VDL_DOWNLOAD_FREE_DAILY 设置免费用户每日可创建的任务数（默认 10，核心功能给得比转换宽松）。
DOWNLOAD_REQUIRE_SUB = os.environ.get("VDL_DOWNLOAD_REQUIRE_SUB", "false").strip().lower() == "true"
DOWNLOAD_FREE_DAILY = int(os.environ.get("VDL_DOWNLOAD_FREE_DAILY", "10") or 10)
DOWNLOAD_SUB_ENABLED = DOWNLOAD_REQUIRE_SUB and bool(CONVERT_SUB_KEY)
_download_quota: dict[str, dict] = {}     # ip -> {"date": "YYYY-MM-DD", "count": int}
_download_quota_lock = threading.Lock()

# ---- 格式 / 片段增强（桌面版本地加工）：基于 lib_id 的 ffmpeg 任务 ----
# process_queue 在 executor 创建后初始化（见下方）
_commentary_dir_raw = os.environ.get("VDL_COMMENTARY_DIR", "").strip()
COMMENTARY_DIR = Path(_commentary_dir_raw) if _commentary_dir_raw else None
# 解说 Python 解释器与工具链(ffmpeg/ffprobe)由下方 _CommentaryRuntime 在模块加载时集中探测。
COMMENTARY_VOICE = os.environ.get("VDL_COMMENTARY_VOICE", "zh-CN-XiaoxiaoNeural").strip() or "zh-CN-XiaoxiaoNeural"
COMMENTARY_TIMEOUT_SECONDS = int(os.environ.get("VDL_COMMENTARY_TIMEOUT", "7200") or 7200)  # 长视频 + whisper 大模型首跑 + edge-tts 排队，默认 2 小时
# 解说 worker 调用模式：local=同机 subprocess(默认) / http=独立 HTTP worker 服务(强机独立部署)
COMMENTARY_MODE = os.environ.get("VDL_COMMENTARY_MODE", "local").strip().lower()
COMMENTARY_ENDPOINT = os.environ.get("VDL_COMMENTARY_ENDPOINT", "").strip().rstrip("/")
COMMENTARY_TOKEN = os.environ.get("VDL_COMMENTARY_TOKEN", "").strip()  # 与 worker 的 WORKER_TOKEN 对应
_HERE = Path(__file__).resolve().parent

def _commentary_is_bundled() -> bool:
    """是否走「单二进制双角色」包内捆绑模式（worker 重入自身，依赖随包内置）。"""
    return os.environ.get("VDL_COMMENTARY_BUNDLED") == "1" or COMMENTARY_MODE == "bundled"

def _commentary_root(sub: str) -> Path:
    """解说管线工作目录(input/output/work)；bundled 模式重定向到可写目录(COMMENTARY_WORK_ROOT)。"""
    wr = os.environ.get("COMMENTARY_WORK_ROOT") or ""
    root = Path(wr) if wr else (COMMENTARY_DIR or Path("."))
    return root / sub
_COMMENTARY_OUT_RAW = os.environ.get("VDL_COMMENTARY_LOCAL_OUTPUT", "").strip()
def _user_data_dir() -> Path:
    """跨平台用户数据目录：Windows 打包后用 %APPDATA%/VideoDownloader（符合 Windows 规范，
    避免凭据被同机其他用户读取）；macOS/Linux 维持 ~/.video-downloader 以兼容现有用户。"""
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        return Path(os.environ.get("APPDATA", Path.home())) / "VideoDownloader"
    return Path.home() / ".video-downloader"

if _COMMENTARY_OUT_RAW:
    COMMENTARY_LOCAL_OUTPUT = Path(_COMMENTARY_OUT_RAW)
elif getattr(sys, "frozen", False):
    # 打包后 _HERE 位于 .app 包内部，若沿用会把上传的大视频写进 /Applications/*.app，
    # 导致替换 app 即丢数据、包体积暴涨、破坏代码签名。一律落到用户目录。
    COMMENTARY_LOCAL_OUTPUT = _user_data_dir() / "commentary_out"
else:
    COMMENTARY_LOCAL_OUTPUT = _HERE.parent / "commentary_out"
COMMENTARY_LOCAL_OUTPUT.mkdir(parents=True, exist_ok=True)
COMMENTARY_WORK_DIR = COMMENTARY_LOCAL_OUTPUT / "work"
COMMENTARY_WORK_DIR.mkdir(parents=True, exist_ok=True)
# 解说 work 目录保留天数（每个 job 是 12 位 hex 目录：upload 副本 + 中间 wav/脚本）。
# 没有清理机制时会长年累积（实测达 21GB），这里按保留期自动清理，可用
# VDL_COMMENTARY_WORK_RETENTION_DAYS 覆盖（默认 7 天）。
COMMENTARY_WORK_RETENTION_DAYS = max(1, int(os.environ.get("VDL_COMMENTARY_WORK_RETENTION_DAYS", "7") or 7))
commentary_jobs: dict[str, dict] = {}
_commentary_lock = threading.Lock()

# ---- 解说视频「接收站」 cache-by-hash：让用户拖过的本地视频不再重复上传 ----
# 把 upload 端点的视频流按 sha256(前 16 位) 落到本目录；重复上传同视频时直接复用
# 这份磁盘文件，0 字节传输。前端拿到 stash_id 后继续走 JSON 提交（不再 multipart）。
# 设计取舍：
#   • 与 _commentary_work_dir 隔离：work 是 job 临时产物（中间 wav/脚本），stash 是用户
#     源视频的「冷缓存」，二者生命周期不同、清理策略不同。
#   • id 形态 `stash:<16hex>`：与 library_mod.encode_id 形状互不冲突，前端 payload
#     完全不变，后端 _resolve_source 看到前缀后转查 stash 索引即可。
#   • 16 位 = 64 bit 哈希 → 误撞概率可忽略（每天累计 GB 级视频）；磁盘按单文件存。
COMMENTARY_STASH_DIR = COMMENTARY_LOCAL_OUTPUT / "stash"
COMMENTARY_STASH_DIR.mkdir(parents=True, exist_ok=True)
COMMENTARY_STASH_RETENTION_DAYS = max(
    1, int(os.environ.get("VDL_COMMENTARY_STASH_RETENTION_DAYS", "14") or 14)
)
_commentary_stash_lock = threading.Lock()
_commentary_stash: dict[str, dict] = {}  # sha16 → {path, size, mtime, name, ext}

_STASH_VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv",
                     ".m4v", ".ts", ".wmv", ".mpeg", ".mpg"}

def _stash_path_for(sha: str, ext: str) -> Path:
    safe_ext = ext.lower() if ext.lower() in _STASH_VIDEO_EXTS else ".mp4"
    # 进一步保险：限制 sha 只含 hex，避免任意路径在后续 _resolve_safe 误读
    safe_sha = "".join(c for c in sha if c in "0123456789abcdef")[:16].ljust(16, "0")
    return COMMENTARY_STASH_DIR / f"{safe_sha}{safe_ext}"

def _stash_register(sha: str, ext: str, src_name: str = "") -> str:
    """注册一份已落盘的 stash；存在则只刷 mtime。返回 'stash:<sha>'。"""
    p = _stash_path_for(sha, ext)
    if not p.exists():
        raise RuntimeError(f"stash 文件不存在：{p}")
    with _commentary_stash_lock:
        _commentary_stash[sha] = {
            "path": str(p),
            "size": p.stat().st_size,
            "mtime": time.time(),
            "name": src_name,
            "ext": p.suffix.lower(),
        }
    return f"stash:{sha}"

def _stash_lookup(sha: str) -> dict | None:
    """查 stash 索引并续命中时间；磁盘文件丢失自动注销。"""
    with _commentary_stash_lock:
        m = _commentary_stash.get(sha)
    if not m:
        # 进程重启后内存索引为空——尝试按 sha 直接在磁盘上找，命中同样回写索引
        for ext in _STASH_VIDEO_EXTS:
            cand = COMMENTARY_STASH_DIR / f"{sha}{ext}"
            if cand.exists():
                m = {
                    "path": str(cand),
                    "size": cand.stat().st_size,
                    "mtime": cand.stat().st_mtime,
                    "name": "",
                    "ext": ext,
                }
                with _commentary_stash_lock:
                    _commentary_stash[sha] = m
                break
    if not m:
        return None
    if not Path(m["path"]).exists():
        with _commentary_stash_lock:
            _commentary_stash.pop(sha, None)
        return None
    with _commentary_stash_lock:
        _commentary_stash[sha]["mtime"] = time.time()
    return m

def _purge_commentary_stash(now: float | None = None) -> int:
    """按 mtime 删除超期 stash 文件 + 同步内存索引。返回删除条数。"""
    now = now or time.time()
    cutoff = now - COMMENTARY_STASH_RETENTION_DAYS * 86400
    removed = 0
    if COMMENTARY_STASH_DIR.exists():
        for f in list(COMMENTARY_STASH_DIR.iterdir()):
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                pass
    with _commentary_stash_lock:
        for k in list(_commentary_stash.keys()):
            p = Path(_commentary_stash[k]["path"])
            if not p.exists():
                _commentary_stash.pop(k, None)
    return removed

# ---- AI 去水印（E2FGVI worker，桌面版可选）：local subprocess 或 http worker ----
AI_DEWATERMARK_ENABLED = bool(
    plat.is_desktop()
    or bool(os.environ.get("VDL_AI_DEWATERMARK_ENABLED"))
)
AI_DEWATERMARK_MODE = os.environ.get("VDL_AI_DEWATERMARK_MODE", "local").strip().lower()
AI_DEWATERMARK_DIR_RAW = os.environ.get("VDL_AI_DEWATERMARK_DIR", "").strip()
AI_DEWATERMARK_DIR = Path(AI_DEWATERMARK_DIR_RAW) if AI_DEWATERMARK_DIR_RAW else None
AI_DEWATERMARK_PYTHON = os.environ.get("VDL_AI_DEWATERMARK_PYTHON", sys.executable)
AI_DEWATERMARK_ENDPOINT = os.environ.get("VDL_AI_DEWATERMARK_ENDPOINT", "http://127.0.0.1:8101")
AI_DEWATERMARK_TOKEN = os.environ.get("VDL_AI_DEWATERMARK_TOKEN", "").strip()
AI_DEWATERMARK_TIMEOUT = int(os.environ.get("VDL_AI_DEWATERMARK_TIMEOUT", "3600") or 3600)

# 启动时检测 GPU（用于 /api/nodes 告知前端）
def _detect_gpu() -> bool:
    try:
        import torch  # noqa: F401
        return torch.cuda.is_available() or (
            hasattr(torch.backends, "mps") and torch.backends.mps.is_available()
        )
    except ImportError:
        return False

AI_GPU_AVAILABLE = _detect_gpu() if AI_DEWATERMARK_ENABLED else False

# 图片 AI 去水印（LaMa ONNX）是否可用：需 cv2 + numpy + onnxruntime。
# 与视频 AI 去水印（E2FGVI worker）相互独立；图片走本地 onnxruntime 推理，免 torch。
# 桌面端不强制启动 prewarm（保持 App 启动轻盈），首次选 AI 时由 dewatermark_ai._ensure_model 惰性下载。
try:
    import dewatermark_ai as _dwc_ai_mod
    dwc_ai_available = _dwc_ai_mod.available()
except Exception:  # noqa: BLE001
    dwc_ai_available = False

# ---- 公开部署护栏：防止实例被当免费下载器薅爆带宽 ---- #
# 设为 0 表示不限制（自托管、内部使用时可关掉）
RATE_LIMIT_PER_HOUR = int(os.environ.get("VDL_RATE_LIMIT_PER_HOUR", "30") or 30)
RATE_LIMIT_WINDOW = 3600
_rate_log: dict[str, list[float]] = {}
_rate_lock = threading.Lock()

def _client_ip(request: Request) -> str:
    """取真实客户端 IP（限流桶键 / 日志用），避免伪造 X-Forwarded-For 绕过限流。

    优先级（安全审计 2026-10-11 P2-17 重排）：
      ① `CF-Connecting-IP` —— Cloudflare 自己写入的权威客户端地址（用户无法伪造）；
      ② `X-Real-IP` —— nginx 的 `$remote_addr`；
      ③ `X-Forwarded-For` 的最近一跳（最右）与原始客户端（最左）；
      ④ 直连 socket 地址。
    规则：**优先返回第一个非环回候选**；只有全部候选都是环回才按本机处理。

    🔴 为什么必须这么改：线上走 Cloudflare 命名隧道时，nginx 看到的 `$remote_addr`
    是 **127.0.0.1**（cloudflared 从本机发起连接），于是 `X-Real-IP` 恒为 127.0.0.1
    → `_is_loopback_ip` 判为环回 → **全站限流（30 次/小时）在隧道入口整体失效**。
    旧实现只取 `X-Real-IP` 单值，正好踩中这一条。
    """
    cands: list[str] = []
    cf = (request.headers.get("cf-connecting-ip") or "").strip()
    if cf:
        cands.append(cf.split(",")[0].strip())
    real = (request.headers.get("x-real-ip") or "").strip()
    if real:
        cands.append(real.split(",")[0].strip())
    fwd = (request.headers.get("x-forwarded-for") or "").strip()
    if fwd:
        parts = [p.strip() for p in fwd.split(",") if p.strip()]
        if parts:
            cands.append(parts[-1])      # 反代追加的最近一跳（用户无法伪造最右）
            cands.append(parts[0])       # 隧道场景下的原始客户端
    for c in cands:
        if c and not _is_loopback_ip(c):
            return c
    for c in cands:
        if c:
            return c
    return request.client.host if request.client else "unknown"


def _is_loopback_ip(ip: str) -> bool:
    """环回地址（桌面 App 自连 127.0.0.1 / ::1 / localhost）判定的唯一真源。

    桌面 App 是单用户本机程序：前端（pywebview/WKWebView）所有请求都来自
    127.0.0.1，与本机 FastAPI 后端共用同一个环回 IP。若对它套用「每 IP 限流」，
    一台机器的全部正常操作会挤进同一个桶，30 次/小时的上限极易打满，导致用户
    在做解析/下载/转码时频繁收到「下载太频繁了」的 429（2026-09-22 用户实测反馈）。
    环回地址不存在被外部薅带宽的滥用场景，故一律豁免；只有真实远端 IP 才受限。

    ⚠️ 2026-10-11 修正：原先这里写「经 Cloudflare / nginx 反代时 _client_ip 会取到
    真实远端地址，不会误判为环回」——**该前提在 CF 隧道下不成立**（nginx 的
    `$remote_addr` 是 127.0.0.1）。真正的保障已移入 `_client_ip`（优先非环回候选），
    本函数只负责「一个字符串是不是环回地址」这一件事。
    """
    if ip in ("127.0.0.1", "::1", "localhost"):
        return True
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


# ---- SSRF 防护：拒绝指向内网 / 环回 / 链路本地 / 云元数据的链接 ----
# 视频站都是公网域名；攻击者若传入内网地址（如 169.254.169.254 云元数据），
# 服务器会去请求并可能泄露凭据，或被当成跳板。入口强制只允许公网可达地址。
_PRIVATE_NETS = [
    ipaddress.ip_network(n) for n in (
        "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
        "172.16.0.0/12", "192.0.0.0/24", "192.168.0.0/16", "198.18.0.0/15",
        "::1/128", "fc00::/7", "fe80::/10",
    )
]

def _assert_safe_url(url: str) -> None:
    """SSRF 护栏：解析主机名，拒绝落在私有 / 环回 / 链路本地 / 保留网段的地址。"""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        raise LinkError("链接缺少主机名", "请检查链接是否完整")
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        raise LinkError("该主机不在允许范围内", "请粘贴公开可访问的视频链接")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # 安全审计 2026-10-11（R-03）：原实现解析失败直接放行（fail-open），攻击者可借
        # 「检查时 DNS 失败、下载时再解析到内网」的重绑定手法绕过护栏。改为 fail-closed；
        # 为降低偶发 DNS 抖动误杀，先重试一次，仍失败才拒绝。
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            raise LinkError("域名解析失败", "无法解析该域名，请稍后重试或更换链接")
    # DNS 污染 / 代理 / CDN 场景下，getaddrinfo 常同时返回「真实公网 IP」和
    # 「保留 / 链路本地假地址」(如本机 youtube.com 解析出 2001::1 这类 Teredo 保留地址)。
    # 只要存在任一公网可达地址即视为合法公开域名，避免被假地址一票否决（与 /api/download
    # 去掉护栏的考量一致）。仅当【所有】解析地址都落在私网 / 环回 / 链路本地 / 保留 /
    # 组播段时才拒绝——这才是真正的内部地址攻击（如 169.254.169.254 云元数据）。
    has_public = False
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        # 安全审计 2026-10-11（R-03）：显式叠加 _PRIVATE_NETS 判定，补齐 is_private 在
        # 部分网段（如 100.64.0.0/10 CGNAT —— 实测 Python 3.13 is_private=False）上的遗漏。
        if any(addr in _net for _net in _PRIVATE_NETS):
            continue
        if not (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast):
            has_public = True
            break
    if not has_public:
        raise LinkError(
            "该链接指向非公开网络，已拒绝",
            "只允许下载公开可访问的视频；内网 / 本地 / 云元数据地址不可用",
        )

# --------------------------------------------------------------------------- #
# 本机路径「可外传/可渲染」护栏（安全审计 2026-10-11，P2-14 / P2-15）
# --------------------------------------------------------------------------- #
# `/api/share/upload_path`（把任意本机文件传上**公网短链**）与
# `/api/pagetool/build`（把任意本机文件读成自包含 HTML，并在其同目录写产物）
# 此前都接受**任意绝对路径** —— 等于对外提供了一个「读任意文件并外传」的原语：
# 拿到本机端口即可把 ~/.ssh/id_rsa、users.json、.auth_secret 之类传成公开链接。
# 两者都是桌面端本机入口（网页版因 nginx 路由不可达），但端口暴露场景下就是实打实的
# 任意文件读取。这里给出一条共用护栏。
#
# 双层判定：**用户目录白名单**（与 routers/convert.py 的 `_resolve_safe_local_path`
# 同口径，保证正常素材/文档不受影响）+ **敏感位置黑名单**（密钥与配置几乎都住在
# 点开头目录 / 应用数据目录 / 钥匙串目录里）。
_SENSITIVE_FILE_RE = re.compile(
    r"^(\.auth_secret"
    r"|users\.json|cards\.json|admin\.json|smtp\.json|share\.json|vault\.json"
    r"|tasks_state\.json|quota\.json|membership.*\.json|membership_plans\.json"
    r"|id_rsa.*|id_ed25519.*|id_ecdsa.*"
    r"|.*\.(pem|key|p12|pfx|jks|keystore|kdbx|ppk|env))$",
    re.IGNORECASE)
_SENSITIVE_DIR_PREFIXES = (
    "/Library/Keychains", "/etc/", "/private/etc/", "/var/", "/System/",
    "/usr/", "/bin/", "/sbin/", "C:\\Windows", "C:\\Program Files",
)
# 用户主目录下的敏感**路径段**（按段比对，不看前缀）。
# 🔴 验证轮补充（P3-2）：只按前缀拦 `/Library/Keychains` 是不够的 —— macOS 的
#   登录钥匙串与浏览器凭证库都在**用户级** `~/Library/...` 下
#   （`~/Library/Keychains/login.keychain-db`、
#    `~/Library/Application Support/Google/Chrome/Default/Cookies`、
#    `~/Library/Cookies/...`），它们落在 `/Users/` 白名单内、也不是点目录，
#   于是能通过「上传成公网短链 / 渲染成网页」把本机凭证带走。
#   任何正常用途都不会去分享 `~/Library` 下的文件，故整段封禁。
_SENSITIVE_DIR_COMPONENTS = frozenset({
    "Library", "AppData", "Keychains", "Keychain",
    "Application Support", "Cookies", "Safari", "Containers",
})
# 允许被外传/渲染的根前缀（用户目录口径，与 routers/convert.py 的本机路径白名单一致）。
# 独立成常量便于测试注入临时目录。
_SHAREABLE_PATH_PREFIXES = ("/Users/", "/home/", "/Volumes/", "C:\\", "D:\\")


def _assert_shareable_local_path(raw: str) -> Path:
    """校验「允许被外传 / 被渲染成网页」的本机路径，返回 resolve 后的 Path。

    违规一律抛 HTTPException（400 语义错误 / 403 安全拒绝）。
    """
    text = str(raw or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="缺少文件路径")
    try:
        rp = Path(os.path.expanduser(text)).resolve()
    except Exception:
        raise HTTPException(status_code=400, detail="无法解析路径：%s" % text)
    if not rp.is_file():
        raise HTTPException(status_code=400, detail="文件不存在或不是普通文件：%s" % text)

    # ① 用户目录白名单（与 convert 的本机路径口径一致）
    if not str(rp).startswith(_SHAREABLE_PATH_PREFIXES):
        raise HTTPException(status_code=403, detail="只允许处理用户目录下的文件")

    # ② 任何点开头的路径段：密钥 / 配置 / 凭证几乎都住在点目录里
    #    （.ssh / .gnupg / .aws / .config / .video-downloader / .videodownloader / .vdl-license…）
    if any(seg.startswith(".") for seg in rp.parts[1:]):
        raise HTTPException(status_code=403,
                            detail="出于安全考虑，不允许处理隐藏目录下的文件")

    # ②b 用户主目录下的敏感路径段（钥匙串 / 浏览器凭证 / 应用容器）
    if any(seg in _SENSITIVE_DIR_COMPONENTS for seg in rp.parts[1:]):
        raise HTTPException(status_code=403,
                            detail="出于安全考虑，不允许处理系统 / 凭证类目录下的文件")

    # ③ 应用自身数据目录（账号表 / 令牌 / 保险箱 / 订单 / 授权中心数据）
    try:
        from auth_store import _base_dir as _auth_base
        rp.relative_to(_auth_base().resolve())
        raise HTTPException(status_code=403, detail="不允许处理应用数据目录下的文件")
    except HTTPException:
        raise
    except Exception:
        pass

    # ④ 系统与钥匙串目录
    if str(rp).startswith(_SENSITIVE_DIR_PREFIXES):
        raise HTTPException(status_code=403, detail="不允许处理系统目录下的文件")

    # ⑤ 密钥 / 账号类文件名
    if _SENSITIVE_FILE_RE.match(rp.name):
        raise HTTPException(status_code=403,
                            detail="出于安全考虑，不允许处理密钥 / 账号类文件")

    return rp


# --------------------------------------------------------------------------- #
# 「本机直连」判定（安全审计 2026-10-11，P3-1）
# --------------------------------------------------------------------------- #
# 🔴 为什么不能用 `request.client.host` 单值判定：
#   uvicorn 默认开启 `--proxy-headers`（Procfile 里没有关掉它），此时 `scope["client"]`
#   已被 `ProxyHeadersMiddleware` 用 `X-Forwarded-For` **重写**过 —— 不再是 socket 对端。
#   而 uvicorn 0.52 的取法是「从右往左取第一个**不受信**的 host；若全部受信则回退最左」
#   （`_TrustedHosts.get_trusted_client_address`，受信集默认 `{"127.0.0.1"}`）。
#   于是链路里只要有一跳是环回（CF 命名隧道下 cloudflared 与 nginx 同机，
#   nginx 的 `$remote_addr` 恰为 127.0.0.1），客户端自填 `X-Forwarded-For: 127.0.0.1`
#   就能让推导结果落在受信集内 → `client.host` 变成 127.0.0.1 → 被当成「本机」。
#
#   受影响的是所有「按本机免鉴权」的分支：
#     · `routers/core.py::_is_local_request` → `_require_admin`（运维端点免密钥）、`_require_local`
#     · `routers/auth.py::_is_loopback` → dev 模式**回传密码重置验证码**（账号接管）、
#       `ensure_superusers(allow_bootstrap=...)`（公网首用户自动提权）
#     · `app.cookie_sync_from_local`（读本机浏览器登录态并上报公共池）
#
#   判据改为：**桌面 App 的 WebView 直连 127.0.0.1、不带任何代理头**；
#   凡带代理头者一律不按本机对待。这条判据不依赖任何反代语义，因此可离线验证。
#   （`routers/system.py` 的代理设置端点早就这么写了，本函数把它抽成共用真源。）
_PROXY_HEADER_NAMES = (
    "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "x-forwarded-port",
    "x-real-ip", "forwarded", "true-client-ip", "x-client-ip",
    "x-cluster-client-ip", "x-original-forwarded-for",
    "cf-connecting-ip", "cf-ray", "cf-ipcountry",
)


def _has_proxy_headers(request) -> bool:
    """请求是否携带任何反代 / 隧道头（带 → 说明不是本机直连）。"""
    try:
        for name in _PROXY_HEADER_NAMES:
            if (request.headers.get(name) or "").strip():
                return True
    except Exception:
        return True          # 取不到头就按「经过代理」从严处理
    return False


def _is_local_direct(request) -> bool:
    """是否为本机直连（socket 对端环回 **且** 未携带任何代理头）。

    这是全仓「本机免鉴权」判定的唯一真源，路由层不要再自己写 client.host 比较。
    """
    if request is None or _has_proxy_headers(request):
        return False
    try:
        host = (request.client.host if request.client else "") or ""
    except Exception:
        return False
    return host in ("127.0.0.1", "::1", "localhost")


# 安全审计 2026-10-11（R-02）：跨源 CSRF 兜底校验的来源白名单。
# 仅放行「本机来源」与「浏览器扩展来源」；无 Origin（同源前端 / 本机直连）放行。
_EXT_ORIGIN_RE = re.compile(r"^(chrome|moz|safari-web|ms-browser)-extension://[^\s]*$")
_LOCAL_ORIGIN_RE = re.compile(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$")


def _cross_site_rejected(request) -> bool:
    """安全审计 2026-10-11（R-02）：判断是否应拒绝该跨源请求（CSRF 兜底）。

    全局 CORS 默认对任意 Origin 放行（浏览器扩展 content script 从任意 https
    页面回传嗅探项所必需，见 routers/sniffer.py），因此对**仅前端同源使用**的
    敏感端点（改配置类），必须额外做来源校验，防止恶意网页改写 base_url
    劫持凭据 / 外传用户内容：

      - 无 Origin 头 → 放行（同源前端、本机直连 curl）；
      - Origin 命中本机 / 扩展白名单 → 放行；
      - 其余（含 "null" 的沙箱 iframe、任意外部站点）→ 拒绝。
    """
    try:
        origin = (request.headers.get("origin") or "").strip()
    except Exception:
        return False
    if not origin:
        return False
    return not (_EXT_ORIGIN_RE.match(origin) or _LOCAL_ORIGIN_RE.match(origin))


def _check_rate_limit(request: Request) -> None:
    """滑动窗口限流。超限抛 429，并告知还要等多久。

    环回客户端（桌面 App 自连）豁免，详见 _is_loopback_ip。
    """
    if RATE_LIMIT_PER_HOUR <= 0:
        return
    ip = _client_ip(request)
    if _is_loopback_ip(ip):
        return
    now = time.time()
    with _rate_lock:
        hits = [t for t in _rate_log.get(ip, []) if now - t < RATE_LIMIT_WINDOW]
        if len(hits) >= RATE_LIMIT_PER_HOUR:
            wait = int((RATE_LIMIT_WINDOW - (now - hits[0])) / 60) + 1
            _rate_log[ip] = hits
            raise HTTPException(
                status_code=429,
                detail=f"下载太频繁了，本实例每小时限 {RATE_LIMIT_PER_HOUR} 次，请 {wait} 分钟后再试；"
                       "需要更高额度可以自己部署一份（见项目 README）",
            )
        hits.append(now)
        _rate_log[ip] = hits
        if len(_rate_log) > 10000:        # 兜底清理，避免长期运行内存堆积
            for key in [k for k, v in _rate_log.items() if not v or now - v[-1] > RATE_LIMIT_WINDOW]:
                _rate_log.pop(key, None)

def _today_str() -> str:
    """当前服务器本地日期 YYYY-MM-DD，用于按自然日重置免费额度。"""
    return time.strftime("%Y-%m-%d", time.localtime())

def _subscription_quota(request, *, enabled, sub_key, free_daily, quota_store, quota_lock, label):
    """通用订阅 / 限次校验（格式转换、下载等增值 / 受限能力共用此一处）。

    返回 (subscribed, free_used, free_daily)：
      - 未启用订阅墙（enabled=False）：subscribed=False，免费额度字段为 0（全免费无限）。
      - 启用墙且携带正确订阅密钥：subscribed=True，不受免费额度限制。
      - 启用墙但免费用户：按 IP 当日计数，超出抛 402（前端据此引导订阅）。
    label 仅用于 402 文案（如「转换」「下载」）。
    """
    if not enabled:
        return (False, 0, 0)
    key = (request.headers.get("x-subscription-key") or "").strip()
    if sub_key and key == sub_key:
        return (True, 0, free_daily)
    ip = _client_ip(request)
    today = _today_str()
    with quota_lock:
        rec = quota_store.get(ip)
        if not rec or rec["date"] != today:
            rec = {"date": today, "count": 0}
            quota_store[ip] = rec
        if rec["count"] >= free_daily:
            raise HTTPException(
                status_code=402,
                detail=f"今日免费{label}次数已用完，订阅可解锁无限{label}",
            )
        rec["count"] += 1
        return (False, rec["count"], free_daily)

def _check_convert_quota(request: Request) -> tuple[bool, int, int]:
    """格式转换订阅 / 限次校验（复用通用 _subscription_quota）。"""
    return _subscription_quota(
        request, enabled=CONVERT_SUB_ENABLED, sub_key=CONVERT_SUB_KEY,
        free_daily=CONVERT_FREE_DAILY, quota_store=_convert_quota,
        quota_lock=_convert_quota_lock, label="转换",
    )


# ---- App 本地算力账号级每日配额（2026-09-29 用户定档）：免费 5 次/日，会员 200 次/日 ---- #
# 覆盖 App 本地重算力功能：转码（含免重传重转/音乐转换）/视频拼接/压缩/超分。
# 与网页云端算力（resource="cloud"）同引擎：按账号 daily_usage 记账 + 异步上云共享。
# 任务成功创建才计费；会员引擎异常 fail-open。紧急停用：VDL_APP_COMPUTE_QUOTA_OFF=true。
_APP_COMPUTE_QUOTA_OFF = os.environ.get("VDL_APP_COMPUTE_QUOTA_OFF", "false").strip().lower() == "true"


# 🔴 2026-10-06 用户定档「每个功能独立配置」：本地重算力从共用一个 `app_compute`
# 键拆成 convert / compress / sr / commentary / bridge 五个独立键。
# `resource` 决定记到哪一份额度；传入的 `label` 只用于给人话提示。
# ⚠️ 不含 `commentary`：视频解说走 `quota.py` 的终身云端额度 + AI 积分，
#    **不走日配额**（守卫 test_feature_usage_gate 会红 —— 别把它配成日配额键）。
# 🔴 2026-10-06 第二轮「剩下没拆的全部拆开」：上一轮把 6 键拆开了，但每键内部
#    还塞着多个用户可见功能 —— 视频拼接混在 convert_video、音频拼接混在
#    convert_audio、视频超分混在 sr。本轮各拆一个独立键，**一个键 = 一个功能**：
#      concat_video  视频拼接（多片段合并成视频）
#      concat_audio  音频拼接（多片段合并成音频）
#      sr_video      视频超分（视频高清修复，与图片「高清修复」分开）
_APP_COMPUTE_KEYS = ("convert_video", "convert_audio", "convert_image",
                    "compress", "sr", "sr_video", "bridge",
                    "concat_video", "concat_audio")
_APP_COMPUTE_LABELS = {
    "convert_video": "视频格式转换", "convert_audio": "音乐转换",
    "convert_image": "图片转换", "compress": "高效压缩",
    "sr": "高清修复", "sr_video": "视频超分", "bridge": "音视频桥接",
    "concat_video": "视频拼接", "concat_audio": "音频拼接",
}


# 🔴 2026-10-06 用户要求「几个分开、不要几个放一起」：视频 / 音乐 / 图片转换
# 虽走同一个端点，但按 `target` 扩展名可判定类型，故拆成三个独立额度键。
_CONVERT_AUDIO_TARGETS = {
    "mp3", "m4a", "wav", "flac", "aac", "opus", "wma", "mp2",
}
_CONVERT_IMAGE_TARGETS = {
    "gif", "png", "jpg", "jpeg", "webp", "bmp", "tiff",
}


def convert_quota_key(target: str, is_image: bool = False) -> tuple[str, str]:
    """按转换目标格式判定该记哪一份额度。

    返回 `(resource, label)`：
      · 视频 → `convert_video`
      · 音频 → `convert_audio`（音乐转换）
      · 图片 → `convert_image`
    `is_image=True` 时优先判为图片（上传转码路径已显式知道源是图片）。

    ⚠️ 未知格式一律落 `convert_video`（不放行）—— 判错就记成视频，
    这是**最保守**的选择：多记一次而非少记。
    """
    t = str(target or "").strip().lower().lstrip(".")
    if is_image or t in _CONVERT_IMAGE_TARGETS:
        return "convert_image", "图片转换"
    if t in _CONVERT_AUDIO_TARGETS:
        return "convert_audio", "音乐转换"
    return "convert_video", "视频格式转换"


def app_compute_gate(request: Request, resource: str = "convert",
                     label: str = "") -> dict:
    """本地算力配额预检（不计数）。超限 402；引擎异常 fail-open。

    `resource` 必须是 `_APP_COMPUTE_KEYS` 之一（旧键 `app_compute` 仍接受，
    作为存量归集与兜底）。`label` 只影响错误提示文案。
    """
    if _APP_COMPUTE_QUOTA_OFF:
        return {"mode": "off"}
    # 未知键一律回落到 convert（而不是放行）—— 传错键时不该变成不限次
    res = resource if resource in _APP_COMPUTE_KEYS else "convert_video"
    name = label or _APP_COMPUTE_LABELS.get(res, "本地处理")
    try:
        from user_membership import current_member_store  # 局部导入，避免 import 顺序问题
        store = current_member_store(request)
        q = store.quota_state(res)
        if q.get("unlimited") or q.get("unknown") or q.get("allowed", True):
            return {"mode": "local", "store": store, "resource": res, "remaining": q.get("remaining")}
        if q.get("tier") == "free":
            detail = (f"今日免费「{name}」额度已用尽（{q['limit']}/日），"
                      f"开通会员可解锁 {q.get('member_limit', 0)} 次/日")
        else:
            detail = f"今日「{name}」配额已用尽（{q['limit']}/日）"
        raise HTTPException(status_code=402, detail=detail)
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — 会员引擎异常 fail-open
        return {"mode": "local", "store": None, "resource": res}


def app_compute_count(request: Request, gate: dict, n: int = 1) -> None:
    """任务成功创建后的计数（失败只记日志，不回滚已建任务）。"""
    if gate.get("mode") in ("off",) or n <= 0:
        return
    try:
        store = gate.get("store")
        if store is None:
            from user_membership import current_member_store
            store = current_member_store(request)
        # 记到 gate 里带的那个独立键（2026-10-06 拆键）
        store.use_daily(gate.get("resource") or "convert_video", n=n)
    except Exception as e:  # noqa: BLE001 — 计数失败绝不回滚已创建的任务
        try:
            logger.warning("[app-compute-quota] 计数失败（忽略）: %s", str(e)[:160])
        except Exception:  # noqa: BLE001
            pass

def _host_of(url: str) -> str:
    """从链接取出主机名（去掉 www./m. 前缀），解析失败返回空串。"""
    try:
        host = (urlparse(url).hostname or "").lower()
        return host.removeprefix("www.").removeprefix("m.")
    except ValueError:
        return ""

import auth_store
# 状态文件放数据目录（避 TCC「下载」目录，见 TaskStore 注释）；下载落地仍在 DOWNLOAD_DIR
store = TaskStore(DOWNLOAD_DIR, state_dir=auth_store._base_dir())
# 线程池只设硬上限；真正的「同时下几个」由 BatchScheduler 的并发计数器软控（可动态调整）
executor = ThreadPoolExecutor(max_workers=VDL_BATCH_HARD_MAX, thread_name_prefix="vdl-dl")
scheduler = BatchScheduler(executor, default_concurrency=MAX_CONCURRENT_DOWNLOADS)
process_queue = pq_mod.ProcessQueue(executor, default_concurrency=2, hard_max=4)

# ---- 订阅监控（桌面版功能）：本地 JSON 持久化 + 后台定时探查新视频 ----
SUB_ENABLED = plat.is_desktop() or bool(os.environ.get("VDL_SUBSCRIPTIONS_ENABLED"))
SUBSCRIBE_PROBE_LIMIT = int(os.environ.get("VDL_SUBSCRIBE_PROBE_LIMIT", "100") or 100)
SUB_CHECK_INTERVAL = int(os.environ.get("VDL_SUB_CHECK_INTERVAL", "1800") or 1800)  # 默认 30 分钟

def _tcc_safe_config_path(name: str) -> Path:
    """配置类 JSON 存放路径：用户数据目录（~/.video-downloader 等），**不是**下载目录。

    根因（2026-09-24，第三次咬人）：此前 .subscriptions.json / .retention.json 放在
    ~/Downloads/VideoDownloader/（TCC 保护目录）。每次重新构建 ad-hoc 签名 cdhash 变化，
    macOS 重新弹「访问下载文件夹」授权框；后端在 import 阶段同步 open() 该文件，
    授权框没人点 → open() 永久阻塞 → 后端永不监听（症状：窗口开了但 8321 连不上，
    pydump 卡 pathlib open）。配置文件搬出 Downloads 后，启动路径不再触碰 TCC 保护目录。
    旧文件迁移：新路径不存在且旧路径存在时尝试复制，任何 OSError 立即吞掉
    （新装环境拿不到授权就先空仓库起步，避免启动被卡）。
    """
    new = _user_data_dir() / name
    if new.exists():
        return new
    old = DOWNLOAD_DIR / name
    if old.exists():
        try:
            new.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old, new)
        except OSError:
            pass
    return new

sub_store = subs_mod.SubscriptionStore(_tcc_safe_config_path(".subscriptions.json"))
prober = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_PROBES, thread_name_prefix="vdl-probe")

# ---- 时效自动清理（桌面版功能）：按保留期/容量上限清理下载目录 ----
# 与媒体库同一开关：只有桌面版（或显式开 VDL_LIBRARY_ENABLED）才管理本地磁盘。
RETENTION_ENABLED = (
    plat.is_desktop()
    or bool(os.environ.get("VDL_LIBRARY_ENABLED"))
    or bool(os.environ.get("VDL_RETENTION_ENABLED"))
)
retention_store = retention_mod.RetentionStore(_tcc_safe_config_path(".retention.json"))

# ---- 库内保险箱（桌面版功能）：选中文件就地 AES 加密为 .vdlenc，播放前临时解密 ----
# 与媒体库同一开关。内存密钥 VAULT_KEY 为 None 即「锁定」态；vault.json 只存 salt+verify，
# 绝不存明文密码或密钥（见 crypto_vault.new_vault / unlock_key）。
CRYPTO_ENABLED = (
    plat.is_desktop()
    or bool(os.environ.get("VDL_LIBRARY_ENABLED"))
    or bool(os.environ.get("VDL_CRYPTO_ENABLED"))
)
VAULT_PATH = Path(
    os.environ.get("VDL_VAULT_CONFIG") or (Path.home() / ".video-downloader" / "vault.json")
)
VAULT_LOCK = threading.Lock()
VAULT_KEY: bytes | None = None  # 解锁后驻留内存；锁定/重启即清空
CRYPTO_JOBS: dict[str, dict] = {}
CRYPTO_LOCK = threading.Lock()
# 解密播放临时目录（与下载目录同盘，避免跨卷复制大文件）
VAULT_TMP = DOWNLOAD_DIR / ".vault_tmp"
_safe_mkdir(VAULT_TMP)
CRYPTO_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="vdl-crypto")

# ---- 桌面版种子下载（libtorrent 集成）：把 magnet/.torrent 下载到本地媒体库 ----
# 与媒体库同一开关策略（桌面版/显式开启）；libtorrent 为可选依赖，未安装时功能整体禁用。
TORRENT_ENABLED = (
    plat.is_desktop()
    or bool(os.environ.get("VDL_LIBRARY_ENABLED"))
    or bool(os.environ.get("VDL_TORRENT_ENABLED"))
)
torrent_manager = torrent_mod.get_manager(DOWNLOAD_DIR)

def _vault_load() -> dict | None:
    try:
        if VAULT_PATH.exists():
            return json.loads(VAULT_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None

def _chmod_600(path: Path) -> None:
    """跨平台收紧文件权限到仅当前用户可读写。Windows 的 os.chmod 只切只读位、不控 ACL，
    故改用 icacls 移除继承并仅授权当前用户（无 pywin32 也可工作）。"""
    if sys.platform == "win32":
        try:
            import subprocess as _sp
            _sp.run(["icacls", str(path), "/inheritance:r",
                     "/grant:r", f"{os.environ.get('USERNAME', '')}:(R,W)"],
                    check=False, capture_output=True)
        except Exception:
            pass
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

def _vault_save(vault: dict) -> None:
    VAULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 唯一临时名原子写（见 atomic_io）：固定名 `.tmp` 下两个并发写者互截断，
    # 保险箱索引整份报废 = 用户加密文件全部「找不到」。_chmod_600 含 Windows icacls
    # 处理，故用 chmod 钩子而不是默认 os.chmod。
    with atomic_io.mutation(VAULT_PATH):
        atomic_io.atomic_write_text(
            VAULT_PATH, json.dumps(vault, ensure_ascii=False), chmod=_chmod_600
        )

def _vault_tmp_for(lib_id: str) -> Path:
    """⚠️ 遗留（安全审计 2026-10-11 P1-7 后已无调用方）。

    解密播放已改为 `crypto_vault.decrypt_range` + 内存流式回传，**不再落盘明文**，
    故不再需要「按 lib_id 派生临时解密文件」。保留函数是为了兼容历史引用，
    新代码请勿使用；`.vault_tmp` 目录仅用于承接升级前遗留的 `.dec` 清理。
    """
    VAULT_TMP.mkdir(parents=True, exist_ok=True)
    return VAULT_TMP / (lib_id + ".dec")

# --------------------------------------------------------------------------- #
# 自动解说（松耦合桥接 commentary-pipeline/process.py，不重写解说逻辑）
# --------------------------------------------------------------------------- #

class _CommentaryRuntime:
    """模块加载时一次性探测解说运行环境，集中管理解释器与工具链。

    打包后 sys.executable 是 .app 主程序、子进程 PATH 可能被截断，
    这里把所有不确定性收敛到一处，并暴露清晰的诊断信息（issues/ready）。
    """

    def __init__(self) -> None:
        self.python = self._resolve_python()
        self.python_ok = self._check_python(self.python)
        self.deps_ok = self._check_deps(self.python) if self.python_ok else False
        self.ffmpeg_dir, self.ffprobe_ok = self._resolve_ffmpeg()
        self.issues = self._collect_issues()

    # ---- 解释器 ----
    def _resolve_python(self) -> str:
        """解析跑 process.py 的解释器，统一委托 commentary_locate。"""
        from commentary_locate import locate_commentary
        loc = locate_commentary()
        if loc is None:
            return sys.executable
        return loc.python

    def _check_python(self, py: str) -> bool:
        # 打包后 sys.executable 通常是 .app 主程序，不应作为解释器——
        # 但「单二进制双角色」bundled 模式下，worker 正是用自身(sys.executable)重入，
        # 依赖已随包内置，故该例外下允许 sys.executable 作为解释器。
        if plat.is_desktop() and py == sys.executable:
            if os.environ.get("VDL_COMMENTARY_BUNDLED") == "1" or COMMENTARY_MODE == "bundled":
                return True
            return False
        # 开发/测试模式（非 frozen 且未显式设 VDL_COMMENTARY_PYTHON）信任当前解释器
        if "VDL_COMMENTARY_PYTHON" not in os.environ and not plat.is_desktop():
            return True
        try:
            r = subprocess.run(
                [py, "-c", "import sys; print(sys.executable)"],
                capture_output=True, text=True, timeout=5,
            )
            return r.returncode == 0 and "python" in (r.stdout or "").lower()
        except Exception:
            return False

    def _check_deps(self, py: str) -> bool:
        if not self.python_ok:
            return False
        # 🔴 包内捆绑模式（单二进制双角色）下**不能**用 `python -c` 探测依赖：
        #    sys.executable 是 GUI 主程序，它不认 `-c`，会当成正常启动再拉起一个应用实例，
        #    30 秒后超时返回非 0 —— 于是明明依赖齐全却恒定报「解说 Python 缺少依赖」，
        #    整个解说功能被卡在「环境未就绪」。依赖完整性由构建期 --collect-all 保证；
        #    若真缺包，worker 子进程会在对应阶段抛 ImportError 并回显到任务日志。
        if os.environ.get("VDL_COMMENTARY_BUNDLED") == "1" or COMMENTARY_MODE == "bundled":
            return True
        try:
            r = subprocess.run(
                [py, "-c", "import faster_whisper, edge_tts"],
                capture_output=True, text=True, timeout=30,
            )
            return r.returncode == 0
        except Exception:
            return False

    # ---- 工具链 ffmpeg / ffprobe ----
    def _resolve_ffmpeg(self) -> tuple[str, bool]:
        """解析 ffmpeg/ffprobe 所在目录；打包后 PATH 可能被截断，这里显式收集所有可能目录。

        策略：
        1. 优先 VDL_FFPROBE_BIN / VDL_FFMPEG_BIN 的目录；
        2. 如果该目录没有 ffprobe，再补充系统常见目录；
        3. 返回所有可用目录（用 ; 拼接），并验证 ffprobe 确实能在合并后的 PATH 里找到。
        """
        dirs: list[str] = []
        seen: set[str] = set()

        def add(d: str | Path) -> None:
            s = str(d)
            if s and s not in seen:
                seen.add(s)
                dirs.append(s)

        # 1) 用户/启动器显式指定的 bin
        for key in ("VDL_FFPROBE_BIN", "VDL_FFMPEG_BIN"):
            v = os.environ.get(key, "").strip()
            if v:
                add(Path(v).parent)

        # 2) 当前进程 PATH 里能直接找到的工具
        for name in ("ffprobe", "ffmpeg"):
            p = shutil.which(name)
            if p:
                add(Path(p).parent)

        # 3) 系统常见目录兜底（按平台补充）
        if sys.platform == "win32":
            add(r"C:\ffmpeg\bin")
            add(os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links"))
        for fb in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"):
            add(fb)

        if not dirs:
            return "", False

        # 4) 验证：把这些目录前置到 PATH 后，ffprobe 是否真的能被发现
        probe_path = os.pathsep.join(dirs + ([os.environ.get("PATH", "")] if os.environ.get("PATH") else []))
        ok = bool(shutil.which("ffprobe", path=probe_path))
        return os.pathsep.join(dirs), ok

    def _collect_issues(self) -> list[str]:
        issues: list[str] = []
        if not self.python_ok:
            issues.append("解说 Python 解释器不可用（请设置 VDL_COMMENTARY_PYTHON 指向装了 faster_whisper/edge_tts 的解释器）")
        elif not self.deps_ok:
            issues.append("解说 Python 缺少依赖（faster_whisper / edge_tts 未安装）")
        if not self.ffprobe_ok:
            issues.append("找不到 ffprobe（请安装 ffmpeg 或设置 VDL_FFPROBE_BIN）")
        return issues

    def env(self) -> dict[str, str]:
        """为 process.py 子进程准备环境变量，关键是把 ffmpeg/ffprobe 目录前置到 PATH，
        并强制 PYTHONIOENCODING=utf-8（Windows 默认 cp936 会让 emoji 日志乱码/抛错）。
        同时注入统一 LLM 配置——若用户已在前端配置了 Key/提供商/模型。
        另外显式设置 COMMENTARY_BASE，确保打包后 config.py 里的 BASE 一定指向管线根目录
        （避免 PyInstaller 的 __file__ 解析差异导致找不到 assets/fonts 下捆绑的中文字体）。"""
        env = os.environ.copy()
        # 防「重入保险丝」误触发（2026-09-15 实测）：worker 子进程以 `sys.executable --vdl-commentary-worker`
        # 重入自身，desktop_launcher 会因 `VDL_WORKER_DEPTH>0` 判为「二次重入」而拒绝执行。若 App 自身的
        # 启动环境意外带入了该变量（某些启动方式会），首层 worker 就会被误拦、任务在「准备输入文件」即失败。
        # server 进程永远不是 worker，因此这里显式清除，保证首层 worker 一定以 depth=0 启动。
        env.pop("VDL_WORKER_DEPTH", None)
        # 🔴 2026-10-20 Windows 实测缺陷：这里原本是 setdefault，若父进程环境里已有
        #    空值/GBK 的 PYTHONIOENCODING 就不会覆盖，管线打印 `¥` 直接 UnicodeEncodeError。
        #    改为强制赋值 + 打开 UTF-8 模式（PYTHONUTF8 影响 open() 默认编码，覆盖更广）。
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        if COMMENTARY_DIR:
            env["COMMENTARY_BASE"] = str(COMMENTARY_DIR)
        if self.ffmpeg_dir:
            env["PATH"] = self.ffmpeg_dir + os.pathsep + env.get("PATH", "")
        # 统一 LLM 配置：仅 Key 非空时注入，无 Key 不污染子进程环境
        inject_llm_env(env)
        # 统一视觉模型配置：auto 不注入（走流水线回退链）；Ollama 注入 base_url+model；
        # 云端仅 Key 非空时注入。任何环境都不会因"没配好"而硬失败。
        inject_vision_env(env)
        # 解说/配音音量手动可调配置：注入旁白响度/原声压低/旁白增益
        inject_commentary_env(env)
        return env

    def ready(self) -> bool:
        return not self.issues

COMMENTARY_RT = _CommentaryRuntime()

def _commentary_work_dir() -> Path:
    d = COMMENTARY_WORK_DIR / uuid.uuid4().hex[:12]
    d.mkdir(parents=True, exist_ok=True)
    return d

def _purge_commentary_work() -> int:
    """清理解说 work 目录里超过保留期的旧 job（防无限累积）。

    安全约束：
      1. 只删 12 位 hex 命名的 job 目录（不碰其他目录/文件）；
      2. 跳过 commentary_jobs 中仍 running 的 src_path 对应目录；
      3. 只删 mtime 早于保留期（COMMENTARY_WORK_RETENTION_DAYS，默认 7 天）的目录。
    返回删除数量。
    """
    if not COMMENTARY_WORK_DIR.exists():
        return 0
    cutoff = time.time() - COMMENTARY_WORK_RETENTION_DAYS * 86400
    hex12 = re.compile(r"^[0-9a-f]{12}$")
    running_dirs = set()
    try:
        with _commentary_lock:
            for job in commentary_jobs.values():
                sp = str(job.get("src_path") or "")
                if sp.startswith(str(COMMENTARY_WORK_DIR)):
                    running_dirs.add(os.path.dirname(sp))
    except Exception:
        pass
    removed = 0
    for d in COMMENTARY_WORK_DIR.iterdir():
        try:
            if not d.is_dir() or not hex12.match(d.name):
                continue
            if str(d) in running_dirs:
                continue
            if d.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
        except Exception:
            continue
    if removed:
        logger.info("已清理 %s 个过期解说 work 目录（保留 %s 天）", removed, COMMENTARY_WORK_RETENTION_DAYS)
    return removed

# 解说任务过程展示：阶段名 + 匹配日志关键词（不区分大小写）
_COMMENTARY_STEP_KEYWORDS = [
    ("准备输入文件", ["准备", "复制", "软链", "输入文件", "symlink", "copy file", "save upload"]),
    ("转写视频语音", ["[1/2] 转写", "whisper", "transcribe", "提取音轨", "转写完成", "transcript"]),
    ("生成 AI 解说词", ["[2/2] 自动解说词", "auto_script", "解说词", "script.json", "生成脚本", "script ready"]),
    ("合成 AI 配音", ["配音", "edge_tts", "tts", "语音合成", "synthesize", "narration"]),
    ("渲染成片", ["剪辑成片", "edit_ffmpeg", "edit.py", "ffmpeg", "渲染", "合并", "concat", "build", "成片"]),
    ("完成", ["全部完成", "成片在", "completed", "done"]),
]

def _ensure_commentary_steps(job: dict) -> list[dict]:
    """初始化或返回解说任务的步骤时间线。"""
    if "steps" not in job or not job["steps"]:
        now = time.time()
        job["steps"] = [
            {"name": name, "status": "pending", "detail": "", "created_at": now, "updated_at": now}
            for name, _ in _COMMENTARY_STEP_KEYWORDS
        ]
    return job["steps"]

def _commentary_log(job: dict, line: str) -> None:
    """给解说任务追加一行带时间戳的运行日志。"""
    if not line:
        return
    ts = time.strftime("%H:%M:%S", time.localtime())
    job.setdefault("logs", []).append(f"{ts}  {line.strip()}")
    if len(job.get("logs", [])) > 200:
        job["logs"][:] = job["logs"][-200:]

# ====== BGM 状态 manifest：成片文件是否已加配乐的真相源 ======
# 文件名约定：<video>.bgm.json，与无音乐 sidecar（.nomusic.mp4）解耦。文件可丢、可重建，
# 不影响成片本体。bgm != "off" 视为「已成片」(final)，bgm == "off" 或无 manifest 视为「待加配乐」(draft)。
def _bgm_state_path(out_path: str | Path) -> Path:
    p = Path(out_path)
    return p.with_suffix(p.suffix + ".bgm.json") if p.suffix else Path(str(p) + ".bgm.json")

def _read_bgm_state(out_path: str | Path) -> dict | None:
    """读取 BGM 状态 manifest；不存在或损坏返回 None（按 draft 处理）。"""
    try:
        meta_p = _bgm_state_path(out_path)
        if not meta_p.is_file():
            return None
        return json.loads(meta_p.read_text(encoding="utf-8"))
    except Exception:
        return None

def _write_bgm_state(out_path: str | Path, *, bgm: str, volume: float = 0.18, source: str = "render", extra: dict | None = None) -> None:
    """写入 BGM 状态 manifest。bgm="off" 也写，便于区分「无 BGM」与「manifest 缺失」。"""
    try:
        meta_p = _bgm_state_path(out_path)
        payload: dict = {
            "bgm": bgm or "off",
            "volume": float(volume) if volume is not None else 0.18,
            "source": source,
            "ts": int(time.time()),
        }
        if extra:
            payload.update(extra)
        meta_p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:
        logger.exception("写 BGM manifest 失败：%s", out_path)

def _update_commentary_steps(job: dict, line: str) -> None:
    """根据 process.py 输出关键词自动推进步骤状态。"""
    steps = _ensure_commentary_steps(job)
    line_lower = line.lower()
    matched = -1
    for idx, (name, keywords) in enumerate(_COMMENTARY_STEP_KEYWORDS):
        if any(k.lower() in line_lower for k in keywords):
            matched = idx
            break
    if matched < 0:
        return
    now = time.time()
    for s in steps[:matched]:
        if s["status"] == "pending":
            s["status"] = "done"
            s["updated_at"] = now
    cur = steps[matched]
    cur["status"] = "running"
    cur["detail"] = line[:200]
    cur["updated_at"] = now

def _commentary_mark_error(job_id: str, detail: str) -> None:
    """把当前 running 的步骤标为 error。"""
    with _commentary_lock:
        job = commentary_jobs.get(job_id)
        if not job:
            return
        steps = _ensure_commentary_steps(job)
        now = time.time()
        for s in steps:
            if s["status"] == "running":
                s["status"] = "error"
                s["detail"] = detail[:200]
                s["updated_at"] = now
                break

def _apply_trim(src_path: str, in_dir: Path, start: float, end: float):
    """服务端预处理裁剪：同一源+起止始终产出同名文件，便于 script-only 与后续 render 复用。
    返回 (裁剪后路径, 裁剪后时长)；裁剪失败则回退原路径。"""
    key = hashlib.md5(f"{src_path}|{start:.3f}|{end:.3f}".encode("utf-8")).hexdigest()[:12]
    trim_out = in_dir / f"trim_{key}.mp4"
    if trim_out.exists() and trim_out.stat().st_size > 0:
        return str(trim_out), (end - start)
    dur = max(0.1, end - start)
    try:
        subprocess.run(
            [FFMPEG_BIN, "-y", "-ss", f"{start:.3f}", "-i", str(src_path),
             "-t", f"{dur:.3f}", *_h264_args(FFMPEG_BIN, "fast"),
             "-c:a", "aac", "-movflags", "+faststart", str(trim_out)],
            capture_output=True, text=True, timeout=1800,
        )
    except Exception as exc:
        logger.warning("视频裁剪失败，回退使用原视频：%s", exc)
        return src_path, 0.0
    if not (trim_out.exists() and trim_out.stat().st_size > 0):
        return src_path, 0.0
    return str(trim_out), dur

def _commentary_needs_trim(ts: float, te: float, src_dur: float) -> bool:
    """折叠区间 (ts, te) 是否需要预裁源片。

    🔴 2026-09-20 用户实测暴露：设「正剧开始」留空 +「片尾开始」= 15 分钟 → 成片仍是 46 分钟整片，
    只有前 15 分钟有解说。根因是这里的判据退化成 `0.0 < ts < te`：
    ts=0（从片头开始裁）被排除 ⇒ 走 else 分支拿到整片源，而 drama 窗口只作用于旁白，
    于是「解说 15 分钟 + 画面 46 分钟」。

    判据取两版历史实现之长：
      · 2026-08-26 `0 <= ts < te`：ts=0 能裁（对），但整片也会白重编码一遍（错）；
      · 2026-09-19 `0.0 < ts < te`：跳过整片（对），却把 ts=0 一起漏掉（错，本次病灶）。
    ⇒ **只要 (ts, te) 是整片的真子区间就裁**（含 ts=0 这种「从片头裁到中间」），
      与 `_fold_commentary_range` 的「覆盖整片 ⇒ 不裁」容差保持完全一致，
      保证整片场景的行为与改造前逐字节相同（不多一次重编码）。
    """
    if not (te > ts):
        return False                              # 空区间 / 未指定 / 探测失败（src_dur=0）
    if ts <= 0.5 and (src_dur <= 0 or te >= src_dur - 0.5):
        return False                              # 覆盖整片 ⇒ 不裁
    if src_dur > 0 and te > src_dur + 1.0:
        return False                              # 越界（沿用旧版守卫）
    return True

def _commentary_eta(job: dict, line: str, src_dur: float) -> None:
    """从子进程进度行推算 ETA，写入 job['eta_remaining']（剩余秒）/ eta_done_at（绝对时间戳）。"""
    now = time.time()
    m = re.search(r"共\s*(\d+)\s*段", line)
    if m:
        job["eta_total_segs"] = int(m.group(1))
    m = re.search(r"开始渲染\s*(\d+)\s*段", line)
    if m:
        job["eta_render_total"] = int(m.group(1))
        job["eta_render_start"] = now
    m = re.search(r"\[(\d+)/(\d+)\]", line)
    if m:
        done = int(m.group(1))
        job["eta_render_done"] = done
        rs = job.get("eta_render_start")
        if rs:
            elapsed = now - rs
            if done > 0:
                per = elapsed / done
                total = job.get("eta_render_total") or done
                # 段是并发渲染的（min(cpu,8)），按并行度折算墙钟时间
                par = max(1, min(os.cpu_count() or 4, 8))
                remaining = per * max(0, total - done) / par
                job["eta_remaining"] = int(remaining)
                job["eta_done_at"] = int(now + remaining)
            return
    # 渲染前（转写/TTS）：用源时长做粗估，进入渲染段后由上面的实时进度收敛
    if job.get("eta_render_start") is None and src_dur:
        elapsed = now - job.get("started_at", now)
        coarse = src_dur * 1.2
        rem = max(0, int(coarse - elapsed))
        job["eta_remaining"] = rem
        job["eta_done_at"] = int(now + rem)

def _commentary_option_args(*, commentary_type: str = "deep_hl", highlight_source: str = "ai",
                             intro_highlight: bool = False, skip_intro_outro: bool = False,
                             no_narrate_intro_outro: bool = True, retain_pct: float | None = None,
                             web: bool = False, one_click: bool = False, mode: str | None = None,
                             style: str = "none", style_intensity: int = 65, vision: bool = False,
                             intro_sec: float | None = None, outro_sec: float | None = None,
                             drama_start_sec: float | None = None, drama_end_sec: float | None = None,
                             bgm: str = "off", bgm_file: str = "", bgm_volume: float = 0.18,
                             subtitle_size: float = 1.0, subtitle_color: str = "FFFFFF",
                             subtitle_border: float = 1.0, subtitle_border_color: str = "000000",
                             subtitle_pos: str = "bottom",
                             max_chars: int = 0, original_speed: bool = True) -> list:
    """把剪辑选项翻译成 process.py 的命令行参数（local / bundled 模式共用）。

    original_speed=False → 带 `--no-original-speed`：渲染层改为按旁白时长放慢画面
    （k = vdur/win）把窗口撑开，用于避免原速模式下旁白被 -t 静默截断。
    """
    args = ["--commentary-type", commentary_type, "--highlight-source", highlight_source]
    if not original_speed:
        args.append("--no-original-speed")
    if style and style != "none":
        args += ["--style", style]
    if style_intensity is not None:
        args += ["--style-intensity", str(style_intensity)]
    if intro_highlight:
        args.append("--intro-highlight")
    if skip_intro_outro:
        args.append("--skip-intro-outro")
    if not no_narrate_intro_outro:
        # 默认就是「保留片头片尾·不解说」，仅当用户显式要全片解说时才带 --narrate-all
        args.append("--narrate-all")
    if retain_pct is not None:
        args += ["--retain-pct", str(retain_pct)]
    if intro_sec is not None and intro_sec > 0:
        args += ["--intro-sec", str(intro_sec)]
    if outro_sec is not None and outro_sec > 0:
        args += ["--outro-sec", str(outro_sec)]
    if drama_start_sec is not None and drama_start_sec > 0:
        args += ["--drama-start-sec", str(drama_start_sec)]
    if drama_end_sec is not None and drama_end_sec > 0:
        args += ["--drama-end-sec", str(drama_end_sec)]
    if web:
        args.append("--web")
    if vision:
        args.append("--vision")
    if one_click:
        args.append("--one-click")
    if mode:  # 旧版兼容字段，仅当显式传了才带
        args += ["--mode", mode]
    # ===== 吸收 NarratoAI：BGM 配乐 / 字幕样式 / 解说字数 =====
    if bgm and bgm != "off":
        if bgm == "user" and bgm_file:
            args += ["--bgm", "user", "--bgm-file", bgm_file]
        elif bgm in ("soft", "light", "epic"):
            args += ["--bgm", bgm]
            if bgm_volume and abs(bgm_volume - 0.18) > 1e-6:
                args += ["--bgm-volume", str(bgm_volume)]
    if subtitle_size and abs(subtitle_size - 1.0) > 1e-6:
        args += ["--subtitle-size", str(subtitle_size)]
    if subtitle_color and subtitle_color.upper() != "FFFFFF":
        args += ["--subtitle-color", subtitle_color]
    if subtitle_border and abs(subtitle_border - 1.0) > 1e-6:
        args += ["--subtitle-border", str(subtitle_border)]
    if subtitle_border_color and subtitle_border_color.upper() != "000000":
        args += ["--subtitle-border-color", subtitle_border_color]
    if subtitle_pos and subtitle_pos != "bottom":
        args += ["--subtitle-pos", subtitle_pos]
    if max_chars and max_chars > 0:
        args += ["--max-chars", str(max_chars)]
    return args

def _parse_feather_opt(raw: str) -> dict:
    """解析前端「原字幕羽化」面板传来的 JSON 选项。

    为什么打包成一个 JSON 字符串而不是 4 个扁平字段：本参数要穿过 Pydantic / Form /
    5 个调用点，扁平化等于 20 处机械改动且极易漏（历史上 subtitle_* 就这么漏过链路）。
    这里只多一个字段，解析与校验集中在这一处。

    取值：
      mode         fade|stretch|blur（缺省/非法 → 不传，管线用自身默认 fade）
      band_y_ratio / band_h_ratio   占画面高的比例，成对出现才生效
      strength     0.4~2.5 强度倍率（1.0 = 管线默认，不传）

    任何一项非法都只丢弃该项，**绝不抛错**——羽化是可选美化，不该拦住整条任务。
    """
    if not raw:
        return {}
    try:
        d = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(d, dict):
        return {}
    out: dict = {}
    mode = str(d.get("mode") or "").strip().lower()
    if mode in ("fade", "stretch", "blur"):
        out["mode"] = mode
    try:
        by, bh = d.get("band_y_ratio"), d.get("band_h_ratio")
        if by is not None and bh is not None:
            by, bh = float(by), float(bh)
            # 比例必须落在 (0,1)；band_h 极小的值会让管线 clamp 到 6px，这里只挡越界
            if 0.0 <= by < 1.0 and 0.0 < bh <= 1.0:
                out["band_y_ratio"], out["band_h_ratio"] = by, bh
    except (TypeError, ValueError):
        pass
    try:
        st = d.get("strength")
        if st is not None:
            st = float(st)
            if 0.4 <= st <= 2.5:
                out["strength"] = st
    except (TypeError, ValueError):
        pass
    # 逐段自适应羽化带（2026-09-16）：勾选后擦除几何按每个解说段自己的探测结果走，
    # 没原字幕的段不擦。与手动带位置（band_y_ratio/band_h_ratio）互斥：
    # 前端勾选时不再传带位置，这里也做一道防御 —— dynamic 优先，丢弃手动比例。
    if d.get("dynamic"):
        out["dynamic"] = True
        out.pop("band_y_ratio", None)
        out.pop("band_h_ratio", None)
        # 自适应之上的「手动微调补救」（2026-09-16）：探测带偶尔盖不住原字幕时，
        # 用户在面板加偏移补救。dy=带顶上下移 / dh=带高加成，都是占画面高比例，
        # 叠加在**每个探测出的带**上（含全片带），逐段自适应仍然生效。
        # 仅 dynamic 模式生效：手动指定带本身就是最终值，再叠加会打架。
        try:
            dy, dh = d.get("dy_ratio"), d.get("dh_ratio")
            if dy is not None:
                dy = float(dy)
                if -0.1 <= dy <= 0.1:
                    out["dy_ratio"] = dy
            if dh is not None:
                dh = float(dh)
                if -0.1 <= dh <= 0.2:
                    out["dh_ratio"] = dh
        except (TypeError, ValueError):
            pass
    return out

def _fold_commentary_range(src_dur: float, trim_start: float = 0.0, trim_end: float = 0.0,
                           drama_start_sec: float | None = None,
                           drama_end_sec: float | None = None) -> tuple[float, float]:
    """把「正剧范围」（绝对秒）折进「真正喂给管线的区间」，返回 (起点, 终点)。

    🔴 2026-09-19 用户实测暴露的问题：`drama_start_sec/drama_end_sec`（界面上的
    「正剧开始/片尾开始时间」）此前**只透传给管线当旁白窗口**，whisper 依旧对整片转写 ——
    用户把一支 45.6 分钟的片子设成只做其中 15 分钟，步骤里照样显示「已裁剪 0~2734s」、
    日志里照样是「开始听写 45.6 分钟音频」，CPU int8 要跑半小时以上（白等）。
    现在起这段区间就是**实际输入**：转写 / 生成脚本 / 渲染都只发生在它里面。

    交集语义（两者都是绝对秒，缺省＝不设限）：
      起点取更靠后的那个，终点取更靠前的那个；
      区间覆盖整片时返回 (0, src_dur)——调用方据此跳过无谓的整片重编码
      （旧逻辑会把整片重编码一遍，还打出误导性的「已裁剪 0~2734s」）。

    🔴 拖反归一化（2026-09-19，与上面同批修）：起点大于终点时**交换**，而不是
      「非法 → 退回整片」。左栏两个时间是自由文本输入，用户完全可能把
      「片尾开始」填得比「正剧开始」还早（UI 只给红色提醒，不拦提交）——
      若此时退回整片，正好复活本次要修的病灶：45 分钟的片子按整片转写
      （CPU int8 半小时以上），而用户只想做其中一段。交换是唯一符合直觉的
      解释，且处理量绝不比用户意图更大。
    """
    dur = float(src_dur or 0.0)
    ts = max(0.0, float(trim_start or 0.0))
    te = float(trim_end or 0.0)
    ds = float(drama_start_sec or 0.0)
    de = float(drama_end_sec or 0.0)
    # 拖反 → 交换（见 docstring 末段）。两端都给了值才判，避免把「终点缺省=片尾」误当拖反。
    if ts > 0 and te > 0 and te < ts:
        ts, te = te, ts
    if ds > 0 and de > 0 and de < ds:
        ds, de = de, ds
    if dur > 0:
        ts = min(ts, dur)
        te = min(te, dur) if te > 0 else dur
    if ds > 0:
        ts = max(ts, ds)
    if de > 0:
        te = min(te, de) if te > 0 else de
    if dur > 0 and te > dur:
        te = dur
    if te <= ts:                      # 非法区间 → 不裁（fail-open，绝不因参数问题出废片）
        return 0.0, (dur if dur > 0 else 0.0)
    if ts <= 0.5 and (dur <= 0 or te >= dur - 0.5):
        return 0.0, (dur if dur > 0 else 0.0)   # 覆盖整片 → 不裁
    return ts, te

def _commentary_charge_on_success(job_id: str) -> None:
    """「产出成功才扣费」的唯一落账口（🔴 2026-10-23）。

    事故背景：解说在转写阶段 1 秒崩溃（av 19 与 faster-whisper 1.2.1 不兼容），
    但 AI 积分已在**开跑前**的预检里扣掉，而失败路径只退云端免费次数、
    **完全没有退积分** ⇒ 用户两次重试被白白扣掉 320 积分、一个成片都没拿到。

    现在预检（`routers/quota.py::precheck_or_raise`）只做**只读**余额校验，
    真正落账挪到这里：只有任务真的产出了解说词或成片才调用 —— 没产出就不扣。

    ⚠️ 调用位置铁律：本函数内部会拿 `_commentary_lock`，而它是**非可重入**的
    普通 `threading.Lock` ⇒ 必须在 `with _commentary_lock:` 块**之外**调用，
    否则自锁死（已有分支都是在 update 完、锁释放后再调）。

    幂等：`billing["charged"]` 置位后不再重复扣（脚本审核后渲染复用父任务账单、
    或同一个成功分支被重入时都靠它兜住）。
    """
    job: dict = {}
    try:
        with _commentary_lock:
            job = commentary_jobs.get(job_id) or {}
            bill = dict(job.get("billing") or {})
        if not bill or bill.get("charged"):
            return
        from routers.quota import charge_commentary_on_success
        res = charge_commentary_on_success(bill) or {}
        bill["charged"] = True
        bill["charged_at"] = time.time()
        bill["charged_ops"] = list(res.get("charged") or [])
        with _commentary_lock:
            if job_id in commentary_jobs:
                commentary_jobs[job_id]["billing"] = bill
                job = commentary_jobs[job_id]
        ops = bill["charged_ops"]
        if ops:
            _commentary_log(job, f"[计费] 已产出，扣除 AI 积分（{'、'.join(ops)}）")
        else:
            _commentary_log(job, "[计费] 已产出，本次无需扣积分")
    except Exception:  # noqa: BLE001 — 记账失败绝不能把已产出的成片作废
        logger.exception("解说产出后扣费异常（不影响成片交付）job=%s", job_id)


def _commentary_run(job_id: str, src_path: str, vertical: bool, voice: str, edit_only: str | None = None, script_only: bool = False, trim_start: float = 0.0, trim_end: float = 0.0, mode: str | None = None, commentary_type: str = "deep_hl", highlight_source: str = "ai", intro_highlight: bool = False, skip_intro_outro: bool = False, no_narrate_intro_outro: bool = True, retain_pct: float | None = None, web: bool = False, one_click: bool = False, title: str = "", style: str = "none", style_intensity: int = 65, src_filename: str = "", vision: bool = False, tts_provider: str = "", correct_transcript: str = "", intro_sec: float | None = None, outro_sec: float | None = None, drama_start_sec: float | None = None, drama_end_sec: float | None = None, export_jianying: str = "", bgm: str = "off", bgm_file: str = "", bgm_volume: float = 0.18, subtitle_size: float = 1.0, subtitle_color: str = "FFFFFF", subtitle_border: float = 1.0, subtitle_border_color: str = "000000", subtitle_pos: str = "bottom", max_chars: int = 0, original_speed: bool = True, feather_opt: str = "") -> None:
    """后台线程：把下载好的视频喂给 commentary-pipeline，等成片回传。

    复用用户现成的 process.py 整条管线（whisper 转写 → edge-tts 配音 → ffmpeg 出片），
    本函数只负责文件桥接与成片定位。算力由解说 worker 独立承担，不影响下载服务。
    HTTP 模式(VDL_COMMENTARY_MODE=http)下转发给独立 worker 服务。
    """
    # 先探源片长（供裁剪逻辑使用）
    try:
        src_dur = fftools.probe_duration(Path(src_path), ffmpeg_bin=FFMPEG_BIN)
    except Exception:
        src_dur = 0.0

    if COMMENTARY_MODE == "http":
        # ⚠️ HTTP worker 模式走的是「把源片整体交给远端 worker」这条路，本地不裁文件
        #    ⇒ 无法把「正剧范围」折成实际输入（远端仍会转写整片）。桌面版默认 local/bundled
        #    模式，不走这里；将来要让 HTTP 模式也享受同一优化，需在远端 worker 侧支持
        #    「只处理 [start, end]」的入参。
        return _commentary_run_http(job_id, src_path, vertical, voice, mode,
                                    commentary_type=commentary_type,
                                    highlight_source=highlight_source,
                                    intro_highlight=intro_highlight,
                                    skip_intro_outro=skip_intro_outro,
                                    no_narrate_intro_outro=no_narrate_intro_outro,
                                    retain_pct=retain_pct, web=web,
                                    one_click=one_click, vision=vision,
                                    tts_provider=tts_provider, correct_transcript=correct_transcript,                                     style=style,
                                    style_intensity=style_intensity,
                                    intro_sec=intro_sec, outro_sec=outro_sec,
                                    drama_start_sec=drama_start_sec, drama_end_sec=drama_end_sec,
                                    export_jianying=export_jianying)
    try:
        # 调用前先确认运行环境就绪，失败直接给清晰错误，避免盲目 subprocess 后误报「执行成功」
        if not COMMENTARY_RT.ready():
            raise RuntimeError("解说环境未就绪：" + "；".join(COMMENTARY_RT.issues))
        base = job_id  # 用 job_id 作安全 ascii 文件名，避开中文/空格对 process.py 路径处理的干扰
        # 解说锚点：优先用调用方传入的标题；否则从源文件名推断（下载任务文件名通常含剧集名）。
        # 目的是给 LLM 一个「当前剧集」的锚点，防止解说词凭记忆跑题到别的剧集。
        if not title:
            try:
                title = Path(src_path).stem or ""
            except Exception:
                title = ""
        in_dir = _commentary_root("input")
        out_dir = _commentary_root("output")
        in_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)

        # ---- 时长裁剪（服务端预处理）----
        # 用确定性命名（源路径+起止哈希）切出裁剪片段，script-only 与后续 render 复用同一文件，避免重复切。
        # src_dur 已在函数开头探测，并用于换算 drama_start_sec/drama_end_sec。
        # 🔴 2026-09-19：区间 = 外层裁剪 ∩「正剧范围」，见 _fold_commentary_range 顶部说明。
        #    折进来之后：①「只做中间 15 分钟」的片子只转写 15 分钟（原先转写整片，白等半小时）；
        #    ② 步骤详情不再打误导性的「已裁剪 0~2734s」（整片不再触发裁剪与重编码）。
        ts, te = _fold_commentary_range(src_dur, trim_start, trim_end,
                                        drama_start_sec, drama_end_sec)
        use_src = src_path
        eff_dur = src_dur
        # 用户只要指定了终点（te > 0），即使起点是 0 也要裁剪——旧条件 te > ts > 0 会漏掉
        # 「从片头裁到中间」这种 ts=0 的场景，导致 worker 拿到全片。
        # 🔴 2026-09-20：判据收敛到 _commentary_needs_trim（见其 docstring）——09-19 引入 fold 时
        #    写成 `0.0 < ts < te`，把 ts=0 一起漏掉，直接导致「选 15 分钟 → 成片 46 分钟整片」。
        if _commentary_needs_trim(ts, te, src_dur):
            use_src, _ = _apply_trim(src_path, in_dir, ts, te)
            eff_dur = te - ts
            # 管线拿到的输入已是 [ts, te] 这段的相对时间轴，drama 窗口必须按新基准给，
            # 否则会把绝对秒当相对秒用（整体偏移一个 ts）。裁到起点的那一端＝0，不传。
            _src_ds = float(drama_start_sec or 0.0)
            _src_de = float(drama_end_sec or 0.0)
            _n_ds = max(0.0, _src_ds - ts) if _src_ds > 0 else 0.0
            _n_de = max(0.0, _src_de - ts) if _src_de > 0 else 0.0
            drama_start_sec = _n_ds if _n_ds > 0.5 else None
            drama_end_sec = _n_de if _n_de > 0.5 else None
            _win = ""
            if _src_ds > 0 or _src_de > 0:
                _win = f"，正剧窗口按裁剪后时间轴 {_n_ds:.0f}~{_n_de:.0f}s"
            print(f"[裁剪] 实际处理区间 {ts:.0f}~{te:.0f}s（共 {eff_dur:.0f}s）{_win}")
        else:
            ts, te = 0.0, src_dur   # 整片：展示与 ETA 都用整片时长

        in_file = in_dir / f"{base}.mp4"
        if in_file.exists() or in_file.is_symlink():
            in_file.unlink()
        try:
            os.symlink(use_src, in_file)
        except OSError:
            shutil.copyfile(use_src, in_file)  # 跨挂载点软链失败则退化为复制

        # 🔴 2026-10-20 Windows 实测缺陷：`os.symlink` 有时**不抛异常却也没建出可用链接**
        #    （实测结果是一个 0 字节普通文件），于是 worker 拿到空视频：任务一直 running、
        #    6 个步骤全停在 pending、日志一片空白，用户视角就是「点了没反应」。
        #    这里以「源文件字节数」为唯一判据复核，对不上就删掉重新实体复制（跨卷/无权限都覆盖）。
        try:
            _src_sz = os.path.getsize(use_src)
            _dst_sz = os.path.getsize(in_file)
        except OSError:
            _src_sz, _dst_sz = 1, 0
        if _dst_sz != _src_sz:
            try:
                if in_file.is_symlink() or in_file.exists():
                    in_file.unlink()
            except OSError:
                pass
            shutil.copyfile(use_src, in_file)
            print(f"[桥接] 软链不可用（目标 {_dst_sz}B ≠ 源 {_src_sz}B），已改为实体复制", flush=True)

        # 子进程参数：edit_only 真 → 走 --edit-only（吃已审核脚本，不重跑转录）；否则 --auto。
        # 加 -u 关闭 stdout 块缓冲，让进度行实时回流（ETA 才平滑）。
        # bundled 模式：用主程序自身(sys.executable)以 --vdl-commentary-worker 重入为 worker，
        # 依赖随包内置(已砍 torch)，无需外部 Python / pip，契合自包含铁律。
        _bundled = _commentary_is_bundled()
        extra = _commentary_option_args(commentary_type=commentary_type,
                                        highlight_source=highlight_source,
                                        intro_highlight=intro_highlight,
                                        skip_intro_outro=skip_intro_outro,
                                        no_narrate_intro_outro=no_narrate_intro_outro,
                                        retain_pct=retain_pct, web=web,
                                        one_click=one_click, mode=mode,
                                        style=style, style_intensity=style_intensity, vision=vision,
                                        intro_sec=intro_sec, outro_sec=outro_sec,
                                        drama_start_sec=drama_start_sec, drama_end_sec=drama_end_sec,
                                        bgm=bgm, bgm_file=bgm_file, bgm_volume=bgm_volume,
                                        subtitle_size=subtitle_size, subtitle_color=subtitle_color,
                                        subtitle_border=subtitle_border,
                                        subtitle_border_color=subtitle_border_color,
                                        subtitle_pos=subtitle_pos,
                                        max_chars=max_chars, original_speed=original_speed)
        if edit_only:
            if _bundled:
                args = [sys.executable, "--vdl-commentary-worker",
                        str(in_file), "--edit-only", edit_only]
            else:
                args = [COMMENTARY_RT.python, "-u", "process.py", str(in_file), "--edit-only", edit_only]
            if vertical:
                args.append("--vertical")
            if voice:
                args += ["--voice", voice]
            # 成片文件名用剧名/标题（而非 job_id 哈希名），与 auto 分支一致
            if title:
                args += ["--out-name", title]
            # 审核后出片：把用户在面板上改过的剪辑选项一并传下去（覆盖脚本自带选项）
            args += extra
        else:
            if _bundled:
                args = [sys.executable, "--vdl-commentary-worker", str(in_file), "--auto"]
            else:
                args = [COMMENTARY_RT.python, "-u", "process.py", str(in_file), "--auto"]
            if vertical:
                args.append("--vertical")
            if voice:
                args += ["--voice", voice]
            if title:
                args += ["--title", title]
            # 成片文件名用剧名/标题（而非 job_id 哈希名），避免成片名是一串无意义 hash
            if title:
                args += ["--out-name", title]
            if script_only:
                args.append("--script-only")
            args += extra

        if export_jianying:
            # 成片生成后再导出剪映可编辑草稿（视频轨+配音轨+字幕轨），供用户在剪映二次精修
            if export_jianying == "__default__":
                # 前端留空时，默认落到解说输出目录下的「<片名>-剪映草稿」
                _stem = _safe_output_stem(title) or base
                export_jianying = str(out_dir / f"{_stem}-jianying")
            args += ["--export-jianying", export_jianying]

        run_env = COMMENTARY_RT.env()
        if tts_provider:
            # 语音克隆/海螺等可切换 TTS 服务商（indextts2=本地 IndexTTS2 语音克隆，需先起推理服务）
            run_env["VDL_TTS_PROVIDER"] = tts_provider
        # 本机克隆引擎（qwen3tts / indextts*）跑的是**单进程、共享一个模型**的本地推理服务：
        # 管线默认 VDL_TTS_CONCURRENCY=8（那是给 edge-tts 的网络 IO 场景调的），
        # 对本地模型等于 8 个请求抢同一份权重——实测会把单句推理拖到十几分钟，
        # 渲染整片基本不可能收敛。这里对克隆引擎降到 1（串行），除非用户自己显式设过。
        if (tts_provider or "").strip().lower() in ("qwen3tts", "indextts2", "indextts_mlx"):
            run_env.setdefault("VDL_TTS_CONCURRENCY", "1")
        # ASR 校正改为 opt-in（2026-09-19）：**默认关闭**，只有前端显式传 '1' 才开启。
        # 它是解说链路里最贵的一次调用（全量转写稿一个请求进出各万级 token），而下游
        # Stage1 只看压缩后的 8000 字、Stage2 高光模式只发关键片段，性价比很低。
        # 留空/None/'0' 一律按关闭处理，保证不设置环境变量时行为也确定。
        run_env["VDL_CORRECT_TRANSCRIPT"] = "1" if correct_transcript == "1" else "0"
        if vision:
            # 视觉理解与片头集数卡检测共用 VDL_VISION_* 配置；显式启用避免子进程因环境变量未设而跳过
            run_env["VDL_VISION_ENABLED"] = "1"
            run_env["VDL_INTRO_VISION_ENABLED"] = "1"
        # ===== 原字幕羽化（2026-09-16）=====
        # 前端「原字幕羽化」面板在预览窗口用 Canvas 试调好的带位置 / 擦除方式 / 强度，
        # 通过 env 透给管线 —— **预览看到什么，成片就烧什么**，不会两套结果打架。
        # ⚠️ 必须传「占画面高的比例」而非像素：竖屏管线 canvas 固定 480x854、
        #    横屏是源分辨率，只有比例在两边都成立（管线侧换算见 _prepare_feather）。
        # 没传 band 比例时管线仍走自己的自动探测，行为与改造前完全一致。
        _fopt = _parse_feather_opt(feather_opt)
        if _fopt:
            if "mode" in _fopt:
                run_env["VDL_FEATHER_MODE"] = _fopt["mode"]
            if "band_y_ratio" in _fopt:
                run_env["VDL_FEATHER_BAND_Y_RATIO"] = f"{_fopt['band_y_ratio']:.6f}"
                run_env["VDL_FEATHER_BAND_H_RATIO"] = f"{_fopt['band_h_ratio']:.6f}"
            if "strength" in _fopt and abs(_fopt["strength"] - 1.0) > 1e-6:
                run_env["VDL_FEATHER_STRENGTH"] = f"{_fopt['strength']:.3f}"
            # 逐段自适应羽化带：擦除几何逐段探测，无原字幕的段不擦。
            # 与手动带位置互斥（_parse_feather_opt 已丢弃手动比例），这里只透开关。
            if _fopt.get("dynamic"):
                run_env["VDL_FEATHER_DYNAMIC"] = "1"
                # 自适应之上的手动微调（带顶偏移 / 带高加成，占画面高比例）
                if "dy_ratio" in _fopt:
                    run_env["VDL_FEATHER_DY_RATIO"] = f"{_fopt['dy_ratio']:.6f}"
                if "dh_ratio" in _fopt:
                    run_env["VDL_FEATHER_DH_RATIO"] = f"{_fopt['dh_ratio']:.6f}"
        # 会员状态下传子进程：让管线侧配额闸门（quota.py）放行无限云端（VDL_IS_MEMBER=1）
        try:
            from routers.quota import _is_member
            if _is_member(None):
                run_env["VDL_IS_MEMBER"] = "1"
        except Exception:
            pass
        # 云端账号 token 下传子进程（2026-10-06 跨端云端额度记账）：
        # 管线是独立进程，拿不到 python 对象，只能走 env。没有 token 时 quota.py
        # 会 fail-open 沿用本机计数（旧口径），故未登录云端账号也不影响本地用户。
        try:
            from routers.quota import _cloud_token
            _ctok = _cloud_token(None)
            if _ctok:
                run_env["VDL_CLOUD_TOKEN"] = _ctok
        except Exception:
            pass

        # Popen 实时读取 stdout/stderr，按行追加到 commentary_jobs[job_id]['progress']，
        # 前端轮询时把进度条回显给用户，避免「30 分钟黑屏焦虑」。
        last_lines: list[str] = []

        # ── 失败退还调查（2026-09-15）────────────────────────────────────
        # 管线侧在发起云端调用**之前**就扣了 1 次终身额度；若整条任务最终失败
        # （网络中断 / 超时 / 模型返回空内容），用户既没拿到成片又少了 1 次机会。
        # 这里先快照用量，任务失败时按实际增量退还，保证「没出片就不扣额度」。
        def _quota_snapshot() -> dict:
            try:
                from routers.quota import get_quota_manager
                return get_quota_manager().snapshot()
            except Exception:
                return {}

        def _refund_quota_if(increased: bool) -> None:
            """任务未按预期产出时，退还本次跑任务期间新增的云端额度。"""
            try:
                from routers.quota import get_quota_manager
                qm = get_quota_manager()
                before, after = _q_before, qm.snapshot()
                dl = int(after.get("lifetime_cloud_used", 0)) - int(before.get("lifetime_cloud_used", 0))
                dd = int(after.get("daily_auto_used", 0)) - int(before.get("daily_auto_used", 0))
                if (dl > 0 or dd > 0) and increased:
                    qm.refund(lifetime=dl, daily=dd)
                    _append(f"  [配额] 本次任务未产出成片，已退还免费云端额度（终身 -{dl}，每日 -{dd}）")
            except Exception:
                pass  # 退还失败绝不能掩盖真正的任务错误

        _q_before = _quota_snapshot()

        def _append(line: str) -> None:
            line = line.rstrip("\n")
            if not line:
                return
            last_lines.append(line)
            if len(last_lines) > 80:  # 只保留最近 80 行，避免内存膨胀
                del last_lines[: len(last_lines) - 80]
            with _commentary_lock:
                job = commentary_jobs.setdefault(job_id, {})
                job["progress"] = list(last_lines)
                _commentary_log(job, line)
                _update_commentary_steps(job, line)
                _commentary_eta(job, line, eff_dur)

        with _commentary_lock:
            job = commentary_jobs.setdefault(job_id, {})
            job["started_at"] = job.get("started_at") or time.time()
            job["source_duration"] = eff_dur
            job["trim_start"] = ts
            job["trim_end"] = te
            job["trim_path"] = use_src if use_src != src_path else ""
            job["src_path"] = src_path
            steps = _ensure_commentary_steps(job)
            steps[0]["status"] = "running"
            # 优先展示用户上传时的原始文件名，upload 端点会把磁盘文件名统一改名为 upload.<ext>
            # 所以 src_path.basename 永远是 upload.mp4 这种占位名，用 src_filename 兜出真实名
            display_src = (src_filename or "").strip() or Path(src_path).name
            # 只在**真的裁了**的时候提示，且写清是「只处理这一段」——
            # 旧文案「已裁剪 0~2734s」（= 整片）会让用户以为没生效（2026-09-19 用户实测）。
            steps[0]["detail"] = f"源视频: {display_src}" + (
                f"（只处理 {ts:.0f}~{te:.0f}s 这段，共 {eff_dur:.0f}s）" if use_src != src_path else "")
            steps[0]["updated_at"] = time.time()

        try:
            proc = subprocess.Popen(
                args, cwd=str(COMMENTARY_DIR),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=run_env,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"解说管线无法启动：{exc}") from exc

        assert proc.stdout is not None
        for line in proc.stdout:
            _append(line)
        try:
            ret = proc.wait(timeout=COMMENTARY_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            _refund_quota_if(True)
            tail = "\n".join(last_lines[-20:]) or "无输出"
            raise RuntimeError(
                f"解说管线超过 {COMMENTARY_TIMEOUT_SECONDS} 秒未完成，已终止。\n最近输出：\n{tail}"
            )
        if ret != 0:
            _refund_quota_if(True)
            tail = "\n".join(last_lines[-20:]) or "无输出"
            raise RuntimeError(f"解说管线退出码 {ret}。\n最近输出：\n{tail}")

        if script_only:
            # --script-only 模式：只出了 script.json，不生成成片
            script_path = _commentary_root("work") / f"{base}.script.json"
            if not script_path.exists():
                _refund_quota_if(True)
                tail = "\n".join(last_lines[-20:]) or "无输出"
                raise RuntimeError(f"解说管线执行成功但未找到脚本文件：{script_path}\n输出：\n{tail}")
            with _commentary_lock:
                job = commentary_jobs[job_id]
                _ensure_commentary_steps(job)
                now = time.time()
                # --script-only 只跑到「生成 AI 解说词」为止：配音/渲染尚未执行，
                # 绝不能标 done（否则 UI 全绿、用户误以为成片已生成）。
                _DONE_IN_SCRIPT_ONLY = ("准备输入文件", "转写视频语音", "生成 AI 解说词")
                for s in job["steps"]:
                    if s["name"] in _DONE_IN_SCRIPT_ONLY:
                        s["status"] = "done"
                        s["updated_at"] = now
                    elif s["name"] in ("合成 AI 配音", "渲染成片"):
                        s["status"] = "pending"
                        s["detail"] = "等待脚本审核通过后执行"
                        s["updated_at"] = now
                    elif s["name"] == "完成":
                        s["status"] = "pending"
                        s["detail"] = "解说词已生成，请先审核脚本再渲染成片"
                        s["updated_at"] = now
                _commentary_log(job, "解说词已生成，等待人工审核后再渲染成片")
                job.update(status="script_ready", script_path=str(script_path), output_path="")
            # 解说词已真实产出（转写 + 大模型都跑过了）⇒ 此时才落账。
            # 后续「审核通过后渲染」复用父任务账单且已置 charged，不会二次扣。
            _commentary_charge_on_success(job_id)
        else:
            # 成片命名：优先按 --out-name（剧名/标题）清洗后的前缀查找；
            # 找不到再退回 job_id(base) 兜底（旧任务或标题为空时）。
            out_stem = _safe_output_stem(title) or base
            candidates = sorted(
                (p for p in out_dir.glob(f"{out_stem}*.mp4") if p.name != in_file.name),
                key=lambda p: p.stat().st_mtime, reverse=True,
            )
            if not candidates:
                candidates = sorted(
                    (p for p in out_dir.glob(f"{base}*.mp4") if p.name != in_file.name),
                    key=lambda p: p.stat().st_mtime, reverse=True,
                )
            out = next(iter(candidates), None)
            if not out:
                _refund_quota_if(True)
                tail = "\n".join(last_lines[-20:]) or "无输出"
                raise RuntimeError(f"解说管线执行成功但未找到成片。process.py 输出：\n{tail}")
            with _commentary_lock:
                job = commentary_jobs[job_id]
                _ensure_commentary_steps(job)
                now = time.time()
                for s in job["steps"]:
                    if s["status"] in ("pending", "running"):
                        s["status"] = "done"
                        s["updated_at"] = now
                job.update(status="completed", output_path=str(out))
            # 成片已落盘 ⇒ 此时才扣积分（失败路径根本走不到这里，自然不扣）
            _commentary_charge_on_success(job_id)
            # 写入 BGM 状态 manifest：bgm=off 视为待配乐（draft），否则视为已成片（final）
            try:
                _write_bgm_state(str(out), bgm=bgm or "off", volume=bgm_volume, source="render")
            except Exception:
                logger.exception("写 BGM 状态 manifest 失败：%s", out)
    except Exception as exc:  # noqa: BLE001
        _commentary_mark_error(job_id, str(exc))
        with _commentary_lock:
            commentary_jobs.setdefault(job_id, {})["status"] = "failed"
            commentary_jobs[job_id]["error"] = str(exc)[:800]
        logger.exception("解说任务 %s 失败", job_id)

def _commentary_run_http(job_id: str, src_path: str, vertical: bool, voice: str, mode: str | None = None, commentary_type: str = "deep_hl", highlight_source: str = "ai", intro_highlight: bool = False, skip_intro_outro: bool = False, no_narrate_intro_outro: bool = True, retain_pct: float | None = None, web: bool = False, one_click: bool = False, style: str = "none", vision: bool = False, tts_provider: str = "", correct_transcript: str = "", intro_sec: float | None = None, outro_sec: float | None = None, drama_start_sec: float | None = None, drama_end_sec: float | None = None, export_jianying: str = "", style_intensity: int = 65) -> None:
    """HTTP 模式：把已下载视频 POST 给独立解说 worker，轮询取回成片到主站本地。"""
    endpoint = COMMENTARY_ENDPOINT
    headers = {"X-Worker-Token": COMMENTARY_TOKEN} if COMMENTARY_TOKEN else {}
    now = time.time()
    http_steps = [
        {"name": "上传视频到 worker", "status": "pending", "detail": "", "created_at": now, "updated_at": now},
        {"name": "worker 渲染中", "status": "pending", "detail": "", "created_at": now, "updated_at": now},
        {"name": "下载成片", "status": "pending", "detail": "", "created_at": now, "updated_at": now},
        {"name": "完成", "status": "pending", "detail": "", "created_at": now, "updated_at": now},
    ]
    with _commentary_lock:
        job = commentary_jobs.setdefault(job_id, {})
        job["steps"] = http_steps
        job["logs"] = []

    def _set_http_step(idx: int, status: str, detail: str = "") -> None:
        with _commentary_lock:
            steps = commentary_jobs.get(job_id, {}).get("steps", [])
            now2 = time.time()
            for i, s in enumerate(steps):
                if i < idx and s["status"] == "pending":
                    s["status"] = "done"
                    s["updated_at"] = now2
            if 0 <= idx < len(steps):
                steps[idx]["status"] = status
                steps[idx]["detail"] = detail[:200]
                steps[idx]["updated_at"] = now2

    def _http_log(line: str) -> None:
        with _commentary_lock:
            job = commentary_jobs.get(job_id)
            if not job:
                return
            ts = time.strftime("%H:%M:%S", time.localtime())
            job.setdefault("logs", []).append(f"{ts}  {line.strip()}")
            if len(job["logs"]) > 200:
                job["logs"][:] = job["logs"][-200:]

    try:
        _set_http_step(0, "running", f"上传 {Path(src_path).name}")
        with open(src_path, "rb") as fh:
            data = {
                "vertical": "true" if vertical else "false",
                "voice": voice,
                "commentary_type": commentary_type,
                "highlight_source": highlight_source,
                "intro_highlight": "true" if intro_highlight else "false",
                "skip_intro_outro": "true" if skip_intro_outro else "false",
                "no_narrate_intro_outro": "true" if no_narrate_intro_outro else "false",
                "web": "true" if web else "false",
                "one_click": "true" if one_click else "false",
                "style": style or "none",
            "vision": "true" if vision else "false",
            "tts_provider": tts_provider or "",
            "correct_transcript": correct_transcript or "",
            "export_jianying": export_jianying or "",
        }
            if retain_pct is not None:
                data["retain_pct"] = str(retain_pct)
            if intro_sec is not None and intro_sec > 0:
                data["intro_sec"] = str(intro_sec)
            if outro_sec is not None and outro_sec > 0:
                data["outro_sec"] = str(outro_sec)
            if drama_start_sec is not None and drama_start_sec > 0:
                data["drama_start_sec"] = str(drama_start_sec)
            if drama_end_sec is not None and drama_end_sec > 0:
                data["drama_end_sec"] = str(drama_end_sec)
            if mode:
                data["mode"] = mode
            resp = requests.post(
                f"{endpoint}/render",
                files={"video": (f"{job_id}.mp4", fh, "video/mp4")},
                data=data,
                headers=headers,
                timeout=600,
            )
        if resp.status_code != 200:
            raise RuntimeError(f"解说 worker /render 返回 {resp.status_code}: {resp.text[:400]}")
        wjob = resp.json().get("job_id")
        if not wjob:
            raise RuntimeError("解说 worker 未返回 job_id")
        _set_http_step(0, "done", "已提交渲染任务")
        _http_log(f"worker job_id: {wjob}")

        _set_http_step(1, "running", f"worker job: {wjob}")
        deadline = time.time() + COMMENTARY_TIMEOUT_SECONDS
        while time.time() < deadline:
            time.sleep(5)
            st = requests.get(f"{endpoint}/status/{wjob}", headers=headers, timeout=30).json()
            status = st.get("status")
            _http_log(f"worker status: {status}")
            if status == "completed":
                break
            if status == "failed":
                raise RuntimeError("解说 worker 渲染失败: " + str(st.get("error", ""))[:600])
        else:
            raise RuntimeError("解说 worker 渲染超时（超过 VDL_COMMENTARY_TIMEOUT）")
        _set_http_step(1, "done", "worker 渲染完成")

        _set_http_step(2, "running", "正在下载成片")
        fr = requests.get(f"{endpoint}/file/{wjob}", headers=headers, stream=True, timeout=(10, 600))
        if fr.status_code != 200:
            raise RuntimeError(f"解说 worker /file 返回 {fr.status_code}")
        out_path = COMMENTARY_LOCAL_OUTPUT / f"{job_id}.mp4"
        downloaded = 0
        with open(out_path, "wb") as o:
            for chunk in fr.iter_content(1024 * 1024):
                if chunk:
                    o.write(chunk)
                    downloaded += len(chunk)
        _set_http_step(2, "done", f"已下载 {downloaded / 1024 / 1024:.1f} MB")

        _set_http_step(3, "done", "成片已就绪")
        with _commentary_lock:
            commentary_jobs[job_id].update(status="completed", output_path=str(out_path))
        _commentary_charge_on_success(job_id)
    except Exception as exc:  # noqa: BLE001
        _set_http_step(1 if http_steps[0]["status"] == "done" else 0, "error", str(exc)[:200])
        with _commentary_lock:
            commentary_jobs.setdefault(job_id, {})["status"] = "failed"
            commentary_jobs[job_id]["error"] = str(exc)[:800]
        logger.exception("解说任务 %s 失败(http 模式)", job_id)

async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        demoted = store.purge_expired()
        if demoted:
            # B5：完成任务到期不再是整条删除，而是降级为历史条目（文件清掉、记录保留）
            logger.info("已将 %s 个到期完成任务转为历史条目（文件已清理）", demoted)
        try:
            store.flush()   # 兜底：节流窗口内没再被触碰的脏任务状态也落盘（B2）
        except Exception:
            logger.exception("任务状态落盘失败")
        # 解说 work 目录保留期清理（防 21GB 级无限累积）
        try:
            await asyncio.to_thread(_purge_commentary_work)
        except Exception:
            logger.exception("解说 work 清理失败")

@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    # ⚠️ 启动期目录清理必须放后台线程（2026-09-24 第四次踩 TCC 坑）：
    # purge_orphans / _purge_commentary_work 会 iterdir/open ~/Downloads 等
    # TCC 保护目录。ad-hoc 签名的包每次重建 cdhash 变化 → 首次运行系统弹
    # 「访问下载文件夹」授权框；**同步**执行时没人点授权 → open() 永久阻塞
    # → uvicorn 永不监听（症状：窗口开了、8321 连不上、日志 240s 假就绪）。
    # 放后台线程后：启动路径不再触碰 TCC，授权框点不点都不影响后端起来。
    def _deferred_startup_cleanup() -> None:
        try:
            orphans = store.purge_orphans()
            if orphans:
                logger.info("已清理 %s 个上次运行遗留的任务目录", orphans)
        except Exception:
            logger.exception("启动时孤儿任务清理失败")
        try:
            _purge_commentary_work()
        except Exception:
            logger.exception("启动时解说 work 清理失败")

    threading.Thread(target=_deferred_startup_cleanup,
                     name="vdl-startup-cleanup", daemon=True).start()
    # 桌面版种子下载：启动 libtorrent session（libtorrent 缺失时内部为空操作）
    if TORRENT_ENABLED and torrent_mod.available():
        try:
            torrent_manager.start()
        except Exception:
            logger.exception("启动种子下载管理器失败")
    cleaner = asyncio.create_task(_cleanup_loop())
    # 公共 Cookie 池后台探测（chrqj 等需登录态的站，避免公共池静默失效）
    try:
        _start_cookie_pool_watchdog()
    except Exception:
        logger.exception("启动公共 Cookie 池探测失败")
    # 启动即确保超级用户标记（后台管理面板授权：admin.json / VDL_ADMIN_IDENTIFIER / 首个注册账号）
    # 🔴 安全审计 2026-10-11（P1-2）：启动时**不允许**「无超管则提升最早注册账号」这条兜底
    #    —— 公网部署（网页版）每次重启都会跑这里，等于把超管送给历史最早的那个注册账号。
    #    启动阶段只认显式名单；桌面单机的兜底由登录/注册路径（本机回环）负责。
    try:
        from auth_store import ensure_superusers
        ensure_superusers(allow_bootstrap=False)
    except Exception:
        logger.exception("确保超级用户失败")
    # AI 引擎空闲自动卸载（2026-09-30）：后台巡检，空闲超阈值（默认 3 分钟）释放
    # onnx 会话，为内存吃紧的用户回收模型权重占用的内存。
    # ⚠️ 必须等上面的 routers 都 import 完（注册动作发生在模块导入时）再启动，
    #    否则注册还没发生，巡检线程空转。lifespan 在此之后运行，顺序正确。
    try:
        engine_idle.ensure_started()
    except Exception:
        logger.exception("启动引擎空闲卸载巡检失败")
    # 配音引擎（edge-tts）自动巡查：启动 25s 后首次探活，之后每 6 小时一次；
    # 判定「版本被微软作废」时自动下载新版到用户目录热补丁层并热加载。
    # 必须放后台线程：探活要联网合成一句话，绝不能挡住服务监听。
    try:
        tts_guard.ensure_started()
    except Exception:
        logger.exception("启动配音引擎巡查失败")
    # 解析需求历史回填授权中心（2026-10-31）：登录用户启动时把本机日志里云端
    # 没见过的需求批量补报（水位断点幂等；未登录 / 离线 / 测试环境自动跳过）。
    # 必须后台线程：要走网络，绝不能挡住服务监听。
    try:
        _cloud_rtr._backfill_demand_in_background()
    except Exception:
        logger.exception("启动解析需求回填失败")
    yield
    cleaner.cancel()
    if TORRENT_ENABLED:
        try:
            torrent_manager.stop()
        except Exception:
            pass
    executor.shutdown(wait=False, cancel_futures=True)
    prober.shutdown(wait=False, cancel_futures=True)

app = FastAPI(title="视频下载站", version="1.0.0", lifespan=lifespan)

# 跨域 CORS：公开站默认允许所有来源（allow_credentials=False，不携凭证，安全）。
# 部署者可通过 VDL_ALLOW_ORIGINS 或双节点 PEER_ENDPOINT 限定具体来源；否则回退 "*"。
# 注意：中间件必须始终注册，否则跨域自托管（含带 X-Subscription-Key 的订阅请求）
# 的浏览器预检会被 405 拒绝。
_cors_origins = ALLOW_ORIGINS if ALLOW_ORIGINS else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Subscription-Key", "X-Api-Key"],
)

# 前端脚本未做版本化，pywebview/WKWebView 易缓存旧 app.js，导致修复不生效。
# 对 HTML/JS/CSS 及首页强制 no-store，确保客户端每次都拉取最新前端。
@app.middleware("http")
async def _no_cache_frontend(request: Request, call_next):
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".js", ".html", ".css", ".htm")):
        resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp
logger.info("CORS 已开启，允许来源：%s", ", ".join(_cors_origins))

# --------------------------------------------------------------------------- #
# 可选 API 鉴权（对外给用户使用时建议开启）
# 设置 VDL_API_TOKEN 后，除 /api/nodes（前端要先拿到 authRequired 才能引导输入 token）
# 与静态资源外，所有 /api/* 请求必须携带正确 token，否则 401。
# token 取自 Authorization: Bearer <t> 或 X-Api-Key: <t>。
# 未设置则维持当前无鉴权行为（个人本机/私用）。
# --------------------------------------------------------------------------- #
API_TOKEN = (os.environ.get("VDL_API_TOKEN") or "").strip()
AUTH_REQUIRED = bool(API_TOKEN)

class _ApiTokenMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not AUTH_REQUIRED:
            return await call_next(request)
        path = request.url.path
        # 静态资源与节点信息接口放行：前端需先读 authRequired 才能引导用户输入 token
        if not path.startswith("/api/") or path == "/api/nodes":
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        token = ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        else:
            token = (request.headers.get("X-Api-Key") or "").strip()
        if token and token == API_TOKEN:
            return await call_next(request)
        return JSONResponse(
            status_code=401,
            content={"error": "缺少或错误的 API Token", "hint": "服务端已启用 VDL_API_TOKEN，请输入访问令牌"},
        )

app.add_middleware(_ApiTokenMiddleware)

# --------------------------------------------------------------------------- #
# 登录门禁（2026-09-26 用户要求「所有功能必须登录才能使用」）
#
# 产品行为对齐下载页：未登录点「开始下载」先弹登录框，登录后自动继续。
# 前端已用 document 捕获阶段统一拦截并弹框（web/app.js 的 _LOGIN_GATED_ACTIONS），
# 这里做**服务端兜底**——防止改前端 JS 或直接 curl 本机 8321 绕过登录白嫖功能。
#
# 只拦「产生结果 / 消耗算力」的写操作（POST）。状态查询与文件读取一律放行：
# 未登录时前端本就拿不到这些任务，拦了只会破坏轮询与预览。
# 授权中心 / 账号 / 会员 / 管理 / 分享落地 / 系统更新等前缀不在拦截范围内
# （其中 /api/system/update 自己已有鉴权）。
# --------------------------------------------------------------------------- #
_LOGIN_GATED_EXACT = {
    # 2026-09-27 用户要求回调门禁位置：「解析链接」是下载的前置步骤（只是拿到清晰度/标题，
    # 不产出文件），点它不该弹登录；只有真正落盘的「开始下载」/「批量下载」才要登录。
    # 故 /api/resolve 从这里移除，前端 _LOGIN_GATED_ACTIONS 同步移除 resolveBtn。
    "/api/download",                      # 创建下载任务
    "/api/batch",                         # 批量下载
    "/api/commentary",                    # 生成解说
    "/api/commentary/script-only",        # 生成解说脚本
    "/api/commentary/script-only/upload",
    "/api/commentary/stash",
    "/api/commentary/upload",             # 上传素材/音色录制
    "/api/subtitles/extract",             # 字幕提取
    "/api/subtitles/burn",                # 烧录硬字幕
    "/api/subtitles/translate",           # 字幕翻译
    "/api/subtitle/extract",              # 本地字幕提取（云侧）
    "/api/process/run",                   # 队列处理
    "/api/retention/run",                 # 存储清理
    "/api/torrents/add",
    "/api/torrents/add-file",
    "/api/share/upload_path",             # 扫码分享上传
    "/api/share/upload_file",
}
_LOGIN_GATED_PREFIXES = (
    "/api/convert",            # /api/convert、/api/convert/local
    "/api/upload-convert",     # 本地文件转换（视频 / 音乐 / 图片共用）
    "/api/upload-chunk",       # 分片上传（转换前置）
    "/api/concat",             # 视频/音频桥接
    "/api/compress",           # /api/compress/local、/api/compress/finish
    "/api/sr",                 # 高清修复 / 超分
    "/api/matting",            # 一键抠图（含模型预热/切换）
    "/api/dw",                 # 图片 / PDF / 视频去水印（含编辑期抽帧）
    "/api/subscriptions",      # 订阅追更
    # 🔴 安全审计 2026-10-11（P2-21）：证件照（2026-10-10 新增）漏登记 ——
    #    `/api/idphoto/make`、`/api/idphoto/{job}/render` 都是「产出结果」的 POST，
    #    却只在限流之下（`VDL_API_TOKEN` 未设时未登录即可制图）。
    #    GET 子路径（查询/取图）不受本门禁影响（门禁只拦 POST）。
    "/api/idphoto",            # 证件照制作 / 重新出图
    # 🔴 安全审计 2026-10-11（P2-15）：文件变网页（`build` 读任意本机路径并写产物）
    #    此前完全不在门禁内，未登录即可触发（盲读 + 任意可写目录投放）。
    "/api/pagetool",           # 文件变网页（生成 / 上传生成）
    "/api/commentary/render",      # 渲染成片
    "/api/commentary/remux-bgm",   # 重新混音
    "/api/commentary/preview",
)


# 「收尾类」任务控制端点放行：暂停 / 继续 / 取消 / 中止已有任务不会产生新结果，
# 但若登录态中途失效（token 过期）而任务仍在跑，用户必须还能把任务停下来。
# 🔴 安全审计 2026-10-13（F-09）：旧实现按「后缀」放行（任何以 /pause|/resume|/cancel|/abort
#    结尾的路径都跳过登录门禁），属过宽白名单 —— 未来新增同后缀写端点会被无声豁免。
#    改为「精确前缀 + 后缀」双匹配：只放行已知的任务控制端点，杜绝后缀通配。
_LOGIN_GATE_EXEMPT_TASK_CTRL = (
    ("/api/torrents/", ("/pause", "/resume")),
    ("/api/dw/video/", ("/pause", "/resume", "/cancel")),
    ("/api/commentary/clone-env/", ("/cancel",)),
    ("/api/tasks/", ("/pause", "/resume", "/cancel", "/abort")),
)
_LOGIN_GATE_EXEMPT_EXACT = frozenset({
    "/api/upload-chunk/abort",
})


def _login_gated_path(path: str) -> bool:
    if path in _LOGIN_GATE_EXEMPT_EXACT:
        return False
    for _pfx, _sufs in _LOGIN_GATE_EXEMPT_TASK_CTRL:
        # 常量里的 suffix 写成 "/pause" 这种**带斜杠**形式（见上），而 rsplit 取出的是
        # 不带斜杠的末段；此前直接比较 ⇒ 永不命中 ⇒ 豁免整段失效（2026-10-14 实测：
        # /api/dw/video/<id>/cancel 被误拦）。这里补回斜杠再比。
        if path.startswith(_pfx) and ("/" + path[len(_pfx):].rsplit("/", 1)[-1]) in _sufs:
            return False
    if path in _LOGIN_GATED_EXACT:
        return True
    return any(path.startswith(p) for p in _LOGIN_GATED_PREFIXES)


# 门禁开关：默认开启。VDL_LOGIN_GATE=0 可整体关闭——离线测试需要在「未模拟登录」的
# 前提下直连这些端点，此时由 tests/test_login_gate.py 单独打开开关验证拦截行为。
LOGIN_GATE_ENABLED = (os.environ.get("VDL_LOGIN_GATE", "1").strip() != "0")


@app.middleware("http")
async def _login_gate(request: Request, call_next):
    if (LOGIN_GATE_ENABLED and request.method == "POST"
            and _login_gated_path(request.url.path)):
        from user_membership import get_current_user_id   # 局部导入，避免循环依赖
        if not get_current_user_id(request):
            return JSONResponse(
                status_code=401,
                content={
                    "ok": False,
                    "code": "NO_AUTH",
                    "error": "请先登录账号后再使用该功能",
                    "hint": "登录 / 注册后即可继续（会员权益与积分按账号计算）",
                },
            )
    return await call_next(request)

# --------------------------------------------------------------------------- #
# 请求模型 & 错误处理
# --------------------------------------------------------------------------- #

class ResolveRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    cookie: str = Field(default="", max_length=8192)
    proxy: str = Field(default="", max_length=256)

class DownloadRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    # 前端 /api/resolve 已解析出的视频标题（剧集名+单集标题），创建任务时直接写入，
    # 避免下载完成前解说取不到 title 而 fallback 成无意义 hash/upload。
    title: str = Field(default="", max_length=256)
    quality: str = Field(default=downloader.BEST_KEY, max_length=16)
    cookie: str = Field(default="", max_length=8192)
    proxy: str = Field(default="", max_length=256)
    # 提取文案："" 不提取 / "spoken" 口播文案 / "description" 发布简介 / "both" 两者
    extract_script: str = Field(default="", max_length=16)
    # 精确指定 CDN 源（如腾讯 hd-1/shd-3），空则按清晰度自适应；由「测速选源」自动填入
    format_id: str = Field(default="", max_length=64)
    # 下载加速：m3u8 分片并行段数（0=用默认 32，范围 1-64）。腾讯等单连接限速站提高可线性提速
    concurrent_fragments: int = Field(default=0, ge=0, le=64)
    # 下载器：native（默认，yt-dlp 原生）/ aria2c（外部下载器，需本机已装，缺失自动回退）
    downloader: str = Field(default="native", max_length=16)
    # 在线观看：前端从解析结果传入，存入任务后任务面板可直接打开观看
    play_url: str = Field(default="", max_length=2048)
    watch_options: list[dict] = Field(default_factory=list)
    is_hls: bool = False
    # CDP 嗅探直链的防盗链 Referer（嗅探来源页）。嗅探 URL 的 host 是 CDN，
    # _base_options 按 host 生成的 Referer 会错（如 upos-*.bilivideo.com 必须带
    # https://www.bilibili.com/），必须显式透传；空值时维持旧行为。
    referer: str = Field(default="", max_length=2048)

class ConvertRequest(BaseModel):
    """格式转换请求（与 routers/convert.py 共享，故保留在 app.py 公开契约层，不迁入 core）。"""
    task_id: str
    target: str
    resolution: str = "original"

def _require_task(task_id: str, device_id: str = ""):
    """取任务。桌面单机无设备隔离，device_id 参数仅为兼容 web 版调用签名保留。

    ⚠️ 安全审计 P0-2：本函数**不做归属校验**，仅按 id 取。对外端点请改用
    `_require_owned_task`，否则会跨用户泄漏/操作他人任务。
    """
    task = store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return task


def _task_owner(request) -> str:
    """任务归属标识（安全审计 P0-2）。

    · 已登录 → "u:" + uid：uid 由服务端从 HMAC 签名 token 解出，客户端**无法伪造**；
    · 未登录 → "d:" + 设备标识：与转换/压缩等既有隔离同源（X-Device-Id / ?device=）；
    · 两者都拿不到 → ""（匿名桶，仅历史遗留任务会出现）。
    前缀区分命名空间，避免 uid 与设备标识串号。
    """
    uid = None
    try:
        from user_membership import get_current_user_id
        uid = get_current_user_id(request)
    except Exception:
        uid = None
    if uid:
        return "u:" + str(uid)
    try:
        dev = (request.headers.get("X-Device-Id")
               or request.query_params.get("device") or "").strip()
    except Exception:
        dev = ""
    return ("d:" + dev[:64]) if dev else ""


def _member_store_for_owner(owner_id: str):
    """按任务归属取生效会员 store —— 给**没有 Request 的后台线程**用（2026-10-20）。

    订阅自动追更跑在 watchdog 线程里，拿不到 Request；而计额度必须落到**订阅主人**
    的账本上，否则会记到匿名桶里（等于谁登录都看不到自己订阅吃掉的次数）。
    归属标识与 `_task_owner()` 同源（"u:<uid>" / "d:<device>" / ""），直接交给
    `user_membership.store_for_owner()` 还原，保证与请求链路的选店逻辑同一份实现。

    取不到时返回 None，调用方按 **fail-open** 放行（宁漏计一次，也不让整轮追更挂掉）。
    """
    try:
        from user_membership import store_for_owner
        return store_for_owner(owner_id)
    except Exception:  # noqa: BLE001 - 会员态是尽力而为，绝不因读不到就中断追更
        logger.exception("按归属取会员 store 失败：owner=%s", owner_id)
        return None


def _require_owned_task(task_id: str, request):
    """取任务并校验归属：非本人任务一律 404（不返回 403，避免任务 id 可枚举）。

    历史遗留任务（owner_id 为空，升级前创建）保持可访问，避免老用户历史任务
    凭空消失；新任务一律带 owner_id。
    """
    task = _require_task(task_id)
    req_owner = _task_owner(request)
    if not req_owner:
        # 匿名请求（无登录态、无设备标识）不 touch 任何任务（2026-10-12 安全修复）
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    owner = getattr(task, "owner_id", "") or ""
    if owner and owner != req_owner:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return task

def _run_convert(job_id: str, src: str, target: str, resolution: str,
                bitrate: str = "", audio: bool = True, rotate: int = 0,
                remux: bool = False, src_is_temp: bool = False,
                audio_bitrate: str = "", image_quality: int = 0,
                resize: int = 0, flatten_alpha: bool = True,
                is_image: bool = False) -> None:
    """后台线程：ffmpeg 转码，更新 CONVERT_JOBS 状态。
    新增参数（上传转码用）：bitrate 视频码率、audio 是否保留音轨、
    rotate 竖屏旋转(0/90/180/270)、remux 仅换容器无损(-c copy)。
    audio_bitrate：音频目标的音质档位（如 "320k"），支持音乐转换选码率。
    image_quality/resize/flatten_alpha：图片目标（CONVERT_IMAGE）的质量档、
    长边像素缩放、不支持 alpha 的目标是否白底合成（图片转换 tab 用）。
    is_image：图片转换 tab 显式标记（gif 同名目标在视频 tab 是"视频→动图"、
    在图片 tab 是"图片→gif"，二者参数不同，必须靠此标志分流）。
    src_is_temp：src 是否为上传落盘的临时文件，True 时转码结束（成败都）
    清理，避免 UPLOAD_TMP 无限堆积；task 模式（已下载文件）必须为 False。
    """
    job = CONVERT_JOBS.get(job_id)
    if not job:
        return
    out = None
    try:
        out = Path(job["out_path"])
        audio_only = target in CONVERT_AUDIO
        if target == "webp" and (is_image or target in CONVERT_IMAGE):
            # WebP 特例：捆绑的 LGPL ffmpeg 无 libwebp 编码器，改用 Pillow（MIT，已随包捆绑）
            # 直接出图（保留 alpha、等比只缩不放），不走 ffmpeg。
            job["stage"] = "转码中"
            job["progress"] = 60
            _convert_image_webp(src, out, image_quality, resize)
            if not out.exists() or out.stat().st_size == 0:
                raise RuntimeError("WebP 转换未产出有效文件")
        else:
            cmd = [FFMPEG_BIN, "-y", "-i", src]
            if target in CONVERT_IMAGE or (is_image and target == "gif"):
                # 图片：单帧输出，无音轨；质量/缩放/白底合成见 _image_encode_args
                cmd += _image_encode_args(target, image_quality, resize, flatten_alpha)
            elif target == "gif":
                cmd += CONVERT_TARGETS["gif"]
            elif remux and rotate == 0:
                # 仅换容器无损复制，忽略码率/分辨率/旋转（旋转需滤镜，与 -c copy 不兼容）
                cmd += ["-c", "copy"]
            else:
                # 音频目标 + 指定音质档位 → 动态参数覆盖固定表（128k/192k/256k/320k）
                _aargs = _audio_encode_args(target, audio_bitrate) if audio_only else None
                cmd += _aargs if _aargs else CONVERT_TARGETS[target]
                if not audio_only:
                    vf = []
                    if resolution != "original":
                        h = {"1080": "1080", "720": "720", "480": "480"}.get(resolution)
                        if h:
                            vf.append(f"scale=-2:{h}")
                    if rotate in (90, 180, 270):
                        # transpose 系列全都是 90° 转置（输出宽高互换）：1=顺时针 90°，
                        # 2=逆时针 90°（等价顺时针 270°）。180° 不能用 transpose：
                        # 原先的 transpose=3 实为「顺时针 90°+垂直翻转」，既不是 180°
                        # 旋转，还会把宽高错误互换——用户选 180° 会把横屏视频转成竖屏。
                        tf = {90: "transpose=1", 180: "hflip,vflip", 270: "transpose=2"}[rotate]
                        vf.append(tf)
                    if vf:
                        cmd += ["-vf", ",".join(vf)]
                    if bitrate:
                        cmd += ["-b:v", str(bitrate)]
                    if not audio:
                        cmd += ["-an"]
            cmd.append(str(out))
            job["stage"] = "转码中"
            job["progress"] = 0
            # 流式读取 ffmpeg stderr，解析总时长与当前进度，实时回写进度百分比
            _re_dur = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
            _re_time = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
            total_dur = 0.0
            proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, bufsize=1, text=True)
            for line in proc.stderr:
                if not total_dur:
                    m = _re_dur.search(line)
                    if m:
                        total_dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                m = _re_time.search(line)
                if m and total_dur > 0:
                    cur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                    job["progress"] = int(min(100, max(0, cur / total_dur * 100)))
            proc.wait(timeout=1800)
            if proc.returncode != 0:
                raise RuntimeError("ffmpeg 执行失败")
            if not out.exists() or out.stat().st_size == 0:
                raise RuntimeError("ffmpeg 未产出有效文件")
        # 可选：存入媒体库（DOWNLOAD_DIR 磁盘目录，scan_library 会自动收录）
        if job.get("to_library"):
            dest = DOWNLOAD_DIR / out.name
            if dest.exists() and dest.resolve() != out.resolve():
                stem = out.stem + f"_{uuid.uuid4().hex[:6]}"
                dest = DOWNLOAD_DIR / f"{stem}{out.suffix}"
            shutil.copy2(out, dest)
            try:
                job["library_id"] = library_mod.encode_id(
                    dest.resolve().relative_to(DOWNLOAD_DIR.resolve()).as_posix())
            except Exception:
                job["library_id"] = ""
        job["status"] = "completed"
        job["progress"] = 100
        logger.info("convert %s done -> %s", job_id, out.name)
    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)[:400]
        logger.warning("convert %s failed: %s", job_id, e)
    finally:
        # 上传临时源文件：转码结束（无论成败）后清理，避免 UPLOAD_TMP 无限堆积
        if src_is_temp and src:
            try:
                _src = Path(src)
                if _src.exists() and (out is None or _src.resolve() != out.resolve()):
                    _src.unlink(missing_ok=True)
            except Exception:
                logger.warning("convert %s cleanup temp src failed: %s", job_id, src)

@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """兜底：任何未被具体 handler 覆盖的异常，向前端返回可读的错误原因。

    默认 FastAPI（debug=False）只会返回笼统的 "Internal Server Error"，
    排查时拿不到真实堆栈。这里把异常类型+消息透传给前端，并写服务端日志，
    让用户（双击启动 App 也能）直接看到 500 的真实成因。
    """
    logger.exception("未捕获异常 %s %s: %s", request.method, request.url.path, exc)
    # 不要拦截 FastAPI 自身的 HTTPException（如 402 订阅提示、400 参数错误）
    from fastapi import HTTPException as _HTTPException

    if isinstance(exc, _HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    msg = f"{type(exc).__name__}: {str(exc)[:200]}"
    # App 可观测性：把未捕获异常留痕到本地 .ops_events.log（供「运维看板 / 诊断」归集）。
    try:
        _opl = Path.home() / ".video-downloader" / ".ops_events.log"
        _opl.parent.mkdir(parents=True, exist_ok=True)
        _opl.open("a", encoding="utf-8").write(
            json.dumps({"ts": int(time.time()), "level": "error",
                        "message": msg,
                        "extra": {"path": str(request.url.path), "method": request.method}},
                       ensure_ascii=False) + "\n")
    except Exception:
        pass
    return JSONResponse(
        status_code=500,
        content={
            "error": msg,
            "hint": "服务端未预期错误，请把上方错误信息反馈，或查看服务端日志",
            "detail": msg,
        },
    )

@app.exception_handler(LinkError)
async def handle_link_error(_: Request, exc: LinkError) -> JSONResponse:
    status = 415 if isinstance(exc, UnsupportedPlatformError) else 400
    content = {"error": exc.message, "hint": exc.hint}
    # 诊断增强：把错误分类与脱敏后的上下文透传给前端，便于精准提示与线上排查
    category = getattr(exc, "category", None) or "unknown"
    content["category"] = category
    ctx = getattr(exc, "context", None) or {}
    if ctx:
        content["diag"] = {
            "host": ctx.get("host"),
            "is_china": ctx.get("is_china"),
            "is_hardened": ctx.get("is_hardened"),
            "cookie_source": ctx.get("cookie_source"),
            "proxy_used": ctx.get("proxy_used"),
            "is_cloud": ctx.get("is_cloud"),
        }
    return JSONResponse(status_code=status, content=content)

@app.exception_handler(downloader.ResolveRestricted)
async def handle_restricted(_: Request, exc: "downloader.ResolveRestricted") -> JSONResponse:
    # 受限内容属于"确认无解"，用 422 与网络/解析异常区分开
    content = {"error": exc.message, "hint": exc.hint}
    content["category"] = getattr(exc, "category", None) or "restricted"
    return JSONResponse(status_code=422, content=content)

# --------------------------------------------------------------------------- #
# 文件系统辅助（桌面版便捷入口）
# --------------------------------------------------------------------------- #

class OpenPathRequest(BaseModel):
    """打开本地目录/文件。仅允许白名单路径（下载目录及其子项）。"""
    path: str = Field(default="", max_length=4096)

# --------------------------------------------------------------------------- #
# 自动解说（增值功能）：下载完 → 一键生成解说成片。壳，逻辑全在 commentary-pipeline
# --------------------------------------------------------------------------- #

class CommentaryRequest(BaseModel):
    task_id: str = Field(default="", max_length=64)
    file_id: str = Field(default="", max_length=2048)
    # 片名锚点（可选）：显式传入时优先作为成片文件名前缀 `<title>-解说完成<时间>.mp4`。
    # 本地拖拽上传（stash）场景由前端带上用户原始文件名，避免片名退化成 sha 哈希。
    title: str = Field(default="", max_length=256)
    vertical: bool = False
    voice: str = Field(default="", max_length=64)
    trim_start: float = Field(default=0.0, ge=0.0)
    trim_end: float = Field(default=0.0, ge=0.0)
    # 剪辑选项（与 commentary-pipeline 的 commentary_options 模型一致）
    commentary_type: str = Field(default="deep_hl",
                                 description="解说类型: deep_hl=高光处叠加深度解说; normal_hl=高光部分普通解说; "
                                             "full_normal=全片普通解说; full_deep=全片深入解说")
    highlight_source: str = Field(default="ai", description="高光来源: ai=AI自动挑; manual=人工在审核面板挑")
    intro_highlight: bool = Field(default=False, description="片头插入最精彩片段当钩子")
    skip_intro_outro: bool = Field(default=False, description="去片头片尾(自动检测边界后剪掉)")
    no_narrate_intro_outro: bool = Field(default=True,
                                         description="保留片头片尾但不解说(默认开启；与 skip_intro_outro 互斥，后者优先)")
    retain_pct: float | None = Field(default=None, description="保留全片时长百分比(10~100, 不填=不裁剪)")
    intro_sec: float | None = Field(default=None, description="手动片头秒数(优先于自动检测，直接跳过该片头时长)")
    outro_sec: float | None = Field(default=None, description="手动片尾秒数(优先于自动检测，直接剪掉该时长的片尾)")
    drama_start_sec: float | None = Field(default=None, description="正剧开始时间秒数(绝对时间，优先级高于intro_sec)")
    drama_end_sec: float | None = Field(default=None, description="片尾开始时间秒数(绝对时间，优先级高于outro_sec)")
    web: bool = Field(default=False, description="联网搜索资料辅助发挥")
    one_click: bool = Field(default=False, description="一键生成: 全片深入解说+AI联网+片头插精彩片段")
    style: str = Field(default="none", description="解说口吻风格: none=默认; funny=搞笑; serious=严肃; domineering=霸道; angry=愤青; suspense=悬疑; healing=治愈; sarcastic=毒舌; short_drama=短剧解说(网感快节奏); movie=影视深度解说")
    style_intensity: int = Field(default=65, ge=0, le=100, description="风格强度 0~100（默认 65）：≤35 口吻克制只轻点缀；≥80 拉满风格表现力。仅在选了具体风格时生效")
    vision: bool = Field(default=False, description="视觉理解：抽帧调多模态模型补全无声/纯画面段落（需配置 VDL_VISION_*）")
    tts_provider: str = Field(default="", max_length=32, description="TTS 服务商: 空=默认 edge; indextts2=本地 IndexTTS2 语音克隆(需先起推理服务); minimax/siliconflow=云端")
    correct_transcript: str = Field(default="", max_length=32, description="转写稿 ASR 校正: '0'=关闭(省 token); 空=默认开启(LLM 修正同音错字/专有名词)")
    mode: str | None = Field(default=None,
                             description="(旧版兼容) 三选一解说模式，会被上面的新选项覆盖")
    export_jianying: str = Field(default="", max_length=2048,
                                 description="导出剪映草稿目录: 非空时成片生成后再导出剪映可编辑草稿"
                                             "(视频轨+配音轨+字幕轨), 可在剪映里二次精修; 空=不导出")
    # ===== 吸收 NarratoAI 优点的增强项 =====
    bgm: str = Field(default="off", description="BGM 自动配乐: off=关闭; soft=柔和氛围; light=轻快; epic=恢弘; user=本地文件(配 bgm_file)")
    bgm_file: str = Field(default="", max_length=2048, description="user 模式下的本地 BGM 文件路径(mp3/wav)")
    bgm_volume: float = Field(default=0.18, ge=0.02, le=0.6, description="BGM 音量 0.02~0.6")
    subtitle_size: float = Field(default=1.0, ge=0.6, le=1.6, description="字幕字号倍率 0.6~1.6")
    subtitle_color: str = Field(default="FFFFFF", description="字幕颜色 hex(如 FFFFFF 白 / FFD700 金 / FF4500 橙)")
    subtitle_border: float = Field(default=1.0, ge=0.3, le=3.0, description="字幕描边粗细倍率 0.3~3.0")
    subtitle_border_color: str = Field(default="000000", description="字幕描边颜色 hex(如 000000 黑 / FFFFFF 白 / 1E90FF 蓝)")
    subtitle_pos: str = Field(default="bottom", description="字幕位置: bottom=底部; center=画面中部; y:<比率>=文字中心距顶部比例(前端拖拽自定义, 如 y:0.42)")
    max_chars: int = Field(default=0, ge=0, description="解说稿总长度上限(字)，0=不限制")
    feather_opt: str = Field(default="", max_length=512, description=(
        "原字幕羽化选项 JSON（前端「原字幕羽化」面板）："
        "{mode:'fade'|'stretch'|'blur', band_y_ratio:0~1, band_h_ratio:0~1, strength:0.4~2.5}。"
        "空=不干预，管线走自身自动探测。传了 band 比例则跳过探测、按预览调好的位置渲染"
        "（保证「预览看到什么，成片就烧什么」）。"))

class ScriptUpdateRequest(BaseModel):
    """PUT /api/commentary/script/{job_id}：提交人工修改后的解说词与全局配音。"""
    title: str = Field(default="", max_length=256)
    voice: str = Field(default="", max_length=64)
    segments: list = Field(default_factory=list)  # [{start, end, narration, note, voice?}, ...]

# 独立「解说成片」标签页：列出所有已生成成片，并支持按 id 直接下载/播放。
# id 为成片绝对路径的 urlsafe base64，便于无状态回查且防止路径穿越。
#
# 成片目录里的「非成品」sidecar：渲染时 edit_ffmpeg 会在同目录额外落一份
# `<成片名>.nomusic.mp4`（无音乐原片，供渲染后单独换/加背景音乐）。它跟成品
# 同名只差后缀，若一起列进「解说历史」会出现两条同名记录、数量翻倍。
# 列表接口按 stem 后缀统一剔除（未来若新增其它 sidecar，只在此追加即可）。
_COMMENTARY_SIDECAR_STEMS = (".nomusic",)

def _commentary_roots() -> list[Path]:
    """扫描两个可能的输出目录：HTTP 模式落地到 COMMENTARY_LOCAL_OUTPUT，
    本地模式落地到 COMMENTARY_DIR/output。"""
    roots = []
    if COMMENTARY_LOCAL_OUTPUT and COMMENTARY_LOCAL_OUTPUT.exists() and COMMENTARY_LOCAL_OUTPUT.is_dir():
        roots.append(COMMENTARY_LOCAL_OUTPUT)
    if COMMENTARY_DIR:
        d = _commentary_root("output")
        if d.exists() and d.is_dir():
            roots.append(d)
    return roots

def _decode_commentary_id(cid: str) -> Path:
    try:
        raw = base64.urlsafe_b64decode(cid.encode("ascii")).decode("utf-8")
    except Exception:
        raise HTTPException(status_code=400, detail="非法的成片标识")
    p = Path(raw).resolve()
    allowed = {r.resolve() for r in _commentary_roots()}
    if not any(p == root or str(p).startswith(str(root) + os.sep) for root in allowed):
        raise HTTPException(status_code=403, detail="越权访问被拒绝")
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="成片不存在或已被清理")
    return p

class CommentaryRenameReq(BaseModel):
    name: str

# ---- 脚本审核专用路由 ----

def _resolve_source(payload: CommentaryRequest, request=None) -> str:
    """解析视频来源（task_id 或 file_id / stash:id），返回绝对路径。

    request 传入时，task_id 分支会校验任务归属（安全审计 P0-2）：否则登录用户可
    借用他人 task_id 生成解说，间接读取别人的视频。
    """
    if payload.file_id:
        # stash: 前缀 → 本机接收站缓存（去重命中时 0 字节传输）
        if payload.file_id.startswith("stash:"):
            sha = payload.file_id[len("stash:"):]
            m = _stash_lookup(sha)
            if not m:
                raise HTTPException(
                    status_code=404,
                    detail="缓存的视频已过期或不存在，请重新上传（stash cache miss）",
                )
            return m["path"]
        p = library_mod._resolve_safe(DOWNLOAD_DIR, payload.file_id)
        if not p:
            raise HTTPException(status_code=404, detail="媒体库文件不存在")
        suffix = p.suffix.lower()
        if suffix == library_mod.ENCRYPTED_EXT:
            raise HTTPException(status_code=409, detail="加密文件不支持生成解说，请先在保险箱解锁")
        if suffix not in library_mod.VIDEO_EXTS:
            raise HTTPException(status_code=409, detail="该文件不是视频，无法生成解说成片")
        return str(p)
    elif payload.task_id:
        task = _require_owned_task(payload.task_id, request) if request is not None \
            else _require_task(payload.task_id)
        if task.status != "completed" or not task.filepath or not task.filepath.exists():
            raise HTTPException(status_code=409, detail="下载任务尚未完成，无法生成解说")
        return str(task.filepath)
    else:
        raise HTTPException(status_code=400, detail="请提供 task_id 或 file_id")

def _probe_video_title(src_path) -> str:
    """用 ffprobe 读取视频 `format.tags.title`（mp4 自带的标题元数据）。

    用途：用户从本地拖拽/选择视频上传时，源文件名常常是「upload.mp4」「VID_xxx.mp4」
    这类无语义名字，导致成片名变成「upload-解说成片...」。mp4 自带的 title 标签
    （yt-dlp/官方下载工具一般会写入）才是真正的剧名/视频标题，优先用它做片名前缀。

    失败/无标签/找不到 ffprobe → 返回空串（调用方继续 fallback 到文件名）。
    """
    try:
        import json as _json
        src_str = str(src_path)
        if not src_str or not Path(src_str).exists():
            return ""
        # PATH 优先解析；找不到时合并 _CommentaryRuntime 探测到的 ffmpeg_dir
        path = os.environ.get("PATH", "") or ""
        ffprobe_bin = shutil.which("ffprobe", path=path) or ""
        if not ffprobe_bin:
            try:
                ffmpeg_dir = getattr(COMMENTARY_RT, "ffmpeg_dir", "") or ""
                if ffmpeg_dir:
                    ffprobe_bin = shutil.which("ffprobe", path=ffmpeg_dir + os.pathsep + path) or ""
            except Exception:
                pass
        if not ffprobe_bin:
            return ""
        r = subprocess.run(
            [ffprobe_bin, "-v", "quiet", "-print_format", "json", "-show_format", src_str],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0 or not r.stdout:
            return ""
        data = _json.loads(r.stdout)
        tags = ((data.get("format") or {}).get("tags") or {})
        return (tags.get("title") or "").strip()
    except Exception:
        return ""


def _probe_video_duration(src_path) -> float:
    """用 ffprobe 读取视频总时长（秒）。失败/找不到 ffprobe → 返回 0.0（调用方按需 fail-open）。"""
    try:
        import json as _json
        src_str = str(src_path)
        if not src_str or not Path(src_str).exists():
            return 0.0
        path = os.environ.get("PATH", "") or ""
        ffprobe_bin = shutil.which("ffprobe", path=path) or ""
        if not ffprobe_bin:
            try:
                ffmpeg_dir = getattr(COMMENTARY_RT, "ffmpeg_dir", "") or ""
                if ffmpeg_dir:
                    ffprobe_bin = shutil.which("ffprobe", path=ffmpeg_dir + os.pathsep + path) or ""
            except Exception:
                pass
        if not ffprobe_bin:
            return 0.0
        r = subprocess.run(
            [ffprobe_bin, "-v", "quiet", "-print_format", "json",
             "-show_entries", "format=duration", src_str],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0 or not r.stdout:
            return 0.0
        data = _json.loads(r.stdout)
        dur = float((data.get("format") or {}).get("duration") or 0) or 0.0
        return dur
    except Exception:
        return 0.0


def _suggest_intro_outro(src_path: str) -> dict:
    """轻量探测片头/片尾边界（前端「起点/终点」预填建议值用，2026-09-20）。

    与管线 `_detect_intro_outro` 的音频/画面信号同源（silencedetect/blackdetect/
    freezedetect，判据与常量一致：段起点≤3s 取结束、段结束贴窗口尾取开始、
    正片区<40% 整体放弃），但**不跑** VAD 人声与视觉 LLM —— 这两项成本高，
    留给渲染时的管线做更精的二次检测；这里只求秒级出结果供输入框预填。
    语义约定：探测成功 → 前端填的值就是渲染用的值（所见即所用，手动优先级最高）；
    探测不到（有声有色的片头，主题曲+画面无静音/黑场）→ ok=False，前端保持
    「自动检测」占位，渲染时管线走全量检测（含视觉集数卡识别）。
    """
    try:
        src_str = str(src_path)
        dur = _probe_video_duration(src_str)
        if dur <= 20:
            return {"ok": False, "lo": None, "hi": None, "dur": dur}
        path = os.environ.get("PATH", "") or ""
        ffmpeg_bin = shutil.which("ffmpeg", path=path) or ""
        if not ffmpeg_bin:
            try:
                ffmpeg_dir = getattr(COMMENTARY_RT, "ffmpeg_dir", "") or ""
                if ffmpeg_dir:
                    ffmpeg_bin = shutil.which("ffmpeg", path=ffmpeg_dir + os.pathsep + path) or ""
            except Exception:
                pass
        if not ffmpeg_bin:
            return {"ok": False, "lo": None, "hi": None, "dur": dur}

        def _probe_edge(start: float, win: float, is_head: bool):
            """单窗口探测：返回边界候选秒或 None（与管线 _detect_edge 同判据）。"""
            try:
                proc = subprocess.run(
                    [ffmpeg_bin, "-hide_banner", "-ss", f"{start:.3f}", "-t", f"{win:.3f}",
                     "-i", src_str,
                     "-af", "silencedetect=n=-40dB:d=0.5",
                     "-vf", "blackdetect=d=0.5:pix_th=0.10,freezedetect=d=0.5",
                     "-f", "null", "-"],
                    stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, timeout=120)
                out = proc.stderr.decode("utf-8", "ignore")
            except Exception:
                return None
            # -ss 在 -i 前：滤镜报的时戳是相对窗口起点的，+start 还原成绝对秒（同管线 _parse_detect）
            segs, pend = [], {"silence": None, "black": None, "freeze": None}
            for line in out.splitlines():
                for kind in ("silence", "black", "freeze"):
                    ms = re.search(rf"{kind}_start:\s*([\d.]+)", line)
                    me = re.search(rf"{kind}_end:\s*([\d.]+)", line)
                    if ms:
                        pend[kind] = float(ms.group(1)) + start
                    if me and pend[kind] is not None:
                        segs.append((pend[kind], float(me.group(1)) + start))
                        pend[kind] = None
            win_end = start + win
            if is_head:
                cands = [e for s, e in segs if s <= 3.0 and e > s]
                return max(cands) if cands else None
            cands = [s for s, e in segs if e >= win_end - 3.0 and e > s]
            return min(cands) if cands else None

        head_win = min(dur * 0.5, 180.0)
        tail_win = min(dur * 0.5, 180.0)
        lo = _probe_edge(0.0, head_win, is_head=True)
        hi = _probe_edge(max(0.0, dur - tail_win), tail_win, is_head=False)
        if lo is not None and lo <= 1.0:
            lo = None
        if hi is not None and not (0.0 < hi < dur - 1.0):
            hi = None
        # 保护与管线一致：正片区过短说明探测不可信 → 整体放弃（交给渲染时全量检测）
        if lo is not None and hi is not None and hi - lo < dur * 0.4:
            return {"ok": False, "lo": None, "hi": None, "dur": dur}
        return {"ok": bool(lo is not None or hi is not None),
                "lo": round(lo, 2) if lo is not None else None,
                "hi": round(hi, 2) if hi is not None else None,
                "dur": round(dur, 2)}
    except Exception:
        return {"ok": False, "lo": None, "hi": None, "dur": 0.0}

def _commentary_title(payload: "CommentaryRequest", src_path: str, request=None) -> str:
    """为解说任务推导「片名锚点」（最终会进成片文件名 `<片名>-解说完成<时间>.mp4`）。

    优先级（2026-09-15 重排，修复成片名变 16 位乱码哈希）：
      ① 调用方显式 title
      ② 本地拖拽上传：stash 里登记的**用户原始文件名**（最贴近用户认知的片名）
      ③ 下载任务标题（URL 解析结果，通常就是站内标题）
      ④ 源文件名 stem（**必须过 _meaningful_stem**：upload/纯数字/哈希不许上片名）
      ⑤ 视频自带 format.tags.title（ffprobe）
      ⑥ v<6hex> 短码兜底
    历史坑：旧实现最后一行直接 `Path(src_path).stem` 就返回，没有任何语义校验。
    本地拖拽上传走 cache-by-hash 接收站，源文件是按 sha256 前 16 位命名的
    `60e0adcf0f011d68.mp4` —— 于是这个哈希被当成片名写进了成片文件名，用户看到
    「60e0adcf0f011d68-解说完成202609150032.mp4」，误以为是乱码。
    """
    # ① 显式 title（前端/接口透传的片名）
    explicit = (getattr(payload, "title", "") or "").strip()
    if explicit:
        return explicit

    # ② stash（本地拖拽上传）：用用户原始文件名；进程重启后索引 name 可能为空
    _fid = (getattr(payload, "file_id", "") or "").strip()
    if _fid.startswith("stash:"):
        try:
            _m = _stash_lookup(_fid[len("stash:"):]) or {}
        except Exception:
            _m = {}
        _orig = (_m.get("name") or "").strip()
        _stem = Path(_orig).stem if _orig else ""
        if _meaningful_stem(_stem):
            return _stem
        _tag = _probe_video_title(_m.get("path") or src_path)
        if _tag:
            return _tag

    # ③ 下载任务标题（含步骤详情反解 + URL 轻量重解析兜底）
    if getattr(payload, "task_id", ""):
        try:
            # 安全审计 P0-2：带上 request 时校验归属，防止借他人 task_id 反查片名。
            t = _require_owned_task(payload.task_id, request) if request is not None \
                else _require_task(payload.task_id)
            tt = (getattr(t, "title", "") or "").strip()
            if tt:
                return tt
            # 兜底 1：任务步骤里可能已记录「已获取标题《...》」，从中反解。
            # 某些旧任务/解析路径在创建时 title 为空，但下载过程中写入了步骤详情。
            for step in getattr(t, "steps", []) or []:
                detail = (step.get("detail") or "").strip()
                if detail.startswith("已获取标题《") and detail.endswith("》"):
                    tt = detail[len("已获取标题《"):-1].strip()
                    if tt:
                        return tt
            # 兜底 2：从 URL 同步轻量解析一次标题（避免 fallback 到无意义 hash）。
            url = getattr(t, "url", "") or ""
            if url:
                try:
                    info = downloader.probe(url, "", "")
                    tt = (downloader.summarize(info).get("title") or "").strip()
                    if tt:
                        # 顺手回填任务，避免下次再解析
                        store.update(t.id, title=tt)
                        return tt
                except Exception:
                    pass
        except Exception:
            pass

    # ④ 源文件名（有语义才用）
    _stem = Path(src_path).stem if src_path else ""
    if _meaningful_stem(_stem):
        return _stem
    # ⑤ 视频自带标题标签
    _tag = _probe_video_title(src_path)
    if _tag and _meaningful_stem(_tag):
        return _tag.strip()
    # ⑥ 短码兜底：绝不把哈希/upload 直接当片名
    import secrets as _secrets
    return "v" + _secrets.token_hex(3)

def _meaningful_stem(s: str) -> bool:
    """判断上传文件名 stem 是否「有语义、值得做片名前缀」。

    用户 2026-08-25 明确：片名用「上传前的名字」。但源文件叫 upload.mp4、
    VID_xxx、纯数字或 job_id 哈希（ee6b7cf95736）这类无语义名不能直接上，
    需走 ffprobe/短码兜底。
    """
    s = (s or "").strip()
    if not s or s.lower() == "upload":
        return False
    if s.isdigit():
        return False
    if 12 <= len(s) <= 32 and all(c in "0123456789abcdefABCDEF" for c in s):
        return False  # 哈希形
    return True

def _safe_output_stem(title: str) -> str:
    """与 commentary-pipeline process._sanitize_filename 对齐的成片文件前缀清洗。

    process.py 用 --out-name 的清洗结果作为 final_output_name 的前缀；app 端查找成片时
    必须用同一规则还原该前缀，否则按 job_id 查找会落空（标题里的中文/空格/特殊字符被清洗后
    与原始 title 不同）。
    """
    import re as _re
    s = (title or "").strip()
    s = _re.sub(r'[\\/:\*?"<>\|\x00-\x1f]', "", s)
    s = s.rstrip(" .")
    if not s:
        return ""
    return s[:100]

# ---- 配音试听 / 预览全部 ----

def _commentary_ffmpeg_bin() -> str:
    """返回解说管线可用的 ffmpeg 路径：优先用 CommentaryRuntime 探测到的捆绑 ffmpeg，
    否则回退到全局 FFMPEG_BIN（格式转换模块同款）。"""
    d = getattr(COMMENTARY_RT, "ffmpeg_dir", "") or ""
    if d:
        cand = os.path.join(d, "ffmpeg")
        if os.path.exists(cand):
            return cand
    return FFMPEG_BIN

def _build_loudness_filter(loudness: str | None, boost: str | None) -> str:
    """构造旁白响度后处理的 ffmpeg 音频滤镜串。

    - loudness="off"/空：不做响度标准化，仅用 boost 增益兜底
    - loudness 为数字（如 "-14"）：loudnorm 标准化到该 LUFS 目标
    - boost 额外线性增益（默认 1.0，夹取 0.5~2.0）
    - 末尾统一加 alimiter 限幅，防止拉响导致破音
    """
    ln = (loudness or "").strip().lower()
    do_loudnorm = ln not in ("", "off")
    if do_loudnorm:
        try:
            lv = float(ln)
        except ValueError:
            raise ValueError(f"loudness 参数无效: {loudness!r}（应为 -18~-10 或 off）")
        filt = f"loudnorm=I={lv:.1f}:TP=-1.0:LRA=11:linear=true,"
    else:
        filt = ""
    bv = 1.0
    if boost not in (None, ""):
        try:
            bv = float(boost)
        except (TypeError, ValueError):
            bv = 1.0
    bv = max(0.5, min(2.0, bv))
    return filt + f"volume={bv:.2f},alimiter=limit=0.98:level=disabled"

def _apply_narration_loudness(src_mp3: Path, loudness: str | None, boost: str | None) -> None:
    """对已有旁白做响度标准化 + 增益（试听即所得，与成片 edit_ffmpeg 一致）。

    ⚠️ 输出容器必须跟输入同后缀（2026-09-18 修）：克隆引擎的试听产物是 .wav，
    而旧写法恒写 .mp3 再把内容改名回 .wav —— 文件后缀与实际编码不符，
    接口又按后缀声明 media_type=audio/wav，浏览器解析会失败（试听直接不出声）。
    现在按后缀选编码：wav → pcm_s16le，其余 → ffmpeg 默认 mp3 编码。

    失败不致命：保留原 TTS 音频，仅打印告警，不让试听整体失败。
    """
    ff = _commentary_ffmpeg_bin()
    if not ff or not os.path.exists(ff):
        print(f"  [试听] 未找到 ffmpeg（{ff}），跳过响度处理，返回原始旁白")
        return
    try:
        filt = _build_loudness_filter(loudness, boost)
    except ValueError as e:
        print(f"  [试听] 响度参数错误，跳过后处理：{e}")
        return
    suffix = src_mp3.suffix.lower()
    tmp = src_mp3.with_name(src_mp3.stem + ".loud" + suffix)
    cmd = [ff, "-y", "-i", str(src_mp3), "-af", filt, "-ar", "44100", "-ac", "2"]
    if suffix == ".wav":
        cmd += ["-c:a", "pcm_s16le"]
    cmd.append(str(tmp))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as e:
        print(f"  [试听] ffmpeg 响度处理异常，跳过：{e}")
        if tmp.exists():
            tmp.unlink()
        return
    if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 100:
        src_mp3.unlink()
        tmp.rename(src_mp3)
    else:
        msg = (proc.stderr or proc.stdout or "").strip()[-300:]
        print(f"  [试听] ffmpeg 响度处理失败，保留原始旁白：{msg}")
        if tmp.exists():
            tmp.unlink()

def _qwen3tts_api_url() -> str:
    """本机 Qwen3-TTS 克隆服务地址（与管线 config.QWEN3TTS_API_URL 同一默认值）。"""
    return (os.environ.get("QWEN3TTS_API_URL") or "http://127.0.0.1:7871").rstrip("/")


def _run_voice_preview_clone(text: str, ref_audio: str, ref_text: str,
                            output: Path, timeout: int = 300) -> None:
    """克隆引擎试听：把文本 + 参考音频 + 文字稿发给本机 Qwen3-TTS 服务，落盘 wav。

    与渲染端行为对齐（scripts/edit_ffmpeg.py 的 qwen3tts 分支）：
      · 服务不可达 → 直接报错，**不静默回退 edge**（用户显式选了克隆，试听听到的必须是克隆声，
        否则「试听正常、成片不是自己的声音」这种误导比报错更糟）；
      · 参考音频缺失 / 文字稿为空 → 报错并提示先配「我的音色」。
    """
    import urllib.error
    import urllib.request

    ref_audio = (ref_audio or "").strip()
    ref_text = (ref_text or "").strip()
    if not ref_audio or not os.path.isfile(ref_audio):
        raise RuntimeError("还没配置音色样本：请先在「我的音色（本地克隆源）」里选一段录音")
    if not ref_text:
        raise RuntimeError("音色样本缺少文字稿：请填写那段录音里念的内容")

    api_url = _qwen3tts_api_url()
    boundary = "----vdlpreviewclone"
    try:
        with open(ref_audio, "rb") as rf:
            parts = [
                (
                    f"--{boundary}\r\n"
                    f"Content-Disposition: form-data; name=\"reference_audio\"; "
                    f"filename=\"{os.path.basename(ref_audio)}\"\r\n"
                    f"Content-Type: audio/wav\r\n\r\n"
                ).encode("utf-8"),
                rf.read(),
                b"\r\n",
            ]
        for k, v in (("text", text.strip()), ("prompt_text", ref_text)):
            parts.append(
                (
                    f"--{boundary}\r\n"
                    f"Content-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n"
                ).encode("utf-8")
            )
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        req = urllib.request.Request(
            f"{api_url}/tts",
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            audio = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "ignore")[:200]
        except Exception:
            pass
        raise RuntimeError(f"克隆服务返回 HTTP {exc.code}：{detail}") from exc
    except (TimeoutError, socket.timeout) as exc:
        # 超时 ≠ 连不上：本地 CPU 推理本来就慢（实测 macOS 13 无 MPS，
        # 一个 3 字短句都能跑过 5 分钟），把「慢」说成「连不上」会让人去查端口，
        # 完全跑偏。这里明确区分。
        raise RuntimeError(
            f"克隆合成超时（>{timeout}s）：本机语音克隆是纯 CPU 推理，速度较慢。"
            "可先改用较短的试听文本，或换「微软 edge-tts」兜底；渲染成片时需要更长时间。"
        ) from exc
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)):
            raise RuntimeError(
                f"克隆合成超时（>{timeout}s）：本机语音克隆是纯 CPU 推理，速度较慢。"
                "可先改用较短的试听文本，或换「微软 edge-tts」兜底。"
            ) from exc
        raise RuntimeError(
            f"连不上本机克隆服务（{api_url}）：{type(exc).__name__}: {exc}。"
            "可稍等 25 秒让服务启动完成，或在引擎下拉里切一次重新拉起。"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"连不上本机克隆服务（{api_url}）：{type(exc).__name__}: {exc}。"
            "可稍等 25 秒让服务启动完成，或在引擎下拉里切一次重新拉起。"
        ) from exc

    if not audio or len(audio) < 100:
        raise RuntimeError("克隆服务返回空音频")
    output.write_bytes(audio)


def _run_voice_preview(text: str, voice: str, output_mp3: Path, timeout: int = 60,
                       loudness: str | None = None, boost: str | None = None,
                       provider: str = "") -> None:
    """用 edge-tts 把一段文本转成指定音色的 mp3。

    bundled 模式：edge_tts 已随包冻结，直接 in-process 调用，避开「subprocess 跑
    .py 脚本 vs PyInstaller frozen exe」的兼容性问题。
    dev 模式：edge_tts 装在 commentary-pipeline .venv 里，subprocess 到 COMMENTARY_RT.python。

    loudness/boost（试听「配音与音量」设置用）：非 None 时对生成好的旁白再做
    ffmpeg 响度标准化 + 增益，使试听与成片响度一致。

    provider（2026-09-18 新增）：传本机克隆引擎时改走 Qwen3-TTS 克隆试听，
    见 _run_voice_preview_clone；空/edge 时保持原有 edge-tts 行为。
    """
    is_clone = str(provider or "").strip().lower() == "qwen3tts"
    if is_clone:
        # 克隆引擎：走本机 7871 服务 + 「我的音色」样本。
        # 🔴 这段刻意「不 return」——要让下面那段响度/增益后处理照样作用在克隆 wav 上，
        #    否则「试听当前设置」在克隆引擎下会听到未处理的干声，与成片响度对不上。
        sample = get_voice_sample()
        _run_voice_preview_clone(text, sample["audio_path"], sample["ref_text"],
                                 output_mp3, timeout=max(timeout, 900))
    else:
        if not COMMENTARY_RT.ready():
            raise RuntimeError("解说环境未就绪：" + "；".join(COMMENTARY_RT.issues))

        # bundled 模式：桌面打包态，edge_tts 已冻结进 exe，直接 in-process 调用
        if plat.is_desktop():
            _run_voice_preview_inprocess(text, voice, output_mp3, timeout=timeout)
        else:
            # dev/外部：subprocess 走 commentary-pipeline 的 venv
            if not COMMENTARY_DIR or not (COMMENTARY_DIR / "scripts" / "voice_preview.py").exists():
                raise RuntimeError("voice_preview.py 不存在（请在 commentary-pipeline/scripts/ 下创建）")
            script = COMMENTARY_DIR / "scripts" / "voice_preview.py"
            cmd = [COMMENTARY_RT.python, str(script), text, voice, str(output_mp3)]
            try:
                proc = subprocess.run(cmd, cwd=str(COMMENTARY_DIR), capture_output=True,
                                      text=True, timeout=timeout, env=COMMENTARY_RT.env())
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"edge-tts 生成超时（>{timeout}s）") from exc
            except Exception as exc:
                raise RuntimeError(f"edge-tts 调用失败：{exc}") from exc
            if proc.returncode != 0:
                msg = (proc.stderr or proc.stdout or "无输出").strip()[:400]
                raise RuntimeError(f"edge-tts 退出码 {proc.returncode}: {msg}")
            if not output_mp3.exists() or output_mp3.stat().st_size < 100:
                raise RuntimeError("edge-tts 未产出有效音频文件")

    # 音量后处理（试听「配音与音量」设置时才带 loudness/boost）
    if loudness is not None or boost is not None:
        _apply_narration_loudness(output_mp3, loudness, boost)

def _run_voice_preview_inprocess(text: str, voice: str, output_mp3: Path, timeout: int = 60) -> None:
    """bundled 模式：in-process 调 edge_tts 合成 mp3，省去 PyInstaller frozen exe 跑 .py 脚本的兼容坑。"""
    import asyncio
    import edge_tts

    FALLBACK_VOICES = [
        "zh-CN-YunjianNeural",
        "zh-CN-YunyangNeural",
        "zh-CN-YunxiaNeural",
        "zh-CN-XiaoxiaoNeural",
    ]

    # 用容器持模块引用：自愈升级后要换成热加载的新模块（局部名重新赋值会遮蔽闭包）
    _mod = {"m": edge_tts}
    _last_exc: list[BaseException] = []
    tried: list[str] = []

    async def _try(v: str, timeout_s: float) -> bool:
        try:
            await asyncio.wait_for(_mod["m"].Communicate(text, v).save(str(output_mp3)), timeout=timeout_s)
            return output_mp3.exists() and output_mp3.stat().st_size > 100
        except Exception as exc:  # noqa: BLE001 - 逐音色试，末次错误留作分类
            _last_exc.append(exc)
            return False

    async def _one_round(first: str) -> bool:
        # 首选音色试 2 次（防网络瞬抖），失败再走回退链
        for _ in range(2):
            if await _try(first, 20):
                return True
        for fb in FALLBACK_VOICES:
            if fb == first:
                continue
            if fb not in tried:
                tried.append(fb)
            if await _try(fb, 20):
                return True
        return False

    async def _main():
        tried.append(voice)
        if await _one_round(voice):
            return
        # 🔴 2026-10-20：全部音色都失败，最可能是 edge-tts 客户端版本被微软作废（403）。
        #    这不是功能坏了 —— 自动升级并热加载后重跑一轮，让用户点「试听」不至于直接被拒。
        exc = _last_exc[-1] if _last_exc else None
        if tts_guard.repair_after_auth_error(exc):
            import importlib
            importlib.invalidate_caches()
            _mod["m"] = importlib.import_module("edge_tts")
            if await _one_round(voice):
                return
        raise RuntimeError(f"edge-tts 生成失败: 已尝试 {', '.join(tried)}，均无法合成该文本")

    try:
        asyncio.run(_main())
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"edge-tts 调用失败：{exc}") from exc
    if not output_mp3.exists() or output_mp3.stat().st_size < 100:
        raise RuntimeError("edge-tts 未产出有效音频文件")

# ---- 本地媒体库（桌面版功能）：浏览 / 播放 / 删除已下载的媒体文件 ----

# ---- 时效自动清理：按保留期/容量上限清理下载目录（预览 → 执行，媒体走回收站） ----

class RetentionConfigRequest(BaseModel):
    auto_enabled: bool | None = None
    interval_hours: float | None = Field(default=None, ge=0.25, le=168)
    temp_enabled: bool | None = None
    temp_days: float | None = Field(default=None, ge=0, le=3650)
    frames_enabled: bool | None = None
    frames_days: float | None = Field(default=None, ge=0, le=3650)
    thumbs_enabled: bool | None = None
    thumbs_days: float | None = Field(default=None, ge=0, le=3650)
    media_enabled: bool | None = None
    media_days: float | None = Field(default=None, ge=1, le=3650)
    quota_enabled: bool | None = None
    quota_gb: float | None = Field(default=None, ge=1, le=100000)
    media_use_trash: bool | None = None

class RetentionRunRequest(BaseModel):
    # 只清指定档位；留空=按当前配置全清。前端「预览后执行」会带上用户勾选的档位。
    categories: list[str] | None = None

def _require_retention() -> None:
    if not RETENTION_ENABLED:
        raise HTTPException(status_code=404, detail="自动清理仅桌面版可用")

# --------------------------------------------------------------------------- #
# 库内保险箱：选中文件就地 AES 加密 / 解密 + 解密播放
# --------------------------------------------------------------------------- #
class CryptoSetPassRequest(BaseModel):
    passwd: str = Field(min_length=1, max_length=512)
    confirm: str = Field(default="", max_length=512)
    old: str = Field(default="", max_length=512)

class CryptoUnlockRequest(BaseModel):
    passwd: str = Field(min_length=1, max_length=512)

class CryptoIdsRequest(BaseModel):
    lib_ids: list[str] = Field(default_factory=list)

def _require_crypto() -> None:
    if not CRYPTO_ENABLED:
        raise HTTPException(status_code=404, detail="保险箱功能未启用")

def _require_unlocked() -> None:
    if VAULT_KEY is None:
        raise HTTPException(status_code=423, detail="保险箱已锁定，请先解锁")

def _crypto_job_status(job_id: str) -> dict:
    with CRYPTO_LOCK:
        job = CRYPTO_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job

def _kind_of(path: Path) -> str:
    suf = path.suffix.lower()
    if suf in library_mod.AUDIO_EXTS:
        return "audio"
    if suf in library_mod.IMAGE_EXTS:
        return "image"
    return "video"

def _run_crypto_job(job_id: str, lib_ids: list[str], mode: str) -> None:
    """mode = 'encrypt' | 'decrypt'。加密把原件移回收站保底；解密还原原名并删除 .vdlenc。"""
    total = len(lib_ids)
    done = 0
    errors: list[str] = []
    with CRYPTO_LOCK:
        job = CRYPTO_JOBS.get(job_id)
        if job:
            job["status"] = "running"
            job["total"] = total

    for lid in lib_ids:
        with CRYPTO_LOCK:
            job = CRYPTO_JOBS.get(job_id)
            if not job or job.get("cancel"):
                with CRYPTO_LOCK:
                    if job:
                        job["status"] = "canceled"
                return
        src = library_mod._resolve_safe(DOWNLOAD_DIR, lid)
        if not src:
            errors.append(lid + ": 文件不存在")
            done += 1
            with CRYPTO_LOCK:
                if job:
                    job["done"] = done
            continue
        try:
            if mode == "encrypt":
                if src.suffix.lower() == library_mod.ENCRYPTED_EXT:
                    errors.append(src.name + ": 已是加密文件")
                else:
                    dst = src.parent / (src.name + library_mod.ENCRYPTED_EXT)
                    crypto_mod.encrypt_file(src, dst, VAULT_KEY, src.name, _kind_of(src))
                    # 原件移回收站保底，绝不静默硬删
                    retention_mod.move_to_trash(src)
            else:  # decrypt
                if src.suffix.lower() != library_mod.ENCRYPTED_EXT:
                    errors.append(src.name + ": 不是加密文件")
                else:
                    orig_name, _kind, _ext = crypto_mod.read_header(src)
                    out = src.parent / orig_name
                    if out.exists():
                        errors.append(src.name + ": 还原目标已存在，跳过")
                    else:
                        tmp_dec = src.parent / "tmp_dec"
                        crypto_mod.decrypt_file(src, tmp_dec, VAULT_KEY)
                        tmp_dec.replace(out)
                        src.unlink()  # 解密成功才删 .vdlenc
        except Exception as exc:  # 单文件失败不中断整批
            errors.append(src.name + ": " + type(exc).__name__ + " " + str(exc)[:120])
        done += 1
        with CRYPTO_LOCK:
            if job:
                job["done"] = done
                job["errors"] = errors

    with CRYPTO_LOCK:
        job = CRYPTO_JOBS.get(job_id)
        if job and job.get("status") != "canceled":
            job["status"] = "completed"
            job["errors"] = errors

def _prune_crypto_jobs() -> None:
    if len(CRYPTO_JOBS) <= 200:
        return
    with CRYPTO_LOCK:
        done = [jid for jid, j in CRYPTO_JOBS.items()
                if j.get("status") in ("completed", "failed", "canceled")]
        for jid in (done[:-50] if len(done) > 50 else []):
            CRYPTO_JOBS.pop(jid, None)

# ---- 桌面版种子下载（libtorrent 集成）：magnet/.torrent → 本地媒体库 ----
class TorrentAddRequest(BaseModel):
    uri: str = Field(min_length=1, max_length=8192)
    name: str = Field(default="", max_length=512)
    paused: bool = False
    save_path: str = Field(default="", max_length=4096)  # 相对 DOWNLOAD_DIR 的子目录；留空=根
    file_priorities: dict[int, int] = Field(default_factory=dict)  # {文件下标: 优先级(0=跳过)}

class TorrentRemoveRequest(BaseModel):
    delete_files: bool = False

class TorrentFilesRequest(BaseModel):
    priorities: dict[int, int] = Field(default_factory=dict)  # {文件下标: 优先级(0=跳过)}

def _require_torrent() -> None:
    if not (TORRENT_ENABLED and torrent_mod.available() and torrent_manager is not None):
        raise HTTPException(status_code=404, detail="种子下载功能未启用（需桌面版并安装 libtorrent 或 aria2）")

# ---- 字幕处理：在线字幕提取 / 内嵌字幕抽取 / 硬字幕烧录 / 可选 LLM 翻译 ----

class SubListRequest(BaseModel):
    lib_id: str = Field(min_length=1)
    cookie: str = Field(default="", max_length=8192)

class SubExtractRequest(BaseModel):
    lib_id: str = Field(min_length=1)
    lang: str = Field(min_length=1, max_length=16)
    cookie: str = Field(default="", max_length=8192)

class SubBurnRequest(BaseModel):
    lib_id: str = Field(min_length=1)
    sub_rel: str = Field(min_length=1, max_length=256)  # 相对视频目录的字幕文件名

class SubTranslateRequest(BaseModel):
    lib_id: str = Field(min_length=1)
    sub_rel: str = Field(min_length=1, max_length=256)
    api_key: str = Field(default="", max_length=512)
    base_url: str = Field(default="", max_length=512)
    model: str = Field(default="", max_length=128)
    target: str = Field(default="简体中文", max_length=32)

def _resolve_lib_video(lib_id: str) -> Path:
    p = library_mod._resolve_safe(DOWNLOAD_DIR, lib_id)
    if not p or p.suffix.lower() not in library_mod.VIDEO_EXTS:
        raise HTTPException(status_code=404, detail="视频文件不存在")
    return p

# ---- LLM 服务商选择器 API（统一配置，前端面板持久化）----

class LLMConfigRequest(BaseModel):
    # 凭据四件套默认 None/空 = 「本次不修改」。不能用 provider 的 "openai" 作默认值：
    # 前端简化后不再提交凭据（由管理员统一配置），若默认值仍是 "openai"，
    # 每次保存都会把管理员配好的 deepseek 打回 openai（实测过的破坏路径）。
    provider: str | None = Field(default=None, max_length=32)
    api_key: str = Field(default="", max_length=256)
    base_url: str = Field(default="", max_length=512)
    model: str = Field(default="", max_length=128)
    reasoning_effort: str = Field(default="low", max_length=16)
    offpeak_only: bool = Field(default=False)
    local_priority: bool = Field(default=False)
    local_model: str = Field(default="", max_length=128)
    # 本机 AI 引擎（MLX）字段。默认 None 表示「本次不修改」——旧版前端或
    # 第三方调用不带这些字段时，不能把用户已配置好的本机引擎抹掉。
    engine: str | None = Field(default=None, max_length=16)
    mlx_model_path: str | None = Field(default=None, max_length=1024)
    mlx_python: str | None = Field(default=None, max_length=1024)
    mlx_max_tokens: int | None = Field(default=None, ge=-1, le=131072)

class VisionConfigRequest(BaseModel):
    # 与 LLM 凭据同原则（2026-09-15）：云视觉凭据由超级管理员通过受管配置统一下发，
    # 用户界面不再暴露任何 Key 输入框。四个字段一律「空值 = 本次不修改」，
    # 默认必须是 None 而不是 "auto"/"" —— 否则前端不提交这些字段时一次保存
    # 就会把管理员配置打回 auto（历史事故路径同 LLMConfigRequest.provider）。
    provider: str | None = Field(default=None, max_length=32)
    api_key: str | None = Field(default=None, max_length=256)
    base_url: str | None = Field(default=None, max_length=512)
    model: str | None = Field(default=None, max_length=128)

class CommentaryConfigRequest(BaseModel):
    """解说(配音/音量)手动可调设置。"""
    # 数值型 LUFS（-18~-10）或字符串 "off"。不能用 Any —— 在 PEP 563
    # （from __future__ import annotations）下，Pydantic v2 的 TypeAdapter
    # 无法为 Any 构建完全定义的 core schema，保存端点会抛
    # "is not fully defined; you should define" 错误。
    narration_loudness: int | float | str = Field(default=-14)
    original_duck: float = Field(default=0.10)       # 0.05~0.30
    narration_boost: float = Field(default=1.0)      # 1.0~1.6

# ---- 格式 / 片段增强：对已下载媒体做本地 ffmpeg 加工（转音频 / GIF / 裁剪 / 压缩 / 放大）----
# 与字幕处理同源（基于 lib_id）；产物落源目录并写侧车 → 媒体库自动可见。

class ProcessRequest(BaseModel):
    # 单个文件（向后兼容）
    lib_id: str = Field(default="", max_length=2048)
    # 批量文件
    lib_ids: list[str] = Field(default_factory=list)
    op: str = Field(min_length=1, max_length=16)
    params: dict = Field(default_factory=dict)

def _run_ai_dewatermark(job_id: str, src: str, params: dict) -> None:
    """AI 去水印：调 watermark-removal worker（HTTP 或本地 subprocess），轮询完成。"""
    import json, time as _time
    import requests as _requests

    x = int(params.get("x", 0) or 0)
    y = int(params.get("y", 0) or 0)
    w = int(params.get("w", 100) or 100)
    h = int(params.get("h", 50) or 50)
    band = int(params.get("band", 5) or 5)
    mode = str(params.get("mode", "auto") or "auto")

    try:
        if AI_DEWATERMARK_MODE == "http":
            resp = _requests.post(
                f"{AI_DEWATERMARK_ENDPOINT}/render",
                json={"video": src, "x": x, "y": y, "w": w, "h": h, "band": band, "mode": mode},
                headers={"X-Worker-Token": AI_DEWATERMARK_TOKEN} if AI_DEWATERMARK_TOKEN else {},
                timeout=30,
            )
            resp.raise_for_status()
            worker_job = resp.json()["job_id"]
        else:
            # local 模式：subprocess 调 process.py
            proc_dir = AI_DEWATERMARK_DIR or (Path(__file__).resolve().parent.parent / "watermark-removal")
            proc = proc_dir / "process.py"
            if not proc.exists():
                raise RuntimeError(f"AI 去水印管线未找到：{proc}")
            import subprocess as _sp
            result = _sp.run(
                [AI_DEWATERMARK_PYTHON, str(proc), src,
                 "--x", str(x), "--y", str(y), "--w", str(w), "--h", str(h),
                 "--band", str(band), "--mode", mode],
                capture_output=True, text=True, timeout=AI_DEWATERMARK_TIMEOUT,
                env=dict(os.environ, E2FGVI_BASE=str(proc_dir / "E2FGVI")),
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "AI 去水印失败")
            # local 模式同步完成，直接走收尾
            out = Path(src).parent / f"{Path(src).stem}_AI去水印.mp4"
            if not out.exists():
                raise RuntimeError("AI 去水印未生成输出文件")
            meta = library_mod._load_sidecar(Path(src))
            fftools._write_sidecar(out, meta, "AI去水印")
            new_id = library_mod.encode_id(out.resolve().relative_to(DOWNLOAD_DIR.resolve()).as_posix())
            with process_queue.lock:
                process_queue.jobs[job_id].update(status="completed", out_path=str(out), lib_id=new_id, name=out.name)
            logger.info("ai_dewatermark %s done -> %s", job_id, out.name)
            return

        # HTTP 模式：轮询 worker 直到完成
        deadline = _time.time() + AI_DEWATERMARK_TIMEOUT
        while _time.time() < deadline:
            _time.sleep(5)
            st = _requests.get(
                f"{AI_DEWATERMARK_ENDPOINT}/status/{worker_job}",
                headers={"X-Worker-Token": AI_DEWATERMARK_TOKEN} if AI_DEWATERMARK_TOKEN else {},
                timeout=10,
            ).json()
            if st["status"] == "completed":
                break
            if st["status"] == "failed":
                raise RuntimeError(st.get("error", "AI 去水印 worker 失败"))
        else:
            raise RuntimeError("AI 去水印超时")

        # 下载成片
        dl = _requests.get(
            f"{AI_DEWATERMARK_ENDPOINT}/file/{worker_job}",
            headers={"X-Worker-Token": AI_DEWATERMARK_TOKEN} if AI_DEWATERMARK_TOKEN else {},
            timeout=60,
        )
        dl.raise_for_status()
        out = Path(src).parent / f"{Path(src).stem}_AI去水印.mp4"
        out.write_bytes(dl.content)

        meta = library_mod._load_sidecar(Path(src))
        fftools._write_sidecar(out, meta, "AI去水印")
        new_id = library_mod.encode_id(out.resolve().relative_to(DOWNLOAD_DIR.resolve()).as_posix())
        with process_queue.lock:
            process_queue.jobs[job_id].update(status="completed", out_path=str(out), lib_id=new_id, name=out.name)
        logger.info("ai_dewatermark %s (http) done -> %s", job_id, out.name)

    except Exception as e:
        with process_queue.lock:
            process_queue.jobs[job_id]["status"] = "failed"
            process_queue.jobs[job_id]["error"] = str(e)[:400]
        logger.warning("ai_dewatermark %s failed: %s", job_id, e)

def _process_log(job_id: str, line: str) -> None:
    if not line:
        return
    with process_queue.lock:
        job = process_queue.jobs.get(job_id)
        if not job:
            return
        ts = time.strftime("%H:%M:%S", time.localtime())
        job.setdefault("logs", []).append(f"{ts}  {line.strip()}")
        if len(job.get("logs", [])) > 200:
            job["logs"][:] = job["logs"][-200:]

def _process_set_step(job_id: str, idx: int, status: str, detail: str = "") -> None:
    with process_queue.lock:
        job = process_queue.jobs.get(job_id)
        if not job:
            return
        steps = job.get("steps") or []
        now = time.time()
        for s in steps[:idx]:
            if s["status"] == "pending":
                s["status"] = "done"
                s["updated_at"] = now
        if 0 <= idx < len(steps):
            steps[idx]["status"] = status
            steps[idx]["detail"] = detail[:200]
            steps[idx]["updated_at"] = now

def _run_process(job_id: str, src: str, op: str, params: dict) -> None:
    """后台线程：按 op 调用 ffmpeg_tools，产物落源目录并写侧车，更新 process_queue.jobs。"""
    with process_queue.lock:
        job = process_queue.jobs.get(job_id)
    if not job:
        return
    _process_set_step(job_id, 1, "done", f"源文件: {Path(src).name}")
    _process_set_step(job_id, 2, "running", f"操作: {op}")
    _process_log(job_id, f"开始处理: {op} -> {src}")
    try:
        src_path = Path(src)
        out_dir = src_path.parent
        meta = library_mod._load_sidecar(src_path)
        p = params or {}
        out = None
        suffix = ""
        if op == "audio":
            out = fftools.extract_audio(src_path, out_dir,
                                       fmt=str(p.get("fmt", "mp3")),
                                       bitrate=str(p.get("bitrate", "192k")),
                                       ffmpeg_bin=FFMPEG_BIN)
            suffix = "音频"
        elif op == "gif":
            out = fftools.make_gif(src_path, out_dir,
                                  start=float(p.get("start", 0) or 0),
                                  duration=float(p.get("duration", 5) or 5),
                                  fps=int(p.get("fps", 12) or 12),
                                  width=int(p.get("width", 480) or 480),
                                  ffmpeg_bin=FFMPEG_BIN)
            suffix = "动图"
        elif op == "trim":
            out = fftools.trim_video(src_path, out_dir,
                                    start=float(p.get("start", 0) or 0),
                                    end=float(p.get("end", 0) or 0),
                                    reencode=bool(p.get("reencode", True)),
                                    ffmpeg_bin=FFMPEG_BIN)
            suffix = "片段"
        elif op == "crop":
            out = fftools.crop_video(src_path, out_dir,
                                    crop_expr=str(p.get("crop_expr", "")),
                                    ffmpeg_bin=FFMPEG_BIN)
            suffix = "裁剪"
        elif op == "compress":
            sh = int(p.get("scale_h", 720) or 720)
            out = fftools.compress_video(src_path, out_dir, scale_h=sh,
                                        crf=int(p.get("crf", 28) or 28),
                                        ffmpeg_bin=FFMPEG_BIN)
            suffix = f"压缩{sh}p"
        elif op == "upscale":
            fac = float(p.get("factor", 2) or 2)
            out = fftools.upscale_video(src_path, out_dir, factor=fac,
                                       sharpen=bool(p.get("sharpen", True)),
                                       ffmpeg_bin=FFMPEG_BIN)
            suffix = f"放大{fac}x"
        elif op == "frame":
            out = fftools.snapshot(src_path, out_dir,
                                  at=float(p.get("at", 1) or 0),
                                  fmt=str(p.get("fmt", "jpg")),
                                  width=int(p.get("width", 0) or 0),
                                  ffmpeg_bin=FFMPEG_BIN)
            suffix = "封面"
        elif op == "sheet":
            out = fftools.contact_sheet(src_path, out_dir,
                                       rows=int(p.get("rows", 3) or 3),
                                       cols=int(p.get("cols", 4) or 4),
                                       width=int(p.get("width", 1280) or 1280),
                                       duration=float(meta.get("duration") or 0),
                                       ffmpeg_bin=FFMPEG_BIN)
            suffix = "预览图"
        elif op == "ringtone":
            out = fftools.make_ringtone(src_path, out_dir,
                                       start=float(p.get("start", 0) or 0),
                                       duration=float(p.get("duration", 30) or 30),
                                       fmt=str(p.get("fmt", "m4r")),
                                       fade=float(p.get("fade", 1) or 0),
                                       ffmpeg_bin=FFMPEG_BIN)
            suffix = "铃声"
        elif op == "dewatermark":
            out = fftools.remove_watermark(src_path, out_dir,
                                          x=int(p.get("x", 0) or 0),
                                          y=int(p.get("y", 0) or 0),
                                          w=int(p.get("w", 100) or 100),
                                          h=int(p.get("h", 50) or 50),
                                          show=bool(p.get("show", False)),
                                          band=int(p.get("band", 10) or 10),
                                          ffmpeg_bin=FFMPEG_BIN)
            if out and bool(p.get("show", False)):
                # show 模式：仅画框不做处理，不写侧车，提示用户再指定位置提交
                _process_set_step(job_id, 3, "done", f"预览框: {out.name}")
                _process_log(job_id, f"dewatermark-show 完成: {out.name}")
                with process_queue.lock:
                    job.update(status="completed", out_path=str(out), lib_id="", name=out.name)
                logger.info("process %s (dewatermark-show) done", job_id)
                return
            suffix = "去水印"
        elif op == "ai_dewatermark":
            _run_ai_dewatermark(job_id, str(src_path), p)
            return  # 异步 worker，不在此处收尾
        elif op == "frames":
            # 批量抽帧：产物是一个子目录（不是单文件），单独收尾
            res = fftools.extract_frames(src_path, out_dir,
                                        start=float(p.get("start", 0) or 0),
                                        end=float(p.get("end", 0) or 0),
                                        interval=float(p.get("interval", 1) or 1),
                                        limit=int(p.get("limit", 200) or 200),
                                        fmt=str(p.get("fmt", "jpg")),
                                        width=int(p.get("width", 0) or 0),
                                        ffmpeg_bin=FFMPEG_BIN)
            if not res:
                raise RuntimeError("未抽到任何帧（检查起止时间是否超出视频时长）")
            frames_dir, count = res
            _process_set_step(job_id, 3, "done", f"{count} 帧 -> {frames_dir.name}")
            _process_log(job_id, f"抽帧完成: {count} 帧")
            with process_queue.lock:
                job.update(status="completed", out_path=str(frames_dir), lib_id="",
                           name=frames_dir.name, count=count, is_dir=True)
            logger.info("process %s (frames) done -> %s (%d 帧)", job_id, frames_dir.name, count)
            return
        else:
            raise ValueError("不支持的处理类型")
        if not out or not out.exists() or out.stat().st_size == 0:
            raise RuntimeError("处理未产出有效文件")
        fftools._write_sidecar(out, meta, suffix)
        new_id = library_mod.encode_id(out.resolve().relative_to(DOWNLOAD_DIR.resolve()).as_posix())
        _process_set_step(job_id, 3, "done", f"输出: {out.name}")
        _process_log(job_id, f"处理完成: {out.name}")
        with process_queue.lock:
            job.update(status="completed", out_path=str(out), lib_id=new_id, name=out.name)
        logger.info("process %s (%s) done -> %s", job_id, op, out.name)
    except Exception as e:  # noqa: BLE001
        _process_set_step(job_id, 2, "error", str(e)[:200])
        _process_log(job_id, f"处理失败: {e}")
        with process_queue.lock:
            job["status"] = "failed"
            job["error"] = str(e)[:400]
        logger.warning("process %s (%s) failed: %s", job_id, op, e)

# ---- 订阅监控：关注频道/UP 主，自动下载新发布的视频 ----

def _run_subscription_check(sub: "subs_mod.Subscription") -> dict:
    """探查频道新视频并加入下载队列；更新已知 id 基线。返回结果摘要。

    基线策略：仅把"成功创建下载任务"的视频 id 计入 last_video_ids，
    解析失败的 id 不计入（下次 check 仍可重试）；任务最终失败由用户在下载面板手动重试。

    配额（2026-10-20）：每条新视频计 1 次「下载」额度（与手动下载同一份），
    额度用尽即停本轮、剩余新视频不进基线（下次重试），返回值带 quota_exhausted。
    """
    items = subs_mod.probe_channel(sub.url, sub.cookie, sub.proxy, limit=SUBSCRIBE_PROBE_LIMIT)
    known = set(sub.last_video_ids)
    fresh = [it for it in items if it["id"] not in known]
    task_ids: list[str] = []
    failed: list[str] = []
    submitted_ids: set[str] = set()
    quota_exhausted = False
    # 按订阅主人取账本（后台线程没有 Request，见 _member_store_for_owner）。
    _mb = _member_store_for_owner(getattr(sub, "owner_id", "") or "")
    for it in fresh:
        if not it.get("url"):
            continue
        try:
            url, platform = parse_source(it["url"])
        except (UnsupportedPlatformError, LinkError):
            failed.append(it["title"])
            continue
        # 🔴 2026-10-20：订阅自动下载与手动下载**计同一份额度**。「使用统计」里
        #    「订阅追更」那一行写着「与视频下载共用下载额度」，但这里此前**一次都没计过**
        #    ⇒ 免费档 10 次/日可被订阅完全绕过；且任务没有 usage_ref，不进「使用详情」。
        #    现在：每下一条先扣 1 次；额度用尽即停本轮，剩余新视频**不进基线**，
        #    下一轮 check 会重试（与「解析失败下次重试」同一策略）。
        _ch = None
        if _mb is not None:
            try:
                _ch = _mb.use_daily("download", 1, title=it["title"], platform=platform.name)
            except Exception:  # noqa: BLE001 - 计额度失败不阻断追更（fail-open）
                logger.exception("订阅 %s 计下载额度失败（本次放行）", sub.id)
                _ch = None
            if _ch is not None and not _ch.get("ok"):
                quota_exhausted = True
                break
        task = store.create(
            url=url, title=it["title"], platform=platform.name,
            quality=downloader.quality_label(sub.quality_key), quality_key=sub.quality_key,
            owner_id=getattr(sub, "owner_id", "") or "",
        )
        scheduler.submit(downloader.run_download, task, store, sub.quality_key, sub.cookie, sub.proxy, SINGLE_DOWNLOAD_RETRIES)
        # ref/账本挂到任务上，结束后由 mark_usage_result 回写成败（与手动下载同口径）
        if _ch is not None and _ch.get("ref"):
            task.usage_ref = str(_ch["ref"])
            task._usage_store = _mb
        task_ids.append(task.id)
        submitted_ids.add(it["id"])
    # 合并基线（保留最近最多 200 条），仅计入成功提交任务的 id
    new_baseline = list(sub.last_video_ids)
    for vid in submitted_ids:
        if vid not in new_baseline:
            new_baseline.append(vid)
    sub_store.update(sub.id, last_video_ids=new_baseline[:200], last_checked=time.time())
    return {"sub_id": sub.id, "checked": len(items), "new_videos": fresh, "task_ids": task_ids,
            "failed": failed, "quota_exhausted": quota_exhausted}

class SubscribeRequest(BaseModel):
    url: str = Field(min_length=4, max_length=8192)
    name: str = Field(default="", max_length=128)
    quality: str = Field(default=downloader.BEST_KEY, max_length=16)
    cookie: str = Field(default="", max_length=8192)
    proxy: str = Field(default="", max_length=256)
    auto_check: bool = True

def _subscription_watchdog() -> None:
    """后台常驻：周期性对开启 auto_check 的订阅探查新视频并下载。"""
    while True:
        time.sleep(SUB_CHECK_INTERVAL)
        try:
            for sub in sub_store.list_all():
                if not sub.auto_check:
                    continue
                try:
                    _run_subscription_check(sub)
                except Exception:
                    logger.exception("订阅 %s 自动检查失败", sub.id)
        except Exception:
            logger.exception("订阅 watchpod 异常")

if SUB_ENABLED:
    _sub_watchdog = threading.Thread(target=_subscription_watchdog, name="vdl-sub-watchdog", daemon=True)
    _sub_watchdog.start()

def _retention_watchdog() -> None:
    """后台常驻：按 interval_hours 周期执行时效清理。

    启动后先静置 120 秒再首跑 —— 避免「刚开 App 就开始删东西」，也给用户留出
    改配置的窗口。总开关 auto_enabled 关闭时只空转，不碰磁盘。
    """
    time.sleep(120)
    while True:
        cfg = retention_store.get()
        interval = max(0.25, float(cfg.interval_hours or 6.0)) * 3600
        if not cfg.auto_enabled:
            time.sleep(min(interval, 1800))
            continue
        try:
            result = retention_mod.run(DOWNLOAD_DIR, cfg)
            if result["removed"] or result["failed"]:
                logger.info("自动清理：移除 %s 项，释放 %s，失败 %s 项",
                            result["removed"], retention_mod.human_size(result["freed"]),
                            result["failed"])
                retention_store.update(last_run=result["ran_at"], last_freed=result["freed"],
                                       last_removed=result["removed"])
        except Exception:
            logger.exception("自动清理执行异常")
        time.sleep(interval)

if RETENTION_ENABLED:
    _ret_watchdog = threading.Thread(target=_retention_watchdog, name="vdl-retention", daemon=True)
    _ret_watchdog.start()

# --------------------------------------------------------------------------- #
# App 完整版路由（app-dev）：挂载全部业务模块
#   包含：core(解析/下载) / crypto / fs /
#     convert(视频转换) / dewatermark(PDF/图片去水印) /
#     commentary(视频解说) / library(媒体库) / retention(时效清理) /
#     torrents / subtitles / llm / process / subscriptions /
#   公共 Cookie 池：代码内联于本文件后部，含接收端 /api/cookie/sync、
#     本机 from-local、查询 status、清理 cache/clear 及后台探测 watchdog。
# 必须在 app.mount("/", StaticFiles) 之前 include，否则 "/" 挂载会前缀匹配吞掉 /api/* 路由
from membership import MembershipStore as _MembershipStore
member_store = _MembershipStore()  # 会员引擎共享单例（/api/member + 功能配额判定共用同一状态文件）
from routers import crypto as _crypto_rtr
app.include_router(_crypto_rtr.router)
from routers import fs as _fs_rtr
app.include_router(_fs_rtr.router)
from routers import convert as _convert_rtr
app.include_router(_convert_rtr.router)
from routers import compress as _compress_rtr
app.include_router(_compress_rtr.router)
from routers import sr as _sr_rtr
app.include_router(_sr_rtr.router)
from routers import dewatermark as _dewatermark_rtr
app.include_router(_dewatermark_rtr.router)
from routers import matting as _matting_rtr
app.include_router(_matting_rtr.router)
from routers import idphoto as _idphoto_rtr
app.include_router(_idphoto_rtr.router)
from routers import subtitle as _subtitle_rtr
app.include_router(_subtitle_rtr.router)
from routers import core as _core_rtr
app.include_router(_core_rtr.router)
from routers import image as _image_rtr
app.include_router(_image_rtr.router)
from routers import commentary as _commentary_rtr
app.include_router(_commentary_rtr.router)
from routers import library as _library_rtr
app.include_router(_library_rtr.router)
from routers import retention as _retention_rtr
app.include_router(_retention_rtr.router)
from routers import torrents as _torrents_rtr
app.include_router(_torrents_rtr.router)
from routers import subtitles as _subtitles_rtr
app.include_router(_subtitles_rtr.router)
from routers import llm as _llm_rtr
app.include_router(_llm_rtr.router)
from routers import vision as _vision_rtr
app.include_router(_vision_rtr.router)
from routers import process as _process_rtr
app.include_router(_process_rtr.router)
from routers import sniffer as _sniffer_rtr
app.include_router(_sniffer_rtr.router)
from routers import subscriptions as _subscriptions_rtr
app.include_router(_subscriptions_rtr.router)
from routers import membership as _membership_rtr
from routers import cloud_account as _cloud_rtr
app.include_router(_membership_rtr.router)
app.include_router(_cloud_rtr.router)
from routers import payment as _payment_rtr
app.include_router(_payment_rtr.router)
from routers import extension as _extension_rtr
app.include_router(_extension_rtr.router)
from routers import quota as _quota_rtr
app.include_router(_quota_rtr.router)
from routers import auth as _auth_rtr
app.include_router(_auth_rtr.router)
from routers import admin as _admin_rtr
app.include_router(_admin_rtr.router)
# Cookie 池管理（2026-10-19）：后台可视化「哪个平台还有效、剩几个账号、多久没更新」，
# 并支持粘贴注入。独立成模块（不动 1500 行的 admin.py），鉴权复用 require_admin。
from routers import admin_cookies as _admin_cookies_rtr
app.include_router(_admin_cookies_rtr.router)
from routers import support as _support_rtr
app.include_router(_support_rtr.router)
from routers import system as _system_rtr
app.include_router(_system_rtr.router)
from routers import share as _share_rtr
app.include_router(_share_rtr.router)   # 扫码分享：配置下发 + 二维码生成（2026-09-20）
from routers import pagetool as _pagetool_rtr
app.include_router(_pagetool_rtr.router)   # 文件变网页：本机文件 → 自包含 HTML 页面（2026-09-30）
from routers import userdata as _userdata_rtr
app.include_router(_userdata_rtr.router)   # 用户信息类数据云备份：设置/收藏/历史 + Cookie 密文（2026-10-16）
from routers import cloud_drive as _cloud_drive_rtr
app.include_router(_cloud_drive_rtr.router)   # 网盘文件源（阶段 0：百度网盘直链解析 + BDUSS 凭据）
# 账号 / per-user 会员解析（A1+B2）：供各路由经 request 取当前用户态 store。
# 必须在模块顶层定义（与 member_store 同级），否则 routers 里 `import app` 取不到。
from user_membership import get_current_user_id, current_member_store

# —— 公共 Cookie 池 + 本机 Cookie 缓存（来自 main 分支，合并时保留）——
# 与「仅本机个人缓存」(cookie_cache.py) 严格隔离：独立存储目录、仅白名单域、入池前验真。
_SYNC_RL = {"ts": {}, "lock": threading.Lock()}

def _sync_rate_ok(ip: str, scope: str = "") -> bool:
    """防滥用限流。环回客户端（桌面 App 自连本机后端）豁免。

    - 不传 scope：按单 IP 30 秒至多一次（面向网页访客的 /api/cookie/contribute）。
    - 传 scope：按 (IP, scope) 各 30 秒一次。令牌鉴权的 /api/cookie/sync 需按域
      批量推送十几次，按 IP 限流会让除第一个外的域全部 429（见 web-dev 同源注释）。
    """
    if _is_loopback_ip(ip):
        return True
    key = f"{ip}|{scope}" if scope else ip
    now = time.time()
    with _SYNC_RL["lock"]:
        last = _SYNC_RL["ts"].get(key, 0)
        if now - last < 30:
            return False
        _SYNC_RL["ts"][key] = now
        return True

_COOKIE_ALERT_TS: dict[str, float] = {}

def _cookie_pool_alert(domain: str) -> None:
    # 节流：同域 1 小时内只告警一次，避免每次 resolve 刷屏（正常空池是常态噪音）
    _now = time.time()
    if _now - _COOKIE_ALERT_TS.get(domain, 0) < 3600:
        return
    _COOKIE_ALERT_TS[domain] = _now
    msg = (f"[cookie_pool] 域名 {domain} 公共 Cookie 池已空/全部失效，"
           f"网页版 chrqj 将 403，请补充 Cookie（App 端『同步 Cookie 到云端』或设 CHRQJ_COOKIE）")
    logger.warning(msg)
    try:
        alert = Path.home() / ".videodownloader" / "cookie_pool_alert.json"
        alert.write_text(json.dumps({"domain": domain, "ts": int(time.time()), "empty": True}, ensure_ascii=False))
    except Exception:
        pass
    wh = os.environ.get("VDL_COOKIE_ALERT_WEBHOOK")
    if wh:
        try:
            requests.post(wh, json={"text": msg}, timeout=10)
        except Exception:
            pass

def _cookie_pool_watchdog() -> None:
    while True:
        try:
            from cookie_pool import verify_and_prune, all_domains
            for d in all_domains():
                try:
                    if verify_and_prune(d) == 0:
                        _cookie_pool_alert(d)
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(1800)  # 30 分钟一轮

def _start_cookie_pool_watchdog() -> None:
    t = threading.Thread(target=_cookie_pool_watchdog, daemon=True)
    t.start()

@app.post("/api/cookie/cache/clear")
def cookie_cache_clear() -> dict:
    """清除本机 Cookie 缓存（仅删 ~/.videodownloader/cookies，不影响浏览器本身）。"""
    from cookie_cache import clear_cookie_cache
    n = clear_cookie_cache()
    return {"ok": True, "cleared": n}

@app.post("/api/cookie/sync")
def cookie_sync(payload: dict, request: Request) -> dict:
    """App 端上报指定站点登录态到公共池（需用户知情同意 + 客户端令牌）。"""
    token = os.environ.get("VDL_COOKIE_SYNC_TOKEN", "")
    supplied = (payload or {}).get("token", "")
    if not token or supplied != token:
        raise HTTPException(status_code=403, detail="无效令牌")
    domain = (payload or {}).get("domain", "")
    cookie = (payload or {}).get("cookie", "")
    if not domain or not cookie:
        raise HTTPException(status_code=400, detail="缺少 domain/cookie")
    from cookie_pool import add_cookie, verify_cookie, is_allowed
    if not is_allowed(domain):
        raise HTTPException(status_code=400, detail="不支持的域名")
    ip = (request.client.host if request.client else "") or ""
    # 按「IP + 域」限流：一次「同步到云端」会按域批量推送十几次，
    # 按 IP 限流会把除第一个以外的域全部 429 掉（见 _sync_rate_ok 注释）。
    if not _sync_rate_ok(ip, domain):
        raise HTTPException(status_code=429, detail="操作过于频繁，请稍后再试")
    ok = verify_cookie(domain, cookie)
    if ok is False:
        raise HTTPException(status_code=400, detail="Cookie 无效，未能通过目标站验真")
    added = add_cookie(domain, cookie, source="sync")
    return {"ok": True, "added": added, "verified": (ok is True)}

@app.post("/api/cookie/sync/from-local")
def cookie_sync_from_local(payload: dict, request: Request) -> dict:
    """仅本机(App 端)调用：读取本机浏览器指定站点登录态并上报到公共池。

    安全审计 2026-10-11（P3-1）：此前只比 `request.client.host`，而该值可被
    `X-Forwarded-For` 伪造（见 `_is_local_direct` 说明）—— 被骗过就等于让公网调用方
    触发「读本机浏览器登录态并上报」。改用共享真源（要求未携带任何代理头）。
    """
    if not _is_local_direct(request):
        raise HTTPException(status_code=403, detail="仅允许本机调用")
    from cookie_pool import add_cookie, verify_cookie, is_allowed, is_contrib_only, _norm_domain
    domain = _norm_domain((payload or {}).get("domain") or "chrqj.com")
    if not is_allowed(domain):
        raise HTTPException(status_code=400, detail="不支持的域名")
    if is_contrib_only(domain):
        # 百度网盘：BDUSS = 完整账号权限，只接受网盘页的显式贡献，不走这条隐式通道
        raise HTTPException(
            status_code=400,
            detail="百度网盘登录态请在网盘页点「贡献到共享池」贡献（需你确认后才会分享）",
        )
    header = downloader.get_browser_cookie_header(domain, f"https://{domain}/")
    if not header:
        raise HTTPException(
            status_code=400,
            detail=f"本机浏览器未检测到 {domain} 登录态，请先在浏览器登录该站",
        )
    ok = verify_cookie(domain, header)
    if ok is False:
        raise HTTPException(status_code=400, detail="本机 Cookie 无效，未能通过目标站验真")
    added = add_cookie(domain, header, source="local")
    # 只写本机池不够：解析时的取用顺序是 本机快照(cookie_cache) > 本机池 > 云端池，
    # 而快照 TTL 有 30 天且命中就不比对浏览器 —— 不刷新它，用户重新登录后
    # 旧快照仍会生效，表现为「明明同步成功了却还是未登录」。这里 header 已在手，
    # 写快照是零成本。
    try:
        from cookie_cache import _save as _cache_save
        _cache_save(domain, header)
    except Exception:
        pass
    # 2026-10-21：此前这里只写本机池就返回，但函数名与 docstring 都写着「上报到公共池」，
    # 调用方看到 added/verified 全 true 会以为已上云（实测云端池毫无变化，白等）。
    # 现改为：有云端配置就顺手推一次，让「登录后立刻生效」不必等前端 30 分钟定时器。
    # 边界：本机池写入才是主线，云端失败只降级进 cloud 字段，绝不把本机成功判成失败。
    cloud: dict | None = None
    url, token = _cloud_sync_config()
    if url and token:
        try:
            resp = _push_cookie_to_cloud(domain, header, url, token)
            cloud = {"pushed": bool((resp or {}).get("ok")), "verified": (resp or {}).get("verified")}
        except Exception as e:  # 网络/限流/云端未就绪：本机已生效，不影响主流程
            cloud = {"pushed": False, "reason": str(e)[:120]}
    return {"ok": True, "added": added, "verified": (ok is True), "cloud": cloud}

def _cloud_sync_config() -> tuple[str, str]:
    """返回 (sync_url, token)。优先读 env，其次读本机配置文件。

    配置文件路径：~/.videodownloader/cloud_sync.json = {"url": "...", "token": "..."}
    桌面版用此配置把本机浏览器登录态推送到网页版(Railway)公共池。
    """
    url = (os.environ.get("VDL_COOKIE_SYNC_URL") or "").strip()
    token = (os.environ.get("VDL_COOKIE_SYNC_TOKEN") or "").strip()
    cfg = Path.home() / ".videodownloader" / "cloud_sync.json"
    if (not url or not token) and cfg.exists():
        try:
            d = json.loads(cfg.read_text(encoding="utf-8"))
            url = url or (d.get("url") or "").strip()
            token = token or (d.get("token") or "").strip()
        except Exception:
            pass
    return url, token

def _ensure_vps_env() -> None:
    """桌面 App 启动时注入 VPS 解析配置：经线上转发端点复用 VPS worker。

    桌面端无本地隧道，若未显式配置 VDL_WORKER_URL，则复用 cloud_sync 配置
    （~/.videodownloader/cloud_sync.json 的 url/token，即推送用的线上地址）：
      - VDL_WORKER_URL    = <线上地址>（如 https://hanyuxz.top）
      - VDL_WORKER_PROXY  = "off"（直连线上，禁用 18889 本地隧道默认代理）
      - VDL_COOKIE_SYNC_TOKEN = <token>（/v1/resolve 转发端点校验用）
    这样 downloader._call_vps_worker 对 14 个 VPS 平台（抖音/快手/微博/爱奇艺/
    红果/微视/1905…）自动走线上转发，桌面 App 零额外配置获得 VPS 解析能力。

    另见下方「双节点转发」：若 cloud_sync.json 声明了 peer（海外节点地址），
    则把本机区域标为 cn、对端标为 peer，让海外链接整体转发给海外节点处理。
    """
    global NODE_REGION, PEER_ENDPOINT
    if os.environ.get("VDL_WORKER_URL"):
        return  # 已显式配置，不覆盖
    url, token = _cloud_sync_config()
    if not url:
        return
    if not os.environ.get("VDL_WORKER_PROXY"):
        os.environ["VDL_WORKER_PROXY"] = "off"
    if not os.environ.get("VDL_COOKIE_SYNC_TOKEN") and token:
        os.environ["VDL_COOKIE_SYNC_TOKEN"] = token
    os.environ["VDL_WORKER_URL"] = url.rstrip("/")
    logger.info("[vps] 桌面端注入 VPS 转发配置: %s", os.environ["VDL_WORKER_URL"])

    # 双节点转发（可选）：桌面端本身跑在海内、没有出国链路，YouTube 等海外站直连
    # 必然失败（解析超时）。若 cloud_sync.json 显式声明了 peer（海外节点地址），则
    # 告知前端「本机区域 = cn、对端 = <peer>」：前端 baseFor() 会把海外链接的解析/
    # 下载整体转发给对端，由对端本地出网取流、成果再经 CDN 回传 —— 实测经
    # Cloudflare 回传比从大陆直连该节点快一个数量级，且本机无需任何翻墙链路。
    # 未声明 peer 时不改动 region/peer，保持原有单节点行为不变。
    if not os.environ.get("VDL_PEER_ENDPOINT") and not os.environ.get("VDL_REGION"):
        cfg_file = Path.home() / ".videodownloader" / "cloud_sync.json"
        if cfg_file.exists():
            try:
                raw = json.loads(cfg_file.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                raw = {}
            peer = str(raw.get("peer") or "").strip().rstrip("/")
            if peer:
                NODE_REGION = str(raw.get("region") or "cn").strip().lower() or "cn"
                PEER_ENDPOINT = peer
                # 同步写入环境变量：downloader.run_remote_download 只能从 os.environ 取
                # （downloader 不能 import app，会循环依赖）。用 setdefault 以免覆盖
                # 部署者显式设置的值；能进这个分支说明两者原本都为空。
                os.environ.setdefault("VDL_REGION", NODE_REGION)
                os.environ.setdefault("VDL_PEER_ENDPOINT", peer)
                logger.info("[vps] 桌面端启用双节点转发: region=%s peer=%s", NODE_REGION, PEER_ENDPOINT)

_ensure_vps_env()

def _push_cookie_to_cloud(domain: str, header: str, url: str, token: str) -> dict:
    """把单站 Cookie 推送到云端公共池（POST /api/cookie/sync）。

    带浏览器 User-Agent 以绕过 Cloudflare 对 Python-urllib 的拦截（error 1010）。
    对 429/5xx 做有限指数退避重试，降低连发导致的限流。
    """
    import time
    import urllib.request
    import urllib.error

    target = url.rstrip("/") + "/api/cookie/sync"
    body = json.dumps({"token": token, "domain": domain, "cookie": header}).encode()
    last_err: Exception | None = None
    for attempt in range(3):
        req = urllib.request.Request(
            target,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise
        except Exception as e:
            last_err = e
            raise
    raise last_err or RuntimeError("推送 Cookie 到云端失败")

def _sync_domains_to_cloud(url: str, token: str) -> list[dict]:
    """按域读取本机浏览器登录态并推送云端池，返回逐域结果。

    to-cloud（定时/超管手动）与 auto（登录态变更触发）共用这一份实现，
    避免两条入口各写一份循环而漂移（站间 1s 间隔是为避开 Cloudflare 限流）。
    """
    import time
    from cookie_pool import _root_domains, is_contrib_only, is_guest_only, is_share_only

    results: list[dict] = []
    # 🔴 2026-10-25：「仅显式贡献」域（百度网盘）必须跳过自动同步 ——
    # 本函数会遍历所有白名单域、静默解密用户浏览器里的登录态推上云，
    # 百度一旦进来就等于替所有用户做了「共享我的网盘账号」这个决定。
    # 它只能走网盘页那个带确认弹窗的「贡献到共享池」按钮。
    # 🔴 2026-10-28：再加两类排除，都是在止「往池子里灌没用的条目」这个血：
    #   ① 分享页/短链/极速版域（iesdouyin / gifshow / chenzhongtech / xhslink）——
    #      登录态在根域，这些域推上去实测 13 条**有效 0 条**，还白解 3 次 Cookies 库；
    #   ② 本机游客态（下方 is_guest_only 逐域判）—— 有 Cookie 没登录态字段，
    #      推上去只会让后台显示「有」，桌面端拿去解析必挂。
    domains = [d for d in sorted(_root_domains())
               if d != "chrqj.com"
               and not is_contrib_only(d)
               and not is_share_only(d)]
    for idx, domain in enumerate(domains):
        try:
            header = downloader.get_browser_cookie_header(domain, f"https://{domain}/")
        except Exception:
            header = None
        if not header:
            results.append({"domain": domain, "pushed": False, "reason": _COOKIE_NO_LOGIN})
            continue
        # 有结构校验表、却一个登录态字段都不命中 ⇒ 本机没登录该站，只是分享页/游客
        # 残留。**reason 必须复用 _COOKIE_NO_LOGIN**：下游 failed 判定是
        # `reason != _COOKIE_NO_LOGIN`，换新常量会被当成失败并留在下一轮无限重试。
        try:
            if is_guest_only(domain, header):
                results.append({"domain": domain, "pushed": False,
                                "reason": _COOKIE_NO_LOGIN, "guest_only": True})
                continue
        except Exception:
            pass
        # 顺手把刚解密出来的新鲜值刷进本机快照。为什么必须做：cookie_cache 的 TTL
        # 是 30 天且「命中就直接用、不与浏览器比对」，用户重新登录后旧快照会一直
        # 挡在新鲜值前面（YouTube 独立链路 2026-09-27 已为此单修过：
        # SIDCC/__Secure-*PSIDTS 是滚动字段，快照落后即被判未登录）。
        # 这里 header 本来就已经解密好了，写快照是零成本，不会多一次解密。
        try:
            from cookie_cache import _save as _cache_save
            _cache_save(domain, header)
        except Exception:
            pass
        try:
            resp = _push_cookie_to_cloud(domain, header, url, token)
            results.append({
                "domain": domain,
                "pushed": bool(resp.get("ok")),
                "verified": resp.get("verified"),
            })
        except Exception as e:
            results.append({"domain": domain, "pushed": False, "reason": str(e)[:120]})
        # 连发易触发 Cloudflare 限流，站间间隔 1s（最后一站除外）
        if idx < len(domains) - 1:
            time.sleep(1.0)
    return results

# 浏览器登录态变更检测：登录任何站点都会写 Cookies 库，所以「库文件 mtime/size 变了」
# 就是「用户刚登录过」最廉价可靠的信号（stat 级，毫秒；不需要解密、不碰钥匙串）。
_COOKIE_WATCH_STATE = Path.home() / ".videodownloader" / "cookie_sync_state.json"

# 推送结果的 reason 常量：只有「本机压根没登录这个站」才算「无需推送」，
# 其余（429 / 超时 / 网络错误）都算失败，必须留在下一轮重试。
# 必须两侧共用同一常量：曾用字面量比较，改字就静默失配、失败判定失效。
_COOKIE_NO_LOGIN = "本机未登录"

def _cookie_db_signature() -> dict[str, str]:
    """各浏览器 Cookies **数据库文件**的 (mtime, size) 快照，key 为库绝对路径。

    🔴 2026-10-28 修复两处（两处都会让「无变化跳过」失效，后者更隐蔽）：
    ① 旧实现遍历 `_BROWSER_COOKIE_PROFILES` 的 root 且只取 `roots[0]` —— 那是
       Windows 路径 `%LOCALAPPDATA%\\...`，在 macOS/Linux 上 glob 不到 ⇒ 快照恒为空
       ⇒ 每轮都全量解密+推送（本机实测 2h 934 次、23% 撞 429）。
    ② 即便取到 root，root 是**目录**：Cookie 原地写进 db 文件不会改父目录 mtime/size
       ⇒ 恒判「未变化」，用户新登录的站反而永远推不上去。
    现改为精确到 db 文件（`downloader.cookie_db_paths()` 委托 browser_cookies 的
    跨平台 profile 枚举，本机 5 个库 / 3.5ms）。
    """
    sig: dict[str, str] = {}
    try:
        paths = downloader.cookie_db_paths()
    except Exception:  # noqa: BLE001 — 探测失败按「无变化」处理，绝不阻断同步入口
        return sig
    for db in paths:
        try:
            st = os.stat(db)
        except OSError:
            continue
        sig[db] = f"{int(st.st_mtime)}:{st.st_size}"
    return sig

@app.post("/api/cookie/sync/auto")
def cookie_sync_auto(request: Request) -> dict:
    """仅本机(App 端)调用：**登录态有变化才**推送云端池。

    为什么需要这条：to-cloud 每次都要逐域实时解密浏览器（14 站实测 16.7s），
    所以前端只能 30 分钟跑一轮 —— 用户登录完要干等。而「是否有变化」用
    Cookies 库的 mtime/size 就能判断（毫秒级），所以前端可以高频调用本接口：
    没变化立刻返回 skipped，有变化才真解密+推送。

    失败不落盘签名：下次调用会重试，避免一次网络抖动把这一轮变更永久跳过。
    """
    ip = (request.client.host if request.client else "") or ""
    if ip not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="仅允许本机调用")
    url, token = _cloud_sync_config()
    if not url or not token:
        # 与 to-cloud 保持同一句指引：只说「未配置」等于让用户去猜。
        raise HTTPException(
            status_code=400,
            detail="未配置云端同步地址/令牌。请在桌面版环境设置 VDL_COOKIE_SYNC_URL 与 "
                   "VDL_COOKIE_SYNC_TOKEN，或写入 ~/.videodownloader/cloud_sync.json",
        )

    sig = _cookie_db_signature()
    prev: dict[str, str] = {}
    try:
        if _COOKIE_WATCH_STATE.exists():
            prev = json.loads(_COOKIE_WATCH_STATE.read_text(encoding="utf-8")) or {}
    except Exception:
        prev = {}
    if sig and prev == sig:
        return {"ok": True, "skipped": True, "reason": "浏览器登录态未变化"}

    results = _sync_domains_to_cloud(url, token)
    pushed = sum(1 for r in results if r.get("pushed"))
    # 「本机未登录」是正常状态，不算失败；429/超时/网络错误才算。
    failed = sum(
        1 for r in results
        if not r.get("pushed") and r.get("reason") != _COOKIE_NO_LOGIN
    )
    # 只有「确实推到过东西，且这一轮没有失败站点」才记签名。
    # 🔴 为什么必须把 failed 也算进来（2026-10-21 连推压测实测踩到）：
    # 签名一旦写上，只要浏览器库不再变化就不会再进这个函数，本轮撞 429/超时的域
    # 就永远不会补推——实测 10 站有登录态、7 站 429，写签名后它们被静默跳过。
    # 反过来，若全站都失败（pushed=0）也保留旧签名，下一轮重试。
    if (pushed or not prev) and not failed:
        try:
            _COOKIE_WATCH_STATE.parent.mkdir(parents=True, exist_ok=True)
            # 必须走 atomic_write_json：本仓禁止裸 Path.write_text 写状态文件
            # （截断/并发会留下半截 JSON，解析失败会被当成「无变化」而反复全量推）。
            atomic_io.atomic_write_json(_COOKIE_WATCH_STATE, sig, indent=None)
        except Exception:
            pass
    return {
        "ok": True,
        "skipped": False,
        "pushed": pushed,
        "failed": failed,
        "retry_next": bool(failed),
        "total": len(results),
        "results": results,
    }

@app.post("/api/cookie/sync/to-cloud")
def cookie_sync_to_cloud(request: Request) -> dict:
    """仅本机(App 端)调用：读取本机浏览器各强反爬站的登录态并推送到网页版公共池。

    这是「网页版由桌面版共享登录态」的核心：桌面版常驻时，自动把抖音/B站/快手等
    登录态推送到 Railway 公共池，网页版访客无需手动粘贴即可复用。
    """
    ip = (request.client.host if request.client else "") or ""
    if ip not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="仅允许本机调用")
    url, token = _cloud_sync_config()
    if not url or not token:
        raise HTTPException(
            status_code=400,
            detail="未配置云端同步地址/令牌。请在桌面版环境设置 VDL_COOKIE_SYNC_URL 与 "
                   "VDL_COOKIE_SYNC_TOKEN，或写入 ~/.videodownloader/cloud_sync.json",
        )
    results = _sync_domains_to_cloud(url, token)
    pushed = sum(1 for r in results if r.get("pushed"))
    return {"ok": True, "pushed": pushed, "total": len(results), "results": results}

@app.post("/api/cookie/contribute")
def cookie_contribute(payload: dict, request: Request) -> dict:
    """访客自愿把本次登录态贡献到公共池（需前端显式勾选 + 后端验真）。

    仅白名单域名；单 IP 限频；verify_cookie 验真（明确无效才拒，网络不可达放行）。
    优酷等站可一并贡献 ckey（播放签名），供 UPS 专用通道使用。
    """
    from urllib.parse import urlparse
    url = (payload or {}).get("url", "")
    cookie = (payload or {}).get("cookie", "")
    ckey = (payload or {}).get("ckey", "")
    host = urlparse(url).netloc if url else ""
    from cookie_pool import add_cookie, add_ckey, is_allowed, verify_cookie, _norm_domain, _strip_sub
    domain = _strip_sub(_norm_domain(host))
    if not domain or not is_allowed(domain):
        raise HTTPException(status_code=400, detail="该平台暂不支持公共池贡献")
    cookie = (cookie or "").strip()
    if not cookie:
        raise HTTPException(status_code=400, detail="缺少 cookie")
    if domain == "bilibili.com" and "=" not in cookie:
        cookie = f"SESSDATA={cookie}"
    ip = (request.client.host if request.client else "") or ""
    if not _sync_rate_ok(ip):
        raise HTTPException(status_code=429, detail="操作过于频繁，请稍后再试")
    ok = verify_cookie(domain, cookie)
    if ok is False:
        raise HTTPException(
            status_code=400,
            detail="Cookie 无效，未能通过目标站验真（请确认已登录且为完整 Cookie；"
                   "优酷需含登录态字段）",
        )
    added = add_cookie(domain, cookie, source="contrib")
    if ckey:
        add_ckey(domain, ckey, source="contrib")
        added = True
    logger.info("[cookie_pool] contrib domain=%s ip=%s added=%s ckey=%s", domain, ip, added, bool(ckey))
    return {"ok": True, "added": added, "verified": (ok is True), "ckey": bool(ckey)}

# 浏览器扩展送进来的域名：只允许常规域名形态（字母数字与 . -），长度 ≤ 100。
# 目的不是白名单（本机缓存允许任意站，只影响用户自己），而是堵路径注入 ——
# cookie_cache._cache_file 把 "/" 换成 "_"，但没必要让它有机会处理 "/" 本身。
_PUSH_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.\-]{0,98}[a-z0-9])?$")

@app.post("/api/cookie/browser-push")
def cookie_browser_push(payload: dict, request: Request) -> dict:
    """接收浏览器扩展采集到的登录态，写入本机 Cookie 缓存（2026-10-18）。

    为什么要这条通道（与 /api/cookie/sync/from-local 的分工）
    ------------------------------------------------------
    from-local 是「App 自己去解密本机浏览器 Cookie 库」：只认 Chrome/Edge/Brave/
    Chromium 的默认 profile 模式，且 macOS 上会撞钥匙串授权。而浏览器扩展读的是
    **用户此刻正在用的那个 profile**，多开、便携版、公司策略加密库都能拿到。
    所以扩展直接把 Cookie 送进来，比 App 自己解密更准。

    安全边界（三条，缺一不可）
    ------------------------
      1) 仅本机直连：socket 对端必须环回且未带任何代理头（_is_local_direct 真源）；
      2) 来源只允许扩展 / 本机页面 / 无 Origin：网页版（https://hanyuxz.top）等
         外部来源一律拒 —— 否则任意站点都能借用户的浏览器把伪造 Cookie 写进本机
         缓存，下一次解析就会带着它们去请求目标站；
      3) 只写 ~/.videodownloader/cookies（本机缓存），**不进公共池**。是否贡献给
         云端由用户在扩展里显式勾选，另走 /api/cookie/contribute。
    另外这里**不做验真**：验真要对目标站发请求（12 个站就是十几次网络往返），而这条
    通道存的是「用户自己机器上的登录态」，存错了只影响他自己、且解析失败会回退到
    浏览器解密；云端池那条必须验真，因为别人也会用得到。
    """
    if not _is_local_direct(request):
        raise HTTPException(status_code=403, detail="仅允许本机调用")
    origin = (request.headers.get("origin") or "").strip()
    if origin and not (_EXT_ORIGIN_RE.match(origin) or _LOCAL_ORIGIN_RE.match(origin)):
        raise HTTPException(status_code=403, detail="仅允许浏览器扩展调用")

    from cookie_pool import _norm_domain
    domain = _norm_domain((payload or {}).get("domain") or "")
    if not domain or not _PUSH_HOST_RE.match(domain):
        raise HTTPException(status_code=400, detail="domain 不合法")
    cookie = ((payload or {}).get("cookie") or "").strip()
    if not cookie or "=" not in cookie:
        raise HTTPException(status_code=400, detail="cookie 不合法（需为 name=value; … 形式）")
    if len(cookie) > 8192:
        raise HTTPException(status_code=400, detail="cookie 过长")
    if "\r" in cookie or "\n" in cookie:
        # 防请求头注入：这份字符串会被原样塞进下游请求的 Cookie 头
        raise HTTPException(status_code=400, detail="cookie 含非法字符")

    from cookie_cache import _save, _cache_file
    _save(domain, cookie)
    # _save 全异常吞掉（历史设计：缓存是增强功能，不能影响主链路），
    # 所以这里回读确认真的落盘了，别对扩展谎报成功。
    saved = False
    try:
        f = _cache_file(domain)
        if f.exists():
            saved = (json.loads(f.read_text(encoding="utf-8")).get("header") or "") == cookie
    except Exception:
        saved = False
    if not saved:
        raise HTTPException(status_code=500, detail="本机缓存写入失败")
    logger.info("[cookie] browser-push domain=%s bytes=%d platform=%s",
                domain, len(cookie), (payload or {}).get("platform") or "")
    return {"ok": True, "saved": True, "domain": domain, "bytes": len(cookie)}

@app.get("/api/cookie/status")
def cookie_status(url: str = "") -> dict:
    """查询某链接是否需要 Cookie、本机浏览器是否已有可用登录态、以及公共池新鲜度。"""
    if not url:
        raise HTTPException(status_code=400, detail="请提供链接")
    _assert_safe_url(url)
    url, platform = parse_source(url)
    host = _host_of(url)
    info = downloader.detect_browser_cookie(host)
    pool_ts = None
    try:
        from cookie_pool import _candidates, _pool_file
        for d in _candidates(host):
            f = _pool_file(d)
            if f.exists():
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    for c in data.get("cookies", []):
                        ts = c.get("ts")
                        if ts and (pool_ts is None or ts > pool_ts):
                            pool_ts = ts
                except Exception:
                    pass
    except Exception:
        pass
    return {
        "host": host,
        "platform": platform.key,
        "needed": downloader.is_cookie_hardened_host(host),
        "available": info["available"],
        "browser": info["browser"],
        "profile": info["profile"],
        # 与 routers/core.py 的同名路由保持同形（路由注册顺序上 routers 先匹配，
        # 但两份返回体必须一致，否则谁先谁后一变，前端就少字段）。
        "label": info.get("label"),
        "running": info.get("running", False),
        "count": info.get("count", 0),
        "status": info.get("status", "absent"),
        "hint": info.get("hint", ""),
        "pool_updated_at": pool_ts,
    }

from routers import voice_studio as _voice_studio_rtr
app.include_router(_voice_studio_rtr.router)

# 静态资源禁缓存（2026-09-29 对齐 web-dev）：js/css 曾被浏览器/边缘按扩展名
# 默认缓存，改完前端用户拿旧文件。前端引用自带 ?v= 版本戳，缓存收益为零。
@app.middleware("http")
async def _static_no_store(request, call_next):
    response = await call_next(request)
    p = request.url.path
    if not p.startswith("/api/") and (
        p.endswith(".js") or p.endswith(".css") or p.endswith(".html") or p == "/"
    ):
        response.headers["Cache-Control"] = "no-store"
    return response


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
