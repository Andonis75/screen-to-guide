# 第三方组件与模型来源

本项目的 Python 依赖在 `requirements.txt` 中固定版本；模型下载与运行分别进行。
项目自有代码与文档采用根目录 MIT 许可证；以下第三方代码与模型继续受各自许可证约束。
发布压缩包不包含 `.runtime`、Python 环境或视觉/语音大模型权重。

## 随包提供的 OCR

`vendor/rapidocr_openvino` 来自 RapidOCR 1.4.4（RapidAI），采用 Apache-2.0。
版权与完整许可证见 `vendor/RapidOCR-LICENSE`。
上游：https://github.com/RapidAI/RapidOCR/tree/v1.4.4

其中 PP-OCRv4 检测、方向分类、中文识别模型及字典来自 PaddleOCR 项目，采用 Apache-2.0。
上游：https://github.com/PaddlePaddle/PaddleOCR
本包只调整推理设备环境变量为 `SCREEN_GUIDE_OCR_DEVICE`，默认 CPU；未改变模型权重。

## 单独下载的模型

- 视觉：Qwen/Qwen2.5-VL-7B-Instruct 的 OpenVINO INT4 转换版本。
  来源：https://huggingface.co/OpenVINO/Qwen2.5-VL-7B-Instruct-int4-ov
  固定版本：`48494b1aceaf169fc276b912baa876aa0e43e535`。
  模型卡声明 Apache-2.0；原作者 Qwen，转换发布者 OpenVINO。
- 语音：OpenAI Whisper base 的 OpenVINO FP16 转换版本。
  来源：https://huggingface.co/OpenVINO/whisper-base-fp16-ov
  固定版本：`84fbe975a79a8c996fd32c036558f29e2db6670f`。
  Whisper 上游代码与权重许可证为 MIT；该转换仓库模型卡声明 Apache-2.0。

模型目录保留上游 README 与 `model_source.json`，记录仓库、精确提交及下载时间。
下载入口不执行仓库中的 Python 代码。

## 外部运行工具

OpenVINO / OpenVINO GenAI 由 Intel 提供，负责本地推理；不表示已验证某种 Intel 硬件。
FFmpeg / FFprobe 由系统现有安装提供，本包不重新分发其二进制文件。
yt-dlp 是可选的视频获取依赖，版本单独固定在 `requirements-download.txt`；用户显式安装后由下载入口调用，发布包不包含其程序或下载媒体。
上游：https://github.com/yt-dlp/yt-dlp
OpenCV、Pillow、NumPy、PyClipper、Shapely、PyYAML、Hugging Face Hub 等依赖保留各自上游许可证。

## 工作流参考

0.2.0 参考了 [BiliNote](https://github.com/JefferyHcool/BiliNote) 的字幕复用、任务恢复和笔记组织思路。本项目的字幕解析、阶段缓存、获取入口及概览渲染为独立实现，未复制该项目代码。单条视频对照不作为通用质量排名。

`scripts/create_test_fixture.py` 生成的界面、项目名称和文件均为虚构工程素材，用于验证程序流程。
