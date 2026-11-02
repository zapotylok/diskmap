// diskmap ui (T-061, K14): the map `diskmap show --json` gives, drawn; plans as
// `diskmap plan --json` gives them. Read-only: nothing here applies a plan.
// Every string from the disks (labels above all) goes into the page as text, never as HTML.
"use strict";

const TOKEN = document.querySelector('meta[name="diskmap-token"]').content;
const SVG = "http://www.w3.org/2000/svg";
const MIN_PX = 8; // the narrowest an extent is drawn, so a 1 MiB partition can still be clicked
const BAR_H = 44;
const SLACK = 1 << 20; // a free extent below this is alignment slack: counted, not drawn (as `diskmap show`)
const HFS_TYPE = "48465300-0000-11AA-AA11-00306543ECAC"; // Apple_HFS (core/model/gpt_types.py)

// selected: the one disk on the screen (T-063). byHand: the person picked it in the header; else it is
// the default, picked again on every read of the map, so a disk attached since shows up first.
const state = { map: null, formats: null, selected: null, byHand: false, extent: null, editor: null, drafts: {} };

async function api(path, body) {
  const init = { headers: { "X-Diskmap-Token": TOKEN } };
  if (body !== undefined) {
    init.method = "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const r = await fetch(path, init);
  const doc = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok) throw new Error(doc.error || `HTTP ${r.status}`);
  return doc;
}

function el(tag, attrs = {}, ...children) {
  const e = tag.startsWith("svg:") ? document.createElementNS(SVG, tag.slice(4)) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") e.setAttribute("class", v);
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return e;
}

// Sizes as `diskmap show` prints them (cli/show.py size): decimal units, as diskutil does, one
// decimal, a no-break space. The same steps on the same doubles; a tie at the decimal goes to
// even, as Python's format does (toFixed would go up).
const NBSP = " ";

function size(n) {
  if (n < 1000) return `${n}${NBSP}B`;
  let v = n, unit;
  for (unit of ["KB", "MB", "GB", "TB"]) {
    v /= 1000;
    if (v < 999.95) break;
  }
  let text = v.toFixed(1);
  if (Number.isInteger(v * 4) && !Number.isInteger(v * 2)) { // exactly .25 or .75: a tie
    const tenths = Math.floor(v * 10);
    text = ((tenths + (tenths % 2)) / 10).toFixed(1);
  }
  return `${text}${NBSP}${unit}`;
}

function family(e) {
  if (e.kind === "free") return "free";
  if (e.apfs) return "apfs";
  const fs = (e.volume && e.volume.filesystem) || "";
  const t = (e.type_name || "").toLowerCase();
  if (fs === "apfs" || t.includes("apfs")) return "apfs";
  if (fs.startsWith("hfs") || t.includes("hfs")) return "hfs";
  if (fs === "exfat" || fs === "msdos" || t.includes("efi")) return "fat";
  if (fs.includes("ntfs") || fs === "ufsd_ntfs") return "ntfs";
  if (fs.includes("ext") || t.includes("linux")) return "ext";
  return "other";
}

// Widths in pixels for weights, each at least MIN_PX, summing to total (or null: no room).
function allocate(weights, total) {
  if (weights.length * MIN_PX > total) return null;
  const sum = weights.reduce((a, b) => a + b, 0) || 1;
  let px = weights.map((w) => Math.max(MIN_PX, (w / sum) * total));
  const over = px.reduce((a, b) => a + b, 0) - total;
  if (over > 0) {
    const room = px.map((p) => p - MIN_PX);
    const roomSum = room.reduce((a, b) => a + b, 0) || 1;
    px = px.map((p, i) => p - (room[i] / roomSum) * over);
  }
  return px;
}

// The text inside a segment W pixels wide at X: a name, and its size on a second line, so a
// segment too narrow for both still says how big it is (T-064). Each line is cut to fit.
function segmentText(g, x, w, name, bytes, cls) {
  if (w <= 40) return;
  const chars = Math.floor((w - 10) / 6.5);
  const fit = (t) => (t.length > chars ? t.slice(0, Math.max(1, chars - 1)) + "…" : t);
  g.append(el("svg:text", { x: x + 6, y: 18, class: cls }, fit(name)),
    el("svg:text", { x: x + 6, y: 33, class: cls }, fit(bytes)));
}

