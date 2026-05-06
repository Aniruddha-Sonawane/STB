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


def build_channels_payload(rows: list[dict], default_source: str) -> dict:
    payload: dict = {}
    for row in rows:
        key = row["epg_no"]
        payload[key] = {
            "name": row["name"],
            "epg_no": row["epg_no"],
            "genre": row["genre"],
            "quality": row["quality"],
            "fta_pay": row["fta_pay"],
            "source": default_source,
        }
    return payload


def build_genre_payload(rows: list[dict]) -> dict:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["genre"]].append(
            {
                "epg_no": row["epg_no"],
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
        "--default-source",
        default="https://www.youtube.com/@TataPlayOfficial/videos",
        help="Fallback source URL to attach to each channel.",
    )
    args = parser.parse_args()

    pdf_path = Path(args.pdf).expanduser().resolve()
    channels_out = Path(args.channels_out).expanduser().resolve()
    genres_out = Path(args.genres_out).expanduser().resolve()

    rows = parse_channel_rows(pdf_path)
    channels_payload = build_channels_payload(rows, args.default_source)
    genres_payload = build_genre_payload(rows)

    channels_out.parent.mkdir(parents=True, exist_ok=True)
    genres_out.parent.mkdir(parents=True, exist_ok=True)

    with channels_out.open("w", encoding="utf-8") as fh:
        json.dump(channels_payload, fh, indent=2, ensure_ascii=False)

    with genres_out.open("w", encoding="utf-8") as fh:
        json.dump(genres_payload, fh, indent=2, ensure_ascii=False)

    print(f"Imported {len(channels_payload)} channels from {pdf_path}")
    print(f"Wrote: {channels_out}")
    print(f"Wrote: {genres_out}")


if __name__ == "__main__":
    main()

