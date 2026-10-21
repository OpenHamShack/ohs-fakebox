  // Overlay editor. The model is the element list the daemon validates
  // (positions are fractions of the frame, text size a fraction of its height,
  // an image's a fraction of its width). Drawn here as HTML over a frame from
  // the picture source; "Render" asks ffmpeg for the same frame with the real
  // overlay on it, so the check is exact rather than a browser's idea of it.
  const OV = { els: [], sel: -1, fields: {}, images: [], locator: "", bg: "", rendered: false, dirty: false,
               cancelArmed: false, panel: null };
  const COLOURS = [["white", "#ffffff"], ["#ffcc00", "#ffcc00"], ["black", "#000000"], ["red", "#ff0000"],
                   ["green", "#008000"], ["cyan", "#00ffff"]];
  const CLOCKS = [["%H:%M:%S", "HH:MM:SS"], ["%H:%M", "HH:MM"], ["%d %b %Y %H:%M", "Date + time"]];
  const SPEEDS = [[0.06, "Slow"], [0.12, "Medium"], [0.24, "Fast"]];
  const PLACE = [["callsign", "Callsign"], ["freq", "Freq"], ["sr", "SR"], ["modcod", "MODCOD"], ["locator", "Locator"], ["weather", "Weather"]];
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const LONGM = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  const LONGD = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
  const p2 = (n) => String(n).padStart(2, "0");
  function strftime(fmt, utc) {
    // The common directives, for the editor's preview only; the box itself
    // uses the C library's strftime on air.
    const d = new Date(), g = (k) => d[(utc ? "getUTC" : "get") + k]();
    const map = { H: p2(g("Hours")), M: p2(g("Minutes")), S: p2(g("Seconds")), d: p2(g("Date")), e: String(g("Date")).padStart(2),
      m: p2(g("Month") + 1), Y: g("FullYear"), y: p2(g("FullYear") % 100), b: MONTHS[g("Month")], h: MONTHS[g("Month")],
      B: LONGM[g("Month")], a: DAYS[g("Day")], A: LONGD[g("Day")], I: p2(g("Hours") % 12 || 12), p: g("Hours") < 12 ? "AM" : "PM",
      Z: utc ? "UTC" : "", "%": "%", T: `${p2(g("Hours"))}:${p2(g("Minutes"))}:${p2(g("Seconds"))}`, R: `${p2(g("Hours"))}:${p2(g("Minutes"))}` };
    return fmt.replace(/%(.)/g, (m, c) => (c in map ? String(map[c]) : m));
  }
  const fillText = (t) => String(t || "").replace(/\{(\w+)\}/g, (m, k) => (k in OV.fields ? OV.fields[k] : m));
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const NAMES = { text: "Text", clock: "Clock", weather: "Weather", image: "Image", ticker: "Ticker" };

  function ovMsg(text, err) { $("ovmsg").textContent = text || ""; $("ovmsg").className = err ? "err" : ""; }

  async function loadFrame(render) {
    const url = "/api/overlay/frame.jpg?render=" + (render ? 1 : 0)
      + (render ? "&elements=" + encodeURIComponent(JSON.stringify(OV.els)) : "") + "&t=" + Date.now();
    ovMsg(render ? "Rendering with ffmpeg…" : "Taking a picture from the source…");
    try {
      const r = await fetch(url);
      if (!r.ok) { ovMsg((await r.json().catch(() => ({}))).error || "No picture", true); return false; }
      const blob = URL.createObjectURL(await r.blob());
      if (render) { OV.renderUrl = blob; } else { OV.bg = blob; }
      ovMsg(render ? "Rendered by ffmpeg: this is what goes on air. Tap Edit to carry on." : "");
      return true;
    } catch (e) { ovMsg("No picture: OpenHamShack did not answer", true); return false; }
  }

  async function openOverlay() {
    const info = await (await fetch("/api/overlay")).json();
    Object.assign(OV, { els: JSON.parse(JSON.stringify(info.elements || [])), fields: info.fields || {},
                        images: info.images || [], locator: info.locator || "", sel: -1, rendered: false, dirty: false,
                        cancelArmed: false, panel: null });
    $("ov").classList.add("open");
    drawOverlay();
    if (await loadFrame(false)) drawOverlay();
  }

  function stageSize() { const s = $("ovstage"); return [s.clientWidth, s.clientHeight]; }

  function elementHtml(e, i, W, H) {
    const fs = (e.size || 0.05) * H;
    const sel = i === OV.sel ? " sel" : "";
    const shift = { left: "0", center: "-50%", right: "-100%" }[e.align || "left"];
    if (e.type === "image") {
      return `<img class="ovel${sel}" data-i="${i}" draggable="false" src="/api/overlay/images/${encodeURIComponent(e.file)}"
        style="left:${e.x * W}px;top:${e.y * H}px;width:${e.size * W}px;opacity:${e.opacity ?? 1};transform:translateX(${shift})">`;
    }
    const colour = e.color && e.color.startsWith("#") ? e.color : e.color || "white";
    let text, empty = "";
    if (e.type === "clock") text = strftime(e.format || "%H:%M:%S", e.utc);
    else if (e.type === "weather") { text = OV.fields.weather || "Weather appears here"; if (!OV.fields.weather) empty = " empty"; }
    else text = fillText(e.text) || "(empty)";
    if (e.type === "ticker") {
      const pad = fs / 4;
      // Shown still; on air it scrolls in from the right edge.
      return `<div class="ovel band${sel}" data-i="${i}" style="top:${e.y * H - pad}px;height:${fs + 2 * pad}px;
        ${e.box ? "background:rgba(0,0,0,.55);" : ""}">
        <span style="position:absolute;left:30%;top:${pad}px;font-size:${fs}px;color:${colour}">${esc(text)}</span></div>`;
    }
    const box = e.box ? `background:rgba(0,0,0,.55);box-shadow:0 0 0 ${fs / 3}px rgba(0,0,0,.55);` : "";
    return `<div class="ovel${sel}${empty}" data-i="${i}" style="left:${e.x * W}px;top:${e.y * H}px;font-size:${fs}px;
      color:${colour};${box}transform:translateX(${shift})">${esc(text)}</div>`;
  }

  function drawStage() {
    const [W, H] = stageSize();
    const st = $("ovstage");
    st.classList.toggle("rendered", OV.rendered);
    st.style.backgroundImage = OV.rendered && OV.renderUrl ? `url(${OV.renderUrl})` : OV.bg ? `url(${OV.bg})` : "";
    st.innerHTML = OV.els.map((e, i) => elementHtml(e, i, W, H)).join("");
  }

  function chips(items, current, attr) {
    return `<div class="chips">${items.map(([v, label]) =>
      `<button class="chip${String(v) === String(current) ? " on" : ""}" ${attr}="${esc(v)}">${label}</button>`).join("")}</div>`;
  }

  function drawSide() {
    const e = OV.els[OV.sel];
    const side = $("ovside");
    if (OV.panel === "images" || (!e && OV.panel === "images")) return drawImagePanel();
    if (!e) {
      side.innerHTML = `<div class="dim">Tap an element on the picture to change it, or drag it to move it.
        Add text, a clock, the weather, a scrolling ticker or an image with the buttons above.</div>
        <div class="dim">${OV.els.length} element${OV.els.length === 1 ? "" : "s"}. The overlay is drawn
        only while <b>Overlay on</b> (above) is chosen, and changes reach the air at the next key-up.</div>`;
      return;
    }
    let h = `<div style="display:flex;justify-content:space-between;align-items:center">
      <h2 style="margin:0">${NAMES[e.type]}</h2><button class="chip" data-ovdo="delete">Delete</button></div>`;
    if (e.type === "text" || e.type === "ticker") {
      h += `<div class="lbl">Text (tap to type)</div><div class="txt tap" data-ovdo="edittext">${esc(e.text || "") || "&nbsp;"}</div>`
        + `<div class="lbl">Insert</div>` + chips(PLACE, "", "data-ovins");
    }
    if (e.type === "clock") {
      const preset = CLOCKS.some(([f]) => f === e.format) ? e.format : "custom";
      h += `<div class="lbl">Format</div>` + chips([...CLOCKS, ["custom", preset === "custom" ? esc(e.format) : "Custom…"]], preset, "data-ovclock")
        + chips([["false", "Local time"], ["true", "UTC"]], String(!!e.utc), "data-ovutc");
    }
    if (e.type === "weather") {
      h += `<div class="dim">${OV.fields.weather ? "Now: " + esc(OV.fields.weather)
        : OV.locator ? "No reading yet: OpenHamShack fetches it every 15 minutes while a weather element is saved."
        : "Set the locator (button above) so OpenHamShack knows where the weather is."}</div>`;
    }
    if (e.type === "image") {
      h += `<div class="lbl">Picture</div>` + chips(OV.images.map((im) => [im.name, esc(im.name)]), e.file, "data-ovfile")
        + `<button class="chip" data-ovdo="images" style="align-self:start">Upload / manage…</button>`
        + `<div class="lbl">Opacity</div><div class="stepper"><button class="chip" data-ovstep="opacity:-0.1">−</button>
           <div class="big">${Math.round((e.opacity ?? 1) * 100)}%</div><button class="chip" data-ovstep="opacity:0.1">+</button></div>`;
    }
    const sizeLabel = e.type === "image" ? `${Math.round(e.size * 100)}% of width` : `${(e.size * 100).toFixed(1)}% of height`;
    const step = e.type === "image" ? 0.02 : 0.005;
    h += `<div class="lbl">Size</div><div class="stepper"><button class="chip" data-ovstep="size:-${step}">−</button>
      <div class="big" style="font-size:28px">${sizeLabel}</div><button class="chip" data-ovstep="size:${step}">+</button></div>`;
    if (e.type !== "image") {
      h += `<div class="lbl">Colour</div><div class="chips">${COLOURS.map(([v, css]) =>
        `<button class="chip swatch${(e.color || "white") === v ? " on" : ""}" data-ovcolor="${v}"><i style="background:${css}"></i></button>`).join("")}</div>`
        + `<div class="lbl">${e.type === "ticker" ? "Band across the picture" : "Box behind"}</div>`
        + chips([["true", "On"], ["false", "Off"]], String(!!e.box), "data-ovbox");
    }
    if (e.type === "ticker") h += `<div class="lbl">Speed</div>` + chips(SPEEDS, e.speed ?? 0.12, "data-ovspeed");
    else h += `<div class="lbl">Align</div>` + chips([["left", "Left"], ["center", "Centre"], ["right", "Right"]], e.align || "left", "data-ovalign");
    side.innerHTML = h;
  }

  function drawImagePanel() {
    const e = OV.els[OV.sel];
    $("ovside").innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center">
      <h2 style="margin:0">Images</h2><button class="chip" data-ovdo="closeimages">Done</button></div>
      <div class="lbl">${e && e.type === "image" ? "Tap one to use it" : "Tap one to place it on the picture"}</div>
      ${OV.images.length ? chips(OV.images.map((im) => [im.name, `${esc(im.name)} · ${Math.ceil(im.bytes / 1024)} kB`]),
        e && e.type === "image" ? e.file : "", "data-ovpick") : `<div class="dim">No images uploaded yet.</div>`}
      <label class="chip" style="display:inline-flex;align-items:center;align-self:start;cursor:pointer">Upload PNG or JPEG…
        <input type="file" id="ovfile" accept="image/png,image/jpeg" style="display:none"></label>
      <div class="dim">Uploading needs a browser with a file picker: open this console on a computer on the
        same network, enter the PIN, then Overlay… → + Image → Upload. PNG with transparency works best; up to 5 MB.</div>`;
    $("ovfile").onchange = uploadImage;
  }

  async function uploadImage(ev) {
    const f = ev.target.files[0]; if (!f) return;
    if (!auth.authorised) return ovMsg("Remote: view only", true);
    ovMsg("Uploading " + f.name + "…");
    const r = await fetch("/api/overlay/image?name=" + encodeURIComponent(f.name), { method: "POST", body: f });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) return ovMsg(body.error || "Upload refused", true);
    OV.images = body.images || OV.images;
    ovMsg("Uploaded " + body.name);
    drawSide();
  }

  function drawOverlay() {
    drawStage(); drawSide();
    $("ovrender").textContent = OV.rendered ? "Edit" : "Render";
    $("ovrender").classList.toggle("on", OV.rendered);
    $("ovloc").textContent = "Locator: " + (OV.locator || "not set");
    $("ovcancel").textContent = OV.cancelArmed ? "Discard changes?" : "Cancel";
    $("ovcancel").classList.toggle("armed", OV.cancelArmed);
    for (const b of document.querySelectorAll("[data-ovadd], #ovsave")) b.disabled = !auth.authorised;
  }

  function changed() {
    OV.dirty = true; OV.cancelArmed = false;
    if (OV.rendered) { OV.rendered = false; ovMsg(""); }
    drawOverlay();
  }

  const DEFAULTS = {
    // Placed where the default callsign (top right) and each other are not.
    text: { text: "{callsign}", x: 0.5, y: 0.45, size: 0.06, align: "center", color: "white", box: true },
    clock: { format: "%H:%M:%S", utc: false, x: 0.03, y: 0.78, size: 0.05, align: "left", color: "white", box: true },
    weather: { x: 0.97, y: 0.78, size: 0.045, align: "right", color: "white", box: true },
    ticker: { text: "CQ CQ de {callsign} on {freq}", y: 0.9, size: 0.05, speed: 0.12, color: "white", box: true, x: 0 },
    image: { x: 0.03, y: 0.04, size: 0.15, align: "left", opacity: 1 },
  };
  function addElement(type, file) {
    if (type === "image" && !file) { OV.panel = "images"; OV.sel = -1; return drawOverlay(); }
    if (OV.els.length >= 20) return ovMsg("At most 20 elements", true);
    const n = OV.els.length;
    OV.els.push({ id: type + Date.now().toString(36), type, ...DEFAULTS[type], ...(file ? { file } : {}) });
    OV.sel = n; OV.panel = null;
    changed();
    if (type === "text" || type === "ticker") editText();
  }

  function editText() {
    const e = OV.els[OV.sel]; if (!e) return;
    Keyboard.open({ title: `${NAMES[e.type]}: {callsign} {freq} {sr} {modcod} {locator} {weather} are filled in`,
      value: e.text || "", maxLength: e.type === "ticker" ? 300 : 120, onOk: (v) => { e.text = v; changed(); } });
  }

  // Keep an element where it is on screen when its alignment changes: the
  // anchor moves to its left edge, centre or right edge.
  function realign(e, align) {
    const el = $("ovstage").querySelector(`[data-i="${OV.sel}"]`);
    const [W] = stageSize();
    if (el && W) {
      const r = el.getBoundingClientRect(), s = $("ovstage").getBoundingClientRect();
      const left = (r.left - s.left) / W, w = r.width / W;
      e.x = +Math.min(1, Math.max(0, left + w * { left: 0, center: 0.5, right: 1 }[align])).toFixed(4);
    }
    e.align = align;
  }

  $("ovside").addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-ovdo],[data-ovins],[data-ovclock],[data-ovutc],[data-ovstep],[data-ovcolor],[data-ovbox],[data-ovspeed],[data-ovalign],[data-ovfile],[data-ovpick]");
    if (!b) return;
    const e = OV.els[OV.sel], d = b.dataset;
    if (d.ovdo === "closeimages") { OV.panel = null; return drawOverlay(); }
    if (d.ovdo === "images") { OV.panel = "images"; return drawOverlay(); }
    if (d.ovpick) {
      if (e && e.type === "image") { e.file = d.ovpick; OV.panel = null; return changed(); }
      return addElement("image", d.ovpick);
    }
    if (!e) return;
    if (d.ovdo === "delete") { OV.els.splice(OV.sel, 1); OV.sel = -1; return changed(); }
    if (d.ovdo === "edittext") return editText();
    if (d.ovins) { e.text = (e.text || "") + (e.text && !e.text.endsWith(" ") ? " " : "") + `{${d.ovins}}`; return changed(); }
    if (d.ovclock) {
      if (d.ovclock !== "custom") { e.format = d.ovclock; return changed(); }
      return Keyboard.open({ title: "Clock format: %H hour, %M minute, %S second, %d day, %b month, %Y year",
        value: e.format, maxLength: 64, onOk: (v) => { if (v) { e.format = v; changed(); } } });
    }
    if (d.ovutc) { e.utc = d.ovutc === "true"; return changed(); }
    if (d.ovstep) {
      const [k, by] = d.ovstep.split(":");
      const lim = k === "opacity" ? [0, 1] : e.type === "image" ? [0.02, 1] : [0.015, 0.3];
      e[k] = +Math.min(lim[1], Math.max(lim[0], (e[k] ?? (k === "opacity" ? 1 : 0.05)) + Number(by))).toFixed(4);
      return changed();
    }
    if (d.ovcolor) { e.color = d.ovcolor; return changed(); }
    if (d.ovbox) { e.box = d.ovbox === "true"; return changed(); }
    if (d.ovspeed) { e.speed = Number(d.ovspeed); return changed(); }
    if (d.ovalign) { realign(e, d.ovalign); return changed(); }
    if (d.ovfile) { e.file = d.ovfile; return changed(); }
  });

  // Dragging: pointer events, so a finger works as well as a mouse. The
  // stage holds the capture, because elements are redrawn as they move.
  let drag = null;
  $("ovstage").addEventListener("pointerdown", (ev) => {
    if (OV.rendered) { OV.rendered = false; ovMsg(""); }
    const t = ev.target.closest(".ovel");
    OV.panel = null;
    if (!t) { OV.sel = -1; drag = null; return drawOverlay(); }
    OV.sel = Number(t.dataset.i);
    const e = OV.els[OV.sel];
    drag = { id: ev.pointerId, x0: ev.clientX, y0: ev.clientY, ex: e.x, ey: e.y, moved: false };
    $("ovstage").setPointerCapture(ev.pointerId);
    drawOverlay();
  });
  $("ovstage").addEventListener("pointermove", (ev) => {
    if (!drag || ev.pointerId !== drag.id) return;
    const [W, H] = stageSize();
    const dx = ev.clientX - drag.x0, dy = ev.clientY - drag.y0;
    if (!drag.moved && Math.hypot(dx, dy) < 6) return;   // a tap selects, it does not nudge
    drag.moved = true;
    const e = OV.els[OV.sel];
    if (e.type !== "ticker") e.x = +Math.min(1, Math.max(0, drag.ex + dx / W)).toFixed(4);  // a ticker spans the width
    e.y = +Math.min(1, Math.max(0, drag.ey + dy / H)).toFixed(4);
    OV.dirty = true; OV.cancelArmed = false;
    drawStage();
  });
  const endDrag = (ev) => { if (drag && ev.pointerId === drag.id) { drag = null; drawOverlay(); } };
  $("ovstage").addEventListener("pointerup", endDrag);
  $("ovstage").addEventListener("pointercancel", endDrag);

  for (const b of document.querySelectorAll("[data-ovadd]")) b.onclick = () => addElement(b.dataset.ovadd);
  $("ovopen").onclick = () => openOverlay();
  $("ovrender").onclick = async () => {
    if (OV.rendered) { OV.rendered = false; ovMsg(""); return drawOverlay(); }
    $("ovrender").disabled = true;
    const ok = await loadFrame(true);
    $("ovrender").disabled = false;
    OV.rendered = ok; drawOverlay();
  };
  $("ovcancel").onclick = () => {
    if (OV.dirty && !OV.cancelArmed) { OV.cancelArmed = true; drawOverlay(); setTimeout(() => { OV.cancelArmed = false; drawOverlay(); }, 4000); return; }
    $("ov").classList.remove("open");
  };
  $("ovsave").onclick = async () => {
    if (!canChange()) return;
    const r = await fetch("/api/overlay", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ elements: OV.els }) });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) return ovMsg(body.error || "Not saved", true);
    OV.els = body.elements; OV.dirty = false;
    $("ov").classList.remove("open");
  };
  $("ovloc").onclick = () => editLocator(OV.locator, (m) => ovMsg(m, true), async () => {
    const info = await (await fetch("/api/overlay")).json();
    OV.locator = info.locator || ""; OV.fields = info.fields || OV.fields;
    drawOverlay();
  });
  // The clocks tick in the editor too.
  setInterval(() => { if ($("ov").classList.contains("open") && !drag && OV.els.some((e) => e.type === "clock")) drawStage(); }, 1000);
  window.addEventListener("resize", () => { if ($("ov").classList.contains("open")) drawStage(); });
