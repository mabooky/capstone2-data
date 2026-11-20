-- 아티스트. 곡과 앨범의 artist_spotify_ids가 가리키는 아티스트이며, 블랙리스트 화면에서 이름과 사진을 보여 줄 때 씁니다.
CREATE TABLE IF NOT EXISTS artists_v4 (
    spotify_id text PRIMARY KEY CHECK (spotify_id ~ '^[0-9A-Za-z]{22}$'),
    name text NOT NULL CHECK (btrim(name) <> ''),
    -- 프로필 사진입니다. 사진이 없는 아티스트는 640픽셀만 있고, 그 이미지는 앨범 커버일 수 있습니다.
    image_url_640 text,
    image_url_320 text,
    image_url_160 text,
    monthly_listeners bigint CHECK (monthly_listeners >= 0), -- Spotify 월간 청취자 수. 값이 없으면 NULL
    top_track_ids text[] NOT NULL DEFAULT '{}' -- 인기곡의 Spotify 곡 ID를 인기 순으로 담습니다. tracks_v4에 없는 곡도 있을 수 있습니다.
);

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

-- 곡. 서비스에서 추천하고 재생하는 단위입니다. valence, arousal은 [-1, 1] 범위로 정규화된 값입니다.
CREATE TABLE IF NOT EXISTS tracks_v4 (
    spotify_id text PRIMARY KEY CHECK (spotify_id ~ '^[0-9A-Za-z]{22}$'),
    reccobeats_id uuid NOT NULL,
    title text NOT NULL CHECK (btrim(title) <> ''),
    artist text NOT NULL CHECK (btrim(artist) <> ''), -- 참여 아티스트 이름을 ', '로 이은 표시용 문자열
    artist_spotify_ids text[] NOT NULL CHECK (cardinality(artist_spotify_ids) > 0), -- artist와 같은 순서
    duration_ms integer NOT NULL CHECK (duration_ms >= 0),
    play_count bigint NOT NULL DEFAULT 0 CHECK (play_count >= 0), -- Spotify 누적 재생 수. 값이 없는 곡은 0
    album_id text NOT NULL REFERENCES albums_v4 (spotify_id),
    album_thumb_url text, -- 목록 화면용 64픽셀 앨범 커버. 앨범 테이블을 조인하지 않으려고 곡에 둠
    valence double precision NOT NULL CHECK (valence BETWEEN -1 AND 1),
    arousal double precision NOT NULL CHECK (arousal BETWEEN -1 AND 1)
);

CREATE INDEX IF NOT EXISTS tracks_v4_artist_spotify_ids_idx ON tracks_v4 USING gin (artist_spotify_ids);
CREATE INDEX IF NOT EXISTS tracks_v4_album_id_idx ON tracks_v4 (album_id);
