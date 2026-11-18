"""data/tracks.csv에서 ISRC가 빈 곡의 ISRC를 Spotify Web API로 조회해 채웁니다.

    python v4/fill_isrc_using_spotify.py [--file 파일] [--limit 개수]

- 접속 정보는 저장소 루트 .env의 SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET입니다. 값은 출력하지 않습니다.
- isrc 열이 빈 곡마다 GET /v1/tracks/{id}를 요청해 응답의 external_ids.isrc를 같은 파일에 채웁니다.
  여러 곡을 한 번에 조회하는 엔드포인트는 쓸 수 없어서 요청은 곡 수만큼입니다.
- ISRC만 채우며 이 파일의 다른 열은 바꾸지 않습니다. 이미 값이 있는 곡(ReccoBeats에서 채워진 곡 등)은 조회하지 않습니다.
- 요청은 한 건씩 차례로 보내며, 이전 응답을 받으면 바로 다음 요청을 보냅니다.
- 중단해도 다시 실행하면 isrc가 빈 곡부터 이어서 진행합니다.
- ISRC가 없거나(응답에 external_ids.isrc 없음), 곡이 없거나(404), 형식이 이상하거나(12자 ISRC 형식이 아님),
  요청이 실패한 곡은 빈칸으로 남기며 다시 실행할 때 재시도합니다.
- 요청이 제한(429)되면 Retry-After만큼 쉬고 다시 시도합니다. Retry-After가 MAX_WAIT_SEC를 넘으면
  기다리지 않고 저장한 뒤 멈추며, 언제 다시 실행하면 되는지 알려 줍니다.
- 채운 값은 SAVE_EVERY곡마다 그리고 종료할 때 파일에 저장합니다. 임시 파일에 쓴 뒤 교체하므로 도중에 멈춰도 파일이 깨지지 않습니다.
  이 스크립트를 실행하는 동안에는 CSV를 다른 곳에서 수정하지 않아야 합니다.
"""

import argparse
import base64
import csv
import json
import os
import re
import signal
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from tqdm import tqdm

import db

DEFAULT_FILE = Path(__file__).resolve().parent / "data" / "tracks.csv"
TOKEN_URL = "https://accounts.spotify.com/api/token"
TRACK_URL = "https://api.spotify.com/v1/tracks/"
HEADER = [
    "spotify_id", "title", "artist", "artist_spotify_ids", "duration_ms", "play_count",
    "album_id", "album_thumb_url", "reccobeats_id", "isrc", "valence", "arousal",
]
ISRC_PATTERN = re.compile(r"[A-Z]{2}[A-Z0-9]{3}[0-9]{7}")
USER_AGENT = "capstone-data/1.0"
REQUEST_TIMEOUT_SEC = 30
TOKEN_MARGIN_SEC = 60  # 토큰이 만료되기 이만큼 전에 새로 받습니다.
RATE_LIMIT_PAUSE_SEC = 30  # 서버가 대기 시간을 알려 주지 않을 때 쉬는 시간
MAX_RATE_LIMIT_RETRIES = 3
MAX_WAIT_SEC = 300  # Retry-After가 이보다 길면 기다리지 않고 멈춥니다.
MAX_CONSECUTIVE_FAILURES = 20  # 연속으로 이만큼 실패하면 차단이나 장애로 보고 중단합니다.
SAVE_EVERY = 500


class RateLimitedLong(Exception):
    """Retry-After가 너무 길어 기다리지 않고 멈춥니다."""

    def __init__(self, wait: float) -> None:
        super().__init__(f"Retry-After {wait:g}초")
        self.wait = wait


def lower_keys(headers) -> dict:
    """응답 헤더를 이름이 소문자인 dict로 바꿉니다. 서버가 보낸 대소문자와 상관없이 같은 이름으로 찾기 위해서입니다."""
    return {name.lower(): value for name, value in headers.items()}


