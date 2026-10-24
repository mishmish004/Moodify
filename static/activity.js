// Phase 24: the mood timeline and "Replay the week", drawn from /activity.json. The feed itself is plain HTML
// from the server; this only hides and shows its rows while replaying. Same chart rules as map.js: one
// sequential hue for energy, size for length, hover/focus tooltips, a table view, text set as text only.
// Under prefers-reduced-motion nothing plays by itself: the day buttons step through instead.

const NS = "http://www.w3.org/2000/svg";
const root = document.getElementById("activity");
const tip = document.getElementById("map-tip");
const still = matchMedia("(prefers-reduced-motion: reduce)");
const WEEK = 7, STEP_MS = 430;  // about 3 seconds for the week
let data, cursor = null, timer = null;

function el(tag, attrs = {}, parent) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (parent) parent.appendChild(node);
  return node;
}
function html(tag, text, parent, cls) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (cls) node.className = cls;
  if (parent) parent.appendChild(node);
  return node;
}
function label(svg, x, y, text, attrs = {}) { const t = el("text", { x, y, ...attrs }, svg); t.textContent = text; return t; }
function shade(energy) { return energy === null ? 0 : Math.min(5, Math.max(1, Math.ceil(energy / 2))); }
function radius(minutes) { return 4 + 8 * Math.sqrt(Math.min(180, Math.max(0, minutes)) / 180); }
function pretty(day) { return new Date(`${day}T12:00:00Z`).toLocaleDateString(undefined, { month: "short", day: "numeric" }); }

function showTip(target, lines) {
  tip.replaceChildren();
  lines.forEach((line, i) => { const n = html(i ? "span" : "strong", line, tip); n.style.display = "block"; });
  tip.hidden = false;
  const r = target.getBoundingClientRect(), box = root.getBoundingClientRect();
  tip.style.left = `${Math.max(0, Math.min(r.left - box.left + 12, box.width - tip.offsetWidth))}px`;
  tip.style.top = `${r.bottom - box.top + 8}px`;
}

function drawTimeline() {
  const box = root.querySelector("#timeline");
  box.replaceChildren();
  const width = box.clientWidth, labelW = 70, rowH = 52, right = 14;
  const days = data.days, x = (day) => labelW + (days.indexOf(day) + 0.5) / days.length * (width - labelW - right);
  const svg = el("svg", { viewBox: `0 0 ${width} ${data.people.length * rowH + 24}`, height: data.people.length * rowH + 24,
                          role: "img", "aria-label": "Playlists per person over the last 30 days" }, box);
  data.people.forEach((p, i) => {
    const y = i * rowH + rowH / 2;
    label(svg, 0, y + 4, p.me ? "You" : p.name.slice(0, 9), {});
    el("line", { x1: labelW, x2: width - right, y1: y, y2: y, class: "axis" }, svg);
    for (const m of p.marks) {
      const s = shade(m.energy);
      const c = el("circle", { cx: x(m.day), cy: y, r: radius(m.minutes), class: "mark", tabindex: 0, "data-day": m.day,
        fill: s ? `var(--seq-${s})` : "var(--others)", "aria-label": `${p.name}: ${m.title}, ${pretty(m.day)}` }, svg);
      const lines = [m.title, `${p.me ? "You" : p.name} · ${pretty(m.day)}`, `${m.minutes} min${m.parts > 1 ? ` · ${m.parts} parts` : ""}` +
        (m.energy === null ? "" : ` · energy ${m.energy}`)];
      for (const ev of ["pointerenter", "focus"]) c.addEventListener(ev, () => showTip(c, lines));
      for (const ev of ["pointerleave", "blur"]) c.addEventListener(ev, () => { tip.hidden = true; });
    }
  });
  const bottom = data.people.length * rowH + 16;
  label(svg, labelW, bottom, pretty(days[0]), { class: "muted" });
  label(svg, width - right, bottom, "today", { class: "muted", "text-anchor": "end" });
  applyCursor();
}

function legend() {
  const box = root.querySelector("#timeline-legend");
  html("span", "Energy: low", box);
  for (let s = 1; s <= 5; s++) { const k = html("i", null, box, "key dot"); k.style.background = `var(--seq-${s})`; k.style.margin = "0"; }
  html("span", "high · size: length", box);
}

function table() {
  const details = html("details", null, root.querySelector("#timeline-table"));
  html("summary", "As a table", details);
  const t = html("table", null, html("div", null, details, "table-wrap"));
  const head = html("tr", null, html("thead", null, t));
  for (const h of ["Who", "Day", "Playlist", "Minutes", "Energy"]) { const th = html("th", h, head); th.scope = "col"; }
  const body = html("tbody", null, t);
  for (const p of data.people) for (const m of p.marks) {
    const r = html("tr", null, body);
    const th = html("th", p.me ? "You" : p.name, r); th.scope = "row";
    for (const v of [m.day, m.title, m.minutes, m.energy ?? "–"]) html("td", v, r);
  }
}

// --- replay ---------------------------------------------------------------------------------------------

function applyCursor() {
  const shown = (day) => cursor === null || day <= cursor;
  for (const li of document.querySelectorAll("#feed li")) li.hidden = !shown(li.dataset.day);
  for (const m of root.querySelectorAll(".mark")) m.classList.toggle("later", !shown(m.dataset.day));
  root.querySelector("#replay-day").textContent = cursor === null ? "" : `Up to ${pretty(cursor)}`;
}
function weekStart() { return data.days.length - WEEK; }
function move(by) {
  stop();
  const days = data.days, i = cursor === null ? days.length - 1 : days.indexOf(cursor);
  const next = Math.min(days.length - 1, Math.max(weekStart() - 1, i + by));
  cursor = next === days.length - 1 && by > 0 ? null : days[next];
  applyCursor();
}
function stop() {
  clearInterval(timer);
  timer = null;
  root.querySelector("#replay-play").textContent = "Replay the week";
}
function play() {
  if (timer) { stop(); return; }
  if (still.matches) { move(1); return; }  // no motion asked for: one day per press
  let i = cursor === null ? weekStart() - 1 : data.days.indexOf(cursor);
  cursor = data.days[i];
  applyCursor();
  root.querySelector("#replay-play").textContent = "Pause";
  timer = setInterval(() => {
    i += 1;
    if (i >= data.days.length) { stop(); cursor = null; applyCursor(); return; }
    cursor = data.days[i];
    applyCursor();
  }, STEP_MS);
}

async function start() {
  const resp = await fetch(root.dataset.src, { headers: { Accept: "application/json" } });
  if (!resp.ok) return;
  data = await resp.json();
  root.querySelector(".timeline-section").hidden = false;
  if (still.matches) root.querySelector("#replay-play").textContent = "Replay the week, a day at a time";
  root.querySelector("#replay-play").addEventListener("click", () => {
    if (still.matches && cursor === null) { cursor = data.days[weekStart() - 1]; applyCursor(); return; }
    play();
  });
  root.querySelector("#replay-back").addEventListener("click", () => move(-1));
  root.querySelector("#replay-next").addEventListener("click", () => move(1));
  legend();
  drawTimeline();
  table();
  let last = root.clientWidth, wait;
  addEventListener("resize", () => {
    clearTimeout(wait);
    wait = setTimeout(() => { if (root.clientWidth !== last) { last = root.clientWidth; drawTimeline(); } }, 150);
  });
}

start();
