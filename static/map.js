// Phase 23: the taste map, drawn in SVG from /map.json. No framework, no CDN (plan.md).
// Rules from the dataviz skill: colour follows the entity (a genre keeps its colour for everyone), one person in
// the accent and the rest grey on shared plots, legends plus selective labels, hover/focus tooltips with big hit
// areas, a table view for every chart, and motion only to explain change (off under prefers-reduced-motion).
// Names and titles come from people's libraries: they're set with textContent, never as HTML.

const NS = "http://www.w3.org/2000/svg";
const root = document.getElementById("map");
const tip = document.getElementById("map-tip");
const still = matchMedia("(prefers-reduced-motion: reduce)");
const NAMES = { energy: "Energy", mood: "Mood", texture: "Texture", groove: "Groove", vocals: "Vocals", era: "Era" };
let data, selected, axes = { x: "energy", y: "mood" }, pinned = null;

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
function label(svg, x, y, text, attrs = {}) {
  const t = el("text", { x, y, ...attrs }, svg);
  t.textContent = text;
  return t;
}
function dim(key) { return data.dimensions.find((d) => d.key === key); }
function scale(key, lo, hi) {
  const d = dim(key);
  return (v) => lo + (Math.min(d.max, Math.max(d.min, v)) - d.min) / (d.max - d.min) * (hi - lo);
}
function fmt(key, v) {
  if (v === null || v === undefined) return "–";
  if (key === "era") return `${Math.round(v / 10) * 10}s`;
  return Number.isInteger(v) ? String(v) : v.toFixed(1);
}
function moveTo(node, x, y, glide) {
  const target = `translate(${x}px, ${y}px)`;
  if (!glide || still.matches) { node.style.transform = target; return; }
  requestAnimationFrame(() => requestAnimationFrame(() => { node.style.transform = target; }));
}

// --- tooltip ------------------------------------------------------------------------------------------------

// `track` (pinned tips only) gets a link to its own service; Spotify's links carry the Spotify icon
function showTip(x, y, lines, track) {
  tip.replaceChildren();
  lines.forEach((line, i) => html(i === 0 ? "strong" : "span", line, tip));
  for (const span of tip.querySelectorAll("span")) span.style.display = "block";
  if (track) {
    const a = html("a", null, tip);
    a.href = track.url;
    if (track.service === "spotify") {
      const img = html("img", null, a);
      Object.assign(img, { src: "/static/spotify-icon.svg", alt: "", width: 21, height: 20 });
    }
    a.append(`Open on ${track.service_name}`);
  }
  tip.hidden = false;
  const box = root.getBoundingClientRect();
  const left = Math.min(x - box.left + 12, box.width - tip.offsetWidth);
  tip.style.left = `${Math.max(0, left)}px`;
  tip.style.top = `${y - box.top + 12}px`;
}
function hideTip() { if (!pinned) tip.hidden = true; }
function unpin() { pinned = null; tip.classList.remove("pinned"); tip.hidden = true; for (const p of root.querySelectorAll(".point.hot")) p.classList.remove("hot"); }
document.addEventListener("keydown", (e) => { if (e.key === "Escape") unpin(); });
document.addEventListener("pointerdown", (e) => { if (pinned && !tip.contains(e.target) && !e.target.closest(".panel svg")) unpin(); });

function tipOn(target, lines) {
  const show = () => { const r = target.getBoundingClientRect(); showTip(r.left + r.width / 2, r.top + r.height / 2, lines()); };
  target.addEventListener("pointerenter", show);
  target.addEventListener("focus", show);
  target.addEventListener("pointerleave", hideTip);
  target.addEventListener("blur", hideTip);
}

// --- tables ---------------------------------------------------------------------------------------------------

function table(parent, head, rows) {
  const details = html("details", null, parent);
  html("summary", "As a table", details);
  const wrap = html("div", null, details, "table-wrap");
  const t = html("table", null, wrap);
  const tr = html("tr", null, html("thead", null, t));
  head.forEach((h) => { const th = html("th", h, tr); th.scope = "col"; });
  const body = html("tbody", null, t);
  for (const row of rows) {
    const r = html("tr", null, body);
    row.forEach((cell, i) => { const c = html(i === 0 ? "th" : "td", cell, r); if (i === 0) c.scope = "row"; });
  }
}

