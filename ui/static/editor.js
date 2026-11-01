// diskmap ui — layout editor (T-061): add partitions after the last one or into a gap between two
// (T-076: `after`, the partition before it — the ones after it are renumbered, D50), repartition the
// whole disk, or resize a partition: shrink it and add partitions after it (T-069), or grow it
// into the free space right after it (T-074). The resize editor keeps its first name, "shrink". The rows are a layout as `diskmap plan` reads it (format, label, size in
// diskutil's units, 20%, rest); the sketch above them is drawn here and is approximate. The
// plan is the core's: what it shows after "Show plan" — the map after, what is lost, the
// preview — is exact, and any edit throws it away, so a plan is never shown beside rows it
// was not built from. Nothing here applies anything (T-062: sudo diskmap apply in a terminal).
"use strict";

const MIB = 2 ** 20;
const MB = 1e6; // what the page writes itself — by dragging, by default — is decimal, as it prints sizes (D43, D49)
const UNITS = { "": 1, k: 1e3, m: 1e6, g: 1e9, t: 1e12, p: 1e15, e: 1e18,
  ki: 2 ** 10, mi: 2 ** 20, gi: 2 ** 30, ti: 2 ** 40, pi: 2 ** 50, ei: 2 ** 60 };
const KIB4 = 4096; // the core rounds a new size up to this (core/planner/shrink.py)
const KIND_TITLE = {
  add: (ed) => (ed.after ? `Add partitions after ${ed.after} on ${ed.disk}` : `Add partitions after the last one on ${ed.disk}`),
  repartition: (ed) => `Repartition ${ed.disk}`,
  shrink: (ed) => `Resize ${ed.slice}`,
};

function formatInfo(name) {
  const all = state.formats ? state.formats.formats : [];
  return all.find((f) => f.format === name) || null;
}

// required / optional / none, as the planner's label_rule says (core/planner/common.py).
function labelRule(name) {
  const f = formatInfo(name);
  return (f && f.label) || "optional";
}

function missingLabel(r) {
  return labelRule(r.format) === "required" && !r.label;
}

function formatFamily(name) {
  const n = name.toLowerCase();
  if (n.includes("ntfs")) return "ntfs";
  if (n.includes("ext")) return "ext";
  if (n.includes("apfs")) return "apfs";
  if (n.includes("hfs")) return "hfs";
  if (n.includes("fat") || n.includes("ms-dos") || n === "efi") return "fat";
  return "other";
}

function defaultFormat() {
  const ok = state.formats ? state.formats.formats.filter((f) => f.plannable) : [];
  return (ok.find((f) => f.format === "ExFAT") || ok[0] || { format: "ExFAT" }).format;
}

function tailOf(disk) {
  const ex = disk.extents || [];
  const last = ex[ex.length - 1];
  return last && last.kind === "free" && last.bytes >= SLACK ? last : null;
}

// The free space right after partition AFTER when another partition follows it (D50); else null.
function gapAfter(disk, after) {
  const ex = disk.extents || [];
  const i = ex.findIndex((e) => e.device === after);
  const free = ex[i + 1];
  if (i < 0 || !free || free.kind !== "free" || free.bytes < SLACK) return null;
  const next = ex.slice(i + 2).find((e) => e.kind !== "free");
  return next ? { free, next } : null;
}

function diskByName(name) {
  return state.map ? state.map.disks.find((d) => d.device === name) : undefined;
}

function capacityOf(disk, kind, after = null) {
  if (kind === "repartition") return disk.size;
  if (after) {
    const g = gapAfter(disk, after);
    return g ? g.free.bytes : 0;
  }
  const t = tailOf(disk);
  return t ? t.bytes : 0;
}

