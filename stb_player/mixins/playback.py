"""
stb_player/mixins/playback.py
=============================
Media playback and channel-switching logic.

What changed vs original
------------------------
* ``switch_channel`` for YouTube sources now uses the scheduler to
  determine the clock-scheduled video and seek offset instead of picking
  a random video every time.  This means:
    – Same video is always on air at a given wall-clock time.
    – Tuning to a channel mid-video will seek to the correct position
      (e.g. a 54-min video started at 1:00 AM; tuning at 1:37 AM will
      seek to the 37-minute mark).
* ``_handle_media_end`` advances to the *next clock-scheduled* video
  instead of picking randomly; this keeps the schedule intact as time
  progresses.
* The pre-resolved startup stream is used with its stored seek offset
  so the very first channel also starts in the right place.
* ``_yt_list`` is now a list of URL strings extracted from the scheduler
  video-dict objects; all related helpers remain compatible.
"""

import glob
import os
import random
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
    # Recovery helpers (unchanged)
    # ------------------------------------------------------------------

    def _recover_youtube_channel(self, channel):
        if channel.get("_recover_inflight"):
            return
        tries = channel.get("_recover_tries", 0)
        if tries >= 2:
            channel["_current_title"] = "Unable to load stream"
            channel["_recover_inflight"] = False
            self.show_epg()
            return

        # Use scheduler to get the next clock-appropriate video
        num = channel.get("number", "")
        next_video = self.scheduler.get_next_video(num)
        if next_video:
            next_url = next_video["url"]
        else:
            candidates = self._youtube_candidates(channel)
            if not candidates:
                channel["_current_title"] = "No playable videos"
                channel["_recover_inflight"] = False
                self.show_epg()
                return
            next_url = random.choice(candidates[: min(10, len(candidates))])

        channel["_recover_tries"] = tries + 1
        channel["_recover_inflight"] = True
        self.channel_request_id += 1
        request_id = self.channel_request_id
        channel["_current_title"] = "Loading..."
        self.show_epg()
        self.root.after(
            8000,
            lambda ch=channel, rid=request_id: self._recover_timeout(ch, rid),
        )
        threading.Thread(
            target=self._resolve_and_play,
            args=(channel, next_url, request_id),
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
    # Position snapshot (for live/file channels; not used for schedule)
    # ------------------------------------------------------------------

    def _snapshot(self):
        previous = self.current_channel
        if not previous:
            return
        number = previous.get("number")
        if not number:
            return
        source = previous.get("source", "")
        is_youtube = isinstance(source, str) and (
            source.startswith("yt:") or "youtube.com" in source
        )
        # Don't snapshot YouTube channels – they use the clock schedule,
        # not a manual resume position.
        if is_youtube:
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
        """Only used for non-YouTube (file/live) channels."""
        state = self.channel_state.get(channel.get("number"))
        if not state:
            return 0
        return state.get("position_ms", 0) + int(
            (_t.time() - state.get("left_at", 0)) * 1000
        )

    # ------------------------------------------------------------------
    # Main channel switch
    # ------------------------------------------------------------------

    def switch_channel(self, channel: dict):
        previous_channel = self.current_channel
        self._snapshot()
        self.current_channel = channel
        self.channel_request_id += 1
        self._epg_row_index = 0
        self._epg_items = []
        request_id = self.channel_request_id
        source = channel.get("source", "")

        is_first_channel = not previous_channel

        # ---- YouTube / yt: channels ----
        if isinstance(source, str) and (
            source.startswith("yt:") or "youtube.com" in source
        ):
            channel["_recover_inflight"] = False

            # Case 1: preloaded next video is ready (from the preload worker)
            if self._preload_channel is channel and self._preload_result:
                stream_url, title, headers, origin_url = self._preload_result
                self._preload_result = None
                self._preload_channel = None
                channel["_current_title"] = title or ""
                self._play_media_source(
                    channel,
                    stream_url,
                    request_id=request_id,
                    title=title,
                    headers=headers,
                    origin_url=origin_url,
                )
                return

            # Case 2: startup pre-resolved stream is available (first launch)
            if is_first_channel:
                prepared = channel.pop("_startup_stream", None)
                seek_ms = channel.pop("_startup_seek_ms", 0)
                if isinstance(prepared, tuple) and prepared[0]:
                    src, title, headers, origin_url = (
                        prepared + (None,) * (4 - len(prepared))
                    )[:4]
                    self._play_media_source(
                        channel,
                        src,
                        request_id=request_id,
                        title=title,
                        headers=headers,
                        origin_url=origin_url,
                        seek_ms=seek_ms,
                    )
                    return

            # Case 3: resolve the clock-scheduled video now (background)
            num = channel.get("number", "")
            scheduled_video, seek_ms = self.scheduler.get_now_playing(num)

            if scheduled_video:
                url = scheduled_video["url"]
                # Use a cached title if we have one
                title = (
                    channel.get("_yt_titles", {}).get(url)
                    or channel.get("_yt_entry_titles", {}).get(url)
                    or scheduled_video.get("title", "")
                    or "Loading…"
                )
                channel["_current_title"] = title
                self.show_epg()
                threading.Thread(
                    target=self._resolve_and_play_scheduled,
                    args=(channel, url, request_id, seek_ms),
                    daemon=True,
                ).start()
            else:
                # No cache yet – fall back to fetching the channel
                channel["_current_title"] = "Loading…"
                self.show_epg()
                threading.Thread(
                    target=self._load_yt_channel,
                    args=(channel, source, request_id),
                    daemon=True,
                ).start()
            return

        # ---- Local file / folder ----
        if source and os.path.isdir(source):
            files = []
            for ext in ("*.mp4", "*.mkv", "*.avi", "*.mov", "*.wmv"):
                files.extend(glob.glob(os.path.join(source, ext)))
            if files:
                source = files[0]

        if is_first_channel:
            prepared = channel.pop("_startup_stream", None)
            if isinstance(prepared, tuple) and prepared[0]:
                source = prepared[0]

        self._play_media_source(
            channel,
            source,
            request_id=request_id,
        )

    # ------------------------------------------------------------------
    # Resolve & play (scheduled – includes seek)
    # ------------------------------------------------------------------

    def _resolve_and_play_scheduled(self, channel, url, request_id, seek_ms: int = 0):
        """
        Background worker: resolve *url* to a direct stream and play it,
        seeking to *seek_ms* so the viewer joins mid-video at the correct
        clock position.
        """
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
                ),
            )
        else:
            channel.setdefault("_yt_failed_urls", set()).add(url)
            # Fall back: try any available video
            self.root.after(
                0,
                lambda: self._show_channel_error(
                    request_id, "Could not resolve scheduled stream"
                ),
            )

    def _resolve_and_play(self, channel, url, request_id):
        """Original resolver (no forced seek) – used for user-selected EPG items."""
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
                ),
            )
        else:
            channel["_recover_inflight"] = False
            if url.startswith("http"):
                channel.setdefault("_yt_failed_urls", set()).add(url)
            self.root.after(
                0,
                lambda: self._show_channel_error(
                    request_id,
                    "Could not resolve stream for selected video",
                ),
            )

    # ------------------------------------------------------------------
    # Lazy YouTube channel load (fallback when cache is empty)
    # ------------------------------------------------------------------

    def _load_yt_channel(self, channel, source, request_id):
        try:
            num = channel.get("number", "")
            if not channel.get("_yt_list"):
                yt_videos, title_map = self.fetch_youtube_videos(source)
                if yt_videos:
                    self.scheduler.update(num, channel.get("name", ""), yt_videos)
                    channel["_yt_list"] = [v["url"] for v in yt_videos]
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

            # Now that we have videos, use the scheduler
            scheduled_video, seek_ms = self.scheduler.get_now_playing(num)
            if scheduled_video:
                url = scheduled_video["url"]
            else:
                candidates = self._youtube_candidates(channel, include_current=True)
                if not candidates:
                    self.root.after(
                        0,
                        lambda: self._show_channel_error(
                            request_id, "No playable videos"
                        ),
                    )
                    return
                url = random.choice(candidates[:10])
                seek_ms = 0

            stream_url, title, headers = self.resolve_youtube_stream(url)
            if not stream_url:
                channel.setdefault("_yt_failed_urls", set()).add(url)
                self.root.after(
                    0,
                    lambda: self._show_channel_error(
                        request_id,
                        f"Cannot resolve stream for {channel.get('name', '')}",
                    ),
                )
                return

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
                ),
            )
        except Exception as exc:
            message = str(exc)
            self.root.after(0, lambda: self._show_channel_error(request_id, message))

    # ------------------------------------------------------------------
    # Preload worker (pre-resolves the next scheduled video)
    # ------------------------------------------------------------------

    def _preload_next(self, channel):
        source = channel.get("source", "")
        if not (isinstance(source, str) and (
            source.startswith("yt:") or "youtube.com" in source
        )):
            return
        if getattr(self, "_preload_worker_channel", None) is channel:
            return
        self._preload_worker_channel = channel

        def _work():
            try:
                num = channel.get("number", "")
                next_video = self.scheduler.get_next_video(num)
                if not next_video:
                    # Fall back to candidates if scheduler has no data
                    candidates = self._youtube_candidates(channel)
                    if not candidates:
                        return
                    random.shuffle(candidates)
                    for url in candidates[:10]:
                        stream_url, title, headers = self.resolve_youtube_stream(url)
                        if stream_url:
                            if title:
                                channel.setdefault("_yt_titles", {})[url] = title
                            self._preload_result = (stream_url, title, headers, url)
                            self._preload_channel = channel
                            self.root.after(0, self._on_preload_ready)
                            return
                        channel.setdefault("_yt_failed_urls", set()).add(url)
                    return

                url = next_video["url"]
                stream_url, title, headers = self.resolve_youtube_stream(url)
                if stream_url:
                    if title:
                        channel.setdefault("_yt_titles", {})[url] = title
                    self._preload_result = (stream_url, title, headers, url)
                    self._preload_channel = channel
                    self.root.after(0, self._on_preload_ready)
                else:
                    channel.setdefault("_yt_failed_urls", set()).add(url)
            finally:
                if getattr(self, "_preload_worker_channel", None) is channel:
                    self._preload_worker_channel = None

        threading.Thread(target=_work, daemon=True).start()

    def _on_preload_ready(self):
        if self._preload_result and self._preload_channel is self.current_channel:
            title = self._preload_result[1] or ""
            if self._epg_items and len(self._epg_items) > 1:
                if self._epg_items[1][2] != "__preload__":
                    self._epg_items.insert(1, (title, "Up Next", "__preload__"))
                else:
                    self._epg_items[1] = (title, "Up Next", "__preload__")
            self._render_epg_rows()

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

        self._epg_items = self._build_epg_items(channel)
        self._epg_row_index = 0

        self.show_epg()
        self._suppress_end_event = True
        self.root.after(300, lambda: setattr(self, "_suppress_end_event", False))

        if source:
            media = self.instance.media_new(source)
            self._apply_headers(media, headers)
            self.player.set_media(media)
            self._set_video_window()
            self.player.audio_set_volume(self._volume)
            self.player.play()

            # For file/live channels use the snapshot resume position;
            # for YouTube channels use the clock-schedule seek offset.
            effective_seek = seek_ms
            if effective_seek == 0:
                src_is_youtube = isinstance(
                    channel.get("source", ""), str
                ) and (
                    channel["source"].startswith("yt:")
                    or "youtube.com" in channel["source"]
                )
                if not src_is_youtube:
                    effective_seek = self._resume_ms(channel)

            if effective_seek > 0:
                self._seek_when_ready(effective_seek, request_id)

            self._preload_next(channel)
            self._start_tick(request_id)
        else:
            self._show_channel_error(
                request_id,
                f"Channel {channel.get('number', '')} has no playable source",
            )

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
        source = channel.get("source", "")
        is_youtube = isinstance(source, str) and (
            source.startswith("yt:") or "youtube.com" in source
        )
        if is_youtube:
            if channel.get("_recover_tries", 0) >= 2:
                channel["_current_title"] = "Channel unavailable"
                channel["_recover_inflight"] = False
                self.show_epg()
                return
            self._recover_youtube_channel(channel)
            return
        channel["_current_title"] = "Playback unavailable"
        self.show_epg()

    # ------------------------------------------------------------------
    # Progress tick (EPG bar update + preload trigger)
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
        source = channel.get("source", "")
        is_youtube = isinstance(source, str) and (
            source.startswith("yt:") or "youtube.com" in source
        )
        if not is_youtube:
            return

        request_id = self.channel_request_id

        # Use preloaded stream if ready
        if self._preload_result and self._preload_channel is channel:
            stream_url, title, headers, origin_url = self._preload_result
            self._preload_result = None
            self._preload_channel = None
            channel["_current_title"] = title or channel.get("_current_title", "")
            self._play_media_source(
                channel,
                stream_url,
                request_id=request_id,
                title=title,
                headers=headers,
                origin_url=origin_url,
                # No seek on a fresh video-end transition
                seek_ms=0,
            )
            return

        # Use the scheduler to determine what should be on air now.
        # Since the current video just ended, the clock has advanced, so
        # get_now_playing will naturally return the next video in the loop.
        num = channel.get("number", "")
        next_video, seek_ms = self.scheduler.get_now_playing(num)
        if next_video:
            url = next_video["url"]
        else:
            candidates = self._youtube_candidates(channel)
            if not candidates:
                self._show_channel_error(request_id, "No playable videos in this channel")
                return
            url = random.choice(candidates[: min(10, len(candidates))])
            seek_ms = 0

        self.channel_request_id += 1
        next_request_id = self.channel_request_id
        channel["_current_title"] = "Loading…"
        self.show_epg()
        threading.Thread(
            target=self._resolve_and_play_scheduled,
            args=(channel, url, next_request_id, seek_ms),
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
                remaining = duration - position
                if (
                    remaining < 15_000
                    and self.current_channel
                    and self._preload_channel is not self.current_channel
                ):
                    self._preload_next(self.current_channel)
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
