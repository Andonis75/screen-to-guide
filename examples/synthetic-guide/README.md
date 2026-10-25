# 合成输出样例

打开 [guide.html](guide.html) 查看完整阅读页，或在 GitHub 阅读 [guide.md](guide.md)。GitHub 文件预览不会执行 HTML 交互；下载本目录后用浏览器打开，或通过本机 HTTP 服务预览。

本样例只演示输出结构、原图和章节播放。9 秒无声视频由 `scripts/create_test_fixture.py` 绘制，三个画面均为虚构，正文由构建脚本预先编写；没有运行 OCR、语音或视觉模型，不作为识别准确率、性能或真实操作效果的证明。

- 章节：0–3 秒、3–6 秒、6–9 秒；截图取自原片 1、4、7 秒，1280×720。
- `lesson-manifest.json` 是中间稿；`guide.html`、`guide.md`、`lesson.json` 由固定渲染器生成。
- 重建需要 Python 环境中的 Pillow、本地中文字体以及 FFmpeg/FFprobe；不下载任何模型。

在项目根目录执行：

```powershell
.\.runtime\venv\Scripts\python.exe -X utf8 scripts/build_example.py --overwrite
```

FFmpeg/FFprobe 不在 PATH 时，用 `--ffmpeg`、`--ffprobe` 指定本机程序；中文字体缺失时用 `--font` 指定。`--output-dir` 可指定其他目录。重建只更新本样例列出的九个文件。
