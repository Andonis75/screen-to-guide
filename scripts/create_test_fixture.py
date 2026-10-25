"""Create a 9-second fictional UI recording for local engineering tests.

This fixture checks extraction/OCR/inference plumbing, not performance on a
user recording. Every frame is visibly labelled as synthetic test material.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
SIZE = (1280, 720)
BLUE, INK, MUTED, LINE = "#194ec4", "#12264c", "#5c6980", "#d9e1ee"


def find_font(explicit: str | None) -> Path:
    candidates = [Path(explicit)] if explicit else [
        Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/msyh.ttf"),
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Chinese font missing; use --font with a local CJK .ttf/.ttc font")


def draw_stage(stage: int, font_path: Path) -> Image.Image:
    image = Image.new("RGB", SIZE, "#f5f7fb")
    draw = ImageDraw.Draw(image)
    fonts = {size: ImageFont.truetype(str(font_path), size) for size in (16, 18, 20, 24, 28, 34, 40)}

    def text(x, y, value, size=20, fill=INK):
        draw.text((x, y), value, font=fonts[size], fill=fill)

    def box(bounds, fill="white", outline=LINE, radius=8):
        draw.rounded_rectangle(bounds, radius=radius, fill=fill, outline=outline, width=2)

    def button(x, y, value, width=180, primary=True):
        box((x, y, x + width, y + 52), BLUE if primary else "white", BLUE if primary else LINE)
        text(x + 22, y + 10, value, fill="white" if primary else INK)

    draw.rectangle((0, 0, 1280, 78), fill="white")
    draw.rectangle((0, 0, 1280, 6), fill=BLUE)
    text(36, 24, "蓝页报告", 28)
    text(252, 31, "工作台", 18, MUTED)
    box((868, 19, 1239, 58), "#fff4dc", "#e7cf91", 4)
    text(887, 27, "工程测试素材 · 虚构界面", 20, "#7c5500")
    draw.rectangle((0, 79, 214, 720), fill="white")
    draw.line((214, 79, 214, 720), fill=LINE, width=2)
    for i, value in enumerate(("我的报告", "模板库", "导出记录")):
        if i == 0:
            box((20, 113 + i * 64, 193, 161 + i * 64), "#edf3ff", "#edf3ff", 5)
        text(43, 123 + i * 64, value, 20, BLUE if i == 0 else MUTED)
    text(34, 616, "全部内容为虚构", 16, MUTED)
    text(34, 647, "仅供本地工程测试", 16, MUTED)
    text(264, 110, "我的报告", 34)
    text(266, 163, "从项目概览到 PDF 文档，在此管理报告。", 20, MUTED)
    text(1001, 116, f"测试阶段 {stage} / 3", 20, MUTED)

    if stage == 1:
        box((264, 225, 1228, 613))
        text(310, 265, "创建第一份项目报告", 28)
        text(310, 321, "选择“新建报告”，填写项目名称与导出格式。", 24, MUTED)
        button(310, 400, "新建报告", 188)
        text(310, 495, "当前报告数量：0", 20, MUTED)
        text(310, 539, "示例数据不会连接任何外部服务。", 18, MUTED)
    elif stage == 2:
        box((264, 225, 1228, 613))
        text(310, 253, "新建报告", 28)
        text(310, 311, "项目名称", 20)
        box((310, 347, 1168, 399), "#fff", BLUE, 4)
        text(330, 358, "星河示例项目", 24)
        text(310, 425, "导出格式", 20)
        box((310, 460, 471, 510), "#edf3ff", BLUE, 5)
        draw.ellipse((329, 477, 345, 493), fill=BLUE)
        text(361, 468, "PDF", 24, BLUE)
        box((492, 460, 678, 510), "white", LINE, 5)
        draw.ellipse((511, 477, 527, 493), outline=MUTED, width=2)
        text(543, 468, "Markdown", 20, MUTED)
        button(988, 542, "导出报告", 180)
    else:
        box((264, 225, 1228, 613))
        draw.ellipse((310, 270, 370, 330), fill="#e4f4eb")
        draw.line((326, 298, 338, 310, 356, 286), fill="#17613b", width=5)
        text(396, 268, "导出完成", 40, "#17613b")
        text(310, 365, "文件名称：星河示例项目.pdf", 28)
        text(310, 421, "文件格式：PDF    ·    状态：已生成", 24, MUTED)
        button(310, 490, "打开文件", 180)
        button(512, 490, "返回报告列表", 218, False)
    text(266, 661, "合成录屏用于测试抽帧、OCR 和本地模型流程，不代表真实使用效果。", 18, MUTED)
    return image


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".runtime/tests/fixture.mp4")
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg"))
    parser.add_argument("--font", help="Local Chinese font; Windows Microsoft YaHei is preferred")
    args = parser.parse_args()
    if not args.ffmpeg:
        parser.error("ffmpeg is not on PATH; pass --ffmpeg")
    output = args.output.resolve()
    if output.suffix.lower() != ".mp4":
        parser.error("--output must end in .mp4")
    output.parent.mkdir(parents=True, exist_ok=True)
    font = find_font(args.font)
    with tempfile.TemporaryDirectory(prefix="fixture-stages-", dir=output.parent) as temp:
        command = [args.ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        for stage in range(1, 4):
            path = Path(temp) / f"stage-{stage}.png"
            draw_stage(stage, font).save(path)
            command += ["-loop", "1", "-framerate", "24", "-t", "3", "-i", str(path)]
        command += ["-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0,format=yuv420p[v]",
                    "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                    "-r", "24", "-t", "9", "-movflags", "+faststart", str(output)]
        subprocess.run(command, check=True, timeout=180)
    print(f"Synthetic engineering fixture: {output}")
    print("1280x720 / 24 fps / 9 seconds / no audio")
    print("0–3s: create report; 3–6s: 星河示例项目 + PDF; 6–9s: export complete")


if __name__ == "__main__":
    main()
