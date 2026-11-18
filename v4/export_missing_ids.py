"""ReccoBeats에 값이 없는 곡의 Spotify ID를 한 줄에 하나씩 data/missing_ids.txt에 적습니다.

    python v4/export_missing_ids.py [--tracks 파일] [--reccobeats 파일] [--output 파일]

- data/tracks.jsonl의 곡 중 data/reccobeats.jsonl에 기록되지 않은 곡의 ID를 tracks.jsonl의 순서대로 적습니다.
- 이 곡들은 ISRC와 (valence, arousal)을 ReccoBeats에서 얻지 못한 곡입니다. 목록은 ISRC 조회 도구에 직접 올리는 용도입니다.
- 파일만 읽고 쓰며 네트워크에 접속하지 않습니다. 입력 파일은 바꾸지 않습니다.
- 실행할 때마다 출력 파일을 처음부터 새로 만듭니다. 같은 입력이면 항상 같은 결과가 나옵니다.
"""

import argparse
import json
import os
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_TRACKS = DATA_DIR / "tracks.jsonl"
DEFAULT_RECCOBEATS = DATA_DIR / "reccobeats.jsonl"
DEFAULT_OUTPUT = DATA_DIR / "missing_ids.txt"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ReccoBeats에 값이 없는 곡의 Spotify ID 목록을 만듭니다.")
    parser.add_argument("--tracks", type=Path, default=DEFAULT_TRACKS, help="곡 JSONL 파일")
    parser.add_argument("--reccobeats", type=Path, default=DEFAULT_RECCOBEATS, help="ReccoBeats 결과 JSONL 파일")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="결과 텍스트 파일")
    args = parser.parse_args()

    for path in (args.tracks, args.reccobeats):
        if not path.is_file():
            parser.error(f"입력 파일이 없습니다: {path}")
    return args


def read_ids(path: Path, key: str) -> tuple[list[str], int]:
    """JSONL에서 key 값을 처음 나온 순서대로, 중복 없이 읽습니다. (ID 목록, 읽을 수 없는 줄 수)"""
    ids: dict[str, None] = {}
    unreadable = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            ids.setdefault(json.loads(line)[key])
        except (json.JSONDecodeError, KeyError, TypeError):
            unreadable += 1
    return list(ids), unreadable


def main() -> None:
    args = parse_args()
    track_ids, unreadable_tracks = read_ids(args.tracks, "id")
    found_ids, unreadable_found = read_ids(args.reccobeats, "spotify_id")
    found = set(found_ids)
    missing = [track_id for track_id in track_ids if track_id not in found]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    temp = args.output.with_name(args.output.name + ".tmp")
    # 임시 파일에 먼저 쓰고 끝나면 교체하므로, 도중에 멈춰도 기존 결과 파일이 깨지지 않습니다.
    temp.write_text("".join(f"{track_id}\n" for track_id in missing), encoding="utf-8")
    os.replace(temp, args.output)

    print(f"{args.tracks.name}: 곡 {len(track_ids):,}곡 (읽을 수 없는 줄 {unreadable_tracks:,}개)")
    print(f"{args.reccobeats.name}: 곡 {len(found):,}곡 (읽을 수 없는 줄 {unreadable_found:,}개)")
    print(f"ReccoBeats에 없는 곡 {len(missing):,}곡의 ID를 기록했습니다.")
    print(f"결과 파일: {args.output}")


if __name__ == "__main__":
    main()
