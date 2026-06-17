/* T&T Produce Prices — reads site/data/summary.json for the market board
   and lazily fetches site/data/history.json the first time a chart opens.

   Written to survive hostile conditions: hand-edited source data is always
   HTML-escaped, every fetch failure shows a message instead of a blank
   page, Chart.js loads lazily so a blocked CDN can't take down the price
   board, and no syntax newer than ~2017 is used so older phones still
   render prices. */

let summary = null;
let history = null;
let historyPromise = null;   // reset to null on failure so a retry can work
let chartJsPromise = null;
const charts = {};           // commodity id -> Chart instance

const $ = function (sel) { return document.querySelector(sel); };
const state = { query: "", category: null, perLb: false, view: "tiers" };

/* Retail vendors and farmers price per pound, NAMIS reports per Kg, so the
   site can show either. Only weighed items convert — prices per bundle,
   bag or 100's mean the same in any unit. */
const LB_PER_KG = 2.20462;
function isWeighed(c) { return c.unit === "Kg"; }
function displayPrice(c, perUnit) {
  return state.perLb && isWeighed(c) ? perUnit / LB_PER_KG : perUnit;
}
function displayUnit(c) {
  return state.perLb && isWeighed(c) ? "lb" : c.unit;
}

/* Trini market names — visitors search what they call it at the market,
   the official NAMIS name wins. Keys are what people type. */
const SYNONYMS = {
  "fig": "banana",
  "green fig": "banana",
  "okra": "ochro",
  "okro": "ochro",
  "eggplant": "melongene",
  "baigan": "melongene",
  "aubergine": "melongene",
  "papaya": "paw paw",
  "chayote": "christophene",
  "cho cho": "christophene",
  "karela": "caraillie",
  "caraille": "caraillie",
  "taro": "dasheen",
  "scallion": "chive",
};

function esc(value) {
  return String(value).replace(/[&<>"']/g, function (ch) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
  });
}

/* Bare "$" — the header tagline and footer say prices are TT dollars. */
const fmtPrice = function (p) { return "$" + p.toFixed(2); };
const fmtDate = function (iso) {
  return new Date(iso + "T12:00:00").toLocaleDateString("en-TT", {
    weekday: "long", day: "numeric", month: "long", year: "numeric",
  });
};
const fmtDateShort = function (iso) {
  return new Date(iso + "T12:00:00").toLocaleDateString("en-TT", {
    day: "numeric", month: "short",
  });
};

const MONTHS_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const MONTHS_LONG = ["January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December"];

/* Axis labels stay short so a phone-width chart fits several without
   overlapping: "2026-06-09" -> "9 Jun", "2026-06" -> "Jun ’26". */
function fmtAxisDate(key) {
  const p = key.split("-");
  if (p.length === 3) return parseInt(p[2], 10) + " " + MONTHS_SHORT[p[1] - 1];
  return MONTHS_SHORT[p[1] - 1] + " ’" + p[0].slice(2);
}

/* Tooltips have room for the full date: "Tuesday 9 June 2026" / "June 2026". */
function fmtTooltipDate(key) {
  return key.split("-").length === 3
    ? fmtDate(key)
    : MONTHS_LONG[key.split("-")[1] - 1] + " " + key.slice(0, 4);
}

/* ~64px per "Jun ’26" label; below 4 ticks the chart stops reading as a
   timeline, above 8 desktop labels start to touch. */
function maxTicksFor(width) {
  return Math.max(4, Math.min(8, Math.floor(width / 64)));
}

/* Trinidad runs on UTC-4 with no daylight saving, so "today at the market"
   is a fixed clock shift — no locale or timezone-database traps. */
function trinidadToday() {
  const t = new Date(Date.now() - 4 * 3600 * 1000);
  return Date.UTC(t.getUTCFullYear(), t.getUTCMonth(), t.getUTCDate());
}

/* Weekdays strictly between the report date and today. 0 is normal (today's
   report just isn't due yet); 1+ means a whole market day passed with no
   report — public holiday, market closure, or the data source is behind. */
function missedMarketDays(reportIso) {
  const today = trinidadToday();
  let missed = 0;
  for (let day = new Date(reportIso + "T00:00:00Z").getTime() + 86400000;
       day < today; day += 86400000) {
    const dow = new Date(day).getUTCDay();
    if (dow >= 1 && dow <= 5) missed++;
  }
  return missed;
}

