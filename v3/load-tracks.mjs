// 시드 CSV의 아티스트에서 곡과 오디오 값을 수집해 tracks_v3에 적재합니다.
//
//   node --env-file=.env v3/load-tracks.mjs [옵션]
//
//   --csv <파일>: 시드 CSV(장르, 아티스트, 아티스트 Spotify ID). 기본값은 seed-artists.csv
//   --genre <장르>: 해당 장르의 시드만 처리
//   --limit <개수>: 앞에서부터 지정한 수의 시드만 처리
//   --top <개수>: 아티스트당 인기도 상위 곡만 유지. 기본값은 전체
//   --show <개수>: 미리보기에서 아티스트별 인기도 상위 곡을 출력
//   --tracks-file <파일>: 수집한 곡 목록(JSON). 기본값은 v3/data/collected-tracks.json
//   --features-file <파일>: 받은 오디오 값(JSONL). 기본값은 v3/data/audio-features.jsonl
//   --yes: DB에 적재합니다. 지정하지 않으면 DB를 변경하지 않습니다.
//
// 단계
//   1. 수집: 곡 목록 파일이 없으면 시드 아티스트의 곡 전체를 수집해 파일로 저장합니다. 파일이 있으면 이 단계를 건너뛰고
//      --csv, --genre, --limit, --top을 무시합니다. 다시 수집하려면 파일을 삭제합니다.
//      시드 아티스트를 ReccoBeats에서 하나라도 찾지 못하면 수집하지 않고 중단합니다.
//   2. 오디오 값: 40곡 단위로 병렬 요청하고, 받은 즉시 곡마다 한 줄씩 파일에 기록합니다.
//      기록된 곡은 다시 받지 않으므로 중단된 실행을 이어서 진행할 수 있습니다.
//   3. 적재: 오디오 값이 있는 곡만 적재합니다. tracks_v3는 capstone2-backend의 src/db/schema.sql로 미리 만들어 두어야 합니다.
//
// 적재 규칙
//   - 오디오 값은 응답 원본 그대로 저장합니다. key, mode는 musical_key, musical_mode 열에 저장합니다.
//   - 한국에서 재생할 수 없는 곡(availableCountries에 KR이 없는 곡)은 오디오 값을 받기 전에 제외하고 적재하지 않습니다.
//   - 연주곡, 같은 곡의 여러 버전은 걸러내거나 합치지 않습니다.
//   - 같은 spotify_id가 이미 있으면 artist_reccobeats_ids와 오디오 값만 갱신합니다.
//
// 시드 CSV의 아티스트 ID는 22자 ID, spotify:artist:ID, 아티스트 페이지 링크를 받으며 빈 행은 건너뜁니다.
//
// 환경변수: DATABASE_URL(없으면 미리보기만 가능), DATABASE_SSL=false

import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const DEFAULT_SEED_CSV = resolve(HERE, 'seed-artists.csv');
// 수집한 곡 목록과 오디오 값은 git에 올리지 않도록 data 폴더에 둡니다.
const DATA_DIR = resolve(HERE, 'data');
const DEFAULT_TRACKS_FILE = resolve(DATA_DIR, 'collected-tracks.json');
const DEFAULT_FEATURES_FILE = resolve(DATA_DIR, 'audio-features.jsonl');
const API_BASE_URL = 'https://api.reccobeats.com/v1';
const TABLE = 'tracks_v3';

const TRACK_BATCH_SIZE = 40; // ids 쿼리로 한 번에 조회할 수 있는 최대 개수
const ARTIST_PAGE_SIZE = 50;
const CONCURRENCY = 3;
const MIN_INTERVAL_MS = 120; // 요청 사이의 최소 간격(초당 약 8회)
const MAX_ATTEMPTS = 5;
const INSERT_BATCH_SIZE = 500;

const SPOTIFY_ID_PATTERN = /^[0-9A-Za-z]{22}$/;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
// /audio-features 응답에서 저장하는 항목
const FEATURE_FIELDS = ['acousticness', 'danceability', 'energy', 'instrumentalness', 'key', 'liveness', 'loudness', 'mode', 'speechiness', 'tempo', 'valence'];
const FEATURE_COLUMNS = ['acousticness', 'danceability', 'energy', 'instrumentalness', 'liveness', 'loudness', 'speechiness', 'tempo', 'valence', 'musical_key', 'musical_mode'];
const INSERT_COLUMNS = [
    'spotify_id', 'reccobeats_id', 'title', 'artist', 'artist_reccobeats_ids',
    'duration_ms', 'popularity', 'isrc', ...FEATURE_COLUMNS,
];

const USAGE = `사용법:
  node --env-file=.env v3/load-tracks.mjs [--csv <시드 CSV>] [--genre <장르>] [--limit <시드 수>] [--top <곡 수>] [--show <곡 수>] [--tracks-file <파일>] [--features-file <파일>] [--yes]`;

