"""곡의 오디오 특징을 ReccoBeats에서 조회해 data/audio_features.jsonl에 저장합니다.

    python v4/fetch_audio_features.py [--concurrency 3] [--batch-size 40] [--limit 개수] [--input 파일] [--output 파일]

- data/tracks.jsonl의 곡 ID(Spotify)를 batch-size개씩 묶어 ReccoBeats의 GET /v1/audio-features에 요청합니다.
- 응답에 있는 곡마다 한 줄을 다음 형식으로 기록합니다. 값은 응답 그대로이며 ID 두 개의 이름만 바꾸었습니다.
  {"spotify_id", "reccobeats_id", "isrc", "acousticness", "danceability", "energy", "instrumentalness",
   "key", "liveness", "loudness", "mode", "speechiness", "tempo", "valence"}
  spotify_id는 응답의 href(Spotify 곡 링크)에서 읽고, reccobeats_id는 응답의 id입니다.
- ReccoBeats에 값이 없는 곡은 응답에 나오지 않으며, 기록하지 않습니다. 그래서 다시 실행하면 다시 요청합니다.
- 요청이 실패한 곡도 기록하지 않습니다. 파일만 보면 "값이 없는 곡"과 "실패한 곡"을 구분할 수 없으므로,
  실행이 끝날 때 출력되는 실패 수가 0이 될 때까지 다시 실행해야 합니다.
- 이미 기록된 곡은 다시 조회하지 않습니다. 중단해도 다시 실행하면 이어서 진행합니다.
- 요청 속도는 --concurrency로 조절하며, 요청 사이에는 최소 간격을 두고, 요청이 제한되면 쉬었다가 다시 시도합니다.
"""

import argparse
import asyncio
import json
import signal
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from tqdm import tqdm

DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_INPUT = DATA_DIR / "tracks.jsonl"
DEFAULT_OUTPUT = DATA_DIR / "audio_features.jsonl"
API_URL = "https://api.reccobeats.com/v1/audio-features"
FEATURE_FIELDS = [
    "isrc", "acousticness", "danceability", "energy", "instrumentalness", "key",
    "liveness", "loudness", "mode", "speechiness", "tempo", "valence",
]
MIN_INTERVAL_SEC = 0.12  # 요청 사이의 최소 간격(초당 약 8회)
REQUEST_TIMEOUT_SEC = 60
RATE_LIMIT_PAUSE_SEC = 60  # 서버가 대기 시간을 알려 주지 않을 때 쉬는 시간
MAX_RATE_LIMIT_RETRIES = 3
MAX_CONSECUTIVE_FAILURES = 20  # 연속으로 이만큼 묶음이 실패하면 차단이나 장애로 보고 중단합니다.


class RateLimited(Exception):
    def __init__(self, retry_after: float | None) -> None:
        super().__init__("요청이 제한되었습니다.")
        self.retry_after = retry_after


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="곡의 오디오 특징을 조회해 저장합니다.")
    parser.add_argument("--concurrency", type=int, default=3, help="동시 요청 수. 기본값은 3")
    parser.add_argument("--batch-size", type=int, default=40, help="요청 한 번에 조회할 곡 수. 기본값은 40")
    parser.add_argument("--limit", type=int, help="이번 실행에서 조회할 최대 곡 수")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="곡 JSONL 파일")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="결과 JSONL 파일")
    args = parser.parse_args()

    if args.concurrency < 1:
        parser.error("--concurrency는 1 이상이어야 합니다.")
    if args.batch_size < 1:
        parser.error("--batch-size는 1 이상이어야 합니다.")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit은 1 이상이어야 합니다.")
    if not args.input.is_file():
        parser.error(f"입력 파일이 없습니다: {args.input}")
    return args


def read_track_ids(path: Path) -> list[str]:
    """곡 JSONL의 Spotify ID를 처음 나온 순서대로, 중복 없이 읽습니다. 읽을 수 없는 줄은 무시합니다."""
    ids: dict[str, None] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            ids.setdefault(json.loads(line)["id"])
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    print(f"{path.name}: 곡 {len(ids):,}곡")
    return list(ids)


def read_done_ids(path: Path) -> set[str]:
    """이미 기록된 곡 ID를 읽습니다. 중단되어 일부만 기록된 줄은 무시합니다."""
    done: set[str] = set()
    if not path.is_file():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            done.add(json.loads(line)["spotify_id"])
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


def request_features(ids: list[str]) -> list[dict]:
    """ReccoBeats에 곡 ID 묶음을 요청해 응답의 content를 돌려줍니다. 요청이 제한되면 RateLimited를 던집니다."""
    query = urllib.parse.urlencode({"ids": ",".join(ids)}, safe=",")
    request = urllib.request.Request(
        f"{API_URL}?{query}", headers={"Accept": "application/json", "User-Agent": "capstone-data/1.0"}
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SEC) as response:
            return json.load(response)["content"]
    except urllib.error.HTTPError as error:
        if error.code == 429:
            retry_after = error.headers.get("Retry-After", "")
            raise RateLimited(float(retry_after) if retry_after.isdigit() else None) from error
        raise