// --- 1. profiles: one row per dimension, one dot per person on a shared axis -------------------------------

function drawProfiles(glide) {
  const box = root.querySelector("#profiles");
  const svg = box.querySelector("svg") || el("svg", {}, box);
  svg.replaceChildren();
  const width = box.clientWidth, pad = 12, rowH = 64;
  svg.setAttribute("viewBox", `0 0 ${width} ${data.dimensions.length * rowH}`);
  svg.setAttribute("height", data.dimensions.length * rowH);
  data.dimensions.forEach((d, i) => {
    const y = i * rowH + 30, x = scale(d.key, pad, width - pad);
    label(svg, pad, y - 16, NAMES[d.key], { "font-weight": 600 });
    el("line", { x1: pad, x2: width - pad, y1: y, y2: y, class: "axis" }, svg);
    label(svg, pad, y + 22, d.key === "era" ? "1940s or older" : `1 ${d.low}`, { class: "muted" });
    label(svg, width - pad, y + 22, d.key === "era" ? "2020s" : `${d.high} 10`, { class: "muted", "text-anchor": "end" });
    const order = [...data.people].sort((a, b) => (a.id === selected) - (b.id === selected));  // selected drawn last
    for (const p of order) {
      const v = p.profile[d.key];
      if (v.mean === null) continue;
      const on = p.id === selected, colour = on ? "var(--accent)" : "var(--others)";
      // The spread bar is drawn around the dot and stops at the axis ends
      const spread = el("line", { x1: x(v.mean - v.spread) - x(v.mean), x2: x(v.mean + v.spread) - x(v.mean), y1: 0, y2: 0,
        class: "spread", stroke: colour, "stroke-opacity": .5 }, svg);
      const hit = el("circle", { r: 14, class: "hit", tabindex: on ? 0 : -1, "aria-label": `${p.name}, ${NAMES[d.key]} ${fmt(d.key, v.mean)}` }, svg);
      const dot = el("circle", { r: on ? 6 : 5, class: "person-dot", fill: colour }, svg);
      for (const node of [spread, hit, dot]) {
        node.style.transform = `translate(${width / 2}px, ${y}px)`;
        moveTo(node, x(v.mean), y, glide);
      }
      tipOn(hit, () => [`${fmt(d.key, v.mean)} ${d.key === "era" ? "" : "of 10"}`.trim(), `${p.name} · ${NAMES[d.key]}, spread ±${fmt(d.key === "era" ? "x" : d.key, v.spread)}`]);
      hit.addEventListener("click", () => select(p.id));
    }
  });
  const legend = root.querySelector("#profiles-legend");
  legend.replaceChildren();
  const me = data.people.find((p) => p.id === selected);
  const a = html("span", null, legend); html("i", null, a, "key dot").style.background = "var(--accent)"; a.append(me.name);
  if (data.people.length > 1) { const b = html("span", null, legend); html("i", null, b, "key dot").style.background = "var(--others)"; b.append("everyone else"); }
  const c = html("span", "line: spread of their music", legend);
  c.style.marginLeft = "auto";
}

function select(id) {
  selected = id;
  for (const b of root.querySelectorAll(".people button")) b.setAttribute("aria-pressed", String(b.dataset.id === id));
  drawProfiles(false);
}

// --- 2. track map: small multiples, one panel per person, nearest-point hover ------------------------------

function jitter(url, salt) {
  // FNV-1a, then a final mix, so similar ids (and the x and y of one song) don't move together
  let h = 2166136261;
  for (const ch of salt + url) h = Math.imul(h ^ ch.charCodeAt(0), 16777619);
  h ^= h >>> 15; h = Math.imul(h, 2246822507); h ^= h >>> 13; h = Math.imul(h, 3266489909); h ^= h >>> 16;
  return (h >>> 0) / 4294967296 - 0.5;
}

