"""PMN-003: local VOICEVOX narration and deterministic video assembly."""
from __future__ import annotations

import io
import json
import http.client
import socket
import hashlib
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFont

VERSION = "0.3.8"
PROJECT_SCHEMA_VERSION = 3
USER_SETTINGS_SCHEMA_VERSION = 1
VOICEVOX_CONNECT_TIMEOUT = 5.0
VOICEVOX_READ_TIMEOUT = 20.0
VOICEVOX_SYNTHESIS_READ_TIMEOUT = 180.0
FFMPEG_STALL_TIMEOUT = 90.0
OVERLAP_TOLERANCE = 0.05
# In a PyInstaller build, source modules live under the bundled runtime directory.
# User-facing state/output must instead live next to the executable.
ROOT = (Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent)
NEMO_ENGINE = "VOICEVOX Nemo（女声2など）"
STANDARD_ENGINE = "VOICEVOX（キャラクターの声）"
CUSTOM_ENGINE = "カスタム（詳細設定）"
ENGINE_PRESETS = {NEMO_ENGINE: "http://127.0.0.1:50121",
                  STANDARD_ENGINE: "http://127.0.0.1:50021"}
DEFAULT_ENGINE_URL = ENGINE_PRESETS[NEMO_ENGINE]
DEFAULT_SPEAKER_LABEL = "女声2 / ノーマル"
SHORT_MODE = "ショート動画（9:16）"
NORMAL_MODE = "通常動画（16:9）"
OUTPUT_MODES = (SHORT_MODE, NORMAL_MODE)
SHORT_PRESETS = ("縦・本番 1080×1920", "縦・試作 720×1280")
NORMAL_PRESETS = ("横 1920×1080",)
PRESETS = {"縦・本番 1080×1920": (1080, 1920), "縦・試作 720×1280": (720, 1280),
           "横 1920×1080": (1920, 1080)}
BG = "#101722"
ACCENT = "#72dec2"

THEMES = {
    "研究所ダーク": {"bg": "#101722", "text": "#f4f7fa", "accent": "#72dec2", "muted": "#93a0ad"},
    "モノクロ": {"bg": "#111111", "text": "#f5f5f5", "accent": "#a7a7a7", "muted": "#8a8a8a"},
    "ライト": {"bg": "#f4f5f7", "text": "#171a1f", "accent": "#475569", "muted": "#6b7280"},
    "ネイビー": {"bg": "#0b1730", "text": "#f4f8ff", "accent": "#69b7ff", "muted": "#92a6c2"},
    "ウォーム": {"bg": "#241915", "text": "#fff4df", "accent": "#e9a15b", "muted": "#c3a78f"},
}
DEFAULT_THEME = "研究所ダーク"

def theme_colors(settings) -> dict[str, str]:
    return THEMES.get(getattr(settings, "theme_id", DEFAULT_THEME), THEMES[DEFAULT_THEME])


class FactoryError(Exception):
    pass


class Cancelled(FactoryError):
    pass


@dataclass
class Scene:
    caption: str
    speech: str
    start: float | None = None
    end: float | None = None


@dataclass
class Settings:
    video: str = ""
    script: str = ""
    title: str = "サンプル動画"
    header_text: str = "VIDEO / REPORT"
    theme_id: str = DEFAULT_THEME
    output_dir: str = ""
    output_mode: str = SHORT_MODE
    preset: str = "縦・本番 1080×1920"
    speaker_id: int = -1
    speaker_label: str = DEFAULT_SPEAKER_LABEL
    speed: float = 1.1
    gap: float = 0.20
    font_path: str = ""
    engine_url: str = DEFAULT_ENGINE_URL
    ffmpeg_path: str = ""
    source_audio_enabled: bool = False
    source_audio_volume: float = 20.0
    logo_path: str = ""
    review_states: list[dict] | None = None

    @classmethod
    def from_dict(cls, data: dict, *, strict: bool = False) -> Settings:
        if not isinstance(data, dict):
            raise FactoryError("プロジェクトの形式が正しくありません。")
        expected = set(cls.__dataclass_fields__)
        unknown = set(data) - expected
        if unknown:
            raise FactoryError("このバージョンでは読めない設定があります: " + ", ".join(sorted(unknown)))
        if strict:
            missing = expected - set(data)
            if missing:
                raise FactoryError("プロジェクトに必要な設定が不足しています: " + ", ".join(sorted(missing)))
        obj = cls(**data)
        for name in ("video", "script", "title", "header_text", "theme_id", "output_dir", "output_mode", "preset", "speaker_label",
                     "font_path", "engine_url", "ffmpeg_path", "logo_path"):
            if not isinstance(getattr(obj, name), str):
                raise FactoryError(f"設定 {name} は文字列で指定してください。")
        if obj.theme_id not in THEMES:
            raise FactoryError(f"未対応のテーマです: {obj.theme_id}")
        if obj.review_states is not None and not isinstance(obj.review_states, list):
            raise FactoryError("配置確認データの形式が正しくありません。")
        return obj


@dataclass
class Clip:
    scene: Scene
    start: float
    take: float
    duration: float
    audio_duration: float
    output_start: float


def seconds(text: str) -> float:
    text = text.strip()
    if not re.fullmatch(r"\d+(?::\d{1,2}){0,2}(?:\.\d{1,3})?", text):
        raise FactoryError(f"時刻の書き方を確認してください: {text}")
    parts = [float(p) for p in text.split(":")]
    if len(parts) > 1 and any(p >= 60 for p in parts[1:]):
        raise FactoryError(f"秒・分は60未満で指定してください: {text}")
    value = sum(p * 60 ** i for i, p in enumerate(reversed(parts)))
    if value > 86400:
        raise FactoryError("24時間を超える時刻には対応していません。")
    return value


def parse_script(script: str) -> list[Scene]:
    """Plain lines are individual scenes; timed headers collect following lines."""
    scenes: list[Scene] = []
    timed: tuple[float, float] | None = None
    pending: list[str] = []

    def add(text: str, window: tuple[float, float] | None = None) -> None:
        bits = text.split("||")
        if len(bits) > 2 or not all(s.strip() for s in bits):
            raise FactoryError("「字幕 || 読み上げ」は左右に文字を入れ、区切りは1個にしてください。")
        caption = bits[0].strip()
        speech = (bits[1] if len(bits) == 2 else caption).replace("\n", " ").strip()
        if len(caption) > 180 or len(speech) > 300:
            raise FactoryError("1セリフが長すぎます。字幕180文字・読み上げ300文字以内に分けてください。")
        scenes.append(Scene(caption, speech, *(window or (None, None))))

    def flush() -> None:
        if timed is not None:
            if not pending:
                raise FactoryError("時間指定の次の行にセリフを入力してください。")
            add("\n".join(pending), timed)
            pending.clear()

    for number, raw in enumerate(script.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"\[\s*([\d:.]+)\s*[-–〜～]\s*([\d:.]+)\s*\]", line)
        if match:
            flush()
            timed = (seconds(match[1]), seconds(match[2]))
            if timed[1] <= timed[0]:
                raise FactoryError(f"{number}行目: 終了は開始より後にしてください。")
        elif line.startswith("["):
            raise FactoryError(f"{number}行目: 時間指定は [00:08-00:14] の形式です。")
        elif timed is not None:
            pending.append(line)
        else:
            add(line)
    flush()
    if not scenes:
        raise FactoryError("台本を入力してください。1行につき1セリフです。")
    if len(scenes) > 80:
        raise FactoryError("初版は80セリフまでです。動画を分けてください。")
    return scenes


