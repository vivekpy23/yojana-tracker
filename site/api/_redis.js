// Upstash Redis over its REST API (no packages needed). Vercel's Upstash integration sets KV_REST_API_URL and
// KV_REST_API_TOKEN; an Upstash database connected by hand uses the UPSTASH_REDIS_REST_* names.
// Files starting with "_" are helpers, not routes.
const URL_ = process.env.KV_REST_API_URL || process.env.UPSTASH_REDIS_REST_URL || "";
const TOKEN = process.env.KV_REST_API_TOKEN || process.env.UPSTASH_REDIS_REST_TOKEN || "";

async function call(path, body) {
  if (!URL_ || !TOKEN) throw new Error("Redis is not configured");
  const r = await fetch(URL_.replace(/\/$/, "") + path, {
    method: "POST",
    headers: { Authorization: "Bearer " + TOKEN, "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(3000),
  });
  if (!r.ok) throw new Error("Redis HTTP " + r.status);
  return r.json();
}

// One command, e.g. ["LPUSH", key, value] -> its result.
async function command(cmd) {
  const d = await call("", cmd);
  if (d.error) throw new Error(d.error);
  return d.result;
}

// Several commands as one MULTI/EXEC transaction -> their results, in order.
async function transaction(cmds) {
  const out = await call("/multi-exec", cmds);
  return out.map((d) => {
    if (d.error) throw new Error(d.error);
    return d.result;
  });
}

// Redis's own clock in seconds, so every function instance uses the same time.
async function now() {
  const [sec, usec] = await command(["TIME"]);
  return Number(sec) + Number(usec) / 1e6;
}

// Requests per key in a sliding window, kept in this instance's memory only (best effort on serverless; the
// address is never stored in Redis or logged).
function limiter(limit, windowMs) {
  const hits = new Map();
  return function allow(key) {
    const t = Date.now(), list = (hits.get(key) || []).filter((h) => t - h < windowMs);
    if (list.length >= limit) { hits.set(key, list); return false; }
    list.push(t);
    hits.set(key, list);
    if (hits.size > 10000) hits.clear();
    return true;
  };
}

function client(req) {
  return String(req.headers["x-forwarded-for"] || "").split(",")[0].trim() || "unknown";
}

function json(res, code, body) {
  res.statusCode = code;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.end(JSON.stringify(body));
}

// The JSON body: Vercel parses it for application/json; fall back to the raw text.
function body(req) {
  if (!String(req.headers["content-type"] || "").includes("application/json")) return undefined;
  let b = req.body;
  if (typeof b === "string") { try { b = JSON.parse(b); } catch (e) { return undefined; } }
  return b && typeof b === "object" ? b : undefined;
}

module.exports = { command, transaction, now, limiter, client, json, body };