function drawPanels(glide) {
  const box = root.querySelector("#panels");
  if (!box.children.length) {
    for (const p of data.people) {
      const panel = html("div", null, box, "panel");
      html("h3", p.name, panel);
      html("p", p.tracks.length ? `${p.tracks.length} of ${p.tracks_total} songs` : "Moodify is still describing this music.", panel, "note");
      el("svg", { "data-id": p.id, role: "img", "aria-label": `${p.name}'s songs by ${axes.x} and ${axes.y}` }, panel);
    }
  }
  for (const svg of box.querySelectorAll("svg")) {
    const p = data.people.find((q) => q.id === svg.dataset.id);
    const width = svg.parentNode.clientWidth, height = Math.round(width * 0.8);
    const left = 48, bottom = 30, top = 24, right = 8;
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("height", height);
    svg.setAttribute("aria-label", `${p.name}'s songs by ${axes.x} and ${axes.y}`);
    let points = svg.querySelector(".points");
    const fresh = !points;
    for (const node of [...svg.children]) if (node !== points) node.remove();
    const x = scale(axes.x, left, width - right), y = scale(axes.y, height - bottom, top);
    const dx = dim(axes.x), dy = dim(axes.y);
    for (const v of [dx.min, (dx.min + dx.max) / 2, dx.max]) {
      el("line", { x1: x(v), x2: x(v), y1: top, y2: height - bottom, class: "grid" }, svg);
    }
    for (const v of [dy.min, (dy.min + dy.max) / 2, dy.max]) {
      el("line", { x1: left, x2: width - right, y1: y(v), y2: y(v), class: "grid" }, svg);
    }
    el("line", { x1: left, x2: width - right, y1: height - bottom, y2: height - bottom, class: "axis" }, svg);
    label(svg, left, height - 8, dx.key === "era" ? "older" : dx.low, { class: "muted" });
    label(svg, width - right, height - 8, dx.key === "era" ? "newer" : dx.high, { class: "muted", "text-anchor": "end" });
    label(svg, (left + width) / 2, height - 8, NAMES[dx.key], { "text-anchor": "middle" });
    label(svg, left, 14, NAMES[dy.key], {});
    label(svg, 2, top + 10, dy.key === "era" ? "newer" : dy.high, { class: "muted" });
    label(svg, 2, height - bottom - 4, dy.key === "era" ? "older" : dy.low, { class: "muted" });
    if (fresh) {
      points = el("g", { class: "points" }, svg);
      for (const t of p.tracks) {
        const c = el("circle", { r: Math.min(8, 3.5 + t.weight * 0.8), class: t.common ? "point common" : "point" }, points);
        c.style.transform = `translate(${(left + width) / 2}px, ${(top + height - bottom) / 2}px)`;
        c.__track = t;
      }
      svg.addEventListener("pointermove", (e) => near(svg, e, false));
      svg.addEventListener("pointerleave", () => { if (!pinned) { hideTip(); for (const q of svg.querySelectorAll(".hot")) q.classList.remove("hot"); } });
      svg.addEventListener("click", (e) => near(svg, e, true));
    } else {
      svg.appendChild(points);
    }
    // Scores are whole numbers, so songs share spots: spread each a little, the same way every time
    for (const c of points.children) {
      const t = c.__track;
      c.__x = x(t[axes.x] + jitter(t.url, "x") * (axes.x === "era" ? 7 : 0.7));
      c.__y = y(t[axes.y] + jitter(t.url, "y") * (axes.y === "era" ? 7 : 0.7));
      moveTo(c, c.__x, c.__y, glide);
    }
  }
}

function near(svg, e, pin) {
  const r = svg.getBoundingClientRect(), scaleX = svg.viewBox.baseVal.width / r.width;
  const px = (e.clientX - r.left) * scaleX, py = (e.clientY - r.top) * scaleX;
  let best = null, bestD = (24 * scaleX) ** 2;
  for (const c of svg.querySelectorAll(".point")) {
    const d = (c.__x - px) ** 2 + (c.__y - py) ** 2;
    if (d < bestD) { best = c; bestD = d; }
  }
  if (pinned && !pin) return;
  for (const q of root.querySelectorAll(".point.hot")) q.classList.remove("hot");
  if (!best) { if (pin) unpin(); else hideTip(); return; }
  best.classList.add("hot");
  const t = best.__track;
  showTip(e.clientX, e.clientY, [t.name, t.artists.join(", "),
    `${NAMES[axes.x]} ${fmt(axes.x, t[axes.x])} · ${NAMES[axes.y]} ${fmt(axes.y, t[axes.y])}`,
    ...(t.common ? ["Common ground: a friend has it too"] : [])], pin ? t : null);
  if (pin) { pinned = best; tip.classList.add("pinned"); }
}

// --- 3. taste match: a heatmap in one sequential hue, values in the cells --------------------------------