def remove_script_scenes(script: str, indexes: set[int] | list[int] | tuple[int, ...]) -> str:
    """Remove parsed scene blocks by zero-based scene index while preserving other text.

    Plain mode removes the corresponding single dialogue line. Timed mode removes the
    time header and all lines belonging to that timed block. This lets the review UI
    delete a narration item without rewriting the rest of the user's script.
    """
    remove = {int(i) for i in indexes if int(i) >= 0}
    if not remove:
        return script
    # Validate first so the block scanner only has to support already-valid syntax.
    scenes = parse_script(script)
    if any(i >= len(scenes) for i in remove):
        raise FactoryError("削除対象のセリフ番号が台本の範囲外です。")

    lines = script.splitlines()
    blocks: list[tuple[int, int]] = []
    timed_start: int | None = None
    time_pattern = re.compile(r"\[\s*([\d:.]+)\s*[-–〜～]\s*([\d:.]+)\s*\]")
    for idx, raw in enumerate(lines):
        line = raw.strip()
        if time_pattern.fullmatch(line):
            if timed_start is not None:
                blocks.append((timed_start, idx))
            timed_start = idx
            continue
        if not line or line.startswith("#"):
            continue
        if timed_start is None:
            blocks.append((idx, idx + 1))
        # In timed mode all non-header lines belong to the current timed scene.
    if timed_start is not None:
        blocks.append((timed_start, len(lines)))

    if len(blocks) != len(scenes):
        raise FactoryError("台本の削除位置を特定できませんでした。いったん通常画面で削除してください。")
    deleted_lines: set[int] = set()
    for scene_index in remove:
        start, end = blocks[scene_index]
        deleted_lines.update(range(start, end))
    kept = [line for i, line in enumerate(lines) if i not in deleted_lines]
    result = "\n".join(kept)
    if script.endswith(("\n", "\r")) and result:
        result += "\n"
    return result


def _even_starts(lengths: list[float], source_duration: float, gap: float) -> list[float]:
    """Distribute narration evenly over the whole source timeline.

    When everything fits, the unused time is divided equally before, between and
    after narration clips. If the narration cannot fit, starts are still spread over
    the video and overlap/overrun is reported by the normal warning logic.
    """
    n = len(lengths)
    if n == 0:
        return []
    required = sum(lengths) + gap * max(0, n - 1)
    if required <= source_duration:
        air = (source_duration - required) / (n + 1)
        cursor = air
        starts: list[float] = []
        for length in lengths:
            starts.append(cursor)
            cursor += length + gap + air
        return starts
    if n == 1:
        return [max(0.0, (source_duration - lengths[0]) / 2)]
    starts = []
    for i, length in enumerate(lengths):
        latest = max(0.0, source_duration - length)
        starts.append(latest * i / (n - 1))
    return starts


def video_identity(path: str | Path) -> str:
    """Logical material identity: same resolved path means the same source by specification."""
    return str(Path(path).expanduser().resolve())


def detect_visual_changes(ffmpeg: str, video: Path, cancel: threading.Event | None = None) -> list[float]:
    """Return likely visual-change times from FFmpeg's scene score.

    Screen recordings often have no hard cuts, so the threshold is deliberately low.
    Failure is non-fatal: the placement algorithm falls back to progress-based anchors.
    """
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "info", "-i", str(video),
           "-vf", "select='gt(scene,0.012)',showinfo", "-an", "-f", "null", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
    lines: list[str] = []
    try:
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise Cancelled("中止しました。")
            line = proc.stderr.readline() if proc.stderr else ""
            if line:
                lines.append(line)
            else:
                time.sleep(.02)
        if proc.stderr:
            lines.extend(proc.stderr.readlines())
    finally:
        if proc.stderr:
            proc.stderr.close()
    if proc.returncode not in (0, None):
        return []
    found: list[float] = []
    for line in lines:
        m = re.search(r"pts_time:([0-9]+(?:\.[0-9]+)?)", line)
        if m:
            t = float(m.group(1))
            if not found or t - found[-1] >= .35:
                found.append(t)
        if len(found) >= 240:
            break
    return found


def _snap_anchor(ideal: float, candidates: list[float], radius: float, minimum: float,
                 latest: float) -> tuple[float, str]:
    valid = [t for t in candidates if minimum <= t <= latest and abs(t - ideal) <= radius]
    if valid:
        return min(valid, key=lambda t: abs(t - ideal)), "場面変化"
    return min(max(ideal, minimum), latest), "映像進行"


def make_timeline(scenes: list[Scene], lengths: list[float], source_duration: float,
                  gap: float, visual_changes: list[float] | None = None,
                  review_states: list[dict] | None = None, source_id: str = "") -> tuple[list[Clip], list[str]]:
    """Place narration ON the original video timeline; never shorten or extend the source.

    Manual/reviewed placements are reused only when they belong to the exact same source video.
    Otherwise untimed lines are distributed evenly across the whole source timeline.
    """
    if len(scenes) != len(lengths) or not scenes:
        raise FactoryError("セリフと音声の数が一致しません。")
    if source_duration <= 0 or not math.isfinite(source_duration):
        raise FactoryError("録画の長さを取得できませんでした。MP4で再保存してみてください。")
    if not math.isfinite(gap) or not 0 <= gap <= 2:
        raise FactoryError("セリフ後の余白は0〜2秒にしてください。")
    if any((not math.isfinite(x) or x <= 0) for x in lengths):
        raise FactoryError("音声の長さが正しくありません。")

    # visual_changes is retained in the public signature for project/test compatibility,
    # but default placement is intentionally deterministic and even across the video.
    saved: dict[tuple[str, str], dict] = {}
    saved_by_index: dict[int, dict] = {}
    for row in review_states or []:
        if not isinstance(row, dict) or row.get("video_id") != source_id:
            continue
        idx = row.get("script_index")
        if isinstance(idx, int) and idx >= 0 and idx not in saved_by_index:
            saved_by_index[idx] = row
        caption = row.get("caption")
        speech = row.get("speech")
        if isinstance(caption, str) and isinstance(speech, str):
            key = (caption, speech)
            if key not in saved:
                saved[key] = row

    clips: list[Clip] = []
    warnings: list[str] = []
    previous_end = 0.0
    even_starts = _even_starts(lengths, source_duration, gap)
    for i, (scene, length) in enumerate(zip(scenes, lengths), 1):
        row = saved_by_index.get(i - 1)
        # Never trust an index alone after insert/delete edits. The row must still
        # describe the same caption+speech; otherwise fall back to content matching.
        if row is not None and (row.get("caption") != scene.caption or row.get("speech") != scene.speech):
            row = None
        if row is None:
            row = saved.get((scene.caption, scene.speech))
        reason = ""
        if row is not None:
            try:
                start = float(row["start"])
                reason = "保存済み配置"
            except (KeyError, TypeError, ValueError):
                row = None
        if row is None and scene.start is not None:
            if scene.end is None or scene.start < 0 or scene.start >= source_duration or scene.end > source_duration + .03:
                raise FactoryError(f"セリフ{i}: 指定範囲が録画の長さ {source_duration:.2f}秒を超えています。")
            start = scene.start
            reason = "台本の時間指定"
        elif row is None:
            start = even_starts[i - 1]
            reason = "均等配置"
            # If a preceding fixed/manual item was moved later, keep this auto item
            # after it when possible. Any unavoidable overrun is left as a warning.
            minimum = previous_end + (gap if clips else 0.0)
            latest = max(0.0, source_duration - length)
            if start < minimum <= latest:
                start = minimum

        if start < 0:
            start = 0.0
        end = start + length
        if clips and start < previous_end - OVERLAP_TOLERANCE:
            warnings.append(f"セリフ{i}: 前の読み上げと重なっています。配置確認で開始位置を直してください。")
        if end > source_duration + .01:
            warnings.append(f"セリフ{i}: 音声が動画の終わりを {end - source_duration:.1f}秒超えます。要調整です。")
        if scene.end is not None and end > scene.end + .01:
            warnings.append(f"セリフ{i}: 読み上げが指定場面 [{scene.start:.2f}-{scene.end:.2f}] を超えます。")
        take = max(0.01, min(source_duration, end) - min(start, source_duration - .01))
        clip = Clip(scene, start, take, length, length, start)
        # Dynamic attributes are serialized by the review UI separately, not asdict(clip).
        clip.reason = reason  # type: ignore[attr-defined]
        clips.append(clip)
        previous_end = max(previous_end, end)

    if source_duration > 60:
        warnings.append(f"元動画は {source_duration:.1f}秒です。60秒の制作目安を超えますが、動画は最後まで残します。")
    return clips, warnings