// A paragraph with a spinner: something is being read or built (T-064).
function loading(text) {
  return el("p", { class: "loading" }, el("span", { class: "spinner", "aria-hidden": "true" }), text);
}

function extentName(e) {
  if (e.kind === "free") return "free";
  const v = e.volume;
  if (v && v.label) return v.label;
  if (e.apfs && e.apfs.volumes.length) return e.apfs.volumes.map((x) => x.name).join(", ");
  return e.type_name || e.type;
}

function shown(disk) {
  return (disk.extents || []).filter((e) => e.kind !== "free" || e.bytes >= SLACK);
}

function bar(disk, width, protectedDisk) {
  const extents = shown(disk);
  const svg = el("svg:svg", { class: "bar", viewBox: `0 0 ${width} ${BAR_H}`, role: "img",
    "aria-label": `Partition map of ${disk.device}` });
  const defs = el("svg:defs");
  const pat = el("svg:pattern", { id: `hatch-${disk.device}`, width: 6, height: 6, patternUnits: "userSpaceOnUse",
    patternTransform: "rotate(45)" });
  pat.append(el("svg:rect", { class: "f-free", width: 6, height: 6 }),
    el("svg:line", { class: "hatch", x1: 0, y1: 0, x2: 0, y2: 6, "stroke-width": 2 }));
  defs.append(pat);
  svg.append(defs);
  const px = allocate(extents.map((e) => e.bytes), width);
  if (px === null) {
    svg.append(el("svg:text", { x: 4, y: 26, class: "dark" }, "Too many partitions to draw at this width."));
    return svg;
  }
  let x = 0;
  extents.forEach((e, i) => {
    const w = px[i];
    const fam = family(e);
    const g = el("svg:g", { class: "ext" + (state.extent === key(disk, e) ? " on" : ""), tabindex: 0,
      onclick: () => pick(disk, e) });
    g.append(el("svg:title", {}, `${e.device || "free"} · ${size(e.bytes)} · ${extentName(e)}`));
    g.append(el("svg:rect", { class: fam === "free" ? "body" : `body f-${fam}`, x: x + 0.5, y: 0.5, width: Math.max(1, w - 1), height: BAR_H - 1,
      rx: 4, fill: fam === "free" ? `url(#hatch-${disk.device})` : null, opacity: protectedDisk ? 0.55 : 1 }));
    segmentText(g, x, w, e.kind === "free" ? "free" : `${e.device.replace(/^disk\d+/, "")} ${extentName(e)}`,
      size(e.bytes), fam === "free" ? "dark" : null);
    svg.append(g);
    x += w;
  });
  return svg;
}

function key(disk, e) {
  return `${disk.device}:${e.start_lba}`;
}

function table(disk) {
  const rows = shown(disk).map((e) => {
    const v = e.volume;
    const fs = e.kind === "free" ? "" : e.apfs ? `APFS container ${e.apfs.reference}` : (v && v.filesystem_name) || "—";
    return el("tr", { class: state.extent === key(disk, e) ? "on" : null, onclick: () => pick(disk, e) },
      el("td", {}, el("span", { class: `swatch b-${family(e)}` }), e.device || "free"),
      el("td", { class: "num" }, size(e.bytes)),
      el("td", {}, e.kind === "free" ? "" : e.type_name || e.type),
      el("td", {}, fs),
      el("td", {}, e.kind === "free" ? "" : extentName(e)),
      el("td", {}, (v && v.mount_point) || ""));
  });
  return el("table", {},
    el("thead", {}, el("tr", {}, ["Device", "Size", "Type", "File system", "Name", "Mounted at"].map((h) =>
      el("th", { class: h === "Size" ? "num" : null }, h)))),
    el("tbody", {}, rows));
}

