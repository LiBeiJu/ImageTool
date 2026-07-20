#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量图片 AI 去水印工具

功能概览：
1. 批量遍历输入目录中的 jpg / jpeg / png / webp 图片。
2. 优先调用豆包/火山图像编辑接口去除画师水印、签名、字幕、多余文字。
3. 如果豆包/火山接口因为 NSFW、内容安全、审核拦截等原因失败，自动降级调用 xAI Grok 图像编辑接口。
4. 强制在请求参数中关闭平台水印，保存时保持原文件名。
5. 内置超时、重试、指数退避、并发限流，避免网络波动或 API 频率限制导致批处理失败。

重要说明：
- 请只处理你拥有版权或已获得授权处理的图片。
- 不同云厂商的图像编辑接口会迭代，若你的控制台给出的 endpoint / model / 字段名不同，优先通过 .env 修改，代码中已集中封装在 ProviderClient 中，便于调试。
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import logging
import mimetypes
import os
import random
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

import requests
from dotenv import load_dotenv
from PIL import Image


# 支持的本地图片格式
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

# 默认提示词：尽量只移除水印与文字，不改动主体内容
DEFAULT_EDIT_PROMPT = (
    "请移除图片中的画师水印、签名、logo、字幕、日期戳和多余文字。"
    "保持原始构图、人物、背景、色彩、画风和分辨率尽可能不变。"
    "不要添加任何新的文字、logo、边框、水印或平台标识。"
)

# 判断是否属于内容安全/NSFW 拦截的关键词。命中后会自动切到 Grok 兜底。
SAFETY_ERROR_KEYWORDS = (
    "nsfw",
    "not safe",
    "unsafe",
    "safety",
    "sensitive",
    "content policy",
    "content_policy",
    "moderation",
    "violation",
    "forbidden",
    "risk",
    "illegal",
    "审核",
    "安全",
    "违规",
    "敏感",
    "拦截",
    "不合规",
)

# 可重试的 HTTP 状态码
RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}


class ProviderError(RuntimeError):
    """API 调用失败时抛出的统一异常。"""

    def __init__(self, provider: str, message: str, *, status_code: Optional[int] = None, safety_blocked: bool = False):
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.safety_blocked = safety_blocked


@dataclass(frozen=True)
class AppConfig:
    """运行配置，全部可由命令行或 .env 覆盖。"""

    input_dir: Path
    output_dir: Path
    concurrency: int
    provider_concurrency: int
    timeout_seconds: int
    max_retries: int
    retry_base_delay: float
    prompt: str
    skip_existing: bool
    copy_on_failure: bool

    volc_endpoint: str
    volc_api_key: str
    volc_model: str
    volc_response_format: str

    xai_endpoint: str
    xai_api_key: str
    xai_model: str
    xai_response_format: str


class RateLimitedSession:
    """带并发限流、超时、重试的 requests 封装。"""

    def __init__(self, provider: str, concurrency: int, timeout_seconds: int, max_retries: int, retry_base_delay: float):
        self.provider = provider
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.session = requests.Session()
        # threading.Semaphore 用于限制同一 Provider 的并发请求数，避免触发 API 频率限制。
        import threading

        self._semaphore = threading.Semaphore(concurrency)

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """发送 HTTP 请求，遇到网络波动或限流状态码自动重试。"""

        last_error: Optional[BaseException] = None
        with self._semaphore:
            for attempt in range(1, self.max_retries + 1):
                try:
                    response = self.session.request(method, url, timeout=self.timeout_seconds, **kwargs)
                    if response.status_code not in RETRYABLE_STATUS_CODES:
                        return response

                    last_error = ProviderError(
                        self.provider,
                        f"HTTP {response.status_code}: {response.text[:500]}",
                        status_code=response.status_code,
                    )
                except requests.RequestException as exc:
                    last_error = exc

                if attempt < self.max_retries:
                    # 指数退避 + 少量随机抖动，降低并发批处理时重复撞限流的概率。
                    delay = self.retry_base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.35)
                    logging.warning("%s 第 %s/%s 次请求失败，%.2f 秒后重试：%s", self.provider, attempt, self.max_retries, delay, last_error)
                    time.sleep(delay)

        raise ProviderError(self.provider, f"请求重试 {self.max_retries} 次后仍失败：{last_error}")


def is_safety_error(text: str, status_code: Optional[int] = None) -> bool:
    """粗略判断 API 错误是否属于内容安全/NSFW 拦截。"""

    lower_text = (text or "").lower()
    if any(keyword in lower_text for keyword in SAFETY_ERROR_KEYWORDS):
        return True
    return status_code in {400, 403, 422} and any(keyword in lower_text for keyword in ("policy", "moderation", "safety"))


