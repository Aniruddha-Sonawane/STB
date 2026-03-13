import glob
import os
import random
import sys
import threading
import time as _t
from tkinter import filedialog, messagebox

import vlc


class PlaybackMixin:
    def _snapshot(self):
        previous = self.current_channel
        if not previous:
            return
        number = previous.get("number")
        if not number:
            return
        position = self.player.get_time()
        if position < 0:
            position = 0
        state = self.channel_state.setdefault(number, {})
        state["position_ms"] = position
        state["left_at"] = _t.time()
        if "_resolved_src" in previous:
            state["source"] = previous["_resolved_src"]

    def _resume_ms(self, channel) -> int:
        state = self.channel_state.get(channel.get("number"))
        if not state:
            return 0
        return state.get("position_ms", 0) + int((_t.time() - state.get("left_at", 0)) * 1000)

    def switch_channel(self, channel: dict):
        self._snapshot()
        self.current_channel = channel
        self.channel_request_id += 1
        self._epg_row_index = 0
        self._epg_items = []
        request_id = self.channel_request_id
        source = channel.get("source", "")

        if isinstance(source, str) and (source.startswith("yt:") or "youtube.com" in source):
            saved = self.channel_state.get(channel.get("number", ""), {})
            saved_source = saved.get("source")

            if self._preload_channel is channel and self._preload_result:
                stream_url, title, headers = self._preload_result
                self._preload_result = None
                self._preload_channel = None
                channel["_current_title"] = title or ""
                self._play_media_source(
                    channel,
                    stream_url,
                    request_id=request_id,
                    title=title,
                    headers=headers,
                )
            elif saved_source:
                self._play_media_source(
                    channel,
                    saved_source,
                    request_id=request_id,
                    title=channel.get("_current_title", ""),
                )
            else:
                channel["_current_title"] = "Loading..."
                self.show_epg()
                threading.Thread(
                    target=self._load_yt_channel,
                    args=(channel, source, request_id),
                    daemon=True,
                ).start()
            return

        if source and os.path.isdir(source):
            files = []
            for ext in ("*.mp4", "*.mkv", "*.avi", "*.mov", "*.wmv"):
                files.extend(glob.glob(os.path.join(source, ext)))
            if files:
                source = files[0]

        self._play_media_source(channel, source, request_id=request_id)

    def _load_yt_channel(self, channel, source, request_id):
        try:
            if "_yt_list" not in channel or not channel["_yt_list"]:
                yt_list, title_map = self.fetch_youtube_videos(source)
                channel["_yt_list"] = yt_list
                channel["_yt_entry_titles"] = title_map
            yt_list = channel.get("_yt_list", [])
            if not yt_list:
                self.root.after(
                    0,
                    lambda: self._show_channel_error(
                        request_id,
                        f"No videos found for channel {channel.get('number', '')}",
                    ),
                )
                return
            random.shuffle(yt_list)
            stream_url = title = headers = None
            for url in yt_list[:15]:
                stream_url, title, headers = self.resolve_youtube_stream(url)
                if stream_url:
                    break
            if not stream_url:
                self.root.after(
                    0,
                    lambda: self._show_channel_error(
                        request_id,
                        f"Cannot resolve stream for {channel.get('name', '')}",
                    ),
                )
                return
            self.root.after(
                0,
                lambda: self._play_media_source(
                    channel,
                    stream_url,
                    request_id=request_id,
                    title=title,
                    headers=headers,
                ),
            )
        except Exception as exc:
            message = str(exc)
            self.root.after(0, lambda: self._show_channel_error(request_id, message))

    def _preload_next(self, channel):
        source = channel.get("source", "")
        if not (isinstance(source, str) and (source.startswith("yt:") or "youtube.com" in source)):
            return

        def _work():
            yt_list = channel.get("_yt_list", [])
            if not yt_list:
                videos, title_map = self.fetch_youtube_videos(source)
                channel["_yt_list"] = videos
                channel["_yt_entry_titles"] = title_map
                yt_list = videos
            if not yt_list:
                return
            current = channel.get("_resolved_src", "")
            pool = [url for url in yt_list if url != current] or yt_list[:]
            random.shuffle(pool)
            for url in pool[:15]:
                stream_url, title, headers = self.resolve_youtube_stream(url)
                if stream_url:
                    self._preload_result = (stream_url, title, headers)
                    self._preload_channel = channel
                    self.root.after(0, self._on_preload_ready)
                    return

        threading.Thread(target=_work, daemon=True).start()

    def _on_preload_ready(self):
        if self._preload_result and self._preload_channel is self.current_channel:
            title = self._preload_result[1] or ""
            if self._epg_items and len(self._epg_items) > 1:
                if self._epg_items[1][2] != "__preload__":
                    self._epg_items.insert(1, (title, "", "__preload__"))
                else:
                    self._epg_items[1] = (title, "", "__preload__")
            self._render_epg_rows()

    def _apply_headers(self, media, headers):
        if not headers:
            return
        user_agent = headers.get("User-Agent") or headers.get("user-agent")
        referer = headers.get("Referer") or headers.get("referer")
        if user_agent:
            media.add_option(f":http-user-agent={user_agent}")
        if referer:
            media.add_option(f":http-referrer={referer}")
        media.add_option(":network-caching=1500")

    def _play_media_source(self, channel, source, request_id=None, title=None, headers=None):
        if request_id is not None and request_id != self.channel_request_id:
            return
        if title:
            channel["_current_title"] = title
        channel["_resolved_src"] = source

        self._epg_items = self._build_epg_items(channel)
        self._epg_row_index = 0

        self.show_epg()
        self.stop()

        if source:
            media = self.instance.media_new(source)
            self._apply_headers(media, headers)
            self.player.set_media(media)
            self._set_video_window()
            self.player.audio_set_volume(self._volume)
            self.player.play()

            seek = self._resume_ms(channel)
            if seek > 0:
                self._seek_when_ready(seek, request_id)

            self._preload_next(channel)
            self._start_tick(request_id)
        else:
            self._show_channel_error(
                request_id,
                f"Channel {channel.get('number', '')} has no playable source",
            )

    def _resolve_and_play(self, channel, url, request_id):
        stream_url, title, headers = self.resolve_youtube_stream(url)
        if stream_url:
            self.root.after(
                0,
                lambda: self._play_media_source(
                    channel,
                    stream_url,
                    request_id=request_id,
                    title=title,
                    headers=headers,
                ),
            )
        else:
            self.root.after(
                0,
                lambda: self._show_channel_error(
                    request_id,
                    "Could not resolve stream for selected video",
                ),
            )

    def _seek_when_ready(self, milliseconds, request_id, attempt=0):
        if request_id != self.channel_request_id or attempt > 30:
            return
        if self.player.get_state() == vlc.State.Playing:
            duration = self.player.get_length()
            if duration > 0:
                milliseconds = min(milliseconds, duration - 2000)
            if milliseconds > 0:
                self.player.set_time(milliseconds)
        else:
            self.root.after(
                100,
                lambda: self._seek_when_ready(milliseconds, request_id, attempt + 1),
            )

    def _show_channel_error(self, request_id, message):
        if request_id is not None and request_id != self.channel_request_id:
            return
        messagebox.showerror("Channel", message)

    def _start_tick(self, request_id):
        if self._epg_tick:
            self.root.after_cancel(self._epg_tick)
        self._do_tick(request_id)

    def _do_tick(self, request_id):
        if request_id != self.channel_request_id:
            return
        try:
            duration = self.player.get_length()
            position = self.player.get_time()
            if duration > 0 and position >= 0:
                fill_w = max(2, min(219, int(220 * position / duration)))
                self.epg_progress.coords(self.progress_fill, 0, 2, fill_w, 8)
        except Exception:
            pass
        self._epg_tick = self.root.after(500, lambda: self._do_tick(request_id))

    def _set_video_window(self):
        self.root.update_idletasks()
        handle = self.video_panel.winfo_id()
        if sys.platform.startswith("win"):
            self.player.set_hwnd(handle)
        elif sys.platform.startswith("linux"):
            self.player.set_xwindow(handle)
        elif sys.platform == "darwin":
            self.player.set_nsobject(handle)

    def open_file(self):
        file_path = filedialog.askopenfilename(
            filetypes=[("Video Files", "*.mp4 *.mkv *.avi *.mov *.wmv")]
        )
        if not file_path:
            return
        media = self.instance.media_new(file_path)
        self.player.set_media(media)
        self._set_video_window()
        self.player.play()

    def play(self):
        self.player.play()

    def pause(self):
        self.player.pause()

    def stop(self):
        self.player.stop()