class Spotify:
    def __init__(self, client_id: str, client_secret: str) -> None:
        self._auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        self._token = ""
        self._expires_at = 0.0

    def _refresh_token(self) -> None:
        request = urllib.request.Request(
            TOKEN_URL,
            data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
            headers={"Authorization": f"Basic {self._auth}", "User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SEC) as response:
            body = json.load(response)
        self._token = body["access_token"]
        self._expires_at = time.monotonic() + body.get("expires_in", 3600) - TOKEN_MARGIN_SEC

    def get_track(self, track_id: str, retry_unauthorized: bool = True) -> tuple[int, dict, dict | None]:
        """곡을 조회해 (상태 코드, 응답 헤더, 본문)을 돌려줍니다. 본문은 200일 때만 있고, 헤더의 이름은 모두 소문자입니다."""
        if time.monotonic() >= self._expires_at:
            self._refresh_token()
        request = urllib.request.Request(
            TRACK_URL + track_id, headers={"Authorization": f"Bearer {self._token}", "User-Agent": USER_AGENT}
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SEC) as response:
                return response.status, lower_keys(response.headers), json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 401 and retry_unauthorized:
                self._expires_at = 0.0  # 토큰이 거부되면 새로 받아 한 번만 다시 시도합니다.
                return self.get_track(track_id, retry_unauthorized=False)
            return error.code, lower_keys(error.headers), None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ISRC가 빈 곡의 ISRC를 Spotify Web API로 조회해 채웁니다.")
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE, help="곡 CSV 파일(tracks.csv)")
    parser.add_argument("--limit", type=int, help="이번 실행에서 조회할 최대 곡 수")
    args = parser.parse_args()

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


def lookup(spotify: Spotify, track_id: str, stop: threading.Event) -> tuple[str, str]:
    """곡 하나의 ISRC를 조회합니다. (결과, ISRC)를 돌려주며 결과는 filled, no_isrc, not_found, bad_format, failed 중 하나입니다.
    Retry-After가 너무 길면 RateLimitedLong을 던집니다."""
    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        try:
            status, headers, body = spotify.get_track(track_id)
        except Exception as error:  # 어떤 실패도 한 곡의 실패로만 다루고 다음으로 넘어갑니다.
            tqdm.write(f"실패: {track_id} - {type(error).__name__}: {str(error)[:120]}")
            return "failed", ""

        if status == 200:
            isrc = str(((body or {}).get("external_ids") or {}).get("isrc") or "").strip().upper()
            if not isrc:
                return "no_isrc", ""
            return ("filled", isrc) if ISRC_PATTERN.fullmatch(isrc) else ("bad_format", isrc)
        if status == 404:
            return "not_found", ""
        if status != 429:
            tqdm.write(f"실패: {track_id} - HTTP {status}")
            return "failed", ""

        retry_after = headers.get("retry-after", "")
        wait = float(retry_after) + 1 if retry_after.isdigit() else RATE_LIMIT_PAUSE_SEC
        if wait > MAX_WAIT_SEC:
            raise RateLimitedLong(wait)
        if attempt == MAX_RATE_LIMIT_RETRIES:
            return "failed", ""
        tqdm.write(f"요청 제한을 받아 {wait:g}초 쉽니다. ({track_id})")
        if stop.wait(wait):  # 쉬는 중에 중단 신호를 받으면 바로 깹니다.
            return "failed", ""
    return "failed", ""


def format_duration(seconds: float) -> str:
    hours, rest = divmod(int(seconds), 3600)
    minutes = rest // 60
    return f"{hours}시간 {minutes}분" if hours else f"{minutes}분"


def main() -> None:
    args = parse_args()
    db.load_env_file()
    client_id, client_secret = os.environ.get("SPOTIFY_CLIENT_ID"), os.environ.get("SPOTIFY_CLIENT_SECRET")
    missing_vars = [name for name, value in (("SPOTIFY_CLIENT_ID", client_id), ("SPOTIFY_CLIENT_SECRET", client_secret)) if not value]
    if missing_vars:
        raise SystemExit(f"{', '.join(missing_vars)} 환경변수가 필요합니다. .env를 확인해 주십시오.")

    rows = read_rows(args.file)
    pending = [index for index, row in enumerate(rows) if not row["isrc"].strip()]
    print(f"{args.file.name}: 곡 {len(rows):,}곡, ISRC가 빈 곡 {len(pending):,}곡")
    if args.limit is not None:
        pending = pending[: args.limit]
        print(f"--limit에 따라 {len(pending):,}곡만 조회합니다.")
    if not pending:
        print("조회할 곡이 없습니다.")
        return

    stop = threading.Event()

    # 첫 Ctrl+C는 진행 중인 요청만 마치고 저장한 뒤 멈추며, 한 번 더 누르면 즉시 종료합니다.
    def on_interrupt(signum, frame) -> None:
        stop.set()
        signal.signal(signal.SIGINT, signal.default_int_handler)
        tqdm.write("중단합니다. 진행 중인 요청을 마치고 저장합니다. (한 번 더 누르면 즉시 종료합니다.)")

    signal.signal(signal.SIGINT, on_interrupt)

    spotify = Spotify(client_id, client_secret)
    stats = {"filled": 0, "no_isrc": 0, "not_found": 0, "bad_format": 0, "failed": 0}
    unsaved = consecutive_failures = 0
    long_wait: float | None = None
    bar = tqdm(total=len(pending), unit="곡", dynamic_ncols=True)
    try:
        for index in pending:
            if stop.is_set():
                break
            try:
                result, isrc = lookup(spotify, rows[index]["spotify_id"], stop)
            except RateLimitedLong as error:
                long_wait = error.wait
                break
            stats[result] += 1
            if result == "filled":
                rows[index]["isrc"] = isrc
                unsaved += 1
            consecutive_failures = consecutive_failures + 1 if result == "failed" else 0
            bar.update(1)
            bar.set_postfix(채움=stats["filled"], 없음=stats["no_isrc"] + stats["not_found"], 실패=stats["failed"])
            if unsaved >= SAVE_EVERY:
                save_rows(args.file, rows)
                unsaved = 0
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                tqdm.write(f"연속 {MAX_CONSECUTIVE_FAILURES}곡이 실패해 중단합니다. 차단되었거나 Spotify 쪽 장애일 수 있습니다.")
                break
    finally:
        bar.close()
        save_rows(args.file, rows)

    remaining = sum(1 for row in rows if not row["isrc"].strip())
    if long_wait is not None:
        state = f"요청 제한으로 멈췄습니다. Retry-After가 {long_wait:g}초({format_duration(long_wait)})입니다. 그 뒤에 다시 실행하십시오"
    elif stop.is_set() or consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
        state = "중단되었습니다"
    else:
        state = "끝났습니다"
    print(
        f"\n{state}. 채움 {stats['filled']:,}곡, ISRC 없음 {stats['no_isrc']:,}곡, 곡 없음(404) {stats['not_found']:,}곡, "
        f"형식 이상 {stats['bad_format']:,}곡, 실패 {stats['failed']:,}곡"
    )
    print(f"ISRC가 빈 곡 {remaining:,}곡이 남았습니다. (없음과 실패는 빈칸으로 남아 다시 실행하면 재요청합니다.)")
    print(f"결과 파일: {args.file}")


if __name__ == "__main__":
    main()
