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
* The startup overlay lifts as soon as warmup finishes – no fixed timeout.
* Channel 100 (or the first channel) is pre-resolved to the
  *clock-scheduled* video so playback starts instantly.
"""

import glob
import os
import random
import sys
import threading
import tkinter as tk
from tkinter import messagebox

import vlc

from stb_player.constants import (
    CHANNELS_FILE,
    C_BADGE_BG,
    C_DIM,
    C_PROG_ACTIVE,
    C_WHITE,
    EPG_AUTO_HIDE_MS,
    IMAGES_DIR,
    IMG_EXTS,
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

        # Persistent scheduler – video cache + clock-based schedule.
        self.scheduler = ChannelScheduler(VIDEO_CACHE_FILE)

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
        for ext in IMG_EXTS:
            files.extend(glob.glob(os.path.join(IMAGES_DIR, ext)))
        return files

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
            text="Loading channels…",
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

        # All channels are ready – dismiss the overlay.
        self.root.after(0, self._finish_startup_loading)

    def _warmup_channel(self, channel):
        """
        Load video metadata for one channel and pre-resolve the
        clock-scheduled stream so it plays instantly when switched to.

        Flow
        ----
        1. If the scheduler cache is valid:  load metadata from cache.
        2. If stale or empty:               fetch from YouTube, save to cache.
        3. Populate channel["_yt_list"] and channel["_yt_entry_titles"].
        4. Ask the scheduler which video should be on air right now
           and pre-resolve its stream URL into channel["_startup_stream"].
        """
        source = channel.get("source", "")
        num = channel.get("number", "")

        # --- YouTube channels ---
        if isinstance(source, str) and (
            source.startswith("yt:") or "youtube.com" in source
        ):
            # 1 / 2 – populate video list
            if self.scheduler.is_stale(num):
                yt_videos, title_map = self.fetch_youtube_videos(source)
                if yt_videos:
                    self.scheduler.update(num, channel.get("name", ""), yt_videos)
                else:
                    # Nothing fetched this time; fall back to whatever is cached
                    title_map = {}
            else:
                # Build title_map from cached data
                title_map = {
                    v["url"]: v["title"]
                    for v in self.scheduler.get_videos(num)
                    if v.get("title")
                }

            videos = self.scheduler.get_videos(num)
            # Keep _yt_list as a list of URL strings for existing code paths
            channel["_yt_list"] = [v["url"] for v in videos]
            channel["_yt_entry_titles"] = title_map

            # 4 – find the clock-scheduled video and pre-resolve its stream
            scheduled_video, seek_ms = self.scheduler.get_now_playing(num)
            if scheduled_video:
                url = scheduled_video["url"]
                stream_url, title, headers = self.resolve_youtube_stream(url)
                if stream_url:
                    channel["_startup_stream"] = (stream_url, title, headers, url)
                    channel["_startup_seek_ms"] = seek_ms
                    if title:
                        channel.setdefault("_yt_titles", {})[url] = title
                else:
                    channel.setdefault("_yt_failed_urls", set()).add(url)
            return

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

    # ------------------------------------------------------------------
    # Keyboard handler
    # ------------------------------------------------------------------

    def on_keypress(self, event=None):
        key = event.keysym

        if key.isdigit():
            if len(self.channel_buffer) < 3:
                self.channel_buffer += key
            self._show_ch_badge(self.channel_buffer)
            if self.buffer_job:
                self.root.after_cancel(self.buffer_job)
            self.buffer_job = self.root.after(3000, self._auto_confirm_channel)
            return

        if key == "Return":
            if self._browse_num is not None:
                self._confirm_browse()
            elif self.channel_buffer:
                self._confirm_typed_channel()
            elif self._epg_row_index > 0:
                self._epg_activate_row()
            return

        if key == "Left":
            self._browse_channel_delta(-1)
            return
        if key == "Right":
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
                self.hide_epg()
            else:
                self.show_epg()
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
            if self._epg_row_index != 0:
                self._epg_row_index = 0
                self._render_epg_rows()
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
        if self._badge_hide_job:
            self.root.after_cancel(self._badge_hide_job)
        self._badge_win.withdraw()
        self.hide_epg()
        channel = self.channels.get(number)
        if channel:
            self.switch_channel(channel)

    # ------------------------------------------------------------------
    # Browse (left/right arrow channel switch)
    # ------------------------------------------------------------------

    def _browse_channel_delta(self, delta: int):
        keys = self._sorted_keys()
        if not keys:
            return
        current_num = (
            self.current_channel.get("number") if self.current_channel else None
        )
        if current_num and current_num in keys:
            idx = keys.index(current_num)
        else:
            idx = 0
        new_idx = (idx + delta) % len(keys)
        self._browse_num = keys[new_idx]
        channel = self.channels.get(self._browse_num)
        if channel:
            self._update_epg(channel)
        self.show_epg(auto_hide=4000)
        if self._browse_hide_job:
            self.root.after_cancel(self._browse_hide_job)
        self._browse_hide_job = self.root.after(3500, self._cancel_browse)

    def _confirm_browse(self):
        number = self._browse_num
        self._browse_num = None
        self.hide_epg()
        channel = self.channels.get(number)
        if channel:
            self.switch_channel(channel)

    def _cancel_browse(self):
        self._browse_num = None
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
        source = channel.get("source", "")
        is_youtube = isinstance(source, str) and (
            source.startswith("yt:") or "youtube.com" in source
        )
        num = channel.get("number", "")

        if is_youtube:
            current_title = channel.get("_current_title", "Now Playing")
            items.append((current_title, "Now Playing", None))

            if self._preload_channel is channel and self._preload_result:
                preload_title = self._preload_result[1] or "Next Video"
                items.append((preload_title, "Up Next", "__preload__"))

            # Show scheduled programme list from cache
            videos = self.scheduler.get_videos(num)
            current_url = channel.get("_current_yt_url", "")
            shown = 0
            for vid in videos:
                url = vid.get("url", "")
                if url == current_url:
                    continue
                title = vid.get("title") or channel.get("_yt_entry_titles", {}).get(url, "")
                if not title:
                    title = url.split("v=")[-1][:11] if "v=" in url else "Video"
                dur = vid.get("duration", 0)
                time_str = _fmt_duration(dur) if dur else ""
                items.append((title, time_str, url))
                shown += 1
                if shown >= 20:
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
        if not self._epg_items:
            self._epg_items = self._build_epg_items(channel)

        if not self.epg_window.winfo_viewable():
            self._epg_row_index = 0
            self._render_epg_rows()
            self.show_epg(auto_hide=EPG_AUTO_HIDE_MS)
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
            )

        elif url and url.startswith("http"):
            channel["_current_title"] = title
            self.channel_request_id += 1
            request_id = self.channel_request_id
            threading.Thread(
                target=self._resolve_and_play,
                args=(channel, url, request_id),
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

def _fmt_duration(seconds: int) -> str:
    """Format a duration in seconds as H:MM or MM:SS."""
    if seconds <= 0:
        return ""
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"
