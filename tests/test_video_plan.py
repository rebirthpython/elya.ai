"""Монтаж без ffmpeg: план от модели, куски, размер кадра, субтитры, разбор вывода ffprobe."""

import pytest

from services.speech import Timed, TimedTranscript, parse_verbose
from video.captions import ass_escape, ass_time, build_ass, build_cues
from video.ffmpeg import ClipInfo, FFmpegError, parse_probe, parse_silence
from video.filters import atempo_chain, final_args, video_bitrate_kbps, video_graph
from video.plan import (
    MUSIC_HINT,
    EditPlan,
    PlanContext,
    Segment,
    TextOverlay,
    default_plan,
    describe,
    fmt_time,
    parse_plan,
    parse_time,
)
from video.timeline import Piece, build_pieces, canvas_size, output_fps, speech_intervals

CLIPS = [
    ClipInfo("clip-1.mp4", 40.0, 1920, 1080, 30.0, True),
    ClipInfo("clip-2.mp4", 12.0, 1080, 1920, 25.0, False),
]
CTX = PlanContext(clips=CLIPS, has_music=False, can_text=True, can_subtitles=True)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(5, 5.0), ("0:05", 5.0), ("1:15", 75.0), ("1:02:03", 3723.0), ("12,5", 12.5), ("0:07.5", 7.5),
     ("15 сек", 15.0), ("20s", 20.0), (None, None), ("до конца", None), ("end", None)],
)
def test_parse_time(value, expected):
    assert parse_time(value) == expected


@pytest.mark.parametrize("value", ["полминуты", -3, True, [1]])
def test_parse_time_rejects_garbage(value):
    with pytest.raises(ValueError):
        parse_time(value)


def test_fmt_time():
    assert [fmt_time(x) for x in (0, 5, 12.5, 75, 3725)] == ["0:00", "0:05", "0:12.5", "1:15", "1:02:05"]


def test_model_plan_is_merged_onto_current_and_clamped():
    base = default_plan(CLIPS).but(format="9:16", subtitles=True)
    parsed = parse_plan(
        {"timeline": [{"clip": "1", "from": "0:05", "to": 99, "speed": "x1.5"}, {"clip": 2, "to": None}],
         "texts": [{"text": "Лето 2026", "from": 0, "to": "0:03", "position": "верх"}], "filter": "warm"},
        CTX, base,
    )
    assert parsed.issues == [] and parsed.notes == []
    plan = parsed.plan
    assert plan.timeline == (Segment(1, 5.0, 40.0, 1.5), Segment(2, 0.0, 12.0, 1.0))  # to=99 -> конец клипа
    assert plan.texts == (TextOverlay("Лето 2026", 0.0, 3.0, "top"),)
    assert plan.format == "9:16" and plan.subtitles  # чего нет в ответе — остаётся как было
    assert plan.filter == "warm"


def test_model_mistakes_become_issues_for_repair():
    parsed = parse_plan(
        {"timeline": [{"clip": 5, "from": 0}, {"clip": 1, "from": "0:50"}, {"clip": 1, "from": 10, "to": 5}],
         "format": "21:9", "rotate": 45},
        CTX, default_plan(CLIPS),
    )
    assert len(parsed.issues) == 5
    assert any("клипа 5 нет" in i for i in parsed.issues)
    assert any("длится 0:40" in i for i in parsed.issues)
    # Ошибочное не применилось: остался прежний таймлайн и формат.
    assert parsed.plan.timeline == default_plan(CLIPS).timeline and parsed.plan.format == "original"