def to_record(item: dict) -> tuple[str, dict]:
    """응답의 곡 하나를 저장할 기록으로 바꿉니다. (Spotify ID, 기록)"""
    spotify_id = item["href"].rstrip("/").rsplit("/", 1)[-1]
    record = {"spotify_id": spotify_id, "reccobeats_id": item["id"]}
    record.update({field: item.get(field) for field in FEATURE_FIELDS})
    return spotify_id, record


async def fetch_all(args: argparse.Namespace, track_ids: list[str], already: int) -> None:
    args.output.parent.mkdir(parents=True, exist_ok=True)
    terminate_last_line(args.output)
    queue: asyncio.Queue[list[str]] = asyncio.Queue()
    for start in range(0, len(track_ids), args.batch_size):
        queue.put_nowait(track_ids[start:start + args.batch_size])

    bar = tqdm(total=already + len(track_ids), initial=already, unit="곡", dynamic_ncols=True)
    stats = {"ok": 0, "missing": 0, "failed": 0, "consecutive_failures": 0}
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    pacing = asyncio.Lock()
    last_request = 0.0

    # 첫 Ctrl+C는 진행 중인 요청만 마치고 멈추며, 한 번 더 누르면 즉시 종료합니다.
    def on_interrupt() -> None:
        stop.set()
        loop.remove_signal_handler(signal.SIGINT)
        tqdm.write("중단합니다. 진행 중인 요청을 마치는 중입니다. (한 번 더 누르면 즉시 종료합니다.)")

    loop.add_signal_handler(signal.SIGINT, on_interrupt)

    async def wait_turn() -> None:
        """요청 사이에 최소 간격을 둡니다."""
        nonlocal last_request
        async with pacing:
            wait = MIN_INTERVAL_SEC - (time.monotonic() - last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            last_request = time.monotonic()

    def advance(count: int) -> None:
        bar.update(count)
        bar.set_postfix(성공=stats["ok"], 없음=stats["missing"], 실패=stats["failed"])

    async def fetch_batch(batch: list[str]) -> None:
        items = None
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            await wait_turn()
            try:
                items = await asyncio.to_thread(request_features, batch)
                break
            except RateLimited as error:
                if attempt == MAX_RATE_LIMIT_RETRIES:
                    break
                wait = error.retry_after or RATE_LIMIT_PAUSE_SEC
                tqdm.write(f"요청 제한을 받아 {wait:g}초 쉽니다.")
                await asyncio.sleep(wait)
            except Exception as error:  # 어떤 실패도 한 묶음의 실패로만 다루고 다음으로 넘어갑니다.
                tqdm.write(f"실패: {type(error).__name__}: {str(error)[:120]}")
                break

        if items is None:
            stats["failed"] += len(batch)
            stats["consecutive_failures"] += 1
            advance(len(batch))
            if stats["consecutive_failures"] >= MAX_CONSECUTIVE_FAILURES:
                tqdm.write(f"연속 {MAX_CONSECUTIVE_FAILURES}개 묶음이 실패해 중단합니다. 차단되었거나 ReccoBeats 쪽 장애일 수 있습니다.")
                stop.set()
            return

        requested = set(batch)
        found: set[str] = set()
        for item in items:
            try:
                spotify_id, record = to_record(item)
            except (KeyError, TypeError):
                continue
            if spotify_id in requested and spotify_id not in found:
                found.add(spotify_id)
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
        output.flush()
        stats["ok"] += len(found)
        stats["missing"] += len(batch) - len(found)
        stats["consecutive_failures"] = 0
        advance(len(batch))

    async def worker() -> None:
        while not stop.is_set():
            try:
                batch = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            await fetch_batch(batch)

    try:
        with args.output.open("a", encoding="utf-8") as output:
            await asyncio.gather(*(worker() for _ in range(args.concurrency)))
    finally:
        bar.close()

    state = "중단되었습니다" if stop.is_set() else "끝났습니다"
    print(f"\n{state}. 성공 {stats['ok']:,}곡, 없음 {stats['missing']:,}곡, 실패 {stats['failed']:,}곡")
    print("없음과 실패는 기록하지 않으므로 다시 실행하면 재요청합니다. 실패가 0이 될 때까지 다시 실행하세요.")
    print(f"결과 파일: {args.output}")


def main() -> None:
    args = parse_args()
    all_ids = read_track_ids(args.input)
    done = read_done_ids(args.output)
    pending = [track_id for track_id in all_ids if track_id not in done]
    already = len(all_ids) - len(pending)
    print(f"이미 기록된 곡 {already:,}곡을 제외하고 {len(pending):,}곡을 조회합니다.")
    if args.limit is not None:
        pending = pending[: args.limit]
        print(f"--limit에 따라 {len(pending):,}곡만 조회합니다.")
    if not pending:
        print("조회할 곡이 없습니다.")
        return
    try:
        asyncio.run(fetch_all(args, pending, already))
    except KeyboardInterrupt:
        raise SystemExit("즉시 종료했습니다. 기록된 곡은 다시 조회하지 않습니다.")


if __name__ == "__main__":
    main()
