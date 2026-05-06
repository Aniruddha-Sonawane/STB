from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import re

from pypdf import PdfReader


def parse_channel_rows(pdf_path: Path) -> list[dict]:
    reader = PdfReader(str(pdf_path))
    rows: list[dict] = []
    for page in reader.pages:
        text = page.extract_text(extraction_mode="layout") or ""
        for raw in text.splitlines():
            line = raw.rstrip()
            if not re.match(r"^\s*\d+\s{2,}", line):
                continue
            parts = re.split(r"\s{2,}", line.strip())
            if len(parts) < 6 or not parts[0].isdigit():
                continue

            sr_no = int(parts[0])
            channel_name = parts[1].strip()
            quality = parts[2].strip()
            genre = parts[3].strip()
            epg_no = parts[4].strip()
            fta_pay = parts[5].strip()
            if not epg_no.isdigit():
                continue

            rows.append(
                {
                    "sr_no": sr_no,
                    "epg_no": epg_no,
                    "name": channel_name,
                    "quality": quality,
                    "genre": genre,
                    "fta_pay": fta_pay,
                }
            )
    rows.sort(key=lambda item: int(item["epg_no"]))
    return rows


def _schedule_block(start: str, playlist: str, title: str) -> dict:
    return {
        "start": start,
        "duration_minutes": 30,
        "playlist": playlist,
        "mode": "daily_rotate",
        "title": title,
    }


def _default_channel_schedule(
    weekday_playlist: str = "",
    weekend_playlist: str = "",
    unscheduled_weekday_playlist: str = "",
    unscheduled_weekend_playlist: str = "",
) -> dict:
    return {
        "weekdays": [
            _schedule_block("20:00", weekday_playlist, "Weekday Prime 8 PM"),
            _schedule_block("20:30", weekday_playlist, "Weekday Prime 8:30 PM"),
        ],
        "weekends": [
            _schedule_block("20:00", weekend_playlist, "Weekend Prime 8 PM"),
            _schedule_block("20:30", weekend_playlist, "Weekend Prime 8:30 PM"),
        ],
        "unscheduled": {
            "weekdays": {
                "playlist": unscheduled_weekday_playlist,
                "mode": "sequential",
                "title": "Weekday Unscheduled Feed",
            },
            "weekends": {
                "playlist": unscheduled_weekend_playlist,
                "mode": "sequential",
                "title": "Weekend Unscheduled Feed",
            },
        },
    }


def build_channels_payload(
    rows: list[dict],
    schedule_ref_file: str,
) -> dict:
    payload: dict = {}
    for row in rows:
        key = row["epg_no"]
        payload[key] = {
            "name": row["name"],
            "epg_no": row["epg_no"],
            "genre": row["genre"],
            "quality": row["quality"],
            "fta_pay": row["fta_pay"],
            "schedule": f"{schedule_ref_file}#{key}",
        }

    # Ensure channel number 100 exists as TataSky.
    payload["100"] = {
        "name": "TataSky",
        "epg_no": "100",
        "genre": "DTH Services",
        "quality": "HD",
        "fta_pay": "FTA",
        "schedule": f"{schedule_ref_file}#100",
    }

    payload = dict(sorted(payload.items(), key=lambda kv: int(kv[0])))
    return payload


def build_schedules_payload(
    channel_numbers: list[str],
    tatasky_playlist: str,
) -> dict:
    channels: dict[str, dict] = {}
    for epg_no in channel_numbers:
        channels[epg_no] = _default_channel_schedule()

    channels["100"] = _default_channel_schedule(
        weekday_playlist=tatasky_playlist,
        weekend_playlist=tatasky_playlist,
        unscheduled_weekday_playlist=tatasky_playlist,
        unscheduled_weekend_playlist=tatasky_playlist,
    )

    return {
        "timezone": "Asia/Kolkata",
        "channels": channels,
    }


def build_genre_payload(channels_payload: dict) -> dict:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for epg_no, row in sorted(channels_payload.items(), key=lambda kv: int(kv[0])):
        grouped[row["genre"]].append(
            {
                "epg_no": epg_no,
                "name": row["name"],
                "quality": row["quality"],
                "fta_pay": row["fta_pay"],
            }
        )
    return {genre: grouped[genre] for genre in sorted(grouped)}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import STB channel list from PDF into JSON."
    )
    parser.add_argument(
        "--pdf",
        required=True,
        help="Path to the source channel-list PDF.",
    )
    parser.add_argument(
        "--channels-out",
        default="channels.json",
        help="Output path for channels JSON.",
    )
    parser.add_argument(
        "--genres-out",
        default="channels_by_genre.json",
        help="Output path for genre grouped JSON.",
    )
    parser.add_argument(
        "--schedules-out",
        default="channel_schedules.json",
        help="Output path for master schedule JSON.",
    )
    parser.add_argument(
        "--tatasky-playlist",
        default="https://www.youtube.com/@TataPlayOfficial/videos",
        help="Playlist/source URL for channel 100 TataSky schedule slots.",
    )
    args = parser.parse_args()

    pdf_path = Path(args.pdf).expanduser().resolve()
    channels_out = Path(args.channels_out).expanduser().resolve()
    genres_out = Path(args.genres_out).expanduser().resolve()
    schedules_out = Path(args.schedules_out).expanduser().resolve()
    schedule_ref_file = schedules_out.name

    rows = parse_channel_rows(pdf_path)
    channels_payload = build_channels_payload(rows, schedule_ref_file)
    schedules_payload = build_schedules_payload(
        channel_numbers=sorted(channels_payload.keys(), key=int),
        tatasky_playlist=args.tatasky_playlist,
    )
    genres_payload = build_genre_payload(channels_payload)

    channels_out.parent.mkdir(parents=True, exist_ok=True)
    genres_out.parent.mkdir(parents=True, exist_ok=True)
    schedules_out.parent.mkdir(parents=True, exist_ok=True)

    with channels_out.open("w", encoding="utf-8") as fh:
        json.dump(channels_payload, fh, indent=2, ensure_ascii=False)

    with schedules_out.open("w", encoding="utf-8") as fh:
        json.dump(schedules_payload, fh, indent=2, ensure_ascii=False)

    with genres_out.open("w", encoding="utf-8") as fh:
        json.dump(genres_payload, fh, indent=2, ensure_ascii=False)

    print(f"Imported {len(channels_payload)} channels from {pdf_path}")
    print(f"Wrote: {channels_out}")
    print(f"Wrote: {schedules_out}")
    print(f"Wrote: {genres_out}")


if __name__ == "__main__":
    main()
