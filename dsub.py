#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsub — 用 DeepSeek 把视频字幕翻译成中文，再嵌回视频（软字幕）或烧录（硬字幕）。

用法:
    dsub                                      # 不带参数 -> 交互向导，一路问到底
    dsub --set-key                            # 安全保存 API Key（DPAPI 加密）
    dsub "视频.mkv"                           # 软字幕嵌回   -> 视频.zh.mkv
    dsub "视频.mkv" --burn                    # 烧录硬字幕   -> 视频.zh.hardsub.mp4
    dsub "视频.mkv" --srt-in "字幕.srt"       # 视频没有字幕时导入字幕文件
    dsub "视频.mkv" --srt-only                # 只要翻译好的字幕文件
    dsub "视频.mkv" --no-translate --burn     # 字幕已是中文：只烧录，不调 API
    dsub --clear-cache                        # 清空翻译缓存

字幕来源优先级: --srt-in > 视频内嵌字幕轨 > 视频同名外挂字幕(.srt/.ass/.ssa/.vtt)

环境变量:
    DEEPSEEK_API_KEY  可选，优先级高于 --set-key 保存的密钥
    DEEPSEEK_BASE_URL 可选，默认 https://api.deepseek.com
    DEEPSEEK_MODEL    可选，默认 deepseek-chat
    FFMPEG            可选，ffmpeg.exe 路径或所在目录，默认从 PATH 找
    FFPROBE           可选，默认取 FFMPEG 同目录的 ffprobe，否则从 PATH 找

数据文件（都在 %USERPROFILE%\\.dsub 下）:
    key.dpapi          DPAPI 加密的 API Key
    trans_cache.json   翻译缓存，按「原文+目标语言」指纹跨视频复用
"""
import argparse
import ctypes
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request

try:
    # 统一 UTF-8 输出：Windows 控制台本来就按 UTF-8 字节转宽字符，管道/重定向到文件也不会乱码。
    # 打包成 exe 后冻结环境不会继承 UTF-8 模式，不显式设置就会打出乱码。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".flv", ".ts", ".m4v", ".webm",
              ".wmv", ".mpg", ".mpeg", ".rmvb", ".m2ts", ".vob", ".3gp", ".f4v"}
__version__ = "1.0.0"
SIDECAR_EXTS = (".srt", ".ass", ".ssa", ".vtt", ".sub")
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".dsub")
KEY_FILE = os.path.join(CONFIG_DIR, "key.dpapi")
KEY_TXT = os.path.join(CONFIG_DIR, "key.txt")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


# ---------------------------------------------------------------- 基础工具

LOG_DIR = os.path.join(CONFIG_DIR, "logs")
MAX_LOGS = 10
LOG_FILE = None


def default_log_path():
    return os.path.join(LOG_DIR, "dsub-%s.log" % time.strftime("%Y%m%d"))


def _write_log(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    except OSError:
        pass


def log(msg):
    print(msg, flush=True)
    if LOG_FILE:
        _write_log(msg)


def logf(msg):
    """只写日志文件，不打到控制台（放详细的 ffmpeg 报错，控制台只留一句结论）"""
    if LOG_FILE:
        _write_log(msg)


def redact(text):
    """别把密钥写进日志。"""
    return re.sub(r"(--api-key[\s=]+)\S+", r"\1***", text)


def prune_logs():
    try:
        files = sorted((os.path.join(LOG_DIR, f) for f in os.listdir(LOG_DIR)
                        if f.startswith("dsub-") and f.endswith(".log")),
                       key=os.path.getmtime)
        for old in files[:-MAX_LOGS]:
            os.remove(old)
    except OSError:
        pass


def setup_log(ns):
    global LOG_FILE
    if ns.no_log:
        LOG_FILE = None
        return
    path = os.path.abspath(ns.log) if ns.log else default_log_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        open(path, "a", encoding="utf-8").close()      # 先探一下能不能写
        LOG_FILE = path
    except OSError as e:
        LOG_FILE = None
        print("! 无法写日志文件（%s），仅输出到屏幕。" % e, file=sys.stderr)
        return
    if not ns.log:
        prune_logs()
    log("")
    log("=" * 64)
    log("dsub %s 启动  %s" % (__version__, time.strftime("%Y-%m-%d %H:%M:%S")))
    log("命令行    %s" % redact(" ".join(sys.argv)))
    log("Python    %s / %s" % (sys.version.split()[0], sys.platform))


def _as_exe(p, name):
    """允许环境变量指向目录。"""
    if os.path.isdir(p):
        return os.path.join(p, name + ".exe")
    return p


def ffmpeg_paths():
    exe = _as_exe(os.environ.get("FFMPEG") or "ffmpeg", "ffmpeg")
    probe = os.environ.get("FFPROBE")
    if probe:
        probe = _as_exe(probe, "ffprobe")
    else:
        d = os.path.dirname(exe)
        probe = os.path.join(d, "ffprobe.exe") if d else "ffprobe"
    return exe, probe


FFMPEG, FFPROBE = ffmpeg_paths()


def check_tools():
    """防呆：开工前确认 ffmpeg/ffprobe 真能跑。"""
    for name, exe in (("ffmpeg", FFMPEG), ("ffprobe", FFPROBE)):
        try:
            subprocess.run([exe, "-version"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30)
        except FileNotFoundError:
            raise SystemExit("找不到 %s（%s）。请安装 ffmpeg 并加入 PATH，"
                             "或用环境变量 FFMPEG / FFPROBE 指定路径。" % (name, exe))
        except Exception as e:
            raise SystemExit("%s 无法执行（%s）：%s" % (name, exe, e))


def run(cmd, cwd=None, capture=True):
    log("$ " + " ".join(cmd))
    try:
        p = subprocess.run(cmd, cwd=cwd, text=True, encoding="utf-8", errors="replace",
                           stdout=subprocess.PIPE if capture else None,
                           stderr=subprocess.PIPE if capture else None)
    except FileNotFoundError:
        raise SystemExit("找不到 %s；请安装 ffmpeg 或设置 FFMPEG 环境变量。" % cmd[0])
    if p.returncode != 0:
        detail = (p.stderr or "")[-2000:] if capture else "（详见上方 ffmpeg 输出）"
        logf("命令失败（退出码 %d）：%s\n%s" % (p.returncode, " ".join(cmd), detail))
        raise SystemExit("命令失败（退出码 %d）：%s\n%s" % (p.returncode, " ".join(cmd), detail))
    return p.stdout or ""


# ---------------------------------------------------------------- 界面：信息 / 进度

def fmt_size(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return "%.2f %s" % (n, u)
        n /= 1024
    return "%.2f PB" % n


def fmt_time(sec):
    sec = int(sec or 0)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%02d:%02d" % (m, s)


def probe(video):
    """一次 ffprobe 拿全部信息。"""
    out = run([FFPROBE, "-v", "error", "-show_format", "-show_streams", "-of", "json", video])
    try:
        return json.loads(out)
    except ValueError:
        raise SystemExit("ffprobe 输出无法解析，请确认 FFPROBE 指向真正的 ffprobe。\n" + out[:200])


def sub_streams(info):
    """视频里的字幕轨，顺序与 ffmpeg 的 0:s:N 一致。"""
    return [s for s in info.get("streams", []) if s.get("codec_type") == "subtitle"]


def _tag(s, name):
    return (s.get("tags") or {}).get(name) or ""


def _duration(info):
    fmt = info.get("format", {})
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    return float(fmt.get("duration") or v.get("duration") or 0)


def print_info(info, video):
    streams = info.get("streams", [])
    fmt = info.get("format", {})
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if v is None:
        raise SystemExit("这个文件里没有视频流，可能不是视频文件：%s" % video)
    dur = _duration(info)
    rate = fmt.get("bit_rate") or v.get("bit_rate")
    fps = v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/0"
    try:
        num, den = (int(x) for x in fps.split("/"))
        fps = "%.3f" % (num / den) if den else "?"
    except Exception:
        fps = str(fps)

    log("=" * 64)
    log("  视频信息")
    log("  文件    %s" % video)
    log("          %s   封装 %s   总码率 %s"
        % (fmt_size(os.path.getsize(video)),
           (fmt.get("format_name") or "?").split(",")[0],
           ("%d kb/s" % (int(rate) // 1000)) if rate else "未知"))
    log("  时长    %s   视频编码 %s %sx%s %s fps"
        % (fmt_time(dur), v.get("codec_name", "?"), v.get("width", "?"),
           v.get("height", "?"), fps))
    for s in streams:
        if s.get("codec_type") != "audio":
            continue
        lang = _tag(s, "language")
        log("  音频    %s %s Hz %sch%s"
            % (s.get("codec_name", "?"), s.get("sample_rate", "?"), s.get("channels", "?"),
               "  (%s)" % lang if lang else ""))
    subs = sub_streams(info)
    if not subs:
        log("  字幕    无内嵌字幕")
    for i, s in enumerate(subs):
        log("  字幕    #%d  %s%s%s" % (i, s.get("codec_name", "?"),
                                      "  (%s)" % _tag(s, "language") if _tag(s, "language") else "",
                                      "  %s" % _tag(s, "title") if _tag(s, "title") else ""))
    log("=" * 64)
    return dur


_LINE = {"w": 0}


def _w(s):
    """显示宽度：中日韩全角字符占两格（按字符数算会留下进度条残影）"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _draw(line):
    if not sys.stdout.isatty():
        return
    pad = max(0, _LINE["w"] - _w(line))
    sys.stdout.write("\r" + line + " " * pad)
    sys.stdout.flush()
    _LINE["w"] = _w(line)


