# 데이터셋 v4

SpotifyScraper로 한국 아티스트의 모든 곡 정보를 수집하고, ReccoBeats로 곡의 오디오 특징을 채워 곡 DB(PostgreSQL)에 올릴 자료를 만드는 파이프라인입니다.
수집한 자료는 `data/`에 저장되며, 아래 순서대로 실행해 다시 만들 수도 있습니다.

## 준비

`requirements.txt` 파일이 저장소 루트에 있어 모든 명령은 저장소 루트에서 실행하는 것을 권장합니다.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`upload_albums.py`는 저장소 루트의 `.env`(커밋되지 않음)에서 `DATABASE_URL`을 읽어 DB에 접속합니다.

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
| 3 | `flatten_tracks.py` | `data/albums.jsonl` | `data/tracks.csv` |
| 4 | `fill_using_reccobeats.py` | `data/tracks.csv` | `data/tracks.csv` (ReccoBeats 값을 채움) |

```bash
python v4/fetch_discographies.py
python v4/fetch_albums.py
python v4/flatten_tracks.py
python v4/fill_using_reccobeats.py
```

> **주의: `flatten_tracks.py`를 다시 실행하면 `data/tracks.csv`를 처음부터 새로 만들어, 4단계에서 채운 내용이 모두 사라집니다.**
> 앨범을 추가로 받은 뒤처럼 3단계를 다시 실행해야 한다면, 먼저 채워진 `tracks.csv`를 커밋해 두고 4단계를 다시 해야 한다는 점을 감안하십시오.
> 실수를 막기 위해 `tracks.csv`에 채워진 값이 하나라도 있으면 `flatten_tracks.py`는 아무것도 하지 않고 중단하며, 정말 새로 만들려면 `--force`를 붙여야 합니다.