function fail(message) {
    console.error(message);
    process.exit(1);
}

function sleep(ms) {
    return new Promise(done => setTimeout(done, ms));
}

// 터미널이면 진행 줄을 덮어쓰고, 아니면 일정 간격마다 새 줄로 출력합니다.
const isTty = Boolean(process.stdout.isTTY);
let statusActive = false;

function writeStatus(text) {
    process.stdout.write(`\r\x1b[K${text}`);
    statusActive = true;
}

function endStatus() {
    if (statusActive) process.stdout.write('\r\x1b[K');
    statusActive = false;
}

function formatDuration(seconds) {
    if (seconds === null || !Number.isFinite(seconds)) return '계산 중';
    const total = Math.max(0, Math.round(seconds));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    if (hours > 0) return `${hours}시간 ${minutes}분`;
    if (minutes > 0) return `${minutes}분 ${total % 60}초`;
    return `${total}초`;
}

// ---------- 명령줄 ----------

function parseArgs(argv) {
    const options = {
        csv: DEFAULT_SEED_CSV, genre: undefined, limit: undefined, top: undefined, show: 0,
        tracksFile: DEFAULT_TRACKS_FILE, featuresFile: DEFAULT_FEATURES_FILE, yes: false,
    };
    const withValue = new Set(['--csv', '--genre', '--limit', '--top', '--show', '--tracks-file', '--features-file']);
    const toPath = value => resolve(value.replace(/^~(?=\/|$)/, process.env.HOME ?? '~'));
    for (let i = 0; i < argv.length; i++) {
        const flag = argv[i];
        if (flag === '--yes') options.yes = true;
        else if (withValue.has(flag)) {
            const value = argv[++i];
            if (value === undefined) fail(`${flag}에 값이 필요합니다.\n${USAGE}`);
            if (flag === '--genre') options.genre = value;
            else if (flag === '--csv') options.csv = toPath(value);
            else if (flag === '--tracks-file') options.tracksFile = toPath(value);
            else if (flag === '--features-file') options.featuresFile = toPath(value);
            else options[flag.slice(2)] = Number(value);
        } else fail(`알 수 없는 옵션입니다: ${flag}\n${USAGE}`);
    }
    if (options.limit !== undefined && !(options.limit >= 1)) fail('--limit은 1 이상의 숫자여야 합니다.');
    if (options.top !== undefined && !(options.top >= 1)) fail('--top은 1 이상의 숫자여야 합니다.');
    if (!(options.show >= 0)) fail('--show는 0 이상의 숫자여야 합니다.');
    return options;
}

// ---------- CSV (시드 읽기용) ----------

/** 따옴표와 줄바꿈을 처리하는 최소 CSV 파서입니다. 열이 모자란 행도 그대로 반환합니다. */
function parseCsv(text) {
    const rows = [];
    let row = [];
    let field = '';
    let quoted = false;
    const source = text.replace(/^﻿/, '');
    for (let i = 0; i < source.length; i++) {
        const char = source[i];
        if (quoted) {
            if (char === '"' && source[i + 1] === '"') { field += '"'; i++; }
            else if (char === '"') quoted = false;
            else field += char;
        } else if (char === '"') quoted = true;
        else if (char === ',') { row.push(field); field = ''; }
        else if (char === '\n' || char === '\r') {
            if (char === '\r' && source[i + 1] === '\n') i++;
            row.push(field); field = '';
            if (row.some(cell => cell !== '')) rows.push(row);
            row = [];
        } else field += char;
    }
    row.push(field);
    if (row.some(cell => cell !== '')) rows.push(row);
    return rows;
}

// ---------- ReccoBeats ----------

let lastRequestAt = 0;
let pacing = Promise.resolve();
let blockedUntil = 0; // 한도 초과(429)를 받으면 이 시각까지 모든 요청이 대기합니다.
let rateLimitHits = 0;
let rateLimitQuiet = false; // 진행 줄을 그리는 동안에는 한도 초과 안내를 출력하지 않습니다.

/** 요청을 최소 간격을 두고 순서대로 내보냅니다. */
function paceRequests() {
    const turn = pacing.then(async () => {
        const wait = Math.max(MIN_INTERVAL_MS - (Date.now() - lastRequestAt), blockedUntil - Date.now());
        if (wait > 0) await sleep(wait);
        lastRequestAt = Date.now();
    });
    pacing = turn.catch(() => {});
    return turn;
}

let requestCount = 0;

