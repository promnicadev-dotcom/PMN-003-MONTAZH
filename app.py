"""Japanese desktop UI. All Tk access stays on the main thread."""
from __future__ import annotations

import io
import json
import os
import queue
import re
import shutil
import subprocess
import time
import sys
import tempfile
import threading
import traceback
import wave
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from audio_preview import play_wav
from timeline_widget import TimelineEditor
from video_preview import VideoPreview
from engine import (ACCENT, BG, PRESETS, ROOT, VERSION, CUSTOM_ENGINE, DEFAULT_ENGINE_URL, DEFAULT_SPEAKER_LABEL,
                    OUTPUT_MODES, SHORT_MODE, NORMAL_MODE, SHORT_PRESETS, NORMAL_PRESETS, THEMES, DEFAULT_THEME,
                    ENGINE_PRESETS, NEMO_ENGINE, Cancelled, FactoryError, engine_preset, theme_colors,
                    Settings, Voicevox, build_review_plan, default_output_root, ensure_output_folders,
                    load_project, parse_script, preview_frame, remove_script_scenes, render, save_project, validate_settings,
                    OVERLAP_TOLERANCE, USER_SETTINGS_SCHEMA_VERSION)

EXAMPLE = """これはサンプル動画です。
録画と台本から、音声と字幕を自動で構成します。
必要に応じて、読み上げ位置を調整できます。
内容を確認したら、MP4を書き出します。
"""


def open_file(path: Path):
    if os.name == "nt":
        os.startfile(str(path))
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.strip().lstrip("#")
    if len(value) != 6:
        raise ValueError(f"Unsupported color: {value}")
    return tuple(int(value[i:i+2], 16) for i in (0, 2, 4))


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(v)))) for v in rgb)


def _mix(color_a: str, color_b: str, ratio: float) -> str:
    ratio = max(0.0, min(1.0, float(ratio)))
    a = _hex_to_rgb(color_a)
    b = _hex_to_rgb(color_b)
    return _rgb_to_hex(tuple(a[i] * (1.0 - ratio) + b[i] * ratio for i in range(3)))


def _luminance(color: str) -> float:
    r, g, b = _hex_to_rgb(color)
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


def _is_light(color: str) -> bool:
    return _luminance(color) >= 0.62


class FactoryApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"PMN-003 MONTAZH / Promnica {VERSION}")
        self._set_good_initial_geometry()
        self.minsize(960, 710)
        self.configure(bg=BG)
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.busy = False
        self.busy_state = "IDLE"
        self.audition_active = False
        self.close_after_idle = False
        self.speakers: dict[str, int] = {}
        self.loaded_engine_url = ""
        self.last_output: Path | None = None
        self.current_project: Path | None = None
        self._voicevox_start_attempted: set[str] = set()
        self._voicevox_process = None
        self._user_settings_writable = True
        # Local-only executable preference. Keep this outside project files so an
        # imported .montazh file can never choose a program to execute.
        self.voicevox_dir_path = tk.StringVar(value="")
        self.review_states: list[dict] = []
        self.review_popup = None
        self.preview_directory = tempfile.TemporaryDirectory(prefix="pmn003_ui_")
        self.vars = {}
        self.controls = []
        self._style()
        self._build()
        self._install_wheel_behavior()
        self.vars["engine_url"].trace_add("write", self._engine_url_changed)
        self.vars["theme_id"].trace_add("write", self._theme_changed)
        self._apply_ui_theme()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._drain)
        self._load_user_settings()
        self._load_last()
        self._write_app_log(f"MONTAZH {VERSION} started")
        # Routine use should not require pressing 「声を読み込み」 every launch.
        # If VOICEVOX is already running, silently load the list and select the saved/default voice.
        self.after(250, self._autoload_voices)


    def _set_good_initial_geometry(self):
        """Open at a large, centered working size without forcing maximized mode."""
        self.update_idletasks()
        sw = max(1024, self.winfo_screenwidth())
        sh = max(768, self.winfo_screenheight())
        # 1920x1080 -> 1600x900, matching the intended comfortable desktop size.
        # On smaller displays, leave a little room for borders/taskbar.
        w = min(1600, max(1100, sw - 96))
        h = min(900, max(760, sh - 120))
        w = min(w, sw - 24)
        h = min(h, sh - 48)
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2)
        self.geometry(f"{w}x{h}+{x}+{y}")

    @staticmethod
    def _fit_popup_to_screen(window, width=1680, height=1000):
        """Open review windows near full-screen while keeping borders/taskbar reachable."""
        window.update_idletasks()
        sw = max(1024, window.winfo_screenwidth())
        sh = max(768, window.winfo_screenheight())
        w = min(width, max(1040, sw - 32))
        h = min(height, max(680, sh - 72))
        w = min(w, sw - 16)
        h = min(h, sh - 40)
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 3)
        window.geometry(f"{w}x{h}+{x}+{y}")

    def _style(self):
        self.option_add("*Font", ("Yu Gothic UI", 10))
        style = ttk.Style(self)
        style.theme_use("clam")

    def _current_theme_colors(self) -> dict[str, str]:
        theme_id = self.vars.get("theme_id").get() if self.vars.get("theme_id") else DEFAULT_THEME
        return THEMES.get(theme_id, THEMES[DEFAULT_THEME])

    def _theme_changed(self, *_):
        self._apply_ui_theme()

    def _apply_ui_theme(self):
        colors = self._current_theme_colors()
        bg = colors["bg"]
        text = colors["text"]
        accent = colors["accent"]
        muted = colors["muted"]
        light = _is_light(bg)
        field = _mix(bg, text, 0.10 if light else 0.08)
        panel = _mix(bg, text, 0.07 if light else 0.06)
        panel_active = _mix(panel, accent, 0.22)
        border = _mix(bg, text, 0.23 if light else 0.18)
        disabled_bg = _mix(bg, text, 0.05)
        disabled_fg = _mix(muted, text, 0.15)
        select_bg = _mix(accent, bg, 0.42)
        select_fg = text if _luminance(select_bg) < 0.60 else bg
        accent_fg = bg if _luminance(accent) >= 0.56 else text
        review_row = _mix(bg, text, 0.11 if light else 0.07)
        review_heading = _mix(bg, text, 0.18 if light else 0.12)

        self.option_add("*TCombobox*Listbox.background", field)
        self.option_add("*TCombobox*Listbox.foreground", text)

        style = ttk.Style(self)
        style.configure(".", background=bg, foreground=text, font=("Yu Gothic UI", 10))
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=text)
        style.configure("Muted.TLabel", background=bg, foreground=muted)
        style.configure("Title.TLabel", background=bg, foreground=accent, font=("Yu Gothic UI", 22, "bold"))
        style.configure("TLabelframe", background=bg, bordercolor=border, lightcolor=border, darkcolor=border)
        style.configure("TLabelframe.Label", background=bg, foreground=accent)
        style.configure("TButton", padding=(11, 7), background=panel, foreground=text, bordercolor=border, lightcolor=border, darkcolor=border)
        style.map("TButton", background=[("active", panel_active), ("disabled", disabled_bg)],
                  foreground=[("disabled", disabled_fg)])
        style.configure("Accent.TButton", background=accent, foreground=accent_fg, bordercolor=accent,
                        lightcolor=accent, darkcolor=accent, font=("Yu Gothic UI", 12, "bold"))
        style.map("Accent.TButton", background=[("active", _mix(accent, text, 0.18)), ("disabled", disabled_bg)],
                  foreground=[("disabled", disabled_fg)])
        for name in ("TEntry", "TCombobox", "TSpinbox"):
            style.configure(name, fieldbackground=field, foreground=text, padding=5,
                            insertcolor=text, arrowcolor=text, bordercolor=border, lightcolor=border, darkcolor=border)
            style.map(name, fieldbackground=[("readonly", field)], foreground=[("readonly", text)])
        style.configure("TCheckbutton", background=bg, foreground=text)
        style.map("TCheckbutton", foreground=[("disabled", disabled_fg)])
        style.configure("Horizontal.TProgressbar", background=accent, troughcolor=field, bordercolor=bg)
        style.configure("Review.Treeview", background=review_row, fieldbackground=review_row,
                        foreground=text, rowheight=28, bordercolor=border)
        style.map("Review.Treeview", background=[("selected", select_bg)], foreground=[("selected", select_fg)])
        style.configure("Review.Treeview.Heading", background=review_heading, foreground=text,
                        font=("Yu Gothic UI", 10, "bold"), relief="flat")
        style.map("Review.Treeview.Heading", background=[("active", _mix(review_heading, accent, 0.18))])
        style.configure("ReviewPanel.TLabel", foreground=text, background=bg)
        style.configure("ReviewMuted.TLabel", foreground=muted, background=bg)

        try:
            self.configure(bg=bg)
        except Exception:
            pass
        if hasattr(self, "form_canvas"):
            self.form_canvas.configure(background=bg)
        if hasattr(self, "script"):
            self.script.configure(background=field, foreground=text, insertbackground=text,
                                  selectbackground=select_bg, selectforeground=select_fg)
        if hasattr(self, "log_box"):
            self.log_box.configure(bg=bg, fg=muted, insertbackground=text,
                                   selectbackground=select_bg, selectforeground=select_fg)
        if getattr(self, "review_popup", None) is not None:
            try:
                if self.review_popup.winfo_exists():
                    self.review_popup.configure(bg=bg)
            except Exception:
                pass

    def var(self, name, default=""):
        value = tk.StringVar(value=default)
        self.vars[name] = value
        return value

    def bool_var(self, name, default=False):
        value = tk.BooleanVar(value=default)
        self.vars[name] = value
        return value

    def double_var(self, name, default=0.0):
        value = tk.DoubleVar(value=default)
        self.vars[name] = value
        return value

    def button(self, parent, text, command, **kwargs):
        btn = ttk.Button(parent, text=text, command=command, **kwargs)
        self.controls.append(btn)
        return btn

    def _build(self):
        # Project-specific overlay header. Keep it available even if the user never
        # opens Advanced Settings so save/load round-trips it reliably.
        self.var("header_text", "VIDEO / REPORT")
        self.var("theme_id", DEFAULT_THEME)
        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)
        # Reserve the action bar FIRST, so tall content cannot push it outside
        # the window. The form above it can always be scrolled independently.
        bottom = ttk.Frame(outer)
        bottom.pack(side="bottom", fill="x")
        viewport = ttk.Frame(outer)
        viewport.pack(fill="both", expand=True)
        self.form_canvas = tk.Canvas(viewport, background=BG, highlightthickness=0)
        form_scroll = ttk.Scrollbar(viewport, orient="vertical", command=self.form_canvas.yview)
        self.form_canvas.configure(yscrollcommand=form_scroll.set)
        form_scroll.pack(side="right", fill="y")
        self.form_canvas.pack(side="left", fill="both", expand=True)
        shell = self.form_body = ttk.Frame(self.form_canvas, padding=(0, 0, 8, 0))
        self.form_window = self.form_canvas.create_window((0, 0), window=shell, anchor="nw")
        shell.bind("<Configure>", self._fit_form)
        self.form_canvas.bind("<Configure>", self._fit_form)
        self.bind_all("<MouseWheel>", self._scroll_form, add="+")
        self.bind_all("<Button-4>", self._scroll_form, add="+")
        self.bind_all("<Button-5>", self._scroll_form, add="+")
        top = ttk.Frame(shell)
        top.pack(fill="x")
        ttk.Label(top, text="PMN-003  MONTAZH", style="Title.TLabel").pack(side="left")
        self.button(top, "プロジェクトを開く", self._load).pack(side="right", padx=(7, 0))
        self.button(top, "保存", self._save).pack(side="right")
        ttk.Label(shell, text="録画 ＋ 台本 → 音声・字幕付きMP4。素材はこのPC内で処理します。",
                  style="Muted.TLabel").pack(anchor="w", pady=(5, 16))
        source = ttk.LabelFrame(shell, text="1  録画とタイトル", padding=12)
        source.pack(fill="x", pady=(0, 14))
        source.columnconfigure(1, weight=1)
        ttk.Label(source, text="録画").grid(row=0, column=0, padx=(0, 12), sticky="w")
        ttk.Entry(source, textvariable=self.var("video")).grid(row=0, column=1, sticky="ew")
        self.button(source, "ファイルを選択", self._video).grid(row=0, column=2, padx=(10, 0))
        ttk.Label(source, text="ヘッダー").grid(row=1, column=0, padx=(0, 12), sticky="w", pady=(8, 0))
        ttk.Entry(source, textvariable=self.vars["header_text"]).grid(row=1, column=1, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(source, text="空欄なら非表示", style="Muted.TLabel").grid(row=2, column=1, columnspan=2, sticky="w", pady=(3, 0))
        ttk.Label(source, text="タイトル").grid(row=3, column=0, padx=(0, 12), sticky="w", pady=(8, 0))
        ttk.Entry(source, textvariable=self.var("title", "サンプル動画")).grid(row=3, column=1, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(source, text="自由入力。例: AX-12 CLEANER 重複整理装置 → AX-12 / CLEANER ＋ 重複整理装置として配置",
                  style="Muted.TLabel").grid(row=4, column=1, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(source, text="テーマ").grid(row=5, column=0, padx=(0, 12), sticky="w", pady=(8, 0))
        self.theme_choice = ttk.Combobox(source, textvariable=self.vars["theme_id"], values=list(THEMES), state="readonly", width=24)
        self.theme_choice.grid(row=5, column=1, sticky="w", pady=(8, 0))
        ttk.Label(source, text="背景・文字・アクセント色を5種類から選択", style="Muted.TLabel").grid(row=6, column=1, columnspan=2, sticky="w", pady=(3, 0))
        middle = ttk.Frame(shell)
        middle.pack(fill="both", expand=True)
        middle.columnconfigure(0, weight=1)
        middle.columnconfigure(1, weight=0, minsize=345)
        middle.rowconfigure(0, weight=1)
        editor = ttk.LabelFrame(middle, text="2  台本  /  1行＝1セリフ", padding=12)
        editor.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        ttk.Label(editor, text="そのまま読み上げ・字幕化します。空行は無視します。",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 7))
        area = ttk.Frame(editor)
        area.pack(fill="both", expand=True)
        self.script = tk.Text(area, wrap="word", height=10, width=40, undo=True,
                              background="#172231", foreground="#edf3f7", insertbackground=ACCENT,
                              selectbackground="#36546a", relief="flat", padx=14, pady=12,
                              font=("Yu Gothic UI", 12), spacing1=3, spacing3=7)
        scrollbar = ttk.Scrollbar(area, command=self.script.yview)
        self.script.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.script.pack(fill="both", expand=True)
        self.script.insert("1.0", EXAMPLE)
        actions = ttk.Frame(editor)
        actions.pack(fill="x", pady=(10, 0))
        self.button(actions, "サンプルを読み込む", self._load_sample_script).pack(side="left")
        self.button(actions, "TXTを読み込む", self._script_file).pack(side="left", padx=(6, 0))
        self.button(actions, "TXTに保存", self._save_script_file).pack(side="left", padx=6)
        self.button(actions, "配置を確認・修正", self._review_timeline).pack(side="left")
        self.button(actions, "表示プレビュー", self._preview).pack(side="right")
        ttk.Label(editor, text="時間指定も可: [00:08-00:14] の次の行にセリフ\n"
                  "読みだけ変える場合: 字幕 || 読み上げ文", style="Muted.TLabel").pack(anchor="w", pady=(10, 0))
        right = ttk.LabelFrame(middle, text="3  声と出力", padding=12)
        right.grid(row=0, column=1, sticky="nsew")
        ttk.Label(right, text=f"VOICEVOX起動時は {DEFAULT_SPEAKER_LABEL} を自動選択します。", style="Muted.TLabel").pack(anchor="w")
        ttk.Label(right, text="音声エンジン").pack(anchor="w", pady=(8, 4))
        self.var("engine_url", DEFAULT_ENGINE_URL)
        self.engine_choice_label = tk.StringVar(value=NEMO_ENGINE)
        self.engine_choice = ttk.Combobox(right, textvariable=self.engine_choice_label,
            values=list(ENGINE_PRESETS), state="readonly", width=32)
        self.engine_choice.pack(fill="x")
        self.engine_choice.bind("<<ComboboxSelected>>", self._engine_selected)
        self.button(right, "声一覧を再読み込み", self._voices).pack(fill="x", pady=(7, 8))
        self.voice_choice = ttk.Combobox(right, textvariable=self.var("speaker_label", DEFAULT_SPEAKER_LABEL), state="readonly", width=32)
        self.voice_choice.pack(fill="x")
        rate = ttk.Frame(right)
        rate.pack(fill="x", pady=10)
        ttk.Label(rate, text="話速").pack(side="left")
        ttk.Spinbox(rate, from_=.5, to=2., increment=.05, width=5, textvariable=self.var("speed", "1.10")).pack(side="left", padx=(7, 15))
        ttk.Label(rate, text="余白(秒)").pack(side="left")
        ttk.Spinbox(rate, from_=0, to=2., increment=.05, width=5, textvariable=self.var("gap", "0.20")).pack(side="left", padx=7)
        self.audition_button = self.button(right, "最初のセリフを試聴", self._audition)
        self.audition_button.pack(fill="x", pady=(0, 12))
        ttk.Label(right, text="書き出し用途").pack(anchor="w")
        self.output_mode_choice = ttk.Combobox(
            right, values=list(OUTPUT_MODES), state="readonly",
            textvariable=self.var("output_mode", SHORT_MODE))
        self.output_mode_choice.pack(fill="x", pady=(5, 8))
        self.output_mode_choice.bind("<<ComboboxSelected>>", self._output_mode_changed)
        ttk.Label(right, text="出力サイズ").pack(anchor="w")
        self.preset_choice = ttk.Combobox(
            right, values=list(SHORT_PRESETS), state="readonly",
            textvariable=self.var("preset", SHORT_PRESETS[0]))
        self.preset_choice.pack(fill="x", pady=(5, 10))
        self._output_mode_changed()
        ttk.Label(right, text="保存先（初期値は本体フォルダ内 output / 変更可）").pack(anchor="w")
        ttk.Entry(right, textvariable=self.var("output_dir", str(default_output_root()))).pack(fill="x", pady=5)
        output_actions = ttk.Frame(right)
        output_actions.pack(fill="x")
        self.button(output_actions, "変更", self._output_folder).pack(side="left", expand=True, fill="x")
        self.button(output_actions, "フォルダを開く", self._open_output_root).pack(side="left", expand=True, fill="x", padx=(6, 0))
        ttk.Label(right, text="台本 → scripts / 完成動画 → videos / 音声 → audio",
                  style="Muted.TLabel").pack(anchor="w", pady=(4, 0))

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=12)
        audio_row = ttk.Frame(right)
        audio_row.pack(fill="x")
        self.source_audio_check = ttk.Checkbutton(
            audio_row, text="元動画の音声を使用する",
            variable=self.bool_var("source_audio_enabled", False),
            command=self._source_audio_changed)
        self.source_audio_check.pack(side="left")
        self.source_audio_value = tk.StringVar(value="20%")
        ttk.Label(audio_row, textvariable=self.source_audio_value, style="Muted.TLabel").pack(side="right")
        self.source_audio_scale = ttk.Scale(
            right, from_=0, to=100, orient="horizontal",
            variable=self.double_var("source_audio_volume", 20.0),
            command=self._source_audio_volume_changed)
        self.source_audio_scale.pack(fill="x", pady=(5, 0))
        ttk.Label(right, text="完成MP4での元動画音量。0%はミュートです。",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 0))
        self._source_audio_changed()
        self.button(right, "詳細設定", self._advanced).pack(fill="x", pady=(10, 0))
        for name, default in (("font_path", ""), ("ffmpeg_path", ""), ("logo_path", "")):
            self.var(name, default)
        status_row = ttk.Frame(bottom)
        status_row.pack(fill="x", pady=(15, 6))
        self.status = tk.StringVar(value=f"待機中 / VOICEVOX起動時は {DEFAULT_SPEAKER_LABEL} を自動選択します。")
        ttk.Label(status_row, textvariable=self.status, style="Muted.TLabel").pack(side="left")
        self.progress = ttk.Progressbar(bottom, maximum=100)
        self.progress.pack(fill="x")
        footer = ttk.Frame(bottom)
        footer.pack(fill="x", pady=(12, 0))
        self.button(footer, "MP4を製造する", self._render, style="Accent.TButton").pack(side="left")
        self.cancel_button = ttk.Button(footer, text="中止", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=8)
        self.open_button = ttk.Button(footer, text="完成フォルダを開く", command=self._open_output, state="disabled")
        self.open_button.pack(side="right")
        self.log_box = tk.Text(bottom, height=2, wrap="word", bg=BG, fg="#a9b7c7", relief="flat",
                               font=("Yu Gothic UI", 9), state="disabled")
        self.log_box.pack(fill="x", pady=(10, 0))

    def _fit_form(self, _event=None):
        width = max(1, self.form_canvas.winfo_width())
        height = max(self.form_body.winfo_reqheight(), self.form_canvas.winfo_height())
        self.form_canvas.itemconfigure(self.form_window, width=width, height=height)
        self.form_canvas.configure(scrollregion=(0, 0, width, height))

    @staticmethod
    def _wheel_units(event):
        if getattr(event, "num", None) in (4, 5):
            return -1 if event.num == 4 else 1
        delta = getattr(event, "delta", 0)
        if delta:
            return -int(delta / 120) or (-1 if delta > 0 else 1)
        return 0

    def _scroll_outer(self, units):
        if not units or self.form_body.winfo_reqheight() <= self.form_canvas.winfo_height():
            return
        self.form_canvas.yview_scroll(units, "units")

    def _scroll_form(self, event):
        widget = event.widget
        # Open combobox pop-downs are Tk Listboxes and keep native wheel scrolling.
        # The script Text has its own edge-aware handoff handler below.
        if isinstance(widget, (tk.Text, tk.Listbox)):
            return
        while widget is not None and widget is not self.form_body:
            widget = getattr(widget, "master", None)
        if widget is None and event.widget is not self.form_canvas:
            return
        units = self._wheel_units(event)
        if not units:
            return
        self._scroll_outer(units)
        return "break"

    def _wheel_control_to_outer(self, event):
        # Closed Combobox/Spinbox: wheel means page navigation, never value changes.
        # The expanded Combobox list is a separate Tk Listbox and remains scrollable.
        self._scroll_outer(self._wheel_units(event))
        return "break"

    def _wheel_script_or_outer(self, event):
        units = self._wheel_units(event)
        if not units:
            return "break"
        first, last = self.script.yview()
        can_scroll_inside = (units < 0 and first > 0.0001) or (units > 0 and last < 0.9999)
        if can_scroll_inside:
            self.script.yview_scroll(units, "units")
        else:
            self._scroll_outer(units)
        return "break"

    def _install_wheel_behavior(self):
        # Replace ttk's default wheel-to-value behavior on closed selection controls.
        # The drop-down itself is a Listbox, so long speaker lists still use the wheel normally.
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bind_class("TCombobox", sequence, self._wheel_control_to_outer)
            self.bind_class("TSpinbox", sequence, self._wheel_control_to_outer)
            self.script.bind(sequence, self._wheel_script_or_outer)

    def _engine_url_changed(self, *_):
        self.engine_choice_label.set(engine_preset(self.vars["engine_url"].get()))
        self.speakers.clear()
        self.loaded_engine_url = ""
        self.voice_choice.configure(values=[])
        self.vars["speaker_label"].set("")

    def _engine_selected(self, _event=None):
        label = self.engine_choice_label.get()
        # Custom endpoints are configured only from 詳細設定; they are not a drop-down item.
        if label not in ENGINE_PRESETS:
            return
        if self.vars["engine_url"].get() != ENGINE_PRESETS[label]:
            self.vars["engine_url"].set(ENGINE_PRESETS[label])
        self._voices()

    def _accept_speakers(self, url, voices):
        if url != self.vars["engine_url"].get():
            self.status.set("接続先が変わりました。「声を読み込み」を押してください。")
            return
        self.speakers = dict(voices)
        self.loaded_engine_url = url
        self.voice_choice.configure(values=list(self.speakers))
        if self.vars["speaker_label"].get() not in self.speakers:
            preferred = DEFAULT_SPEAKER_LABEL if engine_preset(url) == NEMO_ENGINE else ""
            if preferred in self.speakers:
                self.vars["speaker_label"].set(preferred)
            else:
                self.vars["speaker_label"].set(next(iter(self.speakers), ""))
        selected = self.vars["speaker_label"].get()
        if selected:
            self.status.set(f"{len(voices)}種類の声を読み込みました。既定の声: {selected}")
        else:
            self.status.set(f"{len(voices)}種類の声を読み込みました。")

    def _settings(self, require_voice=False):
        try:
            data = {key: var.get() for key, var in self.vars.items()}
            data.update(speed=float(data["speed"]), gap=float(data["gap"]),
                        source_audio_volume=float(data["source_audio_volume"]),
                        script=self.script.get("1.0", "end-1c"),
                        speaker_id=(self.speakers.get(data["speaker_label"], -1)
                                    if self.loaded_engine_url == data["engine_url"] else -1))
            settings = Settings(**data, review_states=self.review_states or None)
            if require_voice:
                validate_settings(settings)
            return settings
        except ValueError as exc:
            raise FactoryError("話速・余白には数値を入力してください。") from exc

    def _apply(self, settings):
        # Set endpoint before restoring the voice, as endpoint changes invalidate it.
        self.vars["engine_url"].set(settings.engine_url)
        for name, var in self.vars.items():
            if name != "engine_url":
                var.set(getattr(settings, name))
        self._output_mode_changed()
        self._source_audio_volume_changed(self.vars["source_audio_volume"].get())
        self._source_audio_changed()
        self._apply_ui_theme()
        self.script.delete("1.0", "end")
        self.script.insert("1.0", settings.script)
        self.review_states = list(settings.review_states or [])
        # Speaker IDs must always be verified by a fresh /speakers response.
        self.speakers.clear()
        self.loaded_engine_url = ""
        self.voice_choice.configure(values=[])

    def _video(self):
        path = filedialog.askopenfilename(title="画面録画を選択", filetypes=[("動画", "*.mp4 *.mkv *.mov *.avi *.webm"), ("全ファイル", "*.*")])
        if path:
            self.vars["video"].set(path)

    def _output_folder(self):
        path = filedialog.askdirectory(title="MONTAZHの作業フォルダを選択",
                                       initialdir=self.vars["output_dir"].get() or str(default_output_root()))
        if path:
            try:
                folders = ensure_output_folders(path)
            except OSError as exc:
                self._error(exc)
                return
            self.vars["output_dir"].set(str(folders["root"]))
            self.status.set(f"作業フォルダ: {folders['root']}")
            self._save_user_settings()
            try:
                save_project(self._last_project_path(), self._settings())
            except Exception:
                # The normal close/render paths save the same state again. A preference
                # write failure here must not block the user from choosing a folder.
                pass

    def _open_output_root(self):
        try:
            folders = ensure_output_folders(self.vars["output_dir"].get())
            self.vars["output_dir"].set(str(folders["root"]))
            open_file(folders["root"])
        except OSError as exc:
            self._error(exc)

    def _source_audio_volume_changed(self, value=None):
        try:
            number = float(self.vars["source_audio_volume"].get() if value is None else value)
        except (tk.TclError, ValueError, TypeError):
            number = 20.0
        self.source_audio_value.set(f"{round(number):d}%")


    def _output_mode_changed(self, _event=None):
        mode = self.vars.get("output_mode").get() if "output_mode" in self.vars else SHORT_MODE
        if mode == NORMAL_MODE:
            values = list(NORMAL_PRESETS)
        else:
            values = list(SHORT_PRESETS)
            mode = SHORT_MODE
            if "output_mode" in self.vars:
                self.vars["output_mode"].set(mode)
        if hasattr(self, "preset_choice"):
            self.preset_choice.configure(values=values)
        current = self.vars.get("preset").get() if "preset" in self.vars else ""
        if current not in values and "preset" in self.vars:
            self.vars["preset"].set(values[0])

    def _source_audio_changed(self):
        state = "normal" if self.vars["source_audio_enabled"].get() else "disabled"
        self.source_audio_scale.configure(state=state)

    def _load_sample_script(self):
        current = self.script.get("1.0", "end-1c").strip()
        if current and current != EXAMPLE.strip():
            if not messagebox.askyesno("サンプルを読み込む", "現在の台本をサンプルに置き換えますか？"):
                return
        self.script.delete("1.0", "end")
        self.script.insert("1.0", EXAMPLE)
        self.status.set("サンプル台本を読み込みました。この画面でそのまま編集できます。")

    def _save_script_file(self):
        initial = (self.vars.get("title").get().strip() if self.vars.get("title") else "MONTAZH") or "MONTAZH"
        safe = re.sub(r'[\\/:*?"<>|]+', '_', initial).strip(' .') or "MONTAZH"
        try:
            folders = ensure_output_folders(self.vars["output_dir"].get())
        except OSError as exc:
            self._error(exc)
            return
        path = filedialog.asksaveasfilename(title="台本TXTを保存", defaultextension=".txt",
                                            initialdir=str(folders["scripts"]),
                                            initialfile=f"{safe}_script.txt",
                                            filetypes=[("台本TXT", "*.txt"), ("全ファイル", "*.*")])
        if not path:
            return
        try:
            Path(path).write_text(self.script.get("1.0", "end-1c").rstrip() + "\n", encoding="utf-8-sig")
            self.status.set(f"台本TXTを保存しました: {Path(path).resolve()}")
        except OSError as exc:
            self._error(exc)

    def _script_file(self):
        path = filedialog.askopenfilename(filetypes=[("台本", "*.txt"), ("全ファイル", "*.*")])
        if path:
            try:
                content = Path(path).read_text(encoding="utf-8-sig")
            except UnicodeDecodeError:
                try:
                    content = Path(path).read_text(encoding="cp932")
                except (UnicodeDecodeError, OSError) as exc:
                    self._error(exc)
                    return
            except OSError as exc:
                self._error(exc)
                return
            if self.script.get("1.0", "end-1c").strip() and not messagebox.askyesno("台本の読み込み", "現在の台本を、選択したTXTの内容に置き換えますか？"):
                return
            self.script.delete("1.0", "end")
            self.script.insert("1.0", content)

    def _save(self):
        initial = re.sub(r'[^A-Za-z0-9_-]+', '_', self.vars["title"].get().strip()).strip('_') or "MONTAZH"
        path = filedialog.asksaveasfilename(
            title="MONTAZHプロジェクトを保存", defaultextension=".montazh",
            initialfile=f"{initial}.montazh",
            filetypes=[("MONTAZHプロジェクト", "*.montazh"), ("旧形式JSON", "*.json")])
        if path:
            if self.busy:
                return
            self.busy = True; self.busy_state = "SAVING"
            self.status.set("プロジェクト保存中…")
            self.update_idletasks()
            try:
                target = Path(path)
                save_project(target, self._settings())
                self.current_project = target
                self.status.set(f"保存しました: {target.name}")
                self._write_app_log(f"operation=SAVE result=success path={target}")
            except Exception as exc:
                self._write_app_log(f"operation=SAVE result=failure path={path} error={exc}")
                self._error(exc)
            finally:
                self.busy = False; self.busy_state = "IDLE"

    def _load(self):
        path = filedialog.askopenfilename(
            title="MONTAZHプロジェクトを開く",
            filetypes=[("MONTAZHプロジェクト", "*.montazh *.json"), ("全ファイル", "*.*")])
        if path:
            if not messagebox.askyesno("プロジェクトを開く",
                    "現在の画面を保存済みプロジェクトに置き換えますか？\n"
                    "台本・時間配置・音声キャッシュをまとめて復元します。"):
                return
            if self.busy:
                return
            self.busy = True; self.busy_state = "LOADING"
            self.status.set("プロジェクト読込中…")
            self.update_idletasks()
            try:
                target = Path(path)
                loaded = load_project(target)  # Work state; UI is untouched until this succeeds.
                self._apply(loaded)
                self.current_project = target
                self.status.set("プロジェクトを復元しました。VOICEVOXを自動確認します。")
                self._write_app_log(f"operation=LOAD result=success path={target}")
                self.after(50, self._autoload_voices)
            except Exception as exc:
                self._write_app_log(f"operation=LOAD result=failure path={path} error={exc}")
                self._error(exc)
            finally:
                self.busy = False; self.busy_state = "IDLE"

    def _load_last(self):
        path = self._last_project_path()
        legacy = ROOT / "last_project.json"
        if not path.is_file() and legacy.is_file():
            path = legacy
        if path.is_file():
            try:
                settings = load_project(path)
                old_documents_default = (Path.home() / "Documents" / "MONTAZH").resolve()
                try:
                    current = Path(settings.output_dir).expanduser().resolve() if settings.output_dir else None
                except OSError:
                    current = None
                # v0.2.4 defaulted to Documents\MONTAZH. Move only that default (or an
                # empty value) to the new portable output folder; explicit user choices stay.
                if current is None or current == old_documents_default:
                    settings.output_dir = str(default_output_root())
                self._apply(settings)
            except Exception:
                self.status.set("前回の設定を読めませんでした。新規作成できます。")

    def _help(self):
        messagebox.showinfo("台本の使い方",
            "基本はこの画面で編集するだけです。\n\n"
            "・1行 = 1セリフ\n"
            "・通常は動画全体へ均等に自動配置\n"
            "・『配置を確認・修正』で開始位置を調整\n\n"
            "細かく指定したい場合だけTXT記法を使えます。\n"
            "時間指定: [00:08-00:14] の次の行にセリフ\n"
            "読みだけ変更: 字幕 || 読み上げ文\n\n"
            "『保存』は .montazh 1ファイルに台本・時間配置・再利用できる音声をまとめます。\n"
            "TXT保存は台本文だけを書き出したい場合の補助機能です。")

    def _advanced(self):
        popup = tk.Toplevel(self)
        popup.title("詳細設定")
        popup.configure(bg=self._current_theme_colors()["bg"])
        popup.transient(self)
        frame = ttk.Frame(popup, padding=20)
        frame.pack(fill="both", expand=True)
        # VOICEVOX folder is a machine-local preference, not project data.
        ttk.Label(frame, text="VOICEVOX / Nemo フォルダ（自動起動したい場合だけ指定）").grid(row=0, column=0, sticky="w", pady=(10, 4))
        ttk.Entry(frame, textvariable=self.voicevox_dir_path, width=65).grid(row=1, column=0)
        def choose_voicevox():
            value = filedialog.askdirectory(
                parent=popup, title="VOICEVOX / Nemo が入っているフォルダを選択")
            if value:
                # Selecting a folder only saves the preference. It must never launch VOICEVOX here.
                self.voicevox_dir_path.set(value)
                self._save_user_settings()
                self._voicevox_start_attempted.clear()
        ttk.Button(frame, text="選択", command=choose_voicevox).grid(row=1, column=1, padx=8)

        fields = (("VOICEVOX接続先（このPC内のみ）", "engine_url"),
                  ("日本語フォント（空欄なら自動）", "font_path"),
                  ("FFmpeg実行ファイル（空欄なら自動）", "ffmpeg_path"),
                  ("動画右上のアイコン（空欄なら非表示）", "logo_path"))
        for offset, (label, name) in enumerate(fields, start=1):
            row = offset * 2
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=(10, 4))
            ttk.Entry(frame, textvariable=self.vars[name], width=65).grid(row=row + 1, column=0)
            if name != "engine_url":
                def choose(key=name):
                    if key == "logo_path":
                        value = filedialog.askopenfilename(
                            parent=popup, title="動画に表示するアイコンを選択",
                            filetypes=[("画像", "*.png *.jpg *.jpeg *.webp"), ("全ファイル", "*.*")])
                    else:
                        value = filedialog.askopenfilename(parent=popup)
                    if value:
                        self.vars[key].set(value)
                ttk.Button(frame, text="選択", command=choose).grid(row=row + 1, column=1, padx=8)
        info_row = (len(fields) + 1) * 2
        ttk.Label(frame, text="VOICEVOXのフォルダ指定はこのPCだけに保存し、プロジェクトファイルには含めません。ヘッダー・テーマ・タイトルは『録画とタイトル』で編集し、プロジェクトごとに保存されます。",
                  style="Muted.TLabel", wraplength=560).grid(row=info_row, column=0, columnspan=2, sticky="w", pady=15)
        def close_advanced():
            self._save_user_settings()
            popup.destroy()
        ttk.Button(frame, text="閉じる", command=close_advanced).grid(row=info_row + 1, column=0, sticky="e")
        popup.protocol("WM_DELETE_WINDOW", close_advanced)
        popup.grab_set()

    def _local_state_root(self) -> Path:
        if os.name == "nt":
            base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support"
        else:
            base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        root = base / "Promnica" / "PMN-003-MONTAZH"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _last_project_path(self) -> Path:
        return self._local_state_root() / "last_project.json"

    @staticmethod
    def _atomic_write_json(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=path.stem + "_", suffix=".tmp", dir=path.parent)
        tmp = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.flush(); os.fsync(handle.fileno())
            json.loads(tmp.read_text(encoding="utf-8"))
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()

    def _user_settings_path(self) -> Path:
        return self._local_state_root() / "settings.json"

    def _load_user_settings(self):
        path = self._user_settings_path()
        legacy = ROOT / "user_settings.json"
        source = path if path.is_file() else legacy if legacy.is_file() else None
        if source is None:
            return
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("設定ファイルがオブジェクトではありません")
            schema = data.get("schema_version")
            if schema is None:
                # Known v0.3.x local settings shape. Keep the old file untouched and migrate in memory.
                value = data.get("voicevox_dir_path", "")
                if not value:
                    old_exe = data.get("voicevox_exe_path", "")
                    if isinstance(old_exe, str) and old_exe.strip():
                        old_path = Path(old_exe).expanduser()
                        value = str(old_path.parent) if old_path.suffix.lower() == ".exe" else old_exe
                migrated = {
                    "schema_version": USER_SETTINGS_SCHEMA_VERSION,
                    "voicevox_dir_path": value if isinstance(value, str) else "",
                    "default_output_dir": data.get("default_output_dir", "") if isinstance(data.get("default_output_dir", ""), str) else "",
                    "default_source_audio_volume": float(data.get("default_source_audio_volume", 20.0)),
                    "window_geometry": data.get("window_geometry", "") if isinstance(data.get("window_geometry", ""), str) else "",
                }
                data = migrated
            elif schema != USER_SETTINGS_SCHEMA_VERSION:
                self._user_settings_writable = False
                raise FactoryError(f"未対応の共通設定schema_versionです: {schema}")
            expected = {"schema_version", "voicevox_dir_path", "default_output_dir",
                        "default_source_audio_volume", "window_geometry"}
            if set(data) != expected:
                self._user_settings_writable = False
                raise FactoryError("共通設定の項目構成が一致しません。旧設定は変更しません。")
            self.voicevox_dir_path.set(str(data["voicevox_dir_path"]))
            if data["default_output_dir"]:
                self.vars["output_dir"].set(str(data["default_output_dir"]))
            try:
                volume = float(data["default_source_audio_volume"])
                if 0 <= volume <= 100:
                    self.vars["source_audio_volume"].set(volume)
            except (TypeError, ValueError):
                pass
            geometry = str(data["window_geometry"]).strip()
            if geometry:
                try:
                    self.geometry(geometry)
                except tk.TclError:
                    pass
        except Exception as exc:
            self._write_app_log(f"user settings load failed path={source}: {exc}")
            self.status.set("共通設定を読み込めませんでした。旧設定は変更せず既定値で起動します。")
            self.after(300, lambda: messagebox.showwarning(
                "共通設定", "共通設定を読み込めませんでした。旧設定ファイルは変更していません。\n" + str(exc)))

    def _save_user_settings(self):
        if not self._user_settings_writable:
            return
        try:
            data = {
                "schema_version": USER_SETTINGS_SCHEMA_VERSION,
                "voicevox_dir_path": self.voicevox_dir_path.get().strip(),
                "default_output_dir": self.vars["output_dir"].get().strip(),
                "default_source_audio_volume": float(self.vars["source_audio_volume"].get()),
                "window_geometry": self.geometry(),
            }
            self._atomic_write_json(self._user_settings_path(), data)
        except Exception as exc:
            self._write_app_log(f"user settings save failed: {exc}")

    def _write_app_log(self, message: str):
        try:
            folders = ensure_output_folders(self.vars.get("output_dir").get() if self.vars.get("output_dir") else "")
            path = folders["logs"] / "app.log"
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"[{stamp}] {message.rstrip()}\n")
        except Exception:
            pass

    def report_callback_exception(self, exc, val, tb):
        detail = "".join(traceback.format_exception(exc, val, tb))
        self._write_app_log("Tk callback exception:\n" + detail)
        self.status.set("画面処理でエラーが発生しました。output/logs/app.log を確認してください。")
        try:
            messagebox.showerror("PMN-003", f"画面処理でエラーが発生しました。\n\n{val}\n\nログ: output/logs/app.log")
        except Exception:
            pass

    def _voicevox_candidates(self, url: str) -> list[Path]:
        """Resolve executable candidates only inside the user-selected folder.

        Deliberately no drive/Program Files/PATH auto-search: automatic startup is opt-in.
        """
        if os.name != "nt":
            return []
        configured = self.voicevox_dir_path.get().strip()
        if not configured:
            return []
        folder = Path(configured).expanduser()
        if not folder.is_dir():
            self._write_app_log(f"configured VOICEVOX folder not found: {folder}")
            return []
        port = url.rsplit(":", 1)[-1]
        names = (["VOICEVOX NEMO.exe", "VOICEVOX Nemo.exe", "VOICEVOX.exe"]
                 if port == "50121" else ["VOICEVOX.exe", "VOICEVOX NEMO.exe", "VOICEVOX Nemo.exe"])
        candidates: list[Path] = []
        # Prefer the selected directory itself, then common immediate subfolders.
        search_dirs = [folder, folder / "VOICEVOX", folder / "VOICEVOX NEMO", folder / "VOICEVOX Nemo"]
        for root in search_dirs:
            if not root.is_dir():
                continue
            for name in names:
                candidate = root / name
                if candidate.is_file():
                    candidates.append(candidate)
        # Some portable packages put the launcher one or two folders below the selected root.
        if not candidates:
            try:
                for candidate in folder.glob("*/*.exe"):
                    if candidate.name.lower() in {n.lower() for n in names}:
                        candidates.append(candidate)
                for candidate in folder.glob("*/*/*.exe"):
                    if candidate.name.lower() in {n.lower() for n in names}:
                        candidates.append(candidate)
            except OSError:
                pass
        seen = set(); result = []
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            key = str(resolved).lower()
            if key not in seen and resolved.is_file():
                seen.add(key); result.append(resolved)
        return result

    def _start_voicevox_in_background(self, url: str):
        if url in self._voicevox_start_attempted:
            return
        self._voicevox_start_attempted.add(url)
        candidates = self._voicevox_candidates(url)
        if not candidates:
            self.events.put(("voice_auto_not_found", None))
            return
        exe = candidates[0]
        try:
            kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
            if os.name == "nt":
                startup = subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = 6  # SW_MINIMIZE
                kwargs["startupinfo"] = startup
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            self._voicevox_process = subprocess.Popen([str(exe)], **kwargs)
            self._write_app_log(f"VOICEVOX auto-start: {exe}")
        except OSError as exc:
            self.events.put(("voice_auto_not_found", str(exc)))
            return
        # Wait for the local API without blocking Tk.
        for _ in range(40):
            if self.stop.is_set():
                return
            try:
                voices = Voicevox(url).speakers()
                self.events.put(("speakers_auto", (url, voices)))
                self.events.put(("status", "VOICEVOXを裏で起動し、声を読み込みました。"))
                return
            except Exception:
                time.sleep(.75)
        self.events.put(("voice_auto_not_found", "VOICEVOXを起動しましたがAPIの準備を確認できませんでした。"))

    def _start(self, operation, state="GENERATING"):
        if self.busy:
            return
        self.busy = True
        self.busy_state = state
        self.stop.clear()
        self.engine_choice.configure(state="disabled")
        self.progress["value"] = 0
        for button in self.controls:
            button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.status.set(f"処理中: {state}")
        target_snapshot = self.vars.get("video").get() if self.vars.get("video") else ""
        def worker():
            try:
                operation()
            except Cancelled as exc:
                self.events.put(("log", str(exc)))
                self.events.put(("status", str(exc)))
            except Exception as exc:
                self._write_app_log(f"operation={state} result=failure target={target_snapshot} error={exc}\n" + traceback.format_exc())
                self.events.put(("error", str(exc)))
            finally:
                self.events.put(("idle", None))
        threading.Thread(target=worker, daemon=True).start()

    def _autoload_voices(self):
        """Load VOICEVOX speakers without modal errors or disabling the UI."""
        url = self.vars["engine_url"].get()
        if self.loaded_engine_url == url and self.speakers:
            return
        self.status.set(f"VOICEVOXの声を自動確認中… 既定: {DEFAULT_SPEAKER_LABEL}")

        def worker():
            try:
                voices = Voicevox(url).speakers()
                self.events.put(("speakers_auto", (url, voices)))
            except Exception as exc:
                # Automatic launch is opt-in: only try it when the user configured a folder.
                self.events.put(("voice_auto_unavailable", str(exc)))
                if self.voicevox_dir_path.get().strip():
                    self._start_voicevox_in_background(url)

        threading.Thread(target=worker, daemon=True).start()

    def _voices(self):
        url = self.vars["engine_url"].get()
        self.speakers.clear()
        self.loaded_engine_url = ""
        self._start(lambda: self.events.put(("speakers", (url, Voicevox(url).speakers()))))

    def _audition(self):
        if self.busy:
            if self.audition_active:
                self._cancel()
            return
        try:
            settings = self._settings()
            scenes = parse_script(settings.script)
            if settings.speaker_id < 0:
                raise FactoryError("VOICEVOXを起動してください。通常は既定の声を自動で読み込みます。")
            if not .5 <= settings.speed <= 2:
                raise FactoryError("話速は0.5〜2.0にしてください。")
        except Exception as exc:
            self._error(exc)
            return
        def run():
            self.events.put(("status", "試聴音声を準備しています…"))
            voice = Voicevox(settings.engine_url)
            wav = voice.synthesize(scenes[0].speech, settings.speaker_id, settings.speed)
            if self.stop.is_set():
                raise Cancelled("試聴を中止しました。")
            path = Path(self.preview_directory.name) / "audition.wav"
            path.write_bytes(wav)
            self.events.put(("status", "試聴中 / 同じボタンで停止できます。"))
            play_wav(path, self.stop)
            self.events.put(("status", "試聴が終わりました。"))
        self._start(run)
        self.audition_active = True
        self.audition_button.configure(text="試聴を停止", state="normal")

    @staticmethod
    def _format_seconds(value):
        return f"{float(value):.2f}"

    def _review_timeline(self):
        try:
            settings = self._settings(require_voice=True)
            parse_script(settings.script)
        except Exception as exc:
            self._error(exc)
            return

        popup = tk.Toplevel(self)
        self.review_popup = popup
        popup.title(f"配置を確認・修正 / MONTAZH v{VERSION}")
        self._fit_popup_to_screen(popup)
        popup.minsize(1100, 720)
        popup.configure(bg=self._current_theme_colors()["bg"])
        popup.transient(self)

        outer = ttk.Frame(popup, padding=14)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer,
                  text="映像を見ながら、下の時間軸でシーク・配置調整します。再生音声は元動画ではなく、現在配置したナレーションです。",
                  style="ReviewMuted.TLabel").pack(anchor="w", pady=(0, 9))

        top = ttk.Frame(outer)
        top.pack(fill="both", expand=True)
        top.columnconfigure(0, weight=1)
        top.columnconfigure(1, weight=0, minsize=300)
        top.rowconfigure(0, weight=1)

        preview = VideoPreview(top, self._current_theme_colors()["bg"])
        preview.grid(row=0, column=0, sticky="nsew", padx=(0, 14))

        side = ttk.LabelFrame(top, text="選択中のセリフ", padding=12)
        side.grid(row=0, column=1, sticky="nsew")
        # Reserve the critical save/close buttons at the bottom first so they remain
        # visible even on displays with large Windows scaling.
        side_actions = ttk.Frame(side)
        side_actions.pack(side="bottom", fill="x", pady=(8, 0))
        selected_caption = tk.StringVar(value="—")
        ttk.Label(side, textvariable=selected_caption, wraplength=270,
                  style="ReviewMuted.TLabel").pack(anchor="w", pady=(0, 10))
        start_var = tk.StringVar()
        end_var = tk.StringVar()
        length_var = tk.StringVar()
        warning_var = tk.StringVar(value="配置画面を開きました。音声をキャッシュ確認中…")

        ttk.Label(side, text="読み上げ開始秒（Enterで反映）", style="ReviewPanel.TLabel").pack(anchor="w")
        start_entry = ttk.Entry(side, textvariable=start_var, width=18)
        start_entry.pack(fill="x", pady=(3, 8))
        ttk.Label(side, text="音声の長さ", style="ReviewPanel.TLabel").pack(anchor="w")
        ttk.Label(side, textvariable=length_var, style="ReviewMuted.TLabel").pack(anchor="w", pady=(3, 8))
        ttk.Label(side, text="終了秒", style="ReviewPanel.TLabel").pack(anchor="w")
        ttk.Label(side, textvariable=end_var, style="ReviewMuted.TLabel").pack(anchor="w", pady=(3, 12))

        state = {"rows": [], "source_duration": 0.0, "video_id": "", "selected": None,
                 "audio_blobs": [], "deleted_script_indexes": set()}

        def rebuild_narration_preview():
            rows = state.get("rows") or []
            blobs = state.get("audio_blobs") or []
            if not rows or len(rows) != len(blobs):
                preview.set_audio_track(None)
                return
            target = Path(self.preview_directory.name) / "montazh_timeline_preview.wav"
            try:
                first = wave.open(io.BytesIO(blobs[0]), "rb")
                params = first.getparams()
                first.close()
                channels, sample_width, rate = params.nchannels, params.sampwidth, params.framerate
                frame_width = channels * sample_width
                total_frames = max(1, int(round(float(state["source_duration"]) * rate)))
                mixed = bytearray(total_frames * frame_width)
                for row, blob in zip(rows, blobs):
                    with wave.open(io.BytesIO(blob), "rb") as src:
                        if (src.getnchannels(), src.getsampwidth(), src.getframerate()) != (channels, sample_width, rate):
                            raise ValueError("VOICEVOX音声形式がセリフ間で一致しません。")
                        raw = src.readframes(src.getnframes())
                    start_frame = max(0, int(round(float(row["start"]) * rate)))
                    start_byte = start_frame * frame_width
                    if start_byte >= len(mixed):
                        continue
                    raw = raw[:len(mixed) - start_byte]
                    # Overlap is normally blocked by the UI. If it exists temporarily, later
                    # narration replaces the overlapping bytes so the problem is audible too.
                    mixed[start_byte:start_byte + len(raw)] = raw
                with wave.open(str(target), "wb") as out:
                    out.setnchannels(channels)
                    out.setsampwidth(sample_width)
                    out.setframerate(rate)
                    out.writeframes(bytes(mixed))
                preview.set_audio_track(target)
            except Exception as exc:
                preview.set_audio_track(None)
                warning_var.set("ナレーション試聴音声を作れませんでした: " + str(exc))

        def overlaps_for(i):
            if i is None or i >= len(state["rows"]):
                return []
            row = state["rows"][i]
            a0 = float(row["start"]); a1 = a0 + float(row["audio_duration"])
            hits = []
            for j, other in enumerate(state["rows"]):
                if j == i:
                    continue
                b0 = float(other["start"]); b1 = b0 + float(other["audio_duration"])
                if min(a1, b1) > max(a0, b0) + OVERLAP_TOLERANCE:
                    hits.append(j + 1)
            return hits

        def refresh_warning():
            if not state["rows"]:
                return
            messages = []
            for i, row in enumerate(state["rows"]):
                end = float(row["start"]) + float(row["audio_duration"])
                if end > state["source_duration"] + .01:
                    messages.append(f"{i+1}番が動画末尾を超過")
                hits = overlaps_for(i)
                if hits and any(h > i + 1 for h in hits):
                    messages.append(f"{i+1}番と{hits[0]}番が重複")
            if messages:
                warning_var.set("注意: " + " / ".join(messages[:4]))
            else:
                warning_var.set("重なり・動画末尾超過はありません。全体を再生して位置を確認してください。")

        def update_detail(i, seek=False):
            if i is None or i >= len(state["rows"]):
                state["selected"] = None
                selected_caption.set("—")
                start_var.set("")
                length_var.set("")
                end_var.set("")
                return
            state["selected"] = i
            row = state["rows"][i]
            selected_caption.set(f"#{i+1}  " + row["caption"].replace("\n", " / "))
            start_var.set(self._format_seconds(row["start"]))
            length_var.set(self._format_seconds(row["audio_duration"]) + " 秒")
            end_var.set(self._format_seconds(row["start"] + row["audio_duration"]) + " 秒")
            if seek:
                preview.seek_to(row["start"])

        def on_timeline_select(i):
            update_detail(i, seek=True)

        def on_timeline_change(i, final):
            update_detail(i, seek=bool(final))
            refresh_warning()
            if final:
                rebuild_narration_preview()

        timeline_box = ttk.LabelFrame(outer, text="ナレーション・字幕タイムライン  /  バーを左右へドラッグ", padding=8)
        timeline_box.pack(fill="x", pady=(12, 0))
        timeline = TimelineEditor(timeline_box, on_select=on_timeline_select,
                                  on_change=on_timeline_change, on_seek=lambda t: preview.seek_to(t),
                                  bg=self._current_theme_colors()["bg"],
                                  bar=self._current_theme_colors()["accent"],
                                  selected=_mix(self._current_theme_colors()["accent"], self._current_theme_colors()["text"], 0.25))
        preview.on_position_change = timeline.set_playhead
        timeline.pack(fill="both", expand=True)
        ttk.Label(timeline_box,
                  text="白い縦線＝現在の再生位置。この時間軸がシークバーです。空いている場所をクリックすると映像とナレーションがその時刻へ移動します。赤枠＝明確な音声の重なり（約0.05秒以下の接触誤差は無視）。",
                  style="ReviewMuted.TLabel").pack(anchor="w", pady=(6, 0))

        def apply_current():
            i = state["selected"]
            if i is None:
                messagebox.showinfo("配置確認", "修正するセリフのバーを選んでください。", parent=popup)
                return
            try:
                start = float(start_var.get())
                if start < 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("配置確認", "開始は0以上の秒数にしてください。", parent=popup)
                return
            length = float(state["rows"][i]["audio_duration"])
            latest = max(0.0, state["source_duration"] - length)
            start = min(start, latest)
            row = state["rows"][i]
            row["start"] = start
            row["reason"] = "手動修正"
            timeline.redraw()
            update_detail(i, seek=True)
            refresh_warning()
            rebuild_narration_preview()

        def shift(delta):
            i = state["selected"]
            if i is None:
                return
            try:
                value = float(start_var.get()) + delta
            except ValueError:
                return
            start_var.set(self._format_seconds(max(0.0, value)))
            apply_current()

        def delete_selected():
            i = state["selected"]
            if i is None or not (0 <= i < len(state["rows"])):
                messagebox.showinfo("配置確認", "削除するセリフのバーを選んでください。", parent=popup)
                return
            row = state["rows"].pop(i)
            if i < len(state["audio_blobs"]):
                state["audio_blobs"].pop(i)
            script_index = row.get("script_index")
            if isinstance(script_index, int):
                state["deleted_script_indexes"].add(script_index)
            # Renumber selection while preserving the original script_index values.
            timeline.set_data(state["rows"], state["source_duration"])
            if state["rows"]:
                next_i = min(i, len(state["rows"]) - 1)
                timeline.select(next_i)
                update_detail(next_i, seek=True)
            else:
                timeline.select(None)
                update_detail(None)
            refresh_warning()
            rebuild_narration_preview()
            warning_var.set("選択したセリフを削除しました。『配置を保存して閉じる』で台本にも反映します。")

        start_entry.bind("<Return>", lambda _e: apply_current())
        start_entry.bind("<KP_Enter>", lambda _e: apply_current())
        move = ttk.Frame(side); move.pack(fill="x")
        ttk.Button(move, text="-0.5秒", command=lambda: shift(-.5)).pack(side="left", expand=True, fill="x")
        ttk.Button(move, text="+0.5秒", command=lambda: shift(.5)).pack(side="left", expand=True, fill="x", padx=(5, 0))
        move2 = ttk.Frame(side); move2.pack(fill="x", pady=(5, 0))
        ttk.Button(move2, text="-0.1秒", command=lambda: shift(-.1)).pack(side="left", expand=True, fill="x")
        ttk.Button(move2, text="+0.1秒", command=lambda: shift(.1)).pack(side="left", expand=True, fill="x", padx=(5, 0))
        def delete_key(_event):
            delete_selected()
            return "break"

        timeline.canvas.bind("<Delete>", delete_key)
        ttk.Button(side, text="選択したセリフを削除", command=delete_selected).pack(fill="x", pady=(9, 0))
        ttk.Label(side, text="タイムラインのバーを選択して Delete キーでも削除できます。削除は配置保存時に台本へ反映し、変更せず閉じれば元に戻ります。",
                  style="ReviewMuted.TLabel", wraplength=270).pack(anchor="w", pady=(4, 0))
        ttk.Label(side, textvariable=warning_var, wraplength=270,
                  style="ReviewMuted.TLabel").pack(anchor="w", pady=(14, 10))

        def save_positions():
            if not state["rows"]:
                messagebox.showerror("配置確認", "すべてのセリフは削除できません。最低1セリフ残してください。", parent=popup)
                return
            deleted = set(state.get("deleted_script_indexes") or ())
            if deleted:
                try:
                    updated_script = remove_script_scenes(settings.script, deleted)
                    parse_script(updated_script)
                except Exception as exc:
                    messagebox.showerror("配置確認", str(exc), parent=popup)
                    return
                self.script.delete("1.0", "end")
                self.script.insert("1.0", updated_script)
            self.review_states = [{"caption": r["caption"], "speech": r["speech"], "script_index": n,
                "start": r["start"], "audio_duration": r["audio_duration"], "reason": r.get("reason", ""),
                "video_id": state["video_id"]} for n, r in enumerate(state["rows"])]
            # If the user already chose a project file, keep timeline edits persisted automatically.
            if self.current_project is not None:
                try:
                    save_project(self.current_project, self._settings())
                except Exception as exc:
                    self._write_app_log("project autosave failed: " + repr(exc))
            if deleted:
                self.status.set(f"配置を保存し、{len(deleted)}セリフを台本から削除しました。")
            else:
                self.status.set("MONTAZHの配置を保存しました。")
            preview.close()
            popup.destroy()

        ttk.Button(side_actions, text="配置を保存して閉じる", command=save_positions,
                   style="Accent.TButton").pack(fill="x")
        ttk.Button(side_actions, text="変更せず閉じる", command=lambda: (preview.close(), popup.destroy())).pack(fill="x", pady=(6, 0))

        popup.protocol("WM_DELETE_WINDOW", lambda: (preview.close(), popup.destroy()))

        def run():
            def review_log(msg):
                self.events.put(("log", msg))
                self.events.put(("review_loading_status", (popup, warning_var, msg)))
            clips, warnings, video_id, audio_blobs = build_review_plan(settings,
                review_log, self.stop, lambda value: self.events.put(("progress", value)))
            rows = []
            prior_by_index = {r.get("script_index"): r for r in (self.review_states or [])
                              if isinstance(r, dict) and r.get("video_id") == video_id
                              and isinstance(r.get("script_index"), int)}
            prior = {r.get("caption"): r for r in (self.review_states or [])
                     if isinstance(r, dict) and r.get("video_id") == video_id}
            from engine import find_ffmpeg, probe_duration
            source_duration = probe_duration(find_ffmpeg(settings.ffmpeg_path), Path(settings.video).resolve(), self.stop)
            for script_index, clip in enumerate(clips):
                saved = prior_by_index.get(script_index)
                if saved and (saved.get("caption") != clip.scene.caption or saved.get("speech") != clip.scene.speech):
                    saved = None
                if not saved:
                    saved = prior.get(clip.scene.caption, {})
                rows.append({"caption": clip.scene.caption, "speech": clip.scene.speech,
                    "start": clip.start, "audio_duration": clip.audio_duration,
                    "reason": saved.get("reason", getattr(clip, "reason", "")),
                    "script_index": script_index})
            self.events.put(("review_plan_v019", (popup, preview, timeline, state, rows, warnings,
                                                   warning_var, update_detail, refresh_warning, rebuild_narration_preview,
                                                   source_duration, video_id, settings.video, audio_blobs)))
        self._start(run, "GENERATING")

    def _accept_review_plan_v019(self, payload):
        (popup, preview, timeline, state, rows, warnings, warning_var, update_detail,
         refresh_warning, rebuild_narration_preview, source_duration, video_id, video_path, audio_blobs) = payload
        try:
            if not popup.winfo_exists():
                return
        except tk.TclError:
            return
        state["rows"] = rows
        state["source_duration"] = source_duration
        state["video_id"] = video_id
        state["audio_blobs"] = audio_blobs
        timeline.set_data(rows, source_duration)
        try:
            preview.open(video_path, source_duration)
            rebuild_narration_preview()
        except Exception as exc:
            warning_var.set("動画プレビューを開けませんでした: " + str(exc))
        refresh_warning()
        if warnings:
            warning_var.set("注意: " + " / ".join(warnings[:3]))
        if rows:
            timeline.select(0)
            update_detail(0, seek=False)
        self.status.set("MONTAZHの仮配置を生成しました。下タイムラインでシークし、配置したナレーションを聞きながら確認できます。")

    def _preview(self):
        try:
            settings = self._settings()
        except Exception as exc:
            self._error(exc)
            return
        def run():
            path = Path(self.preview_directory.name) / "preview.png"
            preview_frame(settings, path, self.stop)
            self.events.put(("preview", path))
        self._start(run)

    def _render(self):
        try:
            settings = self._settings(require_voice=True)
            scenes = parse_script(settings.script)
        except Exception as exc:
            self._error(exc)
            return
        if settings.source_audio_enabled and settings.source_audio_volume > 0:
            audio_summary = (f"元動画音声を {settings.source_audio_volume:g}% で残し、"
                             "読み上げ音声とミックスします。\n"
                             "元動画に音声がない場合はVOICEVOXのみで書き出します。")
        else:
            audio_summary = "録画の元音声は使用せず、VOICEVOX音声のみで書き出します。"
        message = (f"{len(scenes)}セリフ / {settings.speaker_label}\n"
                   f"{settings.preset}\n\n{audio_summary}\n"
                   "元動画は最後まで保持し、未調整のセリフは動画全体へ均等配置します。\n"
                   "配置確認で直した位置はそのまま使用します。\n\n製造を開始しますか？")
        if not messagebox.askyesno("MP4の製造", message):
            return
        try:
            save_project(self._last_project_path(), settings)
        except OSError:
            pass
        def run():
            path = render(settings, lambda msg: self.events.put(("log", msg)), self.stop,
                          lambda value: self.events.put(("progress", value)))
            self.events.put(("complete", path))
        self._start(run, "EXPORTING")

    def _cancel(self):
        self.stop.set()
        self.status.set("中止を要求しました。音声生成中は、そのセリフの応答後に停止します。")

    def _drain(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "idle":
                    self.busy = False
                    self.busy_state = "IDLE"
                    self.audition_active = False
                    for button in self.controls:
                        button.configure(state="normal")
                    self.audition_button.configure(text="最初のセリフを試聴")
                    self.engine_choice.configure(state="readonly")
                    self.cancel_button.configure(state="disabled")
                    if self.close_after_idle:
                        self._close()
                        return
                elif kind in ("log", "status"):
                    self.status.set(value.splitlines()[0][:100])
                    if kind == "log":
                        self.log_box.configure(state="normal")
                        self.log_box.insert("end", value + "\n")
                        self.log_box.see("end")
                        self.log_box.configure(state="disabled")
                elif kind == "progress":
                    self.progress["value"] = value
                elif kind == "error":
                    self._error(value)
                elif kind == "speakers":
                    self._accept_speakers(*value)
                elif kind == "speakers_auto":
                    self._accept_speakers(*value)
                elif kind == "voice_auto_unavailable":
                    self.status.set("VOICEVOXが未起動です。自動起動する場合は詳細設定でフォルダを指定してください。")
                elif kind == "voice_auto_not_found":
                    self.status.set("指定したVOICEVOXフォルダから起動できませんでした。設定を確認するか手動起動してください。")
                    if value:
                        self._write_app_log("VOICEVOX auto-start: " + str(value))
                elif kind == "preview":
                    self._show_preview(value)
                    self.status.set("最初のセリフのレイアウトを表示しました。")
                elif kind == "review_loading_status":
                    popup, warning_var, text = value
                    try:
                        if popup.winfo_exists():
                            warning_var.set(text)
                    except tk.TclError:
                        pass
                elif kind == "review_plan_v019":
                    self._accept_review_plan_v019(value)
                elif kind == "complete":
                    self.last_output = value
                    self.open_button.configure(state="normal")
                    self.status.set(f"製造完了: {value}")
                    try:
                        timeline = json.loads((value.parent / "timeline.json").read_text(encoding="utf-8"))
                        warnings = timeline["warnings"]
                        summary = f"完成尺: {timeline['total_seconds']:.1f}秒\n"
                        if warnings:
                            summary += "\n注意:\n" + "\n".join(warnings[:4])
                            if len(warnings) > 4:
                                summary += f"\nほか{len(warnings) - 4}件。render.logで確認できます。"
                        summary += "\n"
                    except (OSError, ValueError, KeyError):
                        summary = ""
                    messagebox.showinfo("製造完了", f"完成しました。\n{value}\n\n{summary}\n"
                                        "字幕・発音・場面の一致を再生して確認してください。\n"
                                        "credits.txtにVOICEVOXのクレジット案も出力しています。")
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def _show_preview(self, path):
        popup = tk.Toplevel(self)
        popup.title("完成レイアウトのプレビュー / 最初のセリフ")
        popup.configure(bg=self._current_theme_colors()["bg"])
        with Image.open(path) as original:
            im = original.copy()
        im.thumbnail((int(self.winfo_screenwidth() * .70), int(self.winfo_screenheight() * .78)))
        photo = ImageTk.PhotoImage(im, master=popup)
        label = tk.Label(popup, image=photo, bg=self._current_theme_colors()["bg"])
        label.image = photo
        label.pack(padx=10, pady=10)

    def _open_output(self):
        if self.last_output:
            try:
                open_file(self.last_output.parent)
            except OSError as exc:
                self._error(exc)

    def _error(self, exc):
        self._write_app_log("error: " + str(exc))
        self.status.set("停止しました。入力とエラー内容を確認してください。")
        messagebox.showerror("PMN-003", str(exc))

    def _close(self):
        if self.busy:
            if self.audition_active:
                self.close_after_idle = True
                self.stop.set()
                self.status.set("試聴を止めて終了します。音声準備中は、その応答を待ちます。")
                return
            if messagebox.askyesno("処理中です", "処理を安全に停止してMONTAZHを終了しますか？"):
                self.close_after_idle = True
                self._cancel()
            return
        try:
            save_project(self._last_project_path(), self._settings())
        except Exception:
            pass
        self._save_user_settings()
        self.preview_directory.cleanup()
        self.destroy()


if __name__ == "__main__":
    try:
        FactoryApp().mainloop()
    except Exception:
        traceback.print_exc()
        try:
            messagebox.showerror("PMN-003 起動エラー", traceback.format_exc())
        except Exception:
            pass
        sys.exit(1)
