"""tracks_v3의 곡을 SpotifyScraper로 조회해 응답을 data/spotify-tracks.jsonl에 저장합니다.

    python fetch_spotify_tracks.py [--country-codes=KR,KS] [--locale ko] [--concurrency 3] [--limit 개수] [--output 파일]

- --country-codes: ISRC가 지정한 국가 코드로 시작하는 곡만 조회합니다. 지정하지 않으면 모든 곡을 조회합니다.
- 곡마다 한 줄씩 기록하고, 이미 기록된 곡은 건너뜁니다. 중단해도 다시 실행하면 이어서 진행합니다.
- 존재하지 않는 곡(not_found)은 기록해 두어 다시 요청하지 않습니다. 그 밖의 실패는 기록하지 않으므로 다시 실행하면 재시도합니다.
- DB는 읽기만 하며, 요청 속도는 SpotifyScraper의 기본 제한과 재시도 정책을 따릅니다.
"""

import argparse
import asyncio
import json
import re
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

from spotify_scraper import AsyncSpotifyClient
from spotify_scraper.errors import NotFoundError, RateLimitedError

from db import connect

DEFAULT_OUTPUT = Path(__file__).resolve().parent / "data" / "spotify-tracks.jsonl"
RATE_LIMIT_PAUSE_SEC = 60  # 서버가 대기 시간을 알려 주지 않을 때 쉬는 시간
MAX_RATE_LIMIT_RETRIES = 3
MAX_CONSECUTIVE_FAILURES = 20  # 연속으로 이만큼 실패하면 차단이나 장애로 보고 중단합니다.
PROGRESS_INTERVAL = 100


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="tracks_v3의 곡을 SpotifyScraper로 조회해 저장합니다.")
    parser.add_argument("--country-codes", help="ISRC 앞 두 글자 국가 코드를 쉼표로 구분해 지정합니다. 예: KR,KS (기본값: 모든 곡)")
    parser.add_argument("--locale", default="ko", help="표기 언어(BCP-47). 기본값은 ko")
    parser.add_argument("--concurrency", type=int, default=3, help="동시 요청 수. 기본값은 3")
    parser.add_argument("--limit", type=int, help="이번 실행에서 조회할 최대 곡 수")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="결과 JSONL 파일")
    args = parser.parse_args()

    if args.concurrency < 1:
        parser.error("--concurrency는 1 이상이어야 합니다.")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit은 1 이상이어야 합니다.")
    if args.country_codes is not None:
        codes = [code.strip().upper() for code in args.country_codes.split(",") if code.strip()]
        if not codes or not all(re.fullmatch(r"[A-Z]{2}", code) for code in codes):
            parser.error("--country-codes는 KR,KS처럼 두 글자 코드를 쉼표로 구분해 지정해야 합니다.")
        args.country_codes = codes
    return args


def select_track_ids(country_codes: list[str] | None) -> list[str]:
    """조회할 곡의 Spotify ID를 읽습니다. 국가 코드를 지정하면 ISRC 앞 두 글자가 일치하는 곡만 읽습니다."""
    with connect() as connection:
        if country_codes is None:
            rows = connection.execute("SELECT spotify_id FROM tracks_v3 ORDER BY spotify_id").fetchall()
        else:
            rows = connection.execute(
                "SELECT spotify_id FROM tracks_v3 WHERE left(isrc, 2) = ANY(%s) ORDER BY spotify_id",
                (country_codes,),
            ).fetchall()
    return [row[0] for row in rows]


def read_done_ids(path: Path) -> set[str]:
    """이미 기록된 곡의 ID를 읽습니다. 중단되어 일부만 기록된 마지막 줄은 무시합니다."""
    done: set[str] = set()
    if not path.is_file():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            done.add(json.loads(line)["spotify_id"])
        except (json.JSONDecodeError, KeyError):
            continue
    return done


class Progress:
    def __init__(self, total: int) -> None:
        self.total = total
        self.ok = 0
        self.not_found = 0
        self.failed = 0
        self.consecutive_failures = 0
        self.started = time.monotonic()

    @property
    def processed(self) -> int:
        return self.ok + self.not_found + self.failed

    def report(self) -> None:
        elapsed = time.monotonic() - self.started
        rate = self.processed / elapsed if elapsed > 0 else 0
        remaining = (self.total - self.processed) / rate if rate > 0 else 0
        print(
            f"  {self.processed}/{self.total}곡 처리 (성공 {self.ok}, 없음 {self.not_found}, 실패 {self.failed}) "
            f"- 초당 {rate:.2f}곡, 남은 시간 약 {remaining / 60:.0f}분",
            flush=True,
        )


