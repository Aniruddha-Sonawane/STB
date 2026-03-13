"""
stb_player/scheduler.py
=======================
Persistent video-cache and clock-based schedule engine.

The cache stores, for every channel number, the full list of YouTube
video metadata (url, title, duration in seconds).  It is written to
video_cache.json next to channels.json and is refreshed automatically
after CACHE_MAX_AGE seconds (default 24 h).

Schedule logic
--------------
All videos for a channel are played in order, looping forever.  Given
the sum of all durations we can compute, from the real wall-clock time,
exactly which video should be playing and at which second within it –
so every viewer who tunes in at the same time sees the same content,
just like a real broadcast channel.

    total_secs  = sum(v["duration"] for v in videos)
    loop_pos    = int(time.time()) % total_secs
    ↓
    walk videos in order, subtract durations until loop_pos is exhausted
    → (current_video_dict, seek_seconds)
"""

from __future__ import annotations

import json
import os
import threading
import time

# Cache expires after 24 hours; tune down to test faster refreshes.
CACHE_MAX_AGE: int = 24 * 3_600


class ChannelScheduler:
    """Thread-safe cache + deterministic schedule for all channels."""

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self, cache_file: str) -> None:
        self.cache_file = cache_file
        self._lock = threading.Lock()
        self._data: dict = self._load()

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _load(self) -> dict:
        try:
            with open(self.cache_file, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def _save(self) -> None:
        """Write cache to disk.  Called while holding self._lock."""
        try:
            with open(self.cache_file, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, ensure_ascii=False)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def is_stale(self, channel_num: str | int) -> bool:
        """Return True if the cached data is missing or older than CACHE_MAX_AGE."""
        entry = self._data.get(str(channel_num))
        if not entry or not entry.get("videos"):
            return True
        age = time.time() - entry.get("fetched_at", 0)
        return age > CACHE_MAX_AGE

    def get_videos(self, channel_num: str | int) -> list[dict]:
        """
        Return the cached video list for *channel_num*.

        Each item is a dict with keys: ``url`` (str), ``title`` (str),
        ``duration`` (int, seconds – 0 if unknown).
        """
        return list(self._data.get(str(channel_num), {}).get("videos", []))

    def update(self, channel_num: str | int, name: str, videos: list[dict]) -> None:
        """
        Persist a fresh video list for *channel_num*.

        *videos* must be a list of dicts with at least ``url``.
        Missing ``title`` / ``duration`` keys are normalised to defaults.
        """
        normalised = []
        for v in videos:
            normalised.append(
                {
                    "url": v.get("url", ""),
                    "title": v.get("title", ""),
                    "duration": int(v.get("duration") or 0),
                }
            )
        with self._lock:
            self._data[str(channel_num)] = {
                "name": name,
                "fetched_at": time.time(),
                "videos": normalised,
            }
            self._save()

    # ------------------------------------------------------------------
    # Schedule computation
    # ------------------------------------------------------------------

    def _schedulable(self, channel_num: str | int) -> list[dict]:
        """
        Return only the videos that have a known duration > 0.
        Falls back to the full list if nothing has duration metadata.
        """
        all_vids = self.get_videos(channel_num)
        with_dur = [v for v in all_vids if v.get("duration", 0) > 0]
        return with_dur if with_dur else all_vids

    def get_now_playing(self, channel_num: str | int) -> tuple[dict | None, int]:
        """
        Return ``(video_dict, seek_ms)`` for what should be airing right now.

        The position is deterministic: it only depends on the current
        wall-clock second and the total loop duration, so the result is
        the same on every machine at the same time.

        Returns ``(None, 0)`` when no videos are available.
        """
        videos = self._schedulable(channel_num)
        if not videos:
            return None, 0

        total = sum(v.get("duration", 0) for v in videos)
        if total == 0:
            # No duration data at all – just play the first video from
            # the beginning (deterministic by channel number mod list len).
            idx = int(time.time()) % len(videos)
            return videos[idx], 0

        pos_sec = int(time.time()) % total
        elapsed = 0
        for video in videos:
            dur = video.get("duration", 0)
            if elapsed + dur > pos_sec:
                seek_ms = (pos_sec - elapsed) * 1000
                return video, seek_ms
            elapsed += dur

        # Shouldn't happen, but fall back gracefully
        return videos[0], 0

    def get_next_video(
        self, channel_num: str | int, current_url: str = ""
    ) -> dict | None:
        """
        Return the video that *should* be airing right now (post-end seek).

        When a video finishes, the wall clock has advanced, so calling
        this method again will naturally return whichever video corresponds
        to the new time position – which may be the same video (if it's
        long) or the following one.
        """
        video, _ = self.get_now_playing(channel_num)
        return video

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def video_count(self, channel_num: str | int) -> int:
        return len(self.get_videos(channel_num))