function init() {
  // "no-cache" = revalidate with the server (a cheap 304 when unchanged),
  // never trust heuristic caching — prices change every market day.
  fetch("data/summary.json", { cache: "no-cache" })
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(function (data) {
      summary = data;
      $("#report-date").textContent = "Prices for " + fmtDate(summary.report_date);
      $("#generated-at").textContent = "Page data generated " +
        new Date(summary.generated_at).toLocaleString("en-TT") + ".";
      renderStaleNote();
      initChips();
      renderMovers();
      renderList();
      $("#search").addEventListener("input", function (e) {
        state.query = e.target.value.trim().toLowerCase();
        renderList();
      });
      // Shared links land here: #carrot opens the carrot panel directly.
      applyHash();
      window.addEventListener("hashchange", applyHash);
    })
    .catch(function () {
      $("#report-date").innerHTML =
        'Couldn’t load today’s prices — please check your ' +
        'connection and <a href="" style="color:#fff">try again</a>.';
    });
}

/* The per-Kg / per-lb segmented control. The choice is wired up before the
   data arrives (init's fetch is still in flight) and remembered across
   visits; localStorage can throw in private browsing, hence the try/catch. */
function initUnitToggle() {
  const kgBtn = $("#unit-kg");
  const lbBtn = $("#unit-lb");
  // A visitor can briefly get cached HTML with fresh JS right after a
  // deploy; missing buttons must cost them the toggle, not the whole site.
  if (!kgBtn || !lbBtn) return;
  function setPerLb(perLb) {
    // Re-clicking the pressed button is a no-op — without this guard it
    // would pointlessly re-render and close every open panel.
    if (perLb === state.perLb) return;
    state.perLb = perLb;
    kgBtn.setAttribute("aria-pressed", String(!perLb));
    lbBtn.setAttribute("aria-pressed", String(perLb));
    try { localStorage.setItem("priceUnit", perLb ? "lb" : "kg"); } catch (e) {}
    // Re-render everything that shows a price. Open panels close; the
    // visitor just changed how all prices read, so a redraw reads as
    // expected, not as data loss.
    if (summary) {
      renderMovers();
      renderList();
    }
  }
  kgBtn.addEventListener("click", function () { setPerLb(false); });
  lbBtn.addEventListener("click", function () { setPerLb(true); });
  let saved = null;
  try { saved = localStorage.getItem("priceUnit"); } catch (e) {}
  if (saved === "lb") setPerLb(true);
}

/* The "Show" view toggle: Best buys (deal tiers) / By type (produce
   category) / A–Z. Display only — it regroups the same loaded data and never
   touches summary.commodities. Wired before the data arrives and remembered
   across visits, like the unit toggle. */
const VIEWS = ["tiers", "type", "name"];
function initViewControl() {
  const btns = { tiers: $("#view-tiers"), type: $("#view-type"), name: $("#view-az") };
  // Cached HTML with fresh JS right after a deploy: missing buttons must cost
  // the visitor the toggle, not the whole page.
  if (!btns.tiers || !btns.type || !btns.name) return;
  function paint(view) {
    VIEWS.forEach(function (v) {
      btns[v].setAttribute("aria-pressed", String(v === view));
    });
  }
  function setView(view) {
    if (view === state.view) return;   // no-op re-click, like the unit toggle
    state.view = view;
    paint(view);
    try { localStorage.setItem("view", view); } catch (e) {}
    if (summary) renderList();
  }
  VIEWS.forEach(function (v) {
    btns[v].addEventListener("click", function () { setView(v); });
  });
  let saved = null;
  try { saved = localStorage.getItem("view"); } catch (e) {}
  if (VIEWS.indexOf(saved) !== -1) { state.view = saved; paint(saved); }
}

function byName(a, b) { return a.name.localeCompare(b.name); }

/* The "Best buys" tiers, in shopper-priority order. Each commodity carries
   its own tier key (computed in build_site_data.py from harvest volume +
   price band); empty tiers are skipped at render time, so the page only
   shows the tiers that actually apply this month. */
