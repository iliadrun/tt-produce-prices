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
const state = { query: "", category: null };

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

const fmtPrice = function (p) { return "TT$" + p.toFixed(2); };
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

function init() {
  fetch("data/summary.json")
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(function (data) {
      summary = data;
      $("#report-date").textContent = "Prices for " + fmtDate(summary.report_date);
      $("#generated-at").textContent = "Page data generated " +
        new Date(summary.generated_at).toLocaleString("en-TT") + ".";
      initChips();
      renderMovers();
      renderList();
      $("#search").addEventListener("input", function (e) {
        state.query = e.target.value.trim().toLowerCase();
        renderList();
      });
    })
    .catch(function () {
      $("#report-date").innerHTML =
        'Couldn’t load today’s prices — please check your ' +
        'connection and <a href="" style="color:#fff">try again</a>.';
    });
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
      " " + fmtPrice(c.price) + " / " + esc(c.unit);
    el.addEventListener("click", function () {
      state.query = "";
      state.category = null;
      $("#search").value = "";
      initChips();
      renderList();
      const row = document.getElementById("item-" + c.id);
      if (row) {
        row.querySelector(".item-row").click();
        row.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
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
  const amt = Math.abs(c.price_change).toFixed(2);
  if (c.price_change > 0) {
    return ' <span class="chg up" role="img" aria-label="up TT$' + amt +
      ' since last market day">▲ ' + amt + "</span>";
  }
  if (c.price_change < 0) {
    return ' <span class="chg down" role="img" aria-label="down TT$' + amt +
      ' since last market day">▼ ' + amt + "</span>";
  }
  return ' <span class="chg same" role="img" aria-label="unchanged since ' +
    'last market day">●</span>';
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

function renderList() {
  const list = $("#list");
  // Charts on wiped canvases would linger in Chart.js's registry.
  Object.keys(charts).forEach(function (id) {
    charts[id].destroy();
    delete charts[id];
  });
  list.innerHTML = "";
  let shown = 0;
  summary.categories.forEach(function (cat) {
    const items = summary.commodities.filter(function (c) {
      return c.category === cat && matches(c);
    });
    if (!items.length) return;
    const head = document.createElement("h2");
    head.className = "category-head";
    head.textContent = cat;
    list.appendChild(head);
    items.forEach(function (c) {
      list.appendChild(renderItem(c));
      shown++;
    });
  });
  $("#empty").hidden = shown > 0;
  $("#status").textContent = shown + " item" + (shown === 1 ? "" : "s") + " shown";
}

function renderItem(c) {
  const div = document.createElement("div");
  div.className = "item";
  div.id = "item-" + c.id;

  let priceHtml;
  if (c.price !== null) {
    priceHtml = '<div class="price-now">' + fmtPrice(c.price) + "</div>" +
      '<div class="price-unit">per ' + esc(c.unit) + changeBadge(c) + "</div>";
  } else {
    // traded=true with no price happens: some items sell on volume with no
    // posted price that day.
    const label = c.traded ? "no price posted" : "not traded";
    priceHtml = c.last_traded
      ? '<div class="not-traded">' + label + " —<br>" +
        fmtPrice(c.last_traded.price) + " on " +
        fmtDateShort(c.last_traded.date) + "</div>"
      : '<div class="not-traded">' + label + " recently</div>";
  }

  div.innerHTML =
    '<button class="item-row" aria-expanded="false">' +
    '<span class="item-name"><b>' + esc(c.name) + "</b><small>" +
    esc(c.category) + '</small></span>' +
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
    historyPromise = fetch("data/history.json")
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
    return;
  }
  btn.setAttribute("aria-expanded", "true");

  const detail = document.createElement("div");
  detail.className = "detail";
  const facts = c.volume !== null
    ? "Volume today: " + c.volume.toLocaleString() + " " + esc(c.unit)
    : "No volume recorded today.";
  detail.innerHTML =
    '<p class="facts">' + facts + "</p>" +
    '<div class="ranges" role="group" aria-label="Chart range"></div>' +
    '<div class="chart-box"><canvas role="img" aria-label="Price history ' +
    "chart for " + esc(c.name) + '"></canvas></div>' +
    '<p class="chart-note"></p>';
  div.appendChild(detail);

  Promise.all([loadHistory(), loadChartJs()])
    .then(function () {
      // The visitor may have collapsed the panel or searched (wiping the
      // list) while we were fetching.
      if (detail.isConnected) buildRanges(detail, c);
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

function drawChart(detail, c, range) {
  const canvas = detail.querySelector("canvas");
  if (charts[c.id]) charts[c.id].destroy();
  detail.querySelector(".chart-note").textContent =
    range.note + " Prices in TT$ per " + c.unit + ".";
  charts[c.id] = new Chart(canvas, {
    type: "line",
    data: {
      labels: range.pts.map(function (p) { return p[0]; }),
      datasets: [{
        data: range.pts.map(function (p) { return p[1]; }),
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

init();
