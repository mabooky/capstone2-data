"""앨범 단위의 data/albums.jsonl을 곡 단위의 data/tracks.jsonl로 펼칩니다.

    python v4/flatten_tracks.py [--input 파일] [--output 파일]

- 곡을 걸러내지 않습니다. 앨범의 수록곡을 모두 한 줄씩 옮기며, 중복 제거와 inst/MR 제외는 다음 단계에서 합니다.
- 곡마다 한 줄을 다음 형식으로 기록합니다.
  {"id", "title", "artists": [{"id", "name"}], "duration_ms", "play_count", "album_id", "album_thumb_url"}
  id와 artists[].id, album_id는 모두 Spotify ID이며, albums.jsonl과 같은 이름을 씁니다.
- 앨범 안의 곡 순서(트랙 번호, 디스크)는 곡의 속성이 아니라 앨범의 정보이므로 담지 않습니다. albums.jsonl의 수록곡 배열 순서가 그 순서입니다.
- 수록곡의 아티스트는 id가 비어 있으므로 uri(spotify:artist:ID)에서 ID를 읽습니다. 순서는 응답 그대로입니다.
- album_thumb_url은 앨범 커버 중 가로 64픽셀 이미지의 URL입니다. 목록에서 쓰는 작은 이미지이며,
  300·640픽셀 이미지는 앨범 테이블에서 돌려줍니다. 64픽셀 이미지가 없으면 null입니다.
- 실행할 때마다 출력 파일을 처음부터 새로 만듭니다. 같은 입력이면 항상 같은 결과가 나옵니다.
"""

import argparse
import json
import os
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_INPUT = DATA_DIR / "albums.jsonl"
DEFAULT_OUTPUT = DATA_DIR / "tracks.jsonl"
THUMB_WIDTH = 64


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="앨범 단위 JSONL을 곡 단위 JSONL로 펼칩니다.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="앨범 JSONL 파일")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="결과 JSONL 파일")
    args = parser.parse_args()

    if not args.input.is_file():
        parser.error(f"입력 파일이 없습니다: {args.input}")
    return args


def find_thumb_url(images: list[dict]) -> str | None:
    """앨범 커버 중 가로 THUMB_WIDTH 픽셀인 이미지의 URL을 돌려줍니다. 없으면 None입니다."""
    for image in images or []:
        if image.get("width") == THUMB_WIDTH:
            return image.get("url")
    return None


def artist_id(artist: dict) -> str:
    """수록곡 아티스트의 ID를 돌려줍니다. id가 비어 있으면 uri(spotify:artist:ID)의 마지막 부분을 씁니다."""
    return artist.get("id") or artist["uri"].rsplit(":", 1)[-1]


def to_track(track: dict, album: dict, thumb_url: str | None) -> dict:
    return {
        "id": track["id"],
        "title": track["name"],
        "artists": [{"id": artist_id(artist), "name": artist["name"]} for artist in track["artists"]],
        "duration_ms": track["duration_ms"],
        "play_count": track.get("play_count"),
        "album_id": album["id"],
        "album_thumb_url": thumb_url,
    }


def main() -> None:
    args = parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temp = args.output.with_name(args.output.name + ".tmp")

    stats = {"albums": 0, "unreadable": 0, "tracks": 0, "duplicate_ids": 0, "no_thumb": 0, "no_play_count": 0}
    seen: set[str] = set()

    # 임시 파일에 먼저 쓰고 끝나면 교체하므로, 도중에 멈춰도 기존 결과 파일이 깨지지 않습니다.
    with args.input.open(encoding="utf-8") as source, temp.open("w", encoding="utf-8") as output:
        for line in source:
            try:
                album = json.loads(line)["album"]
                thumb_url = find_thumb_url(album["images"])
                tracks = [to_track(track, album, thumb_url) for track in album["tracks"]]
            except (json.JSONDecodeError, KeyError, TypeError):
                stats["unreadable"] += 1
                continue

            stats["albums"] += 1
            for track in tracks:
                if track["id"] in seen:
                    stats["duplicate_ids"] += 1
                    continue
                seen.add(track["id"])
                output.write(json.dumps(track, ensure_ascii=False) + "\n")
                stats["tracks"] += 1
                stats["no_thumb"] += track["album_thumb_url"] is None
                stats["no_play_count"] += track["play_count"] is None

    os.replace(temp, args.output)

    print(f"{args.input.name}: 앨범 {stats['albums']:,}개를 읽었습니다. (읽을 수 없는 줄 {stats['unreadable']:,}개)")
    print(f"곡 {stats['tracks']:,}곡을 기록했습니다. (곡 ID가 겹쳐 건너뛴 곡 {stats['duplicate_ids']:,}곡)")
    print(f"64픽셀 커버가 없는 곡 {stats['no_thumb']:,}곡, play_count가 없는 곡 {stats['no_play_count']:,}곡")
    print(f"결과 파일: {args.output}")


if __name__ == "__main__":
    main()