def read_image_as_data_url(image_path: Path) -> str:
    """把本地图片读成 data URL，兼容多数 OpenAI 风格图像编辑接口。"""

    mime_type = mimetypes.guess_type(str(image_path))[0] or "image/png"
    raw = image_path.read_bytes()
    return f"data:{mime_type};base64,{base64.b64encode(raw).decode('utf-8')}"


def read_image_as_base64(image_path: Path) -> str:
    """把图片读成纯 base64 字符串，兼容返回/入参不使用 data URL 的接口。"""

    return base64.b64encode(image_path.read_bytes()).decode("utf-8")


def write_image_bytes(output_path: Path, content: bytes) -> None:
    """原子化保存图片，避免处理中断时留下半截文件。"""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temp_path.write_bytes(content)
    temp_path.replace(output_path)


def validate_image_file(path: Path) -> None:
    """用 Pillow 快速验证返回内容确实是可打开的图片。"""

    with Image.open(path) as image:
        image.verify()


def extract_image_from_response(provider: str, response_json: Dict[str, Any], session: RateLimitedSession) -> bytes:
    """从常见图像编辑 API 响应中提取图片二进制。"""

    # OpenAI / xAI / Ark 风格：{"data": [{"b64_json": "..."}]} 或 {"data": [{"url": "..."}]}
    data = response_json.get("data")
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            if first.get("b64_json"):
                return base64.b64decode(first["b64_json"])
            if first.get("url"):
                return download_image(provider, first["url"], session)

    # 部分网关风格：{"result": {"image": "..."}} / {"result": {"url": "..."}}
    result = response_json.get("result") or response_json.get("Result")
    if isinstance(result, dict):
        for key in ("image", "image_base64", "b64_json", "binary_data_base64"):
            if result.get(key):
                return base64.b64decode(strip_data_url_prefix(str(result[key])))
        for key in ("url", "image_url", "output_url"):
            if result.get(key):
                return download_image(provider, str(result[key]), session)

    # 部分接口直接返回：{"image_base64": "..."} / {"url": "..."}
    for key in ("image", "image_base64", "b64_json", "binary_data_base64"):
        if response_json.get(key):
            return base64.b64decode(strip_data_url_prefix(str(response_json[key])))
    for key in ("url", "image_url", "output_url"):
        if response_json.get(key):
            return download_image(provider, str(response_json[key]), session)

    raise ProviderError(provider, f"无法从响应中解析图片字段：{json.dumps(response_json, ensure_ascii=False)[:1000]}")


def strip_data_url_prefix(value: str) -> str:
    """去掉 data:image/png;base64, 前缀。"""

    if "," in value and value.startswith("data:"):
        return value.split(",", 1)[1]
    return value


def download_image(provider: str, url: str, session: RateLimitedSession) -> bytes:
    """下载 API 返回的图片 URL。"""

    response = session.request("GET", url)
    if response.status_code >= 400:
        raise ProviderError(provider, f"下载图片失败 HTTP {response.status_code}: {response.text[:300]}", status_code=response.status_code)
    return response.content


class DoubaoVolcClient:
    """
    豆包/火山图像编辑客户端。

    默认使用 OpenAI/Ark 兼容的图像编辑调用方式：
    - endpoint 通过 VOLC_IMAGE_EDIT_ENDPOINT 配置。
    - API Key 通过 VOLC_ACCESSKEY 配置。
    - model 通过 VOLC_IMAGE_EDIT_MODEL 配置。

    如果你在火山控制台拿到的 veImageX 专用 endpoint 或字段名不同，只需要修改 build_payload() 中字段映射。
    """

    provider = "doubao-volc"

    def __init__(self, config: AppConfig):
        self.config = config
        self.http = RateLimitedSession(
            self.provider,
            config.provider_concurrency,
            config.timeout_seconds,
            config.max_retries,
            config.retry_base_delay,
        )

    def edit(self, image_path: Path) -> bytes:
        if not self.config.volc_api_key:
            raise ProviderError(self.provider, "缺少 VOLC_ACCESSKEY，无法调用豆包/火山接口")

        headers = {
            "Authorization": f"Bearer {self.config.volc_api_key}",
            "Content-Type": "application/json",
        }
        payload = self.build_payload(image_path)
        response = self.http.request("POST", self.config.volc_endpoint, headers=headers, json=payload)
        if response.status_code >= 400:
            message = response.text[:1000]
            raise ProviderError(
                self.provider,
                f"豆包/火山接口失败 HTTP {response.status_code}: {message}",
                status_code=response.status_code,
                safety_blocked=is_safety_error(message, response.status_code),
            )

        try:
            response_json = response.json()
        except ValueError as exc:
            raise ProviderError(self.provider, f"豆包/火山接口返回非 JSON 内容：{response.text[:500]}") from exc

        return extract_image_from_response(self.provider, response_json, self.http)

    def build_payload(self, image_path: Path) -> Dict[str, Any]:
        """构造豆包/火山图像编辑请求体，显式关闭平台水印。"""

        return {
            "model": self.config.volc_model,
            "prompt": self.config.prompt,
            # 多数 OpenAI/Ark 兼容接口支持 image 使用 data URL；若你的接口要求纯 base64，可改成 read_image_as_base64(image_path)。
            "image": read_image_as_data_url(image_path),
            "response_format": self.config.volc_response_format,
            # 关键：要求平台不要叠加水印。不同火山接口可能字段名为 watermark / use_watermark / add_watermark。
            "watermark": False,
            "use_watermark": False,
            "add_watermark": False,
            # 关键：尽量请求原图尺寸，避免重采样导致画质下降。
            "size": "original",
        }


