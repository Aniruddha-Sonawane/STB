import threading
from tkinter import messagebox


class YoutubeMixin:
    def _get_audio_tracks(self, url):
        try:
            import yt_dlp as ydl
        except ImportError:
            return []

        options = {
            "quiet": True,
            "skip_download": True,
            "noplaylist": True,
            "no_warnings": True,
        }
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

            options = {
                "quiet": True,
                "skip_download": True,
                "noplaylist": True,
                "no_warnings": True,
            }
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

    def fetch_youtube_videos(self, source) -> list:
        try:
            import yt_dlp as ydl
        except ImportError:
            messagebox.showerror("Dependency", "yt-dlp required. pip install yt-dlp")
            return []

        options = {
            "quiet": True,
            "ignoreerrors": True,
            "extract_flat": True,
            "skip_download": True,
            "no_warnings": True,
            "playlistend": 50,
        }
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
            return []
        if not info:
            return []

        out = []
        title_map = {}
        if "entries" in info:
            for entry in info["entries"]:
                if not entry:
                    continue
                video_url = entry.get("webpage_url") or entry.get("url")
                if video_url and not video_url.startswith("http"):
                    video_url = f"https://www.youtube.com/watch?v={video_url}"
                if video_url:
                    out.append(video_url)
                    title = entry.get("title") or entry.get("fulltitle") or ""
                    if title:
                        title_map[video_url] = title
        else:
            video_url = info.get("url") or info.get("webpage_url")
            if video_url and not video_url.startswith("http"):
                video_url = f"https://www.youtube.com/watch?v={video_url}"
            if video_url:
                out.append(video_url)

        return out, title_map

    def resolve_youtube_stream(self, url):
        try:
            import yt_dlp as ydl
        except ImportError:
            return None, None, None

        options = {
            "quiet": True,
            "ignoreerrors": True,
            "skip_download": True,
            "noplaylist": True,
            "no_warnings": True,
        }
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