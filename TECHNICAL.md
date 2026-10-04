# dsub 技术文档

面向要读代码、改代码、或者想知道「为什么这么写」的人。**使用说明看 [README](README.md)**，这里讲实现。

文中行号对应 `dsub.py` 的 `v1.0.1`（1340 行）。

---

## 1. 概览

### 1.1 它做什么

一条流水线，四个阶段：

```
   视频文件
      │  ffprobe -show_format -show_streams（一次调用拿全部信息）
      ▼
   视频信息 ──► 信息面板（时长/编码/音轨/字幕轨）
      │  choose_source：--srt-in > 内嵌字幕轨 > 同名外挂字幕
      ▼
   源字幕 ──ffmpeg -c:s srt──► src.srt
      │  parse_srt
      ▼
   cues[{start, end, text}]
      │  confirm_chinese：看起来已经是中文就跳过翻译
      ▼
   translate_all ──按句查缓存──► 命中直接复用
      │                      └─► POST /chat/completions（键对齐）
      ▼
   译文列表 ──format_srt──► zh.srt
      │
      ├─ 软字幕嵌回： ffmpeg -c copy -map 1:0        ──► 视频.zh.mkv
      ├─ 烧录硬字幕： ffmpeg -vf subtitles=zh.srt    ──► 视频.zh.hardsub.mp4
      └─ 只导出字幕： 复制 zh.srt                     ──► 视频.zh.srt
```

### 1.2 技术选型

| 决定 | 理由 |
| --- | --- |
| **单文件** | 整个工具 1340 行，拆包只会增加 import 开销和「改一个功能要开三个文件」的成本。复制一个 `.py` 就是安装。 |
| **只用标准库** | 依赖越多，用户的安装门槛越高。`urllib.request` 够调 REST，`subprocess` 够调 ffmpeg，`ctypes` 够调 DPAPI——没有理由引入第三方。 |
| **Python 而不是 PowerShell** | 字幕解析、JSON 键对齐、递归拆批、DPAPI 结构体，用 PowerShell 写会痛苦十倍。 |
| **ffmpeg 走命令行而不是 `ffmpeg-python`** | 命令行参数是 ffmpeg 的官方接口，出错时能直接复制粘贴到终端复现。 |
| **要翻译的字幕一律转成 SRT 处理** | SRT 是最简单的文本字幕格式，解析器 20 行搞定。ASS/VTT 交给 ffmpeg 转，不自己写解析器。 |
| **不用 asyncio / 多线程** | 瓶颈是 API 往返（每批几百毫秒）和编码（CPU 早就吃满了）。并发只会让限速处理和进度显示变复杂。 |

---

## 2. 代码结构

### 2.1 分区

`dsub.py` 用 `# ---- 标题` 注释分了 13 个区，从上到下：

| 行 | 分区 | 职责 |
| --- | --- | --- |
| 52 | 常量 | 扩展名白名单、配置文件路径、CJK 正则 |
| 62 | 基础工具 | 日志、ffmpeg 定位、`run()` |
| 181 | 界面：信息 / 进度 | `probe`、`print_info`、进度条、`run_progress` |
| 345 | 密钥管理 | DPAPI 加解密、保存/读取/删除 |
| 414 | 翻译缓存 | 内容寻址缓存 |
| 456 | DeepSeek 调用 | `api_call`、`chat`、`verify_key` |
| 516 | ffprobe / 选源 | 选字幕轨、找外挂字幕 |
| 578 | SRT 解析 | `parse_srt` / `format_srt` |
| 634 | DeepSeek 翻译 | `translate_items`、`translate_all` |
| 715 | ffmpeg 命令 | `mux_cmd`、`burn_cmd`（纯函数，好测） |
| 749 | 防呆检查 | `check_input`、`check_output`、`confirm_chinese` |
| 818 | 主流程 | `build_parser`、`main`、`dispatch`、`process` |
| 1022 | 交互向导 | `_ask`、`_pick`、`wizard` |
| 1240 | 自检 | `self_test` |

### 2.2 三个入口

```
main(argv)
 ├─ setup_log()                    写日志文件头
 ├─ dispatch(ns, ap)
 │   ├─ --self-test / --set-key / --forget-key / --clear-cache   子命令，直接返回
 │   ├─ 无 video 参数  → wizard(ns)      交互向导
 │   └─ 有 video 参数  → process(...)    一次性执行
 └─ except SystemExit / KeyboardInterrupt → 把错误写进日志
```

`wizard()` 和命令行路径**共用同一个 `process()`**，不存在两套流程。向导只负责把答案填进 `ns`（`argparse.Namespace`），然后调用 `process(ns, video, key, key_src, info, show_info=False)`——`show_info=False` 是因为向导已经打印过信息面板了。

---

## 3. 主流程 `process()`（908 行）

