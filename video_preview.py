from __future__ import annotations

import os
import tempfile
import wave
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import cv2
from PIL import Image, ImageTk


class VideoPreview(ttk.Frame):
    """Video preview whose playback position is controlled by MONTAZH's main timeline.

    The video frames come from OpenCV. Preview audio is a narration-only WAV assembled
    from the current placement plan, so the user hears exactly where the generated lines
    will speak. The source video's own audio is intentionally not played here.
    """

    def __init__(self, parent, bg: str = "#101722", on_position_change=None):
        super().__init__(parent)
        self.bg = bg
        self.on_position_change = on_position_change
        self.cap = None
        self.path: Path | None = None
        self.duration = 0.0
        self.fps = 30.0
        self.current = 0.0
        self.playing = False
        self._after_id = None
        self._photo = None
        self._seek_internal = False

        self._tempdir = tempfile.TemporaryDirectory(prefix="montazh_preview_")
        self._audio_master: Path | None = None
        self._audio_segment: Path | None = None
        self._audio_available = False

        self.canvas = tk.Canvas(self, background="#080d13", highlightthickness=1,
                                highlightbackground="#344256", width=640, height=320)
        self.canvas.pack(fill="both", expand=True)
        controls = ttk.Frame(self)
        controls.pack(fill="x", pady=(7, 0))
        self.play_button = ttk.Button(controls, text="▶ 再生", command=self.toggle)
        self.play_button.pack(side="left")
        self.seek_var = tk.DoubleVar(value=0.0)
        self.seek_scale = ttk.Scale(controls, from_=0.0, to=1.0, variable=self.seek_var,
                                    command=self._seek_scale_changed)
        self.seek_scale.pack(side="left", fill="x", expand=True, padx=10)
        self.seek_scale.bind("<ButtonPress-1>", self._seek_press, add="+")
        self.seek_scale.bind("<ButtonRelease-1>", self._seek_release, add="+")
        self._seek_was_playing = False
        self.time_var = tk.StringVar(value="0.00 / 0.00 秒")
        ttk.Label(controls, textvariable=self.time_var, width=18).pack(side="right")

    def open(self, path: str | Path, duration: float):
        self.close_media(keep_audio=True)
        self.path = Path(path)
        self.duration = max(0.01, float(duration))
        self.seek_scale.configure(to=self.duration)
        self.cap = cv2.VideoCapture(str(self.path))
        if not self.cap.isOpened():
            self.cap.release()
            self.cap = None
            raise RuntimeError("動画プレビューを開けませんでした。MP4を確認してください。")
        fps = float(self.cap.get(cv2.CAP_PROP_FPS) or 0)
        self.fps = fps if 1 <= fps <= 120 else 30.0
        self.show_at(0.0)

    def set_audio_track(self, path: str | Path | None):
        """Set the narration-only preview WAV matching the current placement plan."""
        self._stop_audio()
        if path is None:
            self._audio_master = None
            self._audio_available = False
            return
        p = Path(path)
        self._audio_master = p if p.is_file() else None
        self._audio_available = bool(self._audio_master)
        if self.playing and self._audio_available:
            self._start_audio_from(self.current)

    def close_media(self, keep_audio: bool = False):
        self.pause()
        if self.cap is not None:
            self.cap.release()
        self.cap = None
        self.path = None
        if not keep_audio:
            self._audio_master = None
            self._audio_segment = None
            self._audio_available = False

    def close(self):
        self.close_media()
        try:
            self._tempdir.cleanup()
        except Exception:
            pass

    def _stop_audio(self):
        if os.name != "nt":
            return
        try:
            import winsound
            winsound.PlaySound(None, 0)
        except Exception:
            pass

    def _start_audio_from(self, seconds: float):
        if not self._audio_available or self._audio_master is None or os.name != "nt":
            return
        try:
            import winsound
            with wave.open(str(self._audio_master), "rb") as src:
                rate = src.getframerate()
                total = src.getnframes()
                start_frame = max(0, min(total, int(float(seconds) * rate)))
                src.setpos(start_frame)
                frames = src.readframes(total - start_frame)
                segment = Path(self._tempdir.name) / "narration_segment.wav"
                with wave.open(str(segment), "wb") as dst:
                    dst.setparams(src.getparams())
                    dst.writeframes(frames)
            self._audio_segment = segment
            winsound.PlaySound(str(segment.resolve()),
                               winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        except Exception:
            self._audio_available = False

    def _notify_position(self):
        if self.on_position_change:
            try:
                self.on_position_change(self.current)
            except Exception:
                pass

    def show_at(self, seconds: float):
        if self.cap is None:
            return
        seconds = max(0.0, min(self.duration, float(seconds)))
        self.cap.set(cv2.CAP_PROP_POS_MSEC, seconds * 1000.0)
        ok, frame = self.cap.read()
        if not ok:
            return
        # Keep the transport clock authoritative. OpenCV frame timestamps can lag
        # behind real-time audio when decoding/rendering is slower than playback.
        self.current = seconds
        self._display(frame)
        self._update_time()
        self._notify_position()

    def seek_to(self, seconds: float):
        was_playing = self.playing
        self.pause()
        self.show_at(seconds)
        if was_playing:
            self.play()

    def _seek_press(self, _event):
        self._seek_was_playing = self.playing
        if self.playing:
            self.pause()

    def _seek_release(self, _event):
        self.seek_to(self.seek_var.get())
        if self._seek_was_playing:
            self.play()
        self._seek_was_playing = False

    def _seek_scale_changed(self, value):
        if self._seek_internal:
            return
        # While dragging, update the preview immediately without restarting audio every pixel.
        try:
            t = max(0.0, min(self.duration, float(value)))
        except (TypeError, ValueError):
            return
        if not self.playing:
            self.show_at(t)

    def toggle(self):
        if self.cap is None:
            return
        if self.playing:
            self.pause()
        else:
            self.play()

    def play(self):
        if self.cap is None or self.playing:
            return
        if self.current >= self.duration - 0.03:
            self.show_at(0.0)
        self.cap.set(cv2.CAP_PROP_POS_MSEC, self.current * 1000.0)
        self.playing = True
        self.play_button.configure(text="⏸ 一時停止")
        self._start_audio_from(self.current)
        self._play_origin_seconds = self.current
        self._play_origin_clock = time.monotonic()
        self._tick()

    def pause(self):
        if self.playing and self._play_origin_clock:
            target = self._play_origin_seconds + (time.monotonic() - self._play_origin_clock)
            target = max(0.0, min(self.duration, target))
            self.playing = False
            self._stop_audio()
            self.show_at(target)
        else:
            self.playing = False
            self._stop_audio()
        self.play_button.configure(text="▶ 再生")
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _tick(self):
        if not self.playing or self.cap is None:
            return
        # Narration is played by Windows in real time. Drive the video and the
        # white playhead from the same wall clock so audio cannot run ahead.
        target = self._play_origin_seconds + (time.monotonic() - self._play_origin_clock)
        if target >= self.duration:
            self.show_at(self.duration)
            self.pause()
            return
        self.show_at(target)
        delay = max(15, int(round(1000.0 / min(self.fps, 30.0))))
        self._after_id = self.after(delay, self._tick)

    def _update_time(self):
        self.time_var.set(f"{self.current:.2f} / {self.duration:.2f} 秒")
        self._seek_internal = True
        try:
            self.seek_var.set(self.current)
        finally:
            self._seek_internal = False

    def _display(self, frame):
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(rgb)
        self.update_idletasks()
        max_w = max(320, self.canvas.winfo_width() - 8)
        max_h = max(180, self.canvas.winfo_height() - 8)
        image.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
        self._photo = ImageTk.PhotoImage(image, master=self.canvas)
        self.canvas.delete("all")
        self.canvas.create_image(self.canvas.winfo_width() // 2, self.canvas.winfo_height() // 2,
                                 image=self._photo, anchor="center")
