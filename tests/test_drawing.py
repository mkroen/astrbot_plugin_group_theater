import base64
import json

import httpx
import pytest

from core.drawing import DrawingClient, api_root, normalize_image
from core.story import TheaterError


@pytest.mark.parametrize("protocol", ["openai_chat", "gemini", "openai_images"])
async def test_native_protocols_and_decoding(protocol, png):
    requests = []

    def handler(request):
        requests.append(request)
        b64 = base64.b64encode(png).decode()
        if protocol == "gemini":
            assert request.headers["x-goog-api-key"] == "TEST_ONLY"
            assert "TEST_ONLY" not in str(request.url)
            body = json.loads(request.content)
            assert body["generationConfig"]["responseModalities"] == ["TEXT", "IMAGE"]
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [{"inlineData": {"mimeType": "image/png", "data": b64}}]
                            }
                        }
                    ]
                },
            )
        assert request.headers["Authorization"] == "Bearer TEST_ONLY"
        if protocol == "openai_chat":
            assert str(request.url) == "https://example.test/v1/chat/completions"
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": "![image](data:image/png;base64," + b64 + ")"}}
                    ]
                },
            )
        assert str(request.url) == "https://example.test/v1/images/generations"
        return httpx.Response(200, json={"data": [{"b64_json": b64}]})

    client = DrawingClient(
        {"protocol": protocol, "api_base": "https://example.test/v1", "model": "image-test"},
        "TEST_ONLY",
        transport=httpx.MockTransport(handler),
    )
    output = await client.generate("four panels", [])
    assert output.startswith(b"\x89PNG") and len(requests) == 1


async def test_openai_edits_keeps_multiple_references(png):
    def handler(request):
        assert request.url.path == "/v1/images/edits"
        assert request.content.count(b'name="image[]"') == 2
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png).decode()}]})

    client = DrawingClient(
        {"protocol": "openai_images", "api_base": "https://example.test", "model": "image-test"},
        "TEST_ONLY",
        transport=httpx.MockTransport(handler),
    )
    await client.generate("two people", [png, png])


async def test_http_failure_redacts_secret_and_does_not_retry():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, text="TEST_ONLY detailed upstream secret")

    client = DrawingClient(
        {"api_base": "https://example.test", "model": "test"},
        "TEST_ONLY",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(TheaterError) as e:
        await client.generate("x", [])
    assert "429" in str(e.value) and "TEST_ONLY" not in str(e.value)
    assert len(calls) == 1


async def test_result_download_does_not_forward_api_key(png):
    calls = []

    async def allow(url):
        assert url == "https://cdn.example.test/p.png"

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"data": [{"url": "https://cdn.example.test/p.png"}]})
        assert "authorization" not in request.headers
        return httpx.Response(200, content=png)

    client = DrawingClient(
        {"protocol": "openai_images", "api_base": "https://api.example.test", "model": "test"},
        "TEST_ONLY",
        transport=httpx.MockTransport(handler),
        url_validator=allow,
    )
    await client.generate("x", [])
    assert [r.method for r in calls] == ["POST", "GET"]


async def test_timeout_submits_once():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("TEST_ONLY")

    client = DrawingClient(
        {"api_base": "https://example.test", "model": "test"},
        "TEST_ONLY",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(TheaterError, match="不会重试"):
        await client.generate("x", [])
    assert len(calls) == 1


def test_html_is_not_an_image():
    with pytest.raises(TheaterError):
        normalize_image(b"<html>not image</html>")


@pytest.mark.parametrize(
    "base,version,expected",
    [
        ("https://x.test/v1/", "v1", "https://x.test/v1"),
        ("https://x.test", "v1beta", "https://x.test/v1beta"),
        ("https://x.test/v1beta", "v1", "https://x.test/v1"),
    ],
)
def test_no_duplicate_version_path(base, version, expected):
    assert api_root(base, version) == expected
