"""
Playback logic for scheduled and local channels.
"""

import glob
import os
import sys
import threading
import time as _t
from tkinter import filedialog

import vlc


class PlaybackMixin:
    # ------------------------------------------------------------------
    # Candidate helpers
    # ------------------------------------------------------------------

    def _youtube_candidates(self, channel, include_current=False, limit=25):
        yt_list = channel.get("_yt_list", [])
        if not yt_list:
            return []
        current_url = channel.get("_current_yt_url")
        failed = channel.setdefault("_yt_failed_urls", set())
        candidates = [
            url
            for url in yt_list
            if url.startswith("http")
            and (include_current or url != current_url)
            and url not in failed
        ]
        if not candidates:
            failed.clear()
            candidates = [
                url
                for url in yt_list
                if url.startswith("http") and (include_current or url != current_url)
            ]
        return candidates[:limit]

    # ------------------------------------------------------------------
    # Recovery helpers
    # ------------------------------------------------------------------

    def _recover_youtube_channel(self, channel):
        if channel.get("_recover_inflight"):
            return

        tries = int(channel.get("_recover_tries", 0))
        if tries >= 2:
            channel["_current_title"] = "Channel unavailable"
            channel["_recover_inflight"] = False
            self.show_epg(user_initiated=False)
            return

        channel["_recover_tries"] = tries + 1
        channel["_recover_inflight"] = True
        self.channel_request_id += 1
        request_id = self.channel_request_id
        channel["_current_title"] = "Recovering stream..."
        self.root.after(
            8000,
            lambda ch=channel, rid=request_id: self._recover_timeout(ch, rid),
        )
        threading.Thread(
            target=self._resolve_channel_program,
            args=(channel, request_id, False),
            daemon=True,
        ).start()

    def _recover_timeout(self, channel, request_id):
        if channel is not self.current_channel:
            return
        if request_id != self.channel_request_id:
            return
        if not channel.get("_recover_inflight"):
            return
        channel["_recover_inflight"] = False
        self._show_channel_error(request_id, "Stream request timed out")

    # ------------------------------------------------------------------
    # Position snapshot (for local/file channels)
    # ------------------------------------------------------------------

    def _snapshot(self):
        previous = self.current_channel
        if not previous:
            return
        number = previous.get("number")
        if not number:
            return

        if self._is_youtube_channel(previous):
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
        return state.get("position_ms", 0) + int(
            (_t.time() - state.get("left_at", 0)) * 1000
        )

    # ------------------------------------------------------------------
    # Channel switch
    # ------------------------------------------------------------------

    def switch_channel(
        self,
        channel: dict,
        user_initiated: bool = False,
        force_restart: bool = False,
    ):
        if (
            not force_restart
            and self.current_channel
            and channel.get("number") == self.current_channel.get("number")
        ):

            return

        previous_channel = self.current_channel
        self._snapshot()
        self.current_channel = channel
        self.channel_request_id += 1
        self._epg_row_index = 0
        self._epg_items = []
        request_id = self.channel_request_id
        source = channel.get("source", "")
        is_first_channel = not previous_channel

        if self._is_youtube_channel(channel):
            channel["_recover_inflight"] = False
            channel["_current_title"] = "Loading..."
            if user_initiated:
                self.show_epg(user_initiated=True)
            threading.Thread(
                target=self._resolve_channel_program,
                args=(channel, request_id, user_initiated),
                daemon=True,
            ).start()
            return

        # Local file / folder
        if source and os.path.isdir(source):
            files = []
            for ext in ("*.mp4", "*.mkv", "*.avi", "*.mov", "*.wmv"):
                files.extend(glob.glob(os.path.join(source, ext)))
            if files:
                files.sort()
                source = files[0]

        if is_first_channel:
            prepared = channel.pop("_startup_stream", None)
            if isinstance(prepared, tuple) and prepared[0]:
                source = prepared[0]

        self._play_media_source(
            channel,
            source,
            request_id=request_id,
            show_overlay=user_initiated,
        )

    # ------------------------------------------------------------------
    # Scheduled program resolution
    # ------------------------------------------------------------------

    def _refresh_source_metadata(self, channel, source_key: str) -> bool:
        fetch_source = source_key
        if source_key.startswith("legacy:"):
            fetch_source = str(channel.get("source", "") or "").strip()
        if not fetch_source:
            return False

        yt_videos, title_map = self.fetch_youtube_videos(fetch_source)
        if not yt_videos:
            return False

        self.scheduler.update_metadata(source_key, channel.get("name", ""), yt_videos)
        if title_map:
            channel.setdefault("_yt_entry_titles", {}).update(title_map)
        return True

    def _resolve_channel_program(self, channel, request_id, show_overlay: bool):
        try:
            if request_id != self.channel_request_id or channel is not self.current_channel:
                return
            program = self.scheduler.resolve_program(channel)
            source_key = str(program.get("source_key") or "")
            videos = list(program.get("videos") or [])

            # Load missing metadata lazily for this tuned source only.
            if source_key and not videos:
                self._refresh_source_metadata(channel, source_key)
                program = self.scheduler.resolve_program(channel)
                source_key = str(program.get("source_key") or "")
                videos = list(program.get("videos") or [])
            elif source_key and self.scheduler.metadata_is_stale(source_key):
                # Background refresh - playback still continues with cached data.
                threading.Thread(
                    target=self._refresh_source_metadata,
                    args=(channel, source_key),
                    daemon=True,
                ).start()

            video = program.get("video")
            seek_ms = int(program.get("seek_ms") or 0)
            if video is None and videos:
                video = videos[0]
                seek_ms = 0

            if source_key:
                channel["_current_source_key"] = source_key
            if videos:

                channel["_yt_list"] = [
                    item.get("url", "")
                    for item in videos
                    if item.get("url")
                ]

                title_map = {
                    item.get("url", ""): item.get("title", "")
                    for item in videos
                    if item.get("url")
                }

                channel.setdefault("_yt_entry_titles", {}).update(
                    {
                        url: title
                        for url, title in title_map.items()
                        if title
                    }
                )

                # IMPORTANT:
                # Store upcoming videos for dynamic EPG
                if len(videos) > 1:
                    channel["_upcoming_videos"] = videos[1:]
                else:
                    channel["_upcoming_videos"] = []

            if not video:
                self.root.after(
                    0,
                    lambda: self._show_channel_error(
                        request_id,
                        f"No playable scheduled video for channel {channel.get('number', '')}",
                    ),
                )
                return

            url = video.get("url", "")
            if not url:
                self.root.after(
                    0,
                    lambda: self._show_channel_error(
                        request_id, "Scheduled entry has no video URL"
                    ),
                )
                return

            title = (
                channel.get("_yt_titles", {}).get(url)
                or channel.get("_yt_entry_titles", {}).get(url)
                or video.get("title", "")
                or "Loading..."
            )

            # Current playing title for EPG
            channel["_current_video_title"] = title

            channel["_current_title"] = title

            if request_id != self.channel_request_id or channel is not self.current_channel:
                return
            self._resolve_and_play_scheduled(
                channel,
                url,
                request_id=request_id,
                seek_ms=seek_ms,
                show_overlay=show_overlay,
            )
        except Exception as exc:
            message = str(exc)
            self.root.after(0, lambda: self._show_channel_error(request_id, message))

    # ------------------------------------------------------------------
    # Resolve & play helpers
    # ------------------------------------------------------------------

    def _resolve_and_play_scheduled(
        self,
        channel,
        url,
        request_id,
        seek_ms: int = 0,
        show_overlay: bool = False,
    ):
        stream_url, title, headers = self.resolve_youtube_stream(url)
        if stream_url:
            if title:
                channel.setdefault("_yt_titles", {})[url] = title
            self.root.after(
                0,
                lambda: self._play_media_source(
                    channel,
                    stream_url,
                    request_id=request_id,
                    title=title,
                    headers=headers,
                    origin_url=url,
                    seek_ms=seek_ms,
                    show_overlay=show_overlay,
                ),
            )
            return

        channel.setdefault("_yt_failed_urls", set()).add(url)
        self.scheduler.invalidate_stream(url)
        self.root.after(
            0,
            lambda: self._show_channel_error(request_id, "Could not resolve scheduled stream"),
        )

    def _resolve_and_play(self, channel, url, request_id, show_overlay: bool = True):
        stream_url, title, headers = self.resolve_youtube_stream(url)
        if stream_url:
            if title:
                channel.setdefault("_yt_titles", {})[url] = title
            self.root.after(
                0,
                lambda: self._play_media_source(
                    channel,
                    stream_url,
                    request_id=request_id,
                    title=title,
                    headers=headers,
                    origin_url=url,
                    show_overlay=show_overlay,
                ),
            )
            return

        channel["_recover_inflight"] = False
        if url.startswith("http"):
            channel.setdefault("_yt_failed_urls", set()).add(url)
        self.scheduler.invalidate_stream(url)
        self.root.after(
            0,
            lambda: self._show_channel_error(
                request_id,
                "Could not resolve stream for selected video",
            ),
        )

    # ------------------------------------------------------------------
    # Core playback
    # ------------------------------------------------------------------

    def _apply_headers(self, media, headers):
        if not headers:
            return
        user_agent = headers.get("User-Agent") or headers.get("user-agent")
        referer = headers.get("Referer") or headers.get("referer")
        if user_agent:
            media.add_option(f":http-user-agent={user_agent}")
        if referer:
            media.add_option(f":http-referrer={referer}")
        media.add_option(":network-caching=400")

    def _play_media_source(
        self,
        channel,
        source,
        request_id=None,
        title=None,
        headers=None,
        origin_url=None,
        seek_ms: int = 0,
        show_overlay: bool = False,
    ):
        if request_id is not None and request_id != self.channel_request_id:
            return
        if title:
            channel["_current_title"] = title
        channel["_resolved_src"] = source
        if origin_url:
            channel["_current_yt_url"] = origin_url
            channel.setdefault("_yt_failed_urls", set()).discard(origin_url)
        channel["_recover_tries"] = 0
        channel["_recover_inflight"] = False

        cache_key = str(channel.get("number", ""))

        # Invalidate stale cache so EPG rebuilds with real player position
        if cache_key in self._epg_cache:
            del self._epg_cache[cache_key]

        self._epg_items = self._build_epg_items(channel)
        self._epg_row_index = 0
        if show_overlay:
            self.show_epg(user_initiated=True)
        else:
            self.show_epg(user_initiated=False)

        self._suppress_end_event = True
        self.root.after(300, lambda: setattr(self, "_suppress_end_event", False))

        if not source:
            self._show_channel_error(
                request_id,
                f"Channel {channel.get('number', '')} has no playable source",
            )
            return

        media = self.instance.media_new(source)
        self._apply_headers(media, headers)
        self.player.set_media(media)
        self._set_video_window()
        self.player.audio_set_volume(self._volume)
        self.player.play()

        effective_seek = seek_ms
        if effective_seek == 0 and not self._is_youtube_channel(channel):
            effective_seek = self._resume_ms(channel)

        if effective_seek > 0:
            self._seek_when_ready(effective_seek, request_id)

        self._start_tick(request_id)

    # ------------------------------------------------------------------
    # Seek helper
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Error / recovery
    # ------------------------------------------------------------------

    def _show_channel_error(self, request_id, message):
        if request_id is not None and request_id != self.channel_request_id:
            return
        print(f"[Channel] {message}", file=sys.stderr)
        channel = self.current_channel
        if not channel:
            return
        if self._is_youtube_channel(channel):
            if channel.get("_recover_tries", 0) >= 2:
                channel["_current_title"] = "Channel unavailable"
                channel["_recover_inflight"] = False
                self.show_epg(user_initiated=False)
                return
            self._recover_youtube_channel(channel)
            return

        channel["_current_title"] = "Playback unavailable"
        self.show_epg(user_initiated=False)

    # ------------------------------------------------------------------
    # Progress tick
    # ------------------------------------------------------------------

    def _start_tick(self, request_id):
        if self._epg_tick:
            self.root.after_cancel(self._epg_tick)
        self._do_tick(request_id)

    def _handle_media_end(self):
        if getattr(self, "_suppress_end_event", False):
            return
        channel = self.current_channel
        if not channel:
            return
        if not self._is_youtube_channel(channel):
            return

        self.channel_request_id += 1
        request_id = self.channel_request_id
        # Keep previous title visible while resolving
        if not channel.get("_current_title"):
            channel["_current_title"] = channel.get(
                "name",
                ""
            )
        threading.Thread(
            target=self._resolve_channel_program,
            args=(channel, request_id, False),
            daemon=True,
        ).start()

    def _do_tick(self, request_id):
        if request_id != self.channel_request_id:
            return
        try:
            state = self.player.get_state()
            if state != vlc.State.Playing:
                self._epg_tick = self.root.after(800, lambda: self._do_tick(request_id))
                return
            duration = self.player.get_length()
            position = self.player.get_time()
            if duration > 0 and position >= 0:
                fill_w = max(2, min(219, int(220 * position / duration)))
                self.epg_progress.coords(self.progress_fill, 0, 2, fill_w, 8)
        except Exception:
            pass
        self._epg_tick = self.root.after(500, lambda: self._do_tick(request_id))

    # ------------------------------------------------------------------
    # VLC window binding
    # ------------------------------------------------------------------

    def _set_video_window(self):
        self.root.update_idletasks()
        handle = self.video_panel.winfo_id()
        if sys.platform.startswith("win"):
            self.player.set_hwnd(handle)
        elif sys.platform.startswith("linux"):
            self.player.set_xwindow(handle)
        elif sys.platform == "darwin":
            self.player.set_nsobject(handle)

    # ------------------------------------------------------------------
    # File / direct-play helpers
    # ------------------------------------------------------------------

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
