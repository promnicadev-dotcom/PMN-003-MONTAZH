"""In-process Windows WAV preview. No associated app or external player."""
from pathlib import Path
import threading

from engine import Cancelled, FactoryError, wav_length


def _windows_backend():
    try:
        import winsound
    except ImportError as exc:
        raise FactoryError("アプリ内の試聴はWindowsで利用できます。") from exc
    return winsound


def play_wav(path: Path, cancel: threading.Event) -> None:
    """Run on a worker; an Event interrupts playback without blocking Tk."""
    if cancel.is_set():
        raise Cancelled("試聴を中止しました。")
    duration = wav_length(path.read_bytes())
    backend = _windows_backend()
    try:
        backend.PlaySound(str(path.resolve()), backend.SND_FILENAME | backend.SND_ASYNC | backend.SND_NODEFAULT)
    except RuntimeError as exc:
        raise FactoryError("試聴音声を再生できません。Windowsの音声出力先と音量を確認してください。") from exc
    try:
        # Keep the WAV alive through playback, with a short device-start margin.
        # Event.wait reacts immediately when the user presses Stop.
        if cancel.wait(duration + .20):
            raise Cancelled("試聴を中止しました。")
    finally:
        backend.PlaySound(None, 0)
