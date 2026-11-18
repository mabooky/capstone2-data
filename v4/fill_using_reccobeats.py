"""ReccoBeats에서 곡의 값을 조회해 data/tracks.csv의 빈칸을 채웁니다.

    python v4/fill_using_reccobeats.py [--file 파일] [--concurrency 3] [--batch-size 40] [--limit 개수]

- reccobeats_id가 빈 곡의 Spotify ID를 batch-size개씩 묶어 ReccoBeats의 GET /v1/audio-features에 요청합니다.
- 응답에 있는 곡은 reccobeats_id, isrc, valence, arousal을 채웁니다. 곡은 응답의 href(Spotify 곡 링크)에서 읽은 ID로 찾습니다.
  - reccobeats_id는 응답의 id이고, isrc는 응답 그대로입니다. ISRC가 없으면 빈칸으로 둡니다.
  - arousal은 응답의 energy입니다. valence와 arousal은 DB와 같게 응답의 0~1 값을 값 × 2 - 1로 바꾸어
    [-1, 1] 범위로 적으며, 소수 6자리로 반올림합니다. (응답이 최대 6자리라 값이 손실되지 않습니다.)
  - 곡은 있지만 valence나 energy가 없는 곡은 reccobeats_id와 isrc만 채우고 valence, arousal은 빈칸으로 둡니다.
- 그 밖의 오디오 특징(danceability, tempo 등)은 쓰지 않으므로 담지 않습니다. 이 파일의 다른 열은 바꾸지 않습니다.
- ReccoBeats에 값이 없는 곡은 응답에 나오지 않아 빈칸으로 남고, 다시 실행하면 다시 요청합니다.
  요청이 실패한 곡도 빈칸으로 남으므로, 파일만 보면 "값이 없는 곡"과 "실패한 곡"을 구분할 수 없습니다.
  따라서 실행이 끝날 때 출력되는 실패 수가 0이 될 때까지 다시 실행해야 합니다.
- 중단해도 다시 실행하면 reccobeats_id가 빈 곡부터 이어서 진행합니다.
- 채운 값은 SAVE_EVERY곡마다 그리고 종료할 때 파일에 저장합니다. 임시 파일에 쓴 뒤 교체하므로 도중에 멈춰도 파일이 깨지지 않습니다.
  이 스크립트를 실행하는 동안에는 CSV를 다른 곳에서 수정하지 않아야 합니다.
- 요청 속도는 --concurrency로 조절하며, 요청 사이에는 최소 간격을 두고, 요청이 제한되면 쉬었다가 다시 시도합니다.
"""

import argparse
import asyncio
import csv
import json
import os
import signal
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from tqdm import tqdm

DEFAULT_FILE = Path(__file__).resolve().parent / "data" / "tracks.csv"
API_URL = "https://api.reccobeats.com/v1/audio-features"
HEADER = [
    "spotify_id", "title", "artist", "artist_spotify_ids", "duration_ms", "play_count",
    "album_id", "album_thumb_url", "reccobeats_id", "isrc", "valence", "arousal",
]
MIN_INTERVAL_SEC = 0.12  # 요청 사이의 최소 간격(초당 약 8회)
REQUEST_TIMEOUT_SEC = 60
RATE_LIMIT_PAUSE_SEC = 60  # 서버가 대기 시간을 알려 주지 않을 때 쉬는 시간
MAX_RATE_LIMIT_RETRIES = 3
MAX_CONSECUTIVE_FAILURES = 20  # 연속으로 이만큼 묶음이 실패하면 차단이나 장애로 보고 중단합니다.
SAVE_EVERY = 2000


class RateLimited(Exception):
    def __init__(self, retry_after: float | None) -> None:
        super().__init__("요청이 제한되었습니다.")
        self.retry_after = retry_after


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ReccoBeats에서 곡의 값을 조회해 tracks.csv의 빈칸을 채웁니다.")
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE, help="곡 CSV 파일(tracks.csv)")
    parser.add_argument("--concurrency", type=int, default=3, help="동시 요청 수. 기본값은 3")
    parser.add_argument("--batch-size", type=int, default=40, help="요청 한 번에 조회할 곡 수. 기본값은 40")
    parser.add_argument("--limit", type=int, help="이번 실행에서 조회할 최대 곡 수")
    args = parser.parse_args()

    if args.concurrency < 1:
        parser.error("--concurrency는 1 이상이어야 합니다.")
    if args.batch_size < 1:
        parser.error("--batch-size는 1 이상이어야 합니다.")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit은 1 이상이어야 합니다.")
    if not args.file.is_file():
        parser.error(f"입력 파일이 없습니다: {args.file}")
    return args


