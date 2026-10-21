/* Yojana Tracker – static front end. Reads data/site.json, renders #/ and #/state/XX. */
(function () {
  "use strict";
  var app = document.getElementById("app");
  var DATA = null;
  var showAll = false;

  // ---------- helpers ----------
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function cr(n) { return "₹" + Math.round(n).toLocaleString("en-IN") + " Cr"; }
  // Headline form: ₹20.57 lakh Cr (exact amount shown underneath); smaller amounts stay exact.
  function headline(n) { return Math.abs(n) >= 100000 ? "₹" + (n / 100000).toFixed(2) + " lakh Cr" : cr(n); }
  function val(fig) { return fig && typeof fig.value === "number" ? fig.value : null; }
  var DASH = '<span class="dash" aria-label="not yet available">—</span>';
  function cell(fig) { var v = val(fig); return v === null ? DASH : cr(v); }
  // State revenue: published BE with a link to its source; "N/A" when not published.
  function revCell(fig) {
    var v = val(fig);
    // the ↗ sits in a fixed-width slot (empty for N/A) so the figures stay right-aligned
    if (v === null) return '<span class="dash">N/A</span><span class="src-ico"></span>';
    return cr(v) + '<span class="src-ico">' + (fig.source && fig.source.url ? '<a href="' + esc(fig.source.url) + '" target="_blank" rel="noopener" title="' + esc(fig.source.title || "Source") + '" aria-label="Source">↗</a>' : "") + "</span>";
  }
  function disbScheme(sc) {
    if (sc.despatched == null) return DASH;
    var src = sc.despatched_source || {};
    return '<a href="' + esc(src.url) + '" target="_blank" rel="noopener" title="' +
      esc([src.title, src.period, "as of " + src.as_of].filter(Boolean).join(" · ")) + '">' + cr(sc.despatched) + "</a>";
  }
  var BADGE = {
    verified: ["verified", "Verified"],
    secondary: ["secondary", "Budget docs (via PRS)"],
    reported: ["unverified", "Reported — awaiting official verification"]
  };
  function badge(status) {
    var b = BADGE[status] || BADGE.reported;
    return '<span class="badge ' + b[0] + '">' + b[1] + "</span>";
  }
  var FUNDING = { state: "State scheme", centrally_sponsored: "Centrally sponsored (Centre + state)", central: "Central scheme" };
  // Scheme name (linked to its official page) with funding type, department and note underneath.
  function schemeCell(sc) {
    var name = sc.official_url
      ? '<a href="' + esc(sc.official_url) + '" target="_blank" rel="noopener"' + (sc.description ? ' title="' + esc(sc.description) + '"' : "") + ">" + esc(sc.name) + "</a>"
      : esc(sc.name);
    var meta = [FUNDING[sc.funding], sc.department].filter(Boolean).join(" · ");
    return name + (meta ? '<div class="hint">' + esc(meta) + "</div>" : "") + (sc.note ? '<div class="hint">' + esc(sc.note) + "</div>" : "");
  }
  // Compact source link: a short publisher name ("PRS ↗"), the full document title on hover.
  var PUBLISHERS = [[/prsindia\.org$/, "PRS"], [/indiabudget\.gov\.in$/, "Union Budget"], [/cag\.gov\.in$|cloudfront\.net$/, "CAG"],
    [/myscheme\.gov\.in$/, "myScheme"], [/pib\.gov\.in$/, "PIB"]];
  function publisher(url) {
    var host = "";
    try { host = new URL(url).hostname.replace(/^www\./, ""); } catch (e) { return "Source"; }
    var hit = PUBLISHERS.find(function (p) { return p[0].test(host); });
    return hit ? hit[1] : host;
  }
  function sourceLink(src, label) {
    return src && src.url ? '<a class="src" href="' + esc(src.url) + '" target="_blank" rel="noopener" title="' + esc(src.title || "Source") + '">' +
      esc(label || publisher(src.url)) + '<span aria-hidden="true"> ↗</span></a>' : "";
  }

  function countUp(el, target) {
    var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce) { el.textContent = headline(target); return; }
    var start = performance.now(), dur = 900;
    function tick(now) {
      var p = Math.min(1, (now - start) / dur), e = 1 - Math.pow(1 - p, 3);
      el.textContent = headline(target * e);
      if (p < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  }
  function animate() {
    app.querySelectorAll("[data-count]").forEach(function (el) { countUp(el, +el.getAttribute("data-count")); });
    requestAnimationFrame(function () {
      app.querySelectorAll(".fill[data-w]").forEach(function (el) { el.style.width = el.getAttribute("data-w") + "%"; });
    });
  }

  // ---------- pieces ----------
  // kind: "exp" (navy), "welfare" (teal) or omitted (neutral)
  function statCard(title, fig, kind) {
    var v = val(fig);
    var basis = fig && ({ RE: "Revised Estimate", BE: "Budget Estimate" }[fig.basis] || fig.basis);
    // title · figure · estimate label only; sources and details stay in the data
    return '<div class="card stat' + (kind ? " " + kind : "") + '">' +
      '<div class="eyebrow">' + esc(title) + "</div>" +
      '<div class="figure">' + (v === null
        ? '<div class="big pending">Pending</div>'
        : '<div class="big" data-count="' + v + '">' + headline(v) + "</div>" +
          (basis ? '<div class="exact">' + esc(basis) + "</div>" : "")) + "</div>" +
      "</div>";
  }

  function barChart(title, hint, rows, color, fmt) {
    var max = Math.max.apply(null, rows.map(function (r) { return r.value; }));
    return '<div class="card"><div class="section-head"><h3>' + esc(title) + '</h3><span class="hint">' + esc(hint) + "</span></div>" +
      '<div class="bars">' + rows.map(function (r) {
        return '<div class="bar-row"><div class="labels"><span>' + esc(r.name) + '</span><span class="v">' + fmt(r) + "</span></div>" +
          '<div class="track" role="img" aria-label="' + esc(r.name + ": " + fmt(r)) + '"><div class="fill" style="background:' + color +
          '" data-w="' + (100 * r.value / max).toFixed(1) + '"></div></div></div>';
      }).join("") + "</div></div>";
  }

  // ---------- treemap: area ∝ allocation (squarified) ----------
  // Colours come from the palette by position (--tm-1 … --tm-8, cycling), never from the scheme.
  var TM_COLORS = 8;
  function treemap(title, hint, rows, label) {
    var data = rows.filter(function (r) { return r.value > 0; })
      .sort(function (a, b) { return b.value - a.value; })
      .map(function (r, i) {
        var text = r.name + ": " + cr(r.value);
        return '<div class="tm-cell" role="listitem" tabindex="0" title="' + esc(text) + '" aria-label="' + esc(text) +
          '" data-v="' + r.value + '" style="background:var(--tm-' + (i % TM_COLORS + 1) + ')">' +
          '<span class="tm-name">' + esc(r.name) + '</span><span class="tm-val">' + cr(r.value) + "</span></div>";
      });
    return '<div class="card"><div class="section-head"><h3>' + esc(title) + '</h3><span class="hint">' + esc(hint) + "</span></div>" +
      '<div class="treemap" role="list" aria-label="' + esc(label || title) + '">' + data.join("") + "</div>" +
      '<p class="tm-more hint" aria-hidden="true"></p></div>';
  }
  // Squarified layout (Bruls et al.): rows of cells whose worst aspect ratio is as close to 1 as possible.
  function squarify(areas, x, y, w, h) {
    var out = [], row = [], i = 0;
    function worst(r, side) {
      var s = 0, mx = 0, mn = Infinity;
      r.forEach(function (a) { s += a; mx = Math.max(mx, a); mn = Math.min(mn, a); });
      return Math.max(side * side * mx / (s * s), (s * s) / (side * side * mn));
    }
    function place(r) {
      var s = r.reduce(function (t, a) { return t + a; }, 0);
      if (w >= h) {               // a column on the left
        var cw = s / h, cy = y;
        r.forEach(function (a) { out.push([x, cy, cw, a / cw]); cy += a / cw; });
        x += cw; w -= cw;
      } else {                    // a row along the top
        var rh = s / w, cx = x;
        r.forEach(function (a) { out.push([cx, y, a / rh, rh]); cx += a / rh; });
        y += rh; h -= rh;
      }
    }
    while (i < areas.length) {
      var side = Math.min(w, h), next = row.concat([areas[i]]);
      if (!row.length || worst(next, side) <= worst(row, side)) { row = next; i++; }
      else { place(row); row = []; }
    }
    if (row.length) place(row);
    return out;
  }
  function layoutTreemaps() {
    document.querySelectorAll(".treemap").forEach(function (box) {
      var cells = Array.prototype.slice.call(box.children);
      var W = box.clientWidth, H = box.clientHeight;
      if (!cells.length || !W || !H) return;
      var vals = cells.map(function (c) { return +c.getAttribute("data-v"); });
      var total = vals.reduce(function (t, v) { return t + v; }, 0);
      var rects = squarify(vals.map(function (v) { return v / total * W * H; }), 0, 0, W, H);
      var more = [];
      cells.forEach(function (c, i) {
        var r = rects[i];
        c.style.left = (100 * r[0] / W) + "%"; c.style.top = (100 * r[1] / H) + "%";
        c.style.width = (100 * r[2] / W) + "%"; c.style.height = (100 * r[3] / H) + "%";
        // full: name (up to 3 lines) + amount · small: one-line name + amount ·
        // tiny: too small for readable text, so it is labelled in the key under the treemap instead
        var tiny = r[2] < 92 || r[3] < 44;  // "₹37,100 Cr" needs ~90px at the small size
        c.className = "tm-cell" + (tiny ? " tiny" : r[2] < 120 || r[3] < 64 ? " small" : "");
        if (tiny) more.push('<span class="tm-key"><i style="background:' + c.style.background + '"></i>' + c.innerHTML + "</span>");
      });
      var key = box.parentNode.querySelector(".tm-more");
      if (key) { key.innerHTML = more.join(""); key.hidden = !more.length; }
    });
  }
  var relayout = null;
  window.addEventListener("resize", function () {
    if (relayout) cancelAnimationFrame(relayout);
    relayout = requestAnimationFrame(layoutTreemaps);
  });

  // ---------- views ----------
  function rankStates() {
    return DATA.states.slice().sort(function (a, b) {
      return (val(b.beneficiary_announced) || -1) - (val(a.beneficiary_announced) || -1) || a.name.localeCompare(b.name);
    });
  }

  function home() {
    var c = DATA.central;
    var total = c.total_expenditure;
    var areas = c.spend_areas.map(function (r) { return { name: r.name, value: r.value, pct: 100 * r.value / total }; });
    var ranked = rankStates();
    var rows = showAll ? ranked : ranked.slice(0, 5);

    app.innerHTML =
      '<section class="grid2 stats">' +
        statCard("Central govt · scheme allocations", c.schemes_announced, "welfare") +
        statCard("Central govt · gross tax collected", c.gross_tax, "rev") +
        statCard("Central Revenue", c.revenue, "rev") +
      "</section>" +
      '<section class="stack"><div class="grid2">' +
        barChart("Where the centre spends", "Top 5 · % of total expenditure", areas, "var(--navy)", function (r) { return r.pct.toFixed(1) + "%"; }) +
        treemap("Top 5 central schemes", "Area ∝ allocation · RE " + DATA.fy, c.top_schemes) +
        '</div><p class="hint" style="margin:0">Charts: ' + sourceLink(c.charts_source) + "</p>" +
      "</section>" +
      '<section class="states">' +
        '<div class="section-head"><h2>' + (showAll ? "All states &amp; UTs" : "Top 5 states") + ' by schemes announced</h2><span class="hint">Tap a state to see its schemes</span></div>' +
        '<div class="search"><label for="q">Find any state or UT</label><div class="box">' +
          '<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="color:var(--muted)"><circle cx="11" cy="11" r="7"/><line x1="16.5" y1="16.5" x2="21" y2="21"/></svg>' +
          '<input id="q" type="search" list="state-list" placeholder="Type a state name" autocomplete="off"></div>' +
          '<datalist id="state-list">' + DATA.states.map(function (s) { return '<option value="' + esc(s.name) + '">'; }).join("") + "</datalist></div>" +
        '<div class="table-box"><table><thead><tr><th>State</th><th class="num">Budget (RE)</th><th class="num">Schemes announced ▼</th><th class="num">Own tax (RE)</th><th class="num">State revenue (BE 2025-26)</th></tr></thead><tbody>' +
        rows.map(function (s) {
          var has = val(s.beneficiary_announced) !== null;
          return '<tr class="link' + (has ? "" : " dim") + '" data-code="' + s.code + '">' +
            '<td><a class="state-link" href="#/state/' + s.code + '">' + esc(s.name) + "</a>" + (s.ut ? ' <span class="hint">UT</span>' : "") + "</td>" +
            '<td class="num">' + cell(s.budget) + "</td>" +
            '<td class="num">' + cell(s.beneficiary_announced) + "</td>" +
            '<td class="num">' + cell(s.tax) + "</td>" +
            '<td class="num">' + revCell(s.revenue) + "</td></tr>";
        }).join("") +
        "</tbody></table></div>" +
        '<div class="row-actions"><p class="hint" style="margin:0">— Not yet available. See “How we calculate” below for definitions.</p>' +
          '<button class="btn" id="toggle">' + (showAll ? "Show top 5" : "Show all " + DATA.states.length + " states &amp; UTs") + "</button></div>" +
      "</section>";

    document.getElementById("toggle").onclick = function () { showAll = !showAll; home(); };
    app.querySelectorAll("tr.link").forEach(function (tr) {
      tr.onclick = function (e) { if (e.target.tagName !== "A") location.hash = "#/state/" + tr.getAttribute("data-code"); };
    });
    var q = document.getElementById("q");
    function go() {
      var t = q.value.trim().toLowerCase();
      var hit = DATA.states.find(function (s) { return s.name.toLowerCase() === t; });
      if (hit) location.hash = "#/state/" + hit.code;
    }
    q.addEventListener("change", go);
    q.addEventListener("keydown", function (e) {
      if (e.key === "Enter") {
        var t = q.value.trim().toLowerCase();
        var hit = DATA.states.find(function (s) { return s.name.toLowerCase().indexOf(t) === 0; });
        if (hit) location.hash = "#/state/" + hit.code;
      }
    });
    q.addEventListener("input", go);
    animate();
    layoutTreemaps();
  }

  function state(code) {
    var s = DATA.states.find(function (x) { return x.code === code; });
    if (!s) { location.hash = "#/"; return; }
    var top = s.schemes.slice(0, 5);
    // every scheme with an officially confirmed allocation (a reported scheme carries no amount)
    var allocated = s.schemes.filter(function (sc) { return sc.announced != null; })
      .map(function (sc) { return { name: sc.name, value: sc.announced }; });
    var sources = [];
    top.forEach(function (sc) { (sc.sources || []).forEach(function (src) { if (!sources.some(function (x) { return x.url === src.url; })) sources.push(src); }); });

    app.innerHTML =
      '<div><a class="back" href="#/">← All states</a></div>' +
      '<div class="section-head"><h1>' + esc(s.name) + "</h1>" + (s.ut ? '<span class="hint">Union Territory</span>' : "") + "</div>" +
      '<section class="grid2 stats">' +
        statCard("State budget (total expenditure excl. debt repayment)", s.budget, "exp") +
        statCard("Own tax revenue", s.tax, "rev") +
        statCard("Schemes announced", s.beneficiary_announced, "welfare") +
      "</section>" +
      '<section class="states">' +
        "<h2>Top 5 yojanas by allocation</h2>" +
        (allocated.length > 1 ? treemap("Allocations by yojana", "Area ∝ allocation · confirmed amounts only", allocated,
          "Allocations by yojana, " + s.name) : "") +
        (top.length
          ? '<div class="table-box"><table class="yojanas"><thead><tr><th>#</th><th>Yojana</th><th class="num">Allocation ▼</th><th class="num">Disbursed</th><th>Status</th></tr></thead><tbody>' +
            top.map(function (sc, i) {
              return "<tr><td class=\"rank\">" + (i + 1) + "</td><td>" + schemeCell(sc) + "</td>" +
                '<td class="num">' + (sc.announced == null ? DASH : cr(sc.announced)) + '</td><td class="num">' + disbScheme(sc) +
                "</td><td>" + badge(sc.status) + "</td></tr>";
            }).join("") + '</tbody></table></div><p class="hint" style="margin:0">— Not yet available. Amounts appear only once confirmed in an official source.</p>'
          : '<div class="table-box"><p class="empty">No schemes collected for ' + esc(s.name) + " yet. The daily run will add them as they are found and verified.</p></div>") +
        (sources.length ? '<p class="hint src-list">Sources: ' + sources.map(function (x, i) {
          // a publisher named twice gets a number, so each link stays distinguishable
          var name = publisher(x.url), n = sources.slice(0, i).filter(function (y) { return publisher(y.url) === name; }).length;
          var kind = ({ govt: "Govt", prs: "", news: "News" }[x.kind]);
          return sourceLink(x, (kind ? kind + " · " : "") + name + (n ? " " + (n + 1) : ""));
        }).join(" · ") + "</p>" : "") +
      "</section>";
    window.scrollTo(0, 0);
    animate();
    layoutTreemaps();
  }

  // ---------- feedback (homepage only; posts to server/feedback_server.py) ----------
  var feedback = document.getElementById("feedback");
  (function () {
    var form = document.getElementById("feedback-form");
    var input = document.getElementById("feedback-msg");
    var status = document.getElementById("feedback-status");
    var button = form.querySelector("button");
    var meta = document.querySelector('meta[name="feedback-endpoint"]');
    function say(text, kind) { status.textContent = text; status.className = "feedback-status" + (kind ? " " + kind : ""); }
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var msg = input.value.trim();
      if (msg.length < 3) { say("Please enter a message.", "err"); input.focus(); return; }
      button.disabled = true;
      say("Sending…");
      fetch(meta ? meta.content : "api/feedback", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message: msg })
      })
        .then(function (r) {
          return r.json().catch(function () { return {}; }).then(function (body) {
            if (!r.ok) throw { user: body.error };  // the server's own wording (validation, rate limit)
          });
        })
        .then(function () { form.reset(); say("Thank you — your message has been received.", "ok"); })
        .catch(function (err) { say((err && err.user) || "Couldn’t send your message right now. Please try again later.", "err"); })
        .then(function () { button.disabled = false; });
    });
  })();

  function route() {
    var m = location.hash.match(/^#\/state\/([A-Z]{2})$/);
    feedback.hidden = !!m;
    if (m) { state(m[1]); document.title = (DATA.states.find(function (s) { return s.code === m[1]; }) || {}).name + " · Yojana Tracker"; }
    else { home(); document.title = "Yojana Tracker"; }
  }

  fetch("data/site.json", { cache: "no-cache" })
    .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(function (d) {
      DATA = d;
      document.getElementById("fy").textContent = "FY " + d.fy;
      document.getElementById("updated").textContent = "Updated " + new Date(d.generated_at).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
      window.addEventListener("hashchange", route);
      route();
    })
    .catch(function (e) { app.innerHTML = '<p class="error">Could not load data (' + esc(e.message) + ").</p>"; });
})();
