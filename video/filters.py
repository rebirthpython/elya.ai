"""Команды ffmpeg для монтажа. Только сборка аргументов — запускает их video.service.

Монтаж идёт в два шага, чтобы не держать в памяти кадры длинного видео:
1. каждый кусок (фрагмент клипа или фраза без пауз) кодируется отдельно в одинаковый формат:
   один размер кадра, частота, звук 48 кГц стерео без сжатия — так куски склеиваются без щелчков;
2. куски склеиваются, поверх накладываются надписи, субтитры, затемнения и музыка.
"""

from __future__ import annotations

from video.ffmpeg import ClipInfo
from video.plan import EditPlan
from video.timeline import Piece

TELEGRAM_UPLOAD_LIMIT = 50 * 1024 * 1024  # бот может отправить файл до 50 МБ
TARGET_BYTES = 46 * 1024 * 1024  # с запасом на контейнер и неточность битрейта
AUDIO_KBPS = 160
TRANSITION = 0.3  # секунд затемнения на стыке фрагментов
FADE = 0.7  # секунд в начале и в конце видео

_COMMON = ["-y", "-hide_banner", "-nostdin", "-loglevel", "error"]

COLOR_FILTERS = {
    "bw": "hue=s=0",
    "warm": "colorbalance=rs=0.08:gs=0.02:bs=-0.08:rm=0.05:bm=-0.05",
    "cool": "colorbalance=rs=-0.06:bs=0.08:rm=-0.04:bm=0.06",
    "vivid": "eq=saturation=1.35:contrast=1.06",
    "vintage": "curves=preset=vintage",
    "bright": "eq=brightness=0.06:contrast=1.05:saturation=1.1",
}


