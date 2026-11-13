# 데이터셋 v1: 축소 음악 추천 실험 데이터셋

27,948곡의 Spotify Track ID와 5개 실험 모델 A/B1/B2/C1/C2를 재구성하는 데이터입니다.
기존 15필드는 `music-experiment-20261020-v3`의 PGlite VIEW에서 생성한 값이며,
여기에 MuSe 출처의 `title`, `artist`를 추가해 17열로 구성했습니다.
**검증된 운영 카탈로그가 아닙니다.** `verified=false`, `canonical_song_ids=false`이고
ID namespace는 `music_experiment.common_songs`입니다. Spotify에서의 현재 유효성, 녹음 동일성,
재생 가능성은 이 데이터셋으로 입증되지 않습니다.

이름에 대한 안내입니다. 이 디렉터리의 `v1`은 capstone2-data의 데이터셋 버전입니다.
`manifest.json`의 `format_version`(`music-compact-experiment-v2`)은 파일 형식의 버전이고,
`dataset_id`의 `v3`(`music-experiment-20261020-v3`)는 이 데이터를 만든 실험 스냅샷의 이름입니다.
세 값은 서로 관계가 없습니다.

## 파일과 형식

데이터 파일은 `data/` 아래에 있습니다. `data/`는 이 저장소의 `.gitignore` 대상이므로 이 저장소의 git에는 올라가지 않습니다.
원본은 capstone2-backend 저장소의 git 이력(`src/music/data/experiment-v1/`)에 있습니다.

- `data/songs.jsonl`: 행당 17열의 JSON 문자열 배열 1개입니다. 컬럼 순서는 `manifest.json`의 `columns`가 정의하며
  `title`, `artist`도 비어 있지 않은 문자열입니다. 모든 ID, 원본 점수, 좌표는 문자열입니다.
  bigint와 SQL numeric 정밀도를 유지하기 위해 반올림, 소수 변환, 좌표 재계산을 하지 않았습니다.
- `data/normalization.json`: 정규화 설정의 축별 템플릿 6개, 공통 MuSe source version, 모델별 축 조합을 보존합니다.
  곡별 ReccoBeats source version은 `songs.jsonl`에 그대로 남아 있습니다.
- `data/manifest.json`: 행 수, 컬럼, 버전, `songs.jsonl`과 `normalization.json`의 바이트 수·SHA-256,
  메타데이터 출처·매칭 기록, 과거 원본 DB 전수 대조 결과를 담습니다.
- `data/songs.csv`: `songs.jsonl`과 같은 내용을 사람이 열어 보기 쉽도록 CSV로 만든 사본입니다.
  생성 도구의 출력이 아니며 `manifest.json`에 해시가 기록되어 있지 않습니다.

문자열 배열의 순서는 다음과 같습니다(0부터 시작).

| 인덱스 | 값 |
|---|---|
| 0 | 실험용 song ID |
| 1 | Spotify Track ID |
| 2, 3 | ReccoBeats / MuSe source ID |
| 4 | 해당 곡의 ReccoBeats source version |
| 5, 6 | ReccoBeats 원본 Valence / Energy |
| 7, 8 | MuSe 원본 Valence / Arousal |
| 9, 10 | ReccoBeats 정규화 Valence / Energy |
| 11, 12 | MuSe 이론 정규화 Valence / Arousal |
| 13, 14 | MuSe 관측 정규화 Valence / Arousal |
| 15 | title: MuSe의 track |
| 16 | artist: MuSe의 artist |

모든 정규화 좌표는 -1~1입니다. 모델 A는 9/10, B1은 11/12, B2는 13/14, C1은 9/12, C2는 9/14를 사용합니다.
중복 축만 제거했으며 곡은 삭제하거나 병합하지 않았습니다.
`duration_ms`는 수집하지 않았으며 이 데이터셋에 포함하지 않습니다.

곡명과 아티스트는 MuSe `zenodo-4281165-v1.0.0`의 `muse_dataset.csv`에서 가져왔습니다.
`manifest.json`에는 Spotify ID의 정확한 일치, 원본·정제본 각각 단일 행 매칭,
ReccoBeats의 MuSe 외부 ID 교차 확인이 기록되어 있습니다. 이는 Spotify 공식 검증을 뜻하지 않으며
`metadata.spotify_officially_verified=false`입니다.

## 생성 과정

이 데이터는 다음 과정을 거쳐 만들어졌습니다.

1. **MuSe 정제:** MuSe 원본 `muse_dataset.csv` 90,408행을 곡마다 valence·arousal의 이론·관측 정규화 4개 점수로 변환하고,
   Spotify ID의 유무와 중복으로 분류했습니다. ID가 있는 행은 61,902개, 고유 ID는 60,741개이며,
   중복은 1,063그룹(2,224행)입니다. 중복 그룹은 병합, 평균, 대표 선정을 하지 않았습니다.
2. **ReccoBeats 수집:** 고유 ID 60,741개를 대상으로 ReccoBeats에서 곡 메타데이터와 특징(valence, energy)을 조회했습니다.
   체크포인트를 두고 100개, 1,000개, 10,000개, 60,741개로 단계적으로 넓혔고, 좌표를 확보한 곡이 27,948곡입니다.
3. **적재 준비와 DB 통합:** 수집 결과를 미검증(pending) 소스로 변환해 MuSe 소스와 함께 로컬 PGlite DB에 합쳤습니다.
   소스 118,356개, 점수 417,528개이며 이 시점에 `songs`는 비어 있습니다.
