# STB Scheduler Architecture

This project now uses deterministic, TV-style scheduling:

- `channels.json`: channel metadata only.
- `schedules/<channel>.json`: time blocks (`start`, `duration_minutes`, `playlist`, `mode`).
- `video_cache.json`: long-lived metadata cache (video list, title, duration).
- `stream_cache.json`: short-lived direct stream URL cache.

## Channel config (`channels.json`)

```json
{
  "100": {
    "name": "Tata Play",
    "schedule": "schedules/100.json"
  },
  "103": {
    "name": "Movie Channel",
    "source": "videos/movies/"
  }
}
```

## Schedule config (`schedules/100.json`)

```json
{
  "timezone": "Asia/Kolkata",
  "days": {
    "default": [
      {
        "start": "20:00",
        "duration_minutes": 30,
        "playlist": "https://www.youtube.com/playlist?list=...",
        "mode": "daily_rotate",
        "title": "Prime Slot"
      },
      {
        "start": "20:30",
        "duration_minutes": 30,
        "playlist": "https://www.youtube.com/playlist?list=...",
        "mode": "sequential",
        "title": "Next Slot"
      }
    ]
  }
}
```

## Playback modes

- `sequential`: moves through playlist using video durations.
- `daily_rotate`: same time slot picks next playlist index each day.

## Runtime behavior

- No all-channel startup resolving.
- Channel tune resolves only current slot.
- Re-entering the same channel does not restart playback.
- Auto transitions do not force EPG popup.
- Expired direct stream URLs refresh automatically.

## Import from PDF

Use the importer to rebuild channel metadata from a new provider PDF:

```powershell
python scripts/import_channels_from_pdf.py --pdf "C:\path\Channel List.pdf"
```
