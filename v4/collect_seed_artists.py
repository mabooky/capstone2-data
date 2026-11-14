"""한국 차트 플레이리스트의 아티스트를 SpotifyScraper로 수집해, seed-artists.csv에 없으면 추가합니다.

    python v4/collect_seed_artists.py [--locale ko] [--limit 개수] [--yes]

- 대상은 CHARTS에 적은 Spotify 공식 플레이리스트(주간 차트, Hot Hits Korea, 바이럴 히트, K-Pop ON! 등)입니다.
  각 플레이리스트의 모든 곡에 참여한 아티스트를 모으며, 같은 아티스트는 한 번만 셉니다.
- seed-artists.csv의 아티스트 Spotify ID와 비교해 없는 아티스트만 새로 추가할 대상으로 봅니다.
- --limit: 이번 실행에서 추가할 최대 아티스트 수입니다. 차트에서 먼저 발견한 순서대로 선택합니다.
- --yes를 지정하지 않으면 CSV를 바꾸지 않고 추가할 아티스트를 미리 보여 주기만 합니다.
- --yes를 지정하면 선택한 아티스트를 CSV의 끝에 `아티스트,아티스트 Spotify ID` 형식으로 덧붙입니다. 기존 행은 건드리지 않습니다.
- 차트는 주간 단위로 바뀌므로 실행할 때마다 결과가 다를 수 있습니다.
"""

import argparse
import asyncio
import csv
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from spotify_scraper import AsyncSpotifyClient
from spotify_scraper.errors import NotFoundError, RateLimitedError
from tqdm import tqdm

SEED_CSV = Path(__file__).resolve().parent / "seed-artists.csv"
CSV_HEADER = ["아티스트", "아티스트 Spotify ID"]
SPOTIFY_ID_PATTERN = re.compile(r"[0-9A-Za-z]{22}")
RATE_LIMIT_PAUSE_SEC = 60  # 서버가 대기 시간을 알려 주지 않을 때 쉬는 시간
MAX_RATE_LIMIT_RETRIES = 3

# 이름, 플레이리스트 ID. Spotify가 운영하는 공식 플레이리스트입니다.
CHARTS = [
    ("Top Songs - South Korea", "37i9dQZEVXbJZGli0rRP3r"),
    ("Hot Hits Korea Top 100", "37i9dQZF1DWT9uTRZAYj0c"),
    ("바이럴 히트 코리아", "37i9dQZF1DX8R890mYSheX"),
    ("K-Pop ON! (온)", "37i9dQZF1DX9tPFwDMOaN1"),
]


@dataclass
class Artist:
    spotify_id: str
    name: str
    charts: list[str] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="한국 차트의 아티스트를 seed-artists.csv에 추가합니다.")
    parser.add_argument("--locale", default="ko", help="표기 언어(BCP-47). 기본값은 ko")
    parser.add_argument("--limit", type=int, help="이번 실행에서 추가할 최대 아티스트 수")
    parser.add_argument("--yes", action="store_true", help="CSV에 실제로 추가합니다. 지정하지 않으면 미리보기만 합니다.")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit은 1 이상이어야 합니다.")
    return args


def read_existing_ids(path: Path) -> set[str]:
    """CSV에 이미 있는 아티스트의 Spotify ID를 읽습니다. 첫 줄은 머리글이므로 건너뜁니다."""
    with path.open(encoding="utf-8", newline="") as file:
        rows = list(csv.reader(file))
    return {row[1].strip() for row in rows[1:] if len(row) >= 2 and row[1].strip()}


def artist_id(ref) -> str:
    """아티스트 참조에서 Spotify ID를 얻습니다. id가 비어 있으면 spotify:artist:ID 형식의 uri에서 읽습니다."""
    candidate = ref.id or ref.uri.rsplit(":", 1)[-1]
    return candidate if SPOTIFY_ID_PATTERN.fullmatch(candidate) else ""