async def fetch_all(args: argparse.Namespace, track_ids: list[str]) -> None:
    args.output.parent.mkdir(parents=True, exist_ok=True)
    queue: asyncio.Queue[str] = asyncio.Queue()
    for track_id in track_ids:
        queue.put_nowait(track_id)

    progress = Progress(len(track_ids))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # 첫 Ctrl+C는 진행 중인 요청만 마치고 멈추며, 한 번 더 누르면 즉시 종료합니다.
    def on_interrupt() -> None:
        stop.set()
        loop.remove_signal_handler(signal.SIGINT)
        print("\n중단합니다. 진행 중인 요청을 마치는 중입니다. (한 번 더 누르면 즉시 종료합니다.)", flush=True)

    loop.add_signal_handler(signal.SIGINT, on_interrupt)

    def write(record: dict) -> None:
        output.write(json.dumps(record, ensure_ascii=False) + "\n")
        output.flush()

    async def fetch_one(client: AsyncSpotifyClient, track_id: str) -> None:
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            try:
                track = await client.get_track(track_id, locale=args.locale)
            except NotFoundError:
                write({"spotify_id": track_id, "status": "not_found", "fetched_at": datetime.now(timezone.utc).isoformat()})
                progress.not_found += 1
                progress.consecutive_failures = 0
                return
            except RateLimitedError as error:
                if attempt == MAX_RATE_LIMIT_RETRIES:
                    break
                wait = error.retry_after or RATE_LIMIT_PAUSE_SEC
                print(f"  요청 제한을 받아 {wait:.0f}초 쉽니다. ({track_id})", flush=True)
                await asyncio.sleep(wait)
            except Exception as error:  # 어떤 실패도 한 곡의 실패로만 다루고 다음 곡으로 넘어갑니다.
                print(f"  실패: {track_id} - {type(error).__name__}: {str(error)[:120]}", flush=True)
                break
            else:
                write({
                    "spotify_id": track_id,
                    "status": "ok",
                    "locale": args.locale,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "track": track.to_dict(),
                })
                progress.ok += 1
                progress.consecutive_failures = 0
                return
        progress.failed += 1
        progress.consecutive_failures += 1
        if progress.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            print(f"연속 {MAX_CONSECUTIVE_FAILURES}곡이 실패해 중단합니다. 차단되었거나 Spotify 쪽 장애일 수 있습니다.", flush=True)
            stop.set()

    async def worker(client: AsyncSpotifyClient) -> None:
        while not stop.is_set():
            try:
                track_id = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            before = progress.processed
            await fetch_one(client, track_id)
            if progress.processed // PROGRESS_INTERVAL > before // PROGRESS_INTERVAL:
                progress.report()

    with args.output.open("a", encoding="utf-8") as output:
        async with AsyncSpotifyClient(locale=args.locale) as client:
            await asyncio.gather(*(worker(client) for _ in range(args.concurrency)))

    state = "중단되었습니다" if stop.is_set() else "끝났습니다"
    print(f"\n{state}. 성공 {progress.ok}곡, 없음 {progress.not_found}곡, 실패 {progress.failed}곡 (실패한 곡은 다시 실행하면 재시도합니다.)")
    print(f"결과 파일: {args.output}")


def main() -> None:
    args = parse_args()
    track_ids = select_track_ids(args.country_codes)
    done = read_done_ids(args.output)
    pending = [track_id for track_id in track_ids if track_id not in done]
    if args.limit is not None:
        pending = pending[: args.limit]

    scope = ",".join(args.country_codes) if args.country_codes else "전체"
    already = sum(1 for track_id in track_ids if track_id in done)
    print(f"대상({scope}) {len(track_ids):,}곡 중 이미 기록된 {already:,}곡을 빼고 {len(pending):,}곡을 조회합니다.")
    if not pending:
        print("조회할 곡이 없습니다.")
        return
    asyncio.run(fetch_all(args, pending))


if __name__ == "__main__":
    main()
