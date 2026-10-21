  // Controllers (pad.py, controllers/). /api/pad arrives in a later version
  // than the console may be running against, so a missing endpoint is a
  // state to show, not an error. Everything drawn here comes from the
  // driver's own description of its layout, so a new model needs nothing
  // added to this page.
  let padInfo = null, padModel = null, padPage = 0, padPick = null, padResetArm = null, padDropArm = null,
      padSeenAt = 0;
  async function loadPad() {
    try {
      const r = await fetch("/api/pad");
      padInfo = r.ok ? await r.json() : null;
    } catch (e) { padInfo = null; }
    paintPad();
  }
  const padModels = () => (padInfo && padInfo.controllers) || [];
  const padCur = () => padModels().find((m) => m.model === padModel) || padModels()[0] || null;
  const padAct = (id) => (padInfo.actions || []).find((a) => a.id === id);
  const padName = (id) => !id || id === "none" || id === "none-turn" ? "—" : (padAct(id) || { name: id }).name;
  const padHex = (bg) => "#" + String(bg || "0x1f2937").replace(/^0x/, "").padStart(6, "0").slice(-6);
  function padCell(m, c, b, live) {
    const def = (padPage ? m.pages[0] : m.default)[c.id] || {};
    const changed = Object.keys(b).some((s) => (b[s] || "") !== (def[s] || ""));
    const p = padInfo.pressed;
    const hit = p && p.model === m.model && p.control === c.id && Date.now() / 1000 - p.at < 4;
    const cls = `pc ${c.kind}${hit ? " hit" : ""}${changed ? " changed" : ""}`;
    const pos = c.row === undefined ? "" : `grid-row:${c.row + 1} / span ${c.h || 1};grid-column:${c.col + 1} / span ${c.w || 1};`;
    let inner;
    const face = live && m.faces && m.faces[c.id];
    if (face && b.press !== "none") {
      inner = `<b>${esc(face[0])}</b><small>${esc(face[1])}</small>`;
      return `<button class="${cls}" style="${pos}background:${padHex(face[2])}" data-padc="${esc(c.id)}" `
        + `aria-label="${esc(c.label + ": " + padName(b.press))}"><span class="pid">${esc(c.label)}</span>${inner}</button>`;
    }
    if (c.kind === "knob") inner = `<b>↻ ${esc(padName(b.turn))}</b><small>press: ${esc(padName(b.press))}</small>`;
    else inner = `<b>${esc(padName(b.press))}</b>`;
    return `<button class="${cls}" style="${pos}" data-padc="${esc(c.id)}" aria-label="${esc(c.label + ": " + padName(b.press))}">`
      + `<span class="pid">${esc(c.label)}</span>${inner}</button>`;
  }
  function paintPad() {
    const p = padInfo;
    const none = !p || !p.controllers;
    for (const id of ["padmodels", "padpagerow", "paddraw", "padextra", "padhint", "padreset", "padkpwrap"])
      $(id).style.display = none ? "none" : "";
    $("padbright").style.display = "none";
    if (!p) { $("padstate").textContent = "No controller support in this version."; $("padkeylist").innerHTML = ""; return; }
    if (none) {
      // A daemon from before remapping: its fixed key list, as it was.
      $("padstate").innerHTML = (p.connected ? `<b>Connected</b>${p.model ? ": " + esc(p.model) : ""}` : "<b>Not connected</b>")
        + (p.message ? ` · ${esc(p.message)}` : "");
      $("padbright").style.display = p.connected ? "" : "none";
      $("padbval").textContent = (p.brightness ?? "—") + "%";
      $("padkeylist").innerHTML = (p.keys || []).map((k) => `<b>${esc(k.id)}</b><span>${esc(k.label || k.action || "")}</span>`).join("");
      return;
    }
    // A press on a controller shows that controller: the quickest way to
    // find a key is to press it.
    if (p.pressed && p.pressed.at > padSeenAt) {
      if (padSeenAt && p.pressed.model !== padModel) { padModel = p.pressed.model; padPage = 0; }
      padSeenAt = p.pressed.at;
    }
    if (!padModels().some((m) => m.model === padModel)) padModel = p.current;
    const m = padCur();
    if (!m) return;
    if (padPage >= m.pages.length) padPage = 0;
    $("padmodels").innerHTML = padModels().map((x) => `<button class="chip${x.model === m.model ? " on" : ""}" data-padm="${esc(x.model)}">`
      + `${esc(x.name)}${x.connected ? " · connected" : ""}</button>`).join("");
    $("padstate").innerHTML = (m.connected ? `<b>${esc(m.name)} connected</b>` : `<b>${esc(m.name)} not connected</b>`)
      + (m.pages.length > 1 ? ` · showing page ${m.page + 1} of ${m.pages.length}` : "")
      + (p.message && m.connected && !/^Connected/.test(p.message) ? ` · ${esc(p.message)}` : "");
    $("padbright").style.display = m.connected && m.screen ? "" : "none";
    $("padbval").textContent = (p.brightness ?? "—") + "%";
    const n = m.pages.length, max = p.maxPages || 4;
    $("padpages").innerHTML = m.pages.map((_, i) => `<button class="chip${i === padPage ? " on" : ""}" data-padp="${i}">${i + 1}${i === m.page && n > 1 ? " ◂" : ""}</button>`).join("")
      + `<button class="chip" data-padadd="1"${n >= max ? " disabled" : ""}>Add page</button>`
      + (n > 1 ? `<button class="chip" data-paddrop="1">${padDropArm ? `Tap again to remove page ${n}` : `Remove page ${n}`}</button>` : "");
    const page = m.pages[padPage] || {};
    const live = padPage === m.page;
    const cols = Math.max(1, ...m.controls.map((c) => c.col + (c.w || 1)));
    const wide = m.controls.some((c) => c.kind === "key") ? 92 : 104;
    $("paddraw").style.gridTemplateColumns = `repeat(${cols}, minmax(0, ${wide}px))`;
    $("paddraw").innerHTML = m.controls.map((c) => padCell(m, c, page[c.id] || {}, live)).join("");
    // Keys a keypad sent that the drawing has no place for, and any mapped
    // ones: open-layout models take whatever key their maker chose.
    const drawn = new Set(m.controls.map((c) => c.id));
    const extra = [...new Set([...(m.seenKeys || []), ...Object.keys(page).filter((k) => !drawn.has(k))])];
    $("padextra").innerHTML = m.openLayout
      ? extra.map((k) => padCell(m, { id: k, kind: "key", label: k }, page[k] || { press: "none" }, false)).join("") : "";
    $("padhint").textContent = m.openLayout
      ? "Tap a key to choose what it does. A key that is not drawn appears here once pressed."
      : "Tap a control to choose what it does. Pressing one on the controller lights it here.";
    $("padnotes").innerHTML = (m.notes || []).map(esc).join("<br>");
    $("padreset").textContent = padResetArm ? "Tap again to reset" : "Reset to default";
    const rows = [];
    for (const c of [...m.controls, ...extra.map((k) => ({ id: k, label: k }))]) {
      const b = page[c.id] || {};
      for (const slot of ["turn", "press"]) {
        if (!b[slot] || b[slot] === "none" || b[slot] === "none-turn") continue;
        const a = padAct(b[slot]);
        rows.push(`<b>${esc(c.label)}${slot === "turn" && b.press ? " ↻" : ""}</b><span>${esc(a ? a.label : b[slot])}</span>`);
      }
    }
    $("padkeylist").innerHTML = rows.join("");
    const kps = p.keypads || [];
    $("padkps").innerHTML = kps.length ? kps.map((k) => `<button class="chip${k.chosen ? " on" : ""}" data-padkp="${esc(k.id)}">`
      + `${esc(k.name)}${k.chosen ? (k.unplugged ? " · not plugged in" : k.ready ? " · in use" : " · unplug and plug in again") : ""}</button>`).join("")
      : `<span class="expl">No USB keyboards or keypads plugged in.</span>`;
    if (padPick) paintPadPick();
  }
  async function padSend(body, errEl) {
    const j = await netPost("/api/pad", body, (msg) => { $(errEl).textContent = msg; });
    if (j && j.controllers) { padInfo = j; paintPad(); }
    return j;
  }
  function paintPadPick() {
    const m = padCur(); if (!m || !padPick) return;
    const c = [...m.controls].find((x) => x.id === padPick.id) || { id: padPick.id, label: padPick.id, kind: "key" };
    const b = (m.pages[padPage] || {})[c.id] || {};
    $("padpicktitle").textContent = `${c.label}${m.pages.length > 1 ? ` · page ${padPage + 1}` : ""}`;
    $("padpickslots").innerHTML = c.kind === "knob"
      ? ["turn", "press"].map((s) => `<button class="chip${padPick.slot === s ? " on" : ""}" data-padslot="${s}">${s === "turn" ? "Turn" : "Press"}: ${esc(padName(b[s]))}</button>`).join("")
      : "";
    const holder = (id) => Object.keys(m.pages[0]).find((k) => m.pages[0][k].press === id);
    const groups = padInfo.groups || [];
    const acts = (padInfo.actions || []).filter((a) => a.kind === padPick.slot);
    const chip = (a) => {
      const on = b[padPick.slot] === a.id;
      const h = a.single ? holder(a.id) : null;
      const off = a.single && padPage > 0;
      const note = off ? " (page 1 only: the same key on every page)" : h && h !== c.id ? ` (moves here from ${h})` : "";
      return `<button class="chip${on ? " on" : ""}" data-pada="${esc(a.id)}"${off ? " disabled" : ""}>${esc(a.label)}${esc(note)}</button>`;
    };
    $("padpicklist").innerHTML = groups.map((g) => {
      const here = acts.filter((a) => a.group === g);
      return here.length ? `<h3>${esc(g)}</h3><div class="chips">${here.map(chip).join("")}</div>` : "";
    }).join("") + `<div class="chips"><button class="chip${["none", "none-turn"].includes(b[padPick.slot]) ? " on" : ""}" `
      + `data-pada="${padPick.slot === "turn" ? "none-turn" : "none"}">Nothing</button></div>`;
  }
  $("paddraw").addEventListener("click", padOpen);
  $("padextra").addEventListener("click", padOpen);
  function padOpen(e) {
    const t = e.target.closest("[data-padc]");
    if (!t || !padInfo || !auth.authorised) return;
    const m = padCur();
    const c = m.controls.find((x) => x.id === t.dataset.padc);
    padPick = { id: t.dataset.padc, slot: c && c.kind === "knob" ? "turn" : "press" };
    $("padpickmsg").textContent = "";
    paintPadPick();
    $("padpick").classList.add("open");
  }
  $("padpickclose").onclick = () => { $("padpick").classList.remove("open"); padPick = null; };
  $("padpick").addEventListener("click", async (e) => {
    const s = e.target.closest("[data-padslot]");
    if (s) { padPick.slot = s.dataset.padslot; return paintPadPick(); }
    const a = e.target.closest("[data-pada]");
    if (!a || !padPick) return;
    const j = await padSend({ model: padCur().model, page: padPage, control: padPick.id, slot: padPick.slot, action: a.dataset.pada },
                            "padpickmsg");
    if (j) { $("padpick").classList.remove("open"); padPick = null; }
  });
  $("padmodels").addEventListener("click", (e) => {
    const b = e.target.closest("[data-padm]"); if (!b) return;
    padModel = b.dataset.padm; padPage = 0; paintPad();
  });
  $("padpages").addEventListener("click", async (e) => {
    const b = e.target.closest("button"); if (!b || !padInfo) return;
    const m = padCur();
    if (b.dataset.padp !== undefined) { padPage = Number(b.dataset.padp); return paintPad(); }
    if (!canChange()) return;
    if (b.dataset.padadd) {
      if (await padSend({ model: m.model, pages: m.pages.length + 1 }, "padmsg")) { padPage = m.pages.length; paintPad(); }
      return;
    }
    if (b.dataset.paddrop) {
      // Two taps: a page's keys go with it.
      if (!padDropArm) { padDropArm = setTimeout(() => { padDropArm = null; paintPad(); }, 4000); return paintPad(); }
      clearTimeout(padDropArm); padDropArm = null;
      await padSend({ model: m.model, pages: m.pages.length - 1 }, "padmsg");
      paintPad();
    }
  });
  $("padreset").addEventListener("click", async () => {
    if (!padInfo || !auth.authorised) return;
    if (!padResetArm) { padResetArm = setTimeout(() => { padResetArm = null; paintPad(); }, 4000); return paintPad(); }
    clearTimeout(padResetArm); padResetArm = null;
    if (await padSend({ model: padCur().model, reset: true }, "padmsg")) padPage = 0;
    paintPad();
  });
  $("padkps").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-padkp]"); if (!b || !padInfo || !auth.authorised) return;
    const chosen = new Set((padInfo.keypads || []).filter((k) => k.chosen).map((k) => k.id));
    if (chosen.has(b.dataset.padkp)) chosen.delete(b.dataset.padkp); else chosen.add(b.dataset.padkp);
    await padSend({ keypads: [...chosen] }, "padmsg");
  });
  $("padbright").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-padb]");
    if (!b || !padInfo || !auth.authorised) return;
    const n = Math.max(0, Math.min(100, Number(padInfo.brightness || 0) + Number(b.dataset.padb)));
    padInfo.brightness = n; paintPad();
    const r = await fetch("/api/pad", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ brightness: n }) }).catch(() => null);
    if (r && r.ok) { const j = await r.json().catch(() => null); if (j && "connected" in j) { padInfo = j; paintPad(); } }
    else loadPad();
  });
  // Every second while the Controller page is showing, so a press lights its
  // key promptly; every third second otherwise, as before.
  let padTick = 0;
  setInterval(() => {
    padTick++;
    if (setOpen && (setCat === "controller" || padTick % 3 === 0)) loadPad();
  }, 1000);
