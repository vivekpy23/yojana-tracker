// Live visitor count on Vercel – the same contract as server/presence.py, sharing its Redis key:
//   POST /api/presence {"sid": "<32 hex>"}  heartbeat from an open, visible page -> {"online": n}
//   GET  /api/presence                      -> {"online": n}
// A sorted set {random session id: last heartbeat time}; nothing else (no address, no user agent). Sessions
// drop out 5 minutes after their last heartbeat, and the key itself expires when nobody is online.
const { transaction, now, limiter, client, json, body } = require("./_redis");

const KEY = "yojana:presence", WINDOW_S = 300, KEY_TTL_S = 600;
const allow = limiter(120, 60000);

module.exports = async function handler(req, res) {
  try {
    if (req.method === "GET") {
      const t = await now();
      const [, n] = await transaction([["ZREMRANGEBYSCORE", KEY, "-inf", String(t - WINDOW_S)], ["ZCARD", KEY]]);
      return json(res, 200, { online: Number(n) });
    }
    if (req.method !== "POST") {
      res.setHeader("Allow", "GET, POST");
      return json(res, 405, { error: "Method not allowed." });
    }
    const b = body(req);
    if (!b) return json(res, 415, { error: "Send JSON." });
    if (typeof b.sid !== "string" || !/^[0-9a-f]{32}$/.test(b.sid)) return json(res, 400, { error: "Bad session id." });
    if (!allow(client(req))) return json(res, 429, { error: "Too many requests." });
    const t = await now();
    const [, , n] = await transaction([
      ["ZADD", KEY, String(t), b.sid],
      ["ZREMRANGEBYSCORE", KEY, "-inf", String(t - WINDOW_S)],
      ["ZCARD", KEY],
      ["EXPIRE", KEY, String(KEY_TTL_S)],
    ]);
    return json(res, 200, { online: Number(n) });
  } catch (e) {  // store unreachable or not configured: say so, never make a number up
    return json(res, 503, { error: "Count unavailable." });
  }
};
