from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from tkinter import font as tkfont


OVERLAP_TOLERANCE = 0.05


class TimelineEditor(ttk.Frame):
    """Fit-to-width narration timeline with draggable fixed-duration bars and playhead."""

    LEFT = 42
    RIGHT = 14
    TOP = 30
    ROW_H = 34

    def __init__(self, parent, *, on_select=None, on_change=None, on_seek=None,
                 bg="#101722", bar="#72dec2", selected="#a1efd9"):
        super().__init__(parent)
        self.on_select = on_select
        self.on_change = on_change
        self.on_seek = on_seek
        self.bg = bg
        self.bar = bar
        self.selected = selected
        self.rows: list[dict] = []
        self.duration = 1.0
        self.playhead = 0.0
        self.selected_index: int | None = None
        self.drag_index: int | None = None
        self.drag_offset_seconds = 0.0
        self.dragging_playhead = False
        self._bar_font = tkfont.Font(family="Yu Gothic UI", size=9, weight="bold")

        self.canvas = tk.Canvas(self, background="#121c29", highlightthickness=1,
                                highlightbackground="#344256", height=250, takefocus=True)
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.canvas.bind("<Configure>", lambda _e: self.redraw())
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        # Scroll the vertical timeline with the mouse wheel while the pointer is over it.
        self.canvas.bind("<MouseWheel>", self._mousewheel)
        self.canvas.bind("<Button-4>", lambda _e: self.canvas.yview_scroll(-1, "units"))
        self.canvas.bind("<Button-5>", lambda _e: self.canvas.yview_scroll(1, "units"))

    def set_data(self, rows: list[dict], duration: float):
        self.rows = rows
        self.duration = max(0.01, float(duration))
        self.playhead = max(0.0, min(self.duration, self.playhead))
        if rows and self.selected_index is None:
            self.selected_index = 0
        self.redraw()

    def set_playhead(self, seconds: float):
        seconds = max(0.0, min(self.duration, float(seconds)))
        # Avoid excessive canvas work for tiny video-frame timing differences.
        if abs(seconds - self.playhead) < 0.015:
            return
        self.playhead = seconds
        self.redraw()

    def select(self, index: int | None):
        if index is not None and not (0 <= index < len(self.rows)):
            return
        self.selected_index = index
        self.redraw()
        if index is not None and self.on_select:
            self.on_select(index)

    def redraw(self):
        c = self.canvas
        c.delete("all")
        width = max(520, c.winfo_width())
        plot_w = max(100, width - self.LEFT - self.RIGHT)
        total_h = self.TOP + max(1, len(self.rows)) * self.ROW_H + 12
        c.configure(scrollregion=(0, 0, width, total_h))

        c.create_line(self.LEFT, 22, width - self.RIGHT, 22, fill="#5a6a7f")
        tick_count = 10
        for n in range(tick_count + 1):
            x = self.LEFT + plot_w * n / tick_count
            t = self.duration * n / tick_count
            c.create_line(x, 18, x, 26, fill="#6e8198")
            c.create_text(x, 9, text=f"{t:.0f}", fill="#c7d2de", font=("Yu Gothic UI", 8))

        for i, row in enumerate(self.rows):
            y0 = self.TOP + i * self.ROW_H + 5
            y1 = y0 + 23
            c.create_text(8, (y0 + y1) / 2, text=str(i + 1), fill="#d8e2ec",
                          anchor="w", font=("Yu Gothic UI", 9, "bold"))
            c.create_line(self.LEFT, y1 + 3, width - self.RIGHT, y1 + 3, fill="#243346")
            start = float(row.get("start", 0.0))
            length = max(0.01, float(row.get("audio_duration", 0.01)))
            end = start + length
            x0 = self.LEFT + plot_w * start / self.duration
            x1 = self.LEFT + plot_w * min(end, self.duration) / self.duration
            if x1 - x0 < 5:
                x1 = x0 + 5
            warning = self._warning(i)
            fill = self.selected if i == self.selected_index else self.bar
            outline = "#ffb36b" if warning else "#0a111a"
            rect = c.create_rectangle(x0, y0, x1, y1, fill=fill, outline=outline,
                                      width=3 if warning else 1, tags=(f"bar_{i}", "bar"))
            c.tag_bind(rect, "<Enter>", lambda _e: c.configure(cursor="hand2"))
            c.tag_bind(rect, "<Leave>", lambda _e: c.configure(cursor=""))
            label = row.get("caption", "").replace("\n", " ")
            # Never let caption text spill outside the duration bar: it makes the
            # visual duration look longer than the actual WAV. Truncate by pixel width.
            available = max(0, int(x1 - x0 - 12))
            if available >= 14:
                shown = label
                while shown and self._bar_font.measure(shown) > available:
                    shown = shown[:-1]
                if shown != label and available >= self._bar_font.measure("…"):
                    while shown and self._bar_font.measure(shown + "…") > available:
                        shown = shown[:-1]
                    shown += "…"
                c.create_text(x0 + 6, (y0 + y1) / 2, text=shown, fill="#101722",
                              anchor="w", font=self._bar_font, tags=(f"bar_{i}", "bar"))

        for i in range(1, len(self.rows)):
            a = self.rows[i - 1]
            b = self.rows[i]
            overlap_start = max(float(a["start"]), float(b["start"]))
            overlap_end = min(float(a["start"]) + float(a["audio_duration"]),
                              float(b["start"]) + float(b["audio_duration"]))
            if overlap_end > overlap_start + OVERLAP_TOLERANCE:
                x0 = self.LEFT + plot_w * overlap_start / self.duration
                x1 = self.LEFT + plot_w * min(overlap_end, self.duration) / self.duration
                y0 = self.TOP + (i - 1) * self.ROW_H + 1
                y1 = self.TOP + i * self.ROW_H + 31
                c.create_rectangle(x0, y0, max(x0 + 2, x1), y1, outline="#ff6b6b", width=2)

        # Playback position is shared with the video seek bar.
        px = self.LEFT + plot_w * self.playhead / self.duration
        c.create_line(px, 16, px, total_h - 4, fill="#ffffff", width=2, tags=("playhead",))
        c.create_polygon(px - 5, 16, px + 5, 16, px, 23, fill="#ffffff", outline="", tags=("playhead",))

    def _warning(self, i: int) -> str:
        row = self.rows[i]
        start = float(row["start"])
        end = start + float(row["audio_duration"])
        if end > self.duration + .01:
            return "動画末尾超過"
        for j, other in enumerate(self.rows):
            if j == i:
                continue
            a0, a1 = start, end
            b0 = float(other["start"])
            b1 = b0 + float(other["audio_duration"])
            if min(a1, b1) > max(a0, b0) + OVERLAP_TOLERANCE:
                return "音声重複"
        return ""

    def _index_at(self, event) -> int | None:
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        items = self.canvas.find_overlapping(x, y, x, y)
        for item in reversed(items):
            for tag in self.canvas.gettags(item):
                if tag.startswith("bar_"):
                    try:
                        return int(tag.split("_", 1)[1])
                    except ValueError:
                        pass
        return None

    def _seconds_at_x(self, x: float) -> float:
        width = max(520, self.canvas.winfo_width())
        plot_w = max(100, width - self.LEFT - self.RIGHT)
        return max(0.0, min(self.duration, (x - self.LEFT) / plot_w * self.duration))

    def _press(self, event):
        # Keyboard shortcuts (notably Delete) should apply to the clicked timeline bar.
        self.canvas.focus_set()
        x = self.canvas.canvasx(event.x)
        width = max(520, self.canvas.winfo_width())
        plot_w = max(100, width - self.LEFT - self.RIGHT)
        playhead_x = self.LEFT + plot_w * self.playhead / self.duration
        # The white playhead itself can be grabbed and scrubbed.
        if abs(x - playhead_x) <= 9:
            self.dragging_playhead = True
            t = self._seconds_at_x(x)
            self.playhead = t
            self.redraw()
            if self.on_seek:
                self.on_seek(t)
            return
        i = self._index_at(event)
        if i is None:
            t = self._seconds_at_x(x)
            self.playhead = t
            self.redraw()
            if self.on_seek:
                self.on_seek(t)
            return
        self.selected_index = i
        pointer_t = self._seconds_at_x(self.canvas.canvasx(event.x))
        self.drag_offset_seconds = pointer_t - float(self.rows[i]["start"])
        self.drag_index = i
        self.redraw()
        if self.on_select:
            self.on_select(i)

    def _drag(self, event):
        if self.dragging_playhead:
            t = self._seconds_at_x(self.canvas.canvasx(event.x))
            self.playhead = t
            self.redraw()
            if self.on_seek:
                self.on_seek(t)
            return
        i = self.drag_index
        if i is None:
            return
        pointer_t = self._seconds_at_x(self.canvas.canvasx(event.x))
        length = float(self.rows[i]["audio_duration"])
        latest = max(0.0, self.duration - length)
        start = max(0.0, min(latest, pointer_t - self.drag_offset_seconds))
        self.rows[i]["start"] = start
        self.rows[i]["reason"] = "手動修正"
        self.redraw()
        if self.on_change:
            self.on_change(i, False)

    def _mousewheel(self, event):
        # Windows reports multiples of 120. Trackpads may report smaller deltas.
        delta = getattr(event, "delta", 0)
        if not delta:
            return "break"
        steps = -int(delta / 120) if abs(delta) >= 120 else (-1 if delta > 0 else 1)
        self.canvas.yview_scroll(steps, "units")
        return "break"

    def _release(self, _event):
        if self.dragging_playhead:
            self.dragging_playhead = False
            return
        i = self.drag_index
        self.drag_index = None
        if i is not None and self.on_change:
            self.on_change(i, True)