```
check_input  →  check_tools  →  probe  →  print_info
   →  解析输出路径 + check_output
   →  choose_source
   →  打印「任务」块
   →  mkdtemp 工作目录
   →  抽字幕 → parse_srt → confirm_chinese
   →  translate_all（或直接沿用原文）
   →  format_srt → 写 zh.srt
   →  软封装 / 烧录 / 只导出
   →  finally: 删工作目录
```

### 3.1 输出路径的推导

```python
stem, outdir = splitext(basename(video))[0], dirname(video)
```

| 模式 | 默认输出 | 附带产物 |
| --- | --- | --- |
| 软字幕嵌回 | `<stem>.zh.mkv` | `<stem>.zh.srt`（除非 `--no-srt`） |
| 烧录硬字幕 | `<stem>.zh.hardsub.mp4` | 同上 |
| 只导出字幕 | `<stem>.zh.srt` | —— |

`--srt-only` 时如果 `-o` 给的扩展名不是 `.srt/.ass/.vtt`，会**替换**成 `.srt`（`x.mkv` → `x.srt`，不是 `x.mkv.srt`）。

关键一点：`check_output()` **返回最终路径**，因为开了 `--auto-rename` 时它可能改名。调用方必须接住返回值：

```python
out = check_output(out, video, ns)      # 可能是 out(1).mkv
video_out = out
srt_out = os.path.splitext(out)[0] + ".srt"   # 字幕跟着一起改名
```

### 3.2 「任务」块与数据流提示

打印前会挑出源文件里既不是视频/音频/字幕/附件、又确实存在的流：

```python
dropped = [s for s in info["streams"]
           if s.get("codec_type") not in ("video", "audio", "subtitle", "attachment")]
```

有就提示一句「mkv 装不下，已忽略」。这不是纯粹的信息展示——见 §9.1。

---

## 4. 字幕来源

### 4.1 优先级

```
--srt-in 指定文件
   ↓ 没有则
视频内嵌字幕轨（自动挑非中文轨）
   ↓ 没有可用文本轨则
视频同名外挂字幕（<stem>.srt / .ass / .ssa / .vtt / .sub）
   ↓ 都没有
报错退出
```

`choose_source()`（550 行）实现这段逻辑。注意一个分支：**所有内嵌轨都是图形字幕但旁边有外挂字幕时，会降级到外挂字幕**而不是直接报错。

### 4.2 选轨策略 `pick_sub()`（522 行）

```python
ZH_LANGS = {"chi","zho","zh","cmn","chs","cht","zh-cn","zh-hans","zh-tw"}
BITMAP_CODECS = {"hdmv_pgs_subtitle","dvd_subtitle","dvb_subtitle","xsub"}
```

1. 指定了 `--stream N` → 用它，是图形字幕就报错（要 OCR，本工具不做）
2. 否则第一遍：跳过图形字幕，且 `language` 不在 `ZH_LANGS` 里的第一条
3. 第二遍：只要是文本字幕就要（全中文时至少能配合 `--no-translate` 用）
4. 全图形 → 返回 `(None, None)`，交给上层决定降级还是报错

`--stream N` 里的 N 就是 ffmpeg 的 `0:s:N`。因为 `sub_streams()` 是按文件顺序过滤 `codec_type == "subtitle"`，和 `ffmpeg -select_streams s` 的顺序一致，所以 N 可以直接透传给 `-map 0:s:N`。

### 4.3 抽成 SRT 的三条路径

| 来源 | 做法 |
| --- | --- |
| 内嵌轨 | `ffmpeg -i 视频 -map 0:s:N -c:s srt src.srt` |
| `.srt` 外挂 | `shutil.copyfile`（不过 ffmpeg，避免它改动格式） |
| 其它外挂 | `ffmpeg -i 字幕 -c:s srt src.srt`（ass/ssa/vtt 都走这条） |

---

## 5. SRT 解析与生成

### 5.1 `parse_srt()`（590 行）

不写状态机，直接按空行切块再找时间轴那一行：

```python
text = text.replace("\r\n","\n").replace("\r","\n").lstrip("\ufeff")   # 兼容 CRLF 和 BOM
for block in re.split(r"\n{2,}", text.strip()):
    ti = 第一个含 "-->" 的行
    m  = TIME_RE.search(该行)      # (\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}) --> ...
    body = clean_text(" ".join(时间轴之后的行))
```

为什么容忍 `[,.]`：SRT 规范用逗号，VTT 用点，同一份文件里混用也不罕见。

`clean_text()` 做三件事：

- `\N` `\n` `\h`（ASS 换行/硬空格）→ 空格
- 删掉 `{...}`（ASS 覆盖指令）和 `<...>`（HTML/字体标签）
- 连续空白压成一个空格

**代价**：ASS 的定位标签（`{\an8}` 顶部字幕、`{\pos()}`）会被丢掉，译文一律按默认位置渲染。这是「翻译 + 回嵌」的固有损失，README 里也写了。

### 5.2 为什么多行合并成一行

一条字幕拆成两行（`Hello` / `world`）时，模型很容易把它们当成两条来翻译。合并成一句再送，返回一条，语义完整。

