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
    STARTUP_LOADING_MS,
)


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

        try:
            # Intel HD 530 + D3D11VA is unstable on some Windows setups.
            # Use software decode for predictable startup behavior.
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
        self.root.after(STARTUP_LOADING_MS, self._finish_startup_loading)

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

    def _show_startup_loading(self):
        overlay = tk.Frame(self.root, bg="black")
        overlay.place(relx=0, rely=0, relwidth=1, relheight=1)

        title = tk.Label(
            overlay,
            text="Starting Set Top Box",
            fg=C_WHITE,
            bg="black",
            font=("Arial", 34, "bold"),
        )
        title.place(relx=0.5, rely=0.42, anchor="center")

        self._startup_status = tk.Label(
            overlay,
            text="Loading channels 0/0",
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

    def _set_startup_progress(self, count):
        self._startup_done = min(count, self._startup_total)
        if self._can_update_startup_status():
            self._startup_status.config(
                text=f"Loading channels {self._startup_done}/{self._startup_total}"
            )

    def _start_channel_warmup(self):
        threading.Thread(target=self._warmup_channels, daemon=True).start()

    def _warmup_channels(self):
        channels = list(self.channels.values())
        total = len(channels)
        self.root.after(0, lambda: self._set_startup_progress(0))
        if total == 0:
            self.root.after(0, lambda: self._set_startup_progress(0))
            return
        for index, channel in enumerate(channels, start=1):
            try:
                self._warmup_channel(channel)
            except Exception:
                pass
            self.root.after(0, lambda finished=index: self._set_startup_progress(finished))

    def _warmup_channel(self, channel):
        source = channel.get("source", "")
        if isinstance(source, str) and (source.startswith("yt:") or "youtube.com" in source):
            if "_yt_list" not in channel or not channel.get("_yt_list"):
                yt_list, title_map = self.fetch_youtube_videos(source)
                channel["_yt_list"] = yt_list or []
                channel["_yt_entry_titles"] = title_map or {}
            failed_urls = channel.setdefault("_yt_failed_urls", set())
            yt_list = channel.get("_yt_list", [])
            candidates = [url for url in yt_list if url not in failed_urls and url.startswith("http")][:1]
            if not candidates and yt_list:
                candidates = [url for url in yt_list if url.startswith("http")][:1]
            for url in candidates:
                stream_url, title, headers = self.resolve_youtube_stream(url)
                if stream_url:
                    channel["_startup_stream"] = (stream_url, title, headers, url)
                    if title:
                        channel.setdefault("_yt_titles", {})[url] = title
                else:
                    failed_urls.add(url)
            return

        if source and os.path.isdir(source):
            files = []
            for ext in ("*.mp4", "*.mkv", "*.avi", "*.mov", "*.wmv"):
                files.extend(glob.glob(os.path.join(source, ext)))
            if files:
                files.sort()
                channel["_startup_stream"] = (files[0], "", None, None)

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

    def _on_media_end(self, _event):
        try:
            self.root.after(0, self._handle_media_end)
        except Exception:
            pass

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

        self._digit_canvases = []
        slot_frame = tk.Frame(inner, bg=C_BADGE_BG)
        slot_frame.pack()
        slot_w, slot_h = 22, 30
        for _ in range(3):
            canvas = tk.Canvas(
                slot_frame,
                width=slot_w,
                height=slot_h,
                bg=C_BADGE_BG,
                highlightthickness=0,
                bd=0,
            )
            canvas.pack(side=tk.LEFT, padx=2)
            canvas.create_line(0, slot_h - 3, slot_w, slot_h - 3, fill=C_WHITE, width=2)
            text_id = canvas.create_text(
                slot_w // 2,
                slot_h // 2 - 2,
                text="",
                fill=C_WHITE,
                font=("Arial", 16, "bold"),
                anchor="center",
            )
            self._digit_canvases.append((canvas, text_id))

        self._badge_name = tk.Label(
            inner,
            text="",
            fg=C_DIM,
            bg=C_BADGE_BG,
            font=("Arial", 9),
        )
        self._badge_name.pack(pady=(3, 0))

    def _show_ch_badge(self, num_str, name=""):
        for index, (canvas, text_id) in enumerate(self._digit_canvases):
            channel_digit = num_str[index] if index < len(num_str) else ""
            canvas.itemconfig(text_id, text=channel_digit)
        self._badge_name.config(text=name)

        self._badge_win.update_idletasks()
        self.root.update_idletasks()
        root_w = self.root.winfo_width()
        root_h = self.root.winfo_height()
        root_x = self.root.winfo_rootx()
        root_y = self.root.winfo_rooty()
        badge_w = self._badge_win.winfo_reqwidth()
        x = root_x + root_w - badge_w - int(root_w * 0.05)
        y = root_y + int(root_h * 0.12)
        self._badge_win.geometry(f"+{x}+{y}")
        self._badge_win.deiconify()
        self._badge_win.lift()

    def _hide_ch_badge(self):
        self._badge_win.withdraw()

    def _auto_confirm_channel(self):
        if self.channel_buffer:
            self._confirm_typed_channel()

    def _confirm_typed_channel(self):
        number = self.channel_buffer
        self.channel_buffer = ""
        self._hide_ch_badge()
        channel = self.channels.get(number)
        if channel:
            self.switch_channel(channel)
        else:
            messagebox.showinfo("Channel", f"Channel {number} not found")

    def _browse_channel_delta(self, delta: int):
        keys = self._sorted_keys()
        if not keys:
            return

        reference = self._browse_num or self.current_channel.get("number") or keys[0]
        try:
            index = keys.index(reference)
        except ValueError:
            index = 0
        index = (index + delta) % len(keys)
        self._browse_num = keys[index]

        target_channel = self.channels[self._browse_num]
        browse_items = self._build_browse_items(target_channel)

        number = target_channel.get("number", "")
        name = target_channel.get("name", "")
        self.epg_ch_num.config(text=f" {number} ")
        self.epg_ch_name.config(text=name)

        row_a = browse_items[0] if browse_items else ("", "", None)
        row_b = browse_items[1] if len(browse_items) > 1 else ("", "", None)
        self.epg_active_title.config(text=row_a[0])
        self.epg_active_time.config(text=row_a[1])
        self.epg_next_title.config(text=row_b[0])
        self.epg_next_time.config(text=row_b[1])

        import datetime

        self.date_label.config(text=datetime.datetime.now().strftime("%a %d %b  %I:%M %p"))
        photo = self._pick_image(190, 190)
        if photo:
            self._current_img = photo
            self._epg_img_canvas.itemconfig(self._epg_img_id, image=photo)

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
        if self._browse_hide_job:
            self.root.after_cancel(self._browse_hide_job)
        self._browse_hide_job = self.root.after(EPG_AUTO_HIDE_MS, self._cancel_browse)

    def _build_browse_items(self, channel):
        source = channel.get("source", "")
        is_youtube = isinstance(source, str) and (
            source.startswith("yt:") or "youtube.com" in source
        )
        if is_youtube:
            if not channel.get("_yt_list"):
                self._ensure_youtube_list_async(channel)
            items = []
            current_title = channel.get("_current_title", "")
            if current_title and current_title != "Loading...":
                items.append((current_title, "Now Playing", None))

            yt_titles = channel.get("_yt_titles", {})
            entry_titles = channel.get("_yt_entry_titles", {})
            for url in channel.get("_yt_list", [])[:20]:
                if url == channel.get("_current_yt_url"):
                    continue
                title = yt_titles.get(url) or entry_titles.get(url)
                if not title:
                    continue
                items.append((title, "", url))
                if len(items) >= 2:
                    break

            if items:
                if len(items) == 1:
                    items.append(("", "", None))
                return items

        browse_items = []
        for schedule in channel.get("schedule", []):
            time_str = (
                f"{schedule.get('start', '')} - {schedule.get('end', '')}"
                if schedule.get("start")
                else ""
            )
            browse_items.append((schedule.get("title", ""), time_str, None))
        if not browse_items:
            name = channel.get("name", "")
            browse_items = [(name, "", None), ("", "", None)]
        return browse_items

    def _ensure_youtube_list_async(self, channel):
        if channel.get("_yt_fetching"):
            return
        source = channel.get("source", "")
        is_youtube = isinstance(source, str) and (
            source.startswith("yt:") or "youtube.com" in source
        )
        if not is_youtube:
            return
        channel["_yt_fetching"] = True

        def _worker():
            try:
                yt_list, title_map = self.fetch_youtube_videos(source)
                if yt_list:
                    channel["_yt_list"] = yt_list
                if title_map:
                    existing = channel.setdefault("_yt_entry_titles", {})
                    existing.update(title_map)
            finally:
                channel["_yt_fetching"] = False

        threading.Thread(target=_worker, daemon=True).start()

    def _confirm_browse(self):
        if self._browse_hide_job:
            self.root.after_cancel(self._browse_hide_job)
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

    def _build_epg_items(self, channel):
        items = []
        source = channel.get("source", "")
        is_youtube = isinstance(source, str) and (
            source.startswith("yt:") or "youtube.com" in source
        )

        if is_youtube:
            current_title = channel.get("_current_title", "Now Playing")
            items.append((current_title, "Now Playing", None))

            if self._preload_channel is channel and self._preload_result:
                preload_title = self._preload_result[1] or "Next Video"
                items.append((preload_title, "Up Next", "__preload__"))

            yt_titles = channel.get("_yt_titles", {})
            for url in channel.get("_yt_list", [])[:20]:
                if url == channel.get("_current_yt_url"):
                    continue
                title = yt_titles.get(url, "")
                if not title:
                    entry_title = channel.get("_yt_entry_titles", {}).get(url, "")
                    if entry_title:
                        title = entry_title
                    elif "v=" in url:
                        title = url.split("v=")[-1][:11]
                    else:
                        title = "Video"
                items.append((title, "", url))
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
