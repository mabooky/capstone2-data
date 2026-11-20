"""앨범에 나오는 모든 아티스트를 SpotifyScraper로 조회해 data/artists.jsonl에 저장합니다.

    python v4/fetch_artists.py [--concurrency 3] [--locale ko] [--limit 개수] [--input 파일] [--output 파일]

- data/albums.jsonl에서 앨범의 아티스트와 수록곡의 아티스트를 모두 모아 중복 없이 get_artist를 호출합니다.
  수록곡의 아티스트는 id가 비어 있으므로 uri(spotify:artist:ID)에서 ID를 읽습니다. id와 uri가 모두 비어 있는 항목은 건너뜁니다.
  (fetch_albums.py가 이미 알려진 항목을 채워 두므로 보통 없습니다.)
- 아티스트마다 한 줄을 `{"artist_id": 요청한 ID, "artist": {...}}` 형식으로 기록합니다. artist에는 필요한 값만 담습니다.
  id, name, image_url_640, image_url_320, image_url_160(해당 가로 크기의 프로필 이미지 URL, 없으면 null),
  monthly_listeners(없으면 null), top_tracks(응답의 인기곡 목록을 to_dict한 그대로)
  albums, singles, followers 등 나머지 응답은 저장하지 않습니다. 응답을 그대로 저장하는 fetch_albums.py와 다른 점입니다.
- 이미 기록된 아티스트는 다시 조회하지 않습니다. 중단해도 다시 실행하면 이어서 진행합니다.
- 기록되지 않은 아티스트는 실패한 것으로 보고 다시 실행할 때 재시도합니다. 존재하지 않는 아티스트도 기록하지 않으므로 매번 다시 요청합니다.
- 요청 속도는 --concurrency로 조절하며, 요청이 제한되면 쉬었다가 몇 번 다시 시도합니다.
- 아티스트 이름의 표기 언어는 --locale(BCP-47 언어 태그, 기본값 ko)로 정합니다. 지정하지 않으면 영어 표기로 받습니다.
  언어를 바꿔 다시 받을 때는 이미 기록된 아티스트를 건너뛰므로 기존 출력 파일을 먼저 치워야 합니다.
"""

import argparse
import asyncio
import json
import signal
from pathlib import Path

from spotify_scraper import AsyncSpotifyClient
from spotify_scraper.errors import NotFoundError, RateLimitedError
from tqdm import tqdm

DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_INPUT = DATA_DIR / "albums.jsonl"
DEFAULT_OUTPUT = DATA_DIR / "artists.jsonl"
IMAGE_WIDTHS = (640, 320, 160)
RATE_LIMIT_PAUSE_SEC = 60  # 서버가 대기 시간을 알려 주지 않을 때 쉬는 시간
MAX_RATE_LIMIT_RETRIES = 3
MAX_CONSECUTIVE_FAILURES = 20  # 연속으로 이만큼 실패하면 차단이나 장애로 보고 중단합니다.


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="앨범에 나오는 모든 아티스트를 조회해 저장합니다.")
    parser.add_argument("--concurrency", type=int, default=3, help="동시 요청 수. 기본값은 3")
    parser.add_argument("--locale", default="ko", help="이름의 표기 언어(BCP-47). 기본값은 ko")
    parser.add_argument("--limit", type=int, help="이번 실행에서 조회할 최대 아티스트 수")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="앨범 JSONL 파일")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="결과 JSONL 파일")
    args = parser.parse_args()

    if args.concurrency < 1:
        parser.error("--concurrency는 1 이상이어야 합니다.")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit은 1 이상이어야 합니다.")
    if not args.input.is_file():
        parser.error(f"입력 파일이 없습니다: {args.input}")
    return args


def artist_id(artist: dict) -> str | None:
    """아티스트 참조의 ID를 돌려줍니다. id가 비어 있으면 uri(spotify:artist:ID)의 마지막 부분을 씁니다. 둘 다 없으면 None입니다."""
    if artist.get("id"):
        return artist["id"]
    uri = artist.get("uri") or ""
    return uri.rsplit(":", 1)[-1] if uri.startswith("spotify:artist:") else None


