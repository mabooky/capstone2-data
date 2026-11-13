// songs.jsonl의 Spotify ID로 ReccoBeats에서 곡 정보를 조회하고, 응답의 곡 항목을 그대로 JSONL로 저장합니다.
// GET /v1/track?ids=... 는 인증이 필요 없고 한 번에 최대 40곡까지 조회할 수 있습니다.
//
// 사용법:
//   node v2/fetch-reccobeats-metadata.mjs --probe
//   node v2/fetch-reccobeats-metadata.mjs [--out <결과 파일 경로>] [--limit 200] [--concurrency 2]
//
// 결과 파일의 기본 위치는 이 스크립트와 같은 위치의 data/reccobeats-raw.jsonl 입니다. 이 폴더는 .gitignore에 포함되어 커밋되지 않습니다.
// 시험용 환경변수: RECCOBEATS_API_BASE_URL (가짜 서버로 동작을 확인할 때 사용합니다)
// ReccoBeats의 데이터 저장·재사용 조건은 직접 확인해 주십시오.
//
// 결과 파일의 한 줄은 다음 형태입니다.
//   { "requestedId": "...", "status": 200, "track": { ...ReccoBeats 원본 항목... } }
//   { "requestedId": "...", "status": 404 }   (응답에 포함되지 않은 곡)