def test_unavailable_features_are_switched_off_with_explanation():
    ctx = PlanContext(clips=CLIPS, has_music=False, can_text=False, can_subtitles=False,
                      subtitles_reason="нужен ключ GROQ_API_KEY")
    parsed = parse_plan({"music": True, "subtitles": "да", "texts": ["Привет"], "speed": 9,
                         "timeline": [{"clip": 1, "speed": 10}]}, ctx, default_plan(CLIPS))
    plan = parsed.plan
    assert not plan.music and not plan.subtitles and plan.texts == ()
    assert plan.timeline[0].speed == 4.0
    assert MUSIC_HINT in parsed.notes
    assert any("GROQ_API_KEY" in n for n in parsed.notes)
    assert any("от ×0.25 до ×4" in n for n in parsed.notes)


def test_aliases_and_flags():
    parsed = parse_plan({"format": "vertical", "output": "кружок", "volume": "150%", "music_volume": 30,
                         "need_transcript": True, "note": "Спецэффектов не умею"}, CTX, default_plan(CLIPS))
    assert parsed.plan.output == "circle" and parsed.plan.format == "1:1"  # кружок всегда квадратный
    assert parsed.plan.volume == 1.5 and parsed.plan.music_volume == 0.3  # проценты -> доли
    assert parsed.need_transcript and parsed.note == "Спецэффектов не умею"
    assert parse_plan({"format": "9x16"}, CTX, default_plan(CLIPS)).plan.format == "9:16"


def test_plan_roundtrip_and_key():
    plan = default_plan(CLIPS).but(texts=(TextOverlay("Привет", 1, None, "center"),), remove_pauses=True)
    again = EditPlan.from_dict(plan.to_dict())
    assert again == plan and again.key == plan.key
    assert plan.key != plan.but(remove_pauses=False).key


def test_describe_is_written_by_code():
    plan = default_plan(CLIPS).but(remove_pauses=True, format="9:16", music=True, volume=0)
    lines = describe(plan, CLIPS, removed_pauses=12.0)
    assert lines[0] == "🔗 Склеены клипы 1–2"
    assert "🤐 Паузы вырезаны (−0:12)" in lines
    assert "📐 Формат вертикальный 9:16, размытый фон" in lines
    assert "🔇 Звук видео выключен" in lines and any(line.startswith("🎵 Музыка") for line in lines)
    cut = plan.but(timeline=(Segment(1, 5, 30, 2.0),), remove_pauses=False, format="original", music=False, volume=1)
    assert describe(cut, CLIPS) == ["✂️ Фрагмент: клип 1 0:05–0:30 ×2"]
    assert describe(default_plan(CLIPS[:1]), CLIPS[:1]) == []  # ничего не меняли


def test_speech_intervals_pad_merge_and_drop():
    silences = [(0.0, 1.0), (3.0, 3.2), (5.0, 8.0), (9.9, 10.0)]
    assert speech_intervals(silences, 10.0) == [(0.85, 5.15), (7.85, 10.0)]
    assert speech_intervals([], 4.0) == [(0.0, 4.0)]
    assert speech_intervals([(0.0, 4.0)], 4.0) == []


def test_build_pieces_removes_pauses_and_marks_transitions():
    plan = default_plan(CLIPS).but(remove_pauses=True, transitions=True)
    pieces, trimmed = build_pieces(plan, CLIPS, 30, speech={1: [(1.0, 5.0), (10.0, 20.0)]}, max_seconds=600)
    assert not trimmed
    assert [(p.clip, p.start, round(p.duration, 3)) for p in pieces] == [(1, 1.0, 4.0), (1, 10.0, 10.0), (2, 0.0, 12.0)]
    assert [(p.fade_in, p.fade_out) for p in pieces] == [(False, False), (False, True), (True, False)]


def test_build_pieces_quantizes_to_frames_and_respects_limits():
    plan = EditPlan(timeline=(Segment(1, 0.0, 10.01, 2.0),))
    (piece,), _ = build_pieces(plan, CLIPS, 30, max_seconds=600)
    assert piece.duration * 30 == round(piece.duration * 30)  # целое число кадров
    assert piece.source_duration == pytest.approx(piece.duration * 2)

    pieces, trimmed = build_pieces(default_plan(CLIPS), CLIPS, 30, max_seconds=45)
    assert trimmed and sum(p.duration for p in pieces) == pytest.approx(45.0)
    circle, trimmed = build_pieces(default_plan(CLIPS * 2).but(output="circle"), CLIPS * 2, 30, max_seconds=600)
    assert trimmed and sum(p.duration for p in circle) == pytest.approx(60.0)


