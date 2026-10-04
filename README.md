# dsub — 视频字幕翻译嵌回工具

用 DeepSeek 把视频里的字幕翻译成中文，然后**嵌回视频**（软字幕，不重编码）或**烧录进画面**（硬字幕）。
纯 Python 单文件，只用标准库，**不需要 pip 安装任何东西**。

```
视频.mkv  ──ffmpeg──►  字幕.srt  ──DeepSeek──►  中文.srt  ──ffmpeg──►  视频.zh.mkv（软字幕）
                                                                    视频.zh.hardsub.mp4（烧录）
```

---

## 1. 环境要求

| 需要 | 说明 |
| --- | --- |
| Windows | 也可在 Linux/macOS 跑，但密钥加密（DPAPI）只在 Windows 生效 |
| Python 3.8+ | 已用 Python 3.14 实测；不需要装任何第三方库 |
| ffmpeg + ffprobe | 必须在 PATH 里，或用环境变量指定（见第 6 节） |

检查 ffmpeg 是否就绪：

```powershell
ffmpeg -version
```

没装的话：`winget install Gyan.FFmpeg`，或到 https://www.gyan.dev/ffmpeg/builds/ 下载后把 `bin` 目录加进 PATH。

---

## 2. 安装

两种装法，二选一：

### A. 直接下 exe（不用装 Python）

