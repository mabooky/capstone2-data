"""시드 아티스트의 디스코그래피를 SpotifyScraper로 조회해 data/discographies.jsonl에 저장합니다.

    python v4/fetch_discographies.py [--concurrency 3] [--limit 개수] [--output 파일]

- seed-artists.csv의 아티스트 Spotify ID마다 get_discography를 호출합니다. ID가 비었거나 중복된 행은 건너뜁니다.
- 아티스트마다 한 줄을 `{"artist_id": 요청한 ID, "discography": [응답 그대로]}` 형식으로 기록합니다.
  discography는 SpotifyScraper가 돌려준 앨범 참조(id, uri, name, images)의 목록이며, 응답을 가공하지 않습니다.
- 이미 기록된 아티스트는 다시 조회하지 않습니다. 중단해도 다시 실행하면 이어서 진행합니다.
- 기록되지 않은 아티스트는 실패한 것으로 보고 다시 실행할 때 재시도합니다.
- 요청 속도는 --concurrency로 조절하며, 요청이 제한되면 쉬었다가 몇 번 다시 시도합니다.
"""

import argparse
import asyncio
import csv
import json
import signal
from pathlib import Path

from spotify_scraper import AsyncSpotifyClient
from spotify_scraper.errors import RateLimitedError
from tqdm import tqdm

BASE_DIR = Path(__file__).resolve().parent
SEED_CSV = BASE_DIR / "seed-artists.csv"
DEFAULT_OUTPUT = BASE_DIR / "data" / "discographies.jsonl"
RATE_LIMIT_PAUSE_SEC = 60  # 서버가 대기 시간을 알려 주지 않을 때 쉬는 시간
MAX_RATE_LIMIT_RETRIES = 3
MAX_CONSECUTIVE_FAILURES = 20  # 연속으로 이만큼 실패하면 차단이나 장애로 보고 중단합니다.


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="시드 아티스트의 디스코그래피를 조회해 저장합니다.")
    parser.add_argument("--concurrency", type=int, default=3, help="동시 요청 수. 기본값은 3")
    parser.add_argument("--limit", type=int, help="이번 실행에서 조회할 최대 아티스트 수")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="결과 JSONL 파일")
    args = parser.parse_args()

    if args.concurrency < 1:
        parser.error("--concurrency는 1 이상이어야 합니다.")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit은 1 이상이어야 합니다.")
    return args


def read_seed_ids(path: Path) -> list[str]:
    """seed-artists.csv의 아티스트 Spotify ID를 파일 순서대로 읽습니다. 첫 줄은 머리글이므로 건너뛰고, 빈 ID와 중복은 제외합니다."""
    with path.open(encoding="utf-8", newline="") as file:
        rows = list(csv.reader(file))[1:]
    ids = [row[1].strip() if len(row) >= 2 else "" for row in rows]
    filled = [spotify_id for spotify_id in ids if spotify_id]
    unique = list(dict.fromkeys(filled))
    print(f"seed-artists.csv: {len(rows):,}행, 빈 ID {len(ids) - len(filled):,}행, "
          f"중복 {len(filled) - len(unique):,}행, 고유 아티스트 {len(unique):,}명")
    return unique


def read_done_ids(path: Path) -> set[str]:
    """이미 기록된 아티스트 ID를 읽습니다. 중단되어 일부만 기록된 줄은 무시합니다."""
    done: set[str] = set()
    if not path.is_file():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            done.add(json.loads(line)["artist_id"])
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    return done


async def fetch_all(args: argparse.Namespace, artist_ids: list[str], already: int) -> None:
    args.output.parent.mkdir(parents=True, exist_ok=True)
    queue: asyncio.Queue[str] = asyncio.Queue()
    for artist_id in artist_ids:
        queue.put_nowait(artist_id)

    bar = tqdm(total=already + len(artist_ids), initial=already, unit="명", dynamic_ncols=True)
    stats = {"ok": 0, "albums": 0, "failed": 0, "consecutive_failures": 0}
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
        bar.set_postfix(성공=stats["ok"], 앨범=stats["albums"], 실패=stats["failed"])

    async def fetch_one(client: AsyncSpotifyClient, artist_id: str) -> None:
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            try:
                discography = await client.get_discography(artist_id)
            except RateLimitedError as error:
                if attempt == MAX_RATE_LIMIT_RETRIES:
                    break
                wait = getattr(error, "retry_after", None) or RATE_LIMIT_PAUSE_SEC
                tqdm.write(f"요청 제한을 받아 {wait:g}초 쉽니다. ({artist_id})")
                await asyncio.sleep(wait)
            except Exception as error:  # 어떤 실패도 한 아티스트의 실패로만 다루고 다음으로 넘어갑니다.
                tqdm.write(f"실패: {artist_id} - {type(error).__name__}: {str(error)[:120]}")
                break
            else:
                record = {"artist_id": artist_id, "discography": [album.to_dict() for album in discography]}
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
                output.flush()
                stats["ok"] += 1
                stats["albums"] += len(discography)
                stats["consecutive_failures"] = 0
                advance()
                return
        stats["failed"] += 1
        stats["consecutive_failures"] += 1
        advance()
        if stats["consecutive_failures"] >= MAX_CONSECUTIVE_FAILURES:
            tqdm.write(f"연속 {MAX_CONSECUTIVE_FAILURES}명이 실패해 중단합니다. 차단되었거나 Spotify 쪽 장애일 수 있습니다.")
            stop.set()

    async def worker(client: AsyncSpotifyClient) -> None:
        while not stop.is_set():
            try:
                artist_id = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            await fetch_one(client, artist_id)

    try:
        with args.output.open("a", encoding="utf-8") as output:
            async with AsyncSpotifyClient() as client:
                await asyncio.gather(*(worker(client) for _ in range(args.concurrency)))
    finally:
        bar.close()

    state = "중단되었습니다" if stop.is_set() else "끝났습니다"
    print(f"\n{state}. 성공 {stats['ok']:,}명(앨범 {stats['albums']:,}개), 실패 {stats['failed']:,}명 (실패한 아티스트는 다시 실행하면 재시도합니다.)")
    print(f"결과 파일: {args.output}")


def main() -> None:
    args = parse_args()
    seed_ids = read_seed_ids(SEED_CSV)
    done = read_done_ids(args.output)
    pending = [artist_id for artist_id in seed_ids if artist_id not in done]
    already = len(seed_ids) - len(pending)
    print(f"이미 기록된 아티스트 {already:,}명을 제외하고 {len(pending):,}명을 조회합니다.")
    if args.limit is not None:
        pending = pending[: args.limit]
        print(f"--limit에 따라 {len(pending):,}명만 조회합니다.")
    if not pending:
        print("조회할 아티스트가 없습니다.")
        return
    try:
        asyncio.run(fetch_all(args, pending, already))
    except KeyboardInterrupt:
        raise SystemExit("즉시 종료했습니다. 기록된 아티스트는 다시 조회하지 않습니다.")


if __name__ == "__main__":
    main()
