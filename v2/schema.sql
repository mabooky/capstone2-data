-- 곡 카탈로그 테이블 정의입니다. 데이터는 포함하지 않으며, 여러 번 실행해도 안전합니다.
-- 업로드 스크립트(upload-songs.mjs)가 트랜잭션 안에서 이 파일을 먼저 실행합니다.
-- 한국(KR)에서 재생할 수 없는 곡은 업로드 단계에서 걸러내므로 이 테이블에 들어오지 않습니다.

CREATE TABLE IF NOT EXISTS songs (
    spotify_id text PRIMARY KEY CHECK (spotify_id ~ '^[0-9A-Za-z]{22}$'),
    reccobeats_id uuid NOT NULL,
    title text NOT NULL CHECK (btrim(title) <> ''),
    artist text NOT NULL CHECK (btrim(artist) <> ''),                      -- 아티스트 이름을 ', '로 이은 문자열
    duration_ms integer NOT NULL CHECK (duration_ms >= 0),
    popularity integer CHECK (popularity BETWEEN 0 AND 100),
    valence double precision NOT NULL CHECK (valence BETWEEN -1 AND 1),    -- ReccoBeats, [-1, 1]
    energy double precision NOT NULL CHECK (energy BETWEEN -1 AND 1)       -- ReccoBeats, [-1, 1] (arousal로 사용)
);
