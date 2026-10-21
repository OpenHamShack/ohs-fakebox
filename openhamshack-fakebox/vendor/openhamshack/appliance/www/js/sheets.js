  // The main page's summaries and the windows behind them (docs/ui-review.md,
  // "Proposed restructure of the main console page"): Transmit mode
  // (symbol rate, mode, modulation, FEC, how robust that is), Picture (the
  // source and its rows, size, frame rate) and Tune… for receiver 1 (the
  // receivers' own window, receivers.js). The rows inside them are the ones
  // that were on the page, with the same ids and the same save() rules.

  // Symbol rates by band, said in one place: the band plan's for QO-100,
  // the terrestrial ones elsewhere. The main page, QO-100 and the receiver
  // window each had their own list (33/66 on one, 2000-4000 on another).
  const SR_SETS = {
    qo: { label: "QO-100 rates", rates: [33, 66, 125, 250, 333, 500, 1000, 1500] },
    terrestrial: { label: "Terrestrial rates", rates: [125, 250, 333, 500, 1000, 1500, 2000, 3000, 4000] },
  };
  const txBand = () => Number(cfg.frequencyMHz) >= QO_UP[0] && Number(cfg.frequencyMHz) <= QO_UP[1] ? "qo" : "terrestrial";
  // A row of rate chips: the band's, plus the one in use if it is not
  // among them, and Other… for the pad. attr is the chips' data attribute.
  function srChips(band, current, attr) {
    const set = SR_SETS[band], cur = Number(current);
    const rates = set.rates.includes(cur) || !cur ? set.rates : [...set.rates, cur].sort((a, b) => a - b);
    return `<span class="srband">${set.label}</span>` + rates.map((r) =>
      `<button class="chip${r === cur ? " on" : ""}" ${attr}="${r}">${r}</button>`).join("")
      + `<button class="chip" data-srother="${attr}">Other…</button>`;
  }
  {
    // The QO-100 page's two rows are always the band plan's.
    const qoDown = document.querySelector('#qo [data-key="rxSymbolRateKs"]');
    if (qoDown) qoDown.innerHTML = SR_SETS.qo.rates.map((r) => `<button class="chip" data-v="${r}">${r}</button>`).join("");
    $("qotxsr").innerHTML = SR_SETS.qo.rates.map((r) => `<button class="chip" data-qotxsr="${r}">${r}</button>`).join("");
  }

  const SOURCE_WORDS = { testcard: "Test card", webcam: "Camera", file: "Video file", stream: "Stream (OBS)" };
  function pictureSummary() {
    const src = cfg.source || "testcard", words = [SOURCE_WORDS[src] || src];
    if (src === "testcard" && testcard && testcard.cards) {
      const c = testcard.cards.find((x) => x.id === (testcard.card || cfg.testCard));
      if (c) words.push(c.name);
    } else if (src === "webcam" && cfg.videoDeviceName) words.push(cfg.videoDeviceName);
    else if (src === "file") words.push(cfg.videoFile ? String(cfg.videoFile).split("/").pop() : "none chosen");
    else if (src === "stream") words.push(String(cfg.streamProtocol || "srt").toUpperCase());
    if (tx) words.push(`${tx.width}×${tx.height}${cfg.autoSize !== false ? " (Auto)" : ""}`);
    return words.join(" · ");
  }

  function paintSheets() {
    const s2 = cfg.mode !== "dvbs";
    $("txmodesum").textContent = `${cfg.symbolRateKs ?? "—"} kS · ${s2 ? "DVB-S2" : "DVB-S"} `
      + `${String(cfg.constellation || "qpsk").toUpperCase()} ${cfg.fec || ""}`;
    $("picsum").textContent = pictureSummary();
    // Transmit mode: the rates for where the uplink is.
    const txsr = document.querySelector('#txm [data-key="symbolRateKs"]'), band = txBand();
    const sig = band + cfg.symbolRateKs;
    if (txsr.dataset.sig !== sig) { txsr.dataset.sig = sig; txsr.innerHTML = srChips(band, cfg.symbolRateKs, "data-v"); }
    // How robust: where this setting's threshold sits between the most
    // robust DVB-S2 has (QPSK 1/4) and the least (32APSK 9/10).
    const need = s2 ? modcodNeeds && modcodNeeds.dvbs2[cfg.constellation] && modcodNeeds.dvbs2[cfg.constellation][cfg.fec]
      : modcodNeeds && modcodNeeds.dvbs[cfg.fec];
    $("robust").hidden = need == null;
    if (need != null) {
      const all = Object.values(modcodNeeds.dvbs2).flatMap((o) => Object.values(o)), lo = Math.min(...all), hi = Math.max(...all);
      $("robdot").style.left = `${Math.max(0, Math.min(100, (need - lo) / (hi - lo) * 100)).toFixed(1)}%`;
      $("robtext").textContent = `The far end needs about ${need.toFixed(1)} dB MER to decode this; `
        + `this setting carries ${netBps >= 1e6 ? (netBps / 1e6).toFixed(1) + " Mb/s" : Math.round(netBps / 1000) + " kb/s"}.`;
    }
    const same = hint && cfg.mode === "dvbs2" && cfg.constellation === hint.constellation && cfg.fec === hint.fec;
    $("txmhintrow").hidden = !hint || same;
    if (hint) $("txmhint").textContent = `The station received now could take ${hint.constellation.toUpperCase()} ${hint.fec}.`;
    // Receiver 1, as the Tune… window has it.
    const qo = typeof onQo === "function" && onQo();
    $("rxsum").textContent = cfg.rxEnabled === false ? "Receiver off (transmit only)"
      : `${fmt(cfg.rxFrequencyMHz, 3)} MHz · ${cfg.rxSymbolRateKs} kS${qo ? " · QO-100" : ""}`;
    $("rx1row").hidden = multiRx();    // with several, receiver 1 has a card and a Tune… like the rest
  }

  function openSheet(id) { $(id).classList.add("open"); paintSheets(); }
  $("txmodeopen").onclick = () => openSheet("txm");
  $("picopen").onclick = () => openSheet("picm");
  $("txmclose").onclick = () => $("txm").classList.remove("open");
  $("picmclose").onclick = () => $("picm").classList.remove("open");
  $("txmusehint").onclick = () => { if (hint) save({ mode: "dvbs2", constellation: hint.constellation, fec: hint.fec }); };
  $("rxtune1").onclick = () => {
    if (cfg.rxEnabled === false) return toast("The receiver is off: choose a tuner in Settings › Station.");
    openRxt(1);
  };
  $("rxsig").onclick = () => $("sigbtn").click();
  $("rxaim").onclick = () => showAim(true);
  document.addEventListener("click", (e) => {
    const b = e.target.closest && e.target.closest("button[data-srother]");
    if (!b) return;
    if (!canChange()) return;
    if (b.dataset.srother === "data-rxtsr") {
      const r = rxtR(); if (!r) return;
      return openPad("__rxtsr", `Receiver ${r.id} symbol rate (kS)`, "num", (v) => { const n = Number(v); if (n > 0) rxTune(r.id, { symbolRateKs: n }); });
    }
    openPad("symbolRateKs", "Transmit symbol rate (kS)", "num");
  });

  // A contact waiting to be logged: the first one, with Log, on the main
  // page. Read from /api/log only when the count changes (or every half
  // minute while there are some), never on every poll.
  let logSugAt = 0, logSugN = -1, logSug = null;
  async function loadLogSug() {
    try { const d = await (await fetch("/api/log")).json(); logSug = (d.suggestions || [])[0] || null; }
    catch (e) { /* daemon restarting */ }
    logSugAt = Date.now();
    paintLogSug();
  }
  function paintLogSug() {
    const el = $("logsugline"), s = logPendingN ? logSug : null;
    el.className = s ? "on" : "";
    if (!s) { el.innerHTML = ""; return; }
    const sig = s.id + logPendingN;
    if (el.dataset.sig === sig) return;
    el.dataset.sig = sig;
    el.innerHTML = `<span><b>${esc(s.call)}</b> ${esc(s.summary.replace(/^\S+ /, ""))}</span>`
      + `<button class="chip" id="logsuglog">Log</button><button class="chip ro" id="logsugopen">Log page${logPendingN > 1 ? ` (${logPendingN})` : ""}</button>`;
    $("logsuglog").onclick = async () => {
      if (!canChange()) return;
      const r = await logPost("/api/log/save", { qso: s.entry, fromSuggestion: s.id });
      if (!r.ok) return toast(r.error || "Not logged");
      toast(`${s.call} logged`);
      logSug = null; logSugN = -1; paintLogSug();
    };
    $("logsugopen").onclick = () => showLog(true);
  }
  setInterval(() => {
    if (logPendingN !== logSugN || (logPendingN && Date.now() - logSugAt > 30000)) { logSugN = logPendingN; if (logPendingN) loadLogSug(); else paintLogSug(); }
  }, 1000);