const TIERS = [
  { key: "peak-steal", title: "Peak-season steals",
    blurb: "In season and priced below their usual level." },
  { key: "in-season", title: "In season now",
    blurb: "Freshly in season at everyday prices." },
  { key: "in-demand", title: "In season, in demand",
    blurb: "In season but selling above their usual price." },
  { key: "oos-bargain", title: "Out-of-season bargains",
    blurb: "Past their local season but going cheap — often imported." },
  { key: "oos-scarce", title: "Off-season &amp; limited",
    blurb: "Past their local season and scarce or pricey — often imported." },
  { key: "not-at-market", title: "Not at the market today",
    blurb: "No price posted today; showing the last known price." },
];

/* A quiet "peak harvest" tag for the months a crop is at its most abundant
   (top third of its own year by wholesale volume). Shown in every view so the
   volume signal survives switching away from the tiers. */
function peakBadge(c) {
  return c.harvest === "peak" ? '<span class="tag-peak">peak harvest</span>' : "";
}

function renderStaleNote() {
  const missed = missedMarketDays(summary.report_date);
  if (missed < 1) return;
  const note = $("#stale-note");
  note.hidden = false;
  note.textContent = "That report is " + missed + " market day" +
    (missed === 1 ? "" : "s") + " old — the market may have been closed " +
    "(public holiday), or no newer report has been published yet. " +
    "These are the latest prices available.";
}

/* Make an item visible (clearing any search/filter that hides it), open its
   detail panel, and scroll it into view. Used by mover cards and #hash links. */
function revealItem(id) {
  // Check the id is real BEFORE touching anything: a junk hash (mistyped
  // shared link) must not wipe the visitor's search or category filter.
  const known = summary.commodities.some(function (c) { return c.id === id; });
  if (!known) return;
  let row = document.getElementById("item-" + id);
  if (!row) {
    state.query = "";
    state.category = null;
    $("#search").value = "";
    initChips();
    renderList();
    row = document.getElementById("item-" + id);
  }
  if (!row) return;
  const btn = row.querySelector(".item-row");
  if (btn.getAttribute("aria-expanded") !== "true") btn.click();
  // Hand keyboard/screen-reader focus to the row, so a shared link is
  // announced (name, price, expanded state) instead of leaving focus
  // stranded at the top of the page. Old browsers that don't understand
  // preventScroll just focus-and-jump, which is fine.
  btn.focus({ preventScroll: true });
  row.scrollIntoView({ behavior: "smooth", block: "start" });
}

function applyHash() {
  let id = "";
  try { id = decodeURIComponent(location.hash.slice(1)); } catch (e) { return; }
  if (id) revealItem(id);
}

function initChips() {
  const box = $("#chips");
  const all = ["All"].concat(summary.categories);
  box.innerHTML = "";
  all.forEach(function (cat) {
    const b = document.createElement("button");
    b.className = "chip";
    b.textContent = cat;
    b.dataset.cat = cat;
    b.setAttribute("aria-pressed", String(cat === "All"));
    b.addEventListener("click", function () {
      state.category = cat === "All" ? null : cat;
      // Update pressed states in place so keyboard focus stays on the chip.
      box.querySelectorAll(".chip").forEach(function (x) {
        x.setAttribute("aria-pressed",
          String(x.dataset.cat === (state.category || "All")));
      });
      renderList();
    });
    box.appendChild(b);
  });
}

function renderMovers() {
  const moved = summary.commodities
    .filter(function (c) { return typeof c.price_change === "number" && c.price_change !== 0; })
    .sort(function (a, b) { return Math.abs(b.price_change) - Math.abs(a.price_change); })
    .slice(0, 6);
  if (!moved.length) return;
  const box = $("#movers");
  box.hidden = false;
  box.innerHTML = "";
  moved.forEach(function (c) {
    const el = document.createElement("button");
    el.className = "mover";
    el.innerHTML = "<b>" + esc(c.name) + "</b>" + changeBadge(c) +
      " " + fmtPrice(displayPrice(c, c.price)) + " / " + esc(displayUnit(c));
    el.addEventListener("click", function () { revealItem(c.id); });
    box.appendChild(el);
  });
}