def voice_cache_dir() -> Path:
    """Internal narration cache. Users never need to manage this folder."""
    path = ROOT / ".cache" / "voice"
    path.mkdir(parents=True, exist_ok=True)
    return path


def voice_cache_key(text: str, speaker_id: int, speed: float, engine_url: str) -> str:
    payload = json.dumps({
        "text": text,
        "speaker_id": int(speaker_id),
        "speed": round(float(speed), 6),
        "engine_url": checked_url(engine_url),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cached_speech(voice: "Voicevox", text: str, speaker_id: int, speed: float,
                  engine_url: str) -> tuple[bytes, bool, str]:
    """Return synthesized WAV, reusing a validated on-disk cache when possible."""
    key = voice_cache_key(text, speaker_id, speed, engine_url)
    path = voice_cache_dir() / f"{key}.wav"
    if path.is_file():
        try:
            data = path.read_bytes()
            wav_length(data)
            return data, True, key
        except (OSError, FactoryError):
            try:
                path.unlink()
            except OSError:
                pass
    data = voice.synthesize(text, speaker_id, speed)
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
    return data, False, key


def build_review_plan(settings: Settings, log: Callable[[str], None] = print,
                      cancel: threading.Event | None = None,
                      progress: Callable[[float], None] = lambda x: None) -> tuple[list[Clip], list[str], str, list[bytes]]:
    """Create narration lengths, analyze the source timeline, then propose placements."""
    log("配置確認: 入力検証中…")
    scenes = validate_settings(settings)
    cancel = cancel or threading.Event()
    ffmpeg = find_ffmpeg(settings.ffmpeg_path)
    video = Path(settings.video).resolve()
    log(f"配置確認: 元動画を確認中… {video}")
    duration = probe_duration(ffmpeg, video, cancel)
    source_id = video_identity(video)
    log("配置確認: VOICEVOX API接続確認中…")
    voice = Voicevox(settings.engine_url)
    actual = dict((sid, label) for label, sid in voice.speakers())
    log("配置確認: VOICEVOX API接続確認完了")
    if settings.speaker_id not in actual:
        raise FactoryError("選択した声が見つかりません。「声を読み込み」から選び直してください。")
    settings.speaker_label = actual[settings.speaker_id]
    lengths: list[float] = []
    audio_blobs: list[bytes] = []
    for i, scene in enumerate(scenes):
        if cancel.is_set():
            raise Cancelled("中止しました。")
        data, hit, _cache_key = cached_speech(voice, scene.speech, settings.speaker_id,
                                                settings.speed, settings.engine_url)
        log(("音声キャッシュを再利用 " if hit else "仮配置用の音声を生成 ")
            + f"{i + 1}/{len(scenes)}")
        lengths.append(wav_length(data))
        audio_blobs.append(data)
        progress(55 * (i + 1) / len(scenes))
    log("セリフを動画全体へ均等配置しています…")
    progress(75)
    clips, warnings = make_timeline(scenes, lengths, duration, settings.gap, None,
                                    settings.review_states, source_id)
    progress(100)
    return clips, warnings, source_id, audio_blobs

def checked_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ("", "/")):
        raise FactoryError("初版のVOICEVOX接続先は、このPC内のHTTPアドレスだけに対応します。")
    try:
        parsed.port
    except ValueError as exc:
        raise FactoryError("VOICEVOXのポート番号が正しくありません。") from exc
    return url.rstrip("/")


def engine_preset(url: str) -> str:
    """Label known loopback endpoints while retaining arbitrary local ports."""
    try:
        parsed = urllib.parse.urlparse(checked_url(url))
    except (FactoryError, ValueError):
        return CUSTOM_ENGINE
    for label, endpoint in ENGINE_PRESETS.items():
        if parsed.port == urllib.parse.urlparse(endpoint).port:
            return label
    return CUSTOM_ENGINE


def credit_text(settings: Settings) -> str:
    name = settings.speaker_label.split(" / ")[0]
    if engine_preset(settings.engine_url) == NEMO_ENGINE:
        credit = "VOICEVOX Nemo"
        terms = "https://voicevox.hiroshiba.jp/nemo/term/"
    else:
        credit = f"VOICEVOX:{name}"
        terms = "https://voicevox.hiroshiba.jp/term/"
    return (f"{credit}\n使用スタイル: {settings.speaker_label}\n"
            f"公開前に、この話者の利用規約とクレジット表記を確認してください。\n{terms}\n")


class Voicevox:
    def __init__(self, url: str):
        self.url = checked_url(url)
        parsed = urllib.parse.urlparse(self.url)
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 80

    def request(self, route: str, params: dict | None = None, data=None,
                read_timeout: float = VOICEVOX_READ_TIMEOUT) -> bytes:
        path = route
        if params:
            path += "?" + urllib.parse.urlencode(params)
        payload = None if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        method = "POST" if route in ("/audio_query", "/synthesis") else "GET"
        conn = http.client.HTTPConnection(self.host, self.port, timeout=VOICEVOX_CONNECT_TIMEOUT)
        try:
            try:
                conn.connect()
            except (socket.timeout, TimeoutError) as exc:
                raise FactoryError(f"VOICEVOXへの接続がタイムアウトしました。\n接続先: {self.url}") from exc
            if conn.sock is not None:
                conn.sock.settimeout(read_timeout)
            conn.request(method, path, body=payload, headers=headers)
            try:
                response = conn.getresponse()
                body = response.read()
            except (socket.timeout, TimeoutError) as exc:
                raise FactoryError(f"VOICEVOXの応答待ちがタイムアウトしました。\n接続先: {self.url}") from exc
            if not 200 <= response.status < 300:
                raise FactoryError(f"VOICEVOX API エラー ({response.status})。声を再読み込みして選び直してください。")
            return body
        except FactoryError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise FactoryError("VOICEVOXに接続できません。VOICEVOXを起動し、読み込み完了後に再実行してください。"
                               "\n接続先: " + self.url) from exc
        finally:
            conn.close()

    def speakers(self) -> list[tuple[str, int]]:
        try:
            raw = json.loads(self.request("/speakers"))
            return [(f"{sp['name']} / {style['name']}", int(style["id"]))
                    for sp in raw for style in sp["styles"]
                    if style.get("type", "talk") == "talk"]
        except (ValueError, TypeError, KeyError) as exc:
            raise FactoryError("VOICEVOXの話者一覧が正しくありません。") from exc

    def synthesize(self, text: str, speaker: int, speed: float) -> bytes:
        try:
            query = json.loads(self.request("/audio_query", {"text": text, "speaker": speaker}))
            query.update(speedScale=speed, prePhonemeLength=.06, postPhonemeLength=.10,
                         volumeScale=1.0, outputSamplingRate=24000, outputStereo=False)
        except (ValueError, TypeError, AttributeError) as exc:
            raise FactoryError("VOICEVOXの音声設定を取得できませんでした。") from exc
        data = self.request("/synthesis", {"speaker": speaker}, query,
                            read_timeout=VOICEVOX_SYNTHESIS_READ_TIMEOUT)
        wav_length(data)
        return data


def wav_length(data: bytes) -> float:
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            if wav.getnframes() <= 0:
                raise ValueError("empty")
            return wav.getnframes() / wav.getframerate()
    except (wave.Error, EOFError, ValueError) as exc:
        raise FactoryError("音声が正しいWAV形式ではありません。") from exc


def find_ffmpeg(explicit: str = "") -> str:
    if explicit.strip():
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise FactoryError("指定されたFFmpegが見つかりません。")
        return str(path)
    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError) as exc:
        raise FactoryError("FFmpegが見つかりません。START.cmdを実行して初期準備を完了してください。") from exc


