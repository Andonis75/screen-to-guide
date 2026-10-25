# 开发说明

在项目根目录完成 `scripts/run.ps1 --setup`。Python 测试使用独立环境；播放器测试另外需要 Node.js，最终离线 HTML 不依赖 Node.js。

## 安装测试依赖

下面的缓存设置只影响当前 PowerShell 会话，让安装缓存与临时文件保留在项目所在磁盘：

```powershell
$skillCache = Join-Path (Get-Location).Path '.runtime\cache'
$env:PIP_CACHE_DIR = Join-Path $skillCache 'pip'
$env:TMP = Join-Path $skillCache 'tmp'
$env:TEMP = $env:TMP
New-Item -ItemType Directory -Force -Path $env:PIP_CACHE_DIR,$env:TMP | Out-Null
.\.runtime\venv\Scripts\python.exe -m pip install --cache-dir .runtime/cache/pip -r requirements-dev.txt
```

验证真实 yt-dlp 行为的测试还需要安装可选 `requirements-download.txt`；没有安装时，对应测试会跳过。

## 运行检查

```powershell
$env:PYTHONUTF8="1"
.\.runtime\venv\Scripts\python.exe -m pytest tests -q
node --test tests/test_lesson_playback.cjs
```

媒体集成测试需要 FFmpeg、FFprobe 在 PATH 中。测试所验证的行为与真实视频记录见[验证记录](validation.md)。

## 重建公开样例与分发包

```powershell
.\.runtime\venv\Scripts\python.exe -X utf8 scripts/build_example.py --overwrite
.\.runtime\venv\Scripts\python.exe scripts/package_skill.py
```

公开样例由预先编写的虚构材料生成，用于检查排版和回放。分发包按白名单收集项目文件，排除运行环境、模型下载目录、用户视频与本地输出。

修改教程的正文时，编辑中间 manifest，再由固定渲染器生成成品。教学稿契约见[教学流程](../references/teaching-guide.md)，软件录屏动作依据见[动作记录](../references/action-evidence.md)。