function changeBadge(c) {
  if (typeof c.price_change !== "number") {
    // Priced today but nothing to compare against (no price last market day).
    if (c.price !== null) {
      return ' <span class="chg same" role="img" ' +
        'aria-label="no previous-day price to compare">–</span>';
    }
    return "";
  }
  // A real 1¢/Kg change rounds to 0.00 in lb mode; "<0.01" keeps the
  // number from contradicting its own arrow. Screen readers skip the "<"
  // symbol at default verbosity, so the spoken label spells it out.
  let amt = Math.abs(displayPrice(c, c.price_change)).toFixed(2);
  let spoken = "$" + amt;
  if (Number(amt) === 0) {
    amt = "&lt;0.01";   // entity: amt lands in innerHTML
    spoken = "less than $0.01";
  }
  if (c.price_change > 0) {
    return ' <span class="chg up" role="img" aria-label="up ' + spoken +
      ' since last market day">▲ ' + amt + "</span>";
  }
  if (c.price_change < 0) {
    return ' <span class="chg down" role="img" aria-label="down ' + spoken +
      ' since last market day">▼ ' + amt + "</span>";
  }
  return ' <span class="chg same" role="img" aria-label="unchanged since ' +
    'last market day">●</span>';
}

/* The data values stay "low"/"high"/"typical" (they're also the CSS
   hooks); the visible words deliberately avoid any time reference —
   "for the year" kept being read as "this calendar year". The badge is
   quartile-based, so "than usual" is also the more honest claim. */
const LEVEL_WORDS = {
  low: "cheaper than usual",
  high: "costlier than usual",
  typical: "typical",
};

/* Price-context pill. Quiet by design: a typical price gets no pill (most
   items most days), so the pills that do appear mean something. */
function levelBadge(c) {
  if (c.price_level !== "low" && c.price_level !== "high") return "";
  return '<div class="level ' + c.price_level + '">' +
    LEVEL_WORDS[c.price_level] + "</div>";
}

/* "In shops and markets" — the monthly NAMDEVCO retail survey, shown as
   its own short paragraph inside the open panel (sharing the wholesale
   facts paragraph read as information whiplash). The survey month rides
   inside the sentence so the freshness caveat can't be skimmed past, and
   the wording never implies a wholesale comparison for items the data
   can't honestly compare (the build only emits per-Kg retail where both
   sides weigh). */
const OUTLET_NAMES = {
  farmers_markets: "farmers’ markets",
  municipal_markets: "municipal markets",
  vege_marts: "vege-marts",
  supermarkets: "supermarkets",
};
/* Leading with the thing being priced ("a pack typically costs…") is what
   makes counted units self-explanatory: a pack or bundle is NAMDEVCO's
   size, not a weight, so the Kg/lb toggle can't apply to it. */
const RETAIL_UNIT_NOUNS = {
  "Each": "each one", "Bundle": "a bundle", "Head": "a head", "Pack": "a pack",
};

function monthsApart(ym, laterYmd) {
  return (parseInt(laterYmd.slice(0, 4), 10) * 12 + parseInt(laterYmd.slice(5, 7), 10))
    - (parseInt(ym.slice(0, 4), 10) * 12 + parseInt(ym.slice(5, 7), 10));
}

function retailSentence(c) {
  const r = c.retail;
  if (!r || !summary.retail_month) return "";
  const monthLabel = MONTHS_LONG[summary.retail_month.split("-")[1] - 1] +
    " " + summary.retail_month.slice(0, 4);
  // Weighed retail follows the visitor's Kg/lb toggle — including items
  // the survey prices per lb while the market sells by bundle or count
  // (kind "native", unit "lb"): a pound is a weight wherever it appears.
  const weighed = r.kind === "kg" || r.unit === "lb";
  const fmt = function (v) {
    if (!weighed) return fmtPrice(v);
    const perLbVal = r.kind === "kg" ? v / LB_PER_KG : v;
    return fmtPrice(state.perLb ? perLbVal : perLbVal * LB_PER_KG);
  };
  const noun = weighed
    ? (state.perLb ? "a pound" : "a kilo")
    : (RETAIL_UNIT_NOUNS[r.unit] || "a " + esc(String(r.unit).toLowerCase()));

  let s;
  if (r.wide) {
    // Outlets disagree too much for one "typical" number to be honest.
    s = "In shops and markets, " + noun + " can cost anywhere from " +
      fmt(r.cheap.price) + " at " + OUTLET_NAMES[r.cheap.at] + " to " +
      fmt(r.dear.price) + " at " + OUTLET_NAMES[r.dear.at] + ".";
  } else if (r.cheap && r.dear) {
    s = "In shops and markets, " + noun + " typically costs around " +
      fmt(r.price) + ", ranging from " + fmt(r.cheap.price) + " at " +
      OUTLET_NAMES[r.cheap.at] + " to " + fmt(r.dear.price) + " at " +
      OUTLET_NAMES[r.dear.at] + ".";
  } else {
    s = "In shops and markets, " + noun + " typically costs around " +
      fmt(r.price) + ".";
  }
  s += " NAMDEVCO shop survey, " + monthLabel + ".";
  if (monthsApart(summary.retail_month, summary.report_date) >= 2) {
    s += " That’s the newest shop survey published so far.";
  }
  return s;
}

