"""
stb_player/mixins/base.py
=========================
Application bootstrap, channel loading and startup warmup.

What changed vs original
------------------------
* A ``ChannelScheduler`` instance is created at init time.
* ``_warmup_channel`` reads the scheduler cache first; only hits YouTube
  when the cache is stale (> 24 h old).  After fetching, video metadata
  (url, title, duration) is saved to ``video_cache.json``.
* The startup overlay lifts as soon as warmup finishes â€“ no fixed timeout.
* Channel 100 (or the first channel) is pre-resolved to the
  *clock-scheduled* video so playback starts instantly.
"""

import glob
import os
from pathlib import Path
import random
import sys
import threading
import tkinter as tk
from tkinter import messagebox

import vlc

from stb_player.constants import (
    BASE_DIR,
    CHANNELS_FILE,
    C_BADGE_BG,
    C_DIM,
    C_PROG_ACTIVE,
    C_WHITE,
    EPG_AUTO_HIDE_MS,
    ADSQUARE_DIR,
    IMG_EXTS,
    STREAM_CACHE_FILE,
    VIDEO_CACHE_FILE,
)
from stb_player.scheduler import ChannelScheduler


class BaseMixin:
    def __init__(self, root):
        self.root = root
        self.root.title("STB Media Player")
        self.root.attributes("-fullscreen", True)
        self.root.configure(bg="black")

        self.channels = self._load_channels()
        self.current_channel = {}
        self.channel_request_id = 0

        self.channel_buffer = ""
        self.buffer_job = None
        self.ui_mode = "NORMAL"

        self._browse_num = None
        self._browse_hide_job = None

        self._epg_row_index = 0
        self._epg_items = []

        self.channel_state: dict = {}

        self._preload_result = None
        self._preload_channel = None

        self._mail_visible = False
        self._mail_hide_job = None

        self._volume = 70
        self._vol_hide = None
        self._epg_tick = None
        self._startup_overlay = None
        self._startup_status = None
        self._startup_spinner_job = None
        self._startup_spinner_index = 0
        self._startup_done = 0
        self._startup_total = max(1, len(self.channels))
        self._startup_finished = False
        self._suppress_end_event = False

        # Persistent scheduler â€“ video cache + clock-based schedule.
        self.scheduler = ChannelScheduler(
            VIDEO_CACHE_FILE,
            STREAM_CACHE_FILE,
            str(Path(BASE_DIR)),
        )

        try:
            self.instance = vlc.Instance(
                "--quiet",
                "--no-video-title-show",
                "--avcodec-hw=none",
            )
            self.player = self.instance.media_player_new()
            self.player.audio_set_volume(self._volume)
            self._player_events = self.player.event_manager()
            self._player_events.event_attach(
                vlc.EventType.MediaPlayerEndReached,
                self._on_media_end,
            )
        except Exception:
            messagebox.showerror("VLC Error", "VLC not detected.")
            sys.exit(1)

        self.video_panel = tk.Frame(root, bg="black")
        self.video_panel.pack(fill=tk.BOTH, expand=True)

        root.bind("<Key>", self.on_keypress)

        self.epg_window = None
        self.hide_job = None
        self._prepare_epg()
        self._prepare_mail()
        self._schedule_next_mail()
        self._prepare_ch_badge()
        self._prepare_vol_bar()

        self._img_pool = self._scan_images()
        self._current_img = None
        self._show_startup_loading()
        self._start_channel_warmup()

    # ------------------------------------------------------------------
    # Channel loading
    # ------------------------------------------------------------------

    def _load_channels(self):
        import json

        try:
            with open(CHANNELS_FILE, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            return {}
        for num, info in data.items():
            info.setdefault("number", num)
        return data

    def _scan_images(self):
    
        files = []
    
        if not os.path.exists(ADSQUARE_DIR):
            return files
    
        for ext in IMG_EXTS:
        
            files.extend(
                glob.glob(
                    os.path.join(
                        ADSQUARE_DIR,
                        ext,
                    )
                )
            )
    
        return sorted(files)
    
        # ------------------------------------------------------------------
        # Startup overlay
        # ------------------------------------------------------------------

    def _show_startup_loading(self):
        overlay = tk.Frame(self.root, bg="black")
        overlay.place(relx=0, rely=0, relwidth=1, relheight=1)

        title_lbl = tk.Label(
            overlay,
            text="Starting Set Top Box",
            fg=C_WHITE,
            bg="black",
            font=("Arial", 34, "bold"),
        )
        title_lbl.place(relx=0.5, rely=0.42, anchor="center")

        self._startup_status = tk.Label(
            overlay,
            text="Loading channelsâ€¦",
            fg=C_DIM,
            bg="black",
            font=("Arial", 14),
        )
        self._startup_status.place(relx=0.5, rely=0.5, anchor="center")

        self._startup_overlay = overlay
        self._tick_startup_spinner()
        self._set_startup_progress(0)

    def _can_update_startup_status(self):
        if self._startup_finished:
            return False
        if not self._startup_overlay:
            return False
        if not self._startup_status:
            return False
        try:
            return bool(self._startup_status.winfo_exists())
        except tk.TclError:
            return False

    def _tick_startup_spinner(self):
        if not self._can_update_startup_status():
            return
        phases = ("", ".", "..", "...")
        phase = phases[self._startup_spinner_index % len(phases)]
        self._startup_spinner_index += 1
        if self._can_update_startup_status():
            self._startup_status.config(
                text=f"Loading channels {self._startup_done}/{self._startup_total}{phase}"
            )
        self._startup_spinner_job = self.root.after(350, self._tick_startup_spinner)

    def _set_startup_progress(self, count, label: str = ""):
        self._startup_done = min(count, self._startup_total)
        if self._can_update_startup_status():
            extra = f"  ({label})" if label else ""
            self._startup_status.config(
                text=f"Loading channels {self._startup_done}/{self._startup_total}{extra}"
            )

    # ------------------------------------------------------------------
    # Channel warmup  (runs entirely in a background daemon thread)
    # ------------------------------------------------------------------

    def _start_channel_warmup(self):
        threading.Thread(target=self._warmup_channels, daemon=True).start()

    def _warmup_channels(self):
        channels = list(self.channels.values())
        total = len(channels)
        self.root.after(0, lambda: self._set_startup_progress(0))
        if total == 0:
            self.root.after(0, self._finish_startup_loading)
            return

        for index, channel in enumerate(channels, start=1):
            name = channel.get("name", "")
            try:
                self._warmup_channel(channel)
            except Exception:
                pass
            self.root.after(
                0,
                lambda done=index, n=name: self._set_startup_progress(done, n),
            )

        # Warmup only parses local data; network loading happens on tune.
        self.root.after(0, self._finish_startup_loading)

    def _warmup_channel(self, channel):
        """Parse schedule and prime only local-folder startup streams."""
        self.scheduler.prepare_channel(channel)
        source = channel.get("source", "")

        # --- Local video folder ---
        if source and os.path.isdir(source):
            files = []
            for ext in ("*.mp4", "*.mkv", "*.avi", "*.mov", "*.wmv"):
                files.extend(glob.glob(os.path.join(source, ext)))
            if files:
                files.sort()
                channel["_startup_stream"] = (files[0], "", None, None)


    # ------------------------------------------------------------------
    # Startup finish
    # ------------------------------------------------------------------

    def _finish_startup_loading(self):
        if self._startup_finished:
            return
        self._startup_finished = True
        if self._startup_spinner_job:
            self.root.after_cancel(self._startup_spinner_job)
            self._startup_spinner_job = None
        if self._startup_overlay and self._startup_overlay.winfo_exists():
            self._startup_overlay.destroy()
        self._startup_overlay = None
        self._startup_status = None
        self.root.after(100, self._start_initial_channel)

    # ------------------------------------------------------------------
    # Media-end callback (thread-safe trampoline)
    # ------------------------------------------------------------------

    def _on_media_end(self, _event):
        try:
            self.root.after(0, self._handle_media_end)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Start initial channel after startup
    # ------------------------------------------------------------------

    def _start_initial_channel(self):
        if self.current_channel:
            return
        if not (self.root.winfo_ismapped() and self.video_panel.winfo_ismapped()):
            self.root.after(100, self._start_initial_channel)
            return
        start_number = "100" if "100" in self.channels else None
        if not start_number and self.channels:
            start_number = self._sorted_keys()[0]
        if not start_number:
            return
        channel = self.channels.get(start_number)
        if channel:
            self.switch_channel(channel)
            self.root.after(150, self._restore_startup_window_state)

    def _restore_startup_window_state(self):
        try:
            if self.root.state() == "iconic":
                self.root.deiconify()
                self.root.attributes("-fullscreen", True)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Image helpers
    # ------------------------------------------------------------------

    def _pick_image(self, width, height):
        if not self._img_pool:
            return None
        path = random.choice(self._img_pool)
        try:
            from PIL import Image, ImageTk

            img = Image.open(path)
            img_w, img_h = img.size
            scale = max(width / img_w, height / img_h)
            new_w = int(img_w * scale)
            new_h = int(img_h * scale)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            left = (new_w - width) // 2
            top = (new_h - height) // 2
            img = img.crop((left, top, left + width, top + height))
            return ImageTk.PhotoImage(img)
        except Exception:
            pass

        try:
            photo = tk.PhotoImage(file=path)
            sw = max(1, photo.width() // width)
            sh = max(1, photo.height() // height)
            scale = min(sw, sh)
            return photo.subsample(scale, scale) if scale > 1 else photo
        except Exception:
            return None

    def _sorted_keys(self):
        return sorted(self.channels.keys(), key=lambda value: int(value))

    def _is_youtube_channel(self, channel: dict | None) -> bool:
        if not channel:
            return False
        schedule_ref = channel.get("schedule")
        if isinstance(schedule_ref, str) and schedule_ref.strip():
            return True
        if isinstance(schedule_ref, dict):
            if schedule_ref.get("days") or schedule_ref.get("weekdays") or schedule_ref.get("weekends"):
                return True
        if isinstance(schedule_ref, list) and schedule_ref:
            return True
        source = channel.get("source", "")
        if not isinstance(source, str):
            return False
        src = source.strip().lower()
        return src.startswith("yt:") or "youtube.com" in src or "youtu.be" in src

    # ------------------------------------------------------------------
    # Keyboard handler
    # ------------------------------------------------------------------

    def on_keypress(self, event=None):
        key = event.keysym

        if key.isdigit():
            if len(self.channel_buffer) < 3:
                self.channel_buffer += key
            self.ui_mode = "CHANNEL_INPUT"
            self._show_ch_badge(self.channel_buffer)
            if self.buffer_job:
                self.root.after_cancel(self.buffer_job)
            self.buffer_job = self.root.after(3000, self._auto_confirm_channel)
            return

        if key == "Return":
            if self.channel_buffer:
                self._confirm_typed_channel()
            elif self.ui_mode == "BROWSE" and self._browse_num is not None:
                self._confirm_browse()
            elif self._epg_row_index > 0:
                self._epg_activate_row()
            elif self.current_channel:
                self.show_epg(user_initiated=True)
            return

        if key == "Left":

            if self.ui_mode == "CHANNEL_INPUT":
                return
        
            # Browse preview only
            self._browse_channel_delta(-1)
        
            return
        
        if key == "Right":

            if self.ui_mode == "CHANNEL_INPUT":
                return

            # Browse preview only
            self._browse_channel_delta(+1)

            return

        if key == "Down":
            self._epg_row_move(+1)
            return
        if key == "Up":
            self._epg_row_move(-1)
            return

        if key in ("i", "I"):
            if self.epg_window.winfo_viewable():
                self.ui_mode = "NORMAL"
                self.hide_epg()
            else:
                self.ui_mode = "EPG"
                self.show_epg(user_initiated=True)
            return

        if key in ("m", "M"):
            self._hide_mail() if self._mail_visible else self._show_mail()
            return

        if key in ("XF86AudioRaiseVolume", "plus", "equal"):
            self._change_volume(+5)
            return
        if key in ("XF86AudioLowerVolume", "minus"):
            self._change_volume(-5)
            return

        if key in ("l", "L"):
            self._show_language_picker()
            return

        if key == "Escape":
            if self.ui_mode == "CHANNEL_INPUT":
                self.channel_buffer = ""
                self.ui_mode = "NORMAL"
                if self.buffer_job:
                    self.root.after_cancel(self.buffer_job)
                    self.buffer_job = None
                self._badge_win.withdraw()
            elif self.ui_mode == "BROWSE" and self._browse_num is not None:
                self._cancel_browse()
                self.ui_mode = "NORMAL"
            elif self._epg_row_index != 0:
                self._epg_row_index = 0
                self._render_epg_rows()
            elif self.epg_window.winfo_viewable():
                self.ui_mode = "NORMAL"
                self.hide_epg()
            else:
                self.root.quit()

    # ------------------------------------------------------------------
    # Channel-number badge
    # ------------------------------------------------------------------

    def _prepare_ch_badge(self):
        self._badge_win = tk.Toplevel(self.root)
        self._badge_win.withdraw()
        self._badge_win.overrideredirect(True)
        self._badge_win.attributes("-topmost", True)
        self._badge_win.attributes("-alpha", 0.93)
        self._badge_win.configure(bg=C_BADGE_BG)

        border = tk.Frame(self._badge_win, bg=C_PROG_ACTIVE, bd=0)
        border.pack(padx=2, pady=2)

        inner = tk.Frame(border, bg=C_BADGE_BG)
        inner.pack(padx=6, pady=6)

        self._badge_ch_num = tk.Label(
            inner,
            text="",
            fg=C_WHITE,
            bg=C_BADGE_BG,
            font=("Arial", 52, "bold"),
            width=4,
            anchor="center",
        )
        self._badge_ch_num.pack()

        self._badge_ch_name = tk.Label(
            inner,
            text="",
            fg=C_DIM,
            bg=C_BADGE_BG,
            font=("Arial", 13),
        )
        self._badge_ch_name.pack()

        self._badge_hide_job = None

    def _show_ch_badge(self, number_str: str):
        ch = self.channels.get(number_str)
        name = ch.get("name", "") if ch else ""

        self._badge_ch_num.config(text=number_str)
        self._badge_ch_name.config(text=name)

        self.root.update_idletasks()
        rw = self.root.winfo_width()
        rh = self.root.winfo_height()
        rx = self.root.winfo_rootx()
        ry = self.root.winfo_rooty()
        self._badge_win.update_idletasks()
        bw = self._badge_win.winfo_reqwidth()
        bh = self._badge_win.winfo_reqheight()
        x = rx + rw - bw - 40
        y = ry + rh - bh - 60
        self._badge_win.geometry(f"+{x}+{y}")
        self._badge_win.deiconify()
        self._badge_win.lift()

        if self._badge_hide_job:
            self.root.after_cancel(self._badge_hide_job)
        self._badge_hide_job = self.root.after(3000, self._badge_win.withdraw)

    def _auto_confirm_channel(self):
        self.buffer_job = None
        if self.channel_buffer:
            self._confirm_typed_channel()

    def _confirm_typed_channel(self):
        number = self.channel_buffer
        self.channel_buffer = ""
        self.ui_mode = "NORMAL"
        if self._badge_hide_job:
            self.root.after_cancel(self._badge_hide_job)
        self._badge_win.withdraw()
        channel = self.channels.get(number)
        if channel:
            if self.current_channel and channel.get("number") == self.current_channel.get("number"):
                self.show_epg(user_initiated=True)
                return
            self.hide_epg()
            self.switch_channel(channel, user_initiated=True)

    # ------------------------------------------------------------------
    # Browse (left/right arrow channel switch)
    # ------------------------------------------------------------------

    def _browse_channel_delta(self, delta: int):

       keys = self._sorted_keys()
    
       if not keys:
           return
    
       self.ui_mode = "BROWSE"
    
       current_num = self._browse_num
    
       if not current_num:
           current_num = (
               self.current_channel.get("number")
               if self.current_channel
               else None
           )
    
       if current_num and current_num in keys:
           idx = keys.index(current_num)
       else:
           idx = 0
    
       new_idx = (idx + delta) % len(keys)
    
       self._browse_num = keys[new_idx]
    
       browse_channel = self.channels.get(self._browse_num)
    
       if browse_channel:
        
           # IMPORTANT:
           # Preview only.
           # DO NOT switch playback.
           self._update_epg(
               browse_channel,
               browsing=True,
           )
    
       self.show_epg(
           auto_hide=4000,
           user_initiated=True,
       )
    
       if self._browse_hide_job:
           self.root.after_cancel(self._browse_hide_job)
    
       self._browse_hide_job = self.root.after(
           3500,
           self._cancel_browse,
       )

    def _confirm_browse(self):

        number = self._browse_num
    
        self._browse_num = None
    
        self.ui_mode = "NORMAL"
    
        self.hide_epg()
    
        if not number:
            return
    
        channel = self.channels.get(number)
    
        if not channel:
            return
    
        current_number = (
            self.current_channel.get("number")
            if self.current_channel
            else None
        )
    
        # Same channel -> just reopen EPG
        if current_number == number:
            self.show_epg(user_initiated=True)
            return
    
        # ACTUAL channel switch happens only here
        self.switch_channel(
            channel,
            user_initiated=True,
            force_restart=True,
        )

    def _cancel_browse(self):
        self._browse_num = None
        self.ui_mode = "NORMAL"
        if self.current_channel:
            self._update_epg(self.current_channel)
            if self.hide_job:
                self.root.after_cancel(self.hide_job)
            self.hide_job = self.root.after(EPG_AUTO_HIDE_MS, self.hide_epg)

    # ------------------------------------------------------------------
    # EPG item building
    # ------------------------------------------------------------------

    def _build_epg_items(self, channel):
        items = []
        is_youtube = self._is_youtube_channel(channel)

        if is_youtube:
            program = self.scheduler.resolve_program(channel)
            current_block = program.get("block")
            current_title = channel.get("_current_title", "") or (
                current_block.title if current_block else "Scheduled Program"
            )
            current_time = (
                _fmt_time_range(current_block.start_dt, current_block.end_dt)
                if current_block
                else ""
            )
            items.append((current_title, current_time, None))

            upcoming = self.scheduler.get_upcoming_blocks(channel, count=6)
            for block in upcoming:
                if (
                    current_block
                    and block.start_dt == current_block.start_dt
                    and block.playlist == current_block.playlist
                ):
                    continue
                block_title = block.title or "Scheduled Block"
                block_time = _fmt_time_range(block.start_dt, block.end_dt)
                items.append((block_title, block_time, None))
                if len(items) >= 20:
                    break
        else:
            for schedule in channel.get("schedule", []):
                time_str = (
                    f"{schedule.get('start', '')} - {schedule.get('end', '')}"
                    if schedule.get("start")
                    else ""
                )
                items.append((schedule.get("title", ""), time_str, None))

        if not items:
            items = [("No programme info", "", None)]
        return items

    # ------------------------------------------------------------------
    # EPG row navigation
    # ------------------------------------------------------------------

    def _epg_row_move(self, delta: int):
        channel = self.current_channel
        if not channel:
            return
        self.ui_mode = "EPG"
        if not self._epg_items:
            self._epg_items = self._build_epg_items(channel)

        if not self.epg_window.winfo_viewable():
            self._epg_row_index = 0
            self._render_epg_rows()
            self.show_epg(auto_hide=EPG_AUTO_HIDE_MS, user_initiated=True)
            return

        new_index = self._epg_row_index + delta
        new_index = max(0, min(new_index, len(self._epg_items) - 1))
        self._epg_row_index = new_index
        self._render_epg_rows()
        if self.hide_job:
            self.root.after_cancel(self.hide_job)
        self.hide_job = self.root.after(EPG_AUTO_HIDE_MS, self.hide_epg)

    def _epg_activate_row(self):
        if not self._epg_items or self._epg_row_index == 0:
            return
        title, _, url = self._epg_items[self._epg_row_index]
        self._epg_row_index = 0
        channel = self.current_channel
        request_id = self.channel_request_id

        if url == "__preload__" and self._preload_result:
            stream_url, title, headers, origin_url = self._preload_result
            self._preload_result = None
            self._preload_channel = None
            channel["_current_title"] = title
            self._play_media_source(
                channel,
                stream_url,
                request_id=request_id,
                title=title,
                headers=headers,
                origin_url=origin_url,
                show_overlay=True,
            )

        elif url and url.startswith("http"):
            channel["_current_title"] = title
            self.channel_request_id += 1
            request_id = self.channel_request_id
            threading.Thread(
                target=self._resolve_and_play,
                args=(channel, url, request_id, True),
                daemon=True,
            ).start()

    def _render_epg_rows(self):
        items = self._epg_items
        index = self._epg_row_index

        selected = items[index] if index < len(items) else ("", "", None)
        selected_next = items[index + 1] if index + 1 < len(items) else ("", "", None)

        self.epg_active_title.config(text=selected[0])
        self.epg_active_time.config(text=selected[1])
        self.epg_next_title.config(text=selected_next[0])
        self.epg_next_time.config(text=selected_next[1])


# ------------------------------------------------------------------
# Utility
# ------------------------------------------------------------------

def _fmt_time_range(start_dt, end_dt) -> str:
    start = start_dt.strftime("%I:%M %p").lstrip("0")
    end = end_dt.strftime("%I:%M %p").lstrip("0")
    return f"{start} - {end}"
