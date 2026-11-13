-- 데이터셋 v3를 적재하던 시점(2026-11-12)의 tracks_v3 테이블 정의입니다. 보존용 기록이며 데이터는 포함하지 않습니다.
-- capstone2-backend의 src/db/schema.sql에서 tracks_v3 부분만 가져온 것(커밋 fd88562 시점)이고,
-- data/tracks_v3_20261112_153244.dump의 테이블 구조와 같습니다.
-- 이후 artist_reccobeats_ids는 artist_spotify_ids로, popularity는 play_count로 바뀌었으므로 현재 구조는 backend의 schema.sql을 따릅니다.
-- load-tracks.mjs는 이 구조를 전제로 적재합니다.

-- 곡. 연주곡, 같은 곡의 여러 버전도 걸러내지 않고 저장하며
-- 서비스에 노출할 곡은 조회할 때 정합니다. 오디오 값은 ReccoBeats 응답 원본입니다.
CREATE TABLE IF NOT EXISTS tracks_v3 (
    spotify_id text PRIMARY KEY CHECK (spotify_id ~ '^[0-9A-Za-z]{22}$'),
    reccobeats_id uuid NOT NULL,
    title text NOT NULL CHECK (btrim(title) <> ''),
    artist text NOT NULL CHECK (btrim(artist) <> ''), -- 참여 아티스트 이름을 ', '로 이은 문자열
    artist_reccobeats_ids uuid[] NOT NULL CHECK (cardinality(artist_reccobeats_ids) > 0), -- artist와 같은 순서
    duration_ms integer NOT NULL CHECK (duration_ms >= 0),
    popularity integer CHECK (popularity BETWEEN 0 AND 100),
    isrc text,
    acousticness double precision CHECK (acousticness BETWEEN 0 AND 1),
    danceability double precision CHECK (danceability BETWEEN 0 AND 1),
    energy double precision NOT NULL CHECK (energy BETWEEN 0 AND 1),
    instrumentalness double precision CHECK (instrumentalness BETWEEN 0 AND 1),
    liveness double precision CHECK (liveness BETWEEN 0 AND 1),
    loudness double precision, -- dB
    speechiness double precision CHECK (speechiness BETWEEN 0 AND 1),
    tempo double precision CHECK (tempo >= 0), -- BPM
    valence double precision NOT NULL CHECK (valence BETWEEN 0 AND 1),
    musical_key smallint CHECK (musical_key BETWEEN -1 AND 11), -- 0=C, ..., 11=B, -1=감지 실패
    musical_mode smallint CHECK (musical_mode IN (0, 1)) -- 0=단조, 1=장조
);

CREATE INDEX IF NOT EXISTS tracks_v3_artists_idx ON tracks_v3 USING gin (artist_reccobeats_ids);