### 5.3 键对齐而不是行对齐

发给模型的是**对象**不是数组：

```json
{"0": "Hello there", "1": "Second line"}
```

要求返回同键的对象 `{"0": "你好", "1": "第二行"}`。理由：

- 数组靠顺序对齐，模型多返回/漏返回一条，后面全部错位
- 对象靠键对齐，即使漏了几条，**其余条目依然是对的**

### 5.4 `format_srt()`（606 行）的兜底

```python
t = (translations[i] or "").strip() or c["text"]
```

译不出或译成空 → 回落原文。所以产物永远是完整条数，不会出现空字幕行。

---

## 6. DeepSeek 调用层

### 6.1 请求

```python
POST {base_url}/chat/completions
{
  "model": ns.model,                     # 默认 deepseek-chat
  "messages": [system, user],
  "temperature": 1.3,                    # DeepSeek 官方给翻译场景的建议值
  "response_format": {"type": "json_object"},
  "stream": False
}
```

system 提示词（`translate_items`）：

> 你是字幕翻译。把用户给出的 JSON 里每个值翻译成{目标语言}，键保持不变。只输出 JSON 对象，不要解释、不要加注释。译文口语化、简洁，人名与专有名词保留原文，不要合并或拆分条目。

`temperature=1.3` 是官方翻译建议值；`json_object` 要求提示词里出现 "json" 字样，上面那句已包含。

### 6.2 重试 `chat()`（636 行）

| 情况 | 处理 |
| --- | --- |
| 429 / 500 / 502 / 503 / 504 | 退避重试，最多 4 次，间隔 2/4/6/8 秒 |
| 其它 HTTP 错误（401/402/...） | **立即** `SystemExit`，不浪费时间重试 |
| 网络异常 / 超时 | 同退避重试 |
| 4 次都失败 | 抛错退出 |

错误码到人话的映射在 `api_error_hint()`（477 行）：

- 401 → Key 无效，提示 `--set-key` 重新保存
- 402 → 余额不足
- 429 → 限速，建议调小 `--batch`

### 6.3 拆批重试 `translate_items()`（663 行）

```
发送一批 → 解析 JSON 对象 → 取出键命中的条目
  ├─ 全部命中 → 返回
  ├─ 只有 1 条还失败 → 放弃（返回空，该条回落原文）
  └─ 否则 → 对半拆成两批，各自递归
```

这里有两个刻意的设计：

- **只 catch `ValueError`**（JSON 解析失败）。`chat()` 抛出的 `SystemExit` 会直接穿透——API Key 错了就整体停下，而不是傻乎乎地把一批拆成 128 次请求。
- 递归深度是 log₂(批大小)，最坏情况一批 20 条拆成 20 次单条请求，但只在模型反复输出非法 JSON 时才会发生。

### 6.4 批大小 `chunks()`（621 行）

```python
chunks(todo, ns.batch, 2000)     # 每批最多 batch 条，且总字符数不超过 2000
```

两个上限取先到者：`--batch`（默认 20）和 2000 字符。字符数按 `len(str)` 算（字符数，非字节数），所以 2000 个汉字的一批比 2000 个 ASCII 字母的批次更"重"——但 DeepSeek 的上下文足够，这不是瓶颈。

---

## 7. 翻译缓存

### 7.1 内容寻址

```python
def cache_key(target, text):
    return hashlib.sha1((target + "\x00" + text).encode("utf-8")).hexdigest()
```

- 键 = `SHA1(目标语言 + NUL + 原文)`，值 = 译文
- **目标语言参与哈希**，所以简体/繁体各存各的，不会互相污染
- 存的是**句子**不是文件，所以：
  - 同一个文件重跑 → 全部命中，零请求
  - 相似文件（同系列剧、重复片头尾）→ 重复句子自动命中
  - 字幕改了一行 → 只有那一行重新翻

### 7.2 落盘时机与原子性

每批翻译完就 `save_cache()` 一次：

```python
tmp = CACHE_FILE + ".tmp"
json.dump(cache, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
os.replace(tmp, CACHE_FILE)          # 原子替换，断电也不会写出半个 JSON
```

这就是**中断恢复**的全部实现：Ctrl+C 后缓存里有几批就是几批，重跑接着来。早期版本用一个 `<输出名>.cache.json` 做恢复，引入内容寻址缓存后那个文件被彻底删掉了——同一件事不需要两个机制。

### 7.3 上限与剪枝

```python
MAX_CACHE = 200000
if len(cache) > MAX_CACHE:
    cache = dict(list(cache.items())[-MAX_CACHE:])
```

利用了 Python 3.7+ **dict 保留插入顺序**这一特性：直接砍掉最旧的，不需要额外的 LRU 链表。200000 条 × 约 100 字节 ≈ 20 MB 上限。

> 代码里留了 `ponytail:` 注释说明这个取舍：按插入顺序丢，不是按使用频率丢。真出现"常用的老句子被挤掉"再上 LRU 不迟。

