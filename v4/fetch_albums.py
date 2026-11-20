"""디스코그래피의 모든 앨범을 SpotifyScraper로 조회해 data/albums.jsonl에 저장합니다.

    python v4/fetch_albums.py [--concurrency 3] [--locale ko] [--limit 개수] [--input 파일] [--output 파일]

- data/discographies.jsonl에 있는 모든 앨범 ID를 중복 없이 모아 get_album을 호출합니다. 앨범 유형(앨범, 싱글, EP, 컴필레이션)은 가리지 않습니다.
- 앨범마다 한 줄을 `{"album_id": 요청한 ID, "album": {응답 그대로}}` 형식으로 기록합니다.
  album은 SpotifyScraper가 돌려준 응답(to_dict)이며 수록곡을 포함하고, 응답을 가공하지 않습니다.
- 이미 기록된 앨범은 다시 조회하지 않습니다. 중단해도 다시 실행하면 이어서 진행합니다.
- 기록되지 않은 앨범은 실패한 것으로 보고 다시 실행할 때 재시도합니다. 존재하지 않는 앨범도 기록하지 않으므로 매번 다시 요청합니다.
- 요청 속도는 --concurrency로 조절하며, 요청이 제한되면 쉬었다가 몇 번 다시 시도합니다.
- 아티스트 이름의 표기 언어는 --locale(BCP-47 언어 태그, 기본값 ko)로 정합니다. 지정하지 않으면 영어 표기로 받습니다.
  언어를 바꿔 다시 받을 때는 이미 기록된 앨범을 건너뛰므로 기존 출력 파일을 먼저 치워야 합니다.
- 응답에서 아티스트의 id와 uri가 모두 비어 있는 항목은 조회가 끝난 뒤 MISSING_ARTIST_IDS의 값으로 채웁니다.
  앨범 ID와 아티스트 이름(ko 표기)으로 찾으며, 앨범의 artists와 수록곡의 artists 모두에 적용합니다.
  응답을 가공하지 않는다는 원칙의 유일한 예외이며, 다시 받아도 같은 결과가 나오도록 이 스크립트가 보정을 맡습니다.
  이미 채워졌으면 아무것도 바꾸지 않으므로 몇 번 실행해도 같고, 조회할 앨범이 없을 때도 실행합니다.
"""

import argparse
import asyncio
import json
import os
import signal
from pathlib import Path

from spotify_scraper import AsyncSpotifyClient
from spotify_scraper.errors import NotFoundError, RateLimitedError
from tqdm import tqdm

DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_INPUT = DATA_DIR / "discographies.jsonl"
DEFAULT_OUTPUT = DATA_DIR / "albums.jsonl"
RATE_LIMIT_PAUSE_SEC = 60  # 서버가 대기 시간을 알려 주지 않을 때 쉬는 시간
MAX_RATE_LIMIT_RETRIES = 3
MAX_CONSECUTIVE_FAILURES = 20  # 연속으로 이만큼 실패하면 차단이나 장애로 보고 중단합니다.
# Spotify 응답에서 아티스트의 id와 uri가 비어 있는 항목의 ID입니다. (앨범 ID, 아티스트 이름) -> 아티스트 ID
# 이름은 --locale ko 기준이며, 값은 get_artist로 조회해 이름이 같은지 확인한 것입니다.
MISSING_ARTIST_IDS = {
    ("1VYsQOFcP272bKe8XDARsW", "박진주"): "05AYUT9lXm5E4N2Z2wVlXm",
    ("1VYsQOFcP272bKe8XDARsW", "에일리"): "3uGFTJ7JMllvhgGpumieHF",
    ("1VYsQOFcP272bKe8XDARsW", "미주"): "3IA6AwJ26Mbs01YX5I1xjs",
    ("1VYsQOFcP272bKe8XDARsW", "HAEWON"): "4suGbXz0FM8bO6YeC0E9no",
    ("52n79yL8beXEHDu1GVfs1L", "샘김"): "4BBN286rBKyCWsSPq2cxYO",
    ("2yyvWJA3GcvUwUtQ9XAzAK", "샘김"): "4BBN286rBKyCWsSPq2cxYO",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="디스코그래피의 모든 앨범을 조회해 저장합니다.")
    parser.add_argument("--concurrency", type=int, default=3, help="동시 요청 수. 기본값은 3")
    parser.add_argument("--locale", default="ko", help="이름의 표기 언어(BCP-47). 기본값은 ko")
    parser.add_argument("--limit", type=int, help="이번 실행에서 조회할 최대 앨범 수")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="디스코그래피 JSONL 파일")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="결과 JSONL 파일")
    args = parser.parse_args()

    if args.concurrency < 1:
        parser.error("--concurrency는 1 이상이어야 합니다.")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit은 1 이상이어야 합니다.")
    if not args.input.is_file():
        parser.error(f"입력 파일이 없습니다: {args.input}")
    return args