function matches(c) {
  if (state.category && c.category !== state.category) return false;
  if (!state.query) return true;
  const name = c.name.toLowerCase();
  if (name.includes(state.query)) return true;
  for (const key in SYNONYMS) {
    if (key.startsWith(state.query) || state.query.startsWith(key)) {
      if (name.includes(SYNONYMS[key])) return true;
    }
  }
  return false;
}

function renderItems(list, items) {
  items.forEach(function (c) { list.appendChild(renderItem(c)); });
}

function categoryHead(text) {
  const h = document.createElement("h2");
  h.className = "category-head";
  h.textContent = text;
  return h;
}

/* "By type": the produce categories in the market's own order. */
function renderByType(list, items) {
  let shown = 0;
  summary.categories.forEach(function (cat) {
    const group = items.filter(function (c) { return c.category === cat; });
    if (!group.length) return;
    list.appendChild(categoryHead(cat));
    renderItems(list, group);
    shown += group.length;
  });
  return shown;
}

function tierHead(tier) {
  const head = document.createElement("div");
  head.className = "tier-head";
  head.innerHTML = "<h2>" + tier.title + "</h2><p>" + tier.blurb + "</p>";
  return head;
}

/* "Best buys": each tier in priority order, empty tiers skipped. Every tier
   leads with its peak-harvest items, then falls back to alphabetical. */
function renderTiers(list, items) {
  let shown = 0;
  TIERS.forEach(function (tier) {
    const inTier = items.filter(function (c) { return c.tier === tier.key; });
    if (!inTier.length) return;
    list.appendChild(tierHead(tier));
    inTier.sort(function (a, b) {
      const ap = a.harvest === "peak" ? 0 : 1, bp = b.harvest === "peak" ? 0 : 1;
      return ap !== bp ? ap - bp : byName(a, b);
    });
    renderItems(list, inTier);
    shown += inTier.length;
  });
  return shown;
}

function renderList() {
  const list = $("#list");
  // Charts on wiped canvases would linger in Chart.js's registry.
  Object.keys(charts).forEach(function (id) {
    charts[id].destroy();
    delete charts[id];
  });
  list.innerHTML = "";
  const visible = summary.commodities.filter(matches);
  let shown;
  if (state.view === "name") {
    renderItems(list, visible.slice().sort(byName));
    shown = visible.length;
  } else if (state.view === "type") {
    shown = renderByType(list, visible);
  } else {
    shown = renderTiers(list, visible);
  }
  $("#empty").hidden = shown > 0;
  $("#status").textContent = shown + " item" + (shown === 1 ? "" : "s") + " shown";
}