/** JSON을 반환합니다. 400/404는 null이며, 429와 일시적 오류는 재시도합니다. */
async function getJson(path, params) {
    const query = new URLSearchParams(params).toString().replaceAll('%2C', ',');
    const url = `${API_BASE_URL}${path}?${query}`;
    let limited = 0;
    for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
        await paceRequests();
        requestCount++;
        let response;
        try {
            response = await fetch(url, { headers: { Accept: 'application/json' } });
        } catch (error) {
            if (attempt === MAX_ATTEMPTS) throw new Error(`네트워크 오류: ${path} (${error.message})`);
            await sleep(1000 * attempt);
            continue;
        }
        if (response.status === 429) {
            if (++limited > 60) throw new Error(`한도 초과(429)가 계속되어 중단합니다: ${path}`);
            const waitSec = Number(response.headers.get('retry-after')) || 5 * attempt;
            blockedUntil = Math.max(blockedUntil, Date.now() + waitSec * 1000);
            rateLimitHits++;
            if (!rateLimitQuiet) console.log(`  (요청이 너무 많다는 응답을 받아 ${waitSec}초 쉬었다가 이어갑니다.)`);
            attempt--; // 한도 초과는 재시도 횟수에 포함하지 않습니다.
            continue;
        }
        if (response.status === 400 || response.status === 404) return null;
        if (!response.ok) {
            if (attempt === MAX_ATTEMPTS) throw new Error(`HTTP ${response.status}: ${path}`);
            await sleep(1000 * attempt);
            continue;
        }
        return response.json();
    }
    throw new Error(`요청을 끝내지 못했습니다: ${path}`);
}