// Where the rows go, in bytes: BASE before them on the sketch, CAPACITY for them, SPAN the
// sketch's whole length. For add and repartition the rows have all of it. For a resize the
// sketch is the partition and the free space right after it: BASE is the partition's new size,
// rounded up to 4 KiB as the core does, the rows take the rest — when it shrinks the last one.
// Above its size it grows (T-074): up to limits.maximum, the core's edge of the free space (D48), no rows.
// ERROR: what keeps the sketch from being drawn.
function geometry(ed, disk) {
  if (ed.kind !== "shrink") {
    const c = capacityOf(disk, ed.kind, ed.after);
    return { base: 0, span: c, capacity: c };
  }
  const ex = disk.extents || [];
  const i = ex.findIndex((e) => e.device === ed.slice);
  if (i < 0) return { error: `${ed.slice} is no longer on ${ed.disk}.`, gone: true };
  const part = ex[i];
  const after = ex.slice(i + 1);
  const last = after.every((e) => e.kind === "free");
  const k = after.findIndex((e) => e.kind !== "free");
  const room = (k < 0 ? after : after.slice(0, k)).reduce((a, e) => a + e.bytes, 0); // free right after it
  const span = part.bytes + room;
  const g = { part, last, next: last ? null : after[k], span, base: part.bytes, capacity: 0, grow: false };
  const to = parseSize(ed.draft.to);
  if (to.error || to.kind !== "fixed") {
    return { ...g, error: `new size: ${to.error || "give a size such as 900MB or 20GB, not a percentage or rest"}` };
  }
  const base = Math.ceil(to.bytes / KIB4) * KIB4;
  // The size it has: drawn as it is, the handle to drag; only Show plan says it is no change.
  if (base === part.bytes) return { ...g, base, capacity: span - base, same: true };
  if (base > part.bytes) {
    const max = (ed.limits && ed.limits.maximum) || part.bytes;
    if (max <= part.bytes) return { ...g, error: `${ed.slice} cannot grow: there is no free space right after it` };
    if (base > max) return { ...g, error: `${ed.slice} can be grown to no more than ${size(max)}: the free space right after it` };
    if (ed.rows.length) return { ...g, error: "a grow adds no partitions (diskutil refuses them): remove the rows below" };
    return { ...g, base, capacity: span - base, grow: true };
  }
  if (ed.limits && base < ed.limits.minimum) {
    return { ...g, error: `${ed.slice} can be shrunk to no less than ${size(ed.limits.minimum)}: the space its files take` };
  }
  return { ...g, base, capacity: span - base };
}

// --- sizes, as desired.py reads them; for the sketch only ---------------------------------

function parseSize(text) {
  const s = text.trim();
  if (/^(rest|r)$/i.test(s)) return { kind: "rest" };
  let m = s.match(/^([0-9]+(?:\.[0-9]+)?)%$/);
  if (m) {
    const p = Number(m[1]);
    return p > 0 && p <= 100 ? { kind: "percent", value: p } : { error: "a percentage is above 0 and at most 100" };
  }
  m = s.match(/^([0-9]+(?:\.[0-9]+)?)([kmgtpe]i?)?(b)?$/i);
  if (!m) return { error: "write e.g. 500MB, 20GB, 25% or rest" };
  if (!m[2] && !m[3]) return { error: `${s} has no unit: 500MB, 20GB…` };
  const unit = UNITS[(m[2] || "").toLowerCase()];
  const n = Number(m[1]) * unit;
  if (!(n > 0)) return { error: "zero: write rest for all that is left" };
  // Whole bytes, decided exactly as the core does (Fraction): digits × unit over 10^decimals.
  const [whole, frac = ""] = m[1].split(".");
  if ((BigInt(whole + frac) * BigInt(unit)) % 10n ** BigInt(frac.length) !== 0n) {
    return { error: `${s} is not a whole number of bytes` };
  }
  return { kind: "fixed", bytes: n };
}

function sketch(rows, capacity) {
  const parsed = rows.map((r) => parseSize(r.size));
  const bad = parsed.findIndex((p) => p.error);
  if (bad >= 0) return { error: `partition ${bad + 1}: ${parsed[bad].error}`, parsed };
  const sizes = parsed.map((p) => (p.kind === "fixed" ? p.bytes : p.kind === "percent" ? Math.floor((capacity * p.value) / 100) : null));
  const rests = sizes.filter((s) => s === null).length;
  if (rests > 1) return { error: "rest may appear only once", parsed };
  const taken = sizes.reduce((a, s) => a + (s || 0), 0);
  const left = capacity - taken;
  if (rests) {
    if (left <= 0) return { error: `rest has no room: the others take ${size(taken)} of ${size(capacity)}`, parsed };
    sizes[sizes.indexOf(null)] = left;
  } else if (left < 0) {
    return { error: `the partitions take ${size(taken)}, more than the ${size(capacity)} there is`, parsed };
  }
  return { sizes, free: rests ? 0 : left, parsed };
}

function minBytes(row) {
  const f = formatInfo(row.format);
  return Math.max(MIB, Math.ceil(((f && f.min_size) || 0) / MIB) * MIB);
}

