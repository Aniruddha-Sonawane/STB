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
    IMAGES_DIR,
    IMG_EXTS,
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

        try:
            self.instance = vlc.Instance()
            self.player = self.instance.media_player_new()
            self.player.audio_set_volume(self._volume)
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

        browse_items = []
        for schedule in target_channel.get("schedule", []):
            time_str = (
                f"{schedule.get('start', '')} - {schedule.get('end', '')}"
                if schedule.get("start")
                else ""
            )
            browse_items.append((schedule.get("title", ""), time_str, None))
        if not browse_items:
            name = target_channel.get("name", "")
            browse_items = [(name, "", None), ("", "", None)]

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
        self._browse_hide_job = self.root.after(5000, self._cancel_browse)

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
                if url == channel.get("_resolved_src"):
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
            self.show_epg(auto_hide=0)
            return

        new_index = self._epg_row_index + delta
        new_index = max(0, min(new_index, len(self._epg_items) - 1))
        self._epg_row_index = new_index
        self._render_epg_rows()
        if self.hide_job:
            self.root.after_cancel(self.hide_job)
        self.hide_job = self.root.after(8000, self.hide_epg)

    def _epg_activate_row(self):
        if not self._epg_items or self._epg_row_index == 0:
            return
        title, _, url = self._epg_items[self._epg_row_index]
        self._epg_row_index = 0
        channel = self.current_channel
        request_id = self.channel_request_id

        if url == "__preload__" and self._preload_result:
            stream_url, title, headers = self._preload_result
            self._preload_result = None
            self._preload_channel = None
            channel["_current_title"] = title
            self._play_media_source(
                channel,
                stream_url,
                request_id=request_id,
                title=title,
                headers=headers,
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