function renderItem(c) {
  const div = document.createElement("div");
  div.className = "item";
  div.id = "item-" + c.id;

  let priceHtml;
  if (c.price !== null) {
    priceHtml = '<div class="price-now">' + fmtPrice(displayPrice(c, c.price)) +
      "</div>" +
      '<div class="price-unit">per ' + esc(displayUnit(c)) + changeBadge(c) +
      "</div>" +
      // In the tiers, the tier itself already says cheaper/dearer-than-usual,
      // so the pill is redundant; the by-type and A–Z views still show it.
      (state.view === "tiers" ? "" : levelBadge(c));
  } else {
    // traded=true with no price happens: some items sell on volume with no
    // posted price that day.
    const label = c.traded ? "no price posted" : "not traded";
    priceHtml = c.last_traded
      ? '<div class="not-traded">' + label + " —<br>" +
        fmtPrice(displayPrice(c, c.last_traded.price)) + " on " +
        fmtDateShort(c.last_traded.date) + "</div>"
      : '<div class="not-traded">' + label + " recently</div>";
  }

  div.innerHTML =
    '<button class="item-row" aria-expanded="false">' +
    '<span class="item-name"><b>' + esc(c.name) + "</b><small>" +
    esc(c.category) + '</small>' + peakBadge(c) + "</span>" +
    '<span class="item-price">' + priceHtml + "</span></button>";

  div.querySelector(".item-row").addEventListener("click", function () {
    toggleDetail(div, c);
  });
  return div;
}

function loadChartJs() {
  if (typeof Chart !== "undefined") return Promise.resolve();
  if (!chartJsPromise) {
    chartJsPromise = new Promise(function (resolve, reject) {
      const s = document.createElement("script");
      s.src = "https://cdn.jsdelivr.net/npm/chart.js@4.4.9/dist/chart.umd.min.js";
      s.onload = resolve;
      s.onerror = function () {
        chartJsPromise = null;  // allow retry on the next tap
        reject(new Error("Chart.js failed to load"));
      };
      document.head.appendChild(s);
    });
  }
  return chartJsPromise;
}

function loadHistory() {
  if (history) return Promise.resolve(history);
  if (!historyPromise) {
    historyPromise = fetch("data/history.json", { cache: "no-cache" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        history = data;
        return data;
      })
      .catch(function (err) {
        historyPromise = null;  // a later tap retries instead of re-failing
        throw err;
      });
  }
  return historyPromise;
}

function toggleDetail(div, c) {
  const btn = div.querySelector(".item-row");
  const open = btn.getAttribute("aria-expanded") === "true";
  if (open) {
    if (charts[c.id]) {
      charts[c.id].destroy();
      delete charts[c.id];
    }
    const d = div.querySelector(".detail");
    if (d) d.remove();
    btn.setAttribute("aria-expanded", "false");
    // Only un-claim the address bar if this item is the one it points at.
    // window.history, NOT history — our price-history variable shadows it.
    if (location.hash === "#" + encodeURIComponent(c.id)) {
      window.history.replaceState(null, "", location.pathname + location.search);
    }
    return;
  }
  btn.setAttribute("aria-expanded", "true");
  // replaceState (not location.hash=) so opening items doesn't bury the
  // visitor's back button under one history entry per tap.
  window.history.replaceState(null, "", "#" + encodeURIComponent(c.id));

  const detail = document.createElement("div");
  detail.className = "detail";
  // Whole pounds for volume: 2,503.83 Kg of tomatoes is a meaningful
  // figure, 5,519.97 lb pretends to a precision the conversion doesn't have.
  const vol = state.perLb && isWeighed(c)
    ? Math.round(c.volume * LB_PER_KG)
    : c.volume;
  let facts = c.volume !== null
    ? "Volume today: " + vol.toLocaleString() + " " + esc(displayUnit(c)) + "."
    : "No volume recorded today.";
  if (typeof c.year_low === "number" && typeof c.year_high === "number") {
    // "Monthly averages ranged", not "prices ranged": today's daily price
    // can legitimately sit outside the band, and on ~17 of 77 items it
    // does — wording that claims a hard range would look like broken data.
    facts += " Monthly averages over the past 12 months ranged " +
      fmtPrice(displayPrice(c, c.year_low)) + "–" +
      fmtPrice(displayPrice(c, c.year_high)) +
      (LEVEL_WORDS[c.price_level]
        ? " — today’s price is " + LEVEL_WORDS[c.price_level] + "."
        : ".");
  }
  const shops = retailSentence(c);
  detail.innerHTML =
    '<p class="facts">' + facts + "</p>" +
    (shops ? '<p class="facts">' + shops + "</p>" : "") +
    '<div class="ranges" role="group" aria-label="Chart range"></div>' +
    '<div class="chart-box"><canvas role="img" aria-label="Price history ' +
    "chart for " + esc(c.name) + '"></canvas></div>' +
    '<p class="chart-note"></p>' +
    '<div class="seasonality" hidden></div>';
  div.appendChild(detail);

  Promise.all([loadHistory(), loadChartJs()])
    .then(function () {
      // The visitor may have collapsed the panel or searched (wiping the
      // list) while we were fetching.
      if (detail.isConnected) {
        buildRanges(detail, c);
        renderSeasonality(detail, c);
      }
    })
    .catch(function () {
      if (detail.isConnected) {
        detail.querySelector(".chart-box").innerHTML =
          '<p class="facts">Couldn’t load the price history — ' +
          "check your connection, close this and tap again.</p>";
      }
    });
}