def read_album_ids(path: Path) -> list[str]:
    """디스코그래피 JSONL의 앨범 ID를 처음 나온 순서대로, 중복 없이 읽습니다. 읽을 수 없는 줄은 무시합니다."""
    ids: dict[str, None] = {}
    references = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            albums = json.loads(line)["discography"]
            for album in albums:
                references += 1
                ids.setdefault(album["id"])
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    print(f"{path.name}: 앨범 참조 {references:,}개, 고유 앨범 {len(ids):,}개")
    return list(ids)


def read_done_ids(path: Path) -> set[str]:
    """이미 기록된 앨범 ID를 읽습니다. 중단되어 일부만 기록된 줄은 무시합니다."""
    done: set[str] = set()
    if not path.is_file():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            done.add(json.loads(line)["album_id"])
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    return done


def terminate_last_line(path: Path) -> None:
    """파일이 개행으로 끝나지 않으면 개행을 넣습니다. 강제 종료로 끊긴 줄에 새 기록이 이어 붙는 것을 막습니다."""
    if not path.is_file() or path.stat().st_size == 0:
        return
    with path.open("rb") as file:
        file.seek(-1, 2)
        if file.read(1) == b"\n":
            return
    with path.open("a", encoding="utf-8") as file:
        file.write("\n")


def fill_missing_artist_ids(path: Path) -> None:
    """id와 uri가 모두 빈 아티스트에 MISSING_ARTIST_IDS의 ID를 채워 파일을 다시 씁니다. 바뀐 줄이 없으면 파일을 건드리지 않습니다."""
    if not path.is_file():
        return
    filled = remaining = 0
    lines: list[str] = []
    changed = False

    def fix(album_id: str, artists: list) -> bool:
        nonlocal filled, remaining
        touched = False
        for artist in artists:
            if artist.get("id") or artist.get("uri"):
                continue
            artist_id = MISSING_ARTIST_IDS.get((album_id, artist.get("name")))
            if artist_id is None:
                remaining += 1
                continue
            artist["id"] = artist_id
            artist["uri"] = f"spotify:artist:{artist_id}"
            filled += 1
            touched = True
        return touched

    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            album = json.loads(line)["album"]
            album_id = album["id"]
        except (json.JSONDecodeError, KeyError, TypeError):
            lines.append(line)  # 읽을 수 없는 줄은 그대로 둡니다.
            continue
        touched = fix(album_id, album.get("artists") or [])
        for track in album.get("tracks") or []:
            touched |= fix(album_id, track.get("artists") or [])
        if touched:
            changed = True
            line = json.dumps({"album_id": json.loads(line)["album_id"], "album": album}, ensure_ascii=False)
        lines.append(line)

    if changed:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    print(f"아티스트 ID를 채운 항목 {filled:,}개, 아직 ID가 빈 항목 {remaining:,}개")


