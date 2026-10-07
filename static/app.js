const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const SEV = ["none", "low", "moderate", "high"];
const WHEN = { now: "Now", day_before: "Day before", day_of: "Day of travel", if_conditions_change: "If things change" };
let map, mapLayers = [];

function isoLocal(d) { return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10); }
function show(id, on = true) { $(id).classList.toggle("hidden", !on); }

async function api(path, body) {
  const res = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || data));
  return data;
}

async function init() {
  const t = new Date(); t.setDate(t.getDate() + 1);
  $("#date").value = isoLocal(t);
  $("#date").min = isoLocal(new Date());
  try {
    const h = await api("/api/health");
    $("#ai-pill").textContent = h.ai_enabled ? `AI: Claude (${h.model})` : "AI off · rule-based fallback";
    $("#ai-pill").className = "pill " + (h.ai_enabled ? "on" : "off");
  } catch { $("#ai-pill").textContent = "server unreachable"; }

  document.querySelectorAll(".chip").forEach((b) => b.addEventListener("click", () => { $("#nl-text").value = b.dataset.q; parseAndRun(); }));
  $("#nl-form").addEventListener("submit", (e) => { e.preventDefault(); parseAndRun(); });
  $("#trip-form").addEventListener("submit", (e) => { e.preventDefault(); runAssessment(); });
  for (const id of ["#origin", "#destination"]) $(id).addEventListener("input", (e) => suggest(e.target.value));
  $("#show-context").addEventListener("change", () => document.querySelectorAll("tr.context").forEach((r) => r.classList.toggle("hidden", !$("#show-context").checked)));
}

let suggestTimer;
function suggest(q) {
  clearTimeout(suggestTimer);
  suggestTimer = setTimeout(async () => {
    if (q.length < 2) return;
    const list = await api(`/api/airports?q=${encodeURIComponent(q)}`).catch(() => []);
    $("#airport-list").innerHTML = list.map((a) => `<option value="${esc(a.iata)}">${esc(a.iata)} · ${esc(a.name)} (${esc(a.city)}, ${esc(a.state)})</option>`).join("");
  }, 200);
}

async function parseAndRun() {
  const text = $("#nl-text").value.trim();
  if (!text) return;
  $("#parse-note").textContent = "Understanding your request…";
  try {
    const p = await api("/api/parse", { text });
    $("#origin").value = p.origin_airport || p.origin;
    $("#destination").value = p.destination_airport || p.destination;
    $("#date").value = p.date;
    const how = p.parser?.mode === "ai" ? `Parsed by Claude (${p.parser.model})` : "Parsed by rule-based fallback";
    const assumptions = (p.assumptions || []).length ? ` · Assumed: ${p.assumptions.join("; ")}` : "";
    $("#parse-note").textContent = `${how}: ${p.origin} → ${p.destination} on ${p.date}${assumptions}`;
    runAssessment();
  } catch (e) {
    $("#parse-note").textContent = `Couldn't parse that: ${e.message}`;
  }
}

async function runAssessment() {
  show("#results", false); show("#error", false); show("#loading");
  $("#assess-btn").disabled = true;
  try {
    const r = await api("/api/assess", { origin: $("#origin").value.trim(), destination: $("#destination").value.trim(), date: $("#date").value });
    render(r);
    show("#results");
    drawMap(r.map, r.assessment);
  } catch (e) {
    $("#error").textContent = e.message; show("#error");
  } finally {
    show("#loading", false); $("#assess-btn").disabled = false;
  }
}

function cites(ids) { return (ids || []).map((id) => `<a class="cite" data-id="${esc(id)}">${esc(id)}</a>`).join(""); }
function linkify(text) { return esc(text).replace(/\[\s*(E\d+(?:\s*[,;]\s*E\d+)*)\s*\]/g, (_, ids) => cites(ids.match(/E\d+/g))); }

