"""앨범 단위의 data/albums.jsonl을 곡 단위의 data/tracks.csv로 펼칩니다.

    python v4/flatten_tracks.py [--input 파일] [--output 파일] [--force]

- 곡을 걸러내지 않습니다. 앨범의 수록곡을 모두 한 줄씩 옮기며, 중복 제거와 inst/MR 제외는 다음 단계에서 합니다.
- 곡마다 한 줄을 다음 열로 기록합니다. 열 이름과 순서는 DB의 tracks_v4 테이블과 같습니다.
  spotify_id, title, artist, artist_spotify_ids, duration_ms, play_count, album_id, album_thumb_url,
  reccobeats_id, isrc, valence, arousal
- artist는 곡의 아티스트 이름을 ', '로 이은 표시용 문자열이고, artist_spotify_ids는 같은 순서의 Spotify ID를
  {id1,id2} 형식(PostgreSQL 배열)으로 적은 것입니다. 수록곡의 아티스트는 id가 비어 있으므로 uri(spotify:artist:ID)에서 ID를 읽습니다.
- album_thumb_url은 앨범 커버 중 가로 64픽셀 이미지의 URL입니다. 목록에서 쓰는 작은 이미지이며,
  300·640픽셀 이미지는 앨범 테이블에서 돌려줍니다. 64픽셀 이미지가 없으면 빈칸입니다.
- play_count가 없는 곡은 0으로 적습니다.
- reccobeats_id, isrc, valence, arousal은 이 단계에서 비워 둡니다. 이후 단계가 같은 파일의 빈칸을 채웁니다.
- 앨범 안의 곡 순서(트랙 번호, 디스크)는 곡의 속성이 아니라 앨범의 정보이므로 담지 않습니다. albums.jsonl의 수록곡 배열 순서가 그 순서입니다.
- 실행할 때마다 출력 파일을 처음부터 새로 만듭니다. 같은 입력이면 항상 같은 결과가 나옵니다.
- 출력 파일에 이미 채워진 곡(reccobeats_id, isrc, valence, arousal 중 값이 있는 곡)이 있으면, 다시 만들 때 그 값이
  모두 사라지므로 아무것도 하지 않고 중단합니다. 정말 새로 만들려면 --force를 지정합니다.
"""

import argparse
import csv
import json
import os
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_INPUT = DATA_DIR / "albums.jsonl"
DEFAULT_OUTPUT = DATA_DIR / "tracks.csv"
THUMB_WIDTH = 64
COLUMNS = [
    "spotify_id", "title", "artist", "artist_spotify_ids", "duration_ms", "play_count",
    "album_id", "album_thumb_url", "reccobeats_id", "isrc", "valence", "arousal",
]
FILLED_COLUMNS = ["reccobeats_id", "isrc", "valence", "arousal"]  # 이후 단계가 채우는 열


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="앨범 단위 JSONL을 곡 단위 CSV로 펼칩니다.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="앨범 JSONL 파일")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="결과 CSV 파일")
    parser.add_argument("--force", action="store_true", help="채워진 값이 있어도 출력 파일을 새로 만듭니다.")
    args = parser.parse_args()

    if not args.input.is_file():
        parser.error(f"입력 파일이 없습니다: {args.input}")
    return args


def count_filled_rows(path: Path) -> int:
    """출력 파일에서 이후 단계가 채운 값이 하나라도 있는 곡의 수를 돌려줍니다. 파일이 없거나 읽을 수 없으면 0입니다."""
    if not path.is_file():
        return 0
    try:
        with path.open(encoding="utf-8", newline="") as file:
            return sum(any(row.get(column) for column in FILLED_COLUMNS) for row in csv.DictReader(file))
    except (OSError, UnicodeDecodeError, csv.Error):
        return 0


def find_thumb_url(images: list[dict]) -> str | None:
    """앨범 커버 중 가로 THUMB_WIDTH 픽셀인 이미지의 URL을 돌려줍니다. 없으면 None입니다."""
    for image in images or []:
        if image.get("width") == THUMB_WIDTH:
            return image.get("url")
    return None


def artist_id(artist: dict) -> str:
    """수록곡 아티스트의 ID를 돌려줍니다. id가 비어 있으면 uri(spotify:artist:ID)의 마지막 부분을 씁니다."""
    return artist.get("id") or artist["uri"].rsplit(":", 1)[-1]


def to_row(track: dict, album: dict, thumb_url: str | None) -> tuple[dict, bool]:
    """수록곡 하나를 CSV의 한 행으로 바꿉니다. (행, play_count가 없었는지)를 돌려줍니다."""
    play_count = track.get("play_count")
    artists = track["artists"]
    row = {
        "spotify_id": track["id"],
        "title": track["name"],
        "artist": ", ".join(artist["name"] for artist in artists),
        "artist_spotify_ids": "{" + ",".join(artist_id(artist) for artist in artists) + "}",
        "duration_ms": track["duration_ms"],
        "play_count": 0 if play_count is None else play_count,
        "album_id": album["id"],
        "album_thumb_url": thumb_url,
    }
    row.update({column: "" for column in FILLED_COLUMNS})
    return row, play_count is None


def main() -> None:
    args = parse_args()

    filled = count_filled_rows(args.output)
    if filled and not args.force:
        raise SystemExit(
            f"{args.output}에 채워진 곡이 {filled:,}곡 있습니다. 다시 만들면 이 값이 모두 사라지므로 중단합니다.\n"
            "정말 새로 만들려면 --force를 붙여 다시 실행하십시오."
        )
    if filled:
        print(f"--force에 따라 채워진 곡 {filled:,}곡의 값을 버리고 새로 만듭니다.")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    temp = args.output.with_name(args.output.name + ".tmp")

    stats = {"albums": 0, "unreadable": 0, "tracks": 0, "duplicate_ids": 0, "no_thumb": 0, "no_play_count": 0}
    seen: set[str] = set()

    # 임시 파일에 먼저 쓰고 끝나면 교체하므로, 도중에 멈춰도 기존 결과 파일이 깨지지 않습니다.
    with (
        args.input.open(encoding="utf-8") as source,
        temp.open("w", encoding="utf-8", newline="") as output,
    ):
        writer = csv.DictWriter(output, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        for line in source:
            try:
                album = json.loads(line)["album"]
                thumb_url = find_thumb_url(album["images"])
                rows = [to_row(track, album, thumb_url) for track in album["tracks"]]
            except (json.JSONDecodeError, KeyError, TypeError):
                stats["unreadable"] += 1
                continue

            stats["albums"] += 1
            for row, no_play_count in rows:
                if row["spotify_id"] in seen:
                    stats["duplicate_ids"] += 1
                    continue
                seen.add(row["spotify_id"])
                writer.writerow(row)
                stats["tracks"] += 1
                stats["no_thumb"] += row["album_thumb_url"] is None
                stats["no_play_count"] += no_play_count

    os.replace(temp, args.output)

    print(f"{args.input.name}: 앨범 {stats['albums']:,}개를 읽었습니다. (읽을 수 없는 줄 {stats['unreadable']:,}개)")
    print(f"곡 {stats['tracks']:,}곡을 기록했습니다. (곡 ID가 겹쳐 건너뛴 곡 {stats['duplicate_ids']:,}곡)")
    print(f"64픽셀 커버가 없는 곡 {stats['no_thumb']:,}곡, play_count가 없어 0으로 적은 곡 {stats['no_play_count']:,}곡")
    print(f"결과 파일: {args.output}")


if __name__ == "__main__":
    main()
