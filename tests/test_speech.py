import httpx
import pytest

from services.speech import SpeechError, SpeechToText


def _stt(handler, **kwargs):
    return SpeechToText(
        "key", httpx.AsyncClient(transport=httpx.MockTransport(handler)), model="whisper-x", **kwargs
    )


def _fields(request) -> dict:
    """Текстовые поля multipart-запроса: имя → значение."""
    fields = {}
    boundary = request.headers["content-type"].split("boundary=")[1].encode()
    for part in request.content.split(b"--" + boundary):
        head, _, body = part.partition(b"\r\n\r\n")
        if b'name="' in head and b"filename=" not in head:
            name = head.split(b'name="')[1].split(b'"')[0].decode()
            fields[name] = body.rstrip(b"\r\n").decode()
    return fields


async def test_transcribe_sends_multipart_and_returns_text():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.content
        seen["fields"] = _fields(request)
        return httpx.Response(200, json={"text": "  Привет,\n Эля  "})

    assert await _stt(handler).transcribe(b"OGGDATA") == "Привет, Эля"
    assert seen["auth"] == "Bearer key"
    assert b"OGGDATA" in seen["body"] and b"voice.ogg" in seen["body"]
    # Язык задаётся явно: иначе Whisper принимает короткие русские голосовые за английские.
    assert seen["fields"] == {"model": "whisper-x", "response_format": "verbose_json", "language": "ru"}


async def test_transcribe_language_is_configurable_and_auto_omits_it():
    seen = []

    def handler(request):
        seen.append(_fields(request))
        return httpx.Response(200, json={"text": "hello"})

    await _stt(handler, language="en").transcribe(b"x")
    await _stt(handler, language="").transcribe(b"x")
    assert seen[0]["language"] == "en"
    assert "language" not in seen[1]


async def test_transcribe_drops_hallucinated_silence_segments():
    def handler(request):
        return httpx.Response(200, json={
            "text": "Привет, Эля. Субтитры сделал DimaTorzok",
            "segments": [
                {"text": " Привет, Эля.", "no_speech_prob": 0.01, "avg_logprob": -0.2},
                {"text": " Субтитры сделал DimaTorzok", "no_speech_prob": 0.9, "avg_logprob": -1.4},
                # Уверенная речь с высоким no_speech_prob остаётся: отсев только по обоим признакам.
                {"text": " Как дела?", "no_speech_prob": 0.7, "avg_logprob": -0.3},
            ],
        })

    assert await _stt(handler).transcribe(b"x") == "Привет, Эля. Как дела?"


async def test_transcribe_only_silence_gives_empty_text():
    def handler(request):
        return httpx.Response(200, json={
            "text": "Thank you.",
            "segments": [{"text": " Thank you.", "no_speech_prob": 0.95, "avg_logprob": -1.2}],
        })

    assert await _stt(handler).transcribe(b"x") == ""


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: httpx.Response(401, json={"error": "bad key"}),
        lambda r: httpx.Response(200, text="not json"),
        lambda r: httpx.Response(200, json=["not", "an", "object"]),
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("down", request=r)),
    ],
)
async def test_transcribe_errors(handler):
    with pytest.raises(SpeechError):
        await _stt(handler).transcribe(b"x")