function render(r) {
  const { trip, assessment: a, briefing: b } = r;
  const dateStr = new Date(trip.date + "T12:00:00").toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  const out = trip.days_out === 0 ? "today" : trip.days_out === 1 ? "tomorrow" : `${trip.days_out} days out`;

  $("#banner").className = `card banner ${a.level}`;
  $("#banner").innerHTML = `
    <div class="level ${a.level}">${a.level}<small>disruption risk · ${esc(a.confidence)} confidence</small></div>
    <div>
      <div class="trip">${esc(trip.origin_airport)} → ${esc(trip.destination_airport)} · ${esc(dateStr)} (${out})</div>
      <div class="muted small">${esc(trip.origin_name)} → ${esc(trip.destination_name)} · ${trip.distance_km.toLocaleString()} km
        ${trip.alternates_checked.length ? ` · also checked ${esc(trip.alternates_checked.join(", "))}` : ""}</div>
      <ul>${a.confidence_reasons.map((x) => `<li>${esc(x)}</li>`).join("")}${trip.notes.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
      ${a.drivers.length ? `<div class="small">Driven by ${cites(a.drivers)}</div>` : ""}
    </div>`;

  const m = b.meta || {};
  const badge = m.mode === "ai"
    ? `<span class="ai-badge">AI briefing · ${esc(m.model)} · ${(m.latency_ms / 1000).toFixed(1)}s${m.dropped_citations?.length ? ` · ${m.dropped_citations.length} unsupported claim(s) removed` : " · all citations verified"}${m.backfilled?.length ? ` · ${esc(m.backfilled.join(" & "))} filled from rules` : ""}</span>`
    : `<span class="ai-badge fallback" title="${esc(m.error || "")}">Rule-based summary (AI unavailable)</span>`;
  const disagree = m.mode === "ai" && b.ai_risk_level !== a.level
    ? `<div class="disagree"><strong>AI second opinion: ${esc(b.ai_risk_level)}</strong> (rules say ${esc(a.level)}). ${linkify(b.level_rationale)}</div>` : "";
  $("#briefing").innerHTML = `
    ${badge}
    <p class="headline">${esc(b.headline)}</p>
    <p>${linkify(b.summary)}</p>
    ${disagree}
    <h3>Key risks</h3>
    ${b.key_risks.length ? b.key_risks.map((k) => `<div class="risk-item"><strong>${esc(k.title)}</strong> <span class="likelihood">· ${esc(k.likelihood)} likelihood</span> ${cites(k.evidence_ids)}<div class="small">${linkify(k.explanation)}</div></div>`).join("") : `<div class="muted small">No material risks identified in the evidence.</div>`}
    <h3>Recommended actions</h3>
    ${b.actions.map((x) => `<div class="action-item"><span class="when ${esc(x.when)}">${esc(WHEN[x.when] || x.when)}</span><strong>${esc(x.action)}</strong> ${cites(x.evidence_ids)}<div class="small muted">${linkify(x.why)}</div></div>`).join("")}
    <h3>Uncertainty</h3>
    <p class="small">${linkify(b.uncertainty)}</p>
    ${b.watch_for?.length ? `<h3>Watch for</h3><ul class="small">${b.watch_for.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}`;

  $("#segments").innerHTML = a.segments.map((s) => `
    <div class="segment s${s.severity}">
      <div class="label">${esc(s.label)}</div>
      <div class="lvl">${s.key === "enroute" && !s.signal_ids.length ? "—" : esc(s.level)}${s.corroborated ? ' <span class="tag">corroborated</span>' : ""}</div>
      <div class="small">${esc(s.top || (s.key === "enroute" && !s.signal_ids.length ? "No en-route hazards (or not applicable for this date)" : "No moderate/high signals"))}</div>
      <div class="small">${cites(s.signal_ids)}</div>
    </div>`).join("");
  $("#alternates").innerHTML = a.alternates.length
    ? `<div class="alts"><strong>Same-metro alternates:</strong> ${a.alternates.map((x) => `<span class="alt"><span class="sev s${x.severity}">${esc(x.level)}</span> ${esc(x.iata)} <span class="muted">(${x.side})</span>${x.top ? ` – ${esc(x.top)}` : ""}</span>`).join("")}</div>` : "";

  $("#evidence-body").innerHTML = r.evidence.map((e) => {
    const ctx = !e.counts_toward_score || e.severity === 0;
    return `<tr id="ev-${esc(e.id)}" class="${ctx ? "context" : ""}">
      <td><strong>${esc(e.id)}</strong></td>
      <td>${esc(e.location)}</td>
      <td>${e.url ? `<a href="${esc(e.url)}" target="_blank" rel="noopener">${esc(e.source_name)}</a>` : esc(e.source_name)}</td>
      <td><span class="sev s${e.severity}">${SEV[e.severity]}</span>${e.counts_toward_score ? "" : '<span class="tag">context only</span>'}</td>
      <td><strong>${esc(e.title)}</strong><div>${esc(e.detail)}</div><div class="relevance">${esc(e.relevance)}</div>
        ${e.raw ? `<details><summary>raw source data</summary><pre>${esc(e.raw)}</pre></details>` : ""}</td>
      <td>${esc(e.confidence)}${e.issued_at ? `<div class="muted small">issued ${esc(new Date(e.issued_at).toLocaleString())}</div>` : ""}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="6" class="muted">No evidence returned for this date — see sources below for horizons.</td></tr>`;
  $("#show-context").dispatchEvent(new Event("change"));

  $("#sources").innerHTML = r.sources.map((s) => `
    <div class="source">
      <div><span class="st ${s.status}">${s.status.replace("_", " ")}</span> · <a href="${esc(s.url)}" target="_blank" rel="noopener"><strong>${esc(s.name)}</strong></a></div>
      <div class="muted small">Horizon: ${esc(s.horizon)}${s.latency_ms != null ? ` · ${s.latency_ms} ms` : ""}</div>
      <div class="small">${esc(s.message)}</div>
    </div>`).join("");
  $("#footer-meta").textContent = `Generated ${new Date(r.generated_at).toLocaleString()} in ${(r.elapsed_ms / 1000).toFixed(1)}s. Live data; responses cached up to 5 minutes.`;

  document.querySelectorAll(".cite").forEach((c) => c.addEventListener("click", () => {
    const row = document.getElementById(`ev-${c.dataset.id}`);
    if (!row) return;
    row.classList.remove("hidden", "flash"); void row.offsetWidth; row.classList.add("flash");
    row.scrollIntoView({ behavior: "smooth", block: "center" });
  }));
}

function drawMap(m, a) {
  if (!map) {
    map = L.map("map", { scrollWheelZoom: false });
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 10, attribution: "© OpenStreetMap" }).addTo(map);
  }
  mapLayers.forEach((l) => l.remove()); mapLayers = [];
  const color = { LOW: "#1f9d55", MODERATE: "#d98a00", HIGH: "#d23b3b", UNKNOWN: "#6b7686" }[a.level];
  const segSev = Object.fromEntries(a.segments.filter((s) => s.airport).map((s) => [s.airport, s.severity]));
  const altSev = Object.fromEntries(a.alternates.map((s) => [s.iata, s.severity]));
  const sevColor = (s) => ["#1f9d55", "#1f9d55", "#d98a00", "#d23b3b"][s ?? 0];
  mapLayers.push(L.polyline(m.route, { color, weight: 4, dashArray: "8 6" }).addTo(map));
  m.polygons.forEach((p) => mapLayers.push(L.polygon(p.coords, { color: "#d23b3b", weight: 1, fillOpacity: 0.2 }).bindTooltip(`${p.hazard} SIGMET`).addTo(map)));
  m.airports.forEach((ap) => {
    const primary = ap.role !== "alternate";
    const sev = primary ? segSev[ap.iata] : altSev[ap.iata];
    mapLayers.push(L.circleMarker([ap.lat, ap.lon], { radius: primary ? 9 : 6, color: sevColor(sev), fillColor: sevColor(sev), fillOpacity: primary ? 0.9 : 0.15, weight: 2 })
      .bindTooltip(primary ? ap.iata : `${ap.iata} · ${ap.name}`, { permanent: primary, direction: "top" }).addTo(map));
  });
  const bounds = L.latLngBounds(m.airports.map((p) => [p.lat, p.lon])).pad(0.25);
  // The container may have just been un-hidden; size must be recomputed before fitting.
  setTimeout(() => { map.invalidateSize(); map.fitBounds(bounds); }, 60);
}

init();