// Move the boundary after row I to X bytes from the start; the row after it (or the free
// space after the last row) gives or takes. A rest row stays rest; any other becomes a
// whole number of MB, so what is dragged can be typed and repeated.
function moveBoundary(ed, i, x) {
  const sk = sketch(ed.rows, ed.capacity);
  if (sk.error) return;
  const last = i === ed.rows.length - 1;
  const start = sk.sizes.slice(0, i).reduce((a, b) => a + b, 0);
  const pair = sk.sizes[i] + (last ? sk.free : sk.sizes[i + 1]);
  const lo = minBytes(ed.rows[i]);
  const hi = pair - (last ? 0 : minBytes(ed.rows[i + 1]));
  if (hi < lo) return;
  const top = Math.floor(hi / MB) * MB;
  if (top < lo) return;
  const a = Math.min(Math.max(Math.round((x - start) / MB) * MB, Math.ceil(lo / MB) * MB), top);
  if (sk.parsed[i].kind !== "rest") ed.rows[i].size = mbWord(a);
  if (!last && sk.parsed[i + 1].kind !== "rest") ed.rows[i + 1].size = mbWord(Math.floor((pair - a) / MB) * MB);
}

// BYTES as the page writes a size: MB, so the field says in MB what the sketch and the map print
// in MB — whole MB as the page makes them, decimals only if BYTES has them (952.086528MB).
function mbWord(bytes) {
  const whole = Math.floor(bytes / MB);
  const frac = String(bytes - whole * MB).padStart(6, "0").replace(/0+$/, "");
  return `${whole}${frac ? `.${frac}` : ""}MB`;
}

// Move the end of the partition being resized to X bytes from its start: its new size, in
// whole MB, from diskutil's minimum. With rows after it, less than it is now, leaving the rows
// the room their fixed sizes take (and a MiB for rest). Without, up to limits.maximum, the
// core's edge (T-074, D48), rounded down to a whole MB too: the less than a MB beyond it is
// not worth a field of decimals (Vitali, D49); it can still be typed.
function moveShrink(ed, x) {
  const disk = diskByName(ed.disk);
  const g = disk && ed.limits ? geometry(ed, disk) : null;
  if (!g || !g.part) return;
  const parsed = ed.rows.map((r) => parseSize(r.size));
  const need = parsed.reduce((a, p) => a + (p.kind === "fixed" ? p.bytes : p.kind === "rest" ? MIB : 0), 0);
  const lo = Math.max(MB, Math.ceil(ed.limits.minimum / MB) * MB);
  const max = ed.rows.length ? 0 : ed.limits.maximum;
  const top = max > g.part.bytes ? max : Math.min(g.part.bytes - 1, g.span - need);
  const hi = Math.floor(top / MB) * MB;
  if (hi < lo) return;
  ed.draft.to = mbWord(Math.min(Math.max(Math.round(x / MB) * MB, lo), hi));
}

// What the field holds when a resize is opened: halfway between diskutil's minimum and the
// size now, in whole MB; empty when there is no whole MB to take.
function defaultTo(limits) {
  const lo = Math.max(MB, Math.ceil(limits.minimum / MB) * MB);
  const hi = Math.floor((limits.current - 1) / MB) * MB;
  if (hi < lo) return "";
  return mbWord(Math.max(lo, Math.floor((lo + hi) / 2 / MB) * MB));
}

// A draft from an earlier opening that now asks for no change — the size was applied — or no
// size at all: the field starts afresh (Vitali, 2026-10-30: reopened after a grow, it said
// "already" and drew nothing).
function staleDraft(to, limits) {
  const t = parseSize(to || "");
  return !to || (t.kind === "fixed" && Math.ceil(t.bytes / KIB4) * KIB4 === limits.current);
}

// --- opening and drafts --------------------------------------------------------------------

// AFTER: add only — the partition before the gap to fill (T-076); null: the tail.
function openEditor(disk, kind, after = null) {
  const key = `${disk.device}:${kind}${after ? `:${after}` : ""}`;
  const rows = state.drafts[key] || [{ format: defaultFormat(), label: "", size: "rest" }];
  state.drafts[key] = rows;
  state.editor = { key, disk: disk.device, kind, after, capacity: capacityOf(disk, kind, after), rows, plan: null,
    error: null, busy: false, rev: 0 };
  state.selected = disk.device;
  state.extent = null;
  render();
  renderEditor();
}

// Resize partition E of DISK (T-069, T-074). Nothing is offered before diskutil's resize limits
// are in: the least and the largest size are theirs, and a partition that cannot be resized is
// refused in the planner's words. No rows at first: shrinking alone is the plain case.
function openShrink(disk, e) {
  const key = `${e.device}:shrink`;
  const draft = state.drafts[key] || { to: "", rows: [] };
  state.drafts[key] = draft;
  state.editor = { key, disk: disk.device, kind: "shrink", slice: e.device, draft, rows: draft.rows, limits: null,
    limitsError: null, capacity: 0, plan: null, error: null, busy: false, rev: 0 };
  state.selected = disk.device;
  state.extent = null;
  render();
  renderEditor();
  readLimits(state.editor);
}

