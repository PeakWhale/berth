// PeakWhale Berth web page. Plain JavaScript, no build step.
"use strict";

const $ = (id) => document.getElementById(id);
const SAVE_KEY = "berth.fleet.yaml";
let lastResult = null;

function esc(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  const type = res.headers.get("content-type") || "";
  const body = type.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw body;
  return body;
}

const postText = (path, text) => api(path, { method: "POST", headers: { "Content-Type": "text/plain" }, body: text });
const postJson = (path, data) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });

function showError(err) {
  const box = $("error");
  let lines;
  if (err && Array.isArray(err.problems) && err.problems.length) lines = err.problems;
  else if (err && err.error) lines = [err.error];
  else if (err instanceof Error) lines = [err.message];
  else lines = [String(err)];
  box.innerHTML = "<b>Please check the fleet:</b><ul>" + lines.map((l) => `<li>${esc(l)}</li>`).join("") + "</ul>";
  box.hidden = false;
}
const clearError = () => { $("error").hidden = true; };

function save(text) {
  try { localStorage.setItem(SAVE_KEY, text); } catch (_) { /* storage may be blocked */ }
}
function saved() {
  try { return localStorage.getItem(SAVE_KEY); } catch (_) { return null; }
}

// ---- formatting ------------------------------------------------------------

// Returns HTML-safe text: the currency comes from the fleet, which may be shared.
function money(x, currency) {
  let text;
  try {
    text = new Intl.NumberFormat(undefined, { style: "currency", currency, maximumFractionDigits: 2 }).format(x);
  } catch (_) {
    text = `${x.toFixed(2)} ${currency}`;
  }
  return esc(text);
}
const kw = (x) => `${Math.round(x).toLocaleString()} kW`;
const kwh = (x) => `${Math.round(x).toLocaleString()} kWh`;

function vehicleColor(i) {
  const hue = (190 + i * 137.508) % 360;
  return `hsl(${hue.toFixed(0)} 42% 56%)`;
}

// ---- loading ------------------------------------------------------------------

async function loadExamples() {
  const list = await api("/api/examples");
  $("example").innerHTML = list
    .map((e) => `<option value="${esc(e.name)}">${esc(e.title)} (${e.vehicles} vehicles)</option>`)
    .join("");
  return list;
}

let loadTicket = 0; // only the latest "load example" may fill the editor

async function loadExample() {
  clearError();
  const name = $("example").value;
  if (!name) return;
  const ticket = ++loadTicket;
  const text = await api(`/api/examples/${encodeURIComponent(name)}?format=yaml`);
  if (ticket !== loadTicket) return;
  $("editor").value = text;
  save(text);
}

async function openFleetFile(file) {
  clearError();
  const text = await file.text();
  if (file.name.toLowerCase().endsWith(".json")) {
    const fleet = await postText("/api/convert/from-yaml", text);
    $("editor").value = await postJson("/api/convert/to-yaml", fleet);
  } else {
    $("editor").value = text;
  }
  save($("editor").value);
}

async function openVehiclesCsv(file) {
  clearError();
  const vehicles = await postText("/api/convert/vehicles-csv", await file.text());
  let fleet;
  try {
    fleet = await postText("/api/convert/from-yaml", $("editor").value);
  } catch (err) {
    throw { problems: ["The CSV was read, but the fleet in the editor has problems. Load an example first, then add the CSV."].concat(err.problems || []) };
  }
  fleet.vehicles = vehicles;
  $("editor").value = await postJson("/api/convert/to-yaml", fleet);
  save($("editor").value);
}

async function runPlan() {
  clearError();
  const btn = $("plan");
  btn.disabled = true;
  btn.textContent = "Planning...";
  try {
    const result = await postText("/api/plan/yaml", $("editor").value);
    lastResult = result;
    render(result);
  } catch (err) {
    showError(err);
  } finally {
    btn.disabled = false;
    btn.textContent = "Plan charging";
  }
}

// ---- results ------------------------------------------------------------------

function setCard(id, value, sub, tone) {
  const card = $(id);
  card.querySelector(".value").textContent = value;
  card.querySelector(".sub").innerHTML = sub;
  card.classList.remove("good", "bad");
  if (tone) card.classList.add(tone);
}