def test_silent_everywhere_keeps_the_video():
    plan = EditPlan(timeline=(Segment(1, 0.0, 40.0),), remove_pauses=True)
    pieces, _ = build_pieces(plan, CLIPS, 30, speech={1: []}, max_seconds=600)
    assert len(pieces) == 1 and pieces[0].duration == pytest.approx(40.0)


@pytest.mark.parametrize(
    ("changes", "expected"),
    [({}, (1920, 1080)), ({"format": "9:16"}, (1080, 1920)), ({"format": "1:1"}, (1080, 1080)),
     ({"format": "4:5"}, (1080, 1350)), ({"rotate": 90}, (1080, 1920)), ({"output": "circle"}, (640, 640)),
     ({"output": "gif"}, (720, 404))],
)
def test_canvas_size(changes, expected):
    assert canvas_size(default_plan(CLIPS).but(**changes), CLIPS, 1920) == expected


def test_canvas_never_upscales_and_respects_max_side():
    small = [ClipInfo("a.mp4", 5, 640, 360)]
    assert canvas_size(default_plan(small).but(format="9:16"), small, 1920) == (360, 640)
    assert canvas_size(default_plan(CLIPS), CLIPS, 1280) == (1280, 720)
    assert output_fps(default_plan(CLIPS), CLIPS) == 30


def test_atempo_chain_and_bitrate():
    assert atempo_chain(1.0) == []
    assert atempo_chain(4.0) == ["atempo=2", "atempo=2"]
    assert atempo_chain(0.25) == ["atempo=0.5", "atempo=0.5"]
    assert atempo_chain(1.5) == ["atempo=1.5"]
    assert video_bitrate_kbps(10) == 8000 and video_bitrate_kbps(600) < 700


def test_graphs_by_fit():
    piece = Piece(1, 0, 4, 4)
    horizontal = CLIPS[0]
    vertical = default_plan(CLIPS).but(format="9:16")
    assert "split=2" in video_graph(piece, horizontal, vertical, (1080, 1920), 30)
    assert "crop=1080:1920" in video_graph(piece, horizontal, vertical.but(fit="crop"), (1080, 1920), 30)
    assert "pad=1080:1920" in video_graph(piece, horizontal, vertical.but(fit="pad"), (1080, 1920), 30)
    plain = video_graph(piece, horizontal, default_plan(CLIPS).but(filter="bw", rotate=180), (1920, 1080), 30)
    assert plain.index("hue=s=0") < plain.index("scale=")  # цвет — до вписывания в кадр
    assert "hflip,vflip" in plain


def test_final_args_mute_gif_and_music():
    plan = default_plan(CLIPS)
    muted = final_args(plan.but(volume=0), 10, pieces_list="p.txt", output="r.mp4")
    assert "-an" in muted
    gif = final_args(plan.but(output="gif", music=True), 10, pieces_list="p.txt", output="r.mp4", music="m.mp3")
    assert "-an" in gif and "m.mp3" not in gif
    mixed = final_args(plan.but(music=True), 10, pieces_list="p.txt", output="r.mp4", music="m.mp3")
    graph = mixed[mixed.index("-filter_complex") + 1]
    assert "amix=inputs=2" in graph and "-stream_loop" in mixed
    audio = final_args(plan.but(output="audio"), 10, pieces_list="p.txt", output="r.m4a")
    assert "-vn" in audio and "[v]" not in audio


