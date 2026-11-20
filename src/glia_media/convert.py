"""MP4 하나를 로컬 720p HLS로 변환한다.

가로 화면은 세로를 720으로, 세로 화면은 가로를 720으로 맞춘다.
비율은 유지하고 화면을 자르거나 여백을 넣지 않는다.
원본 파일은 읽기만 한다.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

TARGET_EDGE = 720
SEGMENT_SECONDS = 6
ENCODE_SETTINGS = {
    "target_edge": TARGET_EDGE,
    "segment_seconds": SEGMENT_SECONDS,
    "video_codec": "h264",
    "video_profile": "high",
    "crf": 23,
    "preset": "medium",
    "pixel_format": "yuv420p",
    "audio_codec": "aac",
    "audio_profile": "aac_low",
    "audio_bitrate": "128k",
    "closed_gop": True,
}


class ConvertError(Exception):
    def __init__(
        self,
        summary: str,
        detail: str = "",
        record: Path | None = None,
    ) -> None:
        self.summary = summary
        self.detail = detail
        self.record = record
        super().__init__(summary)


@dataclass(frozen=True)
class ConvertResult:
    playlist: Path
    width: int
    height: int
    duration_seconds: float
    segment_count: int


def display_size(width: int, height: int, rotation: int) -> tuple[int, int]:
    if abs(rotation) % 180 == 90:
        return height, width
    return width, height


def rotation_degrees(stream: dict) -> int:
    for item in stream.get("side_data_list") or []:
        if "rotation" in item:
            return int(round(float(item["rotation"])))
    tags = stream.get("tags") or {}
    if "rotate" in tags:
        return int(round(float(tags["rotate"])))
    return 0


def scale_filter(width: int, height: int) -> str:
    if width >= height:
        scale = f"scale=-2:{TARGET_EDGE}:flags=lanczos"
    else:
        scale = f"scale={TARGET_EDGE}:-2:flags=lanczos"
    return f"{scale},setsar=1"


def primary_video_stream(info: dict) -> dict | None:
    for stream in info.get("streams") or []:
        if stream.get("codec_type") != "video" or _is_attached_picture(stream):
            continue
        return stream
    return None


def primary_audio_stream(info: dict) -> dict | None:
    for stream in info.get("streams") or []:
        if stream.get("codec_type") == "audio":
            return stream
    return None


def matches_720p(source_width: int, source_height: int, width: int, height: int) -> bool:
    if width % 2 or height % 2 or width < 2 or height < 2:
        return False
    if source_width >= source_height:
        if height != TARGET_EDGE:
            return False
    elif width != TARGET_EDGE:
        return False
    source_ratio = source_width / source_height
    output_ratio = width / height
    return abs(source_ratio - output_ratio) < 0.02


def convert(source: Path, output_dir: Path) -> ConvertResult:
    source = source.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    _require_tools()
    _require_source(source)
    _require_output_dir(source, output_dir)

    info = _probe(source)
    video = _video_stream(info, source)
    audio = primary_audio_stream(info)
    if audio is None:
        raise ConvertError(f"영상에 오디오가 없습니다. 소리 없는 파일은 변환하지 않습니다: {source}")

    coded_width = int(video["width"])
    coded_height = int(video["height"])
    shown_width, shown_height = display_size(
        coded_width,
        coded_height,
        rotation_degrees(video),
    )
    source_digest = _sha256(source)
    output_dir.mkdir()
    try:
        return _convert_in_place(
            source,
            output_dir,
            video,
            audio,
            shown_width,
            shown_height,
            source_digest,
        )
    except ConvertError as error:
        _remove_media_outputs(output_dir)
        record = _write_failure(output_dir, error.summary, error.detail)
        raise ConvertError(error.summary, error.detail, record) from None


def _convert_in_place(
    source: Path,
    output_dir: Path,
    video: dict,
    audio: dict,
    shown_width: int,
    shown_height: int,
    source_digest: str,
) -> ConvertResult:
    command = _ffmpeg_command(
        source,
        scale_filter(shown_width, shown_height),
        video,
        audio,
    )
    completed = subprocess.run(
        command,
        cwd=output_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if _sha256(source) != source_digest:
        raise ConvertError("원본 파일이 변환 중에 바뀌었습니다. 결과를 폐기했습니다.")
    if completed.returncode != 0:
        raise ConvertError(
            "FFmpeg가 변환을 끝내지 못했습니다.",
            _tail(completed.stderr),
        )

    playlist = output_dir / "index.m3u8"
    segments = _playlist_segments(playlist)
    output = _probe(playlist)
    output_video = _video_stream(output, playlist)
    if primary_audio_stream(output) is None:
        raise ConvertError("변환 결과에 오디오가 없습니다.")
    output_width = int(output_video["width"])
    output_height = int(output_video["height"])
    shown_output = display_size(
        output_width,
        output_height,
        rotation_degrees(output_video),
    )
    if not matches_720p(shown_width, shown_height, *shown_output):
        raise ConvertError(
            "변환 결과가 720p 규칙과 다릅니다. "
            f"입력 화면 {shown_width}x{shown_height}, 결과 {shown_output[0]}x{shown_output[1]}."
        )
    if output_video.get("codec_name") != "h264":
        raise ConvertError("변환된 영상이 H.264가 아닙니다.")
    output_audio = primary_audio_stream(output)
    if output_audio is None or output_audio.get("codec_name") != "aac":
        raise ConvertError("변환된 오디오가 AAC가 아닙니다.")

    source_duration = _duration(_probe(source))
    output_duration = _duration(output)
    if abs(source_duration - output_duration) > 1:
        raise ConvertError(
            "변환 결과의 길이가 원본과 너무 다릅니다. "
            f"원본 {source_duration:.1f}초, 결과 {output_duration:.1f}초."
        )

    decode_error = _decode_error(playlist)
    if decode_error:
        raise ConvertError("변환 결과를 재생 검사하는 중 오류가 났습니다.", decode_error)
    if _sha256(source) != source_digest:
        raise ConvertError("원본 파일이 변환 중에 바뀌었습니다. 결과를 폐기했습니다.")

    return ConvertResult(
        playlist=playlist,
        width=shown_output[0],
        height=shown_output[1],
        duration_seconds=output_duration,
        segment_count=len(segments),
    )


def _ffmpeg_command(source: Path, video_filter: str, video: dict, audio: dict) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-autorotate",
        "-i",
        str(source),
        "-map",
        f"0:{_stream_index(video)}",
        "-map",
        f"0:{_stream_index(audio)}",
        "-vf",
        video_filter,
        *_color_args(video),
        "-c:v",
        "libx264",
        "-pix_fmt",
        ENCODE_SETTINGS["pixel_format"],
        "-profile:v",
        ENCODE_SETTINGS["video_profile"],
        "-preset",
        ENCODE_SETTINGS["preset"],
        "-crf",
        str(ENCODE_SETTINGS["crf"]),
        "-x264-params",
        "open-gop=0:forced-idr=1",
        "-c:a",
        "aac",
        "-profile:a",
        ENCODE_SETTINGS["audio_profile"],
        "-b:a",
        ENCODE_SETTINGS["audio_bitrate"],
        "-sc_threshold",
        "0",
        "-force_key_frames",
        f"expr:gte(t,n_forced*{SEGMENT_SECONDS})",
        "-hls_time",
        str(SEGMENT_SECONDS),
        "-hls_playlist_type",
        "vod",
        "-hls_list_size",
        "0",
        "-hls_flags",
        "independent_segments+temp_file",
        "-hls_segment_filename",
        "segment-%03d.ts",
        "-f",
        "hls",
        "index.m3u8",
    ]


def _require_tools() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
    if missing:
        names = ", ".join(missing)
        raise ConvertError(f"필요한 프로그램을 찾을 수 없습니다: {names}")


def _require_source(source: Path) -> None:
    if not source.exists():
        raise ConvertError(f"원본 파일을 찾을 수 없습니다: {source}")
    if not source.is_file():
        raise ConvertError(f"원본 경로가 파일이 아닙니다: {source}")
    if source.suffix.lower() != ".mp4":
        raise ConvertError(f"원본은 MP4 파일이어야 합니다: {source}")


def _require_output_dir(source: Path, output_dir: Path) -> None:
    if output_dir == source:
        raise ConvertError("출력 경로가 원본 파일과 같습니다. 원본은 변경하지 않습니다.")
    if output_dir.exists() and not output_dir.is_dir():
        raise ConvertError(f"출력 경로가 폴더가 아닙니다: {output_dir}")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ConvertError(f"출력 폴더가 비어 있지 않습니다: {output_dir}")
    if not output_dir.exists() and not output_dir.parent.exists():
        raise ConvertError(f"출력 폴더의 상위 폴더가 없습니다: {output_dir.parent}")


def _probe(path: Path) -> dict:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise ConvertError(
            f"영상 정보를 읽지 못했습니다: {path}",
            _tail(completed.stderr),
        )
    return json.loads(completed.stdout)


def _video_stream(info: dict, path: Path) -> dict:
    stream = primary_video_stream(info)
    if stream is None:
        raise ConvertError(f"영상 스트림이 없습니다: {path}")
    return stream


def _is_attached_picture(stream: dict) -> bool:
    return bool((stream.get("disposition") or {}).get("attached_pic"))


def _stream_index(stream: dict) -> int:
    if "index" in stream:
        return int(stream["index"])
    raise ConvertError("스트림 번호를 읽지 못했습니다.")


def _color_args(stream: dict) -> list[str]:
    ignored = {"unknown", "unspecified", "reserved"}
    fields = (
        ("color_primaries", "-color_primaries"),
        ("color_transfer", "-color_trc"),
        ("color_space", "-colorspace"),
        ("color_range", "-color_range"),
    )
    args: list[str] = []
    for key, flag in fields:
        value = stream.get(key)
        if value and str(value) not in ignored:
            args.extend([flag, str(value)])
    return args


def _duration(info: dict) -> float:
    raw = (info.get("format") or {}).get("duration")
    if raw is None:
        raise ConvertError("영상 길이를 읽지 못했습니다.")
    return float(raw)


def _playlist_segments(playlist: Path) -> list[Path]:
    if not playlist.is_file():
        raise ConvertError("재생 목록 index.m3u8이 없습니다.")
    text = playlist.read_text(encoding="utf-8")
    if not text.startswith("#EXTM3U") or "#EXT-X-ENDLIST" not in text:
        raise ConvertError("재생 목록이 완성된 HLS 형식이 아닙니다.")
    segments: list[Path] = []
    for line in text.splitlines():
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        if "/" in name or "\\" in name or name.startswith("."):
            raise ConvertError(f"재생 목록의 조각 이름이 올바르지 않습니다: {name}")
        segment = playlist.parent / name
        if not segment.is_file() or segment.stat().st_size == 0:
            raise ConvertError(f"재생 목록에 있는 조각 파일이 없습니다: {name}")
        segments.append(segment)
    if not segments:
        raise ConvertError("HLS 조각 파일이 없습니다.")
    return segments


def _decode_error(playlist: Path) -> str:
    completed = subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "error", "-i", str(playlist), "-f", "null", "-"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        return _tail(completed.stderr) or "재생 검사가 실패했습니다."
    return ""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_failure(output_dir: Path, summary: str, detail: str) -> Path:
    lines = [summary.strip(), ""]
    if detail.strip():
        lines.extend(["자세한 내용:", detail.strip(), ""])
    record = output_dir / "failure.txt"
    record.write_text("\n".join(lines), encoding="utf-8")
    return record


def _remove_media_outputs(output_dir: Path) -> None:
    for name in ("index.m3u8", "index.m3u8.tmp"):
        path = output_dir / name
        if path.exists():
            path.unlink()
    for path in output_dir.glob("segment-*.ts"):
        path.unlink()


def _tail(text: str, limit: int = 30) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-limit:])