function render(r) {
  $("empty").hidden = true;
  $("result").hidden = false;
  $("headline").textContent = r.headline;
  const o = r.optimized, b = r.baseline, s = r.savings, cur = r.currency;

  setCard("card-ready", r.all_ready ? "Yes" : "No",
    `${o.vehicles_ready} of ${o.vehicles_total} ready` + (r.all_ready ? "" : `, ${o.shortfall_kwh.toFixed(1)} kWh short`),
    r.all_ready ? "good" : "bad");
  const pct = s.cost_pct === null ? "" : ` (${Math.abs(s.cost_pct).toFixed(0)}%)`;
  const saving = s.cost >= 0.005
    ? `<span class="save">saves ${money(s.cost, cur)}${pct}</span>`
    : s.cost <= -0.005 ? `<span class="loss">${money(-s.cost, cur)}${pct} more</span>` : "same cost";
  setCard("card-cost", money(o.total_cost, cur), `vs ${money(b.total_cost, cur)} charging on arrival: ${saving}`);
  setCard("card-peak", kw(o.peak_charging_kw), `vs ${kw(b.peak_charging_kw)} charging on arrival`);
  setCard("card-energy", kwh(o.grid_kwh), `${r.vehicles.length} vehicles, step ${r.step_minutes} min`);

  drawChart(r);
  renderShort(r);
  renderVehicles(r);
  $("notes").innerHTML = r.notes.map((n) => `<li>${esc(n)}</li>`).join("");
}

function renderShort(r) {
  const box = $("short");
  if (!r.shortfalls.length) { box.hidden = true; return; }
  const o = r.optimized, b = r.baseline;
  const policy = r.shortfall_policy === "fair"
    ? `Policy <b>fair</b>: the shortfall is shared. Biggest shortfall ${o.max_shortfall_kwh.toFixed(1)} kWh `
      + `(charging on arrival: ${b.max_shortfall_kwh.toFixed(1)} kWh). Set <code>shortfall_policy: most_ready</code> `
      + "to get the most vehicles fully ready."
    : `Policy <b>most_ready</b>: ${o.vehicles_ready} of ${o.vehicles_total} vehicles fully ready `
      + `(charging on arrival: ${b.vehicles_ready}), then the shortfall of the others is kept as small as possible. `
      + "Set <code>shortfall_policy: fair</code> to share the shortfall instead.";
  let html = `<h3>Vehicles that fall short</h3><p class="policy">${policy}</p><ul>`;
  for (const item of r.shortfalls) {
    html += `<li><b>${esc(item.vehicle_id)}</b> (leaves ${esc(item.departure)}): ${item.shortfall_kwh.toFixed(1)} kWh short`;
    if (item.reasons.length) html += "<ul>" + item.reasons.map((x) => `<li>${esc(x)}</li>`).join("") + "</ul>";
    html += "</li>";
  }
  html += "</ul>";
  if (r.diagnosis && r.diagnosis.summary.length) {
    html += "<h3>What would help</h3><ul>" + r.diagnosis.summary.map((x) => `<li>${esc(x)}</li>`).join("") + "</ul>";
  }
  box.innerHTML = html;
  box.hidden = false;
}

// "00:45 +1d" to "03:15 +1d" -> "00:45-03:15 +1d" (same day suffix written once)
function span(start, end) {
  const m1 = /^(.*?)(\s\+\d+d)?$/.exec(start);
  const m2 = /^(.*?)(\s\+\d+d)?$/.exec(end);
  if ((m1[2] || "") === (m2[2] || "")) return `${m1[1]}\u2013${m2[1]}${m1[2] || ""}`;
  return `${start} \u2013 ${end}`;
}

function renderVehicles(r) {
  const multi = r.vehicles.some((v) => v.stops.length > 1);
  const head = "<thead><tr><th>Vehicle</th>" + (multi ? "<th>Stop</th>" : "") + "<th>Arrive</th><th>Leave</th><th class='num'>Need</th>"
    + "<th class='num'>Plan</th><th>Ready</th><th>Charging (Berth)</th>"
    + "<th class='num' title='Battery level at departure when every vehicle charges at full power on arrival'>On arrival</th></tr></thead>";
  const rows = [];
  r.vehicles.forEach((v, i) => {
    v.stops.forEach((st, k) => {
      const first = k === 0;
      const windows = first ? (v.windows.map((w) => `${esc(span(w.start, w.end))} (${w.kwh.toFixed(w.kwh < 1 ? 1 : 0)}&nbsp;kWh)`).join("<br>") || "none needed") : "";
      rows.push(
        "<tr>"
        + `<td>${first ? `<span class="dot" style="background:${vehicleColor(i)}"></span>${esc(v.id)}` : ""}</td>`
        + (multi ? `<td>${v.stops.length > 1 ? st.stop : ""}</td>` : "")
        + `<td>${esc(st.arrival)}</td><td>${esc(st.departure)}</td>`
        + `<td class="num">${st.required_soc_pct.toFixed(0)}%</td>`
        + `<td class="num">${st.departure_soc_pct.toFixed(0)}%</td>`
        + `<td>${st.ready ? '<span class="pill yes">yes</span>' : `<span class="pill no">-${st.shortfall_kwh.toFixed(1)} kWh</span>`}</td>`
        + `<td class="windows">${windows}</td>`
        + `<td class="num">${st.baseline_departure_soc_pct.toFixed(0)}%${st.baseline_ready ? "" : " (short)"}</td>`
        + "</tr>",
      );
    });
  });
  $("vehicles").innerHTML = head + "<tbody>" + rows.join("") + "</tbody>";
}