import { appendFileSync, existsSync, mkdirSync, readFileSync, statSync, truncateSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const SONGS_PATH = new URL('../v1/data/songs.jsonl', import.meta.url);
const DEFAULT_OUT_PATH = fileURLToPath(new URL('./data/reccobeats-raw.jsonl', import.meta.url));
const API_BASE_URL = process.env.RECCOBEATS_API_BASE_URL ?? 'https://api.reccobeats.com/v1';
const BATCH_SIZE = 40;
const MAX_ATTEMPTS = 6;
const MAX_RATE_LIMIT_WAIT_SEC = 300;

const USAGE = `사용법:
  node v2/fetch-reccobeats-metadata.mjs --probe
  node v2/fetch-reccobeats-metadata.mjs [--out <결과 파일 경로>] [--limit <곡 수>] [--concurrency <동시 요청 수>]`;

function parseArgs(argv) {
    const options = { probe: false, out: undefined, limit: undefined, concurrency: 2 };
    for (let i = 0; i < argv.length; i++) {
        const flag = argv[i];
        if (flag === '--probe') {
            options.probe = true;
        } else if (['--out', '--limit', '--concurrency'].includes(flag)) {
            const value = argv[++i];
            if (value === undefined) fail(`${flag}에 값이 필요합니다.\n${USAGE}`);
            options[flag.slice(2)] = flag === '--out' ? value : Number(value);
        } else {
            fail(`알 수 없는 옵션입니다: ${flag}\n${USAGE}`);
        }
    }
    return options;
}

function fail(message) {
    console.error(message);
    process.exit(1);
}

const sleep = ms => new Promise(done => setTimeout(done, ms));

/** songs.jsonl에서 Spotify ID와 MuSe의 제목, 아티스트를 읽습니다. */
function readSongs(limit) {
    const songs = readFileSync(SONGS_PATH, 'utf8').trim().split('\n').map(line => {
        const record = JSON.parse(line);
        return { spotifyId: record[1], museTitle: record[15], museArtist: record[16] };
    });
    return limit === undefined ? songs : songs.slice(0, limit);
}

// ---------- 진행 상황 출력 ----------

let progressVisible = false;

/** 진행률 줄이 화면에 떠 있으면 지우고 메시지를 출력합니다. */
function log(message) {
    if (progressVisible) process.stdout.write('\r' + ' '.repeat(110) + '\r');
    console.log(message);
}

// ---------- 조회 ----------

// 모든 동시 요청이 공유하는 상태입니다. 호출 한도에 걸리면 전체가 함께 잠시 멈춥니다.
const shared = { pausedUntil: 0, stopReason: null, rateLimitStreak: 0 };

async function waitWhilePaused() {
    while (Date.now() < shared.pausedUntil && !shared.stopReason) {
        await sleep(Math.min(shared.pausedUntil - Date.now(), 1000));
    }
}

/** 429 응답을 처리합니다. 대기 시간이 너무 길면 전체 작업을 안전하게 멈춥니다. */
function handleRateLimit(response) {
    // 이미 다른 요청의 429로 전체가 멈춰 있다면 같은 사건이므로 대기 시간을 다시 늘리지 않습니다.
    if (Date.now() < shared.pausedUntil) return;
    const retryAfter = Number(response.headers.get('retry-after'));
    shared.rateLimitStreak++;
    const waitSec = retryAfter > 0 ? retryAfter : Math.min(10 * 2 ** (shared.rateLimitStreak - 1), MAX_RATE_LIMIT_WAIT_SEC);
    if (waitSec > MAX_RATE_LIMIT_WAIT_SEC) {
        shared.stopReason = `호출 한도에 도달했고 ${waitSec}초 뒤에 다시 시도하라는 응답을 받았습니다. 나중에 같은 명령으로 다시 실행해 주십시오.`;
        return;
    }
    shared.pausedUntil = Math.max(shared.pausedUntil, Date.now() + (waitSec + 1) * 1000);
    log(`호출 한도에 도달했습니다. 전체 요청을 ${waitSec + 1}초 동안 멈춥니다.`);
}

/** 재시도 사이에 대기합니다. 실패가 최대 횟수를 넘으면 오류를 던집니다. */
async function backoff(failures, reason) {
    const count = failures + 1;
    if (count >= MAX_ATTEMPTS) throw new Error(`${reason}로 재시도 횟수를 초과했습니다.`);
    const waitSec = Math.min(2 ** count, 30);
    log(`${reason}가 발생했습니다. ${waitSec}초 뒤에 다시 시도합니다. (${count}/${MAX_ATTEMPTS})`);
    await sleep(waitSec * 1000);
    return count;
}

/** 곡 최대 40개를 한 번에 요청합니다. 일시적 오류는 재시도하고, 전체를 멈춰야 하면 null을 돌려줍니다. */
async function requestBatch(ids) {
    let failures = 0;
    for (;;) {
        await waitWhilePaused();
        if (shared.stopReason) return null;
        let response;
        try {
            response = await fetch(`${API_BASE_URL}/track?ids=${ids.join(',')}`, { headers: { Accept: 'application/json' } });
        } catch (error) {
            failures = await backoff(failures, `네트워크 오류(${error.message})`);
            continue;
        }
        if (response.status === 200) {
            shared.rateLimitStreak = 0;
            const body = await response.json();
            return { status: 200, items: body.content ?? [] };
        }
        if (response.status === 429) {
            handleRateLimit(response);
            continue;
        }
        if (response.status >= 500) {
            failures = await backoff(failures, `서버 오류(HTTP ${response.status})`);
            continue;
        }
        if (response.status === 401 || response.status === 403) {
            shared.stopReason = `HTTP ${response.status} 응답을 받았습니다. 응답 본문: ${(await response.text()).slice(0, 200)}`;
            return null;
        }
        return { status: response.status };
    }
}

/** 요청한 곡마다 저장할 기록을 만듭니다. 응답의 href(Spotify 곡 링크)로 요청한 곡과 짝을 맞춥니다. */
function toRecords(ids, items) {
    const byId = new Map(items.map(item => [item.href?.split('/').pop(), item]));
    return ids.map(id => byId.has(id)
        ? { requestedId: id, status: 200, track: byId.get(id) }
        : { requestedId: id, status: 404 });
}

/** 곡 묶음을 조회합니다. 묶음 전체가 400이면 한 곡씩 나누어 문제가 되는 곡만 가려냅니다. */
async function lookup(ids) {
    const result = await requestBatch(ids);
    if (result === null) return null;
    if (result.status === 200) return toRecords(ids, result.items);
    if (result.status === 400 && ids.length > 1) {
        const records = [];
        for (const id of ids) {
            const single = await lookup([id]);
            if (single === null) return null;
            records.push(...single);
        }
        return records;
    }
    return ids.map(id => ({ requestedId: id, status: result.status }));
}

// ---------- 시험 호출 ----------

/** 처음 40곡을 조회해서 응답 필드를 보여줍니다. */
async function probe(songs) {
    const ids = songs.slice(0, BATCH_SIZE).map(song => song.spotifyId);
    const result = await requestBatch(ids);
    if (result === null || result.status !== 200) {
        console.log(`시험 호출에 실패했습니다. ${shared.stopReason ?? `HTTP ${result?.status}`}`);
        process.exit(1);
    }
    const records = toRecords(ids, result.items);
    const found = records.filter(record => record.status === 200);
    console.log(`[곡 조회] ${ids.length}곡을 요청해서 ${found.length}곡을 받았고, ${ids.length - found.length}곡은 응답에 없었습니다.`);
    if (found.length === 0) process.exit(1);
    const sample = found[0].track;
    console.log(`제목: ${sample.trackTitle}`);
    console.log(`아티스트: ${sample.artists?.map(artist => artist.name).join(', ')}`);
    console.log(`길이(ms): ${sample.durationMs}, ISRC: ${sample.isrc ?? '(없음)'}, popularity: ${sample.popularity ?? '(없음)'}`);
    console.log(`한국(KR) 재생 가능 표시: ${sample.availableCountries?.split(',').includes('KR')}`);
    console.log(`응답에 포함된 필드: ${Object.keys(sample).join(', ')}`);
    const bytes = Math.round(found.reduce((sum, record) => sum + JSON.stringify(record.track).length, 0) / found.length);
    console.log(`곡당 응답 크기: 약 ${bytes} 바이트 → ${Math.round(bytes * 27948 / 1024 / 1024)}MB 안팎 (27,948곡 기준, 추정)`);
    console.log('시험 호출이 끝났습니다.');
}

// ---------- 조회와 저장 ----------

/** 저장된 결과를 읽습니다. 쓰다가 끊겨서 깨진 줄은 무시합니다. */
function readRecords(outPath) {
    if (!existsSync(outPath)) return [];
    const records = [];
    for (const line of readFileSync(outPath, 'utf8').split('\n')) {
        if (!line.trim()) continue;
        try {
            records.push(JSON.parse(line));
        } catch {
            // 마지막 줄이 쓰는 도중에 끊긴 경우입니다. 해당 곡은 다시 조회됩니다.
        }
    }
    return records;
}

/** 아직 저장되지 않은 곡을 40곡씩 조회해서 결과가 나오는 대로 파일 끝에 이어 씁니다. 남은 곡 수를 돌려줍니다. */
async function fetchAll(songs, outPath, concurrency) {
    // 이전 실행이 줄 중간에서 끊겼다면 마지막 줄바꿈 뒤의 불완전한 줄을 잘라 냅니다. 해당 곡은 다시 조회됩니다.
    if (existsSync(outPath)) {
        const saved = readFileSync(outPath);
        if (saved.length > 0 && saved[saved.length - 1] !== 0x0a) truncateSync(outPath, saved.lastIndexOf(0x0a) + 1);
    }
    const previous = readRecords(outPath);
    const done = new Set(previous.map(record => record.requestedId));
    const pending = songs.filter(song => !done.has(song.spotifyId));
    const batches = [];
    for (let i = 0; i < pending.length; i += BATCH_SIZE) batches.push(pending.slice(i, i + BATCH_SIZE).map(song => song.spotifyId));
    let succeeded = previous.filter(record => record.status === 200).length;
    let failed = previous.length - succeeded;
    console.log(`전체 ${songs.length}곡 중 이미 저장된 ${done.size}곡(성공 ${succeeded}, 실패 ${failed})을 제외하고 ${pending.length}곡을 조회합니다.`);

    const startedAt = Date.now();
    let processed = 0;
    let nextBatch = 0;
    let lastRenderedAt = 0;

    function render(force = false) {
        const now = Date.now();
        if (!force && now - lastRenderedAt < 250) return;
        lastRenderedAt = now;
        const rate = processed / Math.max((now - startedAt) / 1000, 1);
        const remainingMinutes = rate > 0 ? Math.round((pending.length - processed) / rate / 60) : null;
        const line = `성공 ${succeeded}/${songs.length} (${(succeeded / songs.length * 100).toFixed(1)}%) | 실패 ${failed} | ` +
            `속도 ${rate.toFixed(1)}곡/초 | 남은 시간 ${remainingMinutes === null ? '계산 중' : `약 ${remainingMinutes}분`}`;
        if (process.stdout.isTTY) {
            process.stdout.write('\r' + line.padEnd(110));
            progressVisible = true;
        } else if (force || batches.length < 20 || nextBatch % 50 === 0) {
            console.log(line);
        }
    }

    async function worker() {
        while (!shared.stopReason) {
            const index = nextBatch++;
            if (index >= batches.length) return;
            const records = await lookup(batches[index]);
            if (records === null) return;
            appendFileSync(outPath, records.map(record => JSON.stringify(record)).join('\n') + '\n');
            for (const record of records) {
                processed++;
                if (record.status === 200) succeeded++; else failed++;
            }
            render();
        }
    }

    await Promise.all(Array.from({ length: Math.min(concurrency, batches.length) }, worker));
    render(true);
    if (progressVisible) {
        process.stdout.write('\n');
        progressVisible = false;
    }
    return pending.length - processed;
}

// ---------- 요약 ----------

/** 비교를 위해 대소문자, 발음 부호, 괄호 안의 내용, 기호를 제거합니다. */
function normalize(text) {
    return text.normalize('NFKD').replace(/\p{M}/gu, '').toLowerCase()
        .replace(/\(.*?\)|\[.*?\]/g, ' ').replace(/[^\p{L}\p{N}]+/gu, ' ').trim();
}

function similar(a, b) {
    const left = normalize(a), right = normalize(b);
    return left === '' || right === '' || left.includes(right) || right.includes(left);
}

function summarize(songs, outPath) {
    const records = readRecords(outPath);
    const byId = new Map(records.map(record => [record.requestedId, record]));
    const statusCounts = {};
    for (const record of records) statusCounts[record.status] = (statusCounts[record.status] ?? 0) + 1;
    const found = records.filter(record => record.status === 200);
    const titleMismatches = [], artistMismatches = [];
    for (const song of songs) {
        const track = byId.get(song.spotifyId)?.track;
        if (!track) continue;
        const artist = (track.artists ?? []).map(item => item.name).join(', ');
        if (!similar(song.museTitle, track.trackTitle ?? '')) titleMismatches.push([song, track.trackTitle, artist]);
        if (!similar(song.museArtist, artist)) artistMismatches.push([song, track.trackTitle, artist]);
    }
    console.log('\n===== 요약 =====');
    console.log(`저장된 곡: ${records.length}/${songs.length}, 응답 상태별 개수: ${JSON.stringify(statusCounts)}`);
    console.log(`한국(KR) 재생 가능으로 표시된 곡: ${found.filter(record => record.track.availableCountries?.split(',').includes('KR')).length}곡, ` +
        `표시되지 않은 곡: ${found.filter(record => !record.track.availableCountries?.split(',').includes('KR')).length}곡 (Spotify 판단과 약 93%만 일치하므로 참고용입니다)`);
    console.log(`길이 정보가 없는 곡: ${found.filter(record => !record.track.durationMs).length}곡, 제목이 없는 곡: ${found.filter(record => !record.track.trackTitle).length}곡, 아티스트가 없는 곡: ${found.filter(record => !record.track.artists?.length).length}곡`);
    console.log(`MuSe와 ReccoBeats의 제목이 크게 다른 곡: ${titleMismatches.length}곡, 아티스트가 크게 다른 곡: ${artistMismatches.length}곡`);
    for (const [label, list] of [['제목이', titleMismatches], ['아티스트가', artistMismatches]]) {
        if (list.length === 0) continue;
        console.log(`\n${label} 다른 곡의 예시 (최대 10곡):`);
        for (const [song, title, artist] of list.slice(0, 10)) {
            console.log(`  MuSe: "${song.museTitle} - ${song.museArtist}"  /  ReccoBeats: "${title} - ${artist}"`);
        }
    }
    console.log(`\n결과 파일 크기: ${(statSync(outPath).size / 1024 / 1024).toFixed(1)}MB`);
}

// ---------- 실행 ----------

const options = parseArgs(process.argv.slice(2));
if (options.limit !== undefined && !(options.limit > 0)) fail('--limit은 0보다 큰 숫자여야 합니다.');
if (!(options.concurrency >= 1)) fail('--concurrency는 1 이상의 숫자여야 합니다.');

const songs = readSongs(options.limit);
if (options.probe) {
    await probe(songs);
} else {
    const outPath = resolve((options.out ?? DEFAULT_OUT_PATH).replace(/^~(?=\/|$)/, process.env.HOME ?? '~'));
    mkdirSync(dirname(outPath), { recursive: true });
    process.once('SIGINT', () => {
        shared.stopReason ??= '중단 요청(Ctrl+C)을 받았습니다. 진행 중인 요청을 마무리하고 저장된 곳에서 멈춥니다.';
        log(`\n${shared.stopReason}`);
    });
    const remaining = await fetchAll(songs, outPath, options.concurrency);
    summarize(songs, outPath);
    console.log(`결과 파일: ${outPath}`);
    if (remaining > 0) {
        console.log(`\n아직 ${remaining}곡이 남아 있습니다. ${shared.stopReason ?? '작업이 중단되었습니다.'}`);
        console.log('같은 명령을 다시 실행하면 저장된 곳부터 이어서 진행합니다.');
        process.exit(2);
    }
}