const spotifyIdFromHref = href => (href ?? '').replace(/[?#].*$/, '').replace(/\/+$/, '').split('/').pop() || null;

/** ID, spotify:artist:ID, 아티스트 페이지 링크에서 Spotify 아티스트 ID(22자)를 추출합니다. */
function extractSpotifyArtistId(value) {
    const match = value.trim().match(/^(?:spotify:artist:|https?:\/\/open\.spotify\.com\/(?:intl-[a-z]+\/)?artist\/)?([0-9A-Za-z]{22})(?:[?/#].*)?$/);
    return match ? match[1] : null;
}

// ---------- 곡 선택 ----------

/**
 * 아티스트의 곡에서 Spotify ID, ReccoBeats ID, 제목을 읽을 수 없는 곡을 제외하고 인기도 내림차순으로 정렬합니다.
 * 제목으로 같은 곡을 합치거나 연주곡을 가르지 않으며, 그런 선별은 조회할 때 합니다.
 * 한국 재생 가능 여부(krAvailable)는 곡에 기록만 하며, 적재할 때 collectAndInsert가 제외합니다.
 * 여러 아티스트의 목록에 있는 곡은 spotify_id가 같으므로 한 번만 적재됩니다.
 */
function selectForArtist(tracks, options) {
    const stats = { total: tracks.length, noSpotifyId: 0 };
    const usable = [];
    for (const t of tracks) {
        const spotifyId = spotifyIdFromHref(t.href);
        if (!spotifyId || !SPOTIFY_ID_PATTERN.test(spotifyId) || !UUID_PATTERN.test(t.id ?? '') || !t.trackTitle?.trim()) {
            stats.noSpotifyId++;
            continue;
        }
        usable.push({
            spotifyId, reccobeatsId: t.id, title: t.trackTitle.trim(),
            artist: (t.artists ?? []).map(artist => artist.name).join(', '),
            artistIds: (t.artists ?? []).map(artist => artist.id),
            durationMs: t.durationMs, popularity: Number.isInteger(t.popularity) ? t.popularity : null,
            isrc: t.isrc ?? null, krAvailable: (t.availableCountries ?? '').split(',').includes('KR'),
        });
    }
    usable.sort((a, b) => (b.popularity ?? -1) - (a.popularity ?? -1));
    return { selected: options.top ? usable.slice(0, options.top) : usable, stats };
}

// ---------- 수집 ----------

async function fetchArtistTracks(artistId) {
    const tracks = [];
    let totalElements = 0;
    // 마지막 페이지나 빈 페이지를 만날 때까지 가져옵니다.
    for (let page = 0; ; page++) {
        const data = await getJson(`/artist/${artistId}/track`, { size: ARTIST_PAGE_SIZE, page });
        if (!data || (data.content ?? []).length === 0) break;
        totalElements = data.totalElements ?? tracks.length;
        tracks.push(...data.content);
        if (page + 1 >= (data.totalPages ?? 1)) break;
    }
    return { tracks, totalElements };
}

/** 기록 파일을 읽습니다. 값이 없다고 확인된 곡은 null이며, 중단되어 일부만 기록된 마지막 줄은 무시합니다. */
function readSavedFeatures(path) {
    const features = new Map();
    if (!existsSync(path)) return features;
    for (const line of readFileSync(path, 'utf8').split('\n')) {
        if (!line) continue;
        try {
            const item = JSON.parse(line);
            if (item.id) features.set(item.id, item.none ? null : item);
        } catch {
            // 일부만 기록된 마지막 줄
        }
    }
    return features;
}

/** 오디오 값을 받아 { Spotify ID → 응답 항목 | null } 지도로 반환합니다. 받은 즉시 기록 파일에 덧붙여 중단 후 이어받을 수 있습니다. */
async function fetchFeatures(spotifyIds, featuresFile) {
    const features = readSavedFeatures(featuresFile);
    const todo = spotifyIds.filter(id => !features.has(id));
    console.log(`오디오 값: 전체 ${spotifyIds.length.toLocaleString('ko-KR')}곡 중 기록에 ${(spotifyIds.length - todo.length).toLocaleString('ko-KR')}곡, 새로 받을 곡 ${todo.length.toLocaleString('ko-KR')}곡`);
    console.log(`  오디오 값 기록 파일: ${featuresFile}`);
    if (todo.length === 0) return features;

    const batches = [];
    for (let i = 0; i < todo.length; i += TRACK_BATCH_SIZE) batches.push(todo.slice(i, i + TRACK_BATCH_SIZE));

    const startedAt = Date.now();
    const requestsAtStart = requestCount;
    const samples = [[startedAt, 0]]; // 최근 1분의 처리량으로 남은 시간을 추정합니다.
    let done = 0;
    let lastPrinted = 0;
    const report = force => {
        const now = Date.now();
        if (!force && now - lastPrinted < (isTty ? 500 : 10000)) return;
        lastPrinted = now;
        samples.push([now, done]);
        while (samples.length > 2 && now - samples[0][0] > 60000) samples.shift();
        const [t0, d0] = samples[0];
        const rate = now > t0 ? (done - d0) / ((now - t0) / 1000) : 0;
        const remaining = rate > 0 ? (todo.length - done) / rate : null;
        const waiting = Math.ceil((blockedUntil - now) / 1000);
        const text = `  오디오 값 ${done.toLocaleString('ko-KR')}/${todo.length.toLocaleString('ko-KR')}곡 (${(done / todo.length * 100).toFixed(1)}%) | 초당 ${rate.toFixed(0)}곡 | 남은 시간 약 ${formatDuration(remaining)} | 한도 초과 ${rateLimitHits}회${waiting > 0 ? ` (지금 ${waiting}초 대기)` : ''}`;
        if (isTty) writeStatus(text);
        else console.log(text);
    };

    rateLimitQuiet = true;
    try {
        report(true);
        await runPool(batches, async batch => {
            const got = new Map();
            const collect = data => {
                for (const item of data?.content ?? []) {
                    const id = spotifyIdFromHref(item.href);
                    if (id) got.set(id, item);
                }
            };
            const data = await getJson('/audio-features', { ids: batch.join(',') });
            if (data) collect(data);
            else for (const id of batch) collect(await getJson('/audio-features', { ids: id })); // 묶음 요청이 실패하면 개별로 재요청합니다.

            const lines = [];
            for (const id of batch) {
                const item = got.get(id);
                const usable = item && Number.isFinite(item.valence) && Number.isFinite(item.energy);
                const record = usable
                    ? { id, ...Object.fromEntries(FEATURE_FIELDS.map(field => [field, Number.isFinite(item[field]) ? item[field] : null])) }
                    : { id, none: 1 };
                lines.push(JSON.stringify(record));
                features.set(id, usable ? record : null);
            }
            appendFileSync(featuresFile, `${lines.join('\n')}\n`);
            done += batch.length;
            report(false);
        });
        report(true);
    } finally {
        endStatus();
        rateLimitQuiet = false;
    }
    const seconds = Math.round((Date.now() - startedAt) / 1000);
    console.log(`  오디오 값 ${todo.length.toLocaleString('ko-KR')}곡을 받았습니다. (요청 ${(requestCount - requestsAtStart).toLocaleString('ko-KR')}회, 한도 초과 ${rateLimitHits}회, ${formatDuration(seconds)})`);
    return features;
}

/** 시드 CSV에서 (장르, 아티스트, 아티스트 Spotify ID)를 읽습니다. ID가 빈 행은 건너뜁니다. */
function readSeeds(path, genreFilter) {
    if (!existsSync(path)) fail(`시드 CSV가 없습니다: ${path}`);
    const rows = parseCsv(readFileSync(path, 'utf8'));
    const seeds = [];
    const invalid = [];
    for (let i = 1; i < rows.length; i++) {
        const [genre = '', artist = '', rawId = ''] = rows[i].map(cell => cell.trim());
        if (genreFilter && genre !== genreFilter) continue;
        if (!rawId) continue;
        const spotifyId = extractSpotifyArtistId(rawId);
        if (spotifyId) seeds.push({ genre, artist, spotifyId, line: i + 1 });
        else invalid.push(`${i + 1}행 ${artist}: "${rawId}"`);
    }
    return { seeds, invalid };
}

async function runPool(items, worker) {
    let next = 0;
    await Promise.all(Array.from({ length: Math.min(CONCURRENCY, items.length) }, async () => {
        while (next < items.length) {
            const index = next++;
            await worker(items[index], index);
        }
    }));
}

// ---------- DB ----------

/** 접속 주소에서 호스트, 포트, DB 이름만 추출합니다. 자격 증명은 출력하지 않습니다. */
function describeTarget(databaseUrl) {
    let url;
    try {
        url = new URL(databaseUrl);
    } catch {
        fail('DATABASE_URL의 형식이 올바르지 않습니다. (값은 출력하지 않습니다.)');
    }
    return { url, label: `${url.hostname}:${url.port || '5432'}/${url.pathname.slice(1)}` };
}

async function connect() {
    const target = describeTarget(process.env.DATABASE_URL);
    console.log(`대상 DB: ${target.label}`);
    const isLocal = ['localhost', '127.0.0.1', '::1'].includes(target.url.hostname);
    const ssl = isLocal || process.env.DATABASE_SSL === 'false' ? false : { rejectUnauthorized: false };
    const { default: pg } = await import('pg');
    const client = new pg.Client({ connectionString: process.env.DATABASE_URL, ssl });
    await client.connect();
    return client;
}

/** 이미 적재된 곡의 ID를 읽습니다. 테이블이 없으면 비어 있는 것으로 봅니다. 요약의 신규·기존 집계에만 쓰입니다. */
async function readExisting(client) {
    const exists = (await client.query(`SELECT to_regclass('public.${TABLE}') AS name`)).rows[0].name;
    if (!exists) return { tableExists: false, spotifyIds: new Set() };
    const { rows } = await client.query(`SELECT spotify_id FROM ${TABLE}`);
    return { tableExists: true, spotifyIds: new Set(rows.map(row => row.spotify_id)) };
}

/** 하나의 트랜잭션으로 적재합니다. 이미 있는 곡은 artist_reccobeats_ids와 오디오 값만 갱신합니다. */
async function insertRows(client, rows) {
    await client.query('BEGIN');
    try {
        const updateColumns = ['artist_reccobeats_ids', ...FEATURE_COLUMNS];
        const onConflict = `ON CONFLICT (spotify_id) DO UPDATE SET ${updateColumns.map(column => `${column} = EXCLUDED.${column}`).join(', ')} RETURNING (xmax = 0) AS inserted`;
        let inserted = 0;
        for (let i = 0; i < rows.length; i += INSERT_BATCH_SIZE) {
            const batch = rows.slice(i, i + INSERT_BATCH_SIZE);
            const params = batch.flatMap(row => INSERT_COLUMNS.map(column => row[column]));
            const placeholders = batch
                .map((_, r) => `(${INSERT_COLUMNS.map((__, c) => `$${r * INSERT_COLUMNS.length + c + 1}`).join(', ')})`)
                .join(', ');
            const result = await client.query(`INSERT INTO ${TABLE} (${INSERT_COLUMNS.join(', ')}) VALUES ${placeholders} ${onConflict}`, params);
            inserted += result.rows.filter(row => row.inserted).length;
            const text = `  적재 진행: ${Math.min(i + INSERT_BATCH_SIZE, rows.length).toLocaleString('ko-KR')}/${rows.length.toLocaleString('ko-KR')}곡`;
            if (isTty) writeStatus(text);
            else if ((i / INSERT_BATCH_SIZE) % 20 === 0) console.log(text);
        }
        endStatus();
        const total = (await client.query(`SELECT count(*)::int AS n FROM ${TABLE}`)).rows[0].n;
        await client.query('COMMIT');
        return { inserted, total };
    } catch (error) {
        endStatus();
        await client.query('ROLLBACK').catch(() => {});
        throw error;
    }
}

/** 스키마 제약을 어기는 행을 적재 전에 걸러, 트랜잭션 전체가 실패하지 않게 합니다. */
function rowIssue(row) {
    if (!SPOTIFY_ID_PATTERN.test(row.spotify_id)) return 'Spotify ID 형식이 올바르지 않습니다.';
    if (!UUID_PATTERN.test(row.reccobeats_id) || row.artist_reccobeats_ids.length === 0 || !row.artist_reccobeats_ids.every(id => UUID_PATTERN.test(id ?? ''))) return 'ReccoBeats ID 형식이 올바르지 않습니다.';
    if (!row.title || !row.artist) return '제목이나 아티스트가 비어 있습니다.';
    if (!Number.isInteger(row.duration_ms) || row.duration_ms < 0) return '길이가 올바르지 않습니다.';
    if (row.popularity !== null && !(Number.isInteger(row.popularity) && row.popularity >= 0 && row.popularity <= 100)) return '인기도가 0~100의 정수가 아닙니다.';
    if (!(row.valence >= 0 && row.valence <= 1) || !(row.energy >= 0 && row.energy <= 1)) return 'valence나 energy가 [0, 1] 범위를 벗어났습니다.';
    return null;
}

// ---------- 수집과 적재 ----------

/** 곡 목록 파일을 { Spotify ID → 곡 } 지도로 읽습니다. */
function readCollectedTracks(path) {
    let tracks;
    try {
        tracks = JSON.parse(readFileSync(path, 'utf8'));
    } catch (error) {
        fail(`수집 결과 파일을 읽을 수 없습니다: ${path} (${error.message})`);
    }
    if (!Array.isArray(tracks)) fail(`수집 결과 파일이 곡 배열이 아닙니다: ${path}`);
    const bySpotifyId = new Map();
    for (const t of tracks) {
        if (t && SPOTIFY_ID_PATTERN.test(t.spotifyId ?? '') && !bySpotifyId.has(t.spotifyId)) bySpotifyId.set(t.spotifyId, t);
    }
    return bySpotifyId;
}

async function load(options) {
    const hasCollected = existsSync(options.tracksFile);
    let seeds = [];
    if (!hasCollected) {
        const read = readSeeds(options.csv, options.genre);
        seeds = read.seeds;
        if (read.invalid.length > 0) {
            console.log(`Spotify 아티스트 ID로 읽을 수 없는 행 ${read.invalid.length}개(건너뜁니다):`);
            read.invalid.forEach(line => console.log(`  - ${line}`));
        }
        if (options.limit) seeds = seeds.slice(0, options.limit);
        if (seeds.length === 0) fail('아티스트 ID가 채워진 시드가 없어서 중단합니다.');
    }

    // 기존 곡을 읽을 때만 접속하고 끊습니다. 수집이 길어져도 연결이 끊기지 않도록 적재할 때 다시 접속합니다.
    let existing = { tableExists: false, spotifyIds: new Set() };
    if (process.env.DATABASE_URL) {
        const client = await connect();
        try {
            existing = await readExisting(client);
        } finally {
            await client.end().catch(() => {});
        }
        console.log(`${TABLE} 현재: ${existing.tableExists ? `${existing.spotifyIds.size.toLocaleString('ko-KR')}곡` : '테이블이 아직 없습니다. capstone2-backend의 src/db/schema.sql을 먼저 실행해 주십시오.'}`);
        if (options.yes && !existing.tableExists) fail(`${TABLE} 테이블이 없어서 중단합니다. capstone2-backend의 src/db/schema.sql을 먼저 실행해 주십시오.`);
    } else {
        if (options.yes) fail('--yes로 DB에 올리려면 DATABASE_URL이 필요합니다.\n.env 파일에 적고 --env-file=.env 옵션으로 실행해 주십시오.');
        console.log('DATABASE_URL이 없어 DB 없이 미리보기만 합니다. (이미 있는 곡은 확인하지 못합니다.)');
    }
    if (!hasCollected) {
        console.log(`\n시드 ${seeds.length}개로 시작합니다. (${options.genre ? `장르: ${options.genre}, ` : ''}${options.top ? `아티스트당 최대 ${options.top}곡` : '아티스트당 곡 수 제한 없음'})\n`);
    }
    await collectAndInsert(options, seeds, existing, hasCollected);
}

/** 시드 아티스트를 ReccoBeats에서 찾고 아티스트별 곡 전체를 수집합니다. */
async function collectTracks(options, seeds) {
    // 1) 시드를 ReccoBeats 아티스트로 변환합니다.
    const found = new Map(); // Spotify 아티스트 ID → { id, name }
    const collectArtists = data => {
        for (const artist of data?.content ?? []) {
            const spotifyId = (artist.href ?? '').match(/artist\/([0-9A-Za-z]{22})/)?.[1];
            if (spotifyId && UUID_PATTERN.test(artist.id ?? '')) found.set(spotifyId, { id: artist.id, name: artist.name });
        }
    };
    const spotifyIds = [...new Set(seeds.map(seed => seed.spotifyId))];
    for (let i = 0; i < spotifyIds.length; i += TRACK_BATCH_SIZE) {
        const batch = spotifyIds.slice(i, i + TRACK_BATCH_SIZE);
        const data = await getJson('/artist', { ids: batch.join(',') });
        if (data) collectArtists(data);
        else for (const id of batch) collectArtists(await getJson('/artist', { ids: id })); // 묶음 요청이 실패하면 개별로 재요청합니다.
    }
    const artists = new Map(); // ReccoBeats 아티스트 ID → { id, name, seedNames, genres }
    const unresolved = [];
    for (const seed of seeds) {
        const artist = found.get(seed.spotifyId);
        if (!artist) {
            unresolved.push(seed);
            continue;
        }
        if (!artists.has(artist.id)) artists.set(artist.id, { id: artist.id, name: artist.name, seedNames: new Set(), genres: new Set() });
        artists.get(artist.id).seedNames.add(seed.artist);
        artists.get(artist.id).genres.add(seed.genre);
    }

    // 하나라도 찾지 못하면 수집하지 않습니다.
    if (unresolved.length > 0) {
        console.log(`⚠ ReccoBeats에서 찾지 못한 시드 아티스트 ${unresolved.length}개:`);
        unresolved.forEach(seed => console.log(`  - ${seed.line}행 ${seed.artist} (${seed.spotifyId})`));
        throw new Error('시드 아티스트를 모두 찾은 뒤에 수집할 수 있어서 중단합니다. (아무것도 수집하거나 변경하지 않았습니다.)');
    }

    // 2) 적재 여부와 관계없이 모든 아티스트를 수집합니다.
    const list = [...artists.values()];

    // 3) 아티스트별로 곡 전체를 받아 후보를 고릅니다.
    const results = new Map();
    let done = 0;
    await runPool(list, async artist => {
        const { tracks, totalElements } = await fetchArtistTracks(artist.id);
        results.set(artist.id, { ...selectForArtist(tracks, options), totalElements, fetched: tracks.length });
        done++;
        if (done % 10 === 0 || done === list.length) console.log(`  아티스트 ${done}/${list.length} 처리 (요청 ${requestCount}회)`);
    });

    // 여러 아티스트에 걸친 곡은 spotify_id 기준으로 한 번만 담습니다.
    const tracks = new Map();
    for (const artist of list) {
        for (const t of results.get(artist.id).selected) {
            if (!tracks.has(t.spotifyId)) tracks.set(t.spotifyId, t);
        }
    }
    return { tracks, artists, list, results };
}

/** 곡과 오디오 값으로 적재할 행을 만듭니다. 범위를 벗어난 보조 값은 NULL로 둡니다. */
function buildRow(t, f) {
    const unit = x => (Number.isFinite(x) && x >= 0 && x <= 1 ? x : null);
    return {
        spotify_id: t.spotifyId, reccobeats_id: t.reccobeatsId, title: t.title, artist: t.artist, artist_reccobeats_ids: t.artistIds,
        duration_ms: t.durationMs, popularity: t.popularity, isrc: t.isrc,
        valence: f.valence, energy: f.energy,
        acousticness: unit(f.acousticness), danceability: unit(f.danceability), instrumentalness: unit(f.instrumentalness),
        liveness: unit(f.liveness), speechiness: unit(f.speechiness),
        loudness: Number.isFinite(f.loudness) ? f.loudness : null,
        tempo: Number.isFinite(f.tempo) && f.tempo >= 0 ? f.tempo : null,
        musical_key: Number.isInteger(f.key) && f.key >= -1 && f.key <= 11 ? f.key : null,
        musical_mode: f.mode === 0 || f.mode === 1 ? f.mode : null,
    };
}

async function collectAndInsert(options, seeds, existing, hasCollected) {
    let collected = null;
    let tracks;
    if (hasCollected) {
        tracks = readCollectedTracks(options.tracksFile);
        console.log(`수집 결과 파일에서 ${tracks.size.toLocaleString('ko-KR')}곡을 불러왔습니다. 아티스트 수집은 건너뜁니다.`);
        console.log(`  ${options.tracksFile}`);
        if (options.csv !== DEFAULT_SEED_CSV || options.genre || options.limit || options.top) console.log('  (--csv, --genre, --limit, --top은 새로 수집할 때만 적용되어 무시합니다.)');
        console.log('');
    } else {
        collected = await collectTracks(options, seeds);
        tracks = collected.tracks;
        mkdirSync(dirname(options.tracksFile), { recursive: true });
        writeFileSync(options.tracksFile, JSON.stringify([...tracks.values()]));
        console.log(`수집한 곡 ${tracks.size.toLocaleString('ko-KR')}곡을 ${options.tracksFile}에 저장했습니다. (다음 실행에서는 이 목록으로 이어서 진행합니다.)\n`);
    }

    // 수집 파일에는 전체 곡을 남기고, 한국에서 재생할 수 없는 곡은 오디오 값을 받기 전에 뺍니다.
    const collectedCount = tracks.size;
    tracks = new Map([...tracks].filter(([, t]) => t.krAvailable));
    const krUnavailableCount = collectedCount - tracks.size;

    mkdirSync(dirname(options.featuresFile), { recursive: true });
    const features = await fetchFeatures([...tracks.keys()], options.featuresFile);
    const noFeatures = [];
    const invalidRows = [];
    const rows = [];
    for (const t of tracks.values()) {
        const f = features.get(t.spotifyId);
        if (!f) { noFeatures.push(t); continue; }
        const row = buildRow(t, f);
        const issue = rowIssue(row);
        if (issue) invalidRows.push(`${t.title}: ${issue}`);
        else rows.push(row);
    }

    // ---------- 요약 ----------
    if (collected) {
        console.log(`\n=== 아티스트별 결과 (시드 CSV 이름 → ReccoBeats 이름 : 전체 곡 수 → 올릴 곡 수)`);
        console.log('  (곡 앞의 -- 표시는 한국 재생 불가라서 적재에서 제외되며, 아래 곡 수에는 이런 곡도 포함되어 있습니다.)');
        for (const artist of collected.list) {
            const r = collected.results.get(artist.id);
            const note = r.totalElements > r.fetched ? ` (⚠ 전체 ${r.totalElements}곡 중 ${r.fetched}곡만 조회됨)` : '';
            console.log(`  ${[...artist.seedNames].join(' / ')} → ${artist.name} : ${r.totalElements} → ${r.selected.length}${note}`);
            for (const row of r.selected.slice(0, options.show)) {
                console.log(`      ${String(row.popularity ?? '-').padStart(3)} | ${row.krAvailable ? 'KR' : '--'} | ${row.title}`);
            }
        }
    }
    const count = predicate => rows.filter(predicate).length;
    const quadrant = (v, e) => count(row => (row.valence >= 0.5) === v && (row.energy >= 0.5) === e);
    console.log(`\n=== 요약`);
    if (collected) {
        const noSpotifyId = collected.list.reduce((sum, artist) => sum + collected.results.get(artist.id).stats.noSpotifyId, 0);
        console.log(`  시드 ${seeds.length}개 → 아티스트 ${collected.artists.size}팀, 중복을 합친 곡 ${rows.length.toLocaleString('ko-KR')}곡 (요청 ${requestCount.toLocaleString('ko-KR')}회)`);
        console.log(`  Spotify ID를 읽을 수 없어 뺀 곡: ${noSpotifyId}곡`);
    } else {
        console.log(`  수집 결과 파일의 곡 ${collectedCount.toLocaleString('ko-KR')}곡 중 올릴 곡 ${rows.length.toLocaleString('ko-KR')}곡 (요청 ${requestCount.toLocaleString('ko-KR')}회)`);
    }
    const already = count(row => existing.spotifyIds.has(row.spotify_id));
    console.log(`  이 중 DB에 이미 있는 곡 ${already.toLocaleString('ko-KR')}곡 / 새로 들어갈 곡 ${(rows.length - already).toLocaleString('ko-KR')}곡`);
    console.log(`  한국 재생 불가로 제외한 곡: ${krUnavailableCount.toLocaleString('ko-KR')}곡`);
    console.log(`  사분면(valence ≥ 0.5 / energy ≥ 0.5): 긍정·활기 ${quadrant(true, true)}, 긍정·차분 ${quadrant(true, false)}, 부정·활기 ${quadrant(false, true)}, 부정·차분 ${quadrant(false, false)}`);
    if (noFeatures.length > 0) console.log(`  오디오 값이 없어 제외한 곡: ${noFeatures.length.toLocaleString('ko-KR')}곡`);
    if (invalidRows.length > 0) {
        console.log(`  제약을 어겨 제외한 곡: ${invalidRows.length}곡`);
        invalidRows.slice(0, 5).forEach(line => console.log(`    - ${line}`));
    }

    if (rows.length === 0) {
        console.log('\n올릴 곡이 없습니다.');
        return;
    }
    if (!options.yes) {
        console.log(`\n실제로 올리려면 --yes 옵션을 붙여 다시 실행해 주십시오. (아직 DB는 바꾸지 않았습니다. 오디오 값은 기록 파일에 저장되어 있어서 다시 받지 않습니다.)`);
        return;
    }
    console.log(`\n${TABLE}에 올립니다…`);
    const client = await connect();
    try {
        const { inserted, total } = await insertRows(client, rows);
        console.log(`\n적재를 마쳤습니다. 새로 들어간 곡 ${inserted.toLocaleString('ko-KR')}곡${inserted < rows.length ? ` (이미 있어서 ID 목록과 오디오 값만 맞춘 곡 ${(rows.length - inserted).toLocaleString('ko-KR')}곡)` : ''}, ${TABLE}의 전체 곡 수: ${total.toLocaleString('ko-KR')}곡`);
    } finally {
        await client.end().catch(() => {});
    }
}

// ---------- 실행 ----------

try {
    await load(parseArgs(process.argv.slice(2)));
} catch (error) {
    console.error(`실패했습니다: ${error.message}`);
    process.exit(1);
}
