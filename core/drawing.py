"""Single-attempt drawing adapters, based on generic_image_gen's protocol split.

No key rotation, automatic retries, model probes, or fallback generation calls.
"""

import asyncio
import base64
import io
import ipaddress
import json
import re
import socket
import warnings
from urllib.parse import quote, urlsplit

import httpx
from PIL import Image

from .story import TheaterError

MAX_BYTES = 24 * 1024 * 1024
MAX_RESPONSE = 40 * 1024 * 1024


def normalize_image(raw: bytes, avatar: bool = False) -> bytes:
    if len(raw) > MAX_BYTES:
        raise TheaterError("图片超过24MB限制。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.width * image.height > 20_000_000:
                    raise TheaterError("图片像素数过大。")
                image.seek(0)
                image = image.convert("RGB")
                if avatar:
                    image.thumbnail((512, 512))
                out = io.BytesIO()
                # Re-encode to remove metadata and normalize GIF first frames.
                image.save(out, format="PNG")
                result = out.getvalue()
                if len(result) > MAX_BYTES:
                    raise TheaterError("转换后的图片过大。")
                return result
    except TheaterError:
        raise
    except Exception:
        raise TheaterError("接口返回的内容不是可解码的图片。") from None


def api_root(base: str, version: str) -> str:
    parsed = urlsplit(base.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise TheaterError("API地址应是无账号、查询参数和片段的 http(s) 基础地址。")
    root = base.strip().rstrip("/")
    if root.endswith(("/v1", "/v1beta")):
        root = root.rsplit("/", 1)[0]
    return f"{root}/{version}"


async def public_image_url(url: str):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise TheaterError("图片下载地址必须是公开 HTTPS 地址。")
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, parsed.port or 443)
        if not infos or any(not ipaddress.ip_address(x[4][0]).is_global for x in infos):
            raise TheaterError("拒绝从私网或保留地址下载返回图片。")
    except TheaterError:
        raise
    except Exception:
        raise TheaterError("无法解析图片下载地址。") from None


class DrawingClient:
    def __init__(self, config: dict, api_key: str, transport=None, url_validator=public_image_url):
        self.config = config
        self.key = api_key
        self.transport = transport
        self.url_validator = url_validator

    def validate(self):
        protocol = self.config.get("protocol", "openai_chat")
        if protocol not in {"openai_chat", "gemini", "openai_images"}:
            raise TheaterError("绘图协议不受支持。")
        api_root(self.config.get("api_base", ""), "v1")
        if not self.key or not self.config.get("model"):
            raise TheaterError("请先配置绘图 API 密钥和模型，未发送请求。")

    def _client(self):
        return httpx.AsyncClient(
            timeout=httpx.Timeout(float(self.config.get("timeout", 180)), connect=15),
            trust_env=False,
            follow_redirects=False,
            transport=self.transport,
            proxy=self.config.get("proxy") or None,
        )

    async def _read(self, client, method: str, url: str, limit: int, **kwargs) -> bytes:
        async with client.stream(method, url, **kwargs) as response:
            if not 200 <= response.status_code < 300:
                # Never expose request headers, key-bearing URLs or upstream error bodies.
                raise TheaterError(
                    f"绘图或图片下载接口返回 HTTP {response.status_code}；未自动重试。"
                )
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > limit:
                    raise TheaterError("接口响应超出大小限制。")
            return bytes(chunks)

    async def download(self, url: str) -> bytes:
        await self.url_validator(url)
        async with self._client() as client:
            # Deliberately omit Authorization on result/QQ CDN downloads.
            return await self._read(client, "GET", url, MAX_BYTES)

    async def avatar(self, user_id: str) -> bytes:
        if not user_id.isdigit():
            raise TheaterError("头像用户ID必须为QQ数字ID。")
        data = await self.download(f"https://q.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=640")
        return await asyncio.to_thread(normalize_image, data, True)

    async def generate(self, prompt: str, references: list[bytes]) -> bytes:
        self.validate()
        protocol = self.config.get("protocol", "openai_chat")
        model = self.config["model"]
        base = self.config["api_base"]
        headers = {"Authorization": f"Bearer {self.key}"}
        kwargs = {}
        if protocol == "openai_chat":
            url = api_root(base, "v1") + "/chat/completions"
            content = [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64," + base64.b64encode(x).decode()},
                }
                for x in references
            ]
            content.append(
                {
                    "type": "text",
                    "text": prompt
                    + "\nPage aspect ratio: "
                    + self.config.get("aspect_ratio", "1:1")
                    + "; resolution: "
                    + self.config.get("resolution", "1K"),
                }
            )
            kwargs["json"] = {
                "model": model,
                "messages": [{"role": "user", "content": content}],
                "stream": False,
            }
        elif protocol == "gemini":
            url = (
                api_root(base, "v1beta")
                + "/models/"
                + quote(model, safe="-_.")
                + ":generateContent"
            )
            headers = {"x-goog-api-key": self.key}
            parts = [
                {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(x).decode()}}
                for x in references
            ] + [{"text": prompt}]
            kwargs["json"] = {
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {
                    "responseModalities": ["TEXT", "IMAGE"],
                    "imageConfig": {
                        "aspectRatio": self.config.get("aspect_ratio", "1:1"),
                        "imageSize": self.config.get("resolution", "1K"),
                    },
                },
            }
        else:
            edits = bool(references)
            if edits and self.config.get("generations_only", False):
                raise TheaterError(
                    "该入口配置为仅文生图；请关闭头像或使用支持 edits 的入口，不能静默丢弃头像。"
                )
            url = api_root(base, "v1") + ("/images/edits" if edits else "/images/generations")
            body = {"model": model, "prompt": prompt, "n": 1}
            for key in ("size", "quality", "output_format"):
                if self.config.get(key):
                    body[key] = self.config[key]
            if edits:
                kwargs["data"] = {k: str(v) for k, v in body.items()}
                field = "image" if len(references) == 1 else "image[]"
                kwargs["files"] = [
                    (field, (f"character-{i + 1}.png", raw, "image/png"))
                    for i, raw in enumerate(references)
                ]
            else:
                kwargs["json"] = body
        try:
            async with self._client() as client:
                response = await self._read(
                    client, "POST", url, MAX_RESPONSE, headers=headers, **kwargs
                )
            data = json.loads(response)
            raw = await self.extract(data)
            return await asyncio.to_thread(normalize_image, raw)
        except TheaterError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError):
            raise TheaterError(
                "网络中断或超时，上游是否出图未知；此次提交仍计入额度，不会重试。"
            ) from None
        except Exception:
            raise TheaterError("无法解析绘图结果；此次提交仍计入额度，不会重试。") from None

    async def extract(self, data: dict) -> bytes:
        candidates = []
        for item in data.get("data", []) if isinstance(data.get("data"), list) else []:
            if isinstance(item, dict):
                if item.get("b64_json"):
                    candidates.append(("base64", item["b64_json"]))
                if item.get("url"):
                    candidates.append(("url", item["url"]))
        for candidate in data.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                inline = part.get("inlineData", part.get("inline_data", {}))
                if inline.get("data") and str(
                    inline.get("mimeType", inline.get("mime_type", ""))
                ).startswith("image/"):
                    candidates.append(("base64", inline["data"]))

        def chat_images(value):
            if isinstance(value, dict):
                for k, v in value.items():
                    if (
                        k in {"image_url", "url"}
                        and isinstance(v, str)
                        and v.startswith(("https://", "data:image/"))
                    ):
                        candidates.append(("value", v))
                    else:
                        chat_images(v)
            elif isinstance(value, list):
                for v in value:
                    chat_images(v)
            elif isinstance(value, str):
                for v in re.findall(r"data:image/[^;,\s]+;base64,[A-Za-z0-9+/=]+", value):
                    candidates.append(("value", v))
                for v in re.findall(r"!\[[^\]]*\]\((https://[^\s)]+)\)", value):
                    candidates.append(("url", v))

        chat_images(data.get("choices", []))
        if not candidates:
            raise TheaterError("接口没有返回图片；未再次生成。")
        kind, value = candidates[0]
        if kind == "url" or value.startswith("https://"):
            return await self.download(value)
        if value.startswith("data:image/"):
            value = value.split(",", 1)[1]
        if len(value) > MAX_BYTES * 4 // 3 + 8:
            raise TheaterError("图片数据超过大小限制。")
        return base64.b64decode(value, validate=True)