DB에 올리는 `upload_albums.py`는 이 순서와 별개로, 2단계가 끝난 뒤 언제든 실행할 수 있습니다. ([DB 적재](#db-적재) 참고)

### 1. 아티스트별 디스코그래피

`seed-artists.csv`의 아티스트마다 앨범 목록(ID, 이름, 이미지)을 조회합니다. ID가 비었거나 중복된 행은 건너뜁니다.
한 줄이 아티스트 한 명이며 `{"artist_id", "discography": [...]}` 형식입니다.

### 2. 앨범 상세

디스코그래피의 모든 앨범(싱글, EP, 컴필레이션 포함)을 중복 없이 모아 앨범마다 상세 정보를 조회합니다.
한 줄이 앨범 하나이며 `{"album_id", "album": {...}}` 형식이고, `album`은 수록곡 전체를 포함한 SpotifyScraper의 응답 그대로입니다.
시간이 가장 오래 걸리는 단계로, 동시 요청 3개 기준 약 2시간입니다.
이름의 표기 언어는 `--locale`(기본값 `ko`)로 지정할 수 있습니다.
언어를 바꿔 다시 받을 때는 이미 기록된 앨범을 건너뛰므로 `data/albums.jsonl`을 먼저 지워야 합니다.
조회에 실패한 앨범은 기록되지 않으므로, 끝난 뒤 `data/albums.jsonl`의 앨범 수가 디스코그래피의 고유 앨범 수(실행 첫 줄에 출력됨)와 같은지 확인하고, 모자라면 다시 실행합니다.

### 3. 곡 단위로 펼치기

앨범의 수록곡을 곡마다 한 줄로 펼쳐 `tracks.csv`로 만듭니다. 곡을 걸러내지 않으며, 열은 DB의 `tracks_v4` 테이블과 같은 이름과 순서입니다.

```csv
spotify_id,title,artist,artist_spotify_ids,duration_ms,play_count,album_id,album_thumb_url,reccobeats_id,valence,arousal
```

- `artist`는 곡의 아티스트 이름을 ', '로 이은 표시용 문자열이고, `artist_spotify_ids`는 같은 순서의 Spotify ID를 `{id1,id2}` 형식(PostgreSQL 배열)으로 적은 것입니다.
- 수록곡의 아티스트는 ID가 비어 있어 `uri`(`spotify:artist:ID`)에서 읽습니다.
- `album_thumb_url`은 앨범 커버 중 가로 64픽셀 이미지의 URL입니다. 300, 640픽셀 이미지는 `albums_v4`에 있습니다.
- `play_count`가 없는 곡은 0으로 적습니다.
- `reccobeats_id`, `valence`, `arousal`은 이 단계에서 모두 비어 있으며, 4단계가 같은 파일의 빈칸을 채웁니다.
- 앨범 안의 곡 순서는 담지 않습니다. `albums.jsonl`의 수록곡 배열 순서가 그 순서입니다.

실행할 때마다 `tracks.csv`를 처음부터 새로 만들며, 채워진 값이 있으면 `--force` 없이는 중단합니다. (위 주의 참고)

### 4. ReccoBeats 값 채우기

`tracks.csv`의 곡 중 `reccobeats_id`가 빈 곡의 Spotify ID를 40개씩 묶어 ReccoBeats에 조회하고, 응답에 있는 곡의 `reccobeats_id`, `valence`, `arousal`을 채웁니다.

- `arousal`은 ReccoBeats의 `energy` 값입니다. `valence`와 `arousal`은 DB 스키마와 같게 ReccoBeats의 0~1 값을 [-1, 1] 범위로 바꾸어 적습니다.
- ISRC와 응답의 다른 오디오 특징(`danceability`, `tempo` 등)은 쓰지 않으므로 담지 않습니다.
- ReccoBeats에 곡은 있지만 `valence`나 `energy`가 없는 곡은 `reccobeats_id`만 채우고 `valence`, `arousal`은 빈칸으로 둡니다.
- ReccoBeats에 값이 없는 곡은 응답에 나오지 않아 빈칸으로 남고, 다시 실행하면 다시 요청합니다. 요청이 실패한 곡도 빈칸으로 남아 파일만으로는 두 경우를 구분할 수 없습니다.
  따라서 실행이 끝날 때 출력되는 **실패 수가 0이 될 때까지** 다시 실행한 뒤 다음 단계로 넘어갑니다.
- 2026-11-19 기준으로 70,339곡 중 53,106곡이 채워졌고(그 중 38곡은 `valence`, `arousal`이 빈칸), 17,233곡은 ReccoBeats에 없습니다.
- ISRC는 수집하지 않습니다. 같은 ISRC에 서로 다른 곡이 섞여 있고, ReccoBeats에 값이 없는 곡의 `valence`, `arousal`을 다른 곡에서 복사하는 데에도 도움이 되지 않았기 때문입니다. (2026-11-19 확인)

## DB 적재

`upload_albums.py`는 `data/albums.jsonl`의 앨범을 DB의 `albums_v4` 테이블에 올립니다.
`DATABASE_URL`이 가리키는 DB에 씁니다. 기본으로는 DB에 접속하지 않고 접속 대상(호스트, 포트, DB 이름)과 올릴 내용만 미리 보여 주며, `--yes`를 붙여야 실제로 올립니다.
`albums_v4`가 없으면 만들고, 앨범을 모두 올립니다. 이미 있는 앨범은 새 값으로 고칩니다. 테이블 생성과 적재는 한 트랜잭션이라 실패하면 아무것도 반영되지 않습니다.

```bash
python v4/upload_albums.py          # 미리 보기
python v4/upload_albums.py --yes    # 실제로 올리기
```

`upload_tracks.py`는 `tracks.csv`에서 `valence`, `arousal`이 있는 곡을 `tracks_v4` 테이블에 올립니다. 사용법과 안전장치는 `upload_albums.py`와 같습니다(`--yes`가 없으면 미리 보기).
곡의 `album_id`가 가리키는 앨범이 `albums_v4`에 모두 있어야 하므로 `upload_albums.py`를 먼저 실행합니다. 앨범이 없으면 아무것도 올리지 않고 알려 줍니다.

```bash
python v4/upload_tracks.py          # 미리 보기
python v4/upload_tracks.py --yes    # 실제로 올리기
```

## 수집 스크립트의 공통 동작

- 1, 2단계는 결과를 JSONL에 한 줄씩 기록합니다. 이미 기록된 항목은 다시 조회하지 않으므로, 중간에 멈춰도 같은 명령을 다시 실행하면 이어서 진행합니다.
- 4단계는 `tracks.csv`의 빈칸을 채우며, 채운 값이 일정 개수 쌓일 때마다 그리고 종료할 때 파일을 저장합니다. 임시 파일에 쓴 뒤 교체하므로 도중에 멈춰도 파일이 깨지지 않으며, 다시 실행하면 빈칸인 곡부터 이어서 진행합니다. 실행하는 동안에는 `tracks.csv`를 다른 곳에서 수정하지 않아야 합니다.
- Ctrl+C를 한 번 누르면 진행 중인 요청만 마치고 저장한 뒤 멈추며, 한 번 더 누르면 즉시 종료합니다.
- 조회에 실패한 항목은 기록하지 않거나 빈칸으로 남기며 다시 실행할 때 재시도합니다.
- 요청이 제한되면 쉬었다가 다시 시도합니다. 요청 속도는 1, 2, 4단계에서 `--concurrency`(기본값 3)로 조절합니다.
- `--limit 개수`로 이번 실행에서 조회할 양을 제한할 수 있어 소량으로 먼저 시험해 볼 수 있습니다.

## 같은 곡 판단에 관해...

같은 곡의 여러 버전(원곡, Inst., Live, 언어 버전 등)을 같은 곡으로 판단할 수 있는 단서를 조사한 기록입니다.
아래 수치는 이 날짜의 `tracks.csv`(70,339곡)로 센 값이며, 데이터가 바뀌면 달라집니다. 이 조사에서 "묶음"은 같은 곡으로 보이는 곡들의 모음을 뜻하고, 이를 저장하는 파일은 두지 않았습니다.

- **ISRC가 같음**: 같은 녹음이면 같은 ISRC이지만 믿기 어렵습니다. ISRC가 있는 54,687곡 중 2곡 이상 묶음은 2,732개이고, 길이 차이가 3초를 넘는 묶음이 101개(3.7%), 10초를 넘는 묶음이 21개입니다. 같은 ISRC인데 아예 다른 곡이 섞인 사례도 있습니다. (유키스 "Without You": 약 20초짜리 인트로 트랙이 원곡과 같은 ISRC. 직접 들어서 다른 곡임을 확인) 그래서 ISRC는 수집하지 않기로 했습니다.
- **제목, 아티스트 집합, 0 초과의 같은 `play_count`**: 가장 많이 잡히는 단서입니다. 5,667개 묶음(13,229곡, 제거 가능 7,562곡)이 나오고 길이 차이가 10초를 넘는 묶음은 18개뿐입니다. ISRC가 있는 곡이 들어 있는 4,813개 묶음 중 2,414개에는 서로 다른 ISRC가 섞여 있어, ISRC로는 놓칠 중복을 잡습니다.
- **`play_count`가 같다는 사실만으로는 같은 녹음이 아닙니다**: 언어 표식만 빼면 제목과 아티스트 집합이 같은 355개 묶음 중 원곡과 `play_count`가 같은 묶음은 42개(12%)이고 313개(88%)는 다릅니다. 같은 경우 중에는 BLACKPINK "Lovesick Girls"와 "Lovesick Girls - JP Ver."처럼 가사 언어가 다른 별개의 녹음이 있습니다. (직접 들어서 확인) 아티스트 집합과 `play_count`만 같고 제목이 다른 묶음 2,497개는 대부분 같은 곡의 표기 차이이지만 Inst., Live, Remix, 언어 버전이 섞인 묶음이 300개 안팎입니다. 따라서 제목이 다른 곡을 `play_count`만으로 묶지 않습니다.
- **미리듣기 파일 ID**: `get_track`의 `preview_url` 끝부분(20바이트 파일 ID)은 곡 ID에서 유도하는 규칙을 찾지 못했고, 곡마다 조회해야 얻을 수 있습니다. 같은 ID면 같은 음원이었고(V/A가 모두 같음), 다르다고 다른 곡은 아니었습니다. 같은 녹음으로 보이는 곡 쌍의 18%(표본 30묶음)만 같은 ID를 가졌습니다. 표본이 작아 방향성만 보아야 합니다. 표본 85곡 모두 미리듣기가 있었습니다.
- **(V, A) 거리**: 같은 녹음이어도 ReccoBeats 값이 판본마다 어긋납니다. 위 "제목, 아티스트 집합, 같은 `play_count`" 단서의 묶음에서 거리가 0.1을 넘는 묶음이 22%, 0.3을 넘는 묶음이 5%입니다. V/A는 중복 판정에 쓰기에는 잡음이 큽니다.
- **길이**: 같은 녹음 묶음은 대체로 길이가 같지만(ISRC 묶음 중 (V, A) 거리 0.1 이하인 묶음의 99%가 길이 차이 3초 이하), 같은 곡의 다른 판본(리마스터, 편집)은 달라집니다.

## 다른 파일

- `seed-artists.csv`, `collect_seed_artists.py`: 시드 아티스트 목록과 그 목록을 늘리는 스크립트입니다.
- `db.py`: `.env`를 읽고 DB에 접속하는 공통 모듈입니다. `upload_albums.py`, `upload_tracks.py`가 씁니다.
- `schema.sql`: DB 테이블(`albums_v4`, `tracks_v4`)의 형식을 보여 주는 파일이며 실행용이 아닙니다. 실제로 테이블을 만드는 문장은 `upload_albums.py`(`albums_v4`)와 `upload_tracks.py`(`tracks_v4`)에 있습니다.