def _clear_line():
    if _LINE["w"] and sys.stdout.isatty():
        sys.stdout.write("\r" + " " * _LINE["w"] + "\r")
        sys.stdout.flush()
    _LINE["w"] = 0


def bar_text(frac, width=24):
    frac = max(0.0, min(1.0, frac))
    n = int(frac * width)
    if n >= width:
        return "=" * width
    return "=" * n + ">" + "-" * (width - n - 1)


def draw_bar(label, frac, right, t0):
    el = time.time() - t0
    eta = el / frac - el if frac > 0.005 else 0
    _draw("  %s [%s] %5.1f%%  %s  已用 %s  剩余 %s"
          % (label, bar_text(frac), frac * 100, right, fmt_time(el), fmt_time(eta)))


def with_progress(cmd):
    """在输出文件前插入 ffmpeg 的机器可读进度输出。"""
    return cmd[:-1] + ["-nostats", "-progress", "pipe:1", "-loglevel", "error"] + [cmd[-1]]


def run_progress(cmd, duration, label, cwd=None):
    """跑 ffmpeg 并实时显示进度条。"""
    if not sys.stdout.isatty() or duration <= 0:
        log("  %s…" % label)
        out = run(cmd, cwd=cwd)
        log("  %s 完成" % label)
        return out
    # 不回显整条命令：长命令会折行，把 \r 进度条打乱（出错时仍会完整打印）
    err = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    t0 = time.time()
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=err, text=True,
                         encoding="utf-8", errors="replace", bufsize=1)
    try:
        for line in p.stdout:
            k, _, val = line.strip().partition("=")
            if k != "out_time" or val in ("N/A", ""):
                continue
            try:                       # out_time=HH:MM:SS.uuuuuu，各版本 ffmpeg 都有
                h, m, s = val.split(":")
                done = int(h) * 3600 + int(m) * 60 + float(s)
            except ValueError:
                continue
            draw_bar(label, done / duration, fmt_time(done) + "/" + fmt_time(duration), t0)
    finally:
        p.wait()
        _clear_line()
    err.seek(0)
    detail = err.read()[-2000:]
    err.close()
    if p.returncode != 0:
        raise SystemExit("命令失败（退出码 %d）：%s\n%s" % (p.returncode, " ".join(cmd), detail))
    log("  %s 完成，用时 %.1f 秒" % (label, time.time() - t0))
    return ""


# ---------------------------------------------------------------- 密钥管理

class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi(data, unprotect=False):
    """Windows DPAPI：密钥绑定当前 Windows 账户，别的账户/机器解不开。非 Windows 返回 None。"""
    if sys.platform != "win32":
        return None
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buf = ctypes.create_string_buffer(bytes(data), len(data))
    blob_in = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _BLOB()
    fn = crypt32.CryptUnprotectData if unprotect else crypt32.CryptProtectData
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("DPAPI 调用失败（WinError %d）" % ctypes.get_last_error())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def mask(key):
    return key[:3] + "…" + key[-4:] if len(key) > 10 else "***"


def save_key(key):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    blob = dpapi(key.encode("utf-8"))
    if blob is None:
        with open(KEY_TXT, "w", encoding="utf-8") as f:
            f.write(key)
        try:
            os.chmod(KEY_TXT, 0o600)
        except OSError:
            pass
        log("! 非 Windows 平台无法用 DPAPI 加密，密钥以明文保存，请自行确保目录权限。")
        return KEY_TXT
    tmp = KEY_FILE + ".tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, KEY_FILE)
    return KEY_FILE


def load_key():
    if os.path.isfile(KEY_FILE):
        try:
            raw = dpapi(open(KEY_FILE, "rb").read(), unprotect=True)
            if raw:
                return raw.decode("utf-8").strip()
        except Exception as e:
            log("! 已保存的密钥无法解密（%s）。可能换了 Windows 账户，请重新 --set-key。" % e)
    if os.path.isfile(KEY_TXT):
        return open(KEY_TXT, encoding="utf-8").read().strip()
    return ""


def forget_key():
    hit = [p for p in (KEY_FILE, KEY_TXT) if os.path.isfile(p)]
    for p in hit:
        os.remove(p)
    log("已删除密钥文件。" if hit else "没有已保存的密钥。")


