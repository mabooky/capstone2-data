"""data/albums.jsonl의 앨범을 DB의 albums_v4 테이블에 올립니다.

    python v4/upload_albums.py [--input 파일] [--yes]

- 접속 주소는 저장소 루트 .env의 DATABASE_URL입니다. 비밀번호는 출력하지 않습니다.
- --yes를 지정하지 않으면 DB에 접속하지 않고, 올릴 내용을 미리 보여 주기만 합니다.
- --yes를 지정하면 albums_v4 테이블이 없을 때 만들고, 앨범을 모두 올립니다. 이미 있는 앨범은 새 값으로 고칩니다.
  테이블 생성과 적재는 하나의 트랜잭션이므로, 실패하면 아무것도 반영되지 않습니다.
- 이 스크립트가 다루는 테이블은 albums_v4뿐입니다. 다른 테이블은 읽지도, 바꾸지도 않습니다.
- 앨범 하나에서 albums_v4의 한 행을 만듭니다. 앨범 아티스트의 이름은 ', '로 이어 artist에, ID는 artist_spotify_ids에 담고,
  발매일은 날짜만 남기며, 커버는 가로 640, 300, 64픽셀 이미지의 URL을 각각 담습니다. 해당 크기의 이미지가 없으면 NULL입니다.
- 올리기 전에 모든 앨범의 값을 검사하며, 테이블의 제약을 어기는 앨범이 하나라도 있으면 아무것도 올리지 않고 알려 줍니다.
- 테이블 정의는 이 스크립트(CREATE_TABLE)가 기준입니다. schema.sql은 같은 정의를 보여 주기 위한 파일입니다.
"""

import argparse
import json
import os
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import db