### 7.4 缓存损坏处理

`load_cache()` 里 JSON 解析失败不会崩，只是打一句警告然后当空缓存重建。

---

## 8. 密钥与安全

### 8.1 三个来源

```
--api-key 参数  >  环境变量 DEEPSEEK_API_KEY  >  ~/.dsub/key.dpapi
```

`resolve_key()`（456 行）返回 `(key, 描述)`，描述是给日志和界面看的（形如「已保存的密钥 sk-…cdef」），**永远不含完整密钥**。

### 8.2 DPAPI 加密

Windows 数据保护 API，密钥与**当前 Windows 账户**绑定：

```python
class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]

crypt32.CryptProtectData(byref(blob_in), None, None, None, None, 0, byref(blob_out))
crypt32.CryptUnprotectData(...)        # 第二个参数是描述串，传 None
kernel32.LocalFree(cast(blob_out.pbData, c_void_p))    # 必须释放
```

几个容易踩的点，代码里都处理了：

- **入口标志**：写入的密文是 `01 00 00 00 D0 8C 9D DF ...`，D 开头是 DPAPI 的 blob 头
- **`LocalFree` 必须设 argtypes**：不设 `argtypes = [c_void_p]`，64 位下 ctypes 会按 C int 截断指针，直接崩
- **输入 buffer 要保持引用**：`create_string_buffer` 的结果在调用期间不能被 GC
- **非 Windows 降级**：`sys.platform != "win32"` 时 `dpapi()` 返回 `None`，退化成明文存 `key.txt`（chmod 600）并明确警告

效果：换 Windows 账户、把文件拷到别的机器，都解不开。

### 8.3 先验证后落盘

`set_key_cmd()`（498 行）的流程是「读取 → 验证 → 保存」，不是「保存 → 验证」：

```
getpass 输入（不回显）
  → 前缀不是 sk- 只警告不阻止（不排除将来换格式）
  → verify_key()：真发一次 max_tokens=1 的请求
  → 通过才 save_key()
```

实测：拿无效 Key 跑 `--set-key`，返回 401 并提示，`~/.dsub` 目录都不会被创建。

### 8.4 日志脱敏

```python
def redact(text):
    return re.sub(r"(--api-key[\s=]+)\S+", r"\1***", text)
```

日志头部的「命令行」那一行会过一遍 `redact()`，`--api-key sk-xxx` 变成 `--api-key ***`。环境变量里的 Key 从来不打印。

---

## 9. ffmpeg 命令构造

所有 ffmpeg 命令都是**纯函数**拼出来的（`mux_cmd` / `burn_cmd`），参数是 list 不是字符串——不经 shell，路径里的空格和撇号天然安全，可测性也好（`self_test` 直接断言参数表）。

### 9.1 `SAFE_MAPS`：一次真实故障换来的

```python
SAFE_MAPS = ["-map","0:v?","-map","0:a?","-map","0:s?","-map","0:t?"]
```

早期版本用 `-map 0` 把所有流都搬走。某个用户的 mp4 里有一条 **`tmcd` 时间码数据流**，matroska 明确不收数据流：

```
[matroska] Only audio, video, and subtitles are supported for Matroska.
[out#0/matroska] Could not write header (incorrect codec parameters ?): Invalid argument
```

整个封装直接失败，而且报错看起来像字幕的问题，实际跟字幕无关。

现在的规则：**只搬 mkv/mp4 都认的四类流**——视频、音频、字幕、附件（字体）。`?` 表示该类型不存在时不报错。数据流被丢掉，并在「任务」块里提示一次。

### 9.2 软字幕嵌回 `mux_cmd()`

```
ffmpeg -y -i 视频 -i zh.srt
       -map 0:v? -map 0:a? -map 0:s? -map 0:t? -map 1:0
       -map_metadata 0 -map_chapters 0 -c copy
       -disposition:s:0 0  ...                       # 原字幕轨取消默认
       -disposition:s:N default                      # 中文字幕设为默认
       -metadata:s:s:N language=chi -metadata:s:s:N title=中文
       输出.mkv
```

**N 的推导**：输出流顺序 = `0:v? 0:a? 0:s? 0:t? 1:0`，所以中文字幕在所有字幕流里的序号 = **源文件字幕轨的数量**。这就是 `process()` 要把 `len(streams)` 传给 `mux_cmd()` 的原因（`n_embed` 变量）。

`-c copy` 意味着视频音频**不重编码**——10 分钟的 4K 片子几秒钟就完成。

失败兜底：`-c copy` 报错时改用 `-c:s srt`（把原字幕统一转成 SRT）重试一次。第一次失败的完整报错会写进日志（`logf`），控制台只留一句结论。

### 9.3 烧录 `burn_cmd()`

```
ffmpeg -y -i 视频 -map 0:v:0 -map 0:a?
       -vf "subtitles=zh.srt:force_style=FontName=Microsoft YaHei\,Outline=1\,MarginV=24"
       -c:v libx264 -crf 20 -preset medium -c:a copy
       输出.mp4
```