# ---------------------------------------------------------------- 翻译缓存
# 按「原文 + 目标语言」做内容寻址：同一句话在任何视频里译过一次就永久复用，
# 所以相同文件重跑是零成本，相似文件（大量重复台词/术语）也能省掉大部分请求。

CACHE_FILE = os.path.join(CONFIG_DIR, "trans_cache.json")
MAX_CACHE = 200000            # ponytail: 超过就按插入顺序丢最旧的，够用；要更聪明再上 LRU


def cache_key(target, text):
    return hashlib.sha1((target + "\x00" + text).encode("utf-8")).hexdigest()


def load_cache():
    if os.path.isfile(CACHE_FILE):
        try:
            data = json.load(open(CACHE_FILE, encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except ValueError:
            log("! 翻译缓存文件损坏，已忽略并重建：%s" % CACHE_FILE)
    return {}


def save_cache(cache):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if len(cache) > MAX_CACHE:
        cache = dict(list(cache.items())[-MAX_CACHE:])
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    os.replace(tmp, CACHE_FILE)


def clear_cache():
    if not os.path.isfile(CACHE_FILE):
        log("没有翻译缓存。")
        return
    n = len(load_cache())
    os.remove(CACHE_FILE)
    log("已清空翻译缓存（%d 条）。" % n)


def resolve_key(ns):
    if ns.api_key:
        return ns.api_key.strip(), "--api-key"
    env = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if env:
        return env, "环境变量 DEEPSEEK_API_KEY"
    saved = load_key()
    if saved:
        return saved, "已保存的密钥 %s" % mask(saved)
    return "", ""


def api_call(path, body, key, ns, timeout=180):
    url = ns.base_url.rstrip("/") + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def api_error_hint(code, detail):
    if code == 401:
        return "API Key 无效或已失效。用 dsub --set-key 重新保存。"
    if code == 402:
        return "DeepSeek 账户余额不足，请先充值。"
    if code == 429:
        return "请求过于频繁（限速），稍后重试或调小 --batch。"
    return "DeepSeek 返回 HTTP %s：%s" % (code, detail)


def verify_key(key, ns):
    try:
        api_call("/chat/completions", {"model": ns.model, "max_tokens": 1,
                                       "messages": [{"role": "user", "content": "ping"}]},
                 key, ns, timeout=60)
    except urllib.error.HTTPError as e:
        raise SystemExit(api_error_hint(e.code, e.read().decode("utf-8", "replace")[:300]))
    except Exception as e:
        raise SystemExit("连接 DeepSeek 失败：%s" % e)


def set_key_cmd(ns):
    key = (ns.api_key or "").strip()
    if not key:
        log("在 DeepSeek 控制台创建 Key：https://platform.deepseek.com/api_keys")
        key = getpass.getpass("粘贴 API Key（输入时不回显）: ").strip()
    if not key:
        raise SystemExit("没有输入密钥，已取消。")
    if not key.startswith("sk-"):
        log("! 这个 Key 不以 sk- 开头，DeepSeek 的 Key 通常是 sk- 开头。")
    log("正在验证密钥…")
    verify_key(key, ns)
    path = save_key(key)
    log("密钥可用，已保存：%s" % path)
    if sys.platform == "win32":
        log("已用 Windows DPAPI 加密，只有当前 Windows 账户能解密；换账户需重新 --set-key。")
    log("以后直接运行 dsub 即可，不需要再设置环境变量。")


# ---------------------------------------------------------------- ffprobe / 选源

ZH_LANGS = {"chi", "zho", "zh", "cmn", "chs", "cht", "zh-cn", "zh-hans", "zh-tw"}
BITMAP_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}


def pick_sub(streams, want=None):
    """want 是 0:s:N 里的 N。返回 (N, stream)。"""
    if want is not None:
        if not 0 <= want < len(streams):
            raise SystemExit("--stream %d 超出范围（共 %d 条字幕轨）" % (want, len(streams)))
        s = streams[want]
        if s.get("codec_name") in BITMAP_CODECS:
            raise SystemExit("第 %d 条是图形字幕（%s），无法转成文本，需要 OCR。" % (want, s["codec_name"]))
        return want, s
    for i, s in enumerate(streams):  # 优先非中文轨
        lang = ((s.get("tags") or {}).get("language") or "").lower()
        if s.get("codec_name") not in BITMAP_CODECS and lang not in ZH_LANGS:
            return i, s
    for i, s in enumerate(streams):
        if s.get("codec_name") not in BITMAP_CODECS:
            return i, s
    return None, None


def sidecar_for(video):
    stem = os.path.splitext(video)[0]
    for e in SIDECAR_EXTS:
        p = stem + e
        if os.path.isfile(p):
            return p
    return ""


def choose_source(video, ns, info):
    """返回 (kind, 说明, payload)；kind 为 file / embed。"""
    if ns.srt_in:
        p = os.path.abspath(ns.srt_in)
        if not os.path.isfile(p):
            raise SystemExit("找不到字幕文件：%s" % p)
        return "file", "--srt-in %s" % p, p
    streams = sub_streams(info)
    side = sidecar_for(video)
    if streams:
        n, sub = pick_sub(streams, ns.stream)
        if n is not None:
            desc = "内嵌 0:s:%d（%s%s）" % (n, sub.get("codec_name"),
                                        "，语言 " + sub["tags"]["language"]
                                        if (sub.get("tags") or {}).get("language") else "")
            return "embed", desc, (n, streams)
        if ns.stream is not None:
            raise SystemExit("--stream %d 是图形字幕（PGS/DVD），无法转成文本，需要 OCR。" % ns.stream)
        if not side:
            raise SystemExit("视频里的字幕轨都是图形字幕（PGS/DVD），无法转成文本。"
                             "请用 --srt-in 指定文本字幕文件。")
    if side:
        return "file", "同名外挂字幕 %s" % side, side
    raise SystemExit("这个视频既没有内嵌字幕轨，也没有同名外挂字幕（%s.srt / .ass / .vtt）。\n"
                     "请下载字幕后用 --srt-in \"字幕文件\" 指定，或先做语音识别生成字幕。"
                     % os.path.splitext(os.path.basename(video))[0])


# ---------------------------------------------------------------- SRT 解析

TAG_RE = re.compile(r"\{[^}]*\}|<[^>]*>")
TIME_RE = re.compile(r"(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})")


def clean_text(s):
    s = s.replace("\\N", " ").replace("\\n", " ").replace("\\h", " ")
    s = TAG_RE.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_srt(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    cues = []
    for block in re.split(r"\n{2,}", text.strip()):
        lines = block.split("\n")
        ti = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ti is None:
            continue
        m = TIME_RE.search(lines[ti])
        if not m:
            continue
        cues.append({"start": m.group(1), "end": m.group(2),
                     "text": clean_text(" ".join(lines[ti + 1:]))})
    return cues


def format_srt(cues, translations):
    out = []
    for i, c in enumerate(cues):
        t = (translations[i] or "").strip() or c["text"]
        out.append("%d\n%s --> %s\n%s\n" % (i + 1, c["start"], c["end"], t))
    return "\n".join(out)


def looks_chinese(cues):
    sample = [c["text"] for c in cues[:100] if c["text"].strip()]
    if not sample:
        return False
    return sum(1 for t in sample if CJK_RE.search(t)) / len(sample) >= 0.3


def chunks(seq, max_items, max_chars):
    cur, n = [], 0
    for it in seq:
        ln = len(it[1])
        if cur and (len(cur) >= max_items or n + ln > max_chars):
            yield cur
            cur, n = [], 0
        cur.append(it)
        n += ln
    if cur:
        yield cur


# ---------------------------------------------------------------- DeepSeek 翻译

def chat(messages, ns, key, tries=4):
    last = ""
    for attempt in range(tries):
        try:
            data = api_call("/chat/completions", {
                "model": ns.model, "messages": messages, "temperature": 1.3,
                "response_format": {"type": "json_object"}, "stream": False}, key, ns)
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code not in (429, 500, 502, 503, 504):
                raise SystemExit(api_error_hint(e.code, detail))
            last = "HTTP %s %s" % (e.code, detail)
        except Exception as e:
            last = str(e)
        time.sleep(2 * (attempt + 1))
    raise SystemExit("DeepSeek 连续请求失败：%s" % last)


def parse_json_obj(s):
    s = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", s.strip()).strip()
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b < 0:
        raise ValueError("模型未返回 JSON 对象：" + s[:200])
    return json.loads(s[a:b + 1])


def translate_items(items, ns, key):
    """items: [(cue_index, text)] -> {cue_index: 译文}。缺失的自动对半重试。"""
    prompt = json.dumps({str(i): t for i, t in items}, ensure_ascii=False)
    sysmsg = ("你是字幕翻译。把用户给出的 JSON 里每个值翻译成%s，键保持不变。"
              "只输出 JSON 对象，不要解释、不要加注释。译文口语化、简洁，"
              "人名与专有名词保留原文，不要合并或拆分条目。" % ns.target)
    got = {}
    try:
        obj = parse_json_obj(chat([{"role": "system", "content": sysmsg},
                                   {"role": "user", "content": prompt}], ns, key))
        got = {i: str(obj[str(i)]).strip() for i, _ in items
               if str(i) in obj and str(obj[str(i)]).strip()}
    except ValueError as e:
        log("  ! 返回格式异常，拆分重试：%s" % e)
    if len(got) == len(items) or len(items) == 1:
        return got
    mid = len(items) // 2
    got.update(translate_items(items[:mid], ns, key))
    got.update(translate_items(items[mid:], ns, key))
    return got


def translate_all(cues, ns, key):
    """按句复用缓存，只把没译过的句子发出去。"""
    cache = {} if ns.no_cache else load_cache()
    keys = [cache_key(ns.target, c["text"]) if c["text"].strip() else "" for c in cues]
    todo = [(i, c["text"], keys[i]) for i, c in enumerate(cues)
            if keys[i] and keys[i] not in cache]
    reused = sum(1 for i, c in enumerate(cues) if keys[i] and keys[i] in cache)
    if reused:
        log("  缓存    命中 %d 条，只需新译 %d 条" % (reused, len(todo)))
    total = len(cues)
    done = reused
    t0 = time.time()
    for batch in chunks([(i, t) for i, t, _ in todo], ns.batch, 2000):
        got = translate_items(batch, ns, key)
        for i, _, k in todo:
            if i in got:
                cache[k] = got[i]
        done += len(batch)
        if sys.stdout.isatty():
            draw_bar("翻译", done / max(1, total), "%d/%d 条" % (done, total), t0)
        else:
            log("  翻译    [%d/%d 条]" % (done, total))
        if not ns.no_cache:
            save_cache(cache)
    _clear_line()
    log("  翻译    完成 %d 条（复用 %d，新译 %d），用时 %s"
        % (total, reused, len(todo), fmt_time(time.time() - t0)))
    return [cache.get(keys[i], "") for i in range(len(cues))]


# ---------------------------------------------------------------- ffmpeg 命令

def esc_filter_path(p):
    return (p.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
            .replace(",", "\\,").replace("[", "\\[").replace("]", "\\]"))


# 只搬 mkv/mp4 都认的流；mp4 里常有 tmcd/bin_data 这类数据流，-map 0 会让 matroska 直接拒收
SAFE_MAPS = ["-map", "0:v?", "-map", "0:a?", "-map", "0:s?", "-map", "0:t?"]


def mux_cmd(video, srt, out, n_existing_subs):
    cmd = ([FFMPEG, "-y", "-i", video, "-i", srt] + SAFE_MAPS +
           ["-map", "1:0", "-map_metadata", "0", "-map_chapters", "0", "-c", "copy"])
    for k in range(n_existing_subs):          # 原字幕轨取消默认，让中文字幕自动上屏
        cmd += ["-disposition:s:%d" % k, "0"]
    cmd += ["-disposition:s:%d" % n_existing_subs, "default",
            "-metadata:s:s:%d" % n_existing_subs, "language=chi",
            "-metadata:s:s:%d" % n_existing_subs, "title=中文", out]
    return cmd


def burn_cmd(video, srt_name, out, ns, acodec="copy"):
    vf = "subtitles=" + esc_filter_path(srt_name)
    if ns.force_style:
        vf += ":force_style=" + ns.force_style.replace(",", "\\,")
    cmd = [FFMPEG, "-y", "-i", video, "-map", "0:v:0", "-map", "0:a?",
           "-vf", vf, "-c:v", ns.vcodec, "-crf", str(ns.crf), "-preset", ns.preset,
           "-c:a", acodec]
    if acodec != "copy":
        cmd += ["-b:a", "192k"]
    return cmd + [out]


# ---------------------------------------------------------------- 防呆检查

def check_input(video):
    if not os.path.isfile(video):
        raise SystemExit("找不到文件：%s" % video)
    if os.path.getsize(video) < 1024:
        raise SystemExit("文件太小，不像是视频：%s" % video)
    if os.path.splitext(video)[1].lower() not in VIDEO_EXTS:
        log("! 扩展名 %s 不是常见视频格式，仍然尝试处理。" % os.path.splitext(video)[1])


def unique_path(path):
    """给已存在的路径找一个不冲突的副本名：视频.zh.mkv -> 视频.zh(1).mkv"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    for n in range(1, 1000):
        p = "%s(%d)%s" % (stem, n, ext)
        if not os.path.exists(p):
            return p
    return "%s(%s)%s" % (stem, time.strftime("%Y%m%d%H%M%S"), ext)


def check_output(out, video, ns, srt_only=False):
    """校验输出路径，返回最终要写的路径（可能因为要出副本而改名）。"""
    if os.path.abspath(out) == os.path.abspath(video):
        raise SystemExit("输出会覆盖原视频。请用 -o 换个名字。")
    if os.path.exists(out) and not ns.overwrite:
        if ns.auto_rename:
            new = unique_path(out)
            log("  ! %s 已存在，自动改出副本：%s"
                % (os.path.basename(out), os.path.basename(new)))
            out = new
        else:
            raise SystemExit("输出文件已存在：%s\n要覆盖请加 --overwrite，"
                             "要另存一份请加 --auto-rename。" % out)
    d = os.path.dirname(out) or "."
    if not os.path.isdir(d):
        raise SystemExit("输出目录不存在：%s" % d)
    if srt_only:
        return out
    if not ns.burn and os.path.splitext(out)[1].lower() in (".mp4", ".m4v", ".mov"):
        raise SystemExit("软字幕不能装进 mp4（需要 mov_text），请输出 .mkv，或改用 --burn 烧录。")
    try:
        free = shutil.disk_usage(d).free
        need = os.path.getsize(video) * (2 if ns.burn else 1.2)
        if free < need:
            raise SystemExit("磁盘空间可能不够：预计需要约 %.1f GB，%s 只剩 %.1f GB。"
                             % (need / 2 ** 30, d, free / 2 ** 30))
    except OSError:
        pass
    return out


def confirm_chinese(cues, ns):
    """字幕疑似已经是中文：默认直接拿来用（烧录/嵌回/导出），不白花翻译钱。"""
    if ns.no_translate or ns.yes or not looks_chinese(cues):
        return
    if sys.stdin.isatty():
        log("! 这些字幕看起来已经是中文了。")
        if _ask("  直接使用这份字幕（不翻译）？[Y/n]: ", "y").lower() not in ("n", "no"):
            ns.no_translate = True
            return
        log("  好，还是翻译一遍。")
        return
    raise SystemExit("字幕疑似已是中文。只想烧录/嵌入/导出原字幕请加 --no-translate；"
                     "确实要再翻一遍请加 --yes。")


# ---------------------------------------------------------------- 主流程

def build_parser():
    ap = argparse.ArgumentParser(prog="dsub", description="用 DeepSeek 翻译视频字幕并嵌回/烧录",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("用法:", 1)[-1])
    ap.add_argument("video", nargs="?", help="输入视频")
    ap.add_argument("-o", "--output", help="输出文件（默认 <视频名>.zh.mkv 或 .hardsub.mp4）")
    ap.add_argument("--srt-in", metavar="FILE", help="导入外挂字幕文件（.srt/.ass/.ssa/.vtt）")
    ap.add_argument("-s", "--stream", type=int, help="内嵌字幕轨序号 0:s:N，默认自动选非中文轨")
    ap.add_argument("--burn", action="store_true", help="烧录硬字幕（重编码视频），默认只嵌软字幕")
    ap.add_argument("--no-translate", action="store_true",
                    help="不翻译，直接用原字幕（已有中文字幕只烧录/嵌入/导出时用；不需要 API Key）")
    ap.add_argument("--srt-only", action="store_true", help="只导出字幕文件，不处理视频")
    ap.add_argument("--no-cache", action="store_true", help="不使用也不写入翻译缓存")
    ap.add_argument("--clear-cache", action="store_true", help="清空翻译缓存后退出")
    ap.add_argument("--log", metavar="FILE", help="日志文件路径，默认 %%USERPROFILE%%\\.dsub\\logs\\")
    ap.add_argument("--no-log", action="store_true", help="不写日志文件")
    ap.add_argument("--target", default="简体中文", help="目标语言，默认 简体中文")
    ap.add_argument("--batch", type=int, default=20, help="每次请求的字幕条数，默认 20")
    ap.add_argument("--no-srt", action="store_true", help="不保留翻译后的 .srt 文件")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--overwrite", action="store_true", help="输出已存在时直接覆盖")
    g.add_argument("--auto-rename", action="store_true",
                   help="输出已存在时自动改名出副本（视频.zh(1).mkv），不覆盖也不报错")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="确认要翻译（字幕疑似已是中文时，不加这个就会改成直接用原字幕）")
    ap.add_argument("--set-key", action="store_true", help="安全保存 DeepSeek API Key 后退出")
    ap.add_argument("--forget-key", action="store_true", help="删除已保存的 API Key 后退出")
    ap.add_argument("--api-key", help="本次临时使用的 API Key（不落盘）")
    ap.add_argument("--base-url", default=os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com")
    ap.add_argument("--model", default=os.environ.get("DEEPSEEK_MODEL") or "deepseek-chat")
    ap.add_argument("--force-style", default="FontName=Microsoft YaHei,Outline=1,MarginV=24",
                    help="烧录时的 libass 样式，逗号分隔")
    ap.add_argument("--vcodec", default="libx264", help="烧录编码器，默认 libx264")
    ap.add_argument("--crf", type=int, default=20, help="烧录画质，默认 20")
    ap.add_argument("--preset", default="medium", help="x264 preset，默认 medium")
    ap.add_argument("--self-test", action="store_true", help="运行内置自检后退出")
    ap.add_argument("-V", "--version", action="version", version="dsub " + __version__)
    return ap


def main(argv=None):
    ap = build_parser()
    ns = ap.parse_args(argv)
    if not ns.self_test:
        setup_log(ns)
    try:
        return dispatch(ns, ap)
    except SystemExit as e:
        if isinstance(e.code, str) and e.code:
            logf("!! 出错退出：\n%s" % e.code)
        elif e.code not in (0, None):
            logf("!! 退出码 %s" % e.code)
        if LOG_FILE:
            log("详细日志：%s" % LOG_FILE)
        raise
    except KeyboardInterrupt:
        logf("!! 用户中断（Ctrl+C）")
        _clear_line()
        if LOG_FILE:
            log("已中断。详细日志：%s" % LOG_FILE)
        raise


def dispatch(ns, ap):
    if ns.self_test:
        self_test()
        return 0
    if ns.set_key:
        set_key_cmd(ns)
        return 0
    if ns.forget_key:
        forget_key()
        return 0
    if ns.clear_cache:
        clear_cache()
        return 0
    if ns.srt_only and ns.no_srt:
        ap.error("--srt-only 与 --no-srt 冲突（前者就是要写出字幕文件）")
    if not ns.video:
        return wizard(ns)          # 没给参数 -> 交互向导

    video = os.path.abspath(ns.video)
    key, key_src = resolve_key(ns)
    if not key and not ns.no_translate:
        log("! 没有可用的 API Key；如果字幕已经是中文，用 --no-translate 可以只烧录/导出。")
    return process(ns, video, key, key_src)


def process(ns, video, key, key_src, info=None, show_info=True):
    """完整流程：探测 -> 抽字幕 -> 翻译（可跳过）-> 嵌回/烧录/只导出字幕。"""
    check_input(video)
    check_tools()
    info = info if info is not None else probe(video)
    duration = print_info(info, video) if show_info else _duration(info)

    stem, outdir = os.path.splitext(os.path.basename(video))[0], os.path.dirname(video)
    if ns.srt_only:
        if ns.output:
            out = os.path.abspath(ns.output)
            if not out.lower().endswith((".srt", ".ass", ".vtt")):
                out = os.path.splitext(out)[0] + ".srt"
        else:
            out = os.path.join(outdir, stem + ".zh.srt")
        srt_out = check_output(out, video, ns, srt_only=True)
        video_out = None
    else:
        out = os.path.abspath(ns.output) if ns.output else os.path.join(
            outdir, stem + (".zh.hardsub.mp4" if ns.burn else ".zh.mkv"))
        out = check_output(out, video, ns)
        video_out = out
        srt_out = os.path.splitext(out)[0] + ".srt"

    kind, desc, payload = choose_source(video, ns, info)
    log("  任务")
    log("  字幕    %s" % desc)
    log("  输出    %s" % out)
    log("  模式    %s" % ("只导出字幕文件（不处理视频）" if ns.srt_only else
                          ("烧录硬字幕（重编码视频，耗时较长）" if ns.burn
                           else "软字幕嵌回（视频音频直接复制，很快）")))
    if ns.no_translate:
        log("  翻译    跳过（直接使用原字幕）")
    log("  密钥    %s" % (key_src or "（不需要）"))
    log("=" * 64)
    dropped = [s for s in info.get("streams", [])
               if s.get("codec_type") not in ("video", "audio", "subtitle", "attachment")]
    if dropped and not ns.srt_only:
        log("  ! 源文件里有 %d 个数据流（时间码/元数据等），mkv 装不下，已忽略。" % len(dropped))

    work = tempfile.mkdtemp(prefix="dsub-")
    t_start = time.time()
    try:
        src_srt = os.path.join(work, "src.srt")
        n_embed = 0
        if kind == "embed":
            n, streams = payload
            run([FFMPEG, "-y", "-v", "error", "-i", video, "-map", "0:s:%d" % n, "-c:s", "srt", src_srt])
            n_embed = len(streams)
        elif payload.lower().endswith(".srt"):
            shutil.copyfile(payload, src_srt)
        else:  # ass/ssa/vtt 交给 ffmpeg 转
            run([FFMPEG, "-y", "-v", "error", "-i", payload, "-c:s", "srt", src_srt])

        cues = parse_srt(open(src_srt, encoding="utf-8-sig", errors="replace").read())
        if not cues:
            raise SystemExit("解析出的字幕是空的，确认字幕文件格式是否正确。")
        confirm_chinese(cues, ns)          # 可能是中文 -> 自动改成不翻译
        if not ns.no_translate:
            if not key:
                raise SystemExit("翻译需要 API Key。运行：  dsub --set-key\n"
                                 "或设置环境变量 DEEPSEEK_API_KEY；"
                                 "已有中文字幕只想烧录/导出的话请加 --no-translate。")
            log("  翻译    %d 条字幕 -> %s（模型 %s）" % (len(cues), ns.target, ns.model))
            translations = translate_all(cues, ns, key)
            empty = sum(1 for i, c in enumerate(cues) if c["text"].strip() and not translations[i])
            if empty:
                log("! 有 %d 条没译出来，已保留原文。" % empty)
        else:
            translations = [c["text"] for c in cues]
        zh_srt = os.path.join(work, "zh.srt")
        with open(zh_srt, "w", encoding="utf-8", newline="\n") as f:
            f.write(format_srt(cues, translations))

        if ns.srt_only or not ns.no_srt:
            shutil.copyfile(zh_srt, srt_out)
            log("  字幕    已写出 %s" % srt_out)

        if ns.srt_only:
            pass
        elif ns.burn:
            log("  烧录    开始（%s / CRF %d / preset %s）" % (ns.vcodec, ns.crf, ns.preset))
            try:
                run_progress(with_progress(burn_cmd(video, "zh.srt", video_out, ns)),
                             duration, "烧录", cwd=work)
            except SystemExit:
                log("! 音频无法直接复制进 mp4，改为 AAC 192k 重试")
                run_progress(with_progress(burn_cmd(video, "zh.srt", video_out, ns, "aac")),
                             duration, "烧录", cwd=work)
        else:
            log("  封装    开始（直接复制视频音频流）")
            try:
                run_progress(with_progress(mux_cmd(video, zh_srt, video_out, n_embed)),
                             duration, "封装")
            except SystemExit:
                log("! 直接封装失败，改为把原字幕转成 SRT 后重试（详细报错见日志）")
                run_progress(with_progress([FFMPEG, "-y", "-i", video, "-i", zh_srt] + SAFE_MAPS +
                                           ["-map", "1:0", "-map_metadata", "0", "-map_chapters", "0",
                                            "-c:v", "copy", "-c:a", "copy", "-c:s", "srt",
                                            "-metadata:s:s:%d" % n_embed, "language=chi",
                                            "-metadata:s:s:%d" % n_embed, "title=中文", video_out]),
                             duration, "封装")
        log("完成：%s（总用时 %s）" % (out, fmt_time(time.time() - t_start)))
        if LOG_FILE:
            log("日志：%s" % LOG_FILE)
    except KeyboardInterrupt:
        _clear_line()
        raise SystemExit("\n已中断。已译好的句子存在 %s，重跑同一条命令会接着翻。" % CACHE_FILE)
    finally:
        _clear_line()
        shutil.rmtree(work, ignore_errors=True)
    return 0


# ---------------------------------------------------------------- 交互向导

def _ask(prompt, default=""):
    try:
        return input(prompt).strip() or default
    except EOFError:
        return default


def clean_path(s):
    """处理拖拽进来的路径：带引号、带 & 前缀、带首尾空格。"""
    s = (s or "").strip().lstrip("&").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1]
    return s.strip()


def _pick(prompt, options, default=1, group=1):
    """group=N 表示 options 里每 N 项一组：第一项是选项，后面 N-1 项是它的说明。"""
    n = 0
    for i, label in enumerate(options):
        if i % group:
            log("       %s" % label)
        else:
            n += 1
            log("    %d) %s%s" % (n, label, "   [默认]" if n == default else ""))
    while True:
        s = _ask(prompt, str(default))
        if s.isdigit() and 1 <= int(s) <= n:
            return int(s)
        log("  ! 请输入 1-%d" % n)


def ask_subtitle(ns, video, info):
    """挑字幕来源：内嵌轨 / 同名外挂 / 手动指定文件。"""
    subs = sub_streams(info)
    side = sidecar_for(video)
    options, params = [], []
    for i, s in enumerate(subs):
        if s.get("codec_name") in BITMAP_CODECS:
            continue
        lang = _tag(s, "language")
        options.append("内嵌字幕轨 #%d（%s%s%s）" % (i, s.get("codec_name", "?"),
                                                "，语言 " + lang if lang else "",
                                                "，" + _tag(s, "title") if _tag(s, "title") else ""))
        params.append({"stream": i})
    if side:
        options.append("外挂字幕文件 %s" % side)
        params.append({"srt_in": side})
    options.append("我自己指定字幕文件")
    params.append(None)

    real = len(options) - 1                    # 最后一项永远是「我自己指定」
    if real == 0:
        log("  这个视频没有内嵌字幕，旁边也没有同名外挂字幕。")
    elif real == 1:
        log("  字幕    %s" % options[0])
    else:
        log("  发现这些字幕来源，选一个：")

    while True:
        if real == 0:
            choice = len(options)
        elif real == 1:
            log("    回车就用它，或输入 2 自己指定字幕文件")
            choice = 2 if _ask("  请选择 [1]: ", "1") == "2" else 1
        else:
            choice = _pick("  请选择 [1]: ", options)
        p = params[choice - 1]
        if p is not None:
            if "stream" in p:
                ns.stream = p["stream"]
            else:
                ns.srt_in = p["srt_in"]
            return
        path = clean_path(_ask("  字幕文件路径（.srt / .ass / .ssa / .vtt，可拖进来）: "))
        if path and os.path.isfile(path):
            ns.srt_in = path
            return
        log("  ! 找不到字幕文件：%s" % (path or "(空)"))


def ask_output(ns, video):
    stem = os.path.splitext(os.path.basename(video))[0]
    if ns.srt_only:
        default = os.path.join(os.path.dirname(video), stem + ".zh.srt")
        label = "输出字幕文件"
    else:
        default = os.path.join(os.path.dirname(video),
                               stem + (".zh.hardsub.mp4" if ns.burn else ".zh.mkv"))
        label = "输出文件"
    while True:
        p = clean_path(_ask("  %s [%s]: " % (label, os.path.basename(default))))
        out = os.path.abspath(p) if p else default
        if ns.srt_only and not out.lower().endswith((".srt", ".ass", ".vtt")):
            out = os.path.splitext(out)[0] + ".srt"
        if out == os.path.abspath(video):
            log("  ! 不能覆盖原视频，换个名字。")
            continue
        if not ns.srt_only and not ns.burn and \
                os.path.splitext(out)[1].lower() in (".mp4", ".m4v", ".mov"):
            log("  ! 软字幕装不进 mp4，请用 .mkv，或退回上一步选烧录。")
            continue
        if os.path.exists(out):
            log("  %s 已存在，怎么办？" % os.path.basename(out))
            act = _pick("  请选择 [1]: ",
                        ["覆盖它", "输出副本 %s" % os.path.basename(unique_path(out)),
                         "我自己换个文件名"])
            if act == 1:
                ns.overwrite = True
                break
            if act == 2:
                out = unique_path(out)
                break
            continue
        break
    ns.output = out
    return out


def ensure_key(ns, key, key_src):
    """只有真的要翻译时才问密钥——已有中文字幕只烧录/导出不需要 API Key。"""
    if key:
        return key, key_src
    log("  翻译需要 API Key，去这里创建一个：")
    log("          https://platform.deepseek.com/api_keys")
    k = getpass.getpass("  粘贴 API Key（输入时不回显）: ").strip()
    if not k:
        raise SystemExit("没有密钥，已取消。")
    log("  正在验证密钥…")
    verify_key(k, ns)
    log("  密钥可用。")
    if _ask("  保存到本机（Windows DPAPI 加密）？[Y/n]: ", "y").lower() not in ("n", "no"):
        save_key(k)
        log("  已保存，以后不用再输。")
    return k, "本次输入"


def wizard(ns):
    if not sys.stdin.isatty():
        raise SystemExit("没有给参数、当前也不是交互终端。\n用法：dsub \"视频.mkv\" [选项]    "
                         "完整参数见 dsub --help")
    log("=" * 64)
    log("  dsub 交互模式 —— 直接回车用默认值，Ctrl+C 随时退出")
    log("=" * 64)
    key, key_src = resolve_key(ns)
    if key:
        log("  密钥    %s" % key_src)

    while True:
        video = ""
        while not os.path.isfile(video):
            video = os.path.abspath(clean_path(
                _ask("  视频文件路径（可直接把文件拖进窗口）: ")))
            if not video or not os.path.isfile(video):
                log("  ! 找不到文件：%s" % video)
        check_tools()
        info = probe(video)
        log("=" * 64)
        print_info(info, video)

        ns.srt_in, ns.stream, ns.output, ns.overwrite = None, None, None, False
        ask_subtitle(ns, video, info)

        log("  要翻译吗？")
        ns.no_translate = _pick("  请选择 [1]: ",
                                ["要，翻译成%s（消耗 API 额度）" % ns.target,
                                 "不用，字幕已经是中文了，直接拿来用"],
                                default=2 if ns.no_translate else 1) == 2
        if not ns.no_translate:
            key, key_src = ensure_key(ns, key, key_src)

        log("  输出模式（决定最后生成什么文件）：")
        stem = os.path.splitext(os.path.basename(video))[0]
        mode = _pick("  请选择 [1]: ",
                     ["软字幕嵌回  → %s.zh.mkv" % stem,
                      "  字幕是独立轨道，播放器里能开关、能换字体；视频音频原样复制，画质不变、几秒钟完事",
                      "烧录硬字幕  → %s.zh.hardsub.mp4" % stem,
                      "  字幕印死在画面上，手机/电视/U盘插哪都能看；要重新编码，10 分钟视频大概几分钟到几十分钟",
                      "只导出字幕  → %s.zh.srt" % stem,
                      "  不碰视频，只给你一个中文字幕文件，自己拿去压片或换播放器"],
                     default=3 if ns.srt_only else (2 if ns.burn else 1),
                     group=2)
        ns.burn, ns.srt_only = (mode == 2), (mode == 3)
        if mode == 1:
            log("  说明：视频和音频不重编码（无损、快），中文字幕作为一条新轨道放进 mkv。")
            if os.path.splitext(video)[1].lower() in (".mp4", ".mov", ".m4v", ".ts"):
                log("  提示：mp4 装不了 SRT 软字幕，所以会输出 .mkv —— 画面和声音完全不变，只是换个容器。")
        elif mode == 2:
            log("  说明：字幕焊进画面，要用 %s 重新编码视频（CRF %d，越大越糊越小）。"
                % (ns.vcodec, ns.crf))
        else:
            log("  说明：只写字幕文件，视频文件保持原样、不做任何处理。")

        if ns.burn and _ask("  换编码器吗（显卡加速能快很多）？[y/N]: ").lower() in ("y", "yes"):
            vc = _pick("  编码器 [1]: ",
                       ["libx264（CPU，兼容性最好）", "h264_nvenc（NVIDIA 显卡）",
                        "h264_qsv（Intel 核显）", "h264_amf（AMD 显卡）"])
            ns.vcodec = ["libx264", "h264_nvenc", "h264_qsv", "h264_amf"][vc - 1]
            if ns.vcodec != "libx264":
                ns.preset = "p4" if ns.vcodec == "h264_nvenc" else "medium"

        out = ask_output(ns, video)
        log("-" * 64)
        log("  视频    %s" % os.path.basename(video))
        log("  输出    %s" % out)
        log("  模式    %s" % ("只导出字幕文件" if ns.srt_only
                              else ("烧录硬字幕" if ns.burn else "软字幕嵌回")))
        log("  翻译    %s" % ("跳过（用原字幕）" if ns.no_translate else "翻译成 " + ns.target))
        if _ask("  开始？[Y/n]: ", "y").lower() in ("n", "no"):
            raise SystemExit("已取消。")

        process(ns, video, key, key_src, info, show_info=False)
        if _ask("\n  还要处理下一个视频吗？[y/N]: ").lower() not in ("y", "yes"):
            log("  再见。")
            return 0


# ---------------------------------------------------------------- 自检

def self_test():
    sample = ("\ufeff1\r\n00:00:01,000 --> 00:00:03,000\r\n<i>Hello</i>\r\nworld\r\n\r\n"
              "2\n00:00:04,000 --> 00:00:05,500\n{\\an8}Bye\\N now\n\n")
    cues = parse_srt(sample)
    assert len(cues) == 2, cues
    assert cues[0]["start"] == "00:00:01,000" and cues[0]["end"] == "00:00:03,000"
    assert cues[0]["text"] == "Hello world", cues[0]["text"]
    assert cues[1]["text"] == "Bye now", cues[1]["text"]

    srt = format_srt(cues, ["你好", ""])
    assert srt.startswith("1\n00:00:01,000 --> 00:00:03,000\n你好"), srt
    assert srt.rstrip().endswith("Bye now"), srt

    assert looks_chinese([{"text": "你好世界"}]) is True
    assert looks_chinese([{"text": "hello"}] * 8 + [{"text": "你好"}] * 2) is False  # 20% < 30%
    assert looks_chinese([{"text": "hello"}] * 6 + [{"text": "你好"}] * 4) is True   # 40% >= 30%
    assert [len(b) for b in chunks([(i, "x" * 10) for i in range(5)], 2, 1000)] == [2, 2, 1]
    assert [len(b) for b in chunks([(i, "x" * 10) for i in range(5)], 20, 25)] == [2, 2, 1]

    assert parse_json_obj('```json\n{"0": "你好"}\n```') == {"0": "你好"}
    assert parse_json_obj('好的：{"3":"再见"}') == {"3": "再见"}

    ss = [{"index": 0, "codec_name": "subrip", "tags": {"language": "chi"}},
          {"index": 1, "codec_name": "ass", "tags": {"language": "eng"}}]
    assert pick_sub(ss)[0] == 1 and pick_sub(ss, 0)[0] == 0
    assert pick_sub([{"index": 0, "codec_name": "hdmv_pgs_subtitle", "tags": {}}])[0] is None
    try:
        pick_sub([{"index": 0, "codec_name": "hdmv_pgs_subtitle", "tags": {}}], 0)
        raise AssertionError("图形字幕应当报错")
    except SystemExit:
        pass

    assert mask("sk-1234567890abcdef") == "sk-…cdef"

    tmpd = tempfile.mkdtemp(prefix="dsub-selftest-")
    try:                                   # 输出副本命名
        p = os.path.join(tmpd, "a.mkv")
        assert unique_path(p) == p
        open(p, "w").close()
        assert unique_path(p) == os.path.join(tmpd, "a(1).mkv")
        open(os.path.join(tmpd, "a(1).mkv"), "w").close()
        assert unique_path(p) == os.path.join(tmpd, "a(2).mkv")
        nsx = argparse.Namespace(overwrite=False, auto_rename=True, burn=False)
        assert check_output(p, os.path.join(tmpd, "no-such-video.mkv"), nsx) == \
            os.path.join(tmpd, "a(2).mkv")
        nsx.auto_rename = False
        try:
            check_output(p, os.path.join(tmpd, "no-such-video.mkv"), nsx)
            raise AssertionError("已存在且没表态时应当报错")
        except SystemExit as e:
            assert "--auto-rename" in str(e), str(e)
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)
    assert cache_key("简体中文", "hello") == cache_key("简体中文", "hello")
    assert cache_key("简体中文", "hello") != cache_key("繁體中文", "hello")
    assert cache_key("简体中文", "hello") != cache_key("简体中文", "hello!")

    assert fmt_size(0) == "0.00 B" and fmt_size(1536) == "1.50 KB"
    assert fmt_time(95) == "01:35" and fmt_time(3725) == "1:02:05"
    assert bar_text(0) == ">" + "-" * 23 and bar_text(1) == "=" * 24
    assert bar_text(0.5) == "=" * 12 + ">" + "-" * 11

    info = {"streams": [{"codec_type": "video", "codec_name": "h264"},
                        {"codec_type": "audio"},
                        {"codec_type": "subtitle", "codec_name": "subrip"},
                        {"codec_type": "subtitle", "codec_name": "ass"}]}
    assert [s["codec_name"] for s in sub_streams(info)] == ["subrip", "ass"]
    assert with_progress(["ffmpeg", "out.mkv"])[-1] == "out.mkv"
    assert "-progress" in with_progress(["ffmpeg", "-i", "a", "-c", "copy", "out.mkv"])

    if sys.platform == "win32":     # 密钥加密必须能原样还原
        blob = dpapi(b"sk-test-1234")
        assert blob and blob != b"sk-test-1234"
        assert dpapi(blob, unprotect=True) == b"sk-test-1234"

    m = mux_cmd("v.mkv", "z.srt", "o.mkv", 2)
    assert m[-1] == "o.mkv" and m.count("-metadata:s:s:2") == 2, m
    assert "-disposition:s:2" in m and "-disposition:s:0" in m
    assert m[:3] == [FFMPEG, "-y", "-i"] and "-map" in m
    maps = [m[i + 1] for i, x in enumerate(m[:-1]) if x == "-map"]
    assert maps == ["0:v?", "0:a?", "0:s?", "0:t?", "1:0"], maps   # 没有裸 -map 0：数据流会被 matroska 拒收

    assert _w("abc") == 3 and _w("中文") == 4       # 全角按两格算，进度条才不会留残影
    assert _w("[==>] 100%") == 10

    nsx = argparse.Namespace(vcodec="libx264", crf=20, preset="medium",
                             force_style="FontName=Microsoft YaHei,Outline=1")
    b = burn_cmd("v.mkv", "zh.srt", "o.mp4", nsx)
    vf = b[b.index("-vf") + 1]
    assert vf == "subtitles=zh.srt:force_style=FontName=Microsoft YaHei\\,Outline=1", vf
    assert esc_filter_path("D:\\a b\\zh.srt") == "D\\:/a b/zh.srt"
    assert "-b:a" in burn_cmd("v.mkv", "zh.srt", "o.mp4", nsx, "aac")

    log("self-test OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
