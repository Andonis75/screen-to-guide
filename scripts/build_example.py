"""Build the public synthetic output example without downloads or model inference.

Reuses create_test_fixture.py and render_lesson.py. The authored manuscript
describes three synthetic screen states; it is not an AI-recognition benchmark.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from render_lesson import render_lesson

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "examples" / "synthetic-guide"
FILENAMES = {
    "README.md", "source.mp4", "lesson-manifest.json", "lesson.json", "guide.html", "guide.md",
    "frames/frame-0001.jpg", "frames/frame-0002.jpg", "frames/frame-0003.jpg",
}


def run(command):
    return subprocess.run(command, check=True, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)


def manuscript(video, metadata, timestamps):
    stages = [
        ("认识新建报告入口", 0, 3,
         "虚构的“蓝页报告”工作台显示“我的报告”页面，当前报告数量为 0。主区域提供“新建报告”按钮。",
         "这一章展示入口页面如何写成正文，并绑定同一时刻的截图。这里没有记录鼠标点击，也没有连接实际业务系统。",
         "工作台首页：报告数量为 0，中央显示“新建报告”入口。"),
        ("读懂表单中的示例参数", 3, 6,
         "画面切换到预先绘制的表单。项目名称显示“星河示例项目”，导出格式选中 PDF，旁边另列 Markdown，右下角显示“导出报告”按钮。",
         "这里可以准确记录当前可见的项目名称、格式和按钮位置。三个画面按时间直接切换，不能把画面变化描述成系统记录到了一次填写或点击。",
         "虚构表单：项目名称“星河示例项目”，格式 PDF，右下角为“导出报告”。"),
        ("区分界面结果与真实文件", 6, 9,
         "第三个预先绘制的画面显示“导出完成”，文件名为“星河示例项目.pdf”，下方提供“打开文件”和“返回报告列表”两个按钮。",
         "这个合成视频没有执行导出，也没有生成上述 PDF。文档只说明画面展示了什么。真实录屏转教程时，同样需要区分可见提示、实际动作与独立验证结果。",
         "虚构完成页：显示示例文件名和完成提示，不代表实际生成了文件。"),
    ]
    sections = []
    for index, (title, start, end, paragraph, explanation, caption) in enumerate(stages, 1):
        sections.append({
            "id": f"synthetic-stage-{index}", "title": title,
            "start_seconds": start, "end_seconds": end,
            "paragraphs": [paragraph, explanation],
            "images": [{"path": f"frames/frame-{index:04d}.jpg",
                        "timestamp_seconds": timestamps[index - 1], "caption": caption}],
        })
    return {
        "schema_version": 1, "kind": "lesson",
        "title": "合成样例：三幅界面如何整理成阅读文档",
        "summary": "这是录屏成册的公开输出样例，仅演示文档排版、原图查看和章节播放器。素材为 9 秒虚构界面，正文预先编写，未运行语音识别、OCR 或视觉模型，不代表模型识别效果或真实软件操作过程。",
        "source": {"name": "蓝页报告 · 合成工程测试素材", "author": "录屏成册项目",
                   "duration_seconds": metadata["duration"], "width": metadata["width"],
                   "height": metadata["height"], "sha256": hashlib.sha256(video.read_bytes()).hexdigest()},
        "video": "source.mp4",
        "prerequisites": [
            "使用支持 HTML5 视频的浏览器打开 guide.html，并保留同目录视频与 frames 文件夹。",
            "点击“播放本章”，从该章起点播放，到章末自动暂停。截图下的时间按钮从截图位置开始，仍在本章结束。",
            "点击“重播本章”回到本章起点；点击“继续播放完整视频”解除章节范围。完整视频只有 9 秒，没有音轨。",
            "截图可点开查看原图；页面顶部可下载 Markdown，或调用浏览器打印保存为 PDF。",
        ],
        "sections": sections,
        "coverage": {"method": "Authored synthetic manuscript and exact frames from a generated fixture",
                     "model_inference_used": False, "benchmark": False,
                     "frame_timestamps_seconds": timestamps,
                     "intervals": [{"start_seconds": 0, "end_seconds": 9,
                                    "note": "All three pre-rendered synthetic stages are represented."}]},
    }


def build(output, ffmpeg, ffprobe, font, overwrite):
    if output.is_symlink():
        raise ValueError("Output directory must not be a symbolic link")
    output = output.resolve()
    if output.exists() and not output.is_dir():
        raise ValueError("Output must be a directory")
    for name in FILENAMES:
        target = output / name
        if not target.resolve().is_relative_to(output) or target.is_symlink():
            raise ValueError("Example output paths must stay inside the chosen directory")
        if target.exists() and (not target.is_file() or not overwrite):
            raise ValueError(f"Output exists: {name}; use --overwrite to rebuild this example")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".synthetic-guide-", dir=output.parent) as temporary:
        staging = Path(temporary)
        video = staging / "source.mp4"
        command = [sys.executable, "-X", "utf8", str(ROOT / "scripts/create_test_fixture.py"),
                   "--output", str(video), "--ffmpeg", ffmpeg]
        if font:
            command += ["--font", font]
        run(command)
        probe = json.loads(run([ffprobe, "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=width,height,avg_frame_rate:format=duration",
                              "-of", "json", str(video)]).stdout)
        stream = probe["streams"][0]
        metadata = {"width": stream["width"], "height": stream["height"],
                    "duration": float(probe["format"]["duration"])}
        if (metadata["width"], metadata["height"]) != (1280, 720) or abs(metadata["duration"] - 9) > 0.01:
            raise ValueError("Fixture must be the expected 1280x720, 9-second video")
        frame_data = json.loads(run([ffprobe, "-v", "error", "-select_streams", "v:0",
                                   "-show_frames", "-show_entries", "frame=best_effort_timestamp_time",
                                   "-of", "json", str(video)]).stdout)["frames"]
        indices = [24, 96, 168]
        timestamps = [float(frame_data[index]["best_effort_timestamp_time"]) for index in indices]
        if any(abs(actual - expected) > 0.000001 for actual, expected in zip(timestamps, [1, 4, 7])):
            raise ValueError("Fixture frame timestamps must be exactly 1, 4 and 7 seconds")
        (staging / "frames").mkdir()
        run([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(video),
             "-vf", "select='eq(n,24)+eq(n,96)+eq(n,168)'", "-fps_mode", "passthrough",
             "-frames:v", "3", "-q:v", "3", str(staging / "frames/frame-%04d.jpg")])
        manifest = manuscript(video, metadata, timestamps)
        (staging / "lesson-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        render_lesson(manifest, staging)
        (staging / "README.md").write_text(
            "# 合成输出样例\n\n"
            "打开 [guide.html](guide.html) 查看完整阅读页，或在 GitHub 阅读 [guide.md](guide.md)。"
            "GitHub 文件预览不会执行 HTML 交互；下载本目录后用浏览器打开，或通过本机 HTTP 服务预览。\n\n"
            "本样例只演示输出结构、原图和章节播放。9 秒无声视频由 `scripts/create_test_fixture.py` 绘制，"
            "三个画面均为虚构，正文由构建脚本预先编写；没有运行 OCR、语音或视觉模型，不作为识别准确率、性能或真实操作效果的证明。\n\n"
            "- 章节：0–3 秒、3–6 秒、6–9 秒；截图取自原片 1、4、7 秒，1280×720。\n"
            "- `lesson-manifest.json` 是中间稿；`guide.html`、`guide.md`、`lesson.json` 由固定渲染器生成。\n"
            "- 重建需要 Python 环境中的 Pillow、本地中文字体以及 FFmpeg/FFprobe；不下载任何模型。\n\n"
            "在项目根目录执行：\n\n```powershell\n"
            ".\\.runtime\\venv\\Scripts\\python.exe -X utf8 scripts/build_example.py --overwrite\n```\n\n"
            "FFmpeg/FFprobe 不在 PATH 时，用 `--ffmpeg`、`--ffprobe` 指定本机程序；中文字体缺失时用 `--font` 指定。"
            "`--output-dir` 可指定其他目录。重建只更新本样例列出的九个文件。\n",
            encoding="utf-8")
        total_bytes = sum((staging / name).stat().st_size for name in FILENAMES)
        output.mkdir(parents=True, exist_ok=True)
        for name in sorted(FILENAMES):
            destination = output / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staging / name, destination)
    return {"output_dir": str(output), "files": len(FILENAMES), "bytes": total_bytes,
            "duration_seconds": metadata["duration"], "frame_timestamps_seconds": timestamps,
            "model_inference_used": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--font", help="Path to a local Chinese TTF/TTC font")
    parser.add_argument("--overwrite", action="store_true")
    options = parser.parse_args()
    tools = {name: shutil.which(getattr(options, name)) for name in ("ffmpeg", "ffprobe")}
    if not all(tools.values()):
        parser.error("FFmpeg and FFprobe are required; add them to PATH or pass --ffmpeg/--ffprobe")
    try:
        result = build(options.output_dir, tools["ffmpeg"], tools["ffprobe"], options.font, options.overwrite)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Example build failed: {exc}\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