// ---- chart --------------------------------------------------------------------

function niceStep(max, ticks) {
  const raw = max / ticks;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const n = raw / mag;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * mag;
}

function drawChart(r) {
  const box = $("chart");
  const W = Math.max(box.clientWidth || 700, 320);
  const H = Math.round(Math.min(380, Math.max(240, W * 0.42)));
  const m = { l: 52, r: 12, t: 10, b: 34 };
  const n = r.times.length;
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const x = (i) => m.l + (i / n) * iw;

  const baseTotal = r.other_load_kw.map((v, i) => v + r.baseline_charging_kw[i]);
  const planTotal = r.other_load_kw.map((v, i) => v + r.total_charging_kw[i]);
  const yMax = Math.max(...r.site_limit_kw, ...baseTotal, ...planTotal, 1) * 1.06;
  const step = niceStep(yMax, 5);
  const top = Math.ceil(yMax / step) * step;
  const y = (v) => m.t + ih - (v / top) * ih;

  const parts = [];
  // price shading
  const pmin = Math.min(...r.price), pmax = Math.max(...r.price);
  let i0 = 0;
  for (let i = 1; i <= n; i++) {
    if (i === n || r.price[i] !== r.price[i0]) {
      const level = pmax > pmin ? (r.price[i0] - pmin) / (pmax - pmin) : 0;
      const alpha = (0.04 + 0.22 * level).toFixed(3);
      parts.push(`<rect x="${x(i0)}" y="${m.t}" width="${x(i) - x(i0)}" height="${ih}" fill="rgba(217,119,6,${alpha})"/>`);
      i0 = i;
    }
  }
  // grid and y axis
  for (let v = 0; v <= top + 1e-9; v += step) {
    parts.push(`<line x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}" stroke="#e3eaea"/>`);
    parts.push(`<text x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end">${Math.round(v)}</text>`);
  }
  parts.push(`<text x="12" y="${m.t + ih / 2}" transform="rotate(-90 12 ${m.t + ih / 2})" text-anchor="middle">kW</text>`);

  // stacked areas: other load, then each vehicle
  const layers = [{ values: r.other_load_kw, color: "#c9d1d3", name: "Other load" }].concat(
    r.vehicles.map((v, i) => ({ values: v.power_kw, color: vehicleColor(i), name: v.id })),
  );
  const cum = new Array(n).fill(0);
  for (const layer of layers) {
    if (!layer.values.some((v) => v > 1e-6)) continue;
    const lower = cum.slice();
    for (let i = 0; i < n; i++) cum[i] += layer.values[i];
    let d = `M${x(0)},${y(lower[0])}`;
    for (let i = 0; i < n; i++) d += `L${x(i)},${y(cum[i])}L${x(i + 1)},${y(cum[i])}`;
    for (let i = n - 1; i >= 0; i--) d += `L${x(i + 1)},${y(lower[i])}L${x(i)},${y(lower[i])}`;
    parts.push(`<path d="${d}Z" fill="${layer.color}" stroke="none"/>`);
  }

  const stepLine = (values) => {
    let d = `M${x(0)},${y(values[0])}`;
    for (let i = 0; i < n; i++) d += `L${x(i)},${y(values[i])}L${x(i + 1)},${y(values[i])}`;
    return d;
  };
  parts.push(`<path d="${stepLine(baseTotal)}" fill="none" stroke="#4b5a5d" stroke-width="1.5" stroke-dasharray="5 4"/>`);
  parts.push(`<path d="${stepLine(r.site_limit_kw)}" fill="none" stroke="#b03a2e" stroke-width="2"/>`);

  // x axis labels
  const stride = Math.max(1, Math.ceil(n / Math.max(4, Math.floor(iw / 70))));
  for (let i = 0; i < n; i += stride) {
    const label = r.times[i].replace(/^\d{4}-\d{2}-\d{2} /, "").replace(/ \+\d+d$/, "");
    parts.push(`<line x1="${x(i)}" x2="${x(i)}" y1="${m.t + ih}" y2="${m.t + ih + 4}" stroke="#9aa7a9"/>`);
    parts.push(`<text x="${x(i)}" y="${H - 14}" text-anchor="middle">${esc(label)}</text>`);
  }
  parts.push(`<line x1="${m.l}" x2="${W - m.r}" y1="${m.t + ih}" y2="${m.t + ih}" stroke="#9aa7a9"/>`);
  parts.push(`<line id="hover-line" x1="0" x2="0" y1="${m.t}" y2="${m.t + ih}" stroke="#1d2b2d" stroke-width="1" opacity="0"/>`);
  parts.push(`<rect id="hover" x="${m.l}" y="${m.t}" width="${iw}" height="${ih}" fill="transparent"/>`);

  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Stacked charging power per vehicle over time, with the site limit and price shading">${parts.join("")}</svg>`;

  $("legend").innerHTML =
    '<span><i class="sw" style="background:#c9d1d3"></i>Other load</span>'
    + `<span><i class="sw" style="background:${vehicleColor(0)}"></i>Vehicles (one color each)</span>`
    + '<span><i class="sw line" style="border-color:#b03a2e"></i>Site limit</span>'
    + '<span><i class="sw dash" style="border-color:#4b5a5d"></i>Charge on arrival</span>'
    + '<span><i class="sw" style="background:rgba(217,119,6,0.26)"></i>Higher price</span>';

  const hover = box.querySelector("#hover");
  const line = box.querySelector("#hover-line");
  const tip = $("tip");
  hover.addEventListener("mousemove", (ev) => {
    const rect = box.querySelector("svg").getBoundingClientRect();
    const sx = ((ev.clientX - rect.left) / rect.width) * W;
    const i = Math.min(n - 1, Math.max(0, Math.floor(((sx - m.l) / iw) * n)));
    line.setAttribute("x1", x(i + 0.5));
    line.setAttribute("x2", x(i + 0.5));
    line.setAttribute("opacity", "0.35");
    const charging = r.vehicles
      .map((v) => [v.id, v.power_kw[i]])
      .filter((p) => p[1] > 0.01)
      .sort((a, b) => b[1] - a[1]);
    const list = charging.slice(0, 6).map((p) => `${esc(p[0])}: ${p[1].toFixed(1)} kW`).join("<br>");
    tip.innerHTML = `<b>${esc(r.times[i])}</b>Price ${r.price[i].toFixed(3)} per kWh<br>`
      + `Charging ${r.total_charging_kw[i].toFixed(1)} kW (on arrival: ${r.baseline_charging_kw[i].toFixed(1)} kW)<br>`
      + `Other load ${r.other_load_kw[i].toFixed(0)} kW, site limit ${r.site_limit_kw[i].toFixed(0)} kW`
      + (charging.length ? `<br><br>${list}${charging.length > 6 ? `<br>+ ${charging.length - 6} more` : ""}` : "");
    tip.hidden = false;
    const bx = box.getBoundingClientRect();
    const left = ev.clientX - bx.left + 14;
    tip.style.left = `${Math.min(left, bx.width - 270)}px`;
    tip.style.top = `${ev.clientY - bx.top + box.offsetTop + 10}px`;
  });
  hover.addEventListener("mouseleave", () => { tip.hidden = true; line.setAttribute("opacity", "0"); });
}

