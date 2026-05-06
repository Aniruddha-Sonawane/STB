# STB Master Channel Schedule

This project now uses separate master files:

- `channels.json`: channel number + metadata + schedule reference.
- `channel_schedules.json`: all schedule blocks and fallback feed nodes.
- `channels_by_genre.json`: optional grouped view.

## Schedule format

Each channel schedule in `channel_schedules.json` has:

- `weekdays`: used for Monday-Friday
- `weekends`: used for Saturday-Sunday
- `unscheduled`: fallback feed used outside scheduled slots

Each slot supports:

- `start` (for example `20:00`)
- `duration_minutes` (for example `30`)
- `playlist` (YouTube playlist/channel/videos URL)
- `mode` (`daily_rotate` or `sequential`)
- `title`

`unscheduled` supports separate weekday/weekend playlists:

- `unscheduled.weekdays.playlist`
- `unscheduled.weekends.playlist`

### Daily rotate behavior

For `daily_rotate`, the same slot plays:

- day 1: first video
- day 2: second video
- day 3: third video

...and so on, cycling by playlist length.

## Current setup

- Channel `100` is fixed as `TataSky`.
- Channel `100` uses `https://www.youtube.com/@TataPlayOfficial/videos` for weekday/weekend prime slots.
- Channel `100` also uses Tata Play for unscheduled time.
- All other channels are imported from your PDF and have empty playlist fields for you to fill.

## Regenerate from a new PDF

```powershell
python scripts/import_channels_from_pdf.py --pdf "C:\path\Channel List.pdf" --schedules-out "channel_schedules.json"
```