async function readLimits(ed) {
  ed.limits = null;
  ed.limitsError = null;
  let limits = null, error = null;
  try {
    limits = await api(`/api/limits?slice=${encodeURIComponent(ed.slice)}`);
  } catch (err) {
    error = err.message;
  }
  if (state.editor !== ed) return;
  ed.limits = limits;
  ed.limitsError = error;
  if (limits && staleDraft(ed.draft.to, limits)) ed.draft.to = defaultTo(limits);
  renderEditor();
}

function invalidate(ed) {
  ed.plan = null;
  ed.error = null;
  ed.rev += 1; // a plan still on its way was built from other rows: dropped when it lands
}

// --- the panel -------------------------------------------------------------------------------

function renderEditor() {
  const ed = state.editor;
  const disk = diskByName(ed.disk);
  if (!disk) {
    showDetail(el("h3", {}, ed.disk), el("p", { class: "error" }, `${ed.disk} is no longer attached.`));
    return;
  }
  const parts = [el("h3", {}, KIND_TITLE[ed.kind](ed))];
  if (ed.kind === "shrink") {
    const g = geometry(ed, disk);
    if (g.gone) parts.push(el("p", { class: "error" }, g.error));
    else if (ed.limitsError) parts.push(el("p", { class: "error" }, ed.limitsError));
    else if (!ed.limits) parts.push(loading("Asking diskutil how far it can be resized…"));
    if (g.gone || ed.limitsError || !ed.limits) {
      showDetail(...parts);
      return;
    }
    ed.capacity = g.capacity;
    parts.push(...shrinkHead(ed, g));
  } else if (ed.kind === "repartition") {
    ed.capacity = capacityOf(disk, ed.kind);
    const lost = (disk.extents || []).filter((e) => e.kind === "partition");
    parts.push(el("div", { class: "danger" },
      el("strong", {}, `Everything on ${ed.disk} is destroyed.`),
      lost.length ? el("ul", {}, lost.map((e) => el("li", {}, `${e.device} — ${extentName(e)} (${size(e.bytes)})`))) : null));
  } else if (ed.after) {
    ed.capacity = capacityOf(disk, ed.kind, ed.after);
    const g = gapAfter(disk, ed.after);
    parts.push(g
      ? el("p", { class: "note" }, `Into the ${size(ed.capacity)} free between ${ed.after} and ${g.next.device}. `
        + `The partitions there now keep their places and data; ${g.next.device} and those after it take new names `
        + "— the plan says which. Every volume of the disk is unmounted meanwhile.")
      : el("p", { class: "error" }, `There is no free space between ${ed.after} and the next partition any more.`));
  } else {
    ed.capacity = capacityOf(disk, ed.kind);
    parts.push(el("p", { class: "note" }, `Into the ${size(ed.capacity)} free after the last partition. `
      + "The partitions there now are kept as they are."));
  }
  const g = geometry(ed, disk);
  const rowsAllowed = ed.kind !== "shrink" || g.last && !g.grow;
  const sketchBox = el("div", { class: "sketch" });
  parts.push(el("div", { class: "label-row" }, el("span", { class: "muted" }, "Sketch — approximate; the plan has the exact sizes"),
    el("span", { class: "muted" }, `${size(g.span)}`)), sketchBox, ed.rows.length ? rowsTable(ed) : null,
    el("div", { class: "actions" },
      rowsAllowed ? el("button", { type: "button", onclick: () => { ed.rows.push({ format: defaultFormat(), label: "", size: ed.rows.some((r) => parseSize(r.size).kind === "rest") ? "1GB" : "rest" }); invalidate(ed); renderEditor(); } }, "+ Partition") : null,
      el("button", { type: "button", class: "primary", id: "show-plan", onclick: () => showPlanFor(ed) }, "Show plan")),
    el("div", { id: "editor-live" }));
  showDetail(...parts);
  const svg = sketchSvg(ed);
  sketchBox.append(svg);
  fillSketch(svg, ed);
  refreshLive(ed);
}