三个细节：

**① 用 `cwd` 而不是绝对路径**

`subtitles` 滤镜的路径转义是 Windows 上的经典坑（盘符的 `:` 和反斜杠都要转义，`[` `]` 还是滤镜语法字符）。这里的做法是：把字幕文件放在临时工作目录，**让 ffmpeg 在该目录下执行，滤镜里只写文件名**：

```python
run_progress(burn_cmd(video, "zh.srt", out, ns), cwd=work)
```

`esc_filter_path()` 仍然存在，处理文件名里可能出现逗号/引号的情况，但常规情况下它什么都不用做。

**② `force_style` 的逗号必须转义**

```python
vf += ":force_style=" + ns.force_style.replace(",", "\\,")
```

`--force-style "FontName=Microsoft YaHei,Outline=1"` 会被转成 `FontName=Microsoft YaHei\,Outline=1`，否则 ffmpeg 会把逗号当成滤镜参数分隔符。

**③ 音频兜底**

`-c:a copy` 优先（不重编码）。某些音频编码装不进 mp4 时会失败，此时自动改用 `-c:a aac -b:a 192k` 重试。

### 9.4 进度解析用的是 `out_time`

```python
with_progress(cmd)   # 在输出文件前插入 -nostats -progress pipe:1 -loglevel error
```

读取 `pipe:1` 的每一行，取 `out_time=HH:MM:SS.uuuuuu` 这个字段。**刻意不用 `out_time_ms`**：ffmpeg 6 之前那个字段的单位其实是微秒（历史 bug），用它会让进度条快 1000 倍或慢 1000 倍。`out_time` 是格式化时间串，各版本一致，解析成秒数无误。

`-loglevel error` 让 stderr 保持安静（stderr 重定向到临时文件，出错时读出来），`-nostats` 关掉 ffmpeg 自己的进度行，否则会和我们的进度条打架。

---

## 10. 交互层

### 10.1 信息面板 `print_info()`

一次 `ffprobe -show_format -show_streams` 拿全部信息，然后格式化输出：文件大小、封装格式、总码率、时长、视频编码/分辨率/帧率、音轨编码/采样率/声道/语言、每条字幕轨的序号和语言。

没有视频流时（比如误把音频文件拖进来）直接报错退出。

### 10.2 进度条

```python
def _w(s):      # 显示宽度
    return sum(2 if unicodedata.east_asian_width(c) in ("W","F") else 1 for c in s)
```

**为什么不能按 `len()` 算**：中文占两个字符格。按字符数补空格会少补，上一次进度条的尾巴擦不掉——实测残影长这样：

```
  翻译    完成 172 条（复用 0，新译 172），用时 00:12                  00:00
                                                                    ^^^^^ 上一帧的「剩余 00:00」
```

刷新逻辑就三行：`\r` + 整行 + 补空格到上一帧宽度。

**非 tty 降级**：`sys.stdout.isatty()` 为假（重定向到文件、IDE、CI）时不画进度条，改成每个批次打一行普通文字——不会把 `\r` 塞进日志。

### 10.3 日志

```
~/.dsub/logs/dsub-YYYYMMDD.log     默认，同一天累加
--log FILE                          自定义路径
--no-log                            完全关闭
```

- `log()` → 控制台 + 文件（带 `[HH:MM:SS]` 前缀）
- `logf()` → **只写文件**。用来放 ffmpeg 的完整报错：控制台留一句「直接封装失败，改为…重试（详细报错见日志）」，细节进日志
- `prune_logs()` → 只保留最近 `MAX_LOGS = 10` 份（按 mtime 排序）
- 启动时写入版本、时间、脱敏后的命令行、Python 版本——排查问题时这几行信息量最大

### 10.4 向导状态机 `wizard()`（1160 行）

每轮循环的提问顺序（**顺序固定，输入是按序消费的**）：

| # | 问题 | 备注 |
| --- | --- | --- |
| 1 | 视频文件路径 | 循环直到文件存在；支持拖拽进来的引号/`&` |
| 2 | （打印信息面板） | |
| 3 | 字幕来源 | 内嵌轨/外挂/手动指定，选项数 ≥1 时行为不同 |
| 4 | 要翻译吗 | 选「不用」则跳过第 5 步 |
| 5 | API Key | 只有需要翻译且没有 Key 时才问 |
| 6 | 输出模式 | 软字幕 / 烧录 / 只导出，每项带后果说明 |
| 7 | 换编码器吗 | 只在烧录时问 |
| 8 | 输出文件 | 已存在时三选一：覆盖 / 副本 / 换名 |
| 9 | 开始？ | |
| 10 | 还要处理下一个？ | 是则回到第 1 步，`ns` 复用（Key 和上次的选择会保留） |

**Key 的索取被刻意推迟到第 5 步**：如果用户只是想把已有的中文字幕烧进画面，全程不会看到任何跟 API Key 相关的提示。第 10 步复用 `ns` 则保证同一会话处理第二个视频时不再问 Key。