class GrokXaiClient:
    """xAI Grok 图像编辑兜底客户端。"""

    provider = "xai-grok"

    def __init__(self, config: AppConfig):
        self.config = config
        self.http = RateLimitedSession(
            self.provider,
            config.provider_concurrency,
            config.timeout_seconds,
            config.max_retries,
            config.retry_base_delay,
        )

    def edit(self, image_path: Path) -> bytes:
        if not self.config.xai_api_key:
            raise ProviderError(self.provider, "缺少 XAI_API_KEY，无法调用 Grok 兜底接口")

        headers = {
            "Authorization": f"Bearer {self.config.xai_api_key}",
        }

        # xAI 图像编辑接口通常是 OpenAI 兼容 multipart/form-data；如果你的控制台文档要求 JSON，改 build_payload 即可。
        with image_path.open("rb") as image_file:
            files = {
                "image": (image_path.name, image_file, mimetypes.guess_type(str(image_path))[0] or "application/octet-stream"),
            }
            data = self.build_form_data()
            response = self.http.request("POST", self.config.xai_endpoint, headers=headers, data=data, files=files)

        if response.status_code >= 400:
            message = response.text[:1000]
            raise ProviderError(
                self.provider,
                f"xAI Grok 接口失败 HTTP {response.status_code}: {message}",
                status_code=response.status_code,
                safety_blocked=is_safety_error(message, response.status_code),
            )

        try:
            response_json = response.json()
        except ValueError as exc:
            raise ProviderError(self.provider, f"xAI Grok 接口返回非 JSON 内容：{response.text[:500]}") from exc

        return extract_image_from_response(self.provider, response_json, self.http)

    def build_form_data(self) -> Dict[str, Any]:
        """构造 Grok 图像编辑表单，显式关闭平台水印。"""

        return {
            "model": self.config.xai_model,
            "prompt": self.config.prompt,
            "response_format": self.config.xai_response_format,
            "watermark": "false",
            "use_watermark": "false",
            "add_watermark": "false",
            "size": "original",
        }


