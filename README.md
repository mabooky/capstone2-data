# capstone2-data

스마트 주크박스의 곡 데이터를 수집하고 DB에 적재하는 파이프라인입니다. 서비스 서버(capstone2-backend)와 분리되어 있으며, 곡 DB(`tracks_v3`)에 접속해 작업합니다.

## 구성

| 파일 | 역할 |
|---|---|
| `fetch_spotify_tracks.py` | DB의 곡을 SpotifyScraper로 조회해 한글 표기를 포함한 응답을 `data/spotify_tracks.jsonl`에 저장합니다. DB는 읽기만 합니다. |
| `db.py` | `DATABASE_URL`로 DB에 접속합니다. |

`tracks_v3` 테이블의 정의는 capstone2-backend의 `src/db/schema.sql`이 기준입니다. 이 프로젝트는 만들어진 테이블에만 접속하며, 컬럼이 바뀌면 두 곳을 함께 확인해야 합니다.

## 준비

Python 3.10 이상이 필요합니다.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

`.env`의 `DATABASE_URL`을 채워 주십시오. `.env`와 `data/`는 git에 올리지 않습니다.

## 실행

```bash
python fetch_spotify_tracks.py --country-codes=KR,KS
```

| 옵션 | 설명 |
|---|---|
| `--country-codes` | ISRC가 지정한 국가 코드로 시작하는 곡만 조회합니다. 쉼표로 여러 개를 지정할 수 있으며, 지정하지 않으면 모든 곡을 조회합니다. |
| `--locale` | 표기 언어(BCP-47)입니다. 기본값은 `ko`입니다. |
| `--concurrency` | 동시 요청 수입니다. 기본값은 3입니다. |
| `--limit` | 이번 실행에서 조회할 최대 곡 수입니다. 처음에는 `--limit 20`으로 소량만 실행해 보기를 권합니다. |
| `--output` | 결과 파일 경로입니다. 기본값은 `data/spotify_tracks.jsonl`입니다. |

- 곡마다 한 줄씩 기록하며, 이미 기록된 곡은 건너뜁니다. 중단해도 다시 실행하면 이어서 진행합니다.
- 진행 막대에 전체 대상 중 처리한 곡 수(예: 1,200/33,900), 남은 시간, 성공·없음·실패 수가 실시간으로 표시됩니다.
- Ctrl+C를 한 번 누르면 진행 중인 요청만 마치고 멈춥니다.
- 존재하지 않는 곡은 `status: not_found`로 기록해 다시 요청하지 않습니다. 그 밖의 실패는 기록하지 않으므로 다시 실행하면 재시도합니다.
- 연속으로 20곡이 실패하면 차단이나 장애로 보고 중단합니다.
- 요청 속도는 SpotifyScraper의 기본 제한을 따릅니다.
