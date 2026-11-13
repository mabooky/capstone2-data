"""Spotify 응답의 제목과 아티스트 이름을 tracks_v3의 title, artist에 반영합니다.

- 제목은 Spotify의 곡 이름, 아티스트는 Spotify의 아티스트 이름을 ', '로 이은 문자열로 항상 덮어씁니다.
- 이미 같은 값이면 쓰지 않습니다.
- artist_reccobeats_ids는 건드리지 않으므로, artist와 같은 순서라는 약속은 지켜지지 않을 수 있습니다.
"""

import asyncio
from collections.abc import Iterable

import psycopg
from tqdm import tqdm

from db import connect_async

# 곡 여러 개를 한 문장으로 갱신합니다. 갱신된 행 수(rowcount)가 실제로 바뀐 곡 수입니다.
UPDATE_SQL = """
UPDATE tracks_v3 AS t SET title = v.title, artist = v.artist
FROM unnest(%s::text[], %s::text[], %s::text[]) AS v(spotify_id, title, artist)
WHERE t.spotify_id = v.spotify_id AND (t.title IS DISTINCT FROM v.title OR t.artist IS DISTINCT FROM v.artist)
"""
BATCH_SIZE = 500
MAX_CONSECUTIVE_FAILURES = 20  # 연속으로 이만큼 실패하면 DB 장애로 보고 이번 실행의 반영을 멈춥니다.

Row = tuple[str, str, str]  # (spotify_id, title, artist)


def extract_names(track: dict) -> tuple[str, str] | None:
    """응답에서 (제목, 아티스트 이름들)을 읽습니다. 제목이나 아티스트가 비어 있으면 None입니다."""
    title = (track.get("name") or "").strip()
    names = [(artist.get("name") or "").strip() for artist in track.get("artists") or []]
    artist = ", ".join(name for name in names if name)
    return (title, artist) if title and artist else None


def describe(error: Exception) -> str:
    return f"{type(error).__name__}: {str(error).strip()[:160]}"


class DbSync:
    def __init__(self, connection: psycopg.AsyncConnection) -> None:
        self.connection = connection
        self.lock = asyncio.Lock()
        self.enabled = True
        self.consecutive_failures = 0
        self.changed = 0
        self.skipped = 0
        self.failed = 0

    @classmethod
    async def open(cls) -> "DbSync":
        return cls(await connect_async())

    async def close(self) -> None:
        await self.connection.close()

    async def _reconnect(self) -> None:
        try:
            await self.connection.close()
        except Exception:
            pass
        self.connection = await connect_async()

    async def _update(self, rows: list[Row]) -> int:
        async with self.lock:
            async with self.connection.transaction():
                cursor = await self.connection.execute(
                    UPDATE_SQL, ([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows])
                )
                return cursor.rowcount

    async def _run(self, rows: list[Row]) -> int:
        """갱신합니다. 연결이 끊겼으면 한 번 다시 접속해 재시도합니다."""
        try:
            return await self._update(rows)
        except (psycopg.OperationalError, psycopg.InterfaceError):
            await self._reconnect()
            return await self._update(rows)

    def _record_failure(self, track_id: str, error: Exception) -> None:
        self.failed += 1
        self.consecutive_failures += 1
        tqdm.write(f"DB 반영 실패: {track_id} - {describe(error)}")
        if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            self.enabled = False
            tqdm.write(
                f"연속 {MAX_CONSECUTIVE_FAILURES}곡의 DB 반영이 실패해 이번 실행에서는 DB 반영을 멈춥니다. "
                "다시 실행하면 JSONL을 처음부터 다시 적용합니다."
            )

    async def apply_one(self, track_id: str, track: dict) -> None:
        """방금 받은 곡 하나를 반영합니다. 실패하면 바로 출력하고, 수집은 계속합니다."""
        if not self.enabled:
            return
        names = extract_names(track)
        if names is None:
            self.skipped += 1
            tqdm.write(f"DB 반영 건너뜀(제목이나 아티스트가 비어 있음): {track_id}")
            return
        try:
            self.changed += await self._run([(track_id, *names)])
            self.consecutive_failures = 0
        except Exception as error:
            self._record_failure(track_id, error)

    async def apply_records(self, records: Iterable[tuple[str, dict]]) -> None:
        """JSONL에 있는 곡을 모두 반영합니다. 이미 같은 값인 곡은 쓰지 않습니다."""
        rows: list[Row] = []
        skipped = 0
        for track_id, track in records:
            names = extract_names(track)
            if names is None:
                skipped += 1
            else:
                rows.append((track_id, *names))
        self.skipped += skipped

        async with self.lock:
            cursor = await self.connection.execute("SELECT spotify_id FROM tracks_v3 WHERE spotify_id = ANY(%s)", ([r[0] for r in rows],))
            existing = {row[0] for row in await cursor.fetchall()}
        missing = sum(1 for row in rows if row[0] not in existing)
        rows = [row for row in rows if row[0] in existing]

        changed = failed = 0
        for start in tqdm(range(0, len(rows), BATCH_SIZE), desc="DB 반영", unit="묶음", dynamic_ncols=True):
            batch = rows[start : start + BATCH_SIZE]
            try:
                changed += await self._run(batch)
                continue
            except Exception:
                pass
            # 묶음 전체가 실패하면 곡마다 다시 시도해 문제가 되는 곡만 찾아냅니다.
            for row in batch:
                if not self.enabled:
                    failed += 1
                    continue
                try:
                    changed += await self._run([row])
                    self.consecutive_failures = 0
                except Exception as error:
                    failed += 1
                    self._record_failure(row[0], error)
        self.changed += changed
        unchanged = len(rows) - failed - changed
        print(
            f"DB 반영(JSONL {len(rows) + missing + skipped:,}곡): 변경 {changed:,}곡, 이미 같음 {unchanged:,}곡, "
            f"DB에 없음 {missing:,}곡, 건너뜀 {skipped:,}곡, 실패 {failed:,}곡"
        )
