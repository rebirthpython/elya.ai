import httpx
import pytest

from services.speech import SpeechError, SpeechToText


def _stt(handler):
    return SpeechToText("key", httpx.AsyncClient(transport=httpx.MockTransport(handler)), model="whisper-x")


async def test_transcribe_sends_multipart_and_returns_text():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.content
        return httpx.Response(200, json={"text": "  Привет,\n Эля  "})

    assert await _stt(handler).transcribe(b"OGGDATA") == "Привет, Эля"
    assert seen["auth"] == "Bearer key"
    assert b"whisper-x" in seen["body"] and b"OGGDATA" in seen["body"] and b"voice.ogg" in seen["body"]


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: httpx.Response(401, json={"error": "bad key"}),
        lambda r: httpx.Response(200, text="not json"),
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("down", request=r)),
    ],
)
async def test_transcribe_errors(handler):
    with pytest.raises(SpeechError):
        await _stt(handler).transcribe(b"x")
