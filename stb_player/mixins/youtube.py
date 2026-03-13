"""
stb_player/mixins/youtube.py
============================
YouTube-specific helpers: yt-dlp wrappers for fetching channel video
lists (with duration metadata) and resolving direct stream URLs.

Key change vs original
----------------------
``fetch_youtube_videos`` now returns a **list of dicts**
``[{url, title, duration}, ...]`` instead of a plain URL list.
All callers have been updated accordingly; ``_yt_list`` is always built
by extracting the ``url`` key from these dicts.
"""

import threading
from tkinter import messagebox


class _SilentYdlLogger:
    def debug(self, _msg):
        pass

    def warning(self, _msg):
        pass

    def error(self, _msg):
        pass


class YoutubeMixin:
    # ------------------------------------------------------------------
    # yt-dlp option factory
    # ------------------------------------------------------------------

    def _ydl_options(self, **overrides):
        options = {
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
            "skip_download": True,
            "retries": 1,
            "extractor_retries": 1,
            "socket_timeout": 8,
            "logger": _SilentYdlLogger(),
        }
        options.update(overrides)
        return options

    # ------------------------------------------------------------------
    # Audio-track helpers (unchanged)
    # ------------------------------------------------------------------

    def _get_audio_tracks(self, url):
        try:
            import yt_dlp as ydl
        except ImportError:
            return []

        options = self._ydl_options(noplaylist=True)
        try:
            with ydl.YoutubeDL(options) as ydl_client:
                info = ydl_client.extract_info(url, download=False)
        except Exception:
            return []
        if not info:
            return []

        seen = set()
        tracks = [("auto", "Default (Auto)", None)]

        for fmt in info.get("formats", []):
            if fmt.get("vcodec", "none") not in ("none", None):
                continue
            language = fmt.get("language") or fmt.get("language_preference")
            if not language or language in seen:
                continue
            seen.add(language)
            acodec = fmt.get("acodec", "")
            abr = fmt.get("abr") or fmt.get("tbr") or 0
            if abr:
                label = f"{language.upper()} ({acodec} {int(abr)}kbps)"
            else:
                label = f"{language.upper()} ({acodec})"
            tracks.append((language, label, fmt.get("format_id")))

        return tracks

    def _switch_audio_track(self, fmt_id, lang):
        channel = self.current_channel
        url = channel.get("_resolved_src") or channel.get("source", "")
        if not url or not url.startswith("http"):
            return

        channel["_current_title"] = channel.get("_current_title", "") + f"  [{lang.upper()}]"
        self.channel_request_id += 1
        request_id = self.channel_request_id

        def _work():
            try:
                import yt_dlp as ydl
            except ImportError:
                return

            options = self._ydl_options(noplaylist=True)
            if fmt_id:
                options["format"] = f"bestvideo+{fmt_id}/best"
            try:
                with ydl.YoutubeDL(options) as ydl_client:
                    info = ydl_client.extract_info(url, download=False)
            except Exception:
                return
            if not info:
                return

            stream_url = None
            headers = info.get("http_headers") or {}
            title = info.get("title", channel.get("_current_title", ""))
            direct = info.get("url")
            if direct and direct.startswith("http"):
                stream_url = direct
            if not stream_url:
                for fmt in info.get("formats", []):
                    if fmt_id and fmt.get("format_id") == fmt_id:
                        stream_url = fmt.get("url")
                        break
            if not stream_url:
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

        threading.Thread(target=_work, daemon=True).start()

    # ------------------------------------------------------------------
    # Video-list fetching  (UPDATED – returns dicts with duration)
    # ------------------------------------------------------------------

    def fetch_youtube_videos(self, source: str) -> tuple[list[dict], dict]:
        """
        Fetch the video list for a YouTube channel or playlist.

        Returns
        -------
        videos : list[dict]
            Each dict has keys ``url`` (str), ``title`` (str),
            ``duration`` (int seconds, 0 if unknown).
        title_map : dict[str, str]
            Mapping ``url → title`` for quick lookups (backward-compat).
        """
        try:
            import yt_dlp as ydl
        except ImportError:
            messagebox.showerror("Dependency", "yt-dlp required. pip install yt-dlp")
            return [], {}

        # Increase playlist limit to get more videos for a richer schedule.
        options = self._ydl_options(extract_flat=True, playlistend=100)
        source_url = source
        if source.startswith("yt:"):
            source_url = f"https://www.youtube.com/channel/{source[3:]}/videos"
        elif "youtube.com/@" in source and not source.rstrip("/").endswith("/videos"):
            source_url = f"{source.rstrip('/')}/videos"
        elif "youtube.com/channel/" in source and not source.rstrip("/").endswith("/videos"):
            source_url = f"{source.rstrip('/')}/videos"
        elif "youtube.com/c/" in source and not source.rstrip("/").endswith("/videos"):
            source_url = f"{source.rstrip('/')}/videos"
        elif "youtube.com/user/" in source and not source.rstrip("/").endswith("/videos"):
            source_url = f"{source.rstrip('/')}/videos"

        try:
            with ydl.YoutubeDL(options) as ydl_client:
                info = ydl_client.extract_info(source_url, download=False)
        except Exception:
            return [], {}
        if not info:
            return [], {}

        videos: list[dict] = []
        title_map: dict[str, str] = {}

        if "entries" in info:
            for entry in info["entries"]:
                if not entry:
                    continue
                video_url = entry.get("webpage_url") or entry.get("url")
                if video_url and not video_url.startswith("http"):
                    video_url = f"https://www.youtube.com/watch?v={video_url}"
                if not video_url:
                    continue
                title = entry.get("title") or entry.get("fulltitle") or ""
                duration = int(entry.get("duration") or 0)
                videos.append({"url": video_url, "title": title, "duration": duration})
                if title:
                    title_map[video_url] = title
        else:
            video_url = info.get("url") or info.get("webpage_url")
            if video_url and not video_url.startswith("http"):
                video_url = f"https://www.youtube.com/watch?v={video_url}"
            if video_url:
                title = info.get("title") or ""
                duration = int(info.get("duration") or 0)
                videos.append({"url": video_url, "title": title, "duration": duration})
                if title:
                    title_map[video_url] = title

        return videos, title_map

    # ------------------------------------------------------------------
    # Stream URL resolution (unchanged)
    # ------------------------------------------------------------------

    def resolve_youtube_stream(self, url: str):
        """
        Resolve a YouTube video page URL to a direct streamable URL.

        Returns ``(stream_url, title, headers)`` or ``(None, None, None)``.
        """
        try:
            import yt_dlp as ydl
        except ImportError:
            return None, None, None

        options = self._ydl_options(noplaylist=True)
        try:
            with ydl.YoutubeDL(options) as ydl_client:
                info = ydl_client.extract_info(url, download=False)
        except Exception:
            return None, None, None
        if not info:
            return None, None, None

        title = info.get("title") or "YouTube Video"
        headers = info.get("http_headers") or {}
        direct = info.get("url")
        if direct and direct.startswith("http"):
            return direct, title, headers

        best = None
        best_score = -1
        for fmt in info.get("formats", []):
            stream_url = fmt.get("url")
            if not stream_url:
                continue
            if fmt.get("vcodec") == "none" or fmt.get("acodec") == "none":
                continue
            score = fmt.get("height") or 0
            if fmt.get("ext") == "mp4":
                score += 10000
            if fmt.get("protocol", "").startswith("http"):
                score += 1000
            if score > best_score:
                best_score = score
                best = fmt

        if best:
            return best.get("url"), title, headers

        for fmt in reversed(info.get("formats", [])):
            if fmt.get("url"):
                return fmt.get("url"), title, headers
        return None, title, headers