到 [Releases](https://github.com/GoInevitable/dsub-subtitle/releases) 下载 `dsub.exe`，放到任意目录（例如 `D:\Tools\`）就能用。

```powershell
D:\Tools\dsub.exe "D:\视频\某视频.mkv"
```

**仍然需要 ffmpeg 在 PATH 里。** exe 没做代码签名，Windows SmartScreen 可能提示「未知发布者」，
点「更多信息 → 仍要运行」即可；杀毒软件偶尔会误报 PyInstaller 打包的程序，介意就用 B 方案。

### B. 用源码（需要 Python 3.8+）

把整个 `dsub` 文件夹放到任意位置（例如 `D:\Tools\dsub`），**可选**把它加进 PATH，就能在任意目录直接敲 `dsub`：

```powershell
setx PATH "$env:PATH;D:\Tools\dsub"
```

（`setx` 后需要重开一个终端窗口才生效。）

不加 PATH 也能用，进到文件夹里运行即可：

```powershell
cd D:\Tools\dsub
python dsub.py "D:\视频\某视频.mkv"
```

---

## 3. 三步上手

```powershell
# 第一步：保存 API Key（只需做一次，输入时不回显、加密存储）
python dsub.py --set-key

# 第二步：翻译 + 软字幕嵌回，输出 某视频.zh.mkv
python dsub.py "D:\视频\某视频.mkv"

# 想烧录进画面（任何播放器都能看，但会重编码）
python dsub.py "D:\视频\某视频.mkv" --burn
```

第一次跑建议拿一个几分钟的短视频试，确认效果和花费。

API Key 在 https://platform.deepseek.com/api_keys 创建。

### 什么参数都不加：交互向导

直接敲 `dsub`（不带任何参数）会进入交互模式，一路问下来把事情做完：密钥 → 视频 → 字幕来源 →
输出模式 → 输出文件名 → 确认。**已经保存过密钥就不会再问密钥**；路径可以直接把文件从资源管理器
拖进窗口（带引号、带 `&` 都能认）。跑完还会问要不要接着处理下一个视频。

```
> dsub

================================================================
  dsub 交互模式 —— 直接回车用默认值，Ctrl+C 随时退出
================================================================
  视频文件路径（可直接把文件拖进窗口）: D:\视频\某视频.mkv
================================================================
  视频信息
  文件    D:\视频\某视频.mkv
          1.42 GB   封装 matroska   总码率 2.4 Mb/s
  时长    01:23:45   视频编码 h264 1920x1080 23.976 fps
  音频    aac 48000 Hz 2ch  (jpn)
  字幕    #0  subrip  (eng)
================================================================
  字幕    内嵌字幕轨 #0（subrip，语言 eng）
    回车就用它，或输入 2 自己指定字幕文件
  请选择 [1]: 
  要翻译吗？
    1) 要，翻译成简体中文（消耗 API 额度）   [默认]
    2) 不用，字幕已经是中文了，直接拿来用
  请选择 [1]: 
  翻译需要 API Key，去这里创建一个：
          https://platform.deepseek.com/api_keys
  粘贴 API Key（输入时不回显）: 
  正在验证密钥…
  密钥可用。
  保存到本机（Windows DPAPI 加密）？[Y/n]: y
  已保存，以后不用再输。
  输出模式（决定最后生成什么文件）：
    1) 软字幕嵌回  → 某视频.zh.mkv   [默认]
        字幕是独立轨道，播放器里能开关、能换字体；视频音频原样复制，画质不变、几秒钟完事
    2) 烧录硬字幕  → 某视频.zh.hardsub.mp4
        字幕印死在画面上，手机/电视/U盘插哪都能看；要重新编码，10 分钟视频大概几分钟到几十分钟
    3) 只导出字幕  → 某视频.zh.srt
        不碰视频，只给你一个中文字幕文件，自己拿去压片或换播放器
  请选择 [1]: 2
  说明：字幕焊进画面，要用 libx264 重新编码视频（CRF 20，越大越糊越小）。
  换编码器吗（显卡加速能快很多）？[y/N]: 
  输出文件 [某视频.zh.hardsub.mp4]: 
----------------------------------------------------------------
  视频    某视频.mkv
  输出    D:\视频\某视频.zh.hardsub.mp4
  模式    烧录硬字幕
  翻译    翻译成 简体中文
  开始？[Y/n]: 
  翻译    1284 条字幕 -> 简体中文（模型 deepseek-chat）
  ......
完成：D:\视频\某视频.zh.hardsub.mp4（总用时 12:31）

  还要处理下一个视频吗？[y/N]: n
  再见。
```

向导里每一步都当场校验：文件不存在会重新问、软字幕选了 `.mp4` 会退回重输、输出已存在会问你要不要覆盖。
在脚本、管道等**非交互环境**下不带参数运行，会直接报错告诉你该怎么传参，不会卡在那里等输入。

### 运行时会看到什么

开工前先列出视频信息，再列出这次要做什么；翻译、封装、烧录各有一条进度条
（`已用` / `剩余` 实时估算）。输出被重定向到文件或管道时，进度条自动退化成普通文字行，不会塞满日志。

```
================================================================
  视频信息
  文件    D:\视频\某视频.mkv
          1.42 GB   封装 matroska   总码率 2.4 Mb/s
  时长    01:23:45   视频编码 h264 1920x1080 23.976 fps
  音频    aac 48000 Hz 2ch  (jpn)
  字幕    #0  subrip  (eng)
================================================================
  任务
  字幕    内嵌 0:s:0（subrip，语言 eng）
  输出    D:\视频\某视频.zh.mkv
  模式    软字幕嵌回（视频音频直接复制，很快）
  密钥    已保存的密钥 sk-…cdef
================================================================
  翻译    1284 条字幕 -> 简体中文（模型 deepseek-chat）
  缓存    命中 1200 条，只需新译 84 条
  翻译 [==============>       ]  58.4%  750/1284 条  已用 03:12  剩余 02:17
  翻译    完成 1284 条（复用 1200，新译 84），用时 05:29
  字幕    已写出 D:\视频\某视频.zh.srt
  封装    开始（直接复制视频音频流）
  封装 [=======================>]  96.3%  01:20/01:23  已用 00:12  剩余 00:01
  封装 完成，用时 12.4 秒
完成：D:\视频\某视频.zh.mkv（总用时 05:41）
日志：C:\Users\你\.dsub\logs\dsub-20261004.log
```

---

## 4. 更多用法

```powershell
# 视频没有字幕，导入外挂字幕文件（.srt / .ass / .ssa / .vtt 都行）
python dsub.py "视频.mp4" --srt-in "字幕.srt"

# 视频里有多条字幕轨，指定第 1 条（对应 ffmpeg 的 0:s:1）
python dsub.py "视频.mkv" --stream 1

# 指定输出文件名
python dsub.py "视频.mkv" -o "D:\输出\中文版.mkv"

# 输出已存在时：默认报错；也可以覆盖，或自动出副本
python dsub.py "视频.mkv" --overwrite        # 直接覆盖原输出
python dsub.py "视频.mkv" --auto-rename      # 另存为 视频.zh(1).mkv，不动旧文件

# 不保留翻译好的字幕文件（默认会留一份 .srt 在视频旁边）
python dsub.py "视频.mkv" --no-srt

# 翻成繁体
python dsub.py "视频.mkv" --target "繁體中文"

# 长视频想省钱：调大批次（默认 20 条一次请求）
python dsub.py "视频.mkv" --batch 40

# 换模型
python dsub.py "视频.mkv" --model deepseek-reasoner
```

### 字幕已经是中文：只烧录 / 只嵌回 / 只导出

视频里本来就带中文字幕（或者你已经有一份中文字幕）时，不需要翻译、不消耗 API 额度、**不需要 API Key**：

```powershell
# 直接把视频里的中文字幕烧进画面
python dsub.py "视频.mkv" --no-translate --burn

# 视频里有多条字幕轨，指定第 0 条（中文字幕）软字幕嵌回
python dsub.py "视频.mkv" --no-translate --stream 0

# 同名字幕文件直接烧录
python dsub.py "视频.mkv" --srt-in "中文字幕.srt" --no-translate --burn
```

不加 `--no-translate` 时，如果工具发现字幕本来就是中文，**默认也会直接拿来用**（问你一句，回车即可）；
非交互环境（脚本/管道）下必须明确选一个：`--no-translate` 表示直接用，`--yes` 表示确实要再翻一遍。

### 只要字幕文件：--srt-only

```powershell
# 翻译后只导出字幕，不碰视频
python dsub.py "视频.mkv" --srt-only

# 把视频里的字幕抽出来存成文件（不翻译、不需要 Key）
python dsub.py "视频.mkv" --no-translate --srt-only -o "字幕.srt"
```

`-o` 给的不是 `.srt/.ass/.vtt` 结尾时会自动改成 `.srt`。

### 翻译缓存：相同/相似视频直接复用

每句原文的译文都会按「原文 + 目标语言」的指纹存到 `%USERPROFILE%\.dsub\trans_cache.json`：

- **同一个文件再跑一次 = 零请求、零花费**（换输出格式、换编码器、重跑烧录都算）
- **相似文件**：重复的台词、片头片尾、系列剧的常用句直接命中，只把新句子发出去
- 每翻完一批就落盘，**中途 Ctrl+C 或断网，重跑接着来，不会重复花钱**
- 缓存命中情况会在运行时打印：`翻译 完成 1284 条（复用 1200，新译 84）`

```powershell
python dsub.py --clear-cache          # 清空缓存
python dsub.py "视频.mkv" --no-cache  # 这次不读也不写缓存
```

> 换了 `--target`（比如简体改繁体）会重新翻译，因为缓存键里带了目标语言。

### 日志

每次运行都会写一份日志，出错时看它最快：

```powershell
python dsub.py "视频.mkv" --log "D:\我的日志.txt"   # 指定路径
python dsub.py "视频.mkv" --no-log                  # 不写日志
```

默认写在 `%USERPROFILE%\.dsub\logs\dsub-年月日.log`，**自动只保留最近 10 份**。
里面记了启动时间、完整命令行（`--api-key` 会被打码）、每一条 ffmpeg 命令、ffmpeg 的完整报错、
每次翻译进度。运行结束和出错时都会把这行日志的路径打出来：

```
日志：C:\Users\你\.dsub\logs\dsub-20261004.log
```

> 翻译完成后，字幕文件会同时保留在视频旁边（`某视频.zh.srt`），方便你检查或自己再修。
> 不想要就加 `--no-srt`。

---

## 5. 字幕从哪来

按下面的优先级自动挑，全部都会在运行时打印出来告诉你用了哪个：

| 优先级 | 来源 | 说明 |
| --- | --- | --- |
| 1 | `--srt-in "文件"` | 你显式指定的字幕文件 |
| 2 | 视频内嵌字幕轨 | 自动优先挑**非中文**轨；mkv 里的软字幕通常走这条 |
| 3 | 视频同名外挂字幕 | 例如 `视频.srt` / `视频.ass` / `视频.vtt` 放在视频旁边 |

三条都没有时会直接报错退出，并提示你该怎么做。

挑中的字幕如果本来就是中文，工具默认直接使用（不翻译），见上面「字幕已经是中文」一节。

**不支持**：PGS / DVD 图形字幕（那是图片，需要 OCR）、语音识别（视频里没字幕且你没有字幕文件时需要先用 whisper 之类的工具生成）。

---

## 6. 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 无 | 临时用一次；**优先级高于** `--set-key` 保存的密钥 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | 兼容 OpenAI 协议的中转也可以填这里 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 翻译建议用 `deepseek-chat` |
| `FFMPEG` | PATH 里的 `ffmpeg` | 可以是 exe 路径，也可以直接给 `bin` **目录** |
| `FFPROBE` | FFMPEG 同目录的 `ffprobe` | 同上 |

```powershell
# 只在当前窗口生效
$env:FFMPEG = "D:\ffmpeg\bin"          # 给目录也行
$env:DEEPSEEK_API_KEY = "sk-xxxx"      # 临时用一次，不落盘
python dsub.py "视频.mkv"
```

---

## 7. 密钥安全

`--set-key` 保存的密钥做了这些处理：

1. **输入不回显** —— 用 `getpass`，终端里看不到明文。
2. **先验证再保存** —— 真的调一次 DeepSeek 接口，Key 无效就直接报错、**不写盘**（实测 401 时不会留下任何文件）。
3. **DPAPI 加密存储** —— 用 Windows 数据保护 API（`CryptProtectData`）加密后写入 `%USERPROFILE%\.dsub\key.dpapi`，
   密钥和**当前 Windows 账户**绑定：换个账户、换台机器、把文件拷走都解不开。
4. **日志不打印密钥** —— 只会显示 `sk-…cdef` 这种掩码形式。
5. **三个来源的优先级**：`--api-key` > 环境变量 > 已保存的密钥。

管理命令：

```powershell
python dsub.py --set-key       # 保存 / 覆盖
python dsub.py --forget-key    # 删除已保存的密钥
```

> 非 Windows 平台没有 DPAPI，会退化为明文保存（权限 600）并明确警告。

---

## 8. 输出说明

| 模式 | 参数 | 默认输出 | 特点 |
| --- | --- | --- | --- |
| 软字幕嵌回 | 无 | `视频.zh.mkv` | 视频音频**原样复制不重编码**，几秒到几分钟；播放器里可开关、可换字体 |
| 烧录硬字幕 | `--burn` | `视频.zh.hardsub.mp4` | 字幕焊死在画面上；需要重编码视频（默认 libx264 CRF 20） |
| 只导出字幕 | `--srt-only` | `视频.zh.srt` | 完全不处理视频，只产出翻译好的字幕文件 |

烧录时想看字幕样式，用 `--force-style`（libass 语法，逗号分隔）：

```powershell
python dsub.py "视频.mkv" --burn --force-style "FontName=Microsoft YaHei,FontSize=24,Outline=2,MarginV=30"
```

要硬件加速（快很多，画质略降）可以换编码器：

```powershell
python dsub.py "视频.mkv" --burn --vcodec h264_nvenc --preset p4
```

---

## 9. 内置的防呆

工具会在动手之前拦下这些情况，而不是跑一半才失败：

- ffmpeg / ffprobe 不存在或跑不起来 → 直接告诉你装什么、怎么配
- 输入文件不存在、太小（< 1KB）、扩展名不像视频 → 报错或警告
- **输出文件已存在** → 拒绝；可以选择 `--overwrite` 覆盖、`--auto-rename` 自动出副本（`视频.zh(1).mkv`），向导里是直接三选一
- **输出会覆盖原视频** → 拒绝，必须换 `-o`
- 软字幕输出成 `.mp4` → 拒绝（mp4 装不了 SRT，要么输出 `.mkv` 要么 `--burn`）
- 磁盘剩余空间不够（按输入体积估算）→ 拒绝并给出还差多少
- 字幕疑似**已经是中文** → 警告并询问，非交互环境需要 `--yes` 才继续
- 字幕疑似**已经是中文** → 默认直接拿来用（不翻译），只在交互环境问一句；非交互环境必须显式给 `--no-translate` 或 `--yes`
- 需要翻译却没有 API Key → 明确提示 `--set-key`，并告诉你已有中文字幕时可以用 `--no-translate` 免密钥
- `--srt-only` 和 `--no-srt` 同时给（自相矛盾）→ 直接报错
- 视频既没内嵌字幕也没同名外挂 → 直接告诉你可以用 `--srt-in`（向导里会直接问你要字幕文件）
- 无参数运行但不在交互终端（脚本、管道、CI）→ 明确报错，不会卡住等输入
- 碰到图形字幕（PGS/DVD）→ 明确说需要 OCR，不做无用功
- 模型返回条数不对 → 自动对半拆批重试，仍缺的条目保留原文并提示
- 中途 Ctrl+C → 已翻译的部分存在 `<输出名>.srt.cache.json`，**重跑同一条命令会接着翻**，不会重复花钱

翻译缓存：每翻完一批就落盘一次，全部完成后自动删除。

---

## 10. 常见问题

| 现象 | 原因 / 解决 |
| --- | --- |
| `没有可用的 API Key` | 先跑 `python dsub.py --set-key`，或设置 `DEEPSEEK_API_KEY` |
| `API Key 无效或已失效` | Key 写错了或被删了，重新 `--set-key` |
| `DeepSeek 账户余额不足` | 去官网充值 |
| `这个视频既没有内嵌字幕轨…` | 用 `--srt-in "字幕.srt"`，或先做语音识别 |
| `视频里的字幕轨都是图形字幕` | PGS/DVD 图形字幕，需要 OCR，本工具不处理 |
| `软字幕不能装进 mp4` | 输出 `.mkv`，或加 `--burn` |
| `输出文件已存在` | 加 `--overwrite` 覆盖、`--auto-rename` 出副本，或换 `-o` |
| 字幕是中文却还被翻译 | 正常情况下工具会默认直接用（不翻译）并问你一句；是你自己选了「要翻译」或加了 `--yes` |
| 想确认缓存有没有生效 | 看运行时那行 `翻译 完成 N 条（复用 X，新译 Y）`；`Y=0` 就是全命中 |
| 换了目标语言发现没复用缓存 | 正常，缓存键包含目标语言，简体和繁体各存各的 |
| 字幕时间轴对不上 | 抽字幕时会丢掉 ASS 的定位标签，错位明显的话用 `--srt-in` 传已经对好的字幕 |
| `Only audio, video, and subtitles are supported for Matroska` | 老版本用 `-map 0` 会把 mp4 里的时间码数据流也搬进 mkv。现已只搬视频/音频/字幕/附件流，数据流忽略并提示 |
| 出错了想看细节 | 看运行结束打印的日志路径，ffmpeg 的完整报错都在里面；也可以 `--log` 自己指定 |
| 想看看有没有内部错误 | `python dsub.py --self-test`（自检不联网、不调 ffmpeg） |

---

## 11. 文件清单

```
dsub/
├─ dsub.py      主程序（单文件，仅标准库）
├─ dsub.cmd     Windows 快捷启动，可直接把本目录加进 PATH
├─ LICENSE      MIT
└─ README.md    本文件
```

配置与缓存（不在本目录）：

```
%USERPROFILE%\.dsub\key.dpapi          DPAPI 加密后的 API Key
%USERPROFILE%\.dsub\trans_cache.json   翻译缓存（按句子指纹，跨视频复用）
%USERPROFILE%\.dsub\logs\*.log         运行日志，只留最近 10 份
```

> 两者都在自己的用户目录下，不会写进视频所在目录；缓存可以随时 `--clear-cache` 删掉。