def read_rows(path: Path) -> list[dict]:
    """CSV를 읽어 행 목록을 돌려줍니다. 머리글이 다르면 중단합니다."""
    with path.open(encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames != HEADER:
            raise SystemExit(f"머리글이 예상과 다릅니다: {path}")
        return list(reader)


def save_rows(path: Path, rows: list[dict]) -> None:
    """임시 파일에 먼저 쓰고 교체하므로, 도중에 멈춰도 기존 파일이 깨지지 않습니다."""
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=HEADER, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp, path)


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


def to_cell(value: float) -> str:
    """0~1 값을 값 × 2 - 1로 바꾸어 소수 6자리까지의 문자열로 돌려줍니다. 0이면 '0'입니다."""
    rounded = round(value * 2 - 1, 6)
    if rounded == 0:
        return "0"
    return f"{rounded:.6f}".rstrip("0").rstrip(".")


def to_values(item: dict) -> tuple[str, dict[str, str]]:
    """응답의 곡 하나를 (Spotify ID, 채울 열의 값)으로 바꿉니다. valence나 energy가 없으면 두 열 모두 빈칸입니다."""
    spotify_id = item["href"].rstrip("/").rsplit("/", 1)[-1]
    valence, energy = item.get("valence"), item.get("energy")
    has_features = isinstance(valence, (int, float)) and isinstance(energy, (int, float))
    return spotify_id, {
        "reccobeats_id": item["id"],
        "isrc": item.get("isrc") or "",
        "valence": to_cell(valence) if has_features else "",
        "arousal": to_cell(energy) if has_features else "",
    }


async def fill_all(args: argparse.Namespace, rows: list[dict], pending: list[str], already: int) -> None:
    by_id = {row["spotify_id"]: row for row in rows}
    queue: asyncio.Queue[list[str]] = asyncio.Queue()
    for start in range(0, len(pending), args.batch_size):
        queue.put_nowait(pending[start:start + args.batch_size])

    bar = tqdm(total=already + len(pending), initial=already, unit="곡", dynamic_ncols=True)
    stats = {"ok": 0, "no_features": 0, "missing": 0, "failed": 0, "consecutive_failures": 0, "unsaved": 0}
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    pacing = asyncio.Lock()
    last_request = 0.0

    # 첫 Ctrl+C는 진행 중인 요청만 마치고 저장한 뒤 멈추며, 한 번 더 누르면 즉시 종료합니다.
    def on_interrupt() -> None:
        stop.set()
        loop.remove_signal_handler(signal.SIGINT)
        tqdm.write("중단합니다. 진행 중인 요청을 마치고 저장합니다. (한 번 더 누르면 즉시 종료합니다.)")

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

    async def fill_batch(batch: list[str]) -> None:
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
                spotify_id, values = to_values(item)
            except (KeyError, TypeError, AttributeError):
                continue
            if spotify_id in requested and spotify_id not in found:
                found.add(spotify_id)
                by_id[spotify_id].update(values)
                stats["no_features"] += not values["valence"]
        stats["ok"] += len(found)
        stats["unsaved"] += len(found)
        stats["missing"] += len(batch) - len(found)
        stats["consecutive_failures"] = 0
        advance(len(batch))
        if stats["unsaved"] >= SAVE_EVERY:
            save_rows(args.file, rows)
            stats["unsaved"] = 0

    async def worker() -> None:
        while not stop.is_set():
            try:
                batch = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            await fill_batch(batch)

    try:
        await asyncio.gather(*(worker() for _ in range(args.concurrency)))
    finally:
        bar.close()
        save_rows(args.file, rows)

    state = "중단되었습니다" if stop.is_set() else "끝났습니다"
    print(
        f"\n{state}. 성공 {stats['ok']:,}곡(그 중 valence와 energy가 없어 ISRC만 채운 곡 {stats['no_features']:,}곡), "
        f"없음 {stats['missing']:,}곡, 실패 {stats['failed']:,}곡"
    )
    print("없음과 실패는 빈칸으로 남아 다시 실행하면 재요청합니다. 실패가 0이 될 때까지 다시 실행하세요.")
    print(f"결과 파일: {args.file}")


def main() -> None:
    args = parse_args()
    rows = read_rows(args.file)
    pending = [row["spotify_id"] for row in rows if not row["reccobeats_id"]]
    already = len(rows) - len(pending)
    print(f"{args.file.name}: 곡 {len(rows):,}곡")
    print(f"reccobeats_id가 이미 있는 곡 {already:,}곡을 제외하고 {len(pending):,}곡을 조회합니다.")
    if args.limit is not None:
        pending = pending[: args.limit]
        print(f"--limit에 따라 {len(pending):,}곡만 조회합니다.")
    if not pending:
        print("조회할 곡이 없습니다.")
        return
    try:
        asyncio.run(fill_all(args, rows, pending, already))
    except KeyboardInterrupt:
        raise SystemExit("즉시 종료했습니다. 마지막으로 저장한 뒤의 변경은 반영되지 않았을 수 있습니다.")


if __name__ == "__main__":
    main()