function diskCard(disk, width) {
  const a = disk.access;
  const available = a && a.available;
  const card = el("article", { class: "disk" });
  const head = el("div", { class: "disk-head" },
    el("h2", {}, disk.device),
    disk.size !== undefined ? el("span", {}, size(disk.size)) : null,
    disk.scheme !== undefined ? el("span", { class: "muted" }, disk.scheme || "no partition map") : null,
    a && a.kind ? el("span", { class: "muted" }, a.kind) : null,
    el("span", { class: "badge " + (available ? "ok" : "locked") }, available ? "available" : "protected · read-only"));
  card.append(head);
  if (disk.error) {
    card.append(el("p", { class: "error" }, disk.error));
    return card;
  }
  if (a && !available && a.reasons.length) {
    card.append(el("ul", { class: "reasons" }, a.reasons.map((r) => el("li", {}, r.detail))));
  }
  if (a && a.warnings.length) {
    card.append(el("ul", { class: "warnings" }, a.warnings.map((w) => el("li", {}, w))));
  }
  if (disk.extents) {
    card.append(bar(disk, width, !available), table(disk));
  } else if (disk.apfs) {
    card.append(el("p", {}, `Whole-disk APFS container ${disk.apfs.reference}, no partition map`));
  } else if (disk.volume) {
    card.append(el("p", {}, `Whole-disk file system, no partition map: ${disk.volume.filesystem_name || "unknown"}`));
  }
  if (available) {
    const tail = disk.extents ? tailOf(disk) : null;
    card.append(el("div", { class: "actions" },
      disk.extents ? el("button", { type: "button", disabled: !tail, title: tail ? null : "No free space after the last partition",
        onclick: () => openEditor(disk, "add") }, "Add partitions…") : null,
      el("button", { type: "button", onclick: () => openEditor(disk, "repartition") }, "Repartition…"),
      disk.extents ? el("button", { type: "button", onclick: () => planBackup(disk) }, "Plan: back up the partition map") : null));
  }
  return card;
}

// The first disk diskmap may change, else the first there is.
function defaultDisk(disks) {
  const d = disks.find((x) => x.access && x.access.available) || disks[0];
  return d ? d.device : null;
}

// After every read of the map: keep a disk picked by hand while it is attached, pick the
// default otherwise. An open editor holds its disk as a pick by hand would.
function choose() {
  const disks = state.map.disks;
  const held = state.byHand || (state.editor && state.editor.disk === state.selected);
  if (!(held && disks.some((d) => d.device === state.selected))) {
    state.byHand = false;
    const next = defaultDisk(disks);
    if (next !== state.selected) closeDetail();
    state.selected = next;
  }
}

function pickerText(d) {
  const a = d.access;
  return [d.device, d.size !== undefined ? size(d.size) : null, a && a.kind,
    a && a.available ? "available" : "protected"].filter(Boolean).join(" · ");
}

function renderPicker() {
  const sel = document.getElementById("disk-pick");
  const disks = state.map ? state.map.disks : [];
  sel.replaceChildren(...disks.map((d) => el("option", { value: d.device }, pickerText(d))));
  sel.hidden = !disks.length;
  if (state.selected) sel.value = state.selected;
}

function render() {
  const box = document.getElementById("disks");
  if (!state.map) return; // still reading: what load() put there stays (the resize observer calls here at once)
  const width = Math.max(240, box.clientWidth - 34);
  box.replaceChildren();
  renderPicker();
  const disk = diskByName(state.selected);
  if (disk) box.append(diskCard(disk, width));
  if (state.map.hidden.length) {
    box.append(el("p", { class: "muted" }, `Not shown: ${state.map.hidden.map((h) => h.device).join(", ")}`));
  }
  if (!state.map.disks.length) box.append(el("p", { class: "muted" }, "No disks."));
}

function showDetail(...children) {
  const d = document.getElementById("detail");
  const opening = d.hidden;
  d.hidden = false;
  d.replaceChildren(...children.filter((c) => c !== null && c !== undefined && c !== false)); // as el() skips them
  if (opening) d.scrollIntoView({ block: "nearest", behavior: "smooth" }); // it opens under the map, maybe below the fold
}

function closeDetail() {
  state.editor = null;
  state.extent = null;
  const d = document.getElementById("detail");
  d.hidden = true;
  d.replaceChildren();
}