// What the resize editor says above the sketch: the partition, its limits, the new size.
function shrinkHead(ed, g) {
  const lim = ed.limits;
  const grows = lim.maximum > lim.current
    ? `no more than ${size(lim.maximum)} — the free space right after it.`
    : "it cannot grow: no free space right after it.";
  const out = [
    el("p", { class: "note" }, `${ed.slice} (${extentName(g.part)}) is resized in place and keeps its data: `
      + `${size(lim.current)} now, no less than ${size(lim.minimum)} — the space its files take, as diskutil says; ${grows}`),
    el("div", { class: "field" }, el("label", { for: "shrink-to" }, "New size"),
      el("input", { id: "shrink-to", class: "to", type: "text", value: ed.draft.to, placeholder: "e.g. 900MB or 20GB",
        spellcheck: "false", oninput: (ev) => {
          ed.draft.to = ev.target.value;
          invalidate(ed);
          fillSketch(document.querySelector("svg.editor-bar"), ed);
          refreshLive(ed);
        } })),
  ];
  if (g.last) {
    out.push(el("p", { class: "note" }, "Shrunk, it can have new partitions after it, in the space freed and the free space after it; "
      + "grown, none."));
  } else {
    out.push(el("p", { class: "note" }, `${ed.slice} is not the last partition: it can only be resized alone. `
      + `The space freed stays free between it and ${g.next.device}; a partition put there would renumber the ones after it.`));
  }
  return out;
}

function rowsTable(ed) {
  const offered = state.formats ? state.formats.formats : [];
  const body = ed.rows.map((r, i) => {
    const select = el("select", { "aria-label": `Partition ${i + 1} format`,
      onchange: (ev) => { r.format = ev.target.value; invalidate(ed); renderEditor(); } },
    el("optgroup", { label: "Formats" }, offered.filter((f) => f.plannable).map((f) => el("option", { value: f.format }, optionText(f)))),
    el("optgroup", { label: "Not planned on this Mac" }, offered.filter((f) => !f.plannable).map((f) => el("option", { value: f.format, disabled: true, title: f.why }, optionText(f)))));
    if (!offered.some((f) => f.format === r.format)) select.prepend(el("option", { value: r.format }, r.format));
    select.value = r.format;
    return el("tr", {},
      el("td", { class: "muted" }, String(i + 1)),
      el("td", {}, select),
      el("td", {}, el("input", { class: "label" + (missingLabel(r) ? " missing" : ""), type: "text", value: r.label,
        placeholder: { required: "required", none: r.format === "efi" ? "EFI" : "none" }[labelRule(r.format)] || "no label",
        disabled: labelRule(r.format) === "none", "aria-label": `Partition ${i + 1} label`, spellcheck: "false",
        oninput: (ev) => {
          r.label = ev.target.value;
          ev.target.classList.toggle("missing", missingLabel(r));
          invalidate(ed);
          refreshLive(ed);
        } })),
      el("td", {}, el("input", { class: "size", type: "text", value: r.size, placeholder: "500MB · 20% · rest",
        "aria-label": `Partition ${i + 1} size`, spellcheck: "false",
        oninput: (ev) => { r.size = ev.target.value; invalidate(ed); fillSketch(document.querySelector("svg.editor-bar"), ed); refreshLive(ed); } })),
      el("td", { class: "approx num muted" }), // filled by fillSketch: what rest and 20% come to
      el("td", { class: "row-buttons" },
        el("button", { type: "button", class: "icon", title: "Move up", "aria-label": `Move partition ${i + 1} up`,
          disabled: i === 0, onclick: () => swapRows(ed, i - 1) }, "↑"),
        el("button", { type: "button", class: "icon", title: "Move down", "aria-label": `Move partition ${i + 1} down`,
          disabled: i === ed.rows.length - 1, onclick: () => swapRows(ed, i) }, "↓"),
        el("button", { type: "button", class: "icon", title: "Remove", "aria-label": `Remove partition ${i + 1}`,
          disabled: ed.rows.length === 1 && ed.kind !== "shrink", onclick: () => { ed.rows.splice(i, 1); invalidate(ed); renderEditor(); } }, "✕")));
  });
  return el("table", { class: "rows" },
    el("thead", {}, el("tr", {}, ["#", "Format", "Label", "Size", "≈", ""].map((h) => el("th", { class: h === "≈" ? "num" : null }, h)))),
    el("tbody", {}, body));
}

// Rows I and I+1 change places, in the draft too (ed.rows is the draft's array). The core
// judges the new order when the plan is asked for; an EFI row belongs first (repartition notes).
function swapRows(ed, i) {
  [ed.rows[i], ed.rows[i + 1]] = [ed.rows[i + 1], ed.rows[i]];
  invalidate(ed);
  renderEditor();
}

