import datetime
import random
import threading
import tkinter as tk

from stb_player.constants import (
    C_BADGE_BG,
    C_BG,
    C_BLUE_DOT,
    C_BORDER,
    C_DIM,
    C_DIVIDER,
    C_GREEN,
    C_LIGHT,
    C_PROG_ACTIVE,
    C_PROG_NEXT,
    C_PROGRESS_BG,
    C_PROGRESS_FG,
    C_WHITE,
    C_YELLOW,
    EPG_AUTO_HIDE_MS,
    MAIL_INTERVAL_MS,
)


class UiMixin:
    def _show_language_picker(self):
        channel = self.current_channel
        source = channel.get("source", "") if channel else ""
        is_youtube = isinstance(source, str) and (
            source.startswith("yt:") or "youtube.com" in source
        )
        if not is_youtube:
            return

        url = channel.get("_resolved_src") or channel.get("source", "")
        if not url:
            return

        window = tk.Toplevel(self.root)
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        window.attributes("-alpha", 0.95)
        window.configure(bg=C_BG)

        border = tk.Frame(window, bg=C_PROG_ACTIVE, bd=0)
        border.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        tk.Frame(border, bg="#3B6FD4", height=3).pack(fill=tk.X)

        inner = tk.Frame(border, bg=C_BG)
        inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        tk.Label(
            inner,
            text="Select Audio Language",
            fg=C_WHITE,
            bg=C_BG,
            font=("Arial", 13, "bold"),
            pady=10,
        ).pack()
        tk.Frame(inner, bg=C_DIVIDER, height=1).pack(fill=tk.X, padx=10)

        loading = tk.Label(
            inner,
            text="Fetching available audio tracks...",
            fg=C_DIM,
            bg=C_BG,
            font=("Arial", 11),
            pady=12,
        )
        loading.pack()

        window.bind("<Escape>", lambda _event: window.destroy())
        window.bind(
            "<Key>",
            lambda event: window.destroy() if event.keysym == "Escape" else None,
        )
        window.focus_force()

        self.root.update_idletasks()
        root_w = self.root.winfo_width()
        root_h = self.root.winfo_height()
        root_x = self.root.winfo_rootx()
        root_y = self.root.winfo_rooty()
        win_w, win_h = 500, 340
        window.geometry(f"{win_w}x{win_h}+{root_x + (root_w-win_w)//2}+{root_y + (root_h-win_h)//2}")

        def _fetch():
            tracks = self._get_audio_tracks(url)
            self.root.after(0, lambda: _populate(tracks))

        def _populate(tracks):
            if not window.winfo_exists():
                return
            loading.destroy()
            if not tracks:
                tk.Label(
                    inner,
                    text="No alternate audio tracks found.",
                    fg=C_DIM,
                    bg=C_BG,
                    font=("Arial", 11),
                    pady=16,
                ).pack()
                return

            for index, (lang, label, fmt_id) in enumerate(tracks):
                row_bg = C_PROG_ACTIVE if index == 0 else C_BG
                row_fg = C_WHITE if index == 0 else C_LIGHT

                def _make_row(idx, bg, fg, lang_id, format_id, title):
                    row = tk.Frame(inner, bg=bg, cursor="hand2")
                    row.pack(fill=tk.X, padx=8, pady=2)
                    prefix = tk.Label(
                        row,
                        text=">  " if idx == 0 else "   ",
                        fg=fg,
                        bg=bg,
                        font=("Arial", 12),
                        padx=8,
                        pady=5,
                    )
                    prefix.pack(side=tk.LEFT)
                    tk.Label(
                        row,
                        text=title,
                        fg=fg,
                        bg=bg,
                        font=("Arial", 12),
                        pady=5,
                        anchor="w",
                    ).pack(side=tk.LEFT, fill=tk.X, expand=True)

                    def _hover_on(_event, row_ref=row):
                        row_ref.config(bg=C_PROG_ACTIVE)
                        for widget in row_ref.winfo_children():
                            widget.config(bg=C_PROG_ACTIVE, fg=C_WHITE)

                    def _hover_off(_event, row_ref=row, row_idx=idx):
                        bg_value = C_PROG_ACTIVE if row_idx == 0 else C_BG
                        fg_value = C_WHITE if row_idx == 0 else C_LIGHT
                        row_ref.config(bg=bg_value)
                        for widget in row_ref.winfo_children():
                            widget.config(bg=bg_value, fg=fg_value)

                    def _click(_event, format_ref=format_id, lang_ref=lang_id):
                        window.destroy()
                        self._switch_audio_track(format_ref, lang_ref)

                    row.bind("<Enter>", _hover_on)
                    row.bind("<Leave>", _hover_off)
                    row.bind("<Button-1>", _click)
                    for widget in row.winfo_children():
                        widget.bind("<Button-1>", _click)

                _make_row(index, row_bg, row_fg, lang, fmt_id, label)

            tk.Frame(inner, bg=C_DIVIDER, height=1).pack(fill=tk.X, padx=10, pady=(8, 4))
            tk.Label(
                inner,
                text="Click to select   -   Esc to close",
                fg=C_DIM,
                bg=C_BG,
                font=("Arial", 9),
                pady=4,
            ).pack()

        threading.Thread(target=_fetch, daemon=True).start()

    def _prepare_vol_bar(self):
        self._vol_win = tk.Toplevel(self.root)
        self._vol_win.withdraw()
        self._vol_win.overrideredirect(True)
        self._vol_win.attributes("-topmost", True)
        self._vol_win.attributes("-alpha", 0.93)
        self._vol_win.configure(bg=C_BG)

        border = tk.Frame(self._vol_win, bg=C_PROG_ACTIVE, bd=0)
        border.pack(padx=2, pady=2)
        inner = tk.Frame(border, bg=C_BG)
        inner.pack(padx=6, pady=(10, 6))

        bars = 20
        bar_w = 38
        bar_h = 8
        gap = 5
        canvas_h = bars * (bar_h + gap)

        self._vol_canvas = tk.Canvas(
            inner,
            bg=C_BG,
            highlightthickness=0,
            width=bar_w,
            height=canvas_h,
        )
        self._vol_canvas.pack()

        self._bar_ids = []
        for index in range(bars):
            y1 = canvas_h - (index + 1) * (bar_h + gap) + gap
            y2 = y1 + bar_h
            bar_id = self._vol_canvas.create_rectangle(0, y1, bar_w, y2, fill=C_PROGRESS_BG, outline="")
            self._bar_ids.append(bar_id)

        self._vol_pct = tk.Label(
            inner,
            text="70%",
            fg=C_WHITE,
            bg=C_BG,
            font=("Arial", 10, "bold"),
        )
        self._vol_pct.pack(pady=(6, 0))

        self._VOL_W = bar_w + 28
        self._VOL_H = canvas_h + 60

    def _change_volume(self, delta: int):
        self._volume = max(0, min(100, self._volume + delta))
        self.player.audio_set_volume(self._volume)
        self._show_vol_bar()

    def _show_vol_bar(self):
        filled = round(self._volume / 100 * len(self._bar_ids))
        for index, bar_id in enumerate(self._bar_ids):
            self._vol_canvas.itemconfig(bar_id, fill=C_PROGRESS_FG if index < filled else C_PROGRESS_BG)
        self._vol_pct.config(text=f"{self._volume}%")

        self.root.update_idletasks()
        root_w = self.root.winfo_width()
        root_h = self.root.winfo_height()
        root_x = self.root.winfo_rootx()
        root_y = self.root.winfo_rooty()
        x = root_x + root_w - self._VOL_W - int(root_w * 0.025)
        y = root_y + (root_h - self._VOL_H) // 2
        self._vol_win.geometry(f"{self._VOL_W}x{self._VOL_H}+{x}+{y}")
        self._vol_win.deiconify()
        self._vol_win.lift()
        if self._vol_hide:
            self.root.after_cancel(self._vol_hide)
        self._vol_hide = self.root.after(2500, self._hide_vol_bar)

    def _hide_vol_bar(self):
        self._vol_win.withdraw()

    def _prepare_mail(self):
        self._mail_win = tk.Toplevel(self.root)
        self._mail_win.withdraw()
        self._mail_win.overrideredirect(True)
        self._mail_win.attributes("-topmost", True)
        self._mail_win.attributes("-alpha", 0.92)
        self._mail_win.configure(bg=C_BADGE_BG)

        border = tk.Frame(self._mail_win, bg=C_PROG_ACTIVE, bd=0)
        border.pack(padx=2, pady=2)
        tk.Label(
            border,
            text="Mail",
            fg=C_YELLOW,
            bg=C_BADGE_BG,
            font=("Arial", 18, "bold"),
            padx=12,
            pady=6,
        ).pack()

    def _show_mail(self):
        self._mail_visible = True
        self.root.update_idletasks()
        self._mail_win.update_idletasks()
        root_w = self.root.winfo_width()
        root_x = self.root.winfo_rootx()
        root_y = self.root.winfo_rooty()
        mail_w = self._mail_win.winfo_reqwidth()
        x = root_x + (root_w - mail_w) // 2
        y = root_y + 28
        self._mail_win.geometry(f"+{x}+{y}")
        self._mail_win.deiconify()
        self._mail_win.lift()
        if self._mail_hide_job:
            self.root.after_cancel(self._mail_hide_job)
        self._mail_hide_job = self.root.after(6000, self._hide_mail)

    def _hide_mail(self):
        self._mail_visible = False
        self._mail_win.withdraw()

    def _schedule_next_mail(self):
        jitter = random.randint(-600_000, 600_000)
        delay = max(10_000, MAIL_INTERVAL_MS + jitter)
        self.root.after(delay, self._trigger_mail)

    def _trigger_mail(self):
        self._show_mail()
        self._schedule_next_mail()

    def _prepare_epg(self):
        self.epg_window = tk.Toplevel(self.root)
        self.epg_window.withdraw()
        self.epg_window.overrideredirect(True)
        self.epg_window.attributes("-topmost", True)
        self.epg_window.attributes("-alpha", 0.92)
        self.epg_window.configure(bg=C_BG)

        outer = tk.Frame(self.epg_window, bg=C_BORDER)
        outer.pack(fill=tk.BOTH, expand=True)
        tk.Frame(outer, bg="#3B6FD4", height=3).pack(fill=tk.X)

        body = tk.Frame(outer, bg=C_BG)
        body.pack(fill=tk.BOTH, expand=True, padx=1, pady=(0, 1))

        layout = tk.Frame(body, bg=C_BG)
        layout.pack(fill=tk.BOTH, expand=True)

        img_size = 190
        img_frame = tk.Frame(layout, bg=C_BG, highlightbackground="#3B6FD4", highlightthickness=2, bd=0)
        img_frame.pack(side=tk.LEFT, padx=(10, 8), pady=8)
        self._epg_img_canvas = tk.Canvas(
            img_frame,
            width=img_size,
            height=img_size,
            bg=C_PROG_NEXT,
            highlightthickness=0,
            bd=0,
        )
        self._epg_img_canvas.pack()
        self._epg_img_id = self._epg_img_canvas.create_image(img_size // 2, img_size // 2, anchor="center")

        info = tk.Frame(layout, bg=C_BG)
        info.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8))

        row1 = tk.Frame(info, bg=C_BG)
        row1.pack(fill=tk.X, pady=(10, 4))
        nav = tk.Frame(row1, bg=C_BG)
        nav.pack(side=tk.LEFT)
        tk.Label(nav, text="<", fg="#7BA4E0", bg=C_BG, font=("Arial", 13, "bold")).pack(side=tk.LEFT, padx=(0, 6))
        self.epg_ch_num = tk.Label(
            nav,
            text="",
            fg=C_WHITE,
            bg=C_PROG_ACTIVE,
            font=("Arial", 14, "bold"),
            padx=8,
            pady=2,
        )
        self.epg_ch_num.pack(side=tk.LEFT, padx=(0, 8))
        self.epg_ch_name = tk.Label(nav, text="", fg=C_WHITE, bg=C_BG, font=("Arial", 15, "bold"), anchor="w")
        self.epg_ch_name.pack(side=tk.LEFT)
        tk.Label(nav, text=">", fg="#7BA4E0", bg=C_BG, font=("Arial", 13, "bold")).pack(side=tk.LEFT, padx=(8, 0))
        date_chip = tk.Frame(row1, bg="#1A2340", padx=12, pady=4)
        date_chip.pack(side=tk.RIGHT, padx=(0, 4))
        self.date_label = tk.Label(date_chip, text="", fg=C_WHITE, bg="#1A2340", font=("Arial", 12))
        self.date_label.pack()

        tk.Frame(info, bg=C_DIVIDER, height=1).pack(fill=tk.X, padx=4)

        row_active = tk.Frame(info, bg=C_PROG_ACTIVE)
        row_active.pack(fill=tk.X, padx=4, pady=(5, 2))
        self.epg_active_title = tk.Label(
            row_active,
            text="",
            fg=C_WHITE,
            bg=C_PROG_ACTIVE,
            anchor="w",
            font=("Arial", 14, "bold"),
            padx=12,
            pady=6,
        )
        self.epg_active_title.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.epg_active_time = tk.Label(
            row_active,
            text="",
            fg="#BDD7FF",
            bg=C_PROG_ACTIVE,
            font=("Arial", 11),
            padx=12,
            pady=6,
        )
        self.epg_active_time.pack(side=tk.RIGHT)

        row_next = tk.Frame(info, bg=C_PROG_NEXT)
        row_next.pack(fill=tk.X, padx=4, pady=(0, 5))
        self.epg_next_title = tk.Label(
            row_next,
            text="",
            fg=C_LIGHT,
            bg=C_PROG_NEXT,
            anchor="w",
            font=("Arial", 12),
            padx=12,
            pady=4,
        )
        self.epg_next_title.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.epg_next_time = tk.Label(
            row_next,
            text="",
            fg=C_DIM,
            bg=C_PROG_NEXT,
            font=("Arial", 10),
            padx=12,
            pady=4,
        )
        self.epg_next_time.pack(side=tk.RIGHT)

        tk.Frame(info, bg=C_DIVIDER, height=1).pack(fill=tk.X, padx=4)

        footer = tk.Frame(info, bg=C_BG)
        footer.pack(fill=tk.X, pady=(6, 8))
        progress_area = tk.Frame(footer, bg=C_BG)
        progress_area.pack(side=tk.LEFT, fill=tk.Y, anchor="w", padx=(4, 0))
        self.epg_progress = tk.Canvas(progress_area, width=220, height=10, bg=C_BG, highlightthickness=0, bd=0)
        self.epg_progress.pack(anchor="w")
        self.epg_progress.create_rectangle(0, 2, 220, 8, fill=C_PROGRESS_BG, outline="")
        self.progress_fill = self.epg_progress.create_rectangle(0, 2, 0, 8, fill=C_PROGRESS_FG, outline="")

        btn_frame = tk.Frame(footer, bg=C_BG)
        btn_frame.pack(side=tk.RIGHT, anchor="e", padx=(0, 8))
        for color, label in (
            ("#3B82F6", "Search"),
            (C_GREEN, "Genres"),
            (C_YELLOW, "Alerts"),
            (C_BLUE_DOT, "Language"),
        ):
            item = tk.Frame(btn_frame, bg=C_BG)
            item.pack(side=tk.LEFT, padx=(0, 14))
            tk.Label(item, text=label, fg=color, bg=C_BG, font=("Arial", 9, "bold")).pack()

    def _update_epg(self, channel, browsing=False):
        number = channel.get("number", "")
        name = channel.get("name", "")
        self.epg_ch_num.config(text=f" {number} ")
        self.epg_ch_name.config(text=name)

        photo = self._pick_image(190, 190)
        if photo:
            self._current_img = photo
            self._epg_img_canvas.itemconfig(self._epg_img_id, image=photo)

        if not browsing:
            self._epg_items = self._build_epg_items(channel)
        else:
            items = []
            for schedule in channel.get("schedule", []):
                time_str = (
                    f"{schedule.get('start', '')} - {schedule.get('end', '')}"
                    if schedule.get("start")
                    else ""
                )
                items.append((schedule.get("title", ""), time_str, None))
            if not items:
                items = [(channel.get("name", ""), "", None), ("", "", None)]
            self._epg_items = items

        self._epg_row_index = 0
        self._render_epg_rows()

        now = datetime.datetime.now()
        self.date_label.config(text=now.strftime("%a %d %b  %I:%M %p"))

        if not browsing:
            duration = self.player.get_length()
            position = self.player.get_time()
            if duration > 0 and position >= 0:
                fill_w = max(2, min(219, int(220 * position / duration)))
                self.epg_progress.coords(self.progress_fill, 0, 2, fill_w, 8)
            else:
                self.epg_progress.coords(self.progress_fill, 0, 2, 0, 8)

    def show_epg(self, auto_hide=EPG_AUTO_HIDE_MS):
        if self.current_channel:
            self._update_epg(self.current_channel)

        self.root.update_idletasks()
        root_w = self.root.winfo_width()
        root_h = self.root.winfo_height()
        root_x = self.root.winfo_rootx()
        root_y = self.root.winfo_rooty()
        width = min(1100, int(root_w * 0.94))
        height = 240
        x = root_x + (root_w - width) // 2
        y = root_y + root_h - height - 36
        self.epg_window.geometry(f"{width}x{height}+{x}+{y}")
        self.epg_window.deiconify()
        self.epg_window.lift()

        if self.hide_job:
            self.root.after_cancel(self.hide_job)
        if auto_hide:
            self.hide_job = self.root.after(auto_hide, self.hide_epg)

    def hide_epg(self):
        self.epg_window.withdraw()
        if self._browse_num is not None:
            self._browse_num = None
            if self.current_channel:
                self._update_epg(self.current_channel)
