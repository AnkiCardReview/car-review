// Car Review relay: a dumb, encrypted mailbox between a user's laptop and phone.
//
// Everything stored here is encrypted on the user's devices before upload, so the relay only
// ever holds scrambled bytes. A "box" is named by a one-way hash of the user's pairing secret.
// The first write to a box records a hash of its write key; later writes must present the
// same key, so nobody else can overwrite or fill someone's box.
//
//   GET    /v1/<box>?prefix=grades/   list files (name, size, updated)
//   GET    /v1/<box>/<name>           download a file
//   PUT    /v1/<box>/<name>           upload a file      (header X-Write-Key)
//   DELETE /v1/<box>/<name>           delete a file      (header X-Write-Key)
//   DELETE /v1/<box>                  delete the whole box (header X-Write-Key)
//
// Files older than 3 days are deleted by the daily cron job.

const MAX_FILE_BYTES = 1_900_000; // D1 rows top out at 2 MB
const MAX_BOX_BYTES = 400_000_000; // enough for a big first backfill within 3 days
const KEEP_MS = 3 * 24 * 60 * 60 * 1000;
const BOX = /^[0-9a-f]{64}$/;
const NAME = /^[a-z0-9][a-z0-9/_.-]{0,127}$/;

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin");
    const cors = corsHeaders(origin, env);
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
    try {
      const response = await route(request, env);
      for (const [k, v] of Object.entries(cors)) response.headers.set(k, v);
      return response;
    } catch (err) {
      return json({ error: "server error" }, 500, cors);
    }
  },

  async scheduled(_event, env) {
    const cutoff = Date.now() - KEEP_MS;
    await env.DB.prepare("DELETE FROM files WHERE updated < ?").bind(cutoff).run();
    // Forget boxes that have had no files for 30 days.
    await env.DB.prepare(
      "DELETE FROM boxes WHERE created < ? AND box NOT IN (SELECT DISTINCT box FROM files)"
    ).bind(Date.now() - 30 * 24 * 60 * 60 * 1000).run();
  },
};

async function route(request, env) {
  const url = new URL(request.url);
  const parts = url.pathname.split("/").filter(Boolean);
  if (parts[0] !== "v1" || !BOX.test(parts[1] || "")) return json({ error: "not found" }, 404);
  const box = parts[1];
  const name = parts.slice(2).join("/");
  if (name && !NAME.test(name)) return json({ error: "bad file name" }, 400);

  if (request.method === "GET" && !name) {
    const prefix = url.searchParams.get("prefix") || "";
    const { results } = await env.DB.prepare(
      "SELECT name, length(data) AS size, updated FROM files WHERE box = ? AND substr(name, 1, ?) = ? ORDER BY name"
    ).bind(box, prefix.length, prefix).all();
    return json({ files: results });
  }

  if (request.method === "GET") {
    const row = await env.DB.prepare("SELECT data FROM files WHERE box = ? AND name = ?").bind(box, name).first();
    if (!row) return json({ error: "not found" }, 404);
    // D1 hands BLOB columns back as an array of byte values.
    return new Response(new Uint8Array(row.data), { headers: { "Content-Type": "application/octet-stream", "Cache-Control": "no-store" } });
  }

  // Everything below changes the box, so it needs the box's write key.
  const allowed = await checkWriteKey(request, env, box);
  if (allowed !== true) return allowed;

  if (request.method === "PUT" && name) {
    const body = await request.arrayBuffer();
    if (body.byteLength > MAX_FILE_BYTES) return json({ error: "file too large" }, 413);
    const used = await env.DB.prepare(
      "SELECT coalesce(sum(length(data)), 0) AS bytes FROM files WHERE box = ? AND name != ?"
    ).bind(box, name).first();
    if (used.bytes + body.byteLength > MAX_BOX_BYTES) return json({ error: "box is full" }, 413);
    await env.DB.prepare(
      "INSERT INTO files (box, name, data, updated) VALUES (?, ?, ?, ?) " +
      "ON CONFLICT (box, name) DO UPDATE SET data = excluded.data, updated = excluded.updated"
    ).bind(box, name, body, Date.now()).run();
    return json({ ok: true });
  }

  if (request.method === "DELETE" && name) {
    await env.DB.prepare("DELETE FROM files WHERE box = ? AND name = ?").bind(box, name).run();
    return json({ ok: true });
  }

  if (request.method === "DELETE") {
    await env.DB.batch([
      env.DB.prepare("DELETE FROM files WHERE box = ?").bind(box),
      env.DB.prepare("DELETE FROM boxes WHERE box = ?").bind(box),
    ]);
    return json({ ok: true });
  }

  return json({ error: "method not allowed" }, 405);
}

// Returns true, or an error Response. The first write to a new box claims it.
async function checkWriteKey(request, env, box) {
  const key = request.headers.get("X-Write-Key") || "";
  if (key.length < 32) return json({ error: "missing write key" }, 401);
  const hash = await sha256Hex(key);
  const existing = await env.DB.prepare("SELECT write_hash FROM boxes WHERE box = ?").bind(box).first();
  if (!existing) {
    await env.DB.prepare("INSERT OR IGNORE INTO boxes (box, write_hash, created) VALUES (?, ?, ?)")
      .bind(box, hash, Date.now()).run();
    const claimed = await env.DB.prepare("SELECT write_hash FROM boxes WHERE box = ?").bind(box).first();
    return claimed.write_hash === hash ? true : json({ error: "wrong write key" }, 403);
  }
  return existing.write_hash === hash ? true : json({ error: "wrong write key" }, 403);
}

async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function corsHeaders(origin, env) {
  const allowed = (env.ALLOWED_ORIGINS || "").split(",").map((s) => s.trim());
  if (!origin || !allowed.includes(origin)) return {};
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "GET, PUT, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Write-Key",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
}

function json(value, status = 200, headers = {}) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store", ...headers },
  });
}
