"""data/tracks.csv에서 (valence, arousal) 값이 있는 곡을 DB의 tracks_v4 테이블에 올립니다.

    python v4/upload_tracks.py [--input 파일] [--yes]

- 접속 주소는 저장소 루트 .env의 DATABASE_URL입니다. 비밀번호는 출력하지 않습니다.
- --yes를 지정하지 않으면 DB에 접속하지 않고, 올릴 내용을 미리 보여 주기만 합니다.
- --yes를 지정하면 tracks_v4 테이블과 인덱스가 없을 때 만들고, 곡을 모두 올립니다. 이미 있는 곡은 새 값으로 고칩니다.
  테이블 생성과 적재는 하나의 트랜잭션이므로, 실패하면 아무것도 반영되지 않습니다.
- 이 스크립트가 다루는 테이블은 tracks_v4뿐입니다. albums_v4는 곡의 album_id가 가리키는 앨범이 있는지 읽기만 하며,
  다른 테이블은 읽지도, 바꾸지도 않습니다. 앨범이 먼저 올라가 있어야 하므로 upload_albums.py를 먼저 실행해 주십시오.
- valence와 arousal이 모두 비어 있는 곡은 올리지 않습니다. 둘 중 하나만 있거나 범위([-1, 1])를 벗어난 곡이 있으면 이상이므로 중단합니다.
- 올리기 전에 모든 곡의 값을 검사하며, 테이블의 제약을 어기는 곡이 하나라도 있으면 아무것도 올리지 않고 알려 줍니다.
  --yes일 때는 곡의 album_id가 albums_v4에 모두 있는지도 올리기 전에 확인합니다.
- 테이블 정의는 이 스크립트(CREATE_TABLE)가 기준입니다. schema.sql은 같은 정의를 보여 주기 위한 파일입니다.
"""

import argparse
import csv
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlparse

import db

