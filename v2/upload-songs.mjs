// ReccoBeats 수집 결과(reccobeats_raw.jsonl)와 songs.jsonl의 좌표를 합쳐서 Postgres의 songs 테이블에 올립니다.
// 한국(KR)에서 재생할 수 없다고 표시된 곡과 응답이 없는 곡은 올리지 않습니다.
//
// 사용법:
//   node --env-file=.env v2/upload-songs.mjs --dry-run      (DB에 접속하지 않고 변환 결과만 확인)
//   node --env-file=.env v2/upload-songs.mjs                 (대상 DB와 곡 수만 보여주고 멈춤)
//   node --env-file=.env v2/upload-songs.mjs --yes           (실제로 업로드)
//
// 필요한 환경변수: DATABASE_URL (.env 파일에 넣으면 커밋되지 않습니다)
// 선택 환경변수: DATABASE_SSL=false (로컬이 아닌 DB에 SSL 없이 접속해야 할 때)
// 업로드는 하나의 트랜잭션으로 처리되므로 중간에 실패하면 모두 취소됩니다. 이미 있는 곡은 갱신합니다.

import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const SONGS_PATH = fileURLToPath(new URL('../v1/data/songs.jsonl', import.meta.url));
const SCHEMA_PATH = fileURLToPath(new URL('./schema.sql', import.meta.url));
const DEFAULT_RAW_PATH = fileURLToPath(new URL('./data/reccobeats_raw.jsonl', import.meta.url));

const COLUMNS = ['spotify_id', 'reccobeats_id', 'title', 'artist', 'duration_ms', 'popularity', 'valence', 'energy'];
const SPOTIFY_ID_PATTERN = /^[0-9A-Za-z]{22}$/;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const USAGE = `사용법:
  node --env-file=.env v2/upload-songs.mjs --dry-run
  node --env-file=.env v2/upload-songs.mjs [--yes] [--raw <수집 결과 파일>] [--batch <한 번에 올릴 곡 수>]`;

function fail(message) {
    console.error(message);
    process.exit(1);
}

function parseArgs(argv) {
    const options = { dryRun: false, yes: false, raw: DEFAULT_RAW_PATH, batch: 1000 };
    for (let i = 0; i < argv.length; i++) {
        const flag = argv[i];
        if (flag === '--dry-run') {
            options.dryRun = true;
        } else if (flag === '--yes') {
            options.yes = true;
        } else if (flag === '--raw' || flag === '--batch') {
            const value = argv[++i];
            if (value === undefined) fail(`${flag}에 값이 필요합니다.\n${USAGE}`);
            if (flag === '--raw') options.raw = resolve(value.replace(/^~(?=\/|$)/, process.env.HOME ?? '~'));
            else options.batch = Number(value);
        } else {
            fail(`알 수 없는 옵션입니다: ${flag}\n${USAGE}`);
        }
    }
    if (!(options.batch >= 1 && options.batch <= 5000)) fail('--batch는 1 이상 5000 이하의 숫자여야 합니다.');
    return options;
}

// ---------- 변환 ----------

/** 원본 곡 하나를 테이블 한 줄로 바꿉니다. valence와 energy는 songs.jsonl의 [-1, 1] 정규화 값입니다. */
function toRow(spotifyId, track, song) {
    const popularity = Number.isInteger(track.popularity) && track.popularity >= 0 && track.popularity <= 100
        ? track.popularity
        : null;
    return {
        spotify_id: spotifyId,
        reccobeats_id: track.id,
        title: track.trackTitle ?? '',
        artist: (track.artists ?? []).map(artist => artist.name).join(', '),
        duration_ms: track.durationMs,
        popularity,
        valence: Number(song[9]),
        energy: Number(song[10]),
    };
}

/** 스키마의 제약 조건을 위반할 행을 미리 찾아냅니다. 문제가 없으면 null입니다. */
function findProblem(row) {
    if (!SPOTIFY_ID_PATTERN.test(row.spotify_id)) return 'Spotify ID 형식이 올바르지 않습니다.';
    if (!UUID_PATTERN.test(row.reccobeats_id ?? '')) return 'ReccoBeats ID 형식이 올바르지 않습니다.';
    if (row.title.trim() === '') return '제목이 비어 있습니다.';
    if (row.artist.trim() === '') return '아티스트가 비어 있습니다.';
    if (!Number.isInteger(row.duration_ms) || row.duration_ms < 0) return '길이가 올바르지 않습니다.';
    if (!(row.valence >= -1 && row.valence <= 1)) return 'valence가 [-1, 1] 범위를 벗어났습니다.';
    if (!(row.energy >= -1 && row.energy <= 1)) return 'energy가 [-1, 1] 범위를 벗어났습니다.';
    return null;
}

/** 수집 결과와 songs.jsonl을 합쳐 올릴 행을 만들고, 제외한 곡의 개수를 함께 돌려줍니다. */
export function buildRows(rawPath = DEFAULT_RAW_PATH, songsPath = SONGS_PATH) {
    if (!existsSync(rawPath)) {
        throw new Error(`수집 결과 파일이 없습니다: ${rawPath}\n먼저 fetch-reccobeats-metadata.mjs를 실행해 주십시오.`);
    }
    const tracks = new Map();
    for (const line of readFileSync(rawPath, 'utf8').split('\n')) {
        if (!line.trim()) continue;
        let record;
        try {
            record = JSON.parse(line);
        } catch {
            continue; // 쓰는 도중에 끊긴 줄은 무시합니다.
        }
        if (record.status === 200) tracks.set(record.requestedId, record.track);
    }

    const stats = { total: 0, missing: 0, notInKorea: 0, invalid: 0 };
    const invalidExamples = [];
    const rows = [];
    for (const line of readFileSync(songsPath, 'utf8').trim().split('\n')) {
        const song = JSON.parse(line);
        const spotifyId = song[1];
        stats.total++;
        const track = tracks.get(spotifyId);
        if (!track) {
            stats.missing++;
            continue;
        }
        if (!track.availableCountries?.split(',').includes('KR')) {
            stats.notInKorea++;
            continue;
        }
        const row = toRow(spotifyId, track, song);
        const problem = findProblem(row);
        if (problem) {
            stats.invalid++;
            if (invalidExamples.length < 5) invalidExamples.push(`${spotifyId}: ${problem}`);
            continue;
        }
        rows.push(row);
    }
    return { rows, stats, invalidExamples };
}