def test_cues_from_words_break_on_pauses_and_length():
    words = [Timed(0.0, 0.3, "Привет,"), Timed(0.35, 0.6, "друзья."), Timed(2.0, 2.3, "Сегодня"),
             Timed(2.35, 2.6, "покажу"), Timed(2.65, 3.0, "монтаж")]
    cues = build_cues(TimedTranscript("…", words=words), max_chars=30)
    assert [c.text for c in cues] == ["Привет, друзья.", "Сегодня покажу монтаж"]
    assert cues[0].end <= cues[1].start
    short = build_cues(TimedTranscript("…", words=words), max_chars=12)
    assert all(len(c.text) <= 15 for c in short) and len(short) >= 4


def test_cues_from_segments_share_time_by_length():
    seg = Timed(0.0, 4.0, "раз два три четыре пять шесть семь восемь")
    cues = build_cues(TimedTranscript("…", segments=[seg]), max_chars=20, total=4.0)
    assert len(cues) >= 2 and cues[0].start == 0.0 and cues[-1].end <= 4.0


def test_ass_file():
    assert ass_time(3725.456) == "1:02:05.46"
    assert ass_escape("a {\\b1} b\n c") == "a (/b1) b c"
    text = build_ass(1080, 1920, cues=build_cues(TimedTranscript("", words=[Timed(0, 1, "Слово")])),
                     texts=[TextOverlay("Заголовок", 0, None, "top")], total=10.0)
    assert "PlayResY: 1920" in text
    assert "Dialogue: 1,0:00:00.00,0:00:10.00,top,,0,0,0,,Заголовок" in text
    assert ",Caption,,0,0,0,,Слово" in text


def test_parse_probe_handles_phone_rotation_and_cover_art():
    data = {
        "format": {"duration": "12.5"},
        "streams": [
            {"codec_type": "video", "width": 300, "height": 300, "disposition": {"attached_pic": 1}},
            {"codec_type": "video", "width": 1920, "height": 1080, "avg_frame_rate": "30000/1001",
             "side_data_list": [{"rotation": -90}]},
            {"codec_type": "audio"},
        ],
    }
    info = parse_probe(data, "clip.mp4")
    assert (info.width, info.height, info.vstream, info.has_audio) == (1080, 1920, 1, True)
    assert info.fps == pytest.approx(29.97, abs=0.01) and info.orientation == "вертикальное"
    music = parse_probe({"format": {"duration": "180"}, "streams": [{"codec_type": "audio"}]}, "m.mp3")
    assert not music.has_video and music.duration == 180
    with pytest.raises(FFmpegError):
        parse_probe({"streams": []}, "x")


def test_parse_silence_including_silence_till_the_end():
    log = ("[silencedetect @ 0x1] silence_start: 1.5\n[silencedetect @ 0x1] silence_end: 3.25 | silence_duration: 1.75\n"
           "[silencedetect @ 0x1] silence_start: 9.1\n")
    assert parse_silence(log, 10.0) == [(1.5, 3.25), (9.1, 10.0)]


def test_whisper_hallucinations_are_dropped():
    data = {
        "text": "Привет. Субтитры сделал DimaTorzok",
        "segments": [
            {"start": 0, "end": 1.5, "text": " Привет.", "no_speech_prob": 0.01},
            {"start": 5, "end": 7, "text": "Субтитры сделал DimaTorzok", "no_speech_prob": 0.2},
            {"start": 8, "end": 9, "text": "шум", "no_speech_prob": 0.9, "avg_logprob": -1.4},
            {"start": 10, "end": 11, "text": "Тихо, но слова", "no_speech_prob": 0.7, "avg_logprob": -0.3},
        ],
        "words": [{"word": "Привет.", "start": 0.1, "end": 0.9}, {"word": "Субтитры", "start": 5.1, "end": 5.6}],
    }
    transcript = parse_verbose(data)
    assert transcript.text == "Привет. Тихо, но слова"  # уверенный текст не выбрасывается
    assert [s.text for s in transcript.segments] == ["Привет.", "Тихо, но слова"]
    assert [w.text for w in transcript.words] == ["Привет."]