4. **실험 DB 생성:** 통합 DB를 바이트 단위로 복사한 스냅샷에서, 같은 Spotify ID를 가진 MuSe 단일행과 ReccoBeats 단일행의 쌍
   27,948개를 `music_experiment.common_songs`로 만들고 5개 모델의 VIEW를 정의했습니다.
5. **내보내기:** 스냅샷을 읽기 전용 사본으로 열어 모델 A, B1, B2의 행을 읽고 C1, C2는 축 조합으로 복원했습니다.
   복원한 값이 VIEW의 139,740행과 모두 일치하는지 검증한 뒤 `songs.jsonl`, `normalization.json`, `manifest.json`을 썼습니다.

### 재생성 구현은 포함하지 않았습니다

위 과정은 코드와 로컬 자료가 여러 단계에 걸쳐 있고 복잡해서, 데이터를 다시 만드는 구현은 이 저장소에 포함하지 않았습니다.
관련 코드는 capstone2-backend 저장소의 `archive/music-research-20261027` 브랜치(기준 커밋 `6eb1f96efa37bc1785f925a24039f46b7026fe3d`)의
`src/music/` 아래에서 확인할 수 있습니다.

| 단계 | 위치 |
|---|---|
| MuSe 정제 | `collectors/clean_muse.py` |
| ReccoBeats 수집 | `collectors/reccobeats_batch.py`, `reccobeats_research.py`, `reccobeats_full.py` |
| 적재 준비 | `collectors/stage_reccobeats_integration.py` |
| DB 통합 | `db/load_muse_batches.mjs`, `db/persistent_muse.mjs`, `db/integrate_local_music.mjs` |
| 실험 DB 생성 | `db/prepare_local_experiment.mjs`, `db/004_local_experiment.sql` |
| 내보내기 | `db/export_compact_experiment.mjs`, `db/compact_experiment.ts` |
| 상세 설명 | `SPEC.md`, `collectors/README.md`, `reports/REPRODUCING.md` |

참고할 점은 다음과 같습니다.

- 재생성에는 git에 포함되지 않은 로컬 자료(MuSe 원본, 수집 체크포인트, PGlite DB 스냅샷)가 필요하며 이 저장소에는 없습니다.
  따라서 `data/songs.jsonl`이 사실상 이 데이터의 기준 사본입니다. 이 저장소는 `data/`를 git에 올리지 않으므로,
  분실에 대비해 capstone2-backend의 git 이력도 보존해 두어야 합니다.
- 보관 브랜치의 내보내기 도구는 15열 형식(`music-compact-experiment-v1`)을 만듭니다.
  `title`, `artist`를 추가한 17열 형식(`music-compact-experiment-v2`)을 만든 도구는 이 브랜치에서 확인하지 못했습니다.

## 검증

`data/manifest.json`의 `artifacts`에 기록된 SHA-256과 비교해 파일이 변하지 않았는지 확인할 수 있습니다.

```bash
cd data
sha256sum songs.jsonl normalization.json
```

`manifest.json` 자체는 해시를 기록하지 않습니다. 파일을 수정하거나 줄바꿈을 바꾸면 해시가 달라지므로
이 파일들은 바꾸지 않고 보존합니다. 원본 저장소에서는 `.gitattributes`로 JSON/JSONL의 줄바꿈을 LF로 고정했으므로,
git으로 옮길 때에는 같은 설정이 필요합니다.

## 사용처

capstone2-data의 `v2/` 스크립트(`fetch-reccobeats-metadata.mjs`, `upload-songs.mjs`)가 곡 목록(Spotify ID, 제목, 아티스트)과
정규화 좌표를 이 데이터셋의 `songs.jsonl`에서 읽습니다.

## 출처와 사용 조건

- MuSe: Christopher Akiki, Manuel Burghardt, **MuSe: The Musical Sentiment Dataset**,
  Zenodo v1.0.0 (2021-03-01), https://zenodo.org/records/4281165,
  CC BY 4.0: https://creativecommons.org/licenses/by/4.0/.
  MuSe를 재사용할 때 저자, 원 출처, 라이선스, 변경 사실을 표시합니다.
- ReccoBeats: https://reccobeats.com,
  약관: https://reccobeats.com/docs/documentation/terms-of-service.
- 공유 권한: 프로젝트 사용자가 2026-10-27 대화에서 음악 데이터 공유 권한 확보를 확인하고,
  이 축소 데이터셋을 GitHub에 포함하도록 명시적으로 지시했습니다.
  별도 허가 문서, 허가자, 표준 라이선스명, 추가 귀속 문구는 제공되지 않았습니다.
  이 기록은 사용자 확인의 기록이며 제공자의 허가 원문을 대신하는 증서가 아닙니다.
  구체적인 허가 문서가 제공되면 이를 연결하고 그 조건을 함께 적용합니다.
- 이 데이터셋 전체에 MIT/ISC/CC0 등의 추가 오픈 라이선스를 임의로 부여하지 않았습니다.
  코드의 라이선스가 제3자 데이터의 권한을 대체하지 않으며,
  공유 승인 자체가 오디오, 이미지, 추가 Spotify API 데이터의 사용 권한을 뜻하지 않습니다.

변경 사항은 기존 공통곡 선택 유지, 고정 정규화 좌표 재배열, 중복 축 및 반복 설정 제거,
14필드 복원용 원본·출처 정보 보존, MuSe 곡명·아티스트 추가입니다.
원본 제공자의 추천이나 승인을 주장하지 않습니다.
원본 CSV, DB/WAL, SQLite 체크포인트, 응답 본문, 이미지, 인증정보는 포함하지 않습니다.