function step(v) { return v === null ? 0 : Math.min(5, 1 + Math.floor(v / 20)); }

function drawMatch() {
  const box = root.querySelector("#match");
  box.replaceChildren();
  const people = data.people, n = people.length;
  if (n < 2) { html("p", "Taste match needs at least one friend who shows their taste.", box, "note"); return; }
  const value = (a, b) => (data.match.find((m) => (m.a === a && m.b === b) || (m.a === b && m.b === a)) || {}).match ?? null;
  const labelW = 70, cell = Math.min(64, Math.floor((box.clientWidth - labelW) / n));
  const svg = el("svg", { viewBox: `0 0 ${labelW + n * cell} ${24 + n * cell}`, height: 24 + n * cell, role: "img", "aria-label": "Taste match between each pair" }, box);
  svg.style.maxWidth = `${labelW + n * cell}px`;
  people.forEach((p, j) => label(svg, labelW + j * cell + cell / 2, 16, p.name.slice(0, 8), { "text-anchor": "middle" }));
  people.forEach((a, i) => {
    label(svg, 0, 24 + i * cell + cell / 2 + 4, a.name.slice(0, 9), {});
    people.forEach((b, j) => {
      const x = labelW + j * cell, y = 24 + i * cell;
      if (i === j) { label(svg, x + cell / 2, y + cell / 2 + 4, "–", { class: "muted", "text-anchor": "middle" }); return; }
      const v = value(a.id, b.id), s = step(v);
      const rect = el("rect", { x, y, width: cell, height: cell, rx: 4, class: "cell", fill: s ? `var(--seq-${s})` : "var(--grid)", tabindex: 0,
        "aria-label": `${a.name} and ${b.name}: ${v ?? "not enough music yet"}` }, svg);
      const t = label(svg, x + cell / 2, y + cell / 2 + 5, v ?? "?", { "text-anchor": "middle", class: `cell-text s${s}` });
      t.style.pointerEvents = "none";
      tipOn(rect, () => [v === null ? "Not enough music described yet" : `${v} of 100`, `${a.name} × ${b.name}`]);
    });
  });
  const legend = html("div", null, box, "legend");
  html("span", "Less alike", legend);
  for (let s = 1; s <= 5; s++) { const k = html("i", null, legend, "key"); k.style.background = `var(--seq-${s})`; k.style.margin = "0"; }
  html("span", "more alike (0–100)", legend);
  html("h3", "Common ground", box).style.fontSize = "1rem";
  const list = html("ul", null, box, "pairs");
  for (const c of data.common) {
    const a = people.find((p) => p.id === c.a), b = people.find((p) => p.id === c.b);
    const li = html("li", null, list);
    html("strong", `${a.name} & ${b.name}`, li);
    html("div", c.artists.length ? `Both play: ${c.artists.join(", ")}` : "No artists in common yet.", li, "note");
    if (c.tracks.length) {
      const songs = html("div", "Same songs: ", li, "note");
      c.tracks.forEach((t, i) => {
        if (i) songs.append(" · ");
        const link = html("a", `${t.name} by ${t.artists[0]}`, songs);
        link.href = t.url;
      });
    }
  }
  table(box, ["Pair", "Match (0–100)"], data.match.map((m) => {
    const a = people.find((p) => p.id === m.a), b = people.find((p) => p.id === m.b);
    return [`${a.name} × ${b.name}`, m.match ?? "–"];
  }));
}

// --- 4. genres: one stacked bar per person, top genres plus Other ------------------------------------------

// A label inside a coloured fill: white, except near-black on the grey "Other" and, in light mode, on yellow (slot 4)
function inkOn(genre) {
  const slot = data.genres.indexOf(genre) + 1;
  if (!slot || (slot === 4 && !matchMedia("(prefers-color-scheme: dark)").matches)) return "#0b0b0b";
  return "#ffffff";
}