def default_output_root() -> Path:
    """Return the default portable output folder next to MONTAZH itself."""
    return ROOT / "output"


def ensure_output_folders(root: str | Path = "") -> dict[str, Path]:
    """Create and return the standard MONTAZH output folders."""
    base = Path(root).expanduser() if str(root).strip() else default_output_root()
    base = base.resolve()
    folders = {
        "root": base,
        "scripts": base / "scripts",
        "videos": base / "videos",
        "audio": base / "audio",
        "logs": base / "logs",
    }
    for folder in folders.values():
        folder.mkdir(parents=True, exist_ok=True)
    return folders


def has_audio_stream(ffmpeg: str, video: Path, cancel=None) -> bool:
    """Detect whether the source contains at least one audio stream using FFmpeg only."""
    raw = ff_run([ffmpeg, "-hide_banner", "-i", str(video)], cancel, timeout=60,
                 accept_failure=True)
    text = raw.decode("utf-8", "replace")
    return any("Stream #" in line and "Audio:" in line for line in text.splitlines())


def _terminate_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


def ff_run(args: list[str], cancel: threading.Event | None = None, timeout=1800,
           accept_failure=False) -> bytes:
    """Run short/diagnostic FFmpeg commands with a bounded total duration."""
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=output,
                                stderr=subprocess.STDOUT, creationflags=flags)
        started = time.monotonic()
        try:
            while proc.poll() is None:
                if cancel and cancel.is_set():
                    raise Cancelled("中止しました。元の録画は変更していません。")
                if time.monotonic() - started > timeout:
                    raise FactoryError("FFmpeg処理が規定時間を超えたため停止しました。")
                time.sleep(.08)
        finally:
            if proc.poll() is None:
                _terminate_process(proc)
        output.seek(0)
        result = output.read()
    if proc.returncode and not accept_failure:
        tail = result.decode("utf-8", "replace")[-6000:]
        raise FactoryError(f"FFmpegが異常終了しました (終了コード {proc.returncode})。詳細:\n{tail}")
    return result


def ff_run_progress(args: list[str], cancel: threading.Event | None = None,
                    stall_timeout: float = FFMPEG_STALL_TIMEOUT) -> bytes:
    """Run a long FFmpeg job and abort only when progress stops for too long."""
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    progress_args = list(args[:-1]) + ["-progress", "pipe:1", "-nostats", args[-1]]
    proc = subprocess.Popen(progress_args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, creationflags=flags)
    q: "queue.Queue[bytes | None]"
    import queue as _queue
    q = _queue.Queue()
    chunks: list[bytes] = []

    def reader():
        try:
            assert proc.stdout is not None
            for line in iter(proc.stdout.readline, b""):
                q.put(line)
        finally:
            q.put(None)

    threading.Thread(target=reader, daemon=True).start()
    last_progress = time.monotonic()
    last_marker: bytes | None = None
    try:
        eof = False
        while proc.poll() is None or not eof:
            if cancel and cancel.is_set():
                raise Cancelled("中止しました。元の録画は変更していません。")
            try:
                item = q.get(timeout=.2)
                if item is None:
                    eof = True
                    continue
                chunks.append(item)
                if item.startswith((b"out_time_ms=", b"out_time_us=", b"frame=", b"progress=")):
                    if item != last_marker:
                        last_marker = item
                        last_progress = time.monotonic()
            except _queue.Empty:
                pass
            if proc.poll() is None and time.monotonic() - last_progress > stall_timeout:
                raise FactoryError(f"FFmpegの進捗が{int(stall_timeout)}秒以上停止したため異常終了と判断しました。")
        proc.wait()
    finally:
        if proc.poll() is None:
            _terminate_process(proc)
        if proc.stdout is not None:
            proc.stdout.close()
    result = b"".join(chunks)
    if proc.returncode:
        tail = result.decode("utf-8", "replace")[-6000:]
        raise FactoryError(f"FFmpegが異常終了しました (終了コード {proc.returncode})。詳細:\n{tail}")
    return result


def probe_duration(ffmpeg: str, video: Path, cancel=None) -> float:
    raw = ff_run([ffmpeg, "-hide_banner", "-protocol_whitelist", "file,pipe", "-i", str(video)],
                 cancel, timeout=30, accept_failure=True)
    text = raw.decode("utf-8", "replace")
    match = re.search(r"Duration:\s*(\d+:\d+:\d+(?:\.\d+)?)", text)
    if not match or not re.search(r"Stream .*Video:", text):
        raise FactoryError("映像付きの録画ファイルを読み込めませんでした。MP4で再保存してみてください。")
    return seconds(match[1])


def japanese_font(explicit: str = "") -> str:
    if explicit:
        if not Path(explicit).is_file():
            raise FactoryError("指定されたフォントが見つかりません。")
        return explicit
    win = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    candidates = [win / "meiryob.ttc", win / "YuGothB.ttc", win / "meiryo.ttc",
                  Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
                  Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
                  Path("/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc")]
    for path in candidates:
        if path.is_file():
            return str(path)
    raise FactoryError("日本語フォントが見つかりません。詳細設定から日本語対応のTTF/OTF/TTCを選んでください。")


def product_title_parts(title: str) -> tuple[str, str, str]:
    """Split an optional generic three-part product title.

    MONTAZH does not require PMN naming.  A title is treated as a structured
    product title only when it looks like::

        PRODUCT-NO  ENGLISH-NAME  日本語名

    The first field must contain at least one digit, the second field must be
    an ASCII-style name, and the final field must contain non-ASCII text.
    Examples include ``PMN-001 TERMINUS 終末時限装置`` and
    ``AX-12 CLEANER 重複ファイル整理``.  Everything else is rendered as one
    ordinary free-form title.

    Slash-separated input is accepted for convenience.
    """
    value = str(title or "").replace("\\r", " " ).replace("\\n", " " )
    value = value.replace("\r", " " ).replace("\n", " " ).replace("\t", " " )
    value = re.sub(r"\s*/\s*", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*\d[A-Za-z0-9._-]*)\s+([A-Za-z][A-Za-z0-9._-]*)\s+(.+)$", value)
    if not match:
        return "", "", value
    product_no, english_name, display_name = match.groups()
    # Do not reinterpret a normal all-ASCII title merely because it starts
    # with a model-like token. Structured mode is intended for a final local
    # language/product name, not arbitrary prose.
    if display_name.isascii():
        return "", "", value
    return product_no, english_name, display_name.strip()