def read_artist_ids(path: Path) -> list[str]:
    """앨범 JSONL에서 앨범과 수록곡의 아티스트 ID를 처음 나온 순서대로, 중복 없이 읽습니다. 읽을 수 없는 줄은 무시합니다."""
    ids: dict[str, None] = {}
    references = no_id = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            album = json.loads(line)["album"]
            refs = list(album["artists"])
            for track in album["tracks"]:
                refs.extend(track["artists"])
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        for ref in refs:
            references += 1
            found = artist_id(ref)
            if found is None:
                no_id += 1
            else:
                ids.setdefault(found)
    print(f"{path.name}: 아티스트 참조 {references:,}개, 고유 아티스트 {len(ids):,}명, ID가 없어 건너뛴 참조 {no_id:,}개")
    return list(ids)


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


def to_record(artist) -> dict:
    """응답에서 저장할 값만 골라 한 줄의 기록으로 만듭니다."""
    data = artist.to_dict()
    image_urls = {image["width"]: image["url"] for image in data["images"]}
    return {
        "id": data["id"],
        "name": data["name"],
        **{f"image_url_{width}": image_urls.get(width) for width in IMAGE_WIDTHS},
        "monthly_listeners": data["monthly_listeners"],
        "top_tracks": data["top_tracks"],
    }


async def fetch_all(args: argparse.Namespace, artist_ids: list[str], already: int) -> None:
    args.output.parent.mkdir(parents=True, exist_ok=True)
    terminate_last_line(args.output)
    queue: asyncio.Queue[str] = asyncio.Queue()
    for requested_id in artist_ids:
        queue.put_nowait(requested_id)

    bar = tqdm(total=already + len(artist_ids), initial=already, unit="아티스트", dynamic_ncols=True)
    stats = {"ok": 0, "not_found": 0, "failed": 0, "consecutive_failures": 0}
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
        bar.set_postfix(성공=stats["ok"], 없음=stats["not_found"], 실패=stats["failed"])

    async def fetch_one(client: AsyncSpotifyClient, requested_id: str) -> None:
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            try:
                artist = await client.get_artist(requested_id, locale=args.locale)
            except NotFoundError:
                stats["not_found"] += 1
                stats["consecutive_failures"] = 0  # 아티스트가 없는 것은 차단이나 장애의 신호가 아닙니다.
                tqdm.write(f"없음: {requested_id}")
                advance()
                return
            except RateLimitedError as error:
                if attempt == MAX_RATE_LIMIT_RETRIES:
                    break
                wait = getattr(error, "retry_after", None) or RATE_LIMIT_PAUSE_SEC
                tqdm.write(f"요청 제한을 받아 {wait:g}초 쉽니다. ({requested_id})")
                await asyncio.sleep(wait)
            except Exception as error:  # 어떤 실패도 한 아티스트의 실패로만 다루고 다음으로 넘어갑니다.
                tqdm.write(f"실패: {requested_id} - {type(error).__name__}: {str(error)[:120]}")
                break
            else:
                record = {"artist_id": requested_id, "artist": to_record(artist)}
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
                output.flush()
                stats["ok"] += 1
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
                requested_id = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            await fetch_one(client, requested_id)

    try:
        with args.output.open("a", encoding="utf-8") as output:
            async with AsyncSpotifyClient() as client:
                await asyncio.gather(*(worker(client) for _ in range(args.concurrency)))
    finally:
        bar.close()

    state = "중단되었습니다" if stop.is_set() else "끝났습니다"
    print(
        f"\n{state}. 성공 {stats['ok']:,}명, 없음 {stats['not_found']:,}명, 실패 {stats['failed']:,}명 "
        "(없음과 실패는 기록하지 않으므로 다시 실행하면 재요청합니다.)"
    )
    print(f"결과 파일: {args.output}")


def main() -> None:
    args = parse_args()
    all_ids = read_artist_ids(args.input)
    done = read_done_ids(args.output)
    pending = [found for found in all_ids if found not in done]
    already = len(all_ids) - len(pending)
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