DEFAULT_INPUT = Path(__file__).resolve().parent / "data" / "albums.jsonl"
SPOTIFY_ID_PATTERN = re.compile(r"[0-9A-Za-z]{22}")
ALBUM_TYPES = {"album", "single", "ep", "compilation"}
COVER_WIDTHS = {"cover_url_640": 640, "cover_url_300": 300, "cover_url_64": 64}

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS albums_v4 (
    spotify_id text PRIMARY KEY CHECK (spotify_id ~ '^[0-9A-Za-z]{22}$'),
    name text NOT NULL CHECK (btrim(name) <> ''),
    type text NOT NULL CHECK (type IN ('album', 'single', 'ep', 'compilation')),
    artist text NOT NULL CHECK (btrim(artist) <> ''), -- 앨범 아티스트 이름을 ', '로 이은 표시용 문자열
    artist_spotify_ids text[] NOT NULL CHECK (cardinality(artist_spotify_ids) > 0), -- artist와 같은 순서
    release_date date,
    cover_url_640 text,
    cover_url_300 text,
    cover_url_64 text
)
"""
COLUMNS = ["spotify_id", "name", "type", "artist", "artist_spotify_ids", "release_date", *COVER_WIDTHS]
UPSERT = (
    f"INSERT INTO albums_v4 ({', '.join(COLUMNS)}) VALUES ({', '.join(['%s'] * len(COLUMNS))}) "
    f"ON CONFLICT (spotify_id) DO UPDATE SET {', '.join(f'{c} = EXCLUDED.{c}' for c in COLUMNS[1:])}"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="앨범을 DB의 albums_v4 테이블에 올립니다.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="앨범 JSONL 파일")
    parser.add_argument("--yes", action="store_true", help="DB에 실제로 올립니다. 없으면 미리 보기만 합니다.")
    args = parser.parse_args()

    if not args.input.is_file():
        parser.error(f"입력 파일이 없습니다: {args.input}")
    return args


def artist_id(artist: dict) -> str:
    """앨범 아티스트의 ID입니다. id가 비어 있으면 uri(spotify:artist:ID)의 마지막 부분을 씁니다."""
    return artist.get("id") or artist["uri"].rsplit(":", 1)[-1]


def parse_release_date(value: str | None) -> date | None:
    """'2026-04-03T00:00:00+00:00' 같은 값에서 날짜만 읽습니다. 읽을 수 없으면 None입니다."""
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def to_row(album: dict) -> tuple:
    """앨범 응답 하나를 albums_v4의 한 행으로 바꿉니다. COLUMNS와 같은 순서입니다."""
    artists = album["artists"]
    covers = {image.get("width"): image.get("url") for image in album.get("images") or []}
    return (
        album["id"],
        album["name"],
        album["album_type"],
        ", ".join(artist["name"] for artist in artists),
        [artist_id(artist) for artist in artists],
        parse_release_date(album.get("release_date")),
        *(covers.get(width) for width in COVER_WIDTHS.values()),
    )


def find_problem(row: tuple) -> str | None:
    """테이블의 제약을 어기는 값이 있으면 이유를 돌려줍니다."""
    spotify_id, name, album_type, artist, artist_ids = row[:5]
    if not SPOTIFY_ID_PATTERN.fullmatch(spotify_id):
        return f"앨범 ID 형식이 올바르지 않습니다: {spotify_id!r}"
    if not name.strip():
        return "앨범 이름이 비어 있습니다."
    if album_type not in ALBUM_TYPES:
        return f"알 수 없는 앨범 유형입니다: {album_type!r}"
    if not artist.strip() or not artist_ids:
        return "앨범 아티스트가 비어 있습니다."
    return None


def read_rows(path: Path) -> list[tuple]:
    """앨범 JSONL을 읽어 행으로 바꿉니다. 같은 앨범이 여러 번 나오면 마지막 줄을 씁니다."""
    rows: dict[str, tuple] = {}
    unreadable = duplicates = 0
    problems: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            row = to_row(json.loads(line)["album"])
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
            unreadable += 1
            continue
        problem = find_problem(row)
        if problem:
            problems.append(f"{number}번째 줄({row[0]}): {problem}")
            continue
        duplicates += row[0] in rows
        rows[row[0]] = row

    print(f"{path.name}: 앨범 {len(rows):,}개 (읽을 수 없는 줄 {unreadable:,}개, 중복 앨범 {duplicates:,}개)")
    if problems:
        print(f"올릴 수 없는 앨범이 {len(problems):,}개 있어 중단합니다. 앞의 10개:")
        for problem in problems[:10]:
            print(f"  {problem}")
        raise SystemExit(1)
    if unreadable:
        raise SystemExit("읽을 수 없는 줄이 있어 중단합니다. 입력 파일을 확인해 주십시오.")
    return list(rows.values())


def describe_target() -> str:
    """접속 대상을 비밀번호 없이 설명합니다."""
    db.load_env_file()
    url = os.environ.get("DATABASE_URL")
    if not url:
        return "DATABASE_URL이 설정되어 있지 않습니다."
    parsed = urlparse(url)
    return f"호스트 {parsed.hostname}, 포트 {parsed.port or '기본'}, DB {parsed.path.lstrip('/')}"


def main() -> None:
    args = parse_args()
    rows = read_rows(args.input)
    column = {name: [row[i] for row in rows] for i, name in enumerate(COLUMNS)}
    print(f"발매일 없는 앨범 {column['release_date'].count(None):,}개", end=", ")
    print(", ".join(f"{name} 없는 앨범 {column[name].count(None):,}개" for name in COVER_WIDTHS))
    print(f"접속 대상: {describe_target()}")

    if not args.yes:
        print("\n미리 보기입니다. 앞의 3개 행:")
        for row in rows[:3]:
            print(f"  {row}")
        print("\nDB에는 접속하지 않았습니다. 실제로 올리려면 --yes를 붙여 다시 실행하십시오.")
        return

    with db.connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(CREATE_TABLE)
            cursor.execute("SELECT count(*) FROM albums_v4")
            before = cursor.fetchone()[0]
            cursor.executemany(UPSERT, rows)
            cursor.execute("SELECT count(*) FROM albums_v4")
            after = cursor.fetchone()[0]
    print(f"\n올렸습니다. albums_v4: {before:,}행 -> {after:,}행 (새로 들어간 앨범 {after - before:,}개, 나머지 {len(rows) - (after - before):,}개는 이미 있어 값을 고쳤습니다.)")


if __name__ == "__main__":
    main()