`clean_path()` 处理拖拽输入：去掉成对引号、PowerShell 的 `&` 前缀、首尾空格。

`_pick(prompt, options, default, group)` 的 `group` 参数支持「选项 + 说明行」的混合列表（`group=N` 表示每 N 项一组），所以输出模式的三行说明不会被当成可选项。

---

## 11. 防呆与错误处理

### 11.1 拦截清单

| 情况 | 处理 |
| --- | --- |
| ffmpeg / ffprobe 不存在或跑不起来 | 报错并说明怎么装、怎么用环境变量指定 |
| 输入不存在 / < 1KB / 扩展名可疑 | 报错；扩展名可疑只警告 |
| 输出会覆盖源视频 | 拒绝 |
| 输出已存在 | 拒绝，提示 `--overwrite` 或 `--auto-rename` |
| `--srt-only` 输出成 `.mp4`（软字幕） | 拒绝，提示改 `.mkv` 或 `--burn` |
| 磁盘空间不足 | 按输入体积估算（烧录 ×2、封装 ×1.2）后拒绝 |
| 字幕疑似已是中文 | 默认改用原字幕；非交互环境要求显式表态 |
| 需要翻译但没有 Key | 提示 `--set-key`，并告知 `--no-translate` 可免 Key |
| 图形字幕（PGS/DVD） | 明确说要 OCR，本工具不做 |
| 既无内嵌字幕也无外挂 | 提示用 `--srt-in` |
| `--srt-only` + `--no-srt` | argparse 直接报参数冲突 |
| `--overwrite` + `--auto-rename` | 互斥组，argparse 报错 |

### 11.2 退出码

| 码 | 场景 |
| --- | --- |
| 0 | 成功 |
| 1 | `SystemExit("中文错误信息")`——所有业务错误 |
| 2 | argparse 参数错误（互斥参数、未知参数等） |
| `0xC000013A` | 未捕获的 Ctrl+C（Windows 的 `STATUS_CONTROL_C_EXIT`） |

关于 Ctrl+C 的实际行为，分两个阶段：

- **执行阶段**（抽字幕 / 翻译 / 封装 / 烧录）：`process()` 里 `except KeyboardInterrupt` 接住，打印「已中断。已译好的句子存在 &lt;缓存路径&gt;，重跑同一条命令会接着翻」，然后转成 `SystemExit` → 退出码 1。
- **提问阶段**（向导里的 `input()`）：`main()` 的 `except KeyboardInterrupt` 只写日志然后 **重新抛出**，没有顶层兜底，所以会由 Python 打印 `KeyboardInterrupt` 回溯。这是已知的粗糙处，见 §14。

### 11.3 「疑似已是中文」的判定 `confirm_chinese()`

```python
looks_chinese(cues):   # 取前 100 条非空字幕
    CJK 命中比例 >= 0.3
```

命中后的行为分三种：

- 已有 `--no-translate` 或 `--yes` → 直接按用户说的来
- 交互终端 → 问「直接使用这份字幕（不翻译）？[Y/n]」，**回车即默认不翻译**（省钱优先）
- 非交互 → 报错，明确列出两个选项

### 11.4 输出副本命名 `unique_path()`

```
视频.zh.mkv → 视频.zh(1).mkv → 视频.zh(2).mkv → ...
```

插在扩展名之前。n 到 999 都占用了就退回时间戳后缀（`视频.zh(20261004103000).mkv`），不会死循环。

---

## 12. 测试

### 12.1 `--self-test`

不联网、不调 ffmpeg，纯断言，`python dsub.py --self-test` 一秒出结果。覆盖：

- SRT 解析：BOM、CRLF、多行合并、ASS/HTML 标签清洗、时间轴提取
- `format_srt` 的原文回落
- `looks_chinese` 的阈值边界（20% / 40%）
- `chunks` 的条目数和字符数两个上限
- `parse_json_obj`：` ```json ` 包裹、前后有多余文字
- `pick_sub`：中文轨跳过、指定序号、图形字幕
- `mask`、`cache_key`（确定性 + 目标语言隔离）
- `fmt_size` / `fmt_time` / `bar_text` / `_w`（含全角宽度）
- `mux_cmd` 的映射表**精确等于** `["0:v?","0:a?","0:s?","0:t?","1:0"]`（防止 `-map 0` 回归）
- `burn_cmd` 的 `force_style` 转义、AAC 兜底的 `-b:a`
- `esc_filter_path` 的 Windows 路径转义
- `unique_path` / `check_output` 的改名与拒绝逻辑（用临时目录）
- DPAPI 加解密往返（仅 Windows）

### 12.2 开发期的端到端验证

`--self-test` 只覆盖纯函数。真正跑通需要三样东西，都不难造：

**① 带数据流的样片**（复现 §9.1 那类故障）

```powershell
ffmpeg -y -f lavfi -i "testsrc=size=320x240:rate=10" -f lavfi -i "sine=frequency=440" `
       -t 3 -c:v libx264 -preset ultrafast -c:a aac `
       -write_tmcd 1 -timecode 00:00:00:00 clip.mp4