function optionText(f) {
  const name = f.name && f.name !== f.format ? `${f.name} — ${f.format}` : f.format;
  const m = f.mount;
  const how = !m ? "" : m.mode === "rw" ? "" : m.mode === "ro" ? " · read-only here" : " · does not mount here";
  return name + how;
}

function sketchSvg(ed) {
  const svg = el("svg:svg", { class: "bar editor-bar", role: "img", "aria-label": "Sketch of the new partitions" });
  let drag = null;
  svg.addEventListener("pointerdown", (ev) => {
    const h = ev.target.closest(".handle");
    if (!h) return;
    drag = Number(h.dataset.i);
    svg.setPointerCapture(ev.pointerId);
    ev.preventDefault();
  });
  svg.addEventListener("pointermove", (ev) => {
    if (drag === null) return;
    const r = svg.getBoundingClientRect();
    const g = geometry(ed, diskByName(ed.disk));
    if (g.error) return;
    const x = ((ev.clientX - r.left) / r.width) * g.span;
    if (drag < 0) moveShrink(ed, x);
    else moveBoundary(ed, drag, x - g.base);
    fillSketch(svg, ed);
    document.querySelectorAll("#detail input.size").forEach((input, k) => { input.value = ed.rows[k].size; });
    const to = document.getElementById("shrink-to");
    if (to) to.value = ed.draft.to;
  });
  const end = () => {
    if (drag === null) return;
    drag = null;
    invalidate(ed);
    refreshLive(ed);
  };
  svg.addEventListener("pointerup", end);
  svg.addEventListener("pointercancel", end);
  return svg;
}

function fillSketch(svg, ed) {
  if (!svg) return;
  const W = Math.max(200, Math.round(svg.getBoundingClientRect().width) || 480);
  svg.setAttribute("viewBox", `0 0 ${W} ${BAR_H}`);
  svg.replaceChildren();
  const disk = diskByName(ed.disk);
  const g = disk ? geometry(ed, disk) : { error: `${ed.disk} is no longer attached.` };
  if (!g.error) ed.capacity = g.capacity;
  const sk = g.error ? { error: g.error } : sketch(ed.rows, ed.capacity);
  document.querySelectorAll("#detail td.approx").forEach((td, k) => {
    td.textContent = sk.error || ed.capacity <= 0 ? "" : `≈ ${size(sk.sizes[k])}`;
  });
  if (!g.error && g.span <= 0) {
    svg.append(el("svg:text", { x: 6, y: 27, class: "dark" }, `No free space after ${ed.after || "the last partition"}.`));
    return;
  }
  if (sk.error) {
    svg.append(el("svg:rect", { class: "f-free", x: 0.5, y: 0.5, width: W - 1, height: BAR_H - 1, rx: 4 }),
      el("svg:text", { x: 8, y: 27, class: "dark" }, sk.error));
    return;
  }
  let x = 0;
  const edges = [];
  if (ed.kind === "shrink") { // the partition itself, at its new size; its end is dragged too
    const w = (g.base / g.span) * W;
    const kept = el("svg:g", {});
    kept.append(el("svg:title", {}, `${ed.slice} ${extentName(g.part)} ≈ ${size(g.base)}, data kept${g.grow ? ", grown" : ""}`));
    kept.append(el("svg:rect", { class: `body f-${family(g.part)}`, x: 0.5, y: 0.5, width: Math.max(1, w - 1), height: BAR_H - 1, rx: 4 }));
    segmentText(kept, 0, w, `${ed.slice.replace(/^disk\d+/, "")} ${extentName(g.part)}`, `≈ ${size(g.base)}`, null);
    svg.append(kept);
    x = w;
    const h = el("svg:rect", { class: "handle", x: Math.min(Math.max(w - 5, 0), W - 10), y: 0, width: 10, height: BAR_H });
    h.dataset.i = "-1";
    h.append(el("svg:title", {}, "Drag to set the new size"));
    svg.append(h);
  }
  sk.sizes.forEach((b, i) => {
    const w = (b / g.span) * W;
    const r = ed.rows[i];
    const seg = el("svg:g", {});
    seg.append(el("svg:title", {}, `${i + 1}: ${r.format} ${r.label ? `"${r.label}" ` : ""}≈ ${size(b)}`));
    seg.append(el("svg:rect", { class: `body f-${formatFamily(r.format)}`, x: x + 0.5, y: 0.5, width: Math.max(1, w - 1), height: BAR_H - 1, rx: 4 }));
    segmentText(seg, x, w, `${i + 1} ${r.label || r.format}`, `≈ ${size(b)}`, null);
    svg.append(seg);
    x += w;
    edges.push(x);
  });
  if (sk.free > 0) {
    const w = (sk.free / g.span) * W;
    svg.append(el("svg:rect", { class: "f-free", x: x + 0.5, y: 0.5, width: Math.max(1, w - 1), height: BAR_H - 1, rx: 4 }));
    segmentText(svg, x, w, "free", `≈ ${size(sk.free)}`, "dark");
  }
  // A boundary between two rows can be dragged; so can the end of the last row unless it is rest.
  edges.forEach((ex, i) => {
    const last = i === ed.rows.length - 1;
    if (last && sk.parsed[i].kind === "rest") return;
    const h = el("svg:rect", { class: "handle", x: Math.min(Math.max(ex - 5, 0), W - 10), y: 0, width: 10, height: BAR_H });
    h.dataset.i = String(i);
    h.append(el("svg:title", {}, "Drag to resize"));
    svg.append(h);
  });
}