async def fetch_playlist(client: AsyncSpotifyClient, playlist_id: str, locale: str):
    """플레이리스트를 조회합니다. 요청이 몰려 제한되면 쉬었다가 몇 번 다시 시도합니다."""
    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        try:
            return await client.get_playlist(playlist_id, max_tracks=None, locale=locale)
        except RateLimitedError as error:
            if attempt == MAX_RATE_LIMIT_RETRIES:
                raise
            wait = getattr(error, "retry_after", None) or RATE_LIMIT_PAUSE_SEC
            tqdm.write(f"요청이 제한되어 {wait:g}초 쉬었다가 다시 시도합니다.")
            await asyncio.sleep(wait)


async def collect(args: argparse.Namespace) -> dict[str, Artist]:
    """모든 차트에서 아티스트를 모읍니다. 발견한 순서를 유지합니다."""
    artists: dict[str, Artist] = {}
    failed: list[str] = []
    bar = tqdm(total=len(CHARTS), unit="차트", dynamic_ncols=True)
    async with AsyncSpotifyClient() as client:
        for name, playlist_id in CHARTS:
            bar.set_description(name)
            try:
                playlist = await fetch_playlist(client, playlist_id, args.locale)
            except (NotFoundError, RateLimitedError) as error:
                failed.append(name)
                tqdm.write(f"{name}: 조회하지 못했습니다. ({type(error).__name__})")
            else:
                for item in playlist.tracks:
                    for ref in item.track.artists:
                        spotify_id = artist_id(ref)
                        if not spotify_id:
                            continue
                        artist = artists.setdefault(spotify_id, Artist(spotify_id, ref.name))
                        if name not in artist.charts:
                            artist.charts.append(name)
                tqdm.write(f"{name}: 곡 {len(playlist.tracks)}개")
            bar.update(1)
            bar.set_postfix(아티스트=len(artists), 실패=len(failed))
    bar.close()
    if len(failed) == len(CHARTS):
        raise SystemExit("모든 차트를 조회하지 못해 중단합니다.")
    return artists


def append_to_csv(path: Path, artists: list[Artist]) -> None:
    """CSV의 끝에 아티스트를 덧붙입니다. 마지막 줄이 개행으로 끝나지 않으면 먼저 개행을 넣습니다."""
    with path.open("rb") as file:
        file.seek(0, 2)
        needs_newline = file.tell() > 0 and (file.seek(-1, 2), file.read(1))[1] != b"\n"
    with path.open("a", encoding="utf-8", newline="") as file:
        if needs_newline:
            file.write("\n")
        writer = csv.writer(file, lineterminator="\n")
        for artist in artists:
            writer.writerow([artist.name, artist.spotify_id])


def main() -> None:
    args = parse_args()
    existing = read_existing_ids(SEED_CSV)
    print(f"seed-artists.csv: 아티스트 {len(existing):,}명")

    started = time.monotonic()
    try:
        collected = asyncio.run(collect(args))
    except KeyboardInterrupt:
        raise SystemExit("중단했습니다. CSV는 변경하지 않았습니다.")

    new_artists = [artist for artist in collected.values() if artist.spotify_id not in existing]
    selected = new_artists if args.limit is None else new_artists[: args.limit]
    print(
        f"차트의 아티스트 {len(collected):,}명 중 CSV에 이미 있는 {len(collected) - len(new_artists):,}명을 제외하고 "
        f"새 아티스트 {len(new_artists):,}명을 찾았습니다. ({time.monotonic() - started:.1f}초)"
    )

    for artist in selected:
        print(f"  {artist.name} ({artist.spotify_id}) | {', '.join(artist.charts)}")
    if len(selected) < len(new_artists):
        print(f"  --limit에 따라 {len(selected):,}명만 선택했고 {len(new_artists) - len(selected):,}명은 제외했습니다.")

    if not selected:
        print("추가할 아티스트가 없습니다.")
    elif args.yes:
        append_to_csv(SEED_CSV, selected)
        print(f"{SEED_CSV.name}에 {len(selected):,}명을 추가했습니다.")
    else:
        print(f"미리보기입니다. --yes를 지정하면 {SEED_CSV.name}에 {len(selected):,}명을 추가합니다.")


if __name__ == "__main__":
    main()
