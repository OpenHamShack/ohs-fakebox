  // Touch or Compact (docs/ui-review.md, "Proposed Compact layout"). Touch is
  // the box's own screen: 52-90 px controls read at arm's length. Compact is
  // the same page for a mouse and keyboard - smaller controls, long chip
  // rows as menus, numbers and text typed straight in - so a laptop sees
  // more at once. Same controls, same order, same save() rules: one class on
  // <body> (density-compact) that sets openhamshack.css's size variables again,
  // and the few changes of input below.
  //
  // Which: Auto unless chosen. Auto is Compact for a mouse-and-keyboard
  // browser elsewhere ((pointer: fine) and (hover: hover)) and always Touch
  // on the box's own screen - a mouse plugged into the box must never shrink
  // its touchscreen. A choice is kept per browser (localStorage) or, on the
  // box's own screen, as the box's setting consoleDensity (a laptop running
  // OpenHamShack may want Compact on its own panel, and that must survive a
  // restart). Worked out at load and when chosen, never on every pointer
  // event, so the layout does not jump under the cursor.
  const DENSITIES = ["auto", "touch", "compact"];
  let densityShown = null, densityLocalKnown = false;
  const FINE = matchMedia("(pointer: fine) and (hover: hover)");
  function densityChoice() {
    if (auth.local) return DENSITIES.includes(cfg.consoleDensity) ? cfg.consoleDensity : "auto";
    try { const c = localStorage.getItem("lbDensity"); if (DENSITIES.includes(c)) return c; } catch (e) { /* auto */ }
    return "auto";
  }
  function densityWanted() {
    const c = densityChoice();
    if (c !== "auto") return c;
    return !auth.local && FINE.matches ? "compact" : "touch";
  }
  const compactNow = () => densityShown === "compact";
  function applyDensity() {
    const d = densityWanted();
    for (const b of document.querySelectorAll("[data-density]")) b.classList.toggle("on", b.dataset.density === densityChoice());
    $("densbtn").textContent = d === "compact" ? "Aa+" : "Aa−";
    $("densbtn").title = d === "compact" ? "Larger, for touch" : "Smaller, for a mouse and keyboard";
    if (d === densityShown) return;
    densityShown = d;
    document.body.classList.toggle("density-compact", d === "compact");
    syncSelects();
    if (typeof reportSlot === "function") requestAnimationFrame(reportSlot);   // the picture's place moved
  }
  // Called from refresh(): the first status says whether this is the box's
  // own screen; after that only a choice changes anything.
  function paintDensity() {
    if (!densityLocalKnown) { densityLocalKnown = true; applyDensity(); }
    else for (const b of document.querySelectorAll("[data-density]")) b.classList.toggle("on", b.dataset.density === densityChoice());
    syncSelects();
  }
  function chooseDensity(d) {
    if (auth.local) save({ consoleDensity: d });   // the box's own screen: its setting
    else try { localStorage.setItem("lbDensity", d); } catch (e) { /* this page only */ }
    if (!auth.local) applyDensity(); else { cfg.consoleDensity = d; applyDensity(); }
  }
  document.addEventListener("click", (e) => {
    const b = e.target.closest && e.target.closest("[data-density]");
    if (b) chooseDensity(b.dataset.density);
  });
  $("densbtn").onclick = () => chooseDensity(densityShown === "compact" ? "touch" : "compact");
  applyDensity();   // a first guess (not yet knowing whether this is the box), corrected at the first status

  // Long chip rows as a menu in Compact (data-compact="select"): a <select>
  // built from the row's own chips, whose choice taps the chip - the same
  // handler, the same rules. Rebuilt only when the chips change, and never
  // under an open menu.
  function syncSelects() {
    for (const row of document.querySelectorAll('[data-compact="select"]')) {
      let sel = row.querySelector(":scope > select.csel");
      const chips = [...row.querySelectorAll(":scope > button")];
      const sig = JSON.stringify(chips.map((c) => [c.textContent, c.classList.contains("on"), c.disabled]));
      if (sel && sel.dataset.sig === sig) continue;
      if (sel && document.activeElement === sel) continue;
      if (!sel) {
        sel = document.createElement("select");
        sel.className = "csel";
        sel.setAttribute("aria-label", (row.closest(".field") && row.closest(".field").firstElementChild.textContent) || "Choose");
        sel.addEventListener("change", () => {
          const pick = [...row.querySelectorAll(":scope > button")][Number(sel.value)];
          if (pick) pick.click();
          setTimeout(syncSelects, 50);
        });
        row.prepend(sel);
      }
      sel.dataset.sig = sig;
      const on = chips.findIndex((c) => c.classList.contains("on"));
      sel.innerHTML = (on < 0 ? `<option value="" selected disabled>—</option>` : "")
        + chips.map((c, i) => `<option value="${i}"${i === on ? " selected" : ""}${c.disabled ? " disabled" : ""}>${esc(c.textContent)}</option>`).join("");
    }
  }

  // A field typed into (Compact), over the readout or chip that opens the
  // pad on a touchscreen: Enter keeps it, Esc puts it back, leaving it keeps
  // it if it changed. Over the element rather than inside it, so the poll
  // repainting the readout every second does not take the field away.
  function typeInto(el, key, title, kind) {
    if (document.querySelector(".inlwrap")) return;
    if (!canChange()) return;
    const r = el.getBoundingClientRect();
    const wrap = document.createElement("span");
    wrap.className = "inlwrap";
    wrap.style.cssText = `left:${r.left}px;top:${r.top}px;width:${Math.max(r.width, 200)}px;height:${r.height}px`;
    const f = document.createElement("input");
    f.className = "inl";
    f.inputMode = kind === "num" ? "decimal" : kind === "ip" ? "decimal" : "text";
    const was = key.startsWith("__") || key === "remotePin" ? "" : String(cfg[key] ?? "");
    f.value = was;
    f.setAttribute("aria-label", title);
    f.title = title;
    const pad = document.createElement("button");
    pad.type = "button";
    pad.className = "chip ro";
    pad.textContent = "Pad";
    pad.title = "The number pad";
    wrap.append(f, pad);
    document.body.append(wrap);
    let done = false;
    const close = (keep) => {
      if (done) return;
      done = true;
      wrap.remove();
      const v = f.value.trim().replace(",", ".");
      if (keep && v && v !== was) padCommit(key, kind, v);
    };
    f.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); close(true); }
      else if (e.key === "Escape") { e.preventDefault(); close(false); }
    });
    f.addEventListener("blur", () => setTimeout(() => { if (document.activeElement !== pad) close(true); }, 0));
    pad.addEventListener("mousedown", (e) => e.preventDefault());
    pad.addEventListener("click", () => { close(false); openPad(key, title, kind); });
    addEventListener("scroll", () => close(true), { once: true, capture: true });
    f.focus();
    f.select();
  }

  // The explanations fold to one line in Compact; a click opens one.
  document.addEventListener("click", (e) => {
    if (!compactNow() || !e.target.closest) return;
    const x = e.target.closest(".expl");
    if (x && !e.target.closest("button, a")) x.classList.toggle("open");
  });

  // Keys, in Compact: g then q (QO-100), s (Settings), l (Log), h (HamTV),
  // a (Aim), m (the main page); / searches the log on the Log page and the
  // guide anywhere else; r starts or stops recording. Never one for
  // TRANSMIT: keying stays two deliberate clicks.
  let gAt = 0;
  document.addEventListener("keydown", (e) => {
    if (!compactNow() || e.ctrlKey || e.metaKey || e.altKey || e.repeat) return;
    if (e.target.matches && e.target.matches("input, textarea, select")) return;
    if ($("kbd").classList.contains("open") || $("pad").classList.contains("open") || Help.isOpen()
        || document.querySelector(".modal.open") || $("setup").classList.contains("open") || $("ov").classList.contains("open")) return;
    const k = e.key.toLowerCase();
    if (Date.now() - gAt < 1500) {
      gAt = 0;
      const go = { q: () => showQo(true), s: () => showSettings(true), l: () => showLog(true), h: () => showHam(true),
                   a: () => showAim(true), m: () => $("setback").click() }[k];
      if (go) { e.preventDefault(); go(); }
      return;
    }
    if (k === "g") { gAt = Date.now(); e.preventDefault(); return; }
    if (e.key === "/") { e.preventDefault(); if (document.body.classList.contains("inlog")) $("logsearch").click(); else Help.open(); return; }
    if (k === "r") { e.preventDefault(); save({ rxRecord: !cfg.rxRecord }); }
  });
