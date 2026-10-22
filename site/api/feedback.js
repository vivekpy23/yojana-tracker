// Feedback form on Vercel – the same rules as server/feedback_server.py:
//   POST /api/feedback {"message": "..."} -> 201 {"ok": true, "id"}
// Vercel's filesystem doesn't keep files, so each entry {"id", "message", "timestamp"} is pushed onto the Redis
// list "yojana:feedback" (newest first). Read it in the Upstash console: LRANGE yojana:feedback 0 -1
const crypto = require("crypto");
const { command, limiter, client, json, body } = require("./_redis");

const KEY = "yojana:feedback", MIN_LEN = 3, MAX_LEN = 1000;
const CONTROL = /[\x00-\x08\x0b-\x1f\x7f]/g;
const allow = limiter(5, 600000);

module.exports = async function handler(req, res) {
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    return json(res, 405, { error: "Method not allowed." });
  }
  const b = body(req);
  if (!b) return json(res, 415, { error: "Send JSON." });
  const msg = typeof b.message === "string" ? b.message.replace(CONTROL, "").trim() : "";
  if (msg.length < MIN_LEN) return json(res, 400, { error: "Please enter a message." });
  if (msg.length > MAX_LEN) return json(res, 400, { error: `Please keep your message under ${MAX_LEN} characters.` });
  if (!allow(client(req))) return json(res, 429, { error: "Too many messages. Please try again in a few minutes." });
  const entry = { id: crypto.randomUUID().replace(/-/g, ""), message: msg,
                  timestamp: new Date().toISOString().replace(/\.\d+Z$/, "+00:00") };
  try {
    await command(["LPUSH", KEY, JSON.stringify(entry)]);
  } catch (e) {
    return json(res, 503, { error: "Couldn’t send your message right now. Please try again later." });
  }
  return json(res, 201, { ok: true, id: entry.id });
};