def safe_output_stem(title: str) -> str:
    """Return a Windows-safe, short output stem independent from display wrapping."""
    product_no, english_name, _local_name = product_title_parts(title)
    if product_no and english_name:
        return f"{product_no}_{english_name}"
    value = str(title or "").replace("\\r", "_").replace("\\n", "_")
    value = re.sub(r"[\x00-\x1f<>:\"/\\\\|?*]+", "_", value)
    value = re.sub(r"\s+", "_", value.strip())
    value = re.sub(r"_+", "_", value).strip(" ._")
    return value[:80] or "MONTAZH"


def wrap_text(text: str, font, width: int) -> list[str]:
    lines: list[str] = []
    forbidden_start = "、。，．？！!?)]）】」』ーぁぃぅぇぉっゃゅょァィゥェォッャュョ"
    forbidden_end = "([（【「『"
    for paragraph in text.split("\n"):
        line = ""
        for char in paragraph:
            if line and font.getlength(line + char) > width:
                if (char in forbidden_start or line[-1] in forbidden_end) and len(line) > 1:
                    lines.append(line[:-1])
                    line = line[-1] + char
                else:
                    lines.append(line)
                    line = char
            else:
                line += char
        if line:
            lines.append(line)
    return lines or [""]


def _ellipsize_line(text: str, font, width: int) -> str:
    """Clamp a single line to the box width without failing the whole preview/render."""
    if font.getlength(text) <= width:
        return text
    suffix = "…"
    value = text
    while value and font.getlength(value + suffix) > width:
        value = value[:-1]
    return (value + suffix) if value else suffix