function pick(disk, e) {
  state.editor = null;
  state.selected = disk.device;
  state.extent = key(disk, e);
  render();
  const v = e.volume;
  const facts = [
    ["Device", e.device || "free space"],
    ["Start LBA", e.start_lba],
    ["Blocks", `${e.blocks} × ${disk.block_size} B`],
    ["Size", `${size(e.bytes)} (${e.bytes} bytes)`],
  ];
  if (e.kind !== "free") {
    facts.push(["Type", `${e.type_name || "unknown"} — ${e.type}`]);
    if (v) {
      facts.push(["File system", v.filesystem_name || "none recognized"], ["Label", v.label || "—"],
        ["Mounted at", v.mount_point || "not mounted"]);
      if (v.mount_point) facts.push(["Writable", v.writable ? "yes" : "no"]);
    }
    if (e.apfs) {
      facts.push(["APFS container", `${e.apfs.reference}, ${size(e.apfs.capacity_free)} free`]);
      for (const x of e.apfs.volumes) facts.push([`  ${x.bsd_name}`, `${x.name} (${x.roles.join(", ") || "no role"})`]);
    }
  }
  // A partition of a disk diskmap may change can be resized — shrunk (T-069) or grown (T-074) — unless
  // diskutil is known not to (D26), which the page says at once (T-073); how far, the editor asks diskutil.
  const available = e.kind !== "free" && disk.access && disk.access.available;
  const blocker = available ? shrinkBlocker(e) : null;
  const verb = available ? mountVerb(e) : null;
  const fill = e.kind === "free" && disk.access && disk.access.available ? fillAfter(disk, e) : null;
  showDetail(el("h3", {}, e.device || "Free space"),
    el("table", {}, el("tbody", {}, facts.map(([k, val]) => el("tr", {}, el("td", { class: "muted" }, k), el("td", {}, val))))),
    available ? el("div", { class: "actions" },
      el("button", { type: "button", onclick: () => openShrink(disk, e), disabled: blocker !== null, title: blocker },
        "Resize…"),
      verb ? el("button", { type: "button", onclick: (ev) => toggleMount(disk, e, verb, ev.currentTarget) },
        verb === "mount" ? "Mount" : "Unmount") : null) : null,
    blocker ? el("p", { class: "note" }, blocker) : null,
    fill ? el("div", { class: "actions" },
      el("button", { type: "button", onclick: () => openEditor(disk, "add", fill.after) }, "Add partitions…")) : null);
}

// Free extent E of DISK as the add editor takes it: { after: null } for the tail, { after: the partition
// before it } for a gap between two (T-076, D50); null when nothing can go there — below SLACK, or no
// partition before it (addPartition follows one).
function fillAfter(disk, e) {
  if (e.bytes < SLACK) return null;
  const ex = disk.extents || [];
  const prev = ex.filter((x) => x.kind !== "free" && x.start_lba < e.start_lba).pop();
  if (!prev) return null;
  const t = tailOf(disk);
  if (t && t.start_lba === e.start_lba) return { after: null };
  const g = gapAfter(disk, prev.device);
  return g && g.free.start_lba === e.start_lba ? { after: prev.device } : null;
}

// Why diskutil will not resize partition E, as measured (D26): it resizes HFS+, and only while mounted,
// and APFS physical stores; null: it may, and its resize limits say how far. The server decides all the same.
function shrinkBlocker(e) {
  if (e.apfs) return null;
  const v = e.volume;
  if (e.type === HFS_TYPE && v && v.filesystem === "hfs") {
    return v.mount_point ? null : "Resize: diskutil gives HFS+ resize limits only while it is mounted — mount it first.";
  }
  const what = (v && v.filesystem_name) || e.type_name || "this partition";
  return `Resize: diskutil resizes only HFS+ and APFS in place, not ${what}.`;
}

// "mount" or "unmount" for partition E of a disk diskmap may change (K16), or null: an APFS store's
// volumes are the container's, and a partition with no file system has nothing to mount.
function mountVerb(e) {
  const v = e.volume;
  if (e.kind === "free" || e.apfs || !v || !v.filesystem) return null;
  return v.mount_point ? "unmount" : "mount";
}