// ---- downloads ------------------------------------------------------------------

function download(name, text, type) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function scheduleCsv(r) {
  const head = ["step", "time", "price_per_kwh", "site_limit_kw", "other_load_kw", "available_kw", "total_charging_kw", "baseline_charging_kw"]
    .concat(r.vehicles.map((v) => `${v.id}_kw`));
  const quote = (s) => (/[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s);
  const lines = [head.map(quote).join(",")];
  r.times.forEach((t, i) => {
    lines.push([i, quote(t), r.price[i], r.site_limit_kw[i], r.other_load_kw[i], r.available_kw[i],
      r.total_charging_kw[i].toFixed(3), r.baseline_charging_kw[i].toFixed(3)]
      .concat(r.vehicles.map((v) => v.power_kw[i].toFixed(3))).join(","));
  });
  return lines.join("\n") + "\n";
}

// ---- start --------------------------------------------------------------------

function wire(id, event, fn) {
  $(id).addEventListener(event, async (ev) => {
    try { await fn(ev); } catch (err) { showError(err); }
  });
}

wire("load-example", "click", loadExample);
wire("plan", "click", runPlan);
wire("file-fleet", "change", async (ev) => { if (ev.target.files[0]) await openFleetFile(ev.target.files[0]); ev.target.value = ""; });
wire("file-csv", "change", async (ev) => { if (ev.target.files[0]) await openVehiclesCsv(ev.target.files[0]); ev.target.value = ""; });
wire("dl-csv", "click", () => { if (lastResult) download("berth-schedule.csv", scheduleCsv(lastResult), "text/csv"); });
wire("dl-json", "click", () => { if (lastResult) download("berth-result.json", JSON.stringify(lastResult, null, 2), "application/json"); });
$("editor").addEventListener("input", () => save($("editor").value));
let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (lastResult) drawChart(lastResult); }, 150);
});

(async () => {
  try {
    await loadExamples();
    const text = saved();
    if (text && text.trim()) $("editor").value = text;
    else await loadExample();
  } catch (err) {
    showError(err);
  }
})();