DEFAULT_INPUT = Path(__file__).resolve().parent / "data" / "tracks.csv"
SPOTIFY_ID_PATTERN = re.compile(r"[0-9A-Za-z]{22}")
BATCH_SIZE = 5000

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS tracks_v4 (
    spotify_id text PRIMARY KEY CHECK (spotify_id ~ '^[0-9A-Za-z]{22}$'),
    reccobeats_id uuid, -- 오디오를 올려서 값을 얻은 곡은 없을 수 있음
    title text NOT NULL CHECK (btrim(title) <> ''),
    artist text NOT NULL CHECK (btrim(artist) <> ''), -- 참여 아티스트 이름을 ', '로 이은 표시용 문자열
    artist_spotify_ids text[] NOT NULL CHECK (cardinality(artist_spotify_ids) > 0), -- artist와 같은 순서
    duration_ms integer NOT NULL CHECK (duration_ms >= 0),
    play_count bigint NOT NULL DEFAULT 0 CHECK (play_count >= 0), -- Spotify 누적 재생 수. 값이 없는 곡은 0
    album_id text NOT NULL REFERENCES albums_v4 (spotify_id),
    album_thumb_url text, -- 목록 화면용 64픽셀 앨범 커버. 앨범 테이블을 조인하지 않으려고 곡에 둠
    valence double precision NOT NULL CHECK (valence BETWEEN -1 AND 1),
    arousal double precision NOT NULL CHECK (arousal BETWEEN -1 AND 1)
);
CREATE INDEX IF NOT EXISTS tracks_v4_artist_spotify_ids_idx ON tracks_v4 USING gin (artist_spotify_ids);
CREATE INDEX IF NOT EXISTS tracks_v4_album_id_idx ON tracks_v4 (album_id)
"""
COLUMNS = [
    "spotify_id", "reccobeats_id", "title", "artist", "artist_spotify_ids", "duration_ms",
    "play_count", "album_id", "album_thumb_url", "valence", "arousal",
]
UPSERT = (
    f"INSERT INTO tracks_v4 ({', '.join(COLUMNS)}) VALUES ({', '.join(['%s'] * len(COLUMNS))}) "
    f"ON CONFLICT (spotify_id) DO UPDATE SET {', '.join(f'{c} = EXCLUDED.{c}' for c in COLUMNS[1:])}"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="(V, A) 값이 있는 곡을 DB의 tracks_v4 테이블에 올립니다.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="곡 CSV 파일")
    parser.add_argument("--yes", action="store_true", help="DB에 실제로 올립니다. 없으면 미리 보기만 합니다.")
    args = parser.parse_args()

    if not args.input.is_file():
        parser.error(f"입력 파일이 없습니다: {args.input}")
    return args


def parse_id_array(value: str) -> list[str]:
    """'{id1,id2}' 형식의 문자열을 ID 목록으로 읽습니다."""
    inner = value.strip()
    if not (inner.startswith("{") and inner.endswith("}")):
        raise ValueError(f"배열 형식이 올바르지 않습니다: {value!r}")
    return [item for item in inner[1:-1].split(",") if item]


def to_row(record: dict) -> tuple:
    """CSV의 한 줄을 tracks_v4의 한 행으로 바꿉니다. COLUMNS와 같은 순서입니다. 형식이 틀리면 ValueError입니다."""
    reccobeats_id = record["reccobeats_id"].strip()
    return (
        record["spotify_id"],
        str(uuid.UUID(reccobeats_id)) if reccobeats_id else None,
        record["title"],
        record["artist"],
        parse_id_array(record["artist_spotify_ids"]),
        int(record["duration_ms"]),
        int(record["play_count"] or 0),
        record["album_id"],
        record["album_thumb_url"] or None,
        float(record["valence"]),
        float(record["arousal"]),
    )


def find_problem(row: tuple) -> str | None:
    """테이블의 제약을 어기는 값이 있으면 이유를 돌려줍니다."""
    spotify_id, _, title, artist, artist_ids, duration_ms, play_count, album_id, _, valence, arousal = row
    if not SPOTIFY_ID_PATTERN.fullmatch(spotify_id):
        return f"곡 ID 형식이 올바르지 않습니다: {spotify_id!r}"
    if not title.strip():
        return "곡 제목이 비어 있습니다."
    if not artist.strip() or not artist_ids:
        return "곡 아티스트가 비어 있습니다."
    if duration_ms < 0 or play_count < 0:
        return "곡 길이나 재생 수가 음수입니다."
    if not SPOTIFY_ID_PATTERN.fullmatch(album_id):
        return f"앨범 ID 형식이 올바르지 않습니다: {album_id!r}"
    if not (-1 <= valence <= 1 and -1 <= arousal <= 1):
        return f"valence, arousal이 [-1, 1] 범위를 벗어났습니다: ({valence}, {arousal})"
    return None


def read_rows(path: Path) -> list[tuple]:
    """곡 CSV에서 (V, A) 값이 있는 곡을 읽어 행으로 바꿉니다. 같은 곡이 여러 번 나오면 마지막 줄을 씁니다."""
    rows: dict[str, tuple] = {}
    total = no_value = duplicates = 0
    problems: list[str] = []
    with path.open(encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        missing = set(COLUMNS) - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"{path.name}에 필요한 열이 없습니다: {', '.join(sorted(missing))}")
        for number, record in enumerate(reader, start=2):  # 1번째 줄은 헤더
            total += 1
            has_valence, has_arousal = bool(record["valence"].strip()), bool(record["arousal"].strip())
            if not has_valence and not has_arousal:
                no_value += 1
                continue
            if has_valence != has_arousal:
                problems.append(f"{number}번째 줄({record['spotify_id']}): valence와 arousal 중 하나만 있습니다.")
                continue
            try:
                row = to_row(record)
            except (ValueError, TypeError, KeyError) as error:
                problems.append(f"{number}번째 줄({record['spotify_id']}): 값을 읽을 수 없습니다. ({error})")
                continue
            problem = find_problem(row)
            if problem:
                problems.append(f"{number}번째 줄({row[0]}): {problem}")
                continue
            duplicates += row[0] in rows
            rows[row[0]] = row

    print(f"{path.name}: 전체 {total:,}곡 중 (V, A)가 없는 {no_value:,}곡을 빼고 {len(rows):,}곡 (중복 곡 {duplicates:,}곡)")
    if problems:
        print(f"올릴 수 없는 곡이 {len(problems):,}곡 있어 중단합니다. 앞의 10개:")
        for problem in problems[:10]:
            print(f"  {problem}")
        raise SystemExit(1)
    return list(rows.values())


def describe_target() -> str:
    """접속 대상을 비밀번호 없이 설명합니다."""
    db.load_env_file()
    url = os.environ.get("DATABASE_URL")
    if not url:
        return "DATABASE_URL이 설정되어 있지 않습니다."
    parsed = urlparse(url)
    return f"호스트 {parsed.hostname}, 포트 {parsed.port or '기본'}, DB {parsed.path.lstrip('/')}"


def find_missing_albums(cursor, rows: list[tuple]) -> list[str]:
    """곡의 album_id 중 albums_v4에 없는 앨범 ID를 돌려줍니다."""
    album_ids = sorted({row[COLUMNS.index("album_id")] for row in rows})
    cursor.execute("SELECT spotify_id FROM albums_v4 WHERE spotify_id = ANY(%s)", (album_ids,))
    existing = {found[0] for found in cursor.fetchall()}
    return [album_id for album_id in album_ids if album_id not in existing]


def main() -> None:
    args = parse_args()
    rows = read_rows(args.input)
    print(f"reccobeats_id 없는 곡 {sum(row[1] is None for row in rows):,}곡, 앨범 {len({row[7] for row in rows}):,}개에 속함")
    print(f"접속 대상: {describe_target()}")

    if not args.yes:
        print("\n미리 보기입니다. 앞의 3개 행:")
        for row in rows[:3]:
            print(f"  {row}")
        print("\nDB에는 접속하지 않았습니다. 실제로 올리려면 --yes를 붙여 다시 실행하십시오.")
        return

    with db.connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('albums_v4') IS NOT NULL")
            if not cursor.fetchone()[0]:
                raise SystemExit("albums_v4 테이블이 없습니다. upload_albums.py를 먼저 실행해 주십시오.")
            missing = find_missing_albums(cursor, rows)
            if missing:
                raise SystemExit(
                    f"곡이 가리키는 앨범 {len(missing):,}개가 albums_v4에 없어 중단합니다. 앞의 5개: {', '.join(missing[:5])}\n"
                    "upload_albums.py로 앨범을 먼저 올려 주십시오."
                )

            cursor.execute(CREATE_TABLE)
            cursor.execute("SELECT count(*) FROM tracks_v4")
            before = cursor.fetchone()[0]
            for start in range(0, len(rows), BATCH_SIZE):
                cursor.executemany(UPSERT, rows[start:start + BATCH_SIZE])
            cursor.execute("SELECT count(*) FROM tracks_v4")
            after = cursor.fetchone()[0]
    print(f"\n올렸습니다. tracks_v4: {before:,}행 -> {after:,}행 (새로 들어간 곡 {after - before:,}곡, 나머지 {len(rows) - (after - before):,}곡은 이미 있어 값을 고쳤습니다.)")


if __name__ == "__main__":
    main()