// ---------- 업로드 ----------

/**
 * 스키마를 적용하고 행을 묶음으로 올립니다. 모두 하나의 트랜잭션이라 중간에 실패하면 전부 취소됩니다.
 * client는 query(text, params)를 가진 객체(pg의 Client 등)입니다. 올라간 뒤 테이블의 전체 곡 수를 돌려줍니다.
 */
export async function uploadRows(client, rows, schemaSql, batchSize = 1000, onProgress = () => {}) {
    await client.query('BEGIN');
    try {
        await client.query(schemaSql);
        for (let i = 0; i < rows.length; i += batchSize) {
            const batch = rows.slice(i, i + batchSize);
            const params = batch.flatMap(row => COLUMNS.map(column => row[column]));
            const placeholders = batch
                .map((_, rowIndex) => `(${COLUMNS.map((__, columnIndex) => `$${rowIndex * COLUMNS.length + columnIndex + 1}`).join(', ')})`)
                .join(', ');
            const updates = COLUMNS.slice(1).map(column => `${column} = EXCLUDED.${column}`).join(', ');
            await client.query(
                `INSERT INTO songs (${COLUMNS.join(', ')}) VALUES ${placeholders} ON CONFLICT (spotify_id) DO UPDATE SET ${updates}`,
                params,
            );
            onProgress(Math.min(i + batchSize, rows.length), rows.length);
        }
        const result = await client.query('SELECT count(*)::int AS count FROM songs');
        await client.query('COMMIT');
        return result.rows[0].count;
    } catch (error) {
        await client.query('ROLLBACK');
        throw error;
    }
}

/** 접속 주소에서 호스트, 포트, DB 이름만 꺼내 보여줍니다. 사용자 이름과 비밀번호는 절대 출력하지 않습니다. */
function describeTarget(databaseUrl) {
    let url;
    try {
        url = new URL(databaseUrl);
    } catch {
        fail('DATABASE_URL의 형식이 올바르지 않습니다. (값은 출력하지 않습니다.)');
    }
    return { url, label: `${url.hostname}:${url.port || '5432'}/${url.pathname.slice(1)}` };
}

// ---------- 실행 ----------

function printSummary({ rows, stats, invalidExamples }) {
    console.log(`songs.jsonl의 곡: ${stats.total}곡`);
    console.log(`  - 수집 결과에 없는 곡(제외): ${stats.missing}곡`);
    console.log(`  - 한국(KR) 재생 가능으로 표시되지 않은 곡(제외): ${stats.notInKorea}곡`);
    console.log(`  - 제약 조건을 위반할 곡(제외): ${stats.invalid}곡`);
    for (const example of invalidExamples) console.log(`      ${example}`);
    console.log(`업로드할 곡: ${rows.length}곡`);
}

async function main() {
    const options = parseArgs(process.argv.slice(2));
    const built = buildRows(options.raw);
    printSummary(built);

    if (options.dryRun) {
        console.log('\n변환 결과의 예시 (처음 3곡):');
        for (const row of built.rows.slice(0, 3)) console.log(`  ${JSON.stringify(row)}`);
        console.log('\n--dry-run이므로 DB에는 접속하지 않았습니다.');
        return;
    }
    if (built.rows.length === 0) fail('업로드할 곡이 없어서 중단합니다.');
    if (!process.env.DATABASE_URL) fail('DATABASE_URL이 필요합니다.\n.env 파일에 적고 --env-file=.env 옵션으로 실행해 주십시오.');

    const target = describeTarget(process.env.DATABASE_URL);
    console.log(`\n대상 DB: ${target.label}`);
    if (!options.yes) {
        console.log('실제로 올리려면 --yes 옵션을 붙여 다시 실행해 주십시오. (아직 아무것도 변경하지 않았습니다.)');
        return;
    }

    const isLocal = ['localhost', '127.0.0.1', '::1'].includes(target.url.hostname);
    const ssl = isLocal || process.env.DATABASE_SSL === 'false' ? false : { rejectUnauthorized: false };
    const { default: pg } = await import('pg');
    const client = new pg.Client({ connectionString: process.env.DATABASE_URL, ssl });
    await client.connect();
    try {
        const count = await uploadRows(client, built.rows, readFileSync(SCHEMA_PATH, 'utf8'), options.batch, (done, total) => {
            console.log(`업로드 진행: ${done}/${total}곡`);
        });
        console.log(`\n업로드를 마쳤습니다. songs 테이블의 전체 곡 수: ${count}곡`);
    } finally {
        await client.end();
    }
}

// 다른 파일에서 가져다 쓸 때는 실행하지 않고, 직접 실행했을 때만 main을 호출합니다.
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
    try {
        await main();
    } catch (error) {
        console.error(`업로드에 실패했습니다: ${error.message}`);
        process.exit(1);
    }
}
