'use strict';
// Car Review phone app: downloads the encrypted car list and audio from the relay, plays cards
// with the steering-wheel (next/previous track) buttons, and keeps your grades on the phone,
// uploading them (encrypted) for the Anki add-on. Grades are not applied to Anki by this app.
(() => {
  const RELAY = 'https://car-review-relay.ankicardreview.workers.dev/v1';
  const APP_VERSION = 9; // also in index.html (app.js?v=); sent with grades so problems can be traced
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
  async function relayPut(name, bytes) {
    const r = await fetch(`${RELAY}/${keys.box}/${name}`, {
      method: 'PUT', headers: { 'X-Write-Key': keys.write }, body: await encrypt(name, bytes), signal: timeout(),
    });
    if (!r.ok) throw new Error(`relay said ${r.status}`);
  }

  // ---------- App state ----------
  let list = null;           // the car list from the add-on
  let state = null;          // today's progress on this phone
  let audioReady = new Set();// clip names stored on the phone
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
    return { day, done: {}, learning: {}, grades: [], uploaded: 0, phone: state?.phone || crypto.randomUUID().slice(0, 8) };
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
    if (learning) return { card: learning };
    if (other) return { card: other };
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
    uploadSoon();
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
    saveState();
    uploadSoon();
    log(`${rating === 1 ? 'Again' : rating === 3 ? 'Good' : 'Skip'} ${card.cid}${delay ? ` (back in ${Math.round(delay / 60)} min)` : ''}`);
  }

  function skip(card) {
    remember(card);
    delete state.learning[card.cid];
    state.done[card.cid] = true;
    state.grades.push({ cid: card.cid, rating: 0, at: now(), ms: 0, day: state.day, list: list.made_at, skip: 'not audio friendly' });
    saveState();
    uploadSoon();
    log(`skipped ${card.cid} (marked not audio friendly)`);
  }

  // ---------- Uploading grades ----------
  let uploadTimer = null;
  function uploadSoon() {
    clearTimeout(uploadTimer);
    uploadTimer = setTimeout(uploadGrades, 3000);
  }
  let uploading = false;
  async function uploadGrades() {
    if (!keys || !state || uploading) return;
    const all = [...(state.carried || []), ...state.grades];
    if (!state.resend && (!all.length || state.uploaded >= state.grades.length && !(state.carried || []).length)) { showHome(); return; }
    // No signal: grades stay on the phone, and go up when the 'online' event fires.
    if (navigator.onLine === false) { showHome(); return; }
    uploading = true;
    try {
      const sent = state.grades.length;
      const body = enc.encode(JSON.stringify({ phone: state.phone, day: state.day, app: APP_VERSION, grades: all }));
      await relayPut(`grades/${state.day}-${state.phone}`, body);
      state.uploaded = sent;
      state.carried = [];
      state.resend = false;
      saveState();
    } catch (err) {
      log(`upload waiting for signal (${err.message})`);
      uploadTimer = setTimeout(uploadGrades, 30000);
    } finally {
      uploading = false;
    }
    showHome();
  }

  // ---------- Downloading the list and audio ----------
  async function refresh() {
    if (!keys) return;
    message('Checking for a new list…');
    try {
      const raw = await relayGet('list');
      if (!raw) {
        message('Waiting for your first car list. On your computer, close Anki (or use Tools ▸ Car Review ▸ Send to Car). '
          + 'The first time, your computer also makes the audio, which can take a while; cards appear here as they are ready.');
        showHome();
        return;
      }
      const fresh = JSON.parse(dec.decode(raw));
      if (!list || fresh.made_at !== list.made_at || fresh.cards.length !== list.cards.length) {
        list = fresh;
        await DB.set('kv', 'list', list);
        log(`new list: ${list.cards.length} cards${list.pending ? `, ${list.pending} more still being prepared` : ''}`);
      }
      await loadState();
      await downloadAudio();
      await uploadGrades();
      message(list.pending ? `Your computer is still preparing ${list.pending} more cards. Check again later.`
        : list.unvoiced ? `${list.unvoiced} more cards are waiting for audio on your computer. Plug it in and close Anki, or use Anki ▸ Tools ▸ Car Review ▸ Prepare audio.`
        : !todaysCards().length ? 'Nothing left to review today in your car decks. 🎉'
        : '');
    } catch (err) {
      log(`offline or relay error: ${err.message}`);
      message(list ? 'Offline: using the cards already on this phone. Grades upload by themselves when you have signal.'
        : 'No internet connection, and no cards on this phone yet. Connect to Wi-Fi and tap “Check for a new list”.');
    }
    showHome();
  }

  const cueNames = () => (list.announce ? [...list.announce.blocks, list.announce.end].map((a) => a.audio) : []);
  async function downloadAudio() {
    const needed = [...new Set([...list.cards.flatMap((c) => [c.q_audio, c.a_audio]), ...cueNames()])].filter((n) => !audioReady.has(n));
    if (!needed.length) return;
    const bar = $('h-progress');
    bar.classList.remove('hidden');
    let done = 0;
    const worker = async () => {
      while (needed.length) {
        const name = needed.shift();
        const data = await relayGet(name);
        if (data) {
          await DB.set('audio', name, new Blob([data], { type: 'audio/mpeg' }));
          audioReady.add(name);
        }
        done += 1;
        bar.value = Math.round(100 * done / (done + needed.length));
        message(`Downloading audio… ${done} clips`);
      }
    };
    await Promise.all([worker(), worker(), worker(), worker()]);
    bar.classList.add('hidden');
    // Remove clips no longer in the list, to free space.
    const keep = new Set([...list.cards.flatMap((c) => [c.q_audio, c.a_audio]), ...cueNames()]);
    for (const name of await DB.keys('audio')) {
      if (!keep.has(name)) { await DB.del('audio', name); audioReady.delete(name); }
    }
  }

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
    $('h-done').textContent = state ? String(Object.keys(state.done).length) : '–';
    const total = state ? state.grades.length : 0;
    $('h-up').innerHTML = !state || !total ? '–' : state.uploaded >= total ? '<span class="ok">✓ all</span>' : `<span class="warn">${total - state.uploaded} waiting</span>`;
    $('start').disabled = !list || ready === 0;
    if (list && !listIsToday()) {
      message("Today's list isn't here yet. Your laptop makes it at 4:30 am if it's on and plugged in, "
        + 'or when you open or close Anki. '
        + (listIsYesterday() && due.length
          ? `Meanwhile you can play the ${due.length} reviews due today from last night's list; new and learning cards wait for today's list.`
          : 'Cards sent ahead are already downloaded.'));
    }
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
  function playSilence() {
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
    if (clipUrl) URL.revokeObjectURL(clipUrl);
    clipUrl = URL.createObjectURL(blob);
    playSource(clipUrl, speed());
  }
  function clipDone() {
    if (!clipActive) return;
    clipActive = false;
    trimEnd = null;
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
  let backPresses = 0;
  let playedThisDrive = 0;
  let lastBlock = null; // the furthest deck reached this drive, for the "<deck> done" cue

  function setScreen(text, extra = '') {
    $('d-stage').textContent = text;
    $('d-count').textContent = extra;
    const title = current ? `${text} · ${current.deck}` : text;
    if (navigator.mediaSession) navigator.mediaSession.metadata = new MediaMetadata({ title, artist: 'Car Review' });
  }
  function counts() {
    const left = todaysCards().filter(cardReady).length;
    return `${playedThisDrive} done this drive · ${left} left`;
  }
  const cue = (which) => (list.announce && audioReady.has(which.audio) ? which.audio : null);

  function goNext() {
    clearTimeout(waitTimer);
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
      if (done) playClip(done, () => { if (current === card && stage === 'question') playClip(card.q_audio); });
      else playClip(current.q_audio);
    } else if (pick.waitUntil) {
      current = null;
      stage = 'waiting';
      const mins = Math.max(1, Math.round((pick.waitUntil - now()) / 60000));
      setScreen(`Next card in ${mins} min`, counts());
      $('card-text').textContent = '';
      playSilence();
      waitTimer = setTimeout(goNext, Math.min(60000, Math.max(1000, pick.waitUntil - now())));
    } else {
      current = null;
      const first = stage !== 'done';
      stage = 'done';
      setScreen('All done for today 🎉', counts());
      $('card-text').textContent = '';
      const end = list.announce ? cue(list.announce.end) : null;
      if (first && end) playClip(end); else playSilence();
    }
  }

  function forward() {
    if (stage === 'question') {
      stage = 'answer';
      setScreen('Answer · ⏭ Good  ⏮ Again', counts());
      if ($('show-text').checked) $('card-text').textContent = `${current.question}\n\n→ ${current.answer}`;
      playClip(current.a_audio);
    } else if (stage === 'answer') {
      grade(3);
    } else if (stage === 'waiting' || stage === 'done') {
      goNext();
    }
  }

  // ⏮ on a question: once = repeat it, twice quickly = undo the last answer, three times = skip
  // the card as not audio-friendly. Presses are counted for 0.6 s after the last one.
  function back() {
    if (stage === 'answer') { grade(1); return; }
    if (stage !== 'question' && stage !== 'waiting' && stage !== 'done') return;
    backPresses += 1;
    clearTimeout(backTimer);
    if (backPresses >= 3) { backPresses = 0; backAction(3); return; }
    backTimer = setTimeout(() => { const n = backPresses; backPresses = 0; backAction(n); }, 600);
  }
  function backAction(presses) {
    if (presses === 2) { undo(); return; }
    if (stage !== 'question') return;
    if (presses === 3) {
      skip(current);
      setScreen('Skipped (not audio-friendly)', counts());
      goNext();
      return;
    }
    shownAt = now();
    playClip(current.q_audio); // repeat the question
  }
  function undo() {
    const card = undoLast();
    if (!card) { if (stage === 'question') playClip(current.q_audio); return; }
    playedThisDrive = Math.max(0, playedThisDrive - 1);
    clearTimeout(waitTimer);
    current = card;
    stage = 'answer';
    shownAt = now();
    $('d-deck').textContent = current.deck;
    if ($('show-text').checked) $('card-text').textContent = `${current.question}\n\n→ ${current.answer}`;
    setScreen('Undone · ⏭ Good  ⏮ Again', counts());
    playClip(current.a_audio);
  }

  function grade(rating) {
    answer(current, rating);
    playedThisDrive += 1;
    goNext(); // straight on to the next question: no sound in between
  }

  async function startDrive() {
    driving = true;
    playedThisDrive = 0;
    lastBlock = null;
    history.length = 0;
    playSilence(); // must start from the Start tap: iOS only allows audio after a tap
    if (navigator.mediaSession) {
      navigator.mediaSession.setActionHandler('nexttrack', forward);
      navigator.mediaSession.setActionHandler('previoustrack', back);
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
    driving = false;
    stage = 'idle';
    clearTimeout(waitTimer);
    clearInterval(heartbeat);
    player.pause();
    memory.clear();
    show('home');
    uploadGrades();
    showHome();
  }

  // ---------- Startup ----------
  async function init() {
    const speedSaved = localStorage.getItem('speed');
    if (speedSaved) $('speed').value = speedSaved;
    $('speed-val').textContent = Number($('speed').value).toFixed(1);
    $('show-text').checked = localStorage.getItem('showText') === '1';

    // Pairing link from the add-on's QR code: #pair=<secret> (never sent to any server).
    // A Home Screen app on iPhone gets its own storage, separate from Safari's, so in Safari the
    // code stays in the address: "Add to Home Screen" then saves it, and the Home Screen app
    // pairs itself when it first opens.
    const m = location.hash.match(/pair=([A-Za-z0-9_-]{40,})/);
    if (m) {
      const old = await DB.get('kv', 'pairing');
      if (old && old !== m[1]) { await DB.clear('audio'); await DB.del('kv', 'list'); await DB.del('kv', 'state'); }
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
    await loadState();
    showHome();
    log(`app version ${APP_VERSION} started`);
    refresh();
  }

  $('start').addEventListener('click', startDrive);
  $('refresh').addEventListener('click', refresh);
  $('intro-done').addEventListener('click', () => { localStorage.setItem('introDone', '1'); showHome(); window.scrollTo(0, 0); });
  $('show-intro').addEventListener('click', () => { localStorage.removeItem('introDone'); showHome(); window.scrollTo(0, 0); });
  $('d-fwd').addEventListener('click', forward);
  $('d-back').addEventListener('click', back);
  $('d-stop').addEventListener('click', stopDrive);
  $('speed').addEventListener('input', () => {
    $('speed-val').textContent = Number($('speed').value).toFixed(1);
    localStorage.setItem('speed', $('speed').value);
    if (!player.loop) player.playbackRate = speed();
  });
  $('show-text').addEventListener('change', () => localStorage.setItem('showText', $('show-text').checked ? '1' : '0'));
  $('clear-today').addEventListener('click', async () => {
    if (!confirm("Reset today's progress on this phone? Grades not yet uploaded will be lost.")) return;
    state = freshState(ankiDay());
    await saveState();
    showHome();
  });
  $('unpair').addEventListener('click', async () => {
    if (!confirm('Unpair this phone? Its downloaded cards and audio are deleted. You can pair again by scanning the QR code in Anki.')) return;
    await DB.clear('audio'); await DB.clear('kv');
    keys = null; list = null; state = null; audioReady = new Set();
    show('unpaired');
  });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') return;
    if (!driving) { refresh(); return; }
    if (player.paused) player.play().catch(playFailed);
  });
  window.addEventListener('online', () => uploadSoon());

  // Lets a desktop browser simulate the buttons and the clock while testing.
  window.carReviewTest = {
    forward, back, undo, next: () => goNext(), advance: (ms) => { clockOffset += ms; },
    get stage() { return stage; }, get current() { return current; }, get state() { return state; }, get list() { return list; },
  };

  init().catch((err) => { log(`startup error: ${err.message}`); show('home'); });
})();
