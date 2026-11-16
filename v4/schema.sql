-- 앨범. 곡이 속한 앨범이며, 큰 커버 이미지는 여기서 돌려줍니다.
CREATE TABLE IF NOT EXISTS albums_v4 (
    spotify_id text PRIMARY KEY CHECK (spotify_id ~ '^[0-9A-Za-z]{22}$'),
    name text NOT NULL CHECK (btrim(name) <> ''),
    type text NOT NULL CHECK (type IN ('album', 'single', 'ep', 'compilation')),
    artist text NOT NULL CHECK (btrim(artist) <> ''), -- 앨범 아티스트 이름을 ', '로 이은 표시용 문자열
    artist_spotify_ids text[] NOT NULL CHECK (cardinality(artist_spotify_ids) > 0), -- artist와 같은 순서
    release_date date,
    cover_url_640 text,
    cover_url_300 text,
    cover_url_64 text
);

-- 곡. 서비스에서 추천하고 재생하는 단위입니다. valence, energy는 ReccoBeats 값 그대로(0~1)입니다.
CREATE TABLE IF NOT EXISTS tracks_v4 (
    spotify_id text PRIMARY KEY CHECK (spotify_id ~ '^[0-9A-Za-z]{22}$'),
    reccobeats_id uuid, -- 오디오를 올려서 값을 얻은 곡은 없을 수 있음
    title text NOT NULL CHECK (btrim(title) <> ''),
    artist text NOT NULL CHECK (btrim(artist) <> ''), -- 참여 아티스트 이름을 ', '로 이은 표시용 문자열
    artist_spotify_ids text[] NOT NULL CHECK (cardinality(artist_spotify_ids) > 0), -- artist와 같은 순서
    duration_ms integer NOT NULL CHECK (duration_ms >= 0),
    play_count bigint NOT NULL DEFAULT 0 CHECK (play_count >= 0), -- Spotify 누적 재생 수. 값이 없는 곡은 0
    album_id text NOT NULL REFERENCES albums_v4 (spotify_id),
    album_thumb_url text, -- 목록 화면용 64픽셀 앨범 커버. 앨범 테이블을 조인하지 않으려고 곡에 둠
    valence double precision NOT NULL CHECK (valence BETWEEN 0 AND 1),
    energy double precision NOT NULL CHECK (energy BETWEEN 0 AND 1)
);

CREATE INDEX IF NOT EXISTS tracks_v4_artist_spotify_ids_idx ON tracks_v4 USING gin (artist_spotify_ids);
CREATE INDEX IF NOT EXISTS tracks_v4_album_id_idx ON tracks_v4 (album_id);
