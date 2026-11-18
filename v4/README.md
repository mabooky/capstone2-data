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

## 시드 아티스트 목록

수집 대상은 `seed-artists.csv`(`아티스트,아티스트 Spotify ID`)에 적힌 한국 아티스트이며, 이 파일은 저장소에 포함되어 있습니다. 다음 순서로 만들어졌습니다.

1. 처음 623행(머리글 포함)은 직접 수집했습니다. (커밋 `288e82f`)
2. 그 뒤는 `collect_seed_artists.py`로 Spotify 공식 차트 플레이리스트(주간 차트, Hot Hits Korea, 바이럴 히트, K-Pop ON! 등)의 아티스트 중 목록에 없는 아티스트를 찾아 덧붙였습니다. (커밋 `187ea69`, 2026-11-15 실행)
3. 이후 중복 행과 해외 아티스트를 제거해 지금의 목록이 되었습니다. (커밋 `96c03b6`, `ede9039`)

차트는 매주 바뀌어 스크립트를 다시 실행하면 결과가 달라지므로, 데이터를 재현할 때는 이 파일을 그대로 사용하고 스크립트는 실행하지 않습니다. 목록을 늘리고 싶을 때만 실행하며, `--yes`를 붙이지 않으면 CSV를 바꾸지 않고 추가될 아티스트를 미리 보여 주기만 합니다.

```bash
python v4/collect_seed_artists.py          # 추가될 아티스트 미리 보기
python v4/collect_seed_artists.py --yes    # CSV 끝에 덧붙이기
```

## 실행 순서

| 순서 | 스크립트 | 입력 | 출력 |
|---|---|---|---|
| 1 | `fetch_discographies.py` | `seed-artists.csv` | `data/discographies.jsonl` |
| 2 | `fetch_albums.py` | `data/discographies.jsonl` | `data/albums.jsonl` |
| 3 | `flatten_tracks.py` | `data/albums.jsonl` | `data/tracks.jsonl` |
| 4 | `fetch_audio_features.py` | `data/tracks.jsonl` | `data/audio_features.jsonl` |

```bash
python v4/fetch_discographies.py
python v4/fetch_albums.py
python v4/flatten_tracks.py
python v4/fetch_audio_features.py
```

### 1. 아티스트별 디스코그래피

`seed-artists.csv`의 아티스트마다 앨범 목록(ID, 이름, 이미지)을 조회합니다. ID가 비었거나 중복된 행은 건너뜁니다.
한 줄이 아티스트 한 명이며 `{"artist_id", "discography": [...]}` 형식입니다.

### 2. 앨범 상세

디스코그래피의 모든 앨범(싱글, EP, 컴필레이션 포함)을 중복 없이 모아 앨범마다 상세 정보를 조회합니다.
한 줄이 앨범 하나이며 `{"album_id", "album": {...}}` 형식이고, `album`은 수록곡 전체를 포함한 SpotifyScraper의 응답 그대로입니다.
시간이 가장 오래 걸리는 단계로, 동시 요청 3개 기준 약 2시간입니다.
이름의 표기 언어는 `--locale`(기본값 `ko`)로 지정할 수 있습니다.
언어를 바꿔 다시 받을 때는 이미 기록된 앨범을 건너뛰므로 `data/albums.jsonl`을 먼저 지워야 합니다.

### 3. 곡 단위로 펼치기

앨범의 수록곡을 곡마다 한 줄로 펼칩니다. 곡을 걸러내지 않으며, 한 줄은 다음 형식입니다.

```json
{"id", "title", "artists": [{"id", "name"}], "duration_ms", "play_count", "album_id", "album_thumb_url",
 "isrc", "valence", "arousal"}
```

`isrc`, `valence`, `arousal`은 이 단계에서 모두 `null`이며, 이후 단계가 같은 파일의 빈칸을 채웁니다.
`arousal`은 ReccoBeats의 `energy` 값을 쓰는 항목입니다.

앨범 안의 곡 순서는 담지 않습니다. `albums.jsonl`의 수록곡 배열 순서가 그 순서입니다.

### 4. 오디오 특징

곡의 Spotify ID를 40개씩 묶어 ReccoBeats에서 오디오 특징을 조회합니다. ReccoBeats에 있는 곡마다 한 줄을 기록합니다.

```json
{"spotify_id", "reccobeats_id", "isrc", "acousticness", "danceability", "energy", "instrumentalness",
 "key", "liveness", "loudness", "mode", "speechiness", "tempo", "valence"}
```

이 파일에서만 ID에 `spotify_`, `reccobeats_` 접두사를 붙입니다. 한 줄에 서로 다른 ID가 둘 있기 때문입니다.
ReccoBeats에 값이 없는 곡은 기록하지 않으므로 다시 실행하면 다시 요청합니다. 요청이 실패한 곡도 기록하지 않아 파일만으로는 두 경우를 구분할 수 없습니다.
따라서 실행이 끝날 때 출력되는 **실패 수가 0이 될 때까지** 다시 실행한 뒤 다음 단계로 넘어갑니다.

## 수집 스크립트의 공통 동작

- 이미 기록된 항목은 다시 조회하지 않으므로, 중간에 멈춰도 같은 명령을 다시 실행하면 이어서 진행합니다. Ctrl+C를 한 번 누르면 진행 중인 요청만 마치고 멈춥니다.
- 조회에 실패한 항목은 기록하지 않으며 다시 실행할 때 재시도합니다.
- 요청 속도는 `--concurrency`(기본값 3)로 조절합니다. 요청이 제한되면 쉬었다가 다시 시도합니다.
- `--limit 개수`로 이번 실행에서 조회할 양을 제한할 수 있어 소량으로 먼저 시험해 볼 수 있습니다.

## 다른 파일

- `schema.sql`: DB 테이블(`albums_v4`, `tracks_v4`) 정의입니다. 아직 DB에 적재하는 단계는 없습니다.

## 아직 없는 단계

- ReccoBeats에 있는 곡만 남기는 정제
- 같은 곡의 중복 묶음 처리
- DB 적재
