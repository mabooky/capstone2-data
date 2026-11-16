# 데이터셋 v4

SpotifyScraper로 한국 아티스트의 모든 곡 정보를 수집하는 파이프라인입니다.
수집한 자료는 `data/`에 저장되며, 용량이 커서 저장소에 올리지 않으므로 아래 순서대로 실행해 다시 만듭니다.

## 준비

`requirements.txt` 파일이 저장소 루트에 있어 모든 명령은 저장소 루트에서 실행하는 것을 권장합니다.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 실행 순서

| 순서 | 스크립트 | 입력 | 출력 |
|---|---|---|---|
| 1 | `fetch_discographies.py` | `seed-artists.csv` | `data/discographies.jsonl` |
| 2 | `fetch_albums.py` | `data/discographies.jsonl` | `data/albums.jsonl` |
| 3 | `flatten_tracks.py` | `data/albums.jsonl` | `data/tracks.jsonl` |

```bash
python v4/fetch_discographies.py
python v4/fetch_albums.py
python v4/flatten_tracks.py
```

### 1. 아티스트별 디스코그래피

`seed-artists.csv`의 아티스트마다 앨범 목록(ID, 이름, 이미지)을 조회합니다. ID가 비었거나 중복된 행은 건너뜁니다.
한 줄이 아티스트 한 명이며 `{"artist_id", "discography": [...]}` 형식입니다.

### 2. 앨범 상세

디스코그래피의 모든 앨범(싱글, EP, 컴필레이션 포함)을 중복 없이 모아 앨범마다 상세 정보를 조회합니다.
한 줄이 앨범 하나이며 `{"album_id", "album": {...}}` 형식이고, `album`은 수록곡 전체를 포함한 SpotifyScraper의 응답 그대로입니다.
시간이 가장 오래 걸리는 단계로, 동시 요청 3개 기준 약 2시간입니다.

### 3. 곡 단위로 펼치기

앨범의 수록곡을 곡마다 한 줄로 펼칩니다. 곡을 걸러내지 않으며, 한 줄은 다음 형식입니다.

```json
{"id", "title", "artists": [{"id", "name"}], "duration_ms", "play_count", "album_id", "album_thumb_url"}
```

앨범 안의 곡 순서는 담지 않습니다. `albums.jsonl`의 수록곡 배열 순서가 그 순서입니다.

## 수집 스크립트의 공통 동작

- 이미 기록된 항목은 다시 조회하지 않으므로, 중간에 멈춰도 같은 명령을 다시 실행하면 이어서 진행합니다. Ctrl+C를 한 번 누르면 진행 중인 요청만 마치고 멈춥니다.
- 조회에 실패한 항목은 기록하지 않으며 다시 실행할 때 재시도합니다.
- 요청 속도는 `--concurrency`(기본값 3)로 조절합니다. 요청이 제한되면 쉬었다가 다시 시도합니다.
- `--limit 개수`로 이번 실행에서 조회할 양을 제한할 수 있어 소량으로 먼저 시험해 볼 수 있습니다.

## 다른 파일

- `seed-artists.csv`: 수집 대상인 한국 아티스트 목록(`아티스트,아티스트 Spotify ID`)이며, 저장소에 포함되어 있습니다.
- `collect_seed_artists.py`: 이 목록을 만들 때 쓴 스크립트입니다. Spotify 한국 차트의 아티스트 중 목록에 없는 아티스트를 찾아 추가합니다. 차트가 주간으로 바뀌어 실행할 때마다 결과가 다르므로 재현에는 필요하지 않습니다. 해외 아티스트는 직접 걸러냈습니다.
- `schema.sql`: DB 테이블(`albums_v4`, `tracks_v4`) 정의입니다. 아직 DB에 적재하는 단계는 없습니다.

## 아직 없는 단계

- 곡별 valence, energy 수집(ReccoBeats)
- 같은 곡의 중복 묶음 처리
- DB 적재
