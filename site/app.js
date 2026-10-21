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

  // CAG provisional actual under an estimate: "Actually spent: ₹ X Cr, provisional, CAG · 81.3% of revised estimate".
  // pct comes from the build's validation rules; null there means the two figures are not comparable.
  function actualLine(verb, actual, estimate, pct) {
    var a = val(actual);
    if (a === null) return "";
    var cmp = val(estimate) === null ? "" : " · " + (pct == null ? "N/A" : pct.toFixed(1) + "%") + " of revised estimate";
    var label = (actual.basis === "provisional actuals" ? "provisional" : esc(actual.basis || "")) + ", CAG";
    return '<div class="sub">' + esc(verb) + ": <strong>" + cr(a) + "</strong>, " +
      (actual.source && actual.source.url ? '<a href="' + esc(actual.source.url) + '" target="_blank" rel="noopener">' + label + "</a>" : label) +
      cmp + "</div>";
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
  function statCard(title, fig, extra, kind) {
    var v = val(fig);
    var basis = fig && ({ RE: "Revised estimate", BE: "Budget estimate" }[fig.basis] || fig.basis);
    // three rows (title · figure · source and status), lined up across cards by the grid
    return '<div class="card stat' + (kind ? " " + kind : "") + '">' +
      '<div class="eyebrow">' + esc(title) + "</div>" +
      '<div class="figure">' + (v === null
        ? '<div class="big pending">Pending</div>'
        : '<div class="big" data-count="' + v + '">' + headline(v) + "</div>" +
          '<div class="exact">' + esc([Math.abs(v) >= 100000 ? cr(v) : "", basis].filter(Boolean).join(" · ")) + "</div>") + "</div>" +
      '<div class="card-foot">' + (extra ? '<div class="sub">' + extra + "</div>" : "") +
      (fig ? '<div>' + badge(fig.status) + "</div>" : "") + "</div>" +
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
        statCard("Central govt · scheme allocations", c.schemes_announced, esc(c.schemes_announced.label) + " · " + sourceLink(c.schemes_announced.source), "welfare") +
        statCard("Central govt · gross tax collected", c.gross_tax, esc(c.gross_tax.label) + " · " + sourceLink(c.gross_tax.source)) +
        statCard("Central Revenue", c.revenue, c.revenue ? esc(c.revenue.label) + " · " + sourceLink(c.revenue.source) : "") +
      "</section>" +
      '<section class="stack"><div class="grid2">' +
        barChart("Where the centre spends", "Top 5 · % of total expenditure", areas, "var(--navy)", function (r) { return r.pct.toFixed(1) + "%"; }) +
        barChart("Top 5 central schemes", "By ₹ (RE " + esc(DATA.fy) + ")", c.top_schemes, "var(--teal)", function (r) { return cr(r.value); }) +
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
  }

  function state(code) {
    var s = DATA.states.find(function (x) { return x.code === code; });
    if (!s) { location.hash = "#/"; return; }
    var top = s.schemes.slice(0, 5);
    var sources = [];
    top.forEach(function (sc) { (sc.sources || []).forEach(function (src) { if (!sources.some(function (x) { return x.url === src.url; })) sources.push(src); }); });

    app.innerHTML =
      '<div><a class="back" href="#/">← All states</a></div>' +
      '<div class="section-head"><h1>' + esc(s.name) + "</h1>" + (s.ut ? '<span class="hint">Union Territory</span>' : "") + "</div>" +
      '<section class="grid2 stats">' +
        statCard("State budget (total expenditure excl. debt repayment)", s.budget,
          (s.source ? sourceLink(s.source) : "Data for this state is coming soon.") +
          actualLine("Actually spent", s.budget_actual, s.budget, (s.vs_re || {}).budget), "exp") +
        statCard("Own tax revenue", s.tax, actualLine("Actually collected", s.tax_actual, s.tax, (s.vs_re || {}).tax)) +
        statCard("Schemes announced", s.beneficiary_announced, s.beneficiary_announced ? esc(s.beneficiary_announced.label || "") : "", "welfare") +
      "</section>" +
      '<section class="states">' +
        "<h2>Top 5 yojanas by allocation</h2>" +
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