def iter_images(input_dir: Path) -> Iterable[Path]:
    """递归遍历输入目录下所有支持格式的图片。"""

    for path in sorted(input_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
            yield path


def relative_output_path(input_dir: Path, output_dir: Path, image_path: Path) -> Path:
    """保持原文件名和子目录结构输出。"""

    return output_dir / image_path.relative_to(input_dir)


def process_one_image(image_path: Path, config: AppConfig, doubao: DoubaoVolcClient, grok: GrokXaiClient) -> Tuple[Path, str, str]:
    """处理单张图片，返回：图片路径、状态、说明。"""

    output_path = relative_output_path(config.input_dir, config.output_dir, image_path)
    if config.skip_existing and output_path.exists():
        return image_path, "skipped", "输出文件已存在，跳过"

    try:
        logging.info("开始处理：%s", image_path)
        image_bytes = doubao.edit(image_path)
        provider_used = doubao.provider
    except ProviderError as exc:
        if exc.safety_blocked:
            logging.warning("豆包/火山疑似内容安全拦截，切换 Grok 兜底：%s", image_path)
            image_bytes = grok.edit(image_path)
            provider_used = grok.provider
        else:
            if config.copy_on_failure:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(image_path, output_path)
                return image_path, "copied", f"豆包/火山失败，已复制原图：{exc}"
            raise

    write_image_bytes(output_path, image_bytes)
    validate_image_file(output_path)
    return image_path, "success", f"使用 {provider_used} 完成，输出：{output_path}"


def load_config() -> AppConfig:
    """加载 .env 与命令行参数。"""

    load_dotenv()

    parser = argparse.ArgumentParser(description="批量图片 AI 去水印工具")
    parser.add_argument("--input", default=os.getenv("INPUT_DIR", "input"), help="输入图片文件夹，默认读取 .env 的 INPUT_DIR 或 ./input")
    parser.add_argument("--output", default=os.getenv("OUTPUT_DIR", "output"), help="输出图片文件夹，默认读取 .env 的 OUTPUT_DIR 或 ./output")
    parser.add_argument("--concurrency", type=int, default=int(os.getenv("CONCURRENCY", "3")), help="本地批处理并发数")
    parser.add_argument("--provider-concurrency", type=int, default=int(os.getenv("PROVIDER_CONCURRENCY", "2")), help="单个 API Provider 最大并发数")
    parser.add_argument("--timeout", type=int, default=int(os.getenv("TIMEOUT_SECONDS", "90")), help="单次 HTTP 请求超时时间，单位秒")
    parser.add_argument("--retries", type=int, default=int(os.getenv("MAX_RETRIES", "3")), help="网络波动/限流时最大重试次数")
    parser.add_argument("--retry-base-delay", type=float, default=float(os.getenv("RETRY_BASE_DELAY", "1.5")), help="重试基础等待秒数")
    parser.add_argument("--prompt", default=os.getenv("EDIT_PROMPT", DEFAULT_EDIT_PROMPT), help="图像编辑提示词")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在输出文件")
    parser.add_argument("--copy-on-failure", action="store_true", help="处理失败时把原图复制到输出目录，默认失败则不输出")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"), choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="日志级别")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    return AppConfig(
        input_dir=Path(args.input).resolve(),
        output_dir=Path(args.output).resolve(),
        concurrency=max(1, args.concurrency),
        provider_concurrency=max(1, args.provider_concurrency),
        timeout_seconds=max(1, args.timeout),
        max_retries=max(1, args.retries),
        retry_base_delay=max(0.1, args.retry_base_delay),
        prompt=args.prompt,
        skip_existing=not args.overwrite,
        copy_on_failure=args.copy_on_failure,
        volc_endpoint=os.getenv("VOLC_IMAGE_EDIT_ENDPOINT", "https://ark.cn-beijing.volces.com/api/v3/images/generations"),
        volc_api_key=os.getenv("VOLC_ACCESSKEY", ""),
        volc_model=os.getenv("VOLC_IMAGE_EDIT_MODEL", "doubao-seededit-3-0-i2i-250628"),
        volc_response_format=os.getenv("VOLC_RESPONSE_FORMAT", "b64_json"),
        xai_endpoint=os.getenv("XAI_IMAGE_EDIT_ENDPOINT", "https://api.x.ai/v1/images/edits"),
        xai_api_key=os.getenv("XAI_API_KEY", ""),
        xai_model=os.getenv("XAI_IMAGE_EDIT_MODEL", "grok-2-image"),
        xai_response_format=os.getenv("XAI_RESPONSE_FORMAT", "b64_json"),
    )


def validate_config(config: AppConfig) -> None:
    """启动前做基础配置检查。"""

    if not config.input_dir.exists() or not config.input_dir.is_dir():
        raise SystemExit(f"输入目录不存在：{config.input_dir}")
    if not config.volc_api_key:
        raise SystemExit("缺少 VOLC_ACCESSKEY，请在 .env 中配置豆包/火山 API Key")
    if not config.xai_api_key:
        logging.warning("未配置 XAI_API_KEY：豆包/火山被内容安全拦截时将无法使用 Grok 兜底")


def main() -> int:
    config = load_config()
    validate_config(config)

    images = list(iter_images(config.input_dir))
    if not images:
        logging.warning("输入目录没有找到 jpg/png/webp 图片：%s", config.input_dir)
        return 0

    config.output_dir.mkdir(parents=True, exist_ok=True)
    doubao = DoubaoVolcClient(config)
    grok = GrokXaiClient(config)

    logging.info("发现 %s 张图片，开始批量处理。输入：%s 输出：%s", len(images), config.input_dir, config.output_dir)
    success = skipped = copied = failed = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=config.concurrency) as executor:
        future_map = {
            executor.submit(process_one_image, image_path, config, doubao, grok): image_path
            for image_path in images
        }
        for future in concurrent.futures.as_completed(future_map):
            image_path = future_map[future]
            try:
                _, status, message = future.result()
                if status == "success":
                    success += 1
                    logging.info("成功：%s | %s", image_path.name, message)
                elif status == "skipped":
                    skipped += 1
                    logging.info("跳过：%s | %s", image_path.name, message)
                elif status == "copied":
                    copied += 1
                    logging.warning("复制原图：%s | %s", image_path.name, message)
            except Exception as exc:  # noqa: BLE001 - 批处理需要捕获所有单图异常，继续处理其他图片
                failed += 1
                logging.error("失败：%s | %s", image_path, exc)

    logging.info("处理完成：成功 %s，跳过 %s，复制原图 %s，失败 %s", success, skipped, copied, failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
