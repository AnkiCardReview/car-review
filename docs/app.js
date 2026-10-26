'use strict';
// Car Review phone app: downloads the encrypted car list and audio from the relay, plays cards
// with the steering-wheel (next/previous track) buttons, and keeps your grades on the phone,
// uploading them (encrypted) for the Anki add-on. Grades are not applied to Anki by this app.
(() => {
  const RELAY = 'https://car-review-relay.ankicardreview.workers.dev/v1';
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
    async function run(store, mode, fn) {
      const db = await open();
      return new Promise((resolve, reject) => {
        const tx = db.transaction(store, mode);
        const req = fn(tx.objectStore(store));
        tx.oncomplete = () => resolve(req ? req.result : undefined);
        tx.onerror = () => reject(tx.error);
      });
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
  async function relayGet(name) {
    const r = await fetch(`${RELAY}/${keys.box}/${name}`, { cache: 'no-store' });
    if (r.status === 404) return null;
    if (!r.ok) throw new Error(`relay said ${r.status}`);
    return decrypt(name, await r.arrayBuffer());
  }
  async function relayPut(name, bytes) {
    const r = await fetch(`${RELAY}/${keys.box}/${name}`, {
      method: 'PUT', headers: { 'X-Write-Key': keys.write }, body: await encrypt(name, bytes),
    });
    if (!r.ok) throw new Error(`relay said ${r.status}`);
  }

  // ---------- App state ----------
  let list = null;           // the car list from the add-on
  let state = null;          // today's progress on this phone
  let audioReady = new Set();// clip names stored on the phone
  const logLines = [];
  function log(text) {
    logLines.push(`${new Date().toLocaleTimeString()}  ${text}`);
    $('log').textContent = logLines.slice(-80).join('\n');
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
    state = (await DB.get('kv', 'state')) || freshState(ankiDay());
    if (state.day !== ankiDay()) {
      // A new Anki day: keep grades that still need uploading, start fresh otherwise.
      const pending = state.grades.slice(state.uploaded);
      state = { ...freshState(ankiDay()), carried: pending };
    }
  }
  const saveState = () => DB.set('kv', 'state', state);

  function cardReady(c) { return audioReady.has(c.q_audio) && audioReady.has(c.a_audio); }
  function isDueToday(c) { return c.kind === 'learn' || c.due_day <= ankiDay(); }
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
    let soonest = null;
    let learning = null;
    let other = null;
    for (const c of todaysCards()) {
      if (!cardReady(c)) continue;
      const due = learningDue(c);
      if (due !== null) {
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

  function answer(card, rating) {
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
  async function uploadGrades() {
    if (!keys) return;
    const all = [...(state.carried || []), ...state.grades];
    if (!all.length || state.uploaded >= state.grades.length && !(state.carried || []).length) { showHome(); return; }
    try {
      const body = enc.encode(JSON.stringify({ phone: state.phone, day: state.day, grades: all }));
      await relayPut(`grades/${state.day}-${state.phone}`, body);
      state.uploaded = state.grades.length;
      state.carried = [];
      saveState();
    } catch (err) {
      log(`upload waiting for signal (${err.message})`);
      uploadTimer = setTimeout(uploadGrades, 30000);
    }
    showHome();
  }

  // ---------- Downloading the list and audio ----------
  async function refresh() {
    if (!keys) return;
    message('Checking for a new list…');
    try {
      const raw = await relayGet('list');
      if (!raw) { message('No car list yet. In Anki, use Tools ▸ Car Review ▸ Send to Car.'); showHome(); return; }
      const fresh = JSON.parse(dec.decode(raw));
      if (!list || fresh.made_at !== list.made_at || fresh.cards.length !== list.cards.length) {
        list = fresh;
        await DB.set('kv', 'list', list);
        log(`new list: ${list.cards.length} cards${list.pending ? `, ${list.pending} more still being prepared` : ''}`);
      }
      await loadState();
      await downloadAudio();
      await uploadGrades();
      message(list.pending ? `Your laptop is still preparing ${list.pending} more cards. Check again later.`
        : list.unvoiced ? `${list.unvoiced} more cards are waiting for audio on your laptop (Anki ▸ Tools ▸ Car Review ▸ Prepare audio).`
        : '');
    } catch (err) {
      log(`offline or relay error: ${err.message}`);
      message(list ? 'Offline: using the list already on this phone.' : 'Offline, and no list on this phone yet.');
    }
    showHome();
  }

  async function downloadAudio() {
    const needed = [...new Set(list.cards.flatMap((c) => [c.q_audio, c.a_audio]))].filter((n) => !audioReady.has(n));
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
    const keep = new Set(list.cards.flatMap((c) => [c.q_audio, c.a_audio]));
    for (const name of await DB.keys('audio')) {
      if (!keep.has(name)) { await DB.del('audio', name); audioReady.delete(name); }
    }
  }

  // ---------- Home screen ----------
  function message(text) { $('h-message').textContent = text; }
  function show(section) {
    for (const id of ['unpaired', 'home', 'drive']) $(id).classList.toggle('hidden', id !== section);
  }
  function showHome() {
    if (!keys) { show('unpaired'); return; }
    if (!$('drive').classList.contains('hidden')) return; // don't interrupt a drive
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
  const speed = () => Number($('speed').value);
  const playFailed = (e) => { if (e.name !== 'AbortError') log(`audio: ${e.name} ${e.message}`); };
  function playSilence() {
    player.loop = true;
    player.src = silenceUrl;
    player.play().catch(playFailed);
  }
  function playSource(src, rate) {
    player.loop = false;
    player.src = src;
    player.defaultPlaybackRate = rate;
    player.playbackRate = rate;
    player.preservesPitch = true;
    player.play().catch(playFailed);
  }
  async function playClip(name, then = null) {
    const blob = await DB.get('audio', name);
    if (clipUrl) URL.revokeObjectURL(clipUrl);
    clipUrl = URL.createObjectURL(blob);
    afterClip = then;
    playSource(clipUrl, speed());
  }
  function playSound(name, then) {
    afterClip = then;
    playSource(`sounds/${name}.mp3`, 1);
  }
  player.addEventListener('ended', () => {
    if (player.loop) return;
    const next = afterClip;
    afterClip = null;
    if (next) next(); else playSilence();
  });

  // ---------- Driving ----------
  let driving = false;
  let current = null;   // card being played
  let stage = 'idle';   // question | answer | feedback | waiting | done
  let shownAt = 0;
  let waitTimer = null;
  let backTimer = null;
  let playedThisDrive = 0;

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
      playClip(current.q_audio);
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
      stage = 'done';
      setScreen('All done for today 🎉', counts());
      $('card-text').textContent = '';
      playSound('good', null);
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

  function back() {
    if (stage === 'answer') { grade(1); return; }
    if (stage !== 'question') return;
    if (backTimer) {
      // Second press within half a second: skip this card as not audio friendly.
      clearTimeout(backTimer);
      backTimer = null;
      skip(current);
      playSound('again', goNext);
      return;
    }
    backTimer = setTimeout(() => {
      backTimer = null;
      shownAt = now();
      playClip(current.q_audio); // repeat the question
    }, 500);
  }

  function grade(rating) {
    stage = 'feedback';
    answer(current, rating);
    playedThisDrive += 1;
    setScreen(rating === 3 ? 'Good ✓' : 'Again ↺', counts());
    playSound(rating === 3 ? 'good' : 'again', goNext);
  }

  function startDrive() {
    driving = true;
    playedThisDrive = 0;
    playSilence(); // must start from the Start tap: iOS only allows audio after a tap
    if (navigator.mediaSession) {
      navigator.mediaSession.setActionHandler('nexttrack', forward);
      navigator.mediaSession.setActionHandler('previoustrack', back);
      // Play/pause from a lock screen or headset: keep playing so the buttons stay connected.
      navigator.mediaSession.setActionHandler('play', () => player.paused && playSilence());
      navigator.mediaSession.setActionHandler('pause', () => setTimeout(() => player.paused && playSilence(), 300));
    }
    show('drive');
    goNext();
  }
  function stopDrive() {
    driving = false;
    stage = 'idle';
    clearTimeout(waitTimer);
    player.pause();
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
    const standalone = navigator.standalone === true || matchMedia('(display-mode: standalone)').matches;
    const m = location.hash.match(/pair=([A-Za-z0-9_-]{40,})/);
    if (m) {
      const old = await DB.get('kv', 'pairing');
      if (old && old !== m[1]) { await DB.clear('audio'); await DB.del('kv', 'list'); await DB.del('kv', 'state'); }
      await DB.set('kv', 'pairing', m[1]);
      if (!old || old !== m[1]) log('paired with this laptop');
    }
    $('add-home').classList.toggle('hidden', standalone || !m);
    const code = await DB.get('kv', 'pairing');
    if (!code) { show('unpaired'); return; }
    // Ask the browser to keep our cards and audio even when storage gets tight.
    navigator.storage?.persist?.().catch(() => {});
    keys = await makeKeys(code);
    list = (await DB.get('kv', 'list')) || null;
    audioReady = new Set(await DB.keys('audio'));
    await loadState();
    showHome();
    refresh();
  }

  $('start').addEventListener('click', startDrive);
  $('refresh').addEventListener('click', refresh);
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
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible' && !driving) refresh(); });

  // Lets a desktop browser simulate the buttons and the clock while testing.
  window.carReviewTest = {
    forward, back, advance: (ms) => { clockOffset += ms; },
    get stage() { return stage; }, get current() { return current; }, get state() { return state; }, get list() { return list; },
  };

  init().catch((err) => { log(`startup error: ${err.message}`); show('home'); });
})();
