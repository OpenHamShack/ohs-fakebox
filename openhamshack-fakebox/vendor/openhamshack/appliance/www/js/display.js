  // Display (Settings > Screens). /api/display lists the screens; choices are
  // a draft per screen until "Try it", which the daemon applies and undoes
  // after 15 s unless Keep comes. The bar rides on /api/status so that every
  // console shows it, including the box's own screen when the change was
  // made from a laptop.
  const ROLE = { tv: "TV", console: "Console", single: "Only screen", spare: "Not used", off: "Off" };
  const ROT = { normal: "0°", 90: "90°", 180: "180°", 270: "270°" };
  let disp = null, dispDraft = {}, dispPend = null;
  const hz = (mhz) => (mhz / 1000).toFixed(3).replace(/\.?0+$/, "");
  const sameSetting = (a, b) => (!a.enabled && !b.enabled) || JSON.stringify([a.enabled, a.mode, a.scale, a.transform])
    === JSON.stringify([b.enabled, b.mode, b.scale, b.transform]);

  async function loadDisplay() {
    try { disp = await (await fetch("/api/display")).json(); } catch (e) { return; }
    // A draft the screen now matches has been applied (or was never a change).
    for (const o of disp.outputs || []) if (dispDraft[o.name] && sameSetting(dispDraft[o.name], o.setting)) delete dispDraft[o.name];
    paintDisplay();
  }
  function draftOf(o) {
    if (!dispDraft[o.name]) {
      const s = o.setting;
      dispDraft[o.name] = s.enabled ? { ...s } : { enabled: false, mode: o.native, scale: 1, transform: "normal" };
    }
    return dispDraft[o.name];
  }
  function paintDisplay() {
    if (!disp) return;
    if (!disp.available) {
      $("displist").innerHTML = `<div class="expl">No screens to set: the screens are not running (sway). This is normal on a box without a display.</div>`;
      $("dispacts").innerHTML = "";
      return;
    }
    $("displist").innerHTML = disp.outputs.map((o) => {
      const s = o.setting, d = dispDraft[o.name] || { ...s };
      const cur = s.enabled
        ? `${s.mode[0]}×${s.mode[1]} · ${hz(s.mode[2])} Hz · scale ${s.scale}${s.transform === "normal" ? "" : " · " + ROT[s.transform]}`
          // Scaled or turned, the size windows are laid out in differs from the mode.
          + (o.logical && o.logical.width && (o.logical.width !== s.mode[0] || o.logical.height !== s.mode[1])
            ? ` <span class="dim">(${o.logical.width}×${o.logical.height} for the console and picture)</span>` : "")
        : "switched off";
      let out = `<div class="dispout${s.enabled ? "" : " off"}" data-out="${esc(o.name)}"><div class="nethead">`
        + `<span class="role ${o.role}">${ROLE[o.role] || esc(o.role)}</span><b>${esc(o.label)}</b>`
        + `<span class="dim">${esc(o.name)}</span></div><div class="expl">Now: ${cur}</div>`
        + `<div class="field"><span>Screen</span><div class="chips">`
        + `<button class="chip${d.enabled ? " on" : ""}" data-d="enabled" data-v="1">On</button>`
        + `<button class="chip${d.enabled ? "" : " on"}" data-d="enabled" data-v="0">Off</button></div></div>`;
      if (d.enabled && o.modes.length) {
        const res = o.modes.find((g) => d.mode && g.width === d.mode[0] && g.height === d.mode[1]) || o.modes[0];
        out += `<div class="field"><span>Resolution</span><div class="chips">` + o.modes.map((g) =>
          `<button class="chip${g === res ? " on" : ""}" data-d="res" data-v="${g.width}x${g.height}">${g.width}×${g.height}`
          + `${g.native ? "<small>native</small>" : ""}</button>`).join("") + `</div></div>`
          + `<div class="field"><span>Refresh</span><div class="chips">` + res.rates.map((r) =>
            `<button class="chip${d.mode && d.mode[2] === r ? " on" : ""}" data-d="rate" data-v="${r}">${hz(r)} Hz`
            + `${o.native && res.native && o.native[2] === r ? "<small>native</small>" : ""}</button>`).join("") + `</div></div>`
          + `<div class="field"><span>Scale</span><div class="chips">` + disp.scales.map((v) =>
            `<button class="chip${d.scale === v ? " on" : ""}" data-d="scale" data-v="${v}">${v * 100}%</button>`).join("") + `</div></div>`
          + `<div class="field"><span>Rotation</span><div class="chips">` + disp.transforms.map((t) =>
            `<button class="chip${d.transform === t ? " on" : ""}" data-d="transform" data-v="${t}">${ROT[t]}</button>`).join("") + `</div></div>`;
      }
      if (!sameSetting(d, s)) out += `<div class="chips"><button class="chip on" data-d="try">Try it</button>`
        + `<button class="chip" data-d="cancel">Cancel</button></div>`;
      return out + `</div>`;
    }).join("") || `<div class="expl">No screens connected.</div>`;
    const tv = disp.outputs.find((o) => o.role === "tv"), con = disp.outputs.find((o) => o.role === "console");
    $("dispacts").innerHTML = (tv && con ? `<button class="chip" data-da="swap">Swap TV and console</button>` : "")
      + `<button class="chip" data-da="reset">Reset display settings</button>`;
    // What scale and rotation do, said once under the screens.
    if (!$("dispnote")) $("dispacts").insertAdjacentHTML("afterend", `<div class="expl" id="dispnote"><b>Scale</b> makes
      text and buttons larger on a laptop or high-resolution panel; the console and the picture follow it.
      <b>Rotation</b> turns the picture for a screen mounted on its side; touch turns with it.</div>`);
  }
  $("displist").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-d]"); if (!b) return;
    const card = b.closest("[data-out]"), name = card.dataset.out;
    const o = disp.outputs.find((x) => x.name === name); if (!o) return;
    const d = draftOf(o), k = b.dataset.d, v = b.dataset.v;
    if (k === "enabled") {
      d.enabled = v === "1";
      if (d.enabled && !d.mode) d.mode = o.native;
    } else if (k === "res") {
      const g = o.modes.find((g) => `${g.width}x${g.height}` === v);
      // Keep the refresh rate if the new resolution has it, else its fastest.
      const keep = d.mode && g.rates.includes(d.mode[2]) ? d.mode[2] : g.rates[0];
      d.mode = [g.width, g.height, keep];
    } else if (k === "rate") d.mode = [d.mode[0], d.mode[1], Number(v)];
    else if (k === "scale") d.scale = Number(v);
    else if (k === "transform") d.transform = v;
    else if (k === "cancel") delete dispDraft[name];
    else if (k === "try") { tryDisplay({ outputs: { [name]: d } }); return; }
    paintDisplay();
  });
  $("dispacts").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-da]"); if (!b) return;
    if (b.dataset.da === "reset") { dispDraft = {}; tryDisplay({ reset: true }); return; }
    const tv = disp.outputs.find((o) => o.role === "tv"), con = disp.outputs.find((o) => o.role === "console");
    if (tv && con) tryDisplay({ roles: { tv: con.name, console: tv.name } });
  });
  async function tryDisplay(body) {
    $("dispmsg").textContent = "Applying…";
    const j = await netPost("/api/display", body, (m) => { $("dispmsg").textContent = m; });
    if (!j) return;
    disp = j;
    dispPend = j.pending;
    paintDispPending();
    for (const o of disp.outputs) delete dispDraft[o.name];
    paintDisplay();
  }

  function paintDispPending(st) {
    if (st !== undefined) {
      // Back from pending to none: kept or undone, so show what the screens do now.
      if (dispPend && !st.pending && setOpen && setCat === "screens") loadDisplay();
      dispPend = st.pending;
    }
    const p = dispPend, el = $("disppend");
    el.classList.toggle("on", !!p);
    if (!p) { el.dataset.sig = ""; return; }
    if (el.dataset.sig !== p.what) {
      el.dataset.sig = p.what;
      el.innerHTML = `<span><b>${esc(p.what)}.</b> Keep it? Otherwise it goes back in <b class="left" id="displeft"></b> s.`
        + ` <span class="err" id="disperr"></span></span><div class="chips"><button class="chip on" data-dp="keep">Keep</button><button class="chip" data-dp="undo">Undo</button></div>`;
    }
    $("displeft").textContent = p.secondsLeft;
  }
  $("disppend").addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-dp]"); if (!b) return;
    b.disabled = true;
    // A refusal (a remote browser without the PIN) is said in the bar, which
    // is on whatever page is open; the daemon undoes the change regardless.
    const j = await netPost("/api/display/" + b.dataset.dp, {}, (m) => { $("disperr").textContent = m; });
    if (!j) { b.disabled = false; return; }
    dispPend = null; paintDispPending();
    $("dispmsg").textContent = b.dataset.dp === "keep" ? "Kept." : "Put back as it was.";
    loadDisplay();
  });