def text_in_box(draw, text: str, font_path: str, box: tuple[int, int, int, int],
                size: int, minimum: int, color="#f4f7fa", max_lines=4):
    """Draw text with shrink-to-fit; if still too long, show a clamped layout instead of erroring."""
    x, y, width, height = box
    for font_size in range(size, minimum - 1, -2):
        font = ImageFont.truetype(font_path, font_size)
        lines = wrap_text(text, font, width)
        line_height = int(font_size * 1.50)
        if (len(lines) <= max_lines and line_height * len(lines) <= height
                and all(font.getlength(line) <= width + .1 for line in lines)):
            top = y + (height - line_height * len(lines)) // 2
            for i, line in enumerate(lines):
                bounds = draw.textbbox((0, 0), line, font=font)
                draw.text((x, top + i * line_height - bounds[1]), line, font=font, fill=color)
            return True

    # Last-resort layout: retain a readable minimum font and clamp the final line.
    # The user can therefore inspect the composition and edit the wording, rather than
    # being blocked by a modal error before seeing anything.
    font_size = max(12, minimum)
    font = ImageFont.truetype(font_path, font_size)
    lines = wrap_text(text, font, width)
    line_height = int(font_size * 1.50)
    vertical_capacity = max(1, height // max(1, line_height))
    allowed = max(1, min(max_lines, vertical_capacity))
    clipped = len(lines) > allowed
    lines = lines[:allowed]
    if lines:
        if clipped:
            lines[-1] = _ellipsize_line(lines[-1] + "…", font, width)
        else:
            lines[-1] = _ellipsize_line(lines[-1], font, width)
    top = y + max(0, (height - line_height * len(lines)) // 2)
    for i, line in enumerate(lines):
        bounds = draw.textbbox((0, 0), line, font=font)
        draw.text((x, top + i * line_height - bounds[1]), line, font=font, fill=color)
    return False


def layout(width: int, height: int) -> tuple[int, int, int, int]:
    if height > width:
        return (0, int(height * .245), width, int(height * .44) // 2 * 2)
    return (0, int(height * .12), width, int(height * .62) // 2 * 2)


def _place_logo(im: Image.Image, settings: Settings) -> None:
    """Place the optional Promnica icon in a safe right-side corner."""
    if not settings.logo_path.strip():
        return
    width, height = im.size
    logo_path = Path(settings.logo_path).expanduser()
    try:
        with Image.open(logo_path) as source:
            logo = source.convert("RGBA")
    except OSError as exc:
        raise FactoryError("動画アイコン画像を読み込めません。") from exc
    # The character icon is part of the Promnica identity, not a tiny watermark.
    # Keep it clearly recognizable on a phone-sized Shorts preview.
    max_side = max(72, int(width * (0.18 if height > width else 0.10)))
    logo.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    margin = max(18, int(width * .050))
    x = max(margin, width - margin - logo.width)
    if height > width:
        # Shorts: lower-right is commonly occupied by platform controls, so top-right is safer.
        y = max(18, int(height * .047))
    else:
        # Landscape title occupies the upper-right area, so use the lower-right corner.
        y = max(18, height - margin - logo.height)
    im.alpha_composite(logo, (x, y))


def make_overlay(settings: Settings, caption: str, index: int, count: int, path: Path):
    width, height = PRESETS[settings.preset]
    font_path = japanese_font(settings.font_path)
    colors = theme_colors(settings)
    bg = colors["bg"]
    text_color = colors["text"]
    accent = colors["accent"]
    muted = colors["muted"]
    im = Image.new("RGBA", (width, height), bg)
    draw = ImageDraw.Draw(im)
    x, y, vw, vh = layout(width, height)
    draw.rectangle((x, y, x + vw - 1, y + vh - 1), fill=(0, 0, 0, 0))
    unit = width / 1080 if height > width else height / 1920
    margin = int(width * .065)
    product_no, english_name, local_name = product_title_parts(settings.title)

    small = ImageFont.truetype(font_path, max(18, int(25 * unit)))
    if settings.header_text.strip():
        draw.text((margin, int(height * .060)), settings.header_text.strip(), font=small, fill=accent)

    if height > width:
        # Portrait title hierarchy:
        #   PMN-001 / TERMINUS   <- one compact model/code line
        #   終末時限装置          <- human-readable device name
        # This leaves substantially more room for the actual screen recording.
        right_reserved = int(width * .245) if settings.logo_path.strip() else margin
        usable = max(int(width * .50), width - margin - right_reserved)
        if product_no and english_name:
            model_text = f"{product_no}  /  {english_name}"
            model_box = (margin, int(height * .101), usable, int(height * .050))
            text_in_box(draw, model_text, font_path, model_box,
                        int(48 * unit), int(30 * unit), color=text_color, max_lines=1)
            if local_name:
                jp_box = (margin, int(height * .151), usable, int(height * .070))
                text_in_box(draw, local_name, font_path, jp_box,
                            int(60 * unit), int(34 * unit), color=text_color, max_lines=2)
        else:
            title_box = (margin, int(height * .101), usable, int(height * .120))
            text_in_box(draw, settings.title, font_path, title_box,
                        int(58 * unit), int(30 * unit), color=text_color, max_lines=2)
    else:
        # Landscape keeps the same hierarchy but uses the top strip to the right
        # of the source-video area.
        title_x = int(width * .30)
        title_w = int(width * .56)
        if product_no and english_name:
            model_box = (title_x, int(height * .018), title_w, int(height * .055))
            text_in_box(draw, f"{product_no}  /  {english_name}", font_path, model_box, 42, 26, max_lines=1)
            if local_name:
                jp_box = (title_x, int(height * .072), title_w, int(height * .058))
                text_in_box(draw, local_name, font_path, jp_box, 46, 26, max_lines=1)
        else:
            title_box = (title_x, int(height * .018), title_w, int(height * .112))
            text_in_box(draw, settings.title, font_path, title_box, 48, 26, color=text_color, max_lines=2)

    # Place captions directly below the source-video area in portrait mode so they stay
    # readable in Shorts/Reels without covering the screen recording itself.
    if height > width:
        video_bottom = y + vh
        bottom_y = min(int(height * .742), video_bottom + int(height * .014))
        subtitle_top = bottom_y + int(height * .014)
        subtitle_height = int(height * .100)
    else:
        bottom_y = int(height * .775)
        subtitle_top = bottom_y + int(height * .025)
        subtitle_height = int(height * .165)
    draw.rectangle((margin, bottom_y, margin + int(width * .07), bottom_y + max(3, int(4 * unit))), fill=accent)
    # Caption can use nearly the full width: the icon lives in the title/header zone.
    subtitle_usable = width - margin * 2
    subtitle_box = (margin, subtitle_top, subtitle_usable, subtitle_height)
    text_in_box(draw, caption, font_path, subtitle_box, int(57 * unit) if height > width else 49,
                int(29 * unit) if height > width else 25, color=text_color, max_lines=4 if height > width else 4)
    if index > 0:
        draw.text((margin, int(height * .922)), f"{index:02d} / {count:02d}", font=small, fill=muted)
    _place_logo(im, settings)
    im.save(path)

def video_filter(settings: Settings, take: float, duration: float) -> str:
    width, height = PRESETS[settings.preset]
    x, y, vw, vh = layout(width, height)
    bg = theme_colors(settings)["bg"]
    # Normalize sample aspect ratio BEFORE fitting anamorphic sources.
    return (f"[0:v:0]trim=duration={take:.6f},setpts=PTS-STARTPTS,"
            "scale=trunc(iw*sar/2)*2:ih,setsar=1,"
            f"scale={vw}:{vh}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"pad={vw}:{vh}:(ow-iw)/2:(oh-ih)/2:color={bg},"
            f"pad={width}:{height}:{x}:{y}:color={bg},fps=30,"
            f"tpad=stop_mode=clone:stop_duration={duration:.6f},trim=duration={duration:.6f}[screen];"
            "[screen][1:v]overlay=0:0:format=auto,format=yuv420p[v]")



def base_video_filter(settings: Settings) -> str:
    width, height = PRESETS[settings.preset]
    x, y, vw, vh = layout(width, height)
    bg = theme_colors(settings)["bg"]
    return ("[0:v:0]setpts=PTS-STARTPTS,scale=trunc(iw*sar/2)*2:ih,setsar=1,"
            f"scale={vw}:{vh}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"pad={vw}:{vh}:(ow-iw)/2:(oh-ih)/2:color={bg},"
            f"pad={width}:{height}:{x}:{y}:color={bg},fps=30[screen]")

def validate_settings(settings: Settings, require_voice=True) -> list[Scene]:
    if not settings.video or not Path(settings.video).is_file():
        raise FactoryError("録画ファイルを選択してください。")
    try:
        video_path = Path(settings.video).expanduser()
        if video_path.stat().st_size <= 0:
            raise OSError("empty file")
        with video_path.open("rb") as handle:
            handle.read(1)
    except OSError as exc:
        raise FactoryError(f"元動画を読み込めません: {settings.video}") from exc
    if settings.output_mode not in OUTPUT_MODES:
        raise FactoryError("書き出し用途を選び直してください。")
    if settings.preset not in PRESETS:
        raise FactoryError("出力サイズを選び直してください。")
    if settings.output_mode == SHORT_MODE and settings.preset not in SHORT_PRESETS:
        raise FactoryError("ショート動画は縦9:16の出力サイズを選んでください。")
    if settings.output_mode == NORMAL_MODE and settings.preset not in NORMAL_PRESETS:
        raise FactoryError("通常動画は横16:9の出力サイズを選んでください。")
    if not settings.title.strip() or len(settings.title) > 120:
        raise FactoryError("タイトルは1〜120文字にしてください。")
    try:
        if (not math.isfinite(settings.speed) or not .5 <= settings.speed <= 2
                or not math.isfinite(settings.gap) or not 0 <= settings.gap <= 2):
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise FactoryError("話速は0.5〜2.0、余白は0〜2秒で指定してください。") from exc
    if type(settings.source_audio_enabled) is not bool:
        raise FactoryError("元動画音声のON/OFF設定が正しくありません。")
    try:
        if (not math.isfinite(settings.source_audio_volume)
                or not 0 <= settings.source_audio_volume <= 100):
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise FactoryError("元動画音量は0〜100%で指定してください。") from exc
    if settings.logo_path.strip():
        logo = Path(settings.logo_path).expanduser()
        if not logo.is_file():
            raise FactoryError("動画アイコン画像が見つかりません。詳細設定から選び直してください。")
        if logo.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise FactoryError("動画アイコンは PNG / JPG / WEBP を選択してください。")
        try:
            with Image.open(logo) as check:
                check.verify()
        except (OSError, ValueError) as exc:
            raise FactoryError("動画アイコン画像を読み込めません。別の画像を選択してください。") from exc
    if require_voice and (type(settings.speaker_id) is not int or settings.speaker_id < 0):
        raise FactoryError("VOICEVOXの「声を読み込み」から話者を選択してください。")
    checked_url(settings.engine_url)
    return parse_script(settings.script)


def _project_payload(settings: Settings) -> bytes:
    return json.dumps({"schema_version": PROJECT_SCHEMA_VERSION, "app_version": VERSION,
                       "settings": asdict(settings)},
                      ensure_ascii=False, indent=2).encode("utf-8")


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _atomic_replace(temp: Path, target: Path) -> None:
    _fsync_file(temp)
    os.replace(temp, target)


def save_project(path: Path, settings: Settings) -> None:
    """Atomically save a complete project snapshot. Existing good files remain intact on failure."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _project_payload(settings)
    if path.suffix.lower() != ".montazh":
        fd, name = tempfile.mkstemp(prefix="pmn003_save_", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload); handle.flush(); os.fsync(handle.fileno())
            json.loads(temporary.read_text(encoding="utf-8"))
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return

    referenced: set[str] = set()
    if settings.speaker_id >= 0:
        for scene in parse_script(settings.script):
            referenced.add(voice_cache_key(scene.speech, settings.speaker_id, settings.speed, settings.engine_url))
    fd, name = tempfile.mkstemp(prefix="pmn003_project_", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("project.json", payload)
            cache_root = voice_cache_dir()
            for key in sorted(referenced):
                wav = cache_root / f"{key}.wav"
                if wav.is_file():
                    archive.write(wav, f"audio_cache/{key}.wav")
        with zipfile.ZipFile(temporary, "r") as check:
            bad = check.testzip()
            if bad:
                raise FactoryError(f"プロジェクト一時保存の検査に失敗しました: {bad}")
            _settings_from_payload(check.read("project.json"))
        _atomic_replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _settings_from_payload(payload: bytes) -> Settings:
    try:
        data = json.loads(payload.decode("utf-8-sig"))
        if not isinstance(data, dict):
            raise FactoryError("プロジェクトの形式が正しくありません。")
        schema = data.get("schema_version")
        if schema is None:
            # Real legacy v0.3.x schema: {version, settings}. Convert only this known shape.
            if set(data).issuperset({"version", "settings"}) and isinstance(data.get("settings"), dict):
                obj = Settings.from_dict(data["settings"], strict=False)
            else:
                raise FactoryError("旧形式プロジェクトとして認識できません。")
        elif schema == 1:
            # Known v0.3.4 schema. Preserve the former fixed Promnica header
            # while upgrading to the configurable public-template setting.
            if set(data) != {"schema_version", "app_version", "settings"} or not isinstance(data.get("settings"), dict):
                raise FactoryError("旧schemaプロジェクトの構造が一致しません。")
            migrated = dict(data["settings"])
            migrated.setdefault("header_text", "PROMNICA / FIELD REPORT")
            migrated.setdefault("theme_id", DEFAULT_THEME)
            obj = Settings.from_dict(migrated, strict=True)
        elif schema == 2:
            # Known v0.3.5 schema: add the new project theme with the former visual as default.
            if set(data) != {"schema_version", "app_version", "settings"} or not isinstance(data.get("settings"), dict):
                raise FactoryError("旧schemaプロジェクトの構造が一致しません。")
            migrated = dict(data["settings"])
            migrated.setdefault("theme_id", DEFAULT_THEME)
            obj = Settings.from_dict(migrated, strict=True)
        elif schema == PROJECT_SCHEMA_VERSION:
            if set(data) != {"schema_version", "app_version", "settings"}:
                raise FactoryError("プロジェクトのトップレベル構造が一致しません。")
            obj = Settings.from_dict(data["settings"], strict=True)
        else:
            raise FactoryError(f"未対応のMONTAZHプロジェクトschema_versionです: {schema}")
        # Never execute a binary path supplied by an imported project file.
        obj.ffmpeg_path = ""
        return obj
    except FactoryError:
        raise
    except (UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
        raise FactoryError("PMN-003のプロジェクトファイルを選択してください。") from exc


def _validate_project_source_reference(settings: Settings) -> None:
    if not settings.video:
        raise FactoryError("プロジェクトの元動画参照が空です。")
    video = Path(settings.video).expanduser()
    if not video.is_file():
        raise FactoryError(f"元動画が見つかりません: {video}")
    try:
        if video.stat().st_size <= 0:
            raise OSError("empty file")
        with video.open("rb") as handle:
            handle.read(1)
    except OSError as exc:
        raise FactoryError(f"元動画を読み込めません: {video}") from exc
    # Project-load validation is intentionally content-agnostic: same path is the same
    # logical material, but the current bytes must still be a readable video.
    try:
        probe_duration(find_ffmpeg(""), video, None)
    except FactoryError as exc:
        raise FactoryError(f"元動画を正常に読み込めません: {video}") from exc


def load_project(path: Path) -> Settings:
    path = Path(path)
    if not path.is_file():
        raise FactoryError("プロジェクトファイルが見つかりません。")
    cache_items: list[tuple[str, bytes]] = []
    if zipfile.is_zipfile(path):
        if path.stat().st_size > 150 * 1024 * 1024:
            raise FactoryError("プロジェクトファイルが大きすぎます。")
        try:
            with zipfile.ZipFile(path, "r") as archive:
                if archive.testzip():
                    raise FactoryError("MONTAZHプロジェクトZIPが破損しています。")
                payload = archive.read("project.json")
                if len(payload) > 2 * 1024 * 1024:
                    raise FactoryError("プロジェクト情報が大きすぎます。")
                obj = _settings_from_payload(payload)
                _validate_project_source_reference(obj)
                for info in archive.infolist():
                    m = re.fullmatch(r"audio_cache/([0-9a-f]{64})\.wav", info.filename)
                    if not m:
                        continue
                    if info.file_size > 20 * 1024 * 1024:
                        raise FactoryError("音声キャッシュが大きすぎます。")
                    data = archive.read(info)
                    wav_length(data)
                    cache_items.append((m.group(1), data))
        except FactoryError:
            raise
        except (zipfile.BadZipFile, KeyError, OSError) as exc:
            raise FactoryError("MONTAZHプロジェクトを読み込めませんでした。") from exc
    else:
        if path.stat().st_size > 2 * 1024 * 1024:
            raise FactoryError("プロジェクトファイルが大きすぎます。")
        try:
            obj = _settings_from_payload(path.read_bytes())
            _validate_project_source_reference(obj)
        except OSError as exc:
            raise FactoryError("プロジェクトファイルを読み込めませんでした。") from exc

    # Commit auxiliary cache only after the complete project snapshot has been validated.
    cache_root = voice_cache_dir()
    for key, data in cache_items:
        target = cache_root / f"{key}.wav"
        temp = target.with_suffix(".tmp")
        try:
            temp.write_bytes(data)
            wav_length(temp.read_bytes())
            os.replace(temp, target)
        finally:
            if temp.exists():
                temp.unlink()
    return obj


def srt_time(value: float) -> str:
    ms = round(value * 1000)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def render(settings: Settings, log: Callable[[str], None] = print,
           cancel: threading.Event | None = None, progress: Callable[[float], None] = lambda x: None) -> Path:
    scenes = validate_settings(settings)
    cancel = cancel or threading.Event()
    ffmpeg = find_ffmpeg(settings.ffmpeg_path)
    japanese_font(settings.font_path)
    video = Path(settings.video).resolve()
    source_duration = probe_duration(ffmpeg, video, cancel)
    source_id = video_identity(video)
    voice = Voicevox(settings.engine_url)
    actual = dict((sid, label) for label, sid in voice.speakers())
    if settings.speaker_id not in actual:
        raise FactoryError("選択した声が見つかりません。「声を読み込み」から選び直してください。")
    settings.speaker_label = actual[settings.speaker_id]
    folders = ensure_output_folders(settings.output_dir)
    safe_title = safe_output_stem(settings.title)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    job = folders["videos"] / f"{safe_title}_{stamp}"
    suffix = 2
    while job.exists():
        job = folders["videos"] / f"{safe_title}_{stamp}_{suffix}"
        suffix += 1
    job.mkdir(parents=True, exist_ok=False)
    joblog = job / "render.log"

    def report(message: str):
        with joblog.open("a", encoding="utf-8") as handle:
            handle.write(message + "\n")
        log(message)

    try:
        save_project(job / "project.json", settings)
        script_text = settings.script.rstrip() + "\n"
        (job / "script.txt").write_text(script_text, encoding="utf-8-sig")
        # Keep a clearly discoverable copy in output\scripts as well.
        (folders["scripts"] / f"{job.name}.txt").write_text(script_text, encoding="utf-8-sig")
        audio_dir = folders["audio"] / job.name
        audio_dir.mkdir(parents=True, exist_ok=False)
        wavs: list[Path] = []
        lengths: list[float] = []
        for i, scene in enumerate(scenes):
            if cancel.is_set():
                raise Cancelled("中止しました。")
            data, hit, _cache_key = cached_speech(voice, scene.speech, settings.speaker_id,
                                                settings.speed, settings.engine_url)
            report(("音声キャッシュ再利用 " if hit else "音声生成 ") + f"{i + 1}/{len(scenes)}")
            wav = audio_dir / f"{i + 1:03d}.wav"
            wav.write_bytes(data)
            wavs.append(wav)
            lengths.append(wav_length(data))
            progress(22 * (i + 1) / len(scenes))

        # Reuse confirmed/manual placements for the same source. Untimed scenes are
        # otherwise distributed evenly across the full source duration.
        clips, warnings = make_timeline(scenes, lengths, source_duration, settings.gap, None,
                                        settings.review_states, source_id)
        report(f"完成予定: {source_duration:.2f}秒 / 元動画: {source_duration:.2f}秒（映像は最後まで保持）")
        for warning in warnings:
            report("注意: " + warning)
        # Overlap/overrun are warnings, not blocking errors.
        # The final narration track is mixed on the original-video time axis, so
        # overlapping clips can still be exported and adjusted later if desired.
        ordered = sorted(enumerate(clips, 1), key=lambda x: x[1].start)
        last_end = -1.0
        for number, clip in ordered:
            if clip.start < last_end - OVERLAP_TOLERANCE:
                report(f"注意: セリフ{number}が前の音声と重なっています。このまま重ねて書き出します。")
            if clip.start + clip.audio_duration > source_duration + .01:
                report(f"注意: セリフ{number}の音声が動画末尾を超えています。末尾で切り詰めます。")
            last_end = max(last_end, clip.start + clip.audio_duration)

        timeline_rows = []
        for i, c in enumerate(clips):
            row = {"caption": c.scene.caption, "speech": c.scene.speech, "script_index": i, "start": c.start,
                   "end": c.start + c.audio_duration, "audio_duration": c.audio_duration,
                   "reason": getattr(c, "reason", ""), "video_id": source_id}
            timeline_rows.append(row)
        (job / "timeline.json").write_text(json.dumps({"total_seconds": source_duration,
            "source_seconds": source_duration, "video_id": source_id, "warnings": warnings,
            "placements": timeline_rows}, ensure_ascii=False, indent=2), encoding="utf-8")
        # Keep the optional SRT away from the finished MP4. Some media players
        # automatically load nearby subtitle files, which would make MONTAZH's
        # already-burned-in captions appear twice during normal playback.
        metadata_dir = job / "metadata"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        (metadata_dir / "subtitles.srt").write_text("\n\n".join(
            f"{i + 1}\n{srt_time(c.start)} --> {srt_time(c.start + c.audio_duration)}\n{c.scene.caption}"
            for i, c in enumerate(clips)) + "\n", encoding="utf-8-sig")
        (job / "credits.txt").write_text(credit_text(settings), encoding="utf-8-sig")

        with tempfile.TemporaryDirectory(prefix="pmn003_work_", dir=job) as workdir:
            work = Path(workdir)
            base_card = work / "base.png"
            make_overlay(settings, "", 0, len(clips), base_card)
            cards = []
            for i, clip in enumerate(clips):
                card = work / f"card{i:03d}.png"
                make_overlay(settings, clip.scene.caption, i + 1, len(clips), card)
                cards.append(card)

            # Build one continuous narration WAV on the original-video time axis.
            # Mix instead of concatenating so tiny resampling/rounding differences do not
            # trigger false overlap errors, and intentional overlaps remain exportable.
            from array import array
            narration = work / "narration.wav"
            total_frames = max(1, round(source_duration * 48000))
            mixed = array("h", [0]) * total_frames
            for i, (wav_path, clip) in enumerate(zip(wavs, clips)):
                normalized = work / f"normalized{i:03d}.wav"
                ff_run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav_path),
                        "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(normalized)], cancel)
                with wave.open(str(normalized), "rb") as src:
                    raw = src.readframes(src.getnframes())
                samples = array("h")
                samples.frombytes(raw)
                target_frame = max(0, round(clip.start * 48000))
                if target_frame >= total_frames:
                    continue
                usable = min(len(samples), total_frames - target_frame)
                for n in range(usable):
                    idx = target_frame + n
                    value = mixed[idx] + samples[n]
                    mixed[idx] = max(-32768, min(32767, value))
            with wave.open(str(narration), "wb") as joined:
                joined.setnchannels(1); joined.setsampwidth(2); joined.setframerate(48000)
                joined.writeframes(mixed.tobytes())

            # Continuous source video + persistent Promnica frame + caption cards only while spoken.
            args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-filter_complex_threads", "1",
                    "-i", str(video), "-loop", "1", "-framerate", "30", "-i", str(base_card)]
            for card in cards:
                args += ["-loop", "1", "-framerate", "30", "-i", str(card)]
            args += ["-i", str(narration)]
            narration_input = len(cards) + 2
            filters = [base_video_filter(settings), "[screen][1:v]overlay=0:0:format=auto[v0]"]
            current = "v0"
            for i, clip in enumerate(clips):
                nxt = f"v{i+1}"
                end = min(source_duration, clip.start + clip.audio_duration)
                filters.append(f"[{current}][{i+2}:v]overlay=0:0:format=auto:enable='between(t,{clip.start:.6f},{end:.6f})'[{nxt}]")
                current = nxt
            filters.append(f"[{current}]format=yuv420p[v]")

            use_source_audio = False
            if settings.source_audio_enabled and settings.source_audio_volume > 0:
                report("元動画の音声トラックを確認しています…")
                use_source_audio = has_audio_stream(ffmpeg, video, cancel)
                if use_source_audio:
                    volume = settings.source_audio_volume / 100.0
                    report(f"元動画音声を {settings.source_audio_volume:g}% でナレーションとミックスします。")
                    filters.extend([
                        f"[0:a:0]asetpts=PTS-STARTPTS,aresample=48000,"
                        f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                        f"volume={volume:.6f}[source_audio]",
                        f"[{narration_input}:a:0]aresample=48000,"
                        "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[narration_audio]",
                        "[source_audio][narration_audio]"
                        "amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,"
                        "alimiter=limit=0.95,loudnorm=I=-16:TP=-1.5:LRA=11[a]",
                    ])
                else:
                    report("元動画に音声トラックがありません。VOICEVOX音声のみで書き出します。")
            pending = job / "video.mp4.part"
            report("元動画を最後まで保持して、音声と字幕を配置しています…")
            args += ["-filter_complex", ";".join(filters), "-map", "[v]"]
            if use_source_audio:
                args += ["-map", "[a]"]
            else:
                args += ["-map", f"{narration_input}:a:0", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]
            args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads", "4",
                     "-pix_fmt", "yuv420p", "-ar", "48000", "-ac", "2",
                     "-c:a", "aac", "-b:a", "192k", "-t", f"{source_duration:.6f}", "-movflags", "+faststart",
                     "-f", "mp4", str(pending)]
            ff_run_progress(args, cancel)
            progress(94)
            report("完成ファイルを検査しています…")
            ff_run([ffmpeg, "-hide_banner", "-v", "error", "-xerror", "-i", str(pending),
                    "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"], cancel)
            final = job / f"{safe_title}.mp4"
            os.replace(pending, final)
        progress(100)
        report(f"完成: {final}")
        return final
    except Exception as exc:
        try:
            pending_path = job / "video.mp4.part"
            if pending_path.exists():
                pending_path.unlink()
        except OSError:
            pass
        report(f"停止: {exc}\n作業記録: {job}")
        raise

def preview_frame(settings: Settings, destination: Path, cancel=None) -> Path:
    scenes = validate_settings(settings, require_voice=False)
    ffmpeg = find_ffmpeg(settings.ffmpeg_path)
    scene = scenes[0]
    with tempfile.TemporaryDirectory(prefix="pmn003_preview_") as folder:
        work = Path(folder)
        card = work / "card.png"
        make_overlay(settings, scene.caption, 1, len(scenes), card)
        ff_run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-filter_complex_threads", "1",
                "-protocol_whitelist", "file,pipe", "-ss", str(scene.start or 0), "-i", str(Path(settings.video).resolve()),
                "-i", str(card), "-filter_complex", video_filter(settings, 1, 1),
                "-map", "[v]", "-frames:v", "1", "-threads", "1", str(destination)], cancel)
    if not destination.is_file():
        raise FactoryError("プレビューを取得できませんでした。開始時刻を確認してください。")
    return destination