function drawGenres() {
  const box = root.querySelector("#genres");
  box.replaceChildren();
  const legend = html("div", null, box, "legend");
  const colour = (g) => { const i = data.genres.indexOf(g); return i >= 0 ? `var(--genre-${i + 1})` : "var(--genre-other)"; };
  for (const g of [...data.genres, "Other"]) { const s = html("span", null, legend); html("i", null, s, "key").style.background = colour(g); s.append(g); }
  const width = box.clientWidth, labelW = 70, barH = 20, rowH = 40, gap = 2;
  const people = data.people.filter((p) => p.genres.length);
  if (!people.length) { html("p", "Moodify is still describing everyone's music.", box, "note"); return; }
  const svg = el("svg", { viewBox: `0 0 ${width} ${people.length * rowH}`, height: people.length * rowH, role: "img", "aria-label": "Genres per person" }, box);
  people.forEach((p, i) => {
    const y = i * rowH + (rowH - barH) / 2;
    label(svg, 0, y + barH / 2 + 4, p.name.slice(0, 9), {});
    let x = labelW;
    const span = width - labelW;
    p.genres.forEach(([g, share], k) => {
      const w = Math.max(0, share * span - (k < p.genres.length - 1 ? gap : 0));
      if (w <= 0) { x += share * span; return; }
      const last = k === p.genres.length - 1;
      const seg = last
        ? el("path", { d: `M${x},${y}h${Math.max(0, w - 4)}a4,4 0 0 1 4,4v${barH - 8}a4,4 0 0 1 -4,4h${-Math.max(0, w - 4)}z`, class: "segment", fill: colour(g) }, svg)
        : el("rect", { x, y, width: w, height: barH, class: "segment", fill: colour(g) }, svg);
      tipOn(seg, () => [`${Math.round(share * 100)}%`, `${p.name} · ${g}`]);
      const text = label(svg, x + 6, y + barH / 2 + 4, `${Math.round(share * 100)}%`, { class: "inbar" });
      // Inside the segment only when it fits with room to spare; otherwise the legend, tooltip and table carry it
      if (text.getComputedTextLength() + 12 > w) text.remove();
      else text.style.fill = inkOn(g);
      x += share * span;
    });
  });
  table(box, ["Person", ...data.genres, "Other"], data.people.map((p) => [p.name, ...[...data.genres, "Other"].map((g) => {
    const found = p.genres.find(([name]) => name === g);
    return found ? `${Math.round(found[1] * 100)}%` : "–";
  })]));
}

// --- the profiles table and the axis controls --------------------------------------------------------------

function controls() {
  const people = root.querySelector(".people");
  for (const p of data.people) {
    const b = html("button", p.me ? `${p.name} (you)` : p.name, people);
    b.type = "button";
    b.dataset.id = p.id;
    b.setAttribute("aria-pressed", String(p.id === selected));
    b.addEventListener("click", () => select(p.id));
  }
  table(root.querySelector("#profiles-table"), ["Person", ...data.dimensions.map((d) => NAMES[d.key])],
    data.people.map((p) => [p.name, ...data.dimensions.map((d) => {
      const v = p.profile[d.key];
      return v.mean === null ? "–" : d.key === "era" ? fmt("era", v.mean) : `${fmt(d.key, v.mean)} ± ${fmt(d.key, v.spread)}`;
    })]));
  for (const axis of ["x", "y"]) {
    const select = root.querySelector(`#axis-${axis}`);
    for (const d of data.dimensions) {
      const o = html("option", NAMES[d.key], select);
      o.value = d.key;
      o.selected = d.key === axes[axis];
    }
    select.addEventListener("change", () => { axes[axis] = select.value; unpin(); drawPanels(true); });
  }
  table(root.querySelector("#panels-table"), ["Person", "Song", "Artist", ...data.dimensions.map((d) => NAMES[d.key]), "Genres"],
    data.people.flatMap((p) => p.tracks.slice(0, 20).map((t) => [p.name, t.name, t.artists.join(", "),
      ...data.dimensions.map((d) => d.key === "era" ? `${t.era}s` : t[d.key]), t.genres.join(", ")])));
}

async function start() {
  const resp = await fetch(root.dataset.src, { headers: { Accept: "application/json" } });
  if (!resp.ok) { root.querySelector(".loading").textContent = "The map couldn't load. Reload to try again."; return; }
  data = await resp.json();
  selected = data.people[0].id;
  root.querySelector(".loading").remove();
  root.querySelector(".charts").hidden = false;
  controls();
  drawProfiles(true);
  drawPanels(true);
  drawMatch();
  drawGenres();
  let last = root.clientWidth, timer;
  addEventListener("resize", () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      if (root.clientWidth === last) return;
      last = root.clientWidth;
      drawProfiles(false); drawPanels(false); drawMatch(); drawGenres();
    }, 150);
  });
}

start();