function buildRanges(detail, c) {
  const h = history[c.id] || {};
  const monthly = Object.entries(h.monthly || {});
  const daily = Object.entries(h.daily || {});
  const ranges = [];
  if (daily.length) ranges.push({ label: "Recent", pts: daily, note: "Daily wholesale prices." });
  if (monthly.length) {
    ranges.push({ label: "1Y", pts: monthly.slice(-12), note: "Monthly average prices." });
    ranges.push({ label: "5Y", pts: monthly.slice(-60), note: "Monthly average prices." });
    ranges.push({ label: "Max", pts: monthly, note: "Monthly averages since " + monthly[0][0].slice(0, 4) + "." });
  }
  if (!ranges.length) {
    detail.querySelector(".ranges").remove();
    detail.querySelector(".chart-box").innerHTML =
      '<p class="facts">No price history available for this item yet.</p>';
    return;
  }
  const box = detail.querySelector(".ranges");
  ranges.forEach(function (r, i) {
    const b = document.createElement("button");
    b.className = "range-btn";
    b.textContent = r.label;
    b.setAttribute("aria-pressed", String(i === 0));
    b.addEventListener("click", function () {
      box.querySelectorAll(".range-btn").forEach(function (x) {
        x.setAttribute("aria-pressed", "false");
      });
      b.setAttribute("aria-pressed", "true");
      drawChart(detail, c, r);
    });
    box.appendChild(b);
  });
  drawChart(detail, c, ranges[0]);
}

/* Average each calendar month against its own year's mean, so 2008 and 2026
   can sit in the same average without two decades of inflation drowning the
   seasonal signal. Lenient on purpose: a crop like sorrel only trades
   Oct–Jan, so short years and permanently-empty months are its normal
   shape, not bad data — the real noise filter is requiring each month to
   appear in 3+ years. Returns { months: 12 relative values (null where data
   is thin), years: how many years contributed }, or null when there isn't
   enough history to call anything a pattern. */
function seasonalProfile(monthly) {
  const byYear = {};
  Object.keys(monthly).forEach(function (key) {
    const year = key.slice(0, 4);
    if (!byYear[year]) byYear[year] = [];
    byYear[year].push({ month: Number(key.slice(5, 7)) - 1, price: monthly[key] });
  });
  const ratios = [];
  for (let m = 0; m < 12; m++) ratios.push([]);
  let years = 0;
  Object.keys(byYear).forEach(function (year) {
    const months = byYear[year];
    if (months.length < 4) return;   // too few months to anchor a mean
    let sum = 0;
    months.forEach(function (x) { sum += x.price; });
    const mean = sum / months.length;
    if (!mean) return;
    years++;
    months.forEach(function (x) { ratios[x.month].push(x.price / mean); });
  });
  const profile = ratios.map(function (list) {
    if (list.length < 3) return null;  // one odd year isn't a pattern
    let sum = 0;
    list.forEach(function (r) { sum += r; });
    return sum / list.length;
  });
  const known = profile.filter(function (x) { return x !== null; });
  if (known.length < 4) return null;
  return { months: profile, years: years };
}

