"""곡 DB(PostgreSQL) 연결을 만듭니다. tracks_v3 테이블은 capstone2-backend의 src/db/schema.sql이 기준입니다."""

import os
from pathlib import Path
from urllib.parse import urlparse

import psycopg

ENV_FILE = Path(__file__).resolve().parent / ".env"


def load_env_file(path: Path = ENV_FILE) -> None:
    """`.env`의 KEY=VALUE 줄을 환경변수로 읽습니다. 이미 설정된 환경변수는 덮어쓰지 않습니다."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _connection_options() -> tuple[str, str]:
    """접속 주소와 sslmode를 정합니다. 로컬 DB가 아니면 SSL로 접속하며, DATABASE_SSL=false로 끌 수 있습니다."""
    load_env_file()
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL 환경변수가 필요합니다. .env를 확인해 주십시오.")
    is_local = urlparse(url).hostname in ("localhost", "127.0.0.1", "::1")
    sslmode = "disable" if is_local or os.environ.get("DATABASE_SSL") == "false" else "require"
    return url, sslmode


def connect() -> psycopg.Connection:
    """DATABASE_URL로 접속합니다."""
    url, sslmode = _connection_options()
    return psycopg.connect(url, sslmode=sslmode, connect_timeout=5)


async def connect_async() -> psycopg.AsyncConnection:
    """DATABASE_URL로 비동기 접속합니다. 문장마다 바로 커밋하는 연결입니다."""
    url, sslmode = _connection_options()
    return await psycopg.AsyncConnection.connect(url, sslmode=sslmode, connect_timeout=5, autocommit=True)
