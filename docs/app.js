'use strict';
// Car Review phone app: downloads the encrypted car list and audio from the relay, plays cards
// with the steering-wheel (next/previous track) buttons, and keeps your grades on the phone,
// uploading them (encrypted) for the Anki add-on. Grades are not applied to Anki by this app.
//
// Like Anki, it syncs when you tap Sync, plus three times by itself: when it opens on a new
// Anki day (or with answers still to send), after Stop, and when a drive reaches the end. A sync
// downloads the list only if it changed, fetches new audio in a few bundles and deletes them
// from the mailbox, and tells the laptop which clips this phone holds, so nothing is sent twice.
(() => {
  // A local test relay can be set on a localhost page only (tools: wrangler dev).
  const RELAY = (location.hostname === 'localhost' && localStorage.getItem('relayUrl'))
    || 'https://car-review-relay.ankicardreview.workers.dev/v1';
  const APP_VERSION = 13; // also in index.html (app.js?v=); sent with grades so problems can be traced
  const AUDIO_CAP = 400e6;     // bytes of audio kept on the phone; the oldest-used clips go first
  const AUDIO_KEEP_DAYS = 60;  // clips no list has used for this long are removed
  const $ = (id) => document.getElementById(id);
  const enc = new TextEncoder();
  const dec = new TextDecoder();
  let clockOffset = 0; // only changed by the test hooks
  const now = () => Date.now() + clockOffset;

  // ---------- Storage (IndexedDB: settings/state in "kv", decrypted clips in "audio") ----------
  const DB = (() => {
    let opening;
    const open = () => opening || (opening = new Promise((resolve, reject) => {
      const req = indexedDB.open('car-review', 1);
      req.onupgradeneeded = () => { req.result.createObjectStore('kv'); req.result.createObjectStore('audio'); };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    }));
    // iOS can close the database connection while the app is in the background ("connection
    // lost"); then open it again and retry once instead of failing for the rest of the drive.
    async function run(store, mode, fn, retry = true) {
      try {
        const db = await open();
        return await new Promise((resolve, reject) => {
          const tx = db.transaction(store, mode);
          const req = fn(tx.objectStore(store));
          tx.oncomplete = () => resolve(req ? req.result : undefined);
          tx.onerror = () => reject(tx.error);
          tx.onabort = () => reject(tx.error || new Error('transaction aborted'));
        });
      } catch (err) {
        opening = null;
        if (retry) return run(store, mode, fn, false);
        throw err;
      }
    }
    return {
      get: (store, key) => run(store, 'readonly', (s) => s.get(key)),
      set: (store, key, value) => run(store, 'readwrite', (s) => { s.put(value, key); }),
      del: (store, key) => run(store, 'readwrite', (s) => { s.delete(key); }),
      keys: (store) => run(store, 'readonly', (s) => s.getAllKeys()),
      clear: (store) => run(store, 'readwrite', (s) => { s.clear(); }),
    };
  })();

  // ---------- Encryption (same scheme as the add-on's pairing.py) ----------
  const fromB64url = (s) => {
    s = s.replace(/-/g, '+').replace(/_/g, '/');
    while (s.length % 4) s += '=';
    return Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
  };
  const toB64url = (bytes) => btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  const toHex = (bytes) => [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('');

  async function derive(secret, label) {
    const base = await crypto.subtle.importKey('raw', secret, 'HKDF', false, ['deriveBits']);
    const bits = await crypto.subtle.deriveBits(
      { name: 'HKDF', hash: 'SHA-256', salt: enc.encode('car-review-v1'), info: enc.encode(label) }, base, 256);
    return new Uint8Array(bits);
  }
  async function makeKeys(code) {
    const secret = fromB64url(code);
    const aes = await crypto.subtle.importKey('raw', await derive(secret, 'encrypt'), 'AES-GCM', false, ['encrypt', 'decrypt']);
    return { box: toHex(await derive(secret, 'box')), write: toB64url(await derive(secret, 'write')), aes };
  }
  async function decrypt(name, buffer) {
    const b = new Uint8Array(buffer);
    return crypto.subtle.decrypt({ name: 'AES-GCM', iv: b.slice(0, 12), additionalData: enc.encode(name) }, keys.aes, b.slice(12));
  }
  async function encrypt(name, bytes) {
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const sealed = new Uint8Array(await crypto.subtle.encrypt({ name: 'AES-GCM', iv, additionalData: enc.encode(name) }, keys.aes, bytes));
    const out = new Uint8Array(12 + sealed.length);
    out.set(iv);
    out.set(sealed, 12);
    return out;
  }

  // ---------- Relay ----------
  let keys = null;
  // A weak signal can leave a request hanging for minutes; give up and retry later instead.
  const timeout = () => AbortSignal.timeout ? AbortSignal.timeout(20000) : undefined;
  async function relayGet(name) {
    const r = await fetch(`${RELAY}/${keys.box}/${name}`, { cache: 'no-store', signal: timeout() });
    if (r.status === 404) return null;
    if (!r.ok) throw new Error(`relay said ${r.status}`);
    return decrypt(name, await r.arrayBuffer());
  }
  // The list, unless it's unchanged since `since` (then the relay answers 204 with no body).
  async function relayGetIfChanged(name, since) {
    const q = since ? `?since=${encodeURIComponent(since)}` : '';
    const r = await fetch(`${RELAY}/${keys.box}/${name}${q}`, { cache: 'no-store', signal: timeout() });
    if (r.status === 404) return { missing: true };
    if (r.status === 204) return { unchanged: true };
    if (!r.ok) throw new Error(`relay said ${r.status}`);
    return { data: await decrypt(name, await r.arrayBuffer()), stamp: r.headers.get('X-Updated') };
  }
  async function relayPut(name, bytes) {
    const r = await fetch(`${RELAY}/${keys.box}/${name}`, {
      method: 'PUT', headers: { 'X-Write-Key': keys.write }, body: await encrypt(name, bytes), signal: timeout(),
    });
    if (!r.ok) throw new Error(`relay said ${r.status}`);
  }
  async function relayDelete(name) {
    const r = await fetch(`${RELAY}/${keys.box}/${name}`, { method: 'DELETE', headers: { 'X-Write-Key': keys.write }, signal: timeout() });
    if (!r.ok && r.status !== 404) throw new Error(`relay said ${r.status}`);
  }
  const pipe = (bytes, stream) => new Response(new Blob([bytes]).stream().pipeThrough(stream)).arrayBuffer();
  async function unzip(buffer) {
    const b = new Uint8Array(buffer);
    return b[0] === 0x1f && b[1] === 0x8b ? pipe(b, new DecompressionStream('gzip')) : buffer;
  }
  async function zip(bytes) {
    return window.CompressionStream ? new Uint8Array(await pipe(bytes, new CompressionStream('gzip'))) : bytes;
  }

  // ---------- App state ----------
  let list = null;           // the car list from the add-on
  let state = null;          // today's progress on this phone
  let audioReady = new Set();// clip names stored on the phone
  let clipMeta = {};         // clip name -> [bytes, the last Anki day a list used it]
  let synced = { at: 0, day: -1, stamp: null, cleared: false }; // the last successful sync
  // The log survives closing the app, so after a drive it shows what happened (Settings ▸ Log).
  const logLines = (() => { try { return JSON.parse(localStorage.getItem('log') || '[]'); } catch { return []; } })();
  function log(text) {
    logLines.push(`${new Date().toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit', second: '2-digit' })}  ${text}`);
    logLines.splice(0, Math.max(0, logLines.length - 300));
    try { localStorage.setItem('log', JSON.stringify(logLines)); } catch { /* full: the log is only a help */ }
    $('log').textContent = logLines.slice(-120).join('\n');
  }

  function ankiDay() {
    // Anki's day changes at its rollover hour; the list says when the day after `anki_today` starts.
    if (!list) return 0;
    const secs = now() / 1000;
    return secs < list.next_day_at ? list.anki_today : list.anki_today + 1 + Math.floor((secs - list.next_day_at) / 86400);
  }
  function freshState(day) {
    return { day, done: {}, learning: {}, grades: [], sessions: [], uploaded: 0, phone: state?.phone || crypto.randomUUID().slice(0, 8) };
  }
  async function loadState() {
    // Two copies: IndexedDB and localStorage. Use the one that's further along.
    let saved = null;
    try { saved = await DB.get('kv', 'state'); } catch (err) { log(`state: ${err.message}`); }
    let backup = null;
    try { backup = JSON.parse(localStorage.getItem('state') || 'null'); } catch { backup = null; }
    const progress = (x) => (x ? x.day * 1e6 + x.grades.length : -1);
    state = (progress(backup) > progress(saved) ? backup : saved) || freshState(ankiDay());
    if (state.day !== ankiDay()) {
      // A new Anki day: keep grades that still need uploading, start fresh otherwise.
      const pending = state.grades.slice(state.uploaded);
      saveDay(state); // its totals stay in "Past days"
      state = { ...freshState(ankiDay()), carried: pending };
    }
  }
  function saveState() {
    try { localStorage.setItem('state', JSON.stringify(state)); } catch { /* IndexedDB still has it */ }
    return DB.set('kv', 'state', state).catch((err) => log(`state not saved to storage: ${err.message}`));
  }

  function cardReady(c) { return audioReady.has(c.q_audio) && audioReady.has(c.a_audio); }
  // Every card carries the first Anki day it can be played (learning cards due today, interday
  // learning on their day, buried cards from tomorrow, reviews on their due day).
  // Only a list Anki made on the current Anki day is played: its "today" part is Anki's own
  // study queue for the day (buried siblings, limits and order all Anki's). Cards for later days
  // are sent ahead only so their audio downloads early; the morning list decides what's played.
  const listIsToday = () => !!list && list.anki_today === ankiDay();
  // Fallback when today's list never arrived (the laptop was asleep or unplugged): from the
  // previous day's list, play only reviews due today. Anki shows every due review up to the
  // review limit, so these are cards Anki shows today too. Left out, to be safe: new and learning
  // cards (which ones Anki picks can change overnight), notes with two cards due (Anki buries
  // one), and anything past the deck's review limit. Older lists without limits play nothing.
  const listIsYesterday = () => !!list && list.anki_today === ankiDay() - 1 && Array.isArray(list.review_limits);
  let fallback = { key: null, ids: new Set() };
  function fallbackIds() {
    const day = ankiDay();
    const key = `${list.made_at}:${day}`;
    if (fallback.key === key) return fallback.ids;
    const dueNow = list.cards.filter((c) => c.due_day <= day);
    const perNote = {};
    for (const c of dueNow) perNote[c.nid] = (perNote[c.nid] || 0) + 1;
    for (const [nid, dueDay] of list.held_back || []) if (dueDay <= day) perNote[nid] = (perNote[nid] || 0) + 1;
    const perBlock = {};
    const ids = new Set();
    for (const c of dueNow) {
      if (c.kind !== 'review' || perNote[c.nid] > 1) continue;
      perBlock[c.block] = (perBlock[c.block] || 0) + 1;
      if (perBlock[c.block] <= (list.review_limits[c.block] ?? 0)) ids.add(c.cid);
    }
    fallback = { key, ids };
    return ids;
  }
  function isDueToday(c) {
    if (listIsToday()) return c.due_day <= list.anki_today;
    return listIsYesterday() && fallbackIds().has(c.cid);
  }
  function todaysCards() { return list ? list.cards.filter((c) => isDueToday(c) && !state.done[c.cid]) : []; }

  // ---------- Choosing the next card, the way Anki does ----------
  function learningDue(c) {
    const own = state.learning[c.cid];
    if (own) return own.due;
    if (c.kind === 'learn') return (c.due_at || 0) * 1000;
    return null;
  }
  function nextCard() {
    const ahead = (list.learn_ahead_secs || 0) * 1000;
    const t = now();
    const cards = todaysCards().filter(cardReady);
    // Decks play one after another. As in Anki, a learning card from the list waits for its
    // deck's turn; cards you missed in the car come back when due, whichever deck is playing.
    const firstOther = cards.find((c) => learningDue(c) === null);
    const currentBlock = firstOther ? firstOther.block : Infinity;
    // As in Anki: learning cards due now come first, then the rest of the queue, and only when
    // nothing else is left, learning cards due within the learn-ahead limit.
    let soonest = null;
    let learning = null;
    let other = null;
    for (const c of cards) {
      const due = learningDue(c);
      if (due !== null) {
        if (!state.learning[c.cid] && c.block > currentBlock) continue; // its deck hasn't started yet
        if (due <= t + ahead && (!learning || due < learningDue(learning))) learning = c;
        if (soonest === null || due < soonest) soonest = due;
      } else if (!other) {
        other = c; // list order = Anki's order, one deck block after another
      }
    }
    if (learning && learningDue(learning) <= t) return { card: learning };
    if (other) return { card: other };
    if (learning) return { card: learning };
    if (soonest !== null) return { waitUntil: soonest };
    return {};
  }

  // What each answer changed, so a double-Back can undo it.
  const history = [];
  function remember(card) {
    const own = state.learning[card.cid];
    history.push({ card, learning: own ? { ...own } : undefined, done: !!state.done[card.cid], grades: state.grades.length });
    if (history.length > 50) history.shift();
  }
  function undoLast() {
    const h = history.pop();
    if (!h) return null;
    if (h.learning) state.learning[h.card.cid] = h.learning; else delete state.learning[h.card.cid];
    if (h.done) state.done[h.card.cid] = true; else delete state.done[h.card.cid];
    state.grades.length = h.grades;
    if (state.uploaded > h.grades) { state.uploaded = h.grades; state.resend = true; } // replace the uploaded file
    saveState();
    log(`undid the answer for ${h.card.cid}`);
    return h.card;
  }

  function answer(card, rating) {
    remember(card);
    const t = now();
    const own = state.learning[card.cid];
    let delay = null; // seconds until it comes back, or null when done for today
    if (own) {
      if (rating === 1) { own.idx = 0; delay = own.steps[0] ?? null; }
      else if (own.idx + 1 < own.steps.length) { own.idx += 1; delay = own.steps[own.idx]; }
      if (delay !== null) own.due = t + delay * 1000;
    } else {
      // First answer today: Anki itself worked out what Again and Good do when the list was made.
      delay = rating === 1 ? card.again_secs : card.good_secs;
      if (delay !== null && delay !== undefined) {
        let steps;
        let idx;
        if (card.kind === 'review') { steps = card.relearn_steps; idx = 0; }
        else if (card.kind === 'new') { steps = card.learn_steps; idx = rating === 1 ? 0 : 1; }
        else {
          steps = card.is_relearning ? card.relearn_steps : card.learn_steps;
          const current = Math.max(0, steps.length - (card.remaining_steps || 1));
          idx = rating === 1 ? 0 : current + 1;
        }
        state.learning[card.cid] = { steps, idx, due: t + delay * 1000 };
      } else {
        delay = null;
      }
    }
    if (delay === null) {
      delete state.learning[card.cid];
      state.done[card.cid] = true;
    }
    state.grades.push({ cid: card.cid, rating, at: t, ms: Math.min(60000, t - shownAt), day: state.day, list: list.made_at });
    if (driving && session) session.end = t;
    saveState();
    saveDay();
    log(`${rating === 1 ? 'Again' : rating === 3 ? 'Good' : 'Skip'} ${card.cid}${delay ? ` (back in ${Math.round(delay / 60)} min)` : ''}`);
  }

  function skip(card) {
    remember(card);
    delete state.learning[card.cid];
    state.done[card.cid] = true;
    state.grades.push({ cid: card.cid, rating: 0, at: now(), ms: 0, day: state.day, list: list.made_at, skip: 'not audio friendly' });
    saveState();
    log(`skipped ${card.cid} (marked not audio friendly)`);
  }

  // ---------- Uploading grades ----------
  // Only at Stop, at the end of a drive, and in a sync: never after each answer, never on a timer.
  const unsent = () => (state ? Math.max(0, state.grades.length - state.uploaded) + (state.carried || []).length : 0);
  const gradesWaiting = () => !!state && (unsent() > 0 || !!state.resend);
  let uploading = false;
  async function uploadGrades() {
    if (!keys || !state || uploading || !gradesWaiting()) return true;
    // No signal: grades stay on the phone, and go up when the 'online' event fires or at the next sync.
    if (navigator.onLine === false) { offline = true; renderSync(); return false; }
    uploading = true;
    try {
      const sent = state.grades.length;
      const all = [...(state.carried || []), ...state.grades];
      const body = enc.encode(JSON.stringify({ phone: state.phone, day: state.day, app: APP_VERSION, grades: all, timing: state.timing || [] }));
      await relayPut(`grades/${state.day}-${state.phone}`, body);
      state.uploaded = sent;
      state.carried = [];
      state.resend = false;
      offline = false;
      saveState();
      log(`${sent} answers uploaded`);
      return true;
    } catch (err) {
      log(`answers wait on the phone (${err.message})`);
      offline = true;
      return false;
    } finally {
      uploading = false;
      renderSync();
      showHome();
    }
  }

  // ---------- Sync: the list and audio down, answers up ----------
  let syncing = false;
  let offline = false;   // the last attempt couldn't reach the relay
  let syncNote = '';     // progress shown under the Sync button while it runs
  function saveSync() { return DB.set('kv', 'sync', synced).catch(() => {}); }
  // Why a sync is needed now ('' when it isn't). On a new Anki day, or with answers to send, the
  // app syncs by itself when it opens; otherwise only when you tap Sync.
  function syncReason() {
    if (!list) return 'Get your cards';
    if (gradesWaiting()) return `${unsent() || 'Changed'} answers to send`;
    if (synced.cleared) return 'Audio cleared: download it again';
    if (synced.day !== ankiDay()) return 'New Anki day';
    if (list.pending) return `${list.pending} more cards are being prepared`;
    return '';
  }
  async function runSync(why) {
    if (!keys || syncing || driving) return;
    syncing = true;
    syncNote = 'Checking…';
    renderSync();
    try {
      if (!(await uploadGrades())) throw new Error('no connection');
      const got = await relayGetIfChanged('list', list ? synced.stamp : null);
      if (got.missing) {
        message('Waiting for your first car list. On your computer, close Anki (or use Tools ▸ Car Review ▸ Send to Car). '
          + 'The first time, your computer also makes the audio, which can take a while; cards appear here as they are ready.');
      } else if (got.data) {
        list = JSON.parse(dec.decode(await unzip(got.data)));
        synced.stamp = got.stamp;
        await DB.set('kv', 'list', list);
        log(`new list: ${list.cards.length} cards${list.pending ? `, ${list.pending} more still being prepared` : ''}`);
      }
      if (list) {
        await loadState();
        if (Array.isArray(list.bundles)) await fetchBundles(); else await downloadClips();
        await tidyAudio();
        await sendHave();
        await clearFetched();
      }
      offline = false;
      synced = { ...synced, at: now(), day: list ? ankiDay() : -1, cleared: false };
      await saveSync();
      log(`synced (${why})${got.unchanged ? ': list unchanged' : ''}`);
      if (list) {
        message(list.pending ? `Your computer is still preparing ${list.pending} more cards. Sync again later.`
          : list.unvoiced ? `${list.unvoiced} more cards are waiting for audio on your computer. Plug it in and close Anki, or use Anki ▸ Tools ▸ Car Review ▸ Prepare audio.`
          : !todaysCards().length ? 'Nothing left to review today in your car decks. 🎉'
          : '');
      }
    } catch (err) {
      offline = true;
      log(`sync stopped: ${err.message}`);
      message(list ? 'No connection: using the cards already on this phone. Answers send by themselves when you have signal.'
        : 'No internet connection, and no cards on this phone yet. Connect to Wi-Fi and tap Sync.');
    } finally {
      syncing = false;
      syncNote = '';
      $('h-progress').classList.add('hidden');
      renderSync();
      showHome();
    }
  }
  // Only when it's needed: a new Anki day, answers to send, no list yet, or audio cleared.
  function syncIfNeeded(why) { if (syncReason()) runSync(why); }

  function renderSync() {
    const b = $('sync');
    if (!b) return;
    let cls, label, sub;
    const reason = syncReason();
    if (syncing) {
      cls = 'busy'; label = 'Syncing…'; sub = syncNote;
    } else if (offline && gradesWaiting()) {
      cls = 'off'; label = 'Answers saved on this phone'; sub = "They send by themselves when you're online";
    } else if (offline) {
      cls = 'off'; label = 'No connection'; sub = 'Using the cards on this phone · tap to try again';
    } else if (reason) {
      cls = 'need'; label = 'Sync now'; sub = reason;
    } else {
      cls = 'ok';
      label = `✓ Synced · ${new Date(synced.at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}`;
      sub = 'Up to date: nothing to do';
    }
    b.className = `sync ${cls}`;
    $('sync-label').textContent = label;
    $('sync-sub').textContent = sub;
  }
  function tapSync() {
    // Just synced and nothing has changed: say so instead of asking the relay again.
    if (!syncReason() && !offline && now() - synced.at < 60000) {
      $('sync-sub').textContent = 'Already up to date';
      return;
    }
    runSync('tapped');
  }

  const cueNames = () => (list.announce ? [...list.announce.blocks, list.announce.end].map((a) => a.audio) : []);
  const listClips = () => new Set([...list.cards.flatMap((c) => [c.q_audio, c.a_audio]), ...cueNames()]);
  function keepClip(name, blob) {
    audioReady.add(name);
    clipMeta[name] = [blob.size, ankiDay()];
    return DB.set('audio', name, blob);
  }
  // A bundle is many clips in one file: "CRB1", a 4-byte header length, the JSON header
  // [[name, size], ...], then the clips back to back (helper.py pack_bundle).
  async function storeBundle(buffer) {
    const b = new Uint8Array(buffer);
    if (String.fromCharCode(...b.slice(0, 4)) !== 'CRB1') throw new Error('not an audio bundle');
    const len = new DataView(buffer).getUint32(4);
    const header = JSON.parse(dec.decode(b.slice(8, 8 + len)));
    let at = 8 + len;
    for (const [name, size] of header) {
      await keepClip(name, new Blob([b.slice(at, at + size)], { type: 'audio/mpeg' }));
      at += size;
    }
    return header.length;
  }
  // Bundles listed in the list that this phone hasn't stored yet. Each is deleted from the
  // mailbox once "have" (below) tells the laptop its clips are here.
  async function fetchBundles() {
    const fetched = new Set((await DB.get('kv', 'fetched')) || []);
    const todo = list.bundles.filter((name) => !fetched.has(name));
    if (!todo.length) return;
    const bar = $('h-progress');
    bar.classList.remove('hidden');
    let done = 0;
    let clips = 0;
    for (const name of todo) {
      const data = await relayGet(name);
      if (data) clips += await storeBundle(data);
      fetched.add(name);
      await DB.set('kv', 'fetched', [...fetched]);
      done += 1;
      bar.value = Math.round((100 * done) / todo.length);
      syncNote = `Downloading audio… ${done} of ${todo.length}`;
      renderSync();
    }
    log(`audio: ${clips} clips from ${todo.length} bundles`);
  }
  // Lists from an add-on older than bundles name every clip as its own file.
  async function downloadClips() {
    const needed = [...listClips()].filter((n) => !audioReady.has(n));
    let done = 0;
    for (const name of needed) {
      const data = await relayGet(name);
      if (data) await keepClip(name, new Blob([data], { type: 'audio/mpeg' }));
      done += 1;
      syncNote = `Downloading audio… ${done} of ${needed.length}`;
      renderSync();
    }
  }
  // Clips stay on the phone after their card is played, so a card that comes back doesn't need
  // downloading again. Clips no list has used for AUDIO_KEEP_DAYS go, and the oldest-used go
  // first when they pass AUDIO_CAP.
  async function tidyAudio() {
    const day = ankiDay();
    const inList = listClips();
    for (const name of audioReady) {
      if (!clipMeta[name]) {
        const blob = await DB.get('audio', name).catch(() => null);
        clipMeta[name] = [blob ? blob.size : 30000, day];
      }
      if (inList.has(name)) clipMeta[name][1] = day;
    }
    let total = Object.values(clipMeta).reduce((sum, [size]) => sum + size, 0);
    const old = Object.entries(clipMeta).filter(([name]) => !inList.has(name)).sort((a, b) => a[1][1] - b[1][1]);
    let removed = 0;
    for (const [name, [size, last]] of old) {
      if (total <= AUDIO_CAP && last >= day - AUDIO_KEEP_DAYS) break;
      await DB.del('audio', name);
      audioReady.delete(name);
      delete clipMeta[name];
      total -= size;
      removed += 1;
    }
    for (const name of Object.keys(clipMeta)) if (!audioReady.has(name)) delete clipMeta[name];
    await DB.set('kv', 'clipMeta', clipMeta);
    if (removed) log(`audio: removed ${removed} old clips`);
  }
  // Tell the laptop which clips are on this phone, so it only sends the missing ones.
  async function sendHave() {
    const names = [...audioReady].sort();
    const sig = `${names.length}:${names[0] || ''}:${names[names.length - 1] || ''}:${Object.values(clipMeta).reduce((s, [b]) => s + b, 0)}`;
    if ((await DB.get('kv', 'haveSent')) === sig) return;
    await relayPut('have', await zip(enc.encode(JSON.stringify({ clips: names, app: APP_VERSION }))));
    await DB.set('kv', 'haveSent', sig);
  }
  async function clearFetched() {
    const fetched = (await DB.get('kv', 'fetched')) || [];
    for (const name of fetched) await relayDelete(name);
    await DB.set('kv', 'fetched', []);
  }
  const audioBytes = () => Object.values(clipMeta).reduce((sum, [size]) => sum + size, 0);

  // ---------- Home screen ----------
  function message(text) { $('h-message').textContent = text; }
  function show(section) {
    for (const id of ['unpaired', 'intro', 'home', 'drive']) $(id).classList.toggle('hidden', id !== section);
  }
  // On iPhone the Home Screen app has its own storage, so the walk-through waits until then.
  const isIOS = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const standalone = navigator.standalone === true || matchMedia('(display-mode: standalone)').matches;
  const introNeeded = () => localStorage.getItem('introDone') !== '1' && !(isIOS && !standalone);
  function showHome() {
    if (!keys) { show('unpaired'); return; }
    if (!$('drive').classList.contains('hidden')) return; // don't interrupt a drive
    if (introNeeded()) { show('intro'); return; }
    show('home');
    const due = todaysCards();
    const ready = due.filter(cardReady).length;
    $('h-due').textContent = list ? String(due.length) : '–';
    $('h-ready').innerHTML = list ? (ready === due.length ? `<span class="ok">✓ ${ready}</span>` : `<span class="warn">${ready} of ${due.length}</span>`) : '–';
    $('h-made').textContent = list ? new Date(list.made_at * 1000).toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit' }) : '–';
    const total = state ? state.grades.length : 0;
    $('h-up').innerHTML = !state || !total ? '–' : state.uploaded >= total ? '<span class="ok">✓ all</span>' : `<span class="warn">${total - state.uploaded} waiting</span>`;
    $('start').disabled = !list || ready === 0;
    renderToday();
    renderSync();
    $('h-storage').textContent = audioReady.size ? `${Math.round(audioBytes() / 1e6)} MB (${audioReady.size} clips)` : 'none';
    if (list && !listIsToday()) {
      message("Today's list isn't here yet. Your laptop makes it at 4:30 am if it's on and plugged in, "
        + 'or when you open or close Anki. '
        + (listIsYesterday() && due.length
          ? `Meanwhile you can play the ${due.length} reviews due today from last night's list; new and learning cards wait for today's list.`
          : 'Cards sent ahead are already downloaded.'));
    }
  }

  // ---------- Today's stats ----------
  // Every Start..Stop is a session (in the car or on screen). The "Today" panel adds up today's
  // sessions from the answers themselves, so it's right whether or not Stop was pressed, and
  // answers undone with ⏮⏮ don't count. Each day's totals stay on this phone for "Past days"
  // (they are never uploaded; Anki has its own Car stats).
  let session = null;
  const DAYS_KEPT = 30;
  const pastDays = (() => { try { return JSON.parse(localStorage.getItem('days') || '{}'); } catch { return {}; } })();
  function todaysSessions(s = state) {
    if (!s) return [];
    const recorded = (s.sessions || []).map((x) => ({ ...x, grades: [] }));
    const loose = [];
    for (const g of s.grades) {
      if (!(g.rating > 0)) continue;
      const into = recorded.find((x) => g.at >= x.start && g.at <= x.end);
      if (into) into.grades.push(g); else loose.push(g);
    }
    // Answers from before sessions were kept (app 12): a 10-minute gap starts a new session.
    const out = recorded.filter((x) => x.grades.length);
    let group = null;
    for (const g of loose) {
      if (!group || g.at - group.end > 600000) { group = { start: g.at - (g.ms || 0), end: g.at, grades: [] }; out.push(group); }
      group.grades.push(g);
      group.end = g.at;
    }
    return out.sort((a, b) => a.start - b.start);
  }
  function dayStats(s = state) {
    const sessions = todaysSessions(s);
    const byId = new Map(list ? list.cards.map((c) => [c.cid, c]) : []);
    const seen = new Set();
    const decks = new Map(); // deck -> [right, reviews], first answer of the day only
    let answers = 0, again = 0, right = 0, reviews = 0, ms = 0;
    for (const x of sessions) {
      ms += Math.max(0, x.end - x.start);
      for (const g of x.grades) {
        answers += 1;
        if (g.rating === 1) again += 1;
        const first = !seen.has(g.cid);
        seen.add(g.cid);
        const c = byId.get(g.cid);
        if (!first || !c || c.kind !== 'review') continue;
        const deck = c.deck.split('::')[0];
        const d = decks.get(deck) || [0, 0];
        d[0] += g.rating > 1 ? 1 : 0;
        d[1] += 1;
        decks.set(deck, d);
        reviews += 1;
        right += g.rating > 1 ? 1 : 0;
      }
    }
    return { sessions, answers, again, right, reviews, cards: seen.size, ms, mins: Math.round(ms / 60000), decks: [...decks] };
  }
  // Keep a day's totals for "Past days" (after every answer, and before a new day replaces them).
  function saveDay(s = state) {
    if (!s) return;
    const d = dayStats(s);
    if (!d.answers) { delete pastDays[s.day]; } else {
      pastDays[s.day] = { at: d.sessions[0].start, cards: d.cards, answers: d.answers, mins: d.mins, right: d.right, reviews: d.reviews };
    }
    for (const day of Object.keys(pastDays).map(Number).sort((a, b) => b - a).slice(DAYS_KEPT)) delete pastDays[day];
    try { localStorage.setItem('days', JSON.stringify(pastDays)); } catch { /* only a summary */ }
  }
  const esc = (t) => String(t).replace(/[&<>]/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[ch]));
  const pct = (a, b) => (b ? `${Math.round((100 * a) / b)}%` : '–');
  const minutes = (ms) => (ms > 0 && ms < 30000 ? '<1' : String(Math.round(ms / 60000)));
  const clock = (ms) => new Date(ms).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  function renderToday() {
    const d = dayStats();
    $('t-cards').textContent = String(d.cards);
    $('t-mins').textContent = minutes(d.ms);
    $('t-right').textContent = pct(d.right, d.reviews);
    const rows = d.answers ? [
      ['Answers', `${d.answers} (${d.again} Again)`],
      ...d.decks.map(([deck, [r, n]]) => [`${deck}: reviews right`, `${pct(r, n)} (${r} of ${n})`]),
      ['Time per answer', `${Math.round(d.ms / 1000 / d.answers)} s`],
    ] : [];
    $('t-body').innerHTML = rows.map(([k, v]) => `<div class="stat"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('')
      || '<p>No reviews yet today.</p>';
    $('t-sessions').innerHTML = d.sessions.map((x) => {
      const how = x.buttons === undefined ? '' : x.buttons >= x.screen ? ' · hands-free' : ' · on screen';
      return `<div class="session"><span>${esc(clock(x.start))}</span><b>${minutes(x.end - x.start)} min · ${x.grades.length} answers${how}</b></div>`;
    }).join('');
    $('t-sessions-head').classList.toggle('hidden', !d.sessions.length);
    const older = Object.entries(pastDays).map(([day, v]) => [Number(day), v])
      .filter(([day]) => !state || day < state.day).sort((a, b) => b[0] - a[0]).slice(0, 7);
    $('t-days').innerHTML = older.map(([, v]) => {
      const date = new Date(v.at).toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
      return `<div class="session"><span>${esc(date)}</span><b>${v.cards} cards · ${v.mins} min · ${pct(v.right, v.reviews)} right</b></div>`;
    }).join('');
    $('t-days-head').classList.toggle('hidden', !older.length);
  }

  // ---------- Audio player ----------
  // One audio element is the "now playing" source. Between clips it loops near-silence so iOS
  // keeps sending the steering-wheel buttons to this page.
  const player = new Audio();
  player.preload = 'auto';
  const silenceUrl = (() => {
    const rate = 22050, n = rate * 2, buf = new ArrayBuffer(44 + n * 2), v = new DataView(buf);
    const str = (o, s) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
    str(0, 'RIFF'); v.setUint32(4, 36 + n * 2, true); str(8, 'WAVE'); str(12, 'fmt ');
    v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, rate, true);
    v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); str(36, 'data'); v.setUint32(40, n * 2, true);
    for (let i = 0; i < n; i++) v.setInt16(44 + i * 2, Math.round((Math.random() - 0.5) * 6), true);
    return URL.createObjectURL(new Blob([buf], { type: 'audio/wav' }));
  })();
  let clipUrl = null;
  let afterClip = null;
  let clipActive = false;  // a card clip is playing (not the silence between clips)
  let trimEnd = null;      // where the clip's trailing silence starts
  const LEAD = 0.3;        // every clip starts with about 0.35 s of silence...
  const TAIL = 0.4;        // ...and ends with about 0.45 s: skip both
  const speed = () => Number($('speed').value);
  const playFailed = (e) => { if (e.name !== 'AbortError') log(`audio: ${e.name} ${e.message}`); };

  // ---------- Timing: how long each gap really is on this phone ----------
  // For each clip: how long after your press (or the clip before it) it started playing, how long
  // it played, and how long you took to press after it ended. Only times, no card text. They go
  // up with the grades, so the laptop can show where a drive's time goes (tools/timing_report.py).
  let pressT = null;   // when a button was last pressed (performance.now)
  let pressVia = null; // 'f' (⏭) or 'b' (⏮)
  let prevEnd = null;  // when the last clip ended
  let rec = null;      // the clip playing now
  let lastRec = null;  // the last finished clip, waiting for your next press
  let driveStartedAt = 0;
  let revealTimer = null; // plays the answer by itself (Settings: "Play the answer by itself")
  const perf = () => Math.round(performance.now());
  function markPress(via) {
    clearTimeout(revealTimer);
    if (via === 't' && lastRec?.entry) lastRec.entry.auto = 1;
    const t = perf();
    if (rec && rec.endAt == null) finishRec(t, true);
    if (lastRec && !lastRec.entry.cut && lastRec.entry.wait === undefined) lastRec.entry.wait = t - lastRec.endAt;
    lastRec = null;
    pressT = t;
    pressVia = via;
  }
  function startRec(kind) {
    if (rec && rec.endAt == null) finishRec(perf(), true);
    if (!driving) return;
    rec = { k: kind, from: pressT ?? prevEnd ?? perf(), via: pressT != null ? pressVia : null, at: now() };
    pressT = null;
  }
  function finishRec(t, cut = false) {
    const r = rec;
    rec = null;
    if (!r) return;
    r.endAt = t;
    prevEnd = t;
    const entry = { k: r.k, at: r.at, lag: r.playAt != null ? r.playAt - r.from : -1, len: r.playAt != null ? t - r.playAt : 0 };
    if (r.via) entry.via = r.via;
    if (cut) entry.cut = 1;
    r.entry = entry;
    lastRec = r;
    if (state) {
      state.timing = state.timing || [];
      state.timing.push(entry);
      state.timing.splice(0, Math.max(0, state.timing.length - 800));
    }
  }
  function timingSummary() {
    const xs = (state?.timing || []).filter((e) => e.at >= driveStartedAt);
    const med = (a) => (a.length ? a.slice().sort((x, y) => x - y)[Math.floor(a.length / 2)] : 0);
    const secs = (ms) => (ms / 1000).toFixed(1);
    const lags = xs.filter((e) => e.via && e.lag >= 0 && !(e.via === 'b' && e.k === 'q')).map((e) => e.lag);
    const len = (k) => xs.filter((e) => e.k === k && !e.cut).map((e) => e.len);
    const waits = xs.filter((e) => e.wait >= 0).map((e) => e.wait);
    return `timing (${xs.length} clips): sound ${med(lags)} ms after a press (slowest ${lags.length ? Math.max(...lags) : 0} ms); `
      + `questions ${secs(med(len('q')))} s, answers ${secs(med(len('a')))} s; you pressed ${secs(med(waits))} s after a clip ended`;
  }
  function playSilence() {
    if (rec) finishRec(perf(), true);
    clipToken += 1; // a clip still loading must not start after this
    clipActive = false;
    trimEnd = null;
    player.loop = true;
    player.src = silenceUrl;
    player.play().catch(playFailed);
  }
  function playSource(src, rate) {
    clipActive = true;
    trimEnd = null;
    player.loop = false;
    player.src = src;
    player.defaultPlaybackRate = rate;
    player.playbackRate = rate;
    player.preservesPitch = true;
    player.play().catch((e) => { playFailed(e); if (e.name !== 'AbortError') clipDone(); });
  }
  // Clips for the drive are copied into memory when it starts: iOS can cut a locked app off from
  // its stored data after a while, which used to stop the app until it was opened again.
  const memory = new Map();
  async function loadForDrive() {
    const names = [...todaysCards().flatMap((c) => [c.q_audio, c.a_audio]), ...cueNames()];
    let loaded = 0;
    for (const name of names) {
      if (memory.has(name)) continue;
      try {
        const blob = await DB.get('audio', name);
        if (blob) { memory.set(name, new Blob([await blob.arrayBuffer()], { type: 'audio/mpeg' })); loaded += 1; }
      } catch (err) { log(`audio not loaded: ${err.message}`); }
    }
    log(`drive: ${loaded} clips loaded into memory`);
  }
  let clipToken = 0;
  async function playClip(name, then = null) {
    const token = ++clipToken;
    afterClip = then;
    let blob = memory.get(name);
    if (!blob) {
      try { blob = await DB.get('audio', name); } catch (err) { log(`audio: ${err.message}`); }
    }
    if (token !== clipToken) return; // a button press moved on while the clip was loading
    if (!blob) { log(`audio: clip missing on the phone (${name.slice(0, 8)})`); clipActive = true; clipDone(); return; }
    startRec(current && name === current.q_audio ? 'q' : current && name === current.a_audio ? 'a' : 'cue');
    if (clipUrl) URL.revokeObjectURL(clipUrl);
    clipUrl = URL.createObjectURL(blob);
    playSource(clipUrl, speed());
  }
  function clipDone() {
    if (!clipActive) return;
    clipActive = false;
    trimEnd = null;
    finishRec(perf());
    const next = afterClip;
    afterClip = null;
    if (next) next(); else playSilence();
  }
  player.addEventListener('loadedmetadata', () => {
    if (!clipActive || !isFinite(player.duration) || player.duration < LEAD + TAIL + 0.5) return;
    player.currentTime = LEAD;
    trimEnd = player.duration - TAIL;
  });
  player.addEventListener('timeupdate', () => { if (clipActive && trimEnd && player.currentTime >= trimEnd) clipDone(); });
  player.addEventListener('ended', () => { if (!player.loop) clipDone(); });
  player.addEventListener('playing', () => { if (rec && clipActive && rec.playAt == null) rec.playAt = perf(); });
  // A clip that fails to load must not leave the app waiting.
  player.addEventListener('error', () => { if (clipActive) { log('audio: a clip failed to load, skipping it'); clipDone(); } });
  // iOS can pause the audio (a call, Siri, a navigation prompt). While driving, start it again,
  // or iOS hands the steering-wheel buttons to another app.
  player.addEventListener('pause', () => {
    if (!driving) return;
    setTimeout(() => { if (driving && player.paused && !player.ended) player.play().catch(playFailed); }, 800);
  });
  let heartbeat = null;

  // ---------- Driving ----------
  let driving = false;
  let current = null;   // card being played
  let stage = 'idle';   // question | answer | waiting | done
  let shownAt = 0;
  let waitTimer = null;
  let backTimer = null;
  let playedThisDrive = 0;
  let lastBlock = null; // the furthest deck reached this drive, for the "<deck> done" cue

  // The two big buttons on the screen do what their label says (no double taps needed there).
  const CONTROLS = {
    question: ['Hear again', 'Show answer', 'q'],
    answer: ['Again', 'Good', 'a'],
    waiting: ['Previous card', 'Check now', 'w'],
    done: ['Previous card', 'All done', 'w'],
    idle: ['Back', 'Forward', 'w'],
  };
  function setScreen(text, extra = '') {
    $('d-stage').textContent = text;
    $('d-count').textContent = extra;
    const [left, right, cls] = CONTROLS[stage] || CONTROLS.idle;
    $('d-back-label').textContent = left;
    $('d-fwd-label').textContent = right;
    $('d-controls').className = `row controls stage-${cls}`;
    $('d-fwd').disabled = stage === 'done' || stage === 'idle';
    $('d-skip').disabled = stage !== 'question';
    $('d-prev').disabled = !history.length;
    const remaining = todaysCards().filter(cardReady).length;
    $('d-bar').style.width = `${Math.round((100 * playedThisDrive) / Math.max(1, playedThisDrive + remaining))}%`;
    const title = current ? `${text} · ${current.deck}` : text;
    if (navigator.mediaSession) navigator.mediaSession.metadata = new MediaMetadata({ title, artist: 'Car Review' });
  }
  function counts() {
    const left = todaysCards().filter(cardReady).length;
    return `${playedThisDrive} done this session · ${left} left`;
  }
  const cue = (which) => (list.announce && audioReady.has(which.audio) ? which.audio : null);

  // After a question ends: silence, and if "Play the answer by itself" is on, the answer after
  // the chosen number of seconds (any press first cancels it).
  function playQuestion(card) {
    playClip(card.q_audio, () => {
      playSilence();
      if (!$('auto-answer').checked) return;
      clearTimeout(revealTimer);
      revealTimer = setTimeout(() => {
        if (!driving || current !== card || stage !== 'question') return;
        markPress('t');
        reveal('Answer (played by itself)');
      }, answerSecs() * 1000);
    });
  }
  const answerSecs = () => Math.min(60, Math.max(1, Math.round(Number($('answer-secs').value) || 8)));

  function goNext() {
    clearTimeout(waitTimer);
    clearTimeout(revealTimer);
    if (!driving) return;
    const pick = nextCard();
    if (pick.card) {
      current = pick.card;
      stage = 'question';
      shownAt = now();
      $('d-deck').textContent = current.deck;
      $('card-text').textContent = $('show-text').checked ? current.question : '';
      setScreen('Question', counts());
      // Moving on to the next deck: say the last one is done first.
      const done = lastBlock !== null && current.block > lastBlock && list.announce ? cue(list.announce.blocks[lastBlock]) : null;
      lastBlock = Math.max(lastBlock ?? current.block, current.block);
      const card = current;
      if (done) playClip(done, () => { if (current === card && stage === 'question') playQuestion(card); });
      else playQuestion(current);
    } else if (pick.waitUntil) {
      current = null;
      stage = 'waiting';
      const mins = Math.max(1, Math.round((pick.waitUntil - now()) / 60000));
      setScreen(`Next card in ${mins} min`, counts());
      $('card-text').textContent = '';
      playSilence();
      prevEnd = null; // the next question starts from its timer, not from a press
      waitTimer = setTimeout(goNext, Math.min(60000, Math.max(1000, pick.waitUntil - now())));
    } else {
      current = null;
      const first = stage !== 'done';
      stage = 'done';
      setScreen('All done for today 🎉', counts());
      $('card-text').textContent = '';
      const end = list.announce ? cue(list.announce.end) : null;
      if (first && end) playClip(end); else playSilence();
      if (first) uploadGrades(); // the drive is over: send the answers now if there's signal
    }
  }

  function reveal(screen = 'Answer') {
    stage = 'answer';
    setScreen(screen, counts());
    if ($('show-text').checked) $('card-text').textContent = `${current.question}\n\n→ ${current.answer}`;
    playClip(current.a_audio);
  }

  function forward() {
    markPress('f');
    if (stage === 'question') {
      reveal();
    } else if (stage === 'answer') {
      grade(3);
    } else if (stage === 'waiting' || stage === 'done') {
      goNext();
    }
  }

  // ⏮ on a question: once = hear it again (straight away), twice = go back to the card before,
  // from its question (⏭ then plays its answer), three times = skip this card as not
  // audio-friendly. Car buttons can reach the phone a beat late, so the second press counts for
  // BACK_WINDOW ms after the first, and going back waits BACK_CONFIRM ms for a third.
  // After the last card (or while waiting for one), a single ⏮ goes back a card.
  const BACK_WINDOW = 1200;
  const BACK_CONFIRM = 900;
  let backRun = { n: 0, at: 0, card: null };
  function back() {
    markPress('b');
    clearTimeout(backTimer);
    if (stage === 'answer') { backRun.n = 0; grade(1); return; }
    if (stage === 'waiting' || stage === 'done') { backRun.n = 0; previousCard(); return; }
    if (stage !== 'question') return;
    const t = performance.now();
    const more = backRun.n > 0 && backRun.card === current && t - backRun.at < (backRun.n === 1 ? BACK_WINDOW : BACK_CONFIRM);
    backRun = { n: more ? backRun.n + 1 : 1, at: t, card: current };
    if (backRun.n === 1) { repeatQuestion(); return; }
    if (backRun.n === 2) {
      playSilence();
      setScreen('Going back a card…', counts());
      backTimer = setTimeout(() => { backRun.n = 0; previousCard(); }, BACK_CONFIRM);
      return;
    }
    backRun.n = 0;
    skipCurrent();
  }
  function repeatQuestion() {
    if (stage !== 'question') return;
    shownAt = now();
    setScreen('Question', counts());
    playQuestion(current);
  }
  // Undo the last answer of this session and play that card again from its question.
  function previousCard() {
    clearTimeout(backTimer);
    const card = undoLast();
    if (!card) {
      if (stage === 'question') { setScreen('No earlier card this session', counts()); playQuestion(current); }
      return;
    }
    playedThisDrive = Math.max(0, playedThisDrive - 1);
    clearTimeout(waitTimer);
    current = card;
    stage = 'question';
    shownAt = now();
    $('d-deck').textContent = current.deck;
    $('card-text').textContent = $('show-text').checked ? current.question : '';
    setScreen('Back one card · ⏭ answer', counts());
    saveDay();
    playQuestion(current);
  }
  function skipCurrent() {
    if (stage !== 'question' || !current) return;
    skip(current);
    goNext();
  }

  function grade(rating) {
    answer(current, rating);
    playedThisDrive += 1;
    if (playedThisDrive % 25 === 0) log(timingSummary());
    goNext(); // straight on to the next question: no sound in between
  }

  // Keeps the screen from locking during a session when that setting is on (for reviewing on
  // screen, e.g. walking). The browser lets go of it whenever the app is hidden.
  let wakeLock = null;
  async function keepScreenOn(on) {
    try {
      if (on && $('keep-awake').checked && navigator.wakeLock && !wakeLock) {
        wakeLock = await navigator.wakeLock.request('screen');
        wakeLock.addEventListener('release', () => { wakeLock = null; });
      } else if (!on && wakeLock) {
        await wakeLock.release();
        wakeLock = null;
      }
    } catch (err) { log(`screen can't stay on: ${err.message}`); }
  }
  const pressed = (kind, fn) => () => { if (session) session[kind] += 1; fn(); };

  async function startDrive() {
    driving = true;
    driveStartedAt = now();
    session = { start: driveStartedAt, end: driveStartedAt, buttons: 0, screen: 0 };
    state.sessions = state.sessions || [];
    state.sessions.push(session);
    saveState();
    pressT = null; prevEnd = null; lastRec = null;
    playedThisDrive = 0;
    lastBlock = null;
    history.length = 0;
    backRun.n = 0;
    playSilence(); // must start from the Start tap: iOS only allows audio after a tap
    keepScreenOn(true);
    if (navigator.mediaSession) {
      navigator.mediaSession.setActionHandler('nexttrack', pressed('buttons', forward));
      navigator.mediaSession.setActionHandler('previoustrack', pressed('buttons', back));
      // Play/pause from a lock screen or headset: keep playing so the buttons stay connected.
      navigator.mediaSession.setActionHandler('play', () => player.paused && playSilence());
      navigator.mediaSession.setActionHandler('pause', () => setTimeout(() => player.paused && playSilence(), 300));
    }
    show('drive');
    setScreen('Getting the cards ready…');
    await loadForDrive();
    // Keep the audio running: if anything paused it, start it again.
    clearInterval(heartbeat);
    heartbeat = setInterval(() => {
      if (driving && player.paused) { log('audio was paused, restarting it'); player.play().catch(playFailed); }
    }, 5000);
    log(`drive started (app ${APP_VERSION})`);
    goNext();
  }
  function stopDrive() {
    if (driving && playedThisDrive) log(timingSummary());
    driving = false;
    stage = 'idle';
    session = null;
    clearTimeout(waitTimer);
    clearTimeout(backTimer);
    clearInterval(heartbeat);
    clearTimeout(revealTimer);
    keepScreenOn(false);
    player.pause();
    memory.clear();
    show('home');
    showHome();
    runSync('after Stop'); // sends the answers; the list check is a tiny "unchanged" reply
  }

  // ---------- Startup ----------
  async function init() {
    const speedSaved = localStorage.getItem('speed');
    if (speedSaved) $('speed').value = speedSaved;
    $('speed-val').textContent = Number($('speed').value).toFixed(1);
    $('show-text').checked = localStorage.getItem('showText') === '1';
    $('auto-answer').checked = localStorage.getItem('autoAnswer') === '1';
    $('answer-secs').value = localStorage.getItem('answerSecs') || '8';
    $('keep-awake').checked = localStorage.getItem('keepAwake') === '1';

    // Pairing link from the add-on's QR code: #pair=<secret> (never sent to any server).
    // A Home Screen app on iPhone gets its own storage, separate from Safari's, so in Safari the
    // code stays in the address: "Add to Home Screen" then saves it, and the Home Screen app
    // pairs itself when it first opens.
    const m = location.hash.match(/pair=([A-Za-z0-9_-]{40,})/);
    if (m) {
      const old = await DB.get('kv', 'pairing');
      if (old && old !== m[1]) {
        await DB.clear('audio');
        for (const key of ['list', 'state', 'sync', 'clipMeta', 'fetched', 'haveSent']) await DB.del('kv', key);
      }
      await DB.set('kv', 'pairing', m[1]);
      if (!old || old !== m[1]) log('paired with this laptop');
    }
    $('add-home').classList.toggle('hidden', standalone || !m);
    $('add-home-ios').classList.toggle('hidden', !isIOS);
    $('add-home-android').classList.toggle('hidden', isIOS);
    const code = await DB.get('kv', 'pairing');
    if (!code) { show('unpaired'); return; }
    // Ask the browser to keep our cards and audio even when storage gets tight.
    navigator.storage?.persist?.().catch(() => {});
    keys = await makeKeys(code);
    list = (await DB.get('kv', 'list')) || null;
    audioReady = new Set(await DB.keys('audio'));
    clipMeta = (await DB.get('kv', 'clipMeta')) || {};
    synced = { ...synced, ...((await DB.get('kv', 'sync')) || {}) };
    await loadState();
    showHome();
    log(`app version ${APP_VERSION} started`);
    syncIfNeeded('opened');
  }

  $('start').addEventListener('click', startDrive);
  $('sync').addEventListener('click', tapSync);
  $('clear-audio').addEventListener('click', async () => {
    if (!confirm(`Delete the audio stored on this phone (${Math.round(audioBytes() / 1e6)} MB)? `
      + 'Your cards and answers stay. The audio for your cards downloads again at your next sync, ideally on Wi-Fi.')) return;
    await DB.clear('audio');
    for (const key of ['clipMeta', 'haveSent']) await DB.del('kv', key);
    audioReady = new Set();
    clipMeta = {};
    synced.cleared = true;
    await saveSync();
    log('audio cleared from this phone');
    showHome();
  });
  $('intro-done').addEventListener('click', () => { localStorage.setItem('introDone', '1'); showHome(); window.scrollTo(0, 0); });
  $('show-intro').addEventListener('click', () => { localStorage.removeItem('introDone'); showHome(); window.scrollTo(0, 0); });
  $('d-fwd').addEventListener('click', pressed('screen', forward));
  // On the screen, the left button does what it says: hear the question again, Again, or go back.
  $('d-back').addEventListener('click', pressed('screen', () => {
    if (stage === 'question') { markPress('b'); clearTimeout(backTimer); backRun.n = 0; repeatQuestion(); } else back();
  }));
  $('d-prev').addEventListener('click', pressed('screen', () => { markPress('b'); previousCard(); }));
  $('d-skip').addEventListener('click', pressed('screen', () => {
    if (stage !== 'question' || !confirm("Skip this card in the car from now on? (It doesn't work as audio.) It stays due in Anki as normal.")) return;
    markPress('b');
    skipCurrent();
  }));
  $('d-stop').addEventListener('click', stopDrive);
  $('keep-awake').addEventListener('change', () => {
    localStorage.setItem('keepAwake', $('keep-awake').checked ? '1' : '0');
    if (driving) keepScreenOn($('keep-awake').checked);
  });
  $('speed').addEventListener('input', () => {
    $('speed-val').textContent = Number($('speed').value).toFixed(1);
    localStorage.setItem('speed', $('speed').value);
    if (!player.loop) player.playbackRate = speed();
  });
  $('show-text').addEventListener('change', () => localStorage.setItem('showText', $('show-text').checked ? '1' : '0'));
  $('auto-answer').addEventListener('change', () => localStorage.setItem('autoAnswer', $('auto-answer').checked ? '1' : '0'));
  $('answer-secs').addEventListener('change', () => {
    $('answer-secs').value = String(answerSecs());
    localStorage.setItem('answerSecs', $('answer-secs').value);
  });
  $('clear-today').addEventListener('click', async () => {
    if (!confirm("Reset today's progress on this phone? Grades not yet uploaded will be lost.")) return;
    state = freshState(ankiDay());
    await saveState();
    showHome();
  });
  $('unpair').addEventListener('click', async () => {
    if (!confirm('Unpair this phone? Its downloaded cards and audio are deleted. You can pair again by scanning the QR code in Anki.')) return;
    await DB.clear('audio'); await DB.clear('kv');
    keys = null; list = null; state = null; audioReady = new Set(); clipMeta = {};
    synced = { at: 0, day: -1, stamp: null, cleared: false };
    show('unpaired');
  });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') return;
    if (!driving) { syncIfNeeded('opened'); return; }
    if (player.paused) player.play().catch(playFailed);
    keepScreenOn(true);
  });
  window.addEventListener('online', () => { offline = false; if (gradesWaiting() && !driving) uploadGrades(); else renderSync(); });

  // Lets a desktop browser simulate the buttons and the clock while testing.
  window.carReviewTest = {
    forward, back, previousCard, next: () => goNext(), advance: (ms) => { clockOffset += ms; },
    timingSummary, dayStats, get stage() { return stage; }, get current() { return current; }, get state() { return state; }, get list() { return list; },
  };

  init().catch((err) => { log(`startup error: ${err.message}`); show('home'); });
})();