ffprobe -v error -show_entries stream=codec_type -of csv=p=0 clip.mp4   # 应出现 data
```

**② 本地假 API 服务**（不花钱跑通翻译链路）

起一个 `http.server`，把 `/chat/completions` 的响应构造成 `{"choices":[{"message":{"content":"<键对齐的 JSON>"}}]}`，然后：

```powershell
dsub.exe clip.mp4 --srt-in en.srt --base-url http://127.0.0.1:PORT --api-key sk-test
```

这样能在**不消耗额度**的前提下验证 urllib 编码、JSON 对齐、缓存写入、ffmpeg 封装整条链路。

**③ 无 Key 路径**

造一个带中文字幕轨的 mkv，`--no-translate --burn` 和 `--no-translate --srt-only` 全部不需要联网：

```powershell
ffmpeg -y -i clip.mp4 -i zh.srt -map 0:v -map 0:a -map 1:0 -c copy -c:s srt `
       -metadata:s:s:0 language=chi clip_zh.mkv
```

**注意测试脚本自己别踩 `-map 0`**——验证脚本里写 `-map 0` 会复现同样的封装修复失败，这个坑我踩过。

### 12.3 打包版的额外验证

exe 必须单独验证，冻结环境和源码运行有两处已知差异：

- **stdout 编码**（见 §13.2）——必须重定向到文件后 `hexdump` 确认中文字节是 UTF-8（`已` = `E5 B7 B2`）
- **DPAPI**——ctypes 在冻结环境下同样可用，`--set-key` 要实机跑一次

---

## 13. 构建与发布

### 13.1 打包 exe

```powershell
python -m pip install pyinstaller
python -m PyInstaller --onefile --clean --name dsub --distpath dist --workpath build --specpath build dsub.py
```

产物 `dist\dsub.exe` 约 9.5 MB（绝大部分是 Python 运行时）。`build/`、`dist/`、`*.spec` 都在 `.gitignore` 里——exe 作为 Release 附件分发，不进仓库。

### 13.2 冻结环境的 UTF-8 坑

**第一次打包出来的 exe 中文全是乱码。** 定位过程：

1. 让 exe 写日志文件（日志是显式 `encoding="utf-8"` 打开的）→ **文件里的中文是对的**，说明源码字符串没问题
2. 重定向 stdout 到文件再 `hexdump` → 字节里混着 `EF BF BD`（U+FFFD）
3. `python -c "import sys; print(sys.flags.utf8_mode)"` → **`1`**

结论：源码运行时有 UTF-8 模式兜底，而 **PyInstaller 冻结后不继承它**，stdout 退回系统 ANSI 代码页（cp936），中文就被破坏了。

修法是启动时显式声明，对源码运行无害（Windows 控制台本来就按 UTF-8 字节转宽字符）：

```python
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
```

### 13.3 版本号

唯一来源是 `dsub.py` 的 `__version__`，被两处消费：

- `--version` / `-V`（argparse 的 `action="version"`）
- 日志头部：`dsub 1.0.1 启动 2026-10-04 10:28:10`

**改版本号 = 改这一行 + 重新打包 + 重新打标签**，三者必须同步，否则会出现「exe 说 1.0.0、Release 叫 v1.0.1」的错位。

### 13.4 发布流程

```powershell
# 1. 改 __version__，重新打包，验证
python dsub.py --self-test
.\dist\dsub.exe --version

# 2. 提交 + 打标签
git add -A
git commit -m "版本号 1.0.2"
git tag -a v1.0.2 -m "dsub 1.0.2 - <一句话>"

# 3. 分别推（不要用 --tags：一个标签冲突会中断整个推送）
git push origin main
git push origin v1.0.2

# 4. GitHub → Releases → Draft a new release → 选标签 → 附上 dist\dsub.exe
```

**标签不要重打。** 本项目踩过一次：`v1.0.0` 推上去之后又本地重打指向了新提交，导致 `git push --tags` 被拒绝（`! [rejected] v1.0.0 (already exists)`），看起来像「推不上」，实际是标签冲突。宁可发 `v1.0.1`。

---

## 14. 已知限制与后续方向

### 有意不做

| 不做 | 原因 / 替代 |
| --- | --- |
| 语音识别（ASR） | 视频没字幕时需要 whisper 一类的工具先出字幕，再 `--srt-in` 进来 |
| 图形字幕 OCR | PGS/DVD 是图片，识别是另一个量级的工程 |
| ASS 定位标签保留 | 翻成中文后原文的行长/断句全变了，硬搬 `{\pos()}` 只会错位 |
| 并发请求 | 瓶颈是限速和进度显示的可读性；`--batch` 调大即可 |
| 自动下载字幕 | 字幕站点的接口不稳定，且涉及版权灰色地带 |