// `diskmap mount|unmount` through the server (K16), then the map read again and the partition shown
// afresh — mounted or not is in the map, not guessed here. A refusal is shown under it.
async function toggleMount(disk, e, verb, btn) {
  btn.disabled = true;
  btn.replaceChildren(el("span", { class: "spinner", "aria-hidden": "true" }), verb === "mount" ? "Mounting…" : "Unmounting…");
  let failed = null;
  try {
    await api("/api/mount", { slice: e.device, mount: verb === "mount" });
  } catch (err) {
    failed = err.message;
  }
  await load();
  const now = diskByName(disk.device);
  const again = now && (now.extents || []).find((x) => x.start_lba === e.start_lba && x.device === e.device);
  if (again) pick(now, again);
  if (failed !== null) document.getElementById("detail").append(el("p", { class: "error" }, failed));
}

async function planBackup(disk) {
  state.editor = null;
  state.selected = disk.device;
  state.extent = null;
  render();
  showDetail(el("h3", {}, `Plan for ${disk.device}`), loading("Building the plan…"));
  try {
    const plan = await api("/api/plan", { kind: "backup-map", disk: disk.device });
    showPlan(plan);
  } catch (err) {
    showDetail(el("h3", {}, `Plan for ${disk.device}`), el("p", { class: "error" }, err.message));
  }
}

function showPlan(plan) {
  showDetail(
    el("h3", {}, `Plan for ${plan.disk}`),
    el("p", { class: "note" }, "Nothing has been applied. The plan below is exactly what diskmap prints in a terminal."),
    plan.needs_root ? el("p", { class: "note" }, "Needs root: some steps run under sudo.") : null,
    handoffBox({ kind: "backup-map", disk: plan.disk }, plan),
    el("details", {}, el("summary", {}, `Preview — ${plan.steps.length} steps${plan.needs_root ? ", some under sudo" : ""}`),
      el("pre", { class: "preview" }, plan.preview)));
}

// T-062, T-065: applying is `sudo diskmap apply` in a Terminal window, never here. "Apply…"
// opens a dialog that says what the terminal will ask; "Start in Terminal" has the server
// write the layout file and a script and ask Terminal to open it. There the plan is built
// again, shown, and run only once the disk's name (and a destroyed volume's label) is typed:
// the dialog confirms nothing. Once Terminal has opened, the dialog closes and the header
// waits for the script's exit status, then reads the disks again (watchTerminal). "Copy
// command" writes the same files, opens nothing and shows the command to run by hand.
function handoffBox(request, plan) {
  const lossy = plan.steps.some((s) => s.destroys.length);
  return el("div", { class: "handoff" },
    el("button", { type: "button", class: "primary", onclick: () => applyDialog(request, plan.disk, lossy) }, "Apply…"),
    el("span", { class: "note" }, "in a Terminal window, with sudo"));
}

const POLL_MS = 2000;
// As `diskmap apply` says it before it asks (cli/apply.py BACKUP_FIRST): the map is saved, the files are not (K11).
const BACKUP_FIRST = "Back up the files on this drive first: diskmap saves only the partition map, not the files on it.";

// DISK: the whole disk, the name `apply` asks for — for shrink and grow, request.disk is the partition.
function applyDialog(request, disk, lossy) {
  const dlg = el("dialog", { class: "apply", "aria-labelledby": "apply-title" });
  const body = el("div", {});
  dlg.addEventListener("close", () => dlg.remove());
  const close = (label = "Close") => el("button", { type: "button", onclick: () => dlg.close() }, label);
  const actions = (...buttons) => el("div", { class: "actions end" }, buttons);

  const handOver = async (path) => {
    body.replaceChildren(loading(path === "/api/terminal" ? "Opening Terminal…" : "Writing the files…"));
    let r;
    try {
      r = await api(path, request);
    } catch (err) {
      body.replaceChildren(el("p", { class: "error" }, err.message), actions(close()));
      return;
    }
    if (path === "/api/handoff" || !r.opened) {
      body.replaceChildren(
        path === "/api/handoff"
          ? el("p", {}, "Run this in a terminal. When it has finished, press Refresh.")
          : el("p", { class: "error" }, `${r.error}. Run this in a terminal instead; when it has finished, press Refresh.`),
        commandLine(r.command_line), actions(close()));
      return;
    }
    dlg.close(); // Terminal has the window now; the header says how it goes (watchTerminal)
    watchTerminal(r.id);
  };

  body.append(
    el("ol", { class: "steps" },
      el("li", {}, "Terminal opens a new window; sudo asks for your Mac password."),
      el("li", {}, "diskmap apply builds this plan again and shows it."),
      el("li", {}, `It runs only after you type the disk's name, "${disk}"`
        + (lossy ? ", and the label of a volume it destroys." : "."))),
    el("p", { class: "backup-first" }, BACKUP_FIRST),
    el("p", { class: "note" }, "Nothing is confirmed here: what the terminal shows is what runs."),
    actions(close("Cancel"),
      el("button", { type: "button", onclick: () => handOver("/api/handoff") }, "Copy command…"),
      el("button", { type: "button", class: "primary", onclick: () => handOver("/api/terminal") }, "Start in Terminal")));
  dlg.append(el("h3", { id: "apply-title" }, `Apply the plan for ${disk}`), body);
  document.body.append(dlg);
  dlg.showModal();
}