def _num(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def atempo_chain(speed: float) -> list[str]:
    """atempo в старых ffmpeg принимает 0.5–2, поэтому большие изменения — цепочкой."""
    filters = []
    while speed > 2.0:
        filters.append("atempo=2")
        speed /= 2.0
    while speed < 0.5:
        filters.append("atempo=0.5")
        speed /= 0.5
    if abs(speed - 1.0) > 1e-3:
        filters.append(f"atempo={_num(speed)}")
    return filters


def _orient(plan: EditPlan) -> list[str]:
    chain = []
    if plan.stabilize:
        chain.append("deshake")
    chain += {90: ["transpose=clock"], 180: ["hflip", "vflip"], 270: ["transpose=cclock"]}.get(plan.rotate, [])
    if plan.mirror:
        chain.append("hflip")
    return chain


def video_graph(piece: Piece, clip: ClipInfo, plan: EditPlan, canvas: tuple[int, int], fps: int) -> str:
    """Граф для одного куска: скорость, поворот, вписывание в кадр, цвет, переходы -> [v]."""
    width, height = canvas
    src_w, src_h = (clip.height, clip.width) if plan.rotate in (90, 270) else (clip.width, clip.height)
    pre = ["setpts=(PTS-STARTPTS)/" + _num(piece.speed)] + _orient(plan)
    if plan.filter in COLOR_FILTERS:  # до вписывания в кадр, чтобы чёрные поля остались чёрными
        pre.append(COLOR_FILTERS[plan.filter])
    post = [f"fps={fps}", "setsar=1", "format=yuv420p"]
    if piece.duration >= 4 * TRANSITION:
        if piece.fade_in:
            post.append(f"fade=t=in:st=0:d={TRANSITION}")
        if piece.fade_out:
            post.append(f"fade=t=out:st={_num(piece.duration - TRANSITION)}:d={TRANSITION}")
    # Если кадров чуть не хватает до длительности куска, повторяем последний: -t обрежет лишнее.
    post.append("tpad=stop_mode=clone:stop_duration=1")

    source = f"[0:v:{clip.vstream}]"
    same_shape = abs(src_w / src_h - width / height) < 0.01
    if same_shape:
        geometry = [f"scale={width}:{height}"]
    elif plan.fit == "crop":
        geometry = [f"scale={width}:{height}:force_original_aspect_ratio=increase", f"crop={width}:{height}"]
    elif plan.fit == "pad":
        geometry = [
            f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2",
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black",
        ]
    else:  # blur: видео целиком, а пустые поля — размытая увеличенная копия кадра
        small_w, small_h = max(32, width // 8 // 2 * 2), max(32, height // 8 // 2 * 2)
        radius = max(1, min(8, min(small_w, small_h) // 4 - 1))
        return (
            f"{source}{','.join(pre)},split=2[bg][fg];"
            f"[bg]scale={small_w}:{small_h}:force_original_aspect_ratio=increase,crop={small_w}:{small_h},"
            f"boxblur={radius}:2,scale={width}:{height},eq=brightness=-0.06,setsar=1[bgb];"
            f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1[fgs];"
            f"[bgb][fgs]overlay=(main_w-overlay_w)/2:(main_h-overlay_h)/2,{','.join(post)}[v]"
        )
    return f"{source}{','.join(pre + geometry + post)}[v]"


def audio_graph(piece: Piece) -> str:
    chain = ["asetpts=PTS-STARTPTS", *atempo_chain(piece.speed), "aresample=48000",
             "aformat=sample_fmts=s16:channel_layouts=stereo"]
    if piece.duration >= 4 * TRANSITION:
        if piece.fade_in:
            chain.append(f"afade=t=in:st=0:d={TRANSITION}")
        if piece.fade_out:
            chain.append(f"afade=t=out:st={_num(piece.duration - TRANSITION)}:d={TRANSITION}")
    chain.append("apad")
    return "[0:a:0]" + ",".join(chain) + "[a]"


def piece_args(piece: Piece, clip: ClipInfo, plan: EditPlan, canvas: tuple[int, int], fps: int,
               output: str) -> list[str]:
    """Кусок -> .mkv (H.264 почти без потерь + несжатый звук)."""
    args = [*_COMMON, "-ss", _num(piece.start), "-t", _num(piece.source_duration), "-i", clip.file]
    graph = video_graph(piece, clip, plan, canvas, fps)
    if clip.has_audio:
        graph += ";" + audio_graph(piece)
        audio_map = "[a]"
    else:  # у куска без звука — тишина, чтобы все куски склеивались одинаково
        args += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        audio_map = "1:a"
    return [
        *args, "-filter_complex", graph, "-map", "[v]", "-map", audio_map,
        "-t", _num(piece.duration),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "17",
        "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
        output,
    ]


def concat_list(files: list[str]) -> str:
    return "".join(f"file '{name}'\n" for name in files)


def video_bitrate_kbps(duration: float) -> int:
    """Потолок битрейта, чтобы видео поместилось в лимит Telegram на отправку."""
    budget = TARGET_BYTES * 8 / 1000 / max(duration, 1.0) - AUDIO_KBPS
    return int(min(8000, max(250, budget)))


def final_args(
    plan: EditPlan,
    total: float,
    *,
    pieces_list: str,
    output: str,
    music: str | None = None,
    captions: str | None = None,
) -> list[str]:
    """Склейка кусков и всё, что идёт поверх: надписи, затемнения, громкость, музыка."""
    args = [*_COMMON, "-f", "concat", "-safe", "0", "-i", pieces_list]
    use_music = plan.music and music is not None and plan.output != "gif"
    if use_music:
        args += ["-stream_loop", "-1", "-i", music]

    graph: list[str] = []
    want_video = plan.output != "audio"
    want_audio = plan.output != "gif" and (plan.volume > 0 or use_music)
    fade = plan.fade and total >= 4 * FADE

    if want_video:
        chain = []
        if captions:
            chain.append(f"ass={captions}")
        if fade:
            chain += [f"fade=t=in:st=0:d={FADE}", f"fade=t=out:st={_num(total - FADE)}:d={FADE}"]
        chain.append("format=yuv420p")
        graph.append("[0:v]" + ",".join(chain) + "[v]")

    if want_audio:
        chain = []
        if plan.clean_audio:
            chain += ["afftdn=nf=-25", "dynaudnorm=f=200:g=11"]
        if plan.volume != 1:
            chain.append(f"volume={_num(plan.volume)}")
        if fade:
            chain += [f"afade=t=in:st=0:d={FADE}", f"afade=t=out:st={_num(total - FADE)}:d={FADE}"]
        chain.append("aformat=sample_fmts=fltp:channel_layouts=stereo")
        if use_music:
            graph.append("[0:a]" + ",".join(chain) + "[voice]")
            tail = min(2.0, total / 4)
            graph.append(
                f"[1:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"volume={_num(plan.music_volume)},atrim=duration={_num(total)},"
                f"afade=t=out:st={_num(total - tail)}:d={_num(tail)}[music]"
            )
            # amix делит громкость на число дорожек — возвращаем её и не даём звуку «хрипеть».
            graph.append("[voice][music]amix=inputs=2:duration=first:dropout_transition=0,"
                         "volume=2,alimiter=limit=0.95[a]")
        else:
            graph.append("[0:a]" + ",".join(chain) + "[a]")

    args += ["-filter_complex", ";".join(graph)]
    if want_video:
        args += ["-map", "[v]"]
    if want_audio:
        args += ["-map", "[a]"]
    args += ["-t", _num(total)]
    if want_video:
        rate = video_bitrate_kbps(total)
        args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                 "-maxrate", f"{rate}k", "-bufsize", f"{rate * 2}k", "-pix_fmt", "yuv420p"]
    else:
        args += ["-vn"]
    if want_audio:
        args += ["-c:a", "aac", "-b:a", f"{AUDIO_KBPS if want_video else 192}k"]
    else:
        args += ["-an"]
    if output.endswith((".mp4", ".m4a")):
        args += ["-movflags", "+faststart"]
    return [*args, output]


def speech_audio_args(source: str, output: str, *, concat: bool = False) -> list[str]:
    """Звук для распознавания речи: моно 16 кГц — маленький файл, Whisper больше и не нужно."""
    head = ["-f", "concat", "-safe", "0", "-i", source] if concat else ["-i", source]
    return [*_COMMON, *head, "-vn", "-map", "0:a:0", "-ac", "1", "-ar", "16000",
            "-c:a", "aac", "-b:a", "48k", output]