function renderSeasonality(detail, c) {
  const result = seasonalProfile((history[c.id] || {}).monthly || {});
  if (!result) return;
  const profile = result.months;
  const known = profile.filter(function (x) { return x !== null; });
  const min = Math.min.apply(null, known);
  const max = Math.max.apply(null, known);

  const flat = max - min < 0.08;   // under ~8% swing isn't worth chasing
  let text;
  if (flat) {
    text = esc(c.name) + " costs about the same all year round.";
  } else {
    text = "Usually cheapest in " + MONTHS_LONG[profile.indexOf(min)] +
      ", priciest in " + MONTHS_LONG[profile.indexOf(max)] + ".";
  }
  if (known.length < 12) {
    text += " Months without a bar are when it’s rarely sold.";
  }

  /* Tooltip/aria phrasing for one month's relative price (null = no data). */
  function monthHint(r) {
    if (r === null) return "rarely sold";
    const pct = Math.round((r - 1) * 100);
    if (pct > 1) return "usually about " + pct + "% above the year’s average";
    if (pct < -1) return "usually about " + -pct + "% below the year’s average";
    return "usually about average";
  }

  const nowMonth = new Date().getMonth();
  const box = detail.querySelector(".seasonality");
  box.hidden = false;
  box.innerHTML =
    '<h3 class="season-head">Best time to buy</h3>' +
    '<p class="season-text">' + text + " Based on " + result.years +
    " years of monthly averages.</p>" +
    '<div class="season-bars" role="img" aria-label="Seasonal price ' +
    'pattern. ' + text + " Current month: " + MONTHS_LONG[nowMonth] +
    ", " + monthHint(profile[nowMonth]) + '."></div>' +
    '<div class="season-letters" aria-hidden="true"></div>';

  const bars = box.querySelector(".season-bars");
  const letters = box.querySelector(".season-letters");
  profile.forEach(function (r, m) {
    const bar = document.createElement("div");
    bar.className = "season-bar";
    if (r === null) {
      bar.className += " none";
    } else {
      // Flat profiles still get mid-height bars instead of a 0-to-100
      // exaggeration: scale relative to ±15% around the yearly mean.
      const h = Math.max(8, Math.min(100, ((r - 0.85) / 0.3) * 100));
      bar.style.height = h.toFixed(0) + "%";
      if (!flat && r === min) bar.className += " cheap";
      if (!flat && r === max) bar.className += " dear";
    }
    bar.title = MONTHS_LONG[m] +
      (m === nowMonth ? " (this month): " : ": ") + monthHint(r);
    bars.appendChild(bar);
    const letter = document.createElement("span");
    // Inner .lab exists so the current-month pill can hug the text instead
    // of stretching across the whole flex column; .rest is the collapsible
    // remainder narrow screens hide in CSS, falling back to a
    // J F M A M J… strip instead of colliding.
    letter.innerHTML = '<span class="lab">' + MONTHS_SHORT[m].charAt(0) +
      '<span class="rest">' + MONTHS_SHORT[m].slice(1) + "</span></span>";
    if (m === nowMonth) letter.className = "now";
    letters.appendChild(letter);
  });
}

function drawChart(detail, c, range) {
  const canvas = detail.querySelector("canvas");
  if (charts[c.id]) charts[c.id].destroy();
  detail.querySelector(".chart-note").textContent =
    range.note + " Prices in TT$ per " + displayUnit(c) + ".";
  charts[c.id] = new Chart(canvas, {
    type: "line",
    data: {
      labels: range.pts.map(function (p) { return p[0]; }),
      datasets: [{
        data: range.pts.map(function (p) { return displayPrice(c, p[1]); }),
        borderColor: "#1d7a46",
        backgroundColor: "rgba(29,122,70,0.08)",
        fill: true,
        pointRadius: range.pts.length > 60 ? 0 : 3,
        borderWidth: 2,
        tension: 0.25,
        spanGaps: true,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      // Recompute the tick budget when the canvas changes size (phone
      // rotation, window resize) so labels never crowd back in.
      onResize: function (chart, size) {
        chart.options.scales.x.ticks.maxTicksLimit = maxTicksFor(size.width);
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: function (items) { return fmtTooltipDate(items[0].label); },
            label: function (ctx) { return fmtPrice(ctx.parsed.y); },
          },
        },
      },
      scales: {
        x: {
          ticks: {
            maxRotation: 0,
            autoSkipPadding: 12,
            maxTicksLimit: maxTicksFor(canvas.parentNode.clientWidth || 320),
            // Category-scale callbacks receive the tick index, not the label.
            callback: function (value) {
              return fmtAxisDate(this.getLabelForValue(value));
            },
          },
        },
        y: { ticks: { callback: function (v) { return "$" + Number(v).toFixed(2); } } },
      },
    },
  });
}

initUnitToggle();
initViewControl();
init();
