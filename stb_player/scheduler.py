"""
stb_player/scheduler.py
=======================
Schedule + cache engine.

This module separates:
1) channel/program scheduling (clock-time blocks from JSON),
2) playlist metadata cache (long lived),
3) direct stream cache (short lived; CDN links expire).

Schedule files are expected in this shape:

{
  "timezone": "Asia/Kolkata",
  "days": {
    "default": [
      {
        "start": "20:00",
        "duration_minutes": 30,
        "playlist": "https://www.youtube.com/playlist?list=...",
        "mode": "daily_rotate",
        "title": "Prime Time Block"
      }
    ]
  }
}
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
import threading
import time as unix_time
try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python without zoneinfo
    ZoneInfo = None


METADATA_CACHE_MAX_AGE = 12 * 3600
STREAM_CACHE_TTL = 25 * 60
DEFAULT_SLOT_MINUTES = 30

_DAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


@dataclass
class ScheduleBlock:
    title: str
    playlist: str
    mode: str
    start: str
    duration_minutes: int
    start_dt: datetime
    end_dt: datetime
    day_key: str
    block_index: int


class ChannelScheduler:
    def __init__(
        self,
        metadata_cache_file: str,
        stream_cache_file: str,
        project_root: str,
    ) -> None:
        self.metadata_cache_file = metadata_cache_file
        self.stream_cache_file = stream_cache_file
        self.project_root = Path(project_root)
        self._lock = threading.Lock()
        self._metadata = self._load_metadata()
        self._streams = self._load_streams()
        self._schedule_cache: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _safe_json_load(self, path: str) -> dict:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    def _safe_json_save(self, path: str, data: dict) -> None:
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _load_metadata(self) -> dict:
        loaded = self._safe_json_load(self.metadata_cache_file)
        if "sources" in loaded and isinstance(loaded["sources"], dict):
            return loaded

        # Legacy migration: old cache was keyed by channel number.
        sources = {}
        for legacy_key, legacy_value in loaded.items():
            if not isinstance(legacy_value, dict):
                continue
            videos = legacy_value.get("videos")
            if not isinstance(videos, list):
                continue
            source_key = f"legacy:{legacy_key}"
            sources[source_key] = {
                "name": legacy_value.get("name", ""),
                "fetched_at": float(legacy_value.get("fetched_at", 0)),
                "videos": self._normalise_videos(videos),
            }

        migrated = {"version": 2, "sources": sources}
        self._safe_json_save(self.metadata_cache_file, migrated)
        return migrated

    def _load_streams(self) -> dict:
        loaded = self._safe_json_load(self.stream_cache_file)
        if "items" in loaded and isinstance(loaded["items"], dict):
            return loaded
        fresh = {"version": 1, "items": {}}
        self._safe_json_save(self.stream_cache_file, fresh)
        return fresh

    # ------------------------------------------------------------------
    # Metadata cache (playlist/channel video lists)
    # ------------------------------------------------------------------

    def _normalise_videos(self, videos: list[dict]) -> list[dict]:
        normalised: list[dict] = []
        for item in videos:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url", "")).strip()
            if not url:
                continue
            normalised.append(
                {
                    "url": url,
                    "title": str(item.get("title", "") or ""),
                    "duration": int(item.get("duration") or 0),
                }
            )
        return normalised

    def metadata_is_stale(self, source_key: str) -> bool:
        entry = self._metadata.get("sources", {}).get(source_key, {})
        if not entry.get("videos"):
            return True
        age = unix_time.time() - float(entry.get("fetched_at", 0))
        return age > METADATA_CACHE_MAX_AGE

    def get_videos(self, source_key: str) -> list[dict]:
        entry = self._metadata.get("sources", {}).get(source_key, {})
        return list(entry.get("videos", []))

    def update_metadata(self, source_key: str, name: str, videos: list[dict]) -> None:
        normalised = self._normalise_videos(videos)
        with self._lock:
            self._metadata.setdefault("sources", {})
            self._metadata["sources"][source_key] = {
                "name": name,
                "fetched_at": unix_time.time(),
                "videos": normalised,
            }
            self._safe_json_save(self.metadata_cache_file, self._metadata)

    # ------------------------------------------------------------------
    # Stream cache (short-lived resolved CDN URLs)
    # ------------------------------------------------------------------

    def get_stream(self, video_url: str) -> tuple[str | None, str | None, dict | None]:
        item = self._streams.get("items", {}).get(video_url)
        if not isinstance(item, dict):
            return None, None, None

        now_ts = unix_time.time()
        expires_at = float(item.get("expires_at", 0) or 0)
        resolved_at = float(item.get("resolved_at", 0) or 0)
        if expires_at and now_ts >= expires_at:
            return None, None, None
        if not expires_at and now_ts - resolved_at > STREAM_CACHE_TTL:
            return None, None, None

        stream_url = item.get("stream_url")
        if not stream_url:
            return None, None, None
        title = item.get("title")
        headers = item.get("headers") or {}
        return str(stream_url), (str(title) if title else None), headers

    def put_stream(
        self,
        video_url: str,
        stream_url: str,
        title: str | None,
        headers: dict | None,
        expires_at: float | None = None,
    ) -> None:
        with self._lock:
            self._streams.setdefault("items", {})
            self._streams["items"][video_url] = {
                "stream_url": stream_url,
                "title": title or "",
                "headers": headers or {},
                "resolved_at": unix_time.time(),
                "expires_at": float(expires_at or 0),
            }
            self._safe_json_save(self.stream_cache_file, self._streams)

    def invalidate_stream(self, video_url: str) -> None:
        with self._lock:
            items = self._streams.setdefault("items", {})
            if video_url in items:
                items.pop(video_url, None)
                self._safe_json_save(self.stream_cache_file, self._streams)

    # ------------------------------------------------------------------
    # Schedule loading
    # ------------------------------------------------------------------

    def prepare_channel(self, channel: dict) -> None:
        self.get_schedule(channel)

    def get_schedule(self, channel: dict) -> dict | None:
        schedule_ref = channel.get("schedule")
        if isinstance(schedule_ref, list):
            return {"timezone": channel.get("timezone", "Asia/Kolkata"), "days": {"default": schedule_ref}}

        if not isinstance(schedule_ref, str) or not schedule_ref.strip():
            return None

        cache_key = schedule_ref.strip()
        cached = self._schedule_cache.get(cache_key)
        if cached is not None:
            return cached

        schedule_path = Path(cache_key)
        if not schedule_path.is_absolute():
            schedule_path = self.project_root / schedule_path

        loaded = self._safe_json_load(str(schedule_path))
        if not loaded:
            self._schedule_cache[cache_key] = None
            return None

        if "days" not in loaded or not isinstance(loaded["days"], dict):
            self._schedule_cache[cache_key] = None
            return None

        if "timezone" not in loaded:
            loaded["timezone"] = channel.get("timezone", "Asia/Kolkata")

        self._schedule_cache[cache_key] = loaded
        return loaded

    # ------------------------------------------------------------------
    # Program resolution
    # ------------------------------------------------------------------

    def resolve_program(self, channel: dict, when_dt: datetime | None = None) -> dict:
        source = str(channel.get("source", "") or "").strip()
        schedule = self.get_schedule(channel)
        if schedule:
            block = self._resolve_block(schedule, when_dt)
            if not block:
                return {
                    "video": None,
                    "seek_ms": 0,
                    "source_key": "",
                    "block": None,
                    "videos": [],
                }

            source_key = block.playlist
            videos = self.get_videos(source_key)
            if not videos:
                legacy_key = f"legacy:{channel.get('number', '')}"
                legacy_videos = self.get_videos(legacy_key)
                if legacy_videos:
                    self.update_metadata(source_key, channel.get("name", ""), legacy_videos)
                    videos = self.get_videos(source_key)
            video, seek_ms = self._select_video_for_block(block, videos, when_dt)
            return {
                "video": video,
                "seek_ms": seek_ms,
                "source_key": source_key,
                "block": block,
                "videos": videos,
            }

        # Legacy fallback: per-channel source playlist/page.
        if source:
            source_key = f"legacy:{channel.get('number', '')}" if not source.startswith("http") else source
            videos = self.get_videos(source_key)
            video, seek_ms = self._select_legacy(videos)
            return {
                "video": video,
                "seek_ms": seek_ms,
                "source_key": source_key,
                "block": None,
                "videos": videos,
            }

        return {
            "video": None,
            "seek_ms": 0,
            "source_key": "",
            "block": None,
            "videos": [],
        }

    def get_upcoming_blocks(
        self,
        channel: dict,
        count: int = 3,
        when_dt: datetime | None = None,
    ) -> list[ScheduleBlock]:
        schedule = self.get_schedule(channel)
        if not schedule:
            return []

        now = self._to_schedule_tz(schedule, when_dt)
        if not now:
            return []

        blocks: list[ScheduleBlock] = []
        cursor = now
        for _ in range(max(1, count)):
            block = self._resolve_block(schedule, cursor)
            if not block:
                break
            if blocks and block.start_dt == blocks[-1].start_dt and block.playlist == blocks[-1].playlist:
                cursor = block.end_dt + timedelta(seconds=1)
                continue
            blocks.append(block)
            cursor = block.end_dt + timedelta(seconds=1)
        return blocks

    def _select_legacy(self, videos: list[dict]) -> tuple[dict | None, int]:
        if not videos:
            return None, 0
        with_duration = [item for item in videos if int(item.get("duration") or 0) > 0]
        target = with_duration if with_duration else videos
        total = sum(int(item.get("duration") or 0) for item in target)
        if total <= 0:
            idx = int(unix_time.time()) % len(target)
            return target[idx], 0

        pos = int(unix_time.time()) % total
        elapsed = 0
        for item in target:
            dur = int(item.get("duration") or 0)
            if elapsed + dur > pos:
                return item, (pos - elapsed) * 1000
            elapsed += dur
        return target[0], 0

    def _select_video_for_block(
        self,
        block: ScheduleBlock,
        videos: list[dict],
        when_dt: datetime | None = None,
    ) -> tuple[dict | None, int]:
        if not videos:
            return None, 0

        if when_dt is not None:
            now = when_dt
        elif block.start_dt.tzinfo is not None:
            now = datetime.now(block.start_dt.tzinfo)
        else:
            now = datetime.now()
        if now.tzinfo is None and block.start_dt.tzinfo is not None:
            now = now.replace(tzinfo=block.start_dt.tzinfo)
        if now < block.start_dt:
            now = block.start_dt
        elapsed_sec = max(0, int((now - block.start_dt).total_seconds()))

        mode = (block.mode or "sequential").lower()

        if mode == "daily_rotate":
            day_seed = block.start_dt.date().toordinal() + block.block_index
            idx = day_seed % len(videos)
            chosen = videos[idx]
            duration = int(chosen.get("duration") or 0)
            if duration > 0:
                return chosen, (elapsed_sec % duration) * 1000
            return chosen, 0

        # sequential (default)
        total = sum(int(item.get("duration") or 0) for item in videos)
        if total <= 0:
            idx = (block.start_dt.date().toordinal() + elapsed_sec) % len(videos)
            return videos[idx], 0

        pos = elapsed_sec % total
        cursor = 0
        for item in videos:
            duration = int(item.get("duration") or 0)
            if duration <= 0:
                continue
            if cursor + duration > pos:
                return item, (pos - cursor) * 1000
            cursor += duration
        return videos[0], 0

    # ------------------------------------------------------------------
    # Block resolution helpers
    # ------------------------------------------------------------------

    def _tzinfo(self, tz_name: str):
        if ZoneInfo is None:
            return None
        try:
            return ZoneInfo(tz_name)
        except Exception:
            return None

    def _to_schedule_tz(self, schedule: dict, when_dt: datetime | None = None) -> datetime | None:
        tz_name = str(schedule.get("timezone") or "Asia/Kolkata")
        tz = self._tzinfo(tz_name)
        now = when_dt or datetime.now(tz=tz)
        if tz is None:
            return now
        if now.tzinfo is None:
            return now.replace(tzinfo=tz)
        return now.astimezone(tz)

    def _day_blocks(self, schedule: dict, day_key: str) -> list[dict]:
        days = schedule.get("days", {})
        blocks = days.get(day_key)
        if not blocks:
            blocks = days.get("default", [])
        if not isinstance(blocks, list):
            return []
        return [item for item in blocks if isinstance(item, dict)]

    def _parse_hhmm(self, value: str) -> time | None:
        if not isinstance(value, str):
            return None
        value = value.strip()
        if not value:
            return None
        try:
            hh, mm = value.split(":")
            return time(hour=int(hh), minute=int(mm))
        except Exception:
            return None

    def _materialize_day(self, schedule: dict, day_date: date) -> list[ScheduleBlock]:
        day_key = _DAY_NAMES[day_date.weekday()]
        raw = self._day_blocks(schedule, day_key)
        rows = []
        for index, item in enumerate(raw):
            start_str = str(item.get("start") or "").strip()
            start_tm = self._parse_hhmm(start_str)
            playlist = str(item.get("playlist") or "").strip()
            if not start_tm or not playlist:
                continue
            duration = int(item.get("duration_minutes") or DEFAULT_SLOT_MINUTES)
            if duration <= 0:
                duration = DEFAULT_SLOT_MINUTES
            rows.append((start_tm, index, item, duration))

        rows.sort(key=lambda item: (item[0].hour, item[0].minute))
        if not rows:
            return []

        tz_name = str(schedule.get("timezone") or "Asia/Kolkata")
        tz = self._tzinfo(tz_name)

        blocks: list[ScheduleBlock] = []
        for idx, (start_tm, original_index, item, default_duration) in enumerate(rows):
            start_dt = datetime.combine(day_date, start_tm)
            if tz is not None:
                start_dt = start_dt.replace(tzinfo=tz)

            duration_minutes = int(item.get("duration_minutes") or 0)
            if duration_minutes <= 0:
                next_start_tm = rows[(idx + 1) % len(rows)][0]
                next_day = day_date if idx + 1 < len(rows) else day_date + timedelta(days=1)
                next_start_dt = datetime.combine(next_day, next_start_tm)
                if tz is not None:
                    next_start_dt = next_start_dt.replace(tzinfo=tz)
                delta = next_start_dt - start_dt
                duration_minutes = max(DEFAULT_SLOT_MINUTES, int(delta.total_seconds() // 60))

            end_dt = start_dt + timedelta(minutes=duration_minutes)
            title = str(item.get("title") or item.get("name") or "").strip()
            mode = str(item.get("mode") or "sequential").strip().lower()
            blocks.append(
                ScheduleBlock(
                    title=title,
                    playlist=str(item.get("playlist", "")).strip(),
                    mode=mode,
                    start=start_tm.strftime("%H:%M"),
                    duration_minutes=duration_minutes,
                    start_dt=start_dt,
                    end_dt=end_dt,
                    day_key=day_key,
                    block_index=original_index,
                )
            )

        return blocks

    def _resolve_block(self, schedule: dict, when_dt: datetime | None = None) -> ScheduleBlock | None:
        now = self._to_schedule_tz(schedule, when_dt)
        if not now:
            return None

        today = now.date()
        yesterday = today - timedelta(days=1)

        candidates = self._materialize_day(schedule, yesterday) + self._materialize_day(
            schedule, today
        )
        for block in candidates:
            if block.start_dt <= now < block.end_dt:
                return block

        today_blocks = self._materialize_day(schedule, today)
        if not today_blocks:
            return None

        # If current time is outside explicit ranges, snap to nearest previous block.
        past_blocks = [block for block in today_blocks if block.start_dt <= now]
        if past_blocks:
            return past_blocks[-1]
        return today_blocks[0]