// --- what depends on the rows but not on the inputs: notes, the plan ------------------------

function refreshLive(ed) {
  const box = document.getElementById("editor-live");
  if (!box || state.editor !== ed) return;
  const btn = document.getElementById("show-plan");
  if (btn) btn.disabled = ed.busy;
  const out = [];
  const trial = state.formats && state.formats.trial;
  ed.rows.forEach((r, i) => {
    if (missingLabel(r)) {
      out.push(el("p", { class: "warn" }, `Partition ${i + 1} (${r.format}) needs a label: diskutil formats no volume without a name.`));
    }
  });
  ed.rows.forEach((r, i) => {
    const m = (formatInfo(r.format) || {}).mount;
    if (m && m.mode !== "rw") {
      out.push(el("p", { class: "warn" }, `Partition ${i + 1} (${r.format}): the last trial mount on this Mac `
        + (m.mode === "ro" ? `mounted it read-only (${m.by}).` : `could not mount it: ${m.detail}`)));
    }
  });
  if (ed.plan) {
    // the plan says how this Mac mounts what it makes, under MOUNTING ON THIS MAC
  } else if (trial && !trial.at) {
    out.push(el("p", { class: "note" }, "No trial mount yet: diskmap doctor --trial finds how this Mac mounts each format."));
  } else if (trial && !trial.holds) {
    out.push(el("p", { class: "note" }, `The last trial mount was on another setup (${trial.changed.join("; ")}).`));
  }
  if (ed.busy) out.push(loading("Building the plan…"));
  if (ed.error) out.push(el("p", { class: "error" }, ed.error));
  if (ed.plan) out.push(...planView(ed.plan, ed));
  box.replaceChildren(...out);
}

async function showPlanFor(ed) {
  const rev = ed.rev;
  const disk = diskByName(ed.disk);
  const g = ed.kind === "shrink" && disk ? geometry(ed, disk) : {};
  if (g.same) {
    ed.error = `${ed.slice} is ${size(g.part.bytes)} already: drag its end or give a smaller or a larger size.`;
    refreshLive(ed);
    return;
  }
  ed.busy = true;
  ed.error = null;
  refreshLive(ed);
  const layout = {
    partitions: ed.rows.map((r) => {
      const p = { format: r.format, size: r.size.trim() };
      if (r.label && labelRule(r.format) !== "none") p.label = r.label;
      return p;
    }),
  };
  const request = ed.kind !== "shrink" ? { kind: ed.kind, disk: ed.disk, layout, ...(ed.after ? { after: ed.after } : {}) }
    : resizeRequest(ed, layout);
  try {
    const plan = await api("/api/plan", request);
    if (ed.rev === rev) {
      ed.plan = plan;
      ed.planRequest = request; // what Apply… hands over: the request this plan was built from
    }
  } catch (err) {
    if (ed.rev === rev) ed.error = err.message;
  }
  ed.busy = false;
  refreshLive(ed);
}

// A resize asks for a shrink or a grow, as the new size is below or above the size now (T-074);
// a grow carries no layout. Which one, the sketch's geometry says — the same rounding as the core.
function resizeRequest(ed, layout) {
  const disk = diskByName(ed.disk);
  const g = disk ? geometry(ed, disk) : {};
  const to = ed.draft.to.trim();
  if (g.grow) return { kind: "grow", disk: ed.slice, to };
  return { kind: "shrink", disk: ed.slice, to, ...(ed.rows.length ? { layout } : {}) };
}

