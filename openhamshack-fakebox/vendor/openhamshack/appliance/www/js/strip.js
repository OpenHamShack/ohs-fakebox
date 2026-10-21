  // The receive status strip under the header, on every page, and the REC
  // pill beside ON AIR. What is received, the margin over the threshold for
  // the mode received (the number an operator acts on; MER alone needs its
  // threshold beside it), where it is tuned, and whether it is recording.
  // Every element with class rxstrip gets the same contents, so a layout
  // that wants the strip somewhere else (under a docked picture) adds one.
  let stripSearchFrom = null, recFile = null, recFrom = 0;
  const STRIP_HINT_AFTER = 30000;   // searching this long: say what to check
  const mmss = (s) => { s = Math.max(0, Math.round(s)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };

  // The receiver the strip follows: the one heard, with several.
  function stripRx() {
    if (multiRx()) {
      const r = receivers.find((x) => x.id === rxView.selected) || receivers[0];
      if (r) return { ...r, where: [fmt(r.frequencyMHz, 3) + " MHz", r.symbolRateKs + " kS"], id: r.id };
    }
    return { ...lastRx, enabled: cfg.rxEnabled !== false, id: 1,
             where: [fmt(cfg.rxFrequencyMHz, 3) + " MHz", (cfg.rxSymbolRateKs ?? "—") + " kS"] };
  }

  function stripHtml(r) {
    const where = `<span class="smono">${esc(r.where.join(" · "))}</span>`;
    const who = multiRx() ? `<span class="srx">Rx ${r.id}</span>` : "";
    if (r.enabled === false) return `${who}<b class="sst">Receiver off</b><span class="sdim">transmit only</span>`;
    if (!r.locked) {
      const searching = !r.label || r.label === "Searching";
      if (!searching) { stripSearchFrom = null; return `${who}<b class="sst">${esc(r.label)}</b>${where}`; }
      stripSearchFrom = stripSearchFrom || Date.now();
      const long = Date.now() - stripSearchFrom > STRIP_HINT_AFTER;
      const qo = typeof onQo === "function" && onQo();
      // That the tuner is there and what it is feeding, so "Searching"
      // says the receiver works and is simply hearing nothing.
      const lnb = r.id === 1 && qo && cfg.rxLnbSupply && cfg.rxLnbSupply !== "off" ? `LNB ${cfg.rxLnbSupply} V on` : "";
      const kit = [r.usbTuner || r.tuner, lnb].filter(Boolean).join(" · ");
      return `${who}<b class="sst">Searching</b>${where}<span class="sdim">no signal yet</span>`
        + (kit ? `<span class="sdim">${esc(kit)}</span>` : "")
        + (long ? `<span class="shint">${qo ? "Check the LNB supply, or try the beacon" : "Check the antenna, frequency and symbol rate"}</span>` : "");
    }
    stripSearchFrom = null;
    const m = r.marginDb;
    const margin = m != null
      ? `<span class="smargin ${esc(r.band || "")}">margin ${m < 0 ? "−" : "+"}${Math.abs(m).toFixed(1)} dB${m >= 0 ? ` (D${m.toFixed(1)})` : ""}</span>` : "";
    return `${who}<span class="sdot"></span><b class="sst">Locked</b>`
      + (r.name ? `<b class="swho">${esc(r.name)}</b>` : "") + where
      + (r.mer != null ? `<span class="smer">MER ${r.mer.toFixed(1)} dB</span>` : "") + margin
      + (r.modcod ? `<span class="sdim">${esc(r.modcod)}</span>` : "");
  }

  function paintStrips() {
    const r = stripRx();
    const rec = (lastRx && lastRx.recording) || null;
    if (rec && rec.file) { if (rec.file !== recFile) { recFile = rec.file; recFrom = Date.now(); } }
    else recFile = null;
    const html = stripHtml(r) + (recFile ? `<span class="srec">● REC ${mmss((Date.now() - recFrom) / 1000)}</span>` : "")
      + `<span class="sgo">Signal ›</span>`;
    for (const el of document.querySelectorAll(".rxstrip")) {
      el.classList.toggle("locked", !!r.locked);
      el.classList.toggle("off", r.enabled === false);
      if (el.dataset.html !== html) { el.dataset.html = html; el.innerHTML = html; }
    }
    const pill = $("recpill");
    pill.classList.toggle("on", !!recFile);
    pill.textContent = recFile ? `● REC ${mmss((Date.now() - recFrom) / 1000)}` : "";
  }
  document.addEventListener("click", (e) => {
    if (e.target.closest && e.target.closest(".rxstrip, #recpill")) $("sigbtn").click();
  });