// While Terminal runs `apply`, the header says so and the page asks every POLL_MS how it
// ended; then it says that and reads the disks again. It lives in the header because the
// plan it came from is gone after that read: once the run wrote to the disk, the editor it
// came from — if it is still the one below the map — is closed, and its draft forgotten:
// what it drew is the map before. A run that wrote nothing leaves it, to be tried again.
// One at a time: a new handoff replaces it.
// The header holds only the outcome, in a box of fixed width; the rest is in a dialog it
// opens (T-068): what the journal says does not fit on one line.
const watch = { id: null, timer: null };

function watchTerminal(id, owner = state.editor) {
  clearTimeout(watch.timer);
  watch.id = id;
  terminalStatus({ kind: "wait", label: "Running in Terminal…" });
  const poll = async () => {
    let res;
    try {
      res = await api(`/api/terminal/result?id=${encodeURIComponent(id)}`);
    } catch (err) {
      if (watch.id === id) {
        terminalStatus({ kind: "bad", label: "Result unknown", rows: [],
          note: `Cannot learn how Terminal ended: ${err.message}. Press Refresh when it has.`, details: [], restore: [] });
      }
      return;
    }
    if (watch.id !== id) return; // dismissed or replaced meanwhile
    if (!res.done) {
      watch.timer = setTimeout(poll, POLL_MS);
      return;
    }
    terminalStatus(endedView(res));
    if (res.run && res.run.outcome !== "not applied" && owner && state.editor === owner) {
      delete state.drafts[owner.key];
      closeDetail();
    }
    load();
  };
  watch.timer = setTimeout(poll, POLL_MS);
}

// How apply ended, from the journal (T-068): the Terminal window may be closed already.
// KIND: good (applied), warn (the disk as it was: nothing ran, or no write started), bad
// (part of it written, or not known). RESTORE only when part of it was written: otherwise
// the map is the one before and there is nothing to put back.
const OUTCOMES = {
  "applied": ["good", "Applied", "Every step of the plan was done."],
  "not applied": ["warn", "Not applied",
    "No step that writes to the disk started: its partition map is the one before the run."],
  "partially applied": ["bad", "Partially applied",
    "A step that writes to the disk started and the plan did not finish: the map is somewhere between "
    + "before and after. The steps done say where; the map backup puts it back as it was before the run."],
};

function endedView(res) {
  const rows = [["Exit status", String(res.status)]];
  if (res.journal_error) {
    return { kind: "bad", label: "Result unknown", rows, details: [], restore: [],
      note: `What diskmap apply did is not known: ${res.journal_error}` };
  }
  if (!res.run) {
    return { kind: "warn", label: "Nothing was run", rows, details: [], restore: [],
      note: "diskmap apply ended before the plan began: it refused, was cancelled, or its window was closed "
        + "before the confirmation." };
  }
  const [kind, label, note] = OUTCOMES[res.run.outcome] ?? ["bad", res.run.outcome, ""];
  return {
    kind: kind === "good" && res.status !== 0 ? "warn" : kind,
    label, note, rows: [...rows, ["Run", res.run.id]], details: res.run.details, restore: res.run.restore,
    journal: true,
  };
}