async def fetch_all(args: argparse.Namespace, album_ids: list[str], already: int) -> None:
    args.output.parent.mkdir(parents=True, exist_ok=True)
    terminate_last_line(args.output)
    queue: asyncio.Queue[str] = asyncio.Queue()
    for album_id in album_ids:
        queue.put_nowait(album_id)

    bar = tqdm(total=already + len(album_ids), initial=already, unit="앨범", dynamic_ncols=True)
    stats = {"ok": 0, "tracks": 0, "not_found": 0, "failed": 0, "consecutive_failures": 0}
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    # 첫 Ctrl+C는 진행 중인 요청만 마치고 멈추며, 한 번 더 누르면 즉시 종료합니다.
    def on_interrupt() -> None:
        stop.set()
        loop.remove_signal_handler(signal.SIGINT)
        tqdm.write("중단합니다. 진행 중인 요청을 마치는 중입니다. (한 번 더 누르면 즉시 종료합니다.)")

    loop.add_signal_handler(signal.SIGINT, on_interrupt)

    def advance() -> None:
        bar.update(1)
        bar.set_postfix(성공=stats["ok"], 곡=stats["tracks"], 없음=stats["not_found"], 실패=stats["failed"])

    async def fetch_one(client: AsyncSpotifyClient, album_id: str) -> None:
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            try:
                album = await client.get_album(album_id, locale=args.locale)
            except NotFoundError:
                stats["not_found"] += 1
                stats["consecutive_failures"] = 0  # 앨범이 없는 것은 차단이나 장애의 신호가 아닙니다.
                advance()
                return
            except RateLimitedError as error:
                if attempt == MAX_RATE_LIMIT_RETRIES:
                    break
                wait = getattr(error, "retry_after", None) or RATE_LIMIT_PAUSE_SEC
                tqdm.write(f"요청 제한을 받아 {wait:g}초 쉽니다. ({album_id})")
                await asyncio.sleep(wait)
            except Exception as error:  # 어떤 실패도 한 앨범의 실패로만 다루고 다음으로 넘어갑니다.
                tqdm.write(f"실패: {album_id} - {type(error).__name__}: {str(error)[:120]}")
                break
            else:
                record = {"album_id": album_id, "album": album.to_dict()}
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
                output.flush()
                stats["ok"] += 1
                stats["tracks"] += len(album.tracks)
                stats["consecutive_failures"] = 0
                advance()
                return
        stats["failed"] += 1
        stats["consecutive_failures"] += 1
        advance()
        if stats["consecutive_failures"] >= MAX_CONSECUTIVE_FAILURES:
            tqdm.write(f"연속 {MAX_CONSECUTIVE_FAILURES}개가 실패해 중단합니다. 차단되었거나 Spotify 쪽 장애일 수 있습니다.")
            stop.set()

    async def worker(client: AsyncSpotifyClient) -> None:
        while not stop.is_set():
            try:
                album_id = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            await fetch_one(client, album_id)

    try:
        with args.output.open("a", encoding="utf-8") as output:
            async with AsyncSpotifyClient() as client:
                await asyncio.gather(*(worker(client) for _ in range(args.concurrency)))
    finally:
        bar.close()

    state = "중단되었습니다" if stop.is_set() else "끝났습니다"
    print(
        f"\n{state}. 성공 {stats['ok']:,}개(수록곡 {stats['tracks']:,}곡), 없음 {stats['not_found']:,}개, 실패 {stats['failed']:,}개 "
        "(없음과 실패는 기록하지 않으므로 다시 실행하면 재요청합니다.)"
    )
    print(f"결과 파일: {args.output}")


def main() -> None:
    args = parse_args()
    all_ids = read_album_ids(args.input)
    done = read_done_ids(args.output)
    pending = [album_id for album_id in all_ids if album_id not in done]
    already = len(all_ids) - len(pending)
    print(f"이미 기록된 앨범 {already:,}개를 제외하고 {len(pending):,}개를 조회합니다.")
    if args.limit is not None:
        pending = pending[: args.limit]
        print(f"--limit에 따라 {len(pending):,}개만 조회합니다.")
    if pending:
        try:
            asyncio.run(fetch_all(args, pending, already))
        except KeyboardInterrupt:
            raise SystemExit("즉시 종료했습니다. 기록된 앨범은 다시 조회하지 않습니다.")
    else:
        print("조회할 앨범이 없습니다.")
    fill_missing_artist_ids(args.output)


if __name__ == "__main__":
    main()