function planView(plan, ed) {
  const lost = plan.steps.flatMap((s) => s.destroys);
  const out = [el("h4", {}, "After the plan")];
  // First, as in the preview: this macOS is not one diskmap was tested on (K19).
  if (plan.untested) out.push(el("div", { class: "warnbox untested" }, el("strong", {}, "Not tested on this macOS"), el("p", {}, plan.untested)));
  out.push(afterBar(plan, ed), afterTable(plan));
  if (lost.length) {
    out.push(el("div", { class: "danger" }, el("strong", {}, "Data lost"),
      el("ul", {}, lost.map((x) => el("li", {}, `${x.device} — ${x.what}${x.used !== null ? ` · ${size(x.used)} used` : ""}`
        + (x.certain ? "" : " · diskutil cannot tell what is here — assume data"))))));
  } else {
    out.push(el("p", { class: "note" }, "No volume loses its data."));
  }
  if (plan.mounting.length) out.push(el("div", { class: "warnbox" }, el("strong", {}, "Mounting on this Mac"), el("ul", {}, plan.mounting.map((t) => el("li", {}, t)))));
  if (plan.notes.length) out.push(el("ul", { class: "notes" }, plan.notes.map((t) => el("li", {}, t))));
  out.push(el("p", { class: "note" }, "Nothing has been applied. The preview below is what diskmap prints in a terminal."));
  out.push(handoffBox(ed.planRequest, plan));
  out.push(el("details", {}, el("summary", {}, `Preview — ${plan.steps.length} steps${plan.needs_root ? ", some under sudo" : ""}`),
    el("pre", { class: "preview" }, plan.preview)));
  return out;
}

function afterExtents(plan) {
  const bs = plan.block_size;
  const parts = [...plan.after].sort((a, b) => a.start_lba - b.start_lba);
  const out = [];
  let at = parts.length ? Math.min(parts[0].start_lba, 34) : 34;
  for (const p of parts) {
    if ((p.start_lba - at) * bs >= SLACK) out.push({ role: "free", bytes: (p.start_lba - at) * bs });
    out.push(p);
    at = p.start_lba + p.blocks;
  }
  const end = plan.blocks - 33; // the backup GPT at the end
  if ((end - at) * bs >= SLACK) out.push({ role: "free", bytes: (end - at) * bs });
  return out;
}

function afterBar(plan, ed) {
  const extents = afterExtents(plan);
  const box = document.getElementById("detail");
  const W = Math.max(240, (box ? box.clientWidth : 520) - 40);
  const svg = el("svg:svg", { class: "bar", viewBox: `0 0 ${W} ${BAR_H}`, role: "img", "aria-label": "Partition map after the plan" });
  const px = allocate(extents.map((e) => e.bytes), W);
  if (px === null) return svg;
  let x = 0;
  extents.forEach((e, i) => {
    const w = px[i];
    const fam = e.role === "new" ? formatFamily(e.format) : e.role === "free" ? "free" : e.role === "added" ? "added" : "kept";
    const g = el("svg:g", {});
    g.append(el("svg:title", {}, `${e.device || e.role} · ${size(e.bytes)} · ${e.what || ""}`));
    g.append(el("svg:rect", { class: `body f-${fam}`, x: x + 0.5, y: 0.5, width: Math.max(1, w - 1), height: BAR_H - 1, rx: 4 }));
    segmentText(g, x, w, e.role === "free" ? "free" : `${(e.device || "").replace(/^disk\d+/, "") || "+"} ${e.label || e.what || ""}`,
      size(e.bytes), fam === "free" || fam === "kept" || fam === "added" ? "dark" : null);
    svg.append(g);
    x += w;
  });
  return svg;
}

function afterTable(plan) {
  const role = { new: "new", kept: "kept", restored: "from backup", added: "added by diskutil" };
  const what = (e) => (e.role === "resized" ? `${e.bytes > e.was_bytes ? "grown" : "shrunk"}, data kept`
    : e.was ? `kept, was ${e.was}` : role[e.role] || e.role); // renumbered by a partition added before it (D50)
  return el("table", {},
    el("thead", {}, el("tr", {}, ["Device", "Size", "", "What"].map((h) => el("th", { class: h === "Size" ? "num" : null }, h)))),
    el("tbody", {}, [...plan.after].sort((a, b) => a.start_lba - b.start_lba).map((e) => el("tr", {},
      el("td", {}, e.device || (e.entry ? `entry ${e.entry}` : "—")),
      el("td", { class: "num", title: `${e.bytes} bytes` }, size(e.bytes)),
      el("td", { class: "muted" }, what(e)),
      el("td", {}, e.what || e.type_name || e.type)))));
}