// VIEW: endedView's, or { kind: "wait", label }. The label opens the dialog that says the
// rest; the ✕ stops waiting, or puts away what it said.
function terminalStatus(view) {
  const box = document.getElementById("terminal-status");
  const wait = view.kind === "wait";
  const dismiss = el("button", { type: "button", class: "icon", title: wait ? "Stop waiting" : "Dismiss",
    "aria-label": wait ? "Stop waiting" : "Dismiss", onclick: () => {
      clearTimeout(watch.timer);
      watch.id = null;
      box.hidden = true;
    } }, "✕");
  box.className = view.kind;
  box.replaceChildren(
    wait
      ? el("span", { class: "spinner", "aria-hidden": "true" })
      : el("button", { type: "button", class: "label", title: "Details", onclick: () => endedDialog(view) }, view.label),
    ...(wait ? [el("span", { class: "label" }, view.label)] : []),
    dismiss);
  box.hidden = false;
}

function endedDialog(view) {
  const dlg = el("dialog", { class: "apply ended", "aria-labelledby": "ended-title" },
    el("h3", { id: "ended-title", class: view.kind }, `diskmap apply: ${view.label}`),
    el("p", {}, view.note),
    el("dl", {}, view.rows.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)])),
    view.details.length ? el("ul", { class: "details" }, view.details.map((d) => el("li", {}, d))) : null,
    view.restore.length ? el("p", {}, "To put the map back as it was before the run:") : null,
    view.restore.map((c) => commandLine(c)),
    view.journal ? el("p", { class: "note" }, "diskmap journal shows every run in full.") : null,
    el("div", { class: "actions end" }, el("button", { type: "button", onclick: () => dlg.close() }, "Close")));
  dlg.addEventListener("close", () => dlg.remove());
  document.body.append(dlg);
  dlg.showModal();
}

function commandLine(text) {
  const copy = el("button", { type: "button", class: "icon", title: "Copy", onclick: async () => {
    try {
      await navigator.clipboard.writeText(text);
      copy.textContent = "Copied";
    } catch {
      copy.textContent = "Select and copy";
    }
  } }, "Copy");
  return el("div", { class: "command" }, el("pre", { class: "preview" }, text), copy);
}

// Reading the map takes seconds (diskutil per partition). The first time the page says so in
// place of the map; on Refresh the map stays, dimmed and out of reach, until the new one is in.
async function load() {
  const btn = document.getElementById("refresh");
  const main = document.querySelector("main");
  btn.disabled = true;
  btn.replaceChildren(el("span", { class: "spinner", "aria-hidden": "true" }), "Reading…");
  if (state.map) main.classList.add("stale");
  else document.getElementById("disks").replaceChildren(loading("Reading the disks…"));
  main.setAttribute("aria-busy", "true");
  try {
    [state.map, state.formats] = await Promise.all([api("/api/map"), api("/api/formats")]);
    choose();
    render();
    if (state.editor) {
      invalidate(state.editor); // the map may have changed under the plan
      renderEditor();
      if (state.editor.kind === "shrink") readLimits(state.editor); // and the limits with it
    }
  } catch (err) {
    document.getElementById("disks").replaceChildren(el("p", { class: "error" }, err.message));
  } finally {
    btn.disabled = false;
    btn.replaceChildren("Refresh");
    main.classList.remove("stale");
    main.removeAttribute("aria-busy");
  }
}

document.getElementById("refresh").addEventListener("click", load);
// The native window's app menu (A′) opens the same box: one text, one version.
function showAbout() {
  document.getElementById("about").showModal();
}
document.getElementById("about-open").addEventListener("click", showAbout);
document.getElementById("about-close").addEventListener("click", () => document.getElementById("about").close());
document.getElementById("disk-pick").addEventListener("change", (ev) => {
  state.selected = ev.target.value;
  state.byHand = true;
  closeDetail();
  render();
});
// The bars are drawn to the width they get, so redraw when the window's width changes.
let resizeTimer;
const widths = new Map();
new ResizeObserver((entries) => {
  let changed = false;
  for (const e of entries) {
    const w = Math.round(e.contentRect.width);
    if (widths.get(e.target) !== w) changed = true;
    widths.set(e.target, w);
  }
  if (!changed) return;
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    render();
    if (state.editor) fillSketch(document.querySelector("svg.editor-bar"), state.editor);
  }, 100);
}).observe(document.getElementById("disks"));
document.addEventListener("DOMContentLoaded", load); // after editor.js, which the page also needs