### 可以更好

- **向导里 Ctrl+C 的收尾**：`process()` 阶段的中断已被捕获并转成友好提示，但在提问阶段（`input()`）按 Ctrl+C 会由 Python 打印 `KeyboardInterrupt` 回溯。顶层加一个 `except KeyboardInterrupt` 转成干净退出即可，代价是重新打包。
- **缓存剪枝策略**：目前按插入顺序丢最旧的（§7.3），要按使用频率丢得引入 LRU 结构。
- **并发翻译**：批与批之间其实可以并发（限速允许的话），能显著缩短长片的翻译时间。
- **`--force-style` 的预设**：现在要用户自己写 libass 语法，可以内置「小字/大字/描边」几档。
- **exe 体积**：9.5 MB 里大部分用不上，`--exclude-module` 能压一些，但收益有限。

---

## 附录 A：文件与数据布局

```
仓库
├─ dsub.py        主程序
├─ dsub.cmd       Windows 启动器（%~dp0 转发，兼容没装 python 命令只有 py 的机器）
├─ README.md      使用说明
├─ TECHNICAL.md   本文档
├─ LICENSE        MIT
└─ .gitignore     忽略 __pycache__ / build / dist / 视频字幕

用户数据（%USERPROFILE%\.dsub\）
├─ key.dpapi          DPAPI 密文；换 Windows 账户解不开
├─ key.txt            仅非 Windows 平台降级时出现（明文 + chmod 600）
├─ trans_cache.json   翻译缓存，dict{sha1: 译文}
└─ logs\
   └─ dsub-YYYYMMDD.log   保留最近 10 份

运行时临时目录（%TEMP%\dsub-xxxx\，结束即删）
├─ src.srt    抽出来的源字幕
└─ zh.srt     翻译后的字幕（烧录时 ffmpeg 的 cwd 就在这里）
```

## 附录 B：参数与环境变量速查

| 参数 | 作用 |
| --- | --- |
| `-o/--output` | 输出路径（`--srt-only` 时是字幕路径） |
| `--srt-in FILE` | 指定外挂字幕 |
| `-s/--stream N` | 内嵌字幕轨序号 `0:s:N` |
| `--burn` | 烧录硬字幕 |
| `--srt-only` | 只导出字幕 |
| `--no-translate` | 不翻译，直接用原字幕 |
| `--target` | 目标语言，默认「简体中文」 |
| `--batch N` | 每批条数，默认 20 |
| `--no-srt` | 不保留翻译后的 srt |
| `--no-cache` | 不读不写翻译缓存 |
| `--clear-cache` | 清空缓存后退出 |
| `--overwrite` / `--auto-rename` | 输出已存在时：覆盖 / 出副本 |
| `-y/--yes` | 确认要翻译（字幕疑似已是中文时） |
| `--log FILE` / `--no-log` | 日志路径 / 关闭日志 |
| `--set-key` / `--forget-key` | 保存 / 删除 API Key |
| `--api-key` / `--base-url` / `--model` | 临时覆盖调用参数 |
| `--force-style` / `--vcodec` / `--crf` / `--preset` | 烧录相关 |
| `--self-test` / `-V/--version` | 自检 / 版本 |

| 环境变量 | 默认 |
| --- | --- |
| `DEEPSEEK_API_KEY` | 无（优先级高于已保存的 Key） |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` |
| `DEEPSEEK_MODEL` | `deepseek-chat` |
| `FFMPEG` | PATH 里的 `ffmpeg`（可以是目录） |
| `FFPROBE` | FFMPEG 同目录的 `ffprobe` |

## 附录 C：关键函数索引

| 函数 | 行 | 一句话 |
| --- | --- | --- |
| `log` / `logf` | 81 / 87 | 控制台+文件 / 只写文件 |
| `run` | 166 | 跑子进程，失败带 stderr 抛错 |
| `probe` | 199 | 一次 ffprobe 拿全部信息 |
| `print_info` | 223 | 信息面板 |
| `draw_bar` | 298 | 画一帧进度条 |
| `run_progress` | 310 | 带进度条跑 ffmpeg |
| `dpapi` | 351 | DPAPI 加解密 |
| `save_key` / `load_key` | 375 / 394 | 密钥落盘与读取 |
| `cache_key` | 422 | `SHA1(target + NUL + text)` |
| `chat` | 636 | 带退避重试的 API 调用 |
| `translate_items` | 663 | 键对齐 + 对半拆批 |
| `translate_all` | 685 | 缓存命中 + 分批翻译 + 落盘 |
| `parse_srt` / `format_srt` | 590 / 606 | 字幕解析与生成 |
| `mux_cmd` / `burn_cmd` | 726 / 737 | 两条 ffmpeg 命令（纯函数） |
| `check_output` | 772 | 输出校验，可能改名 |
| `process` | 908 | 主流程 |
| `wizard` | 1160 | 交互向导 |
| `self_test` | 1242 | 自检 |
