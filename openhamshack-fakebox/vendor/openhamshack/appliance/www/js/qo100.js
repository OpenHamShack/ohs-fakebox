  // QO-100 page. /api/qo100 is polled only while the page is open: the
  // daemon holds its connection to BATC for as long as it is polled, and
  // drops it soon after, so leaving the page is what lets it go.
  const QO_OFFSET = 8089.5;          // downlink = uplink + 8089.5 (also in /api/qo100)
  const QO_UP = [2401.0, 2410.0];    // the wideband transponder's uplink
  let qoOpen = false, qo = null, qoTuning = null, lnbArm = null;
  const qoLo = () => Number(cfg.qo100LnbMHz) || 9750;
  // Through the LNB on QO-100's downlink. An offset alone is not enough:
  // HamTV's downconverter is an offset too, and its LO is no LNB's.
  const onQo = () => Number(cfg.rxOffsetMHz) > 0 && Number(cfg.rxFrequencyMHz) >= 10000;

  function showQo(open) {
    qoOpen = open;
    if (open) { showSettings(false); showAim(false); showHam(false); if (typeof showLog === "function") showLog(false); }
    document.body.classList.toggle("inqo", open);
    if (open) { $("qo").scrollTop = 0; paintQo(); loadQo(); if (chatOpen) loadChat(); }
  }
  $("qobtn").onclick = () => showQo(true);

  async function loadQo() {
    try { qo = await (await fetch(`/api/qo100?wf=${wfSeq}`)).json(); }
    catch (e) { qo = { error: "OpenHamShack is not answering", signals: [], spectrum: [] }; }
    if (qo.waterfall) drawWaterfall(qo.waterfall);
    paintQo();
  }

  // Waterfall: newest row at the top, one row a second (the daemon keeps
  // the last three minutes and sends only what this page has not drawn).
  let wfSeq = 0;
  const WF_LUT = (() => {
    // Floor (0 dB) dark, through blue and cyan to yellow and red at 15 dB
    // and over: the beacon and a strong station stand out, the noise not.
    const stops = [[-3, [8, 12, 28]], [0.5, [10, 20, 70]], [3, [20, 70, 190]], [6, [20, 190, 220]],
                   [10, [250, 230, 60]], [15, [240, 60, 30]]];
    const lut = new Uint8ClampedArray(256 * 3);
    for (let b = 0; b < 256; b++) {
      const db = -3 + b * 0.125;
      let k = 0; while (k < stops.length - 2 && db > stops[k + 1][0]) k++;
      const [d0, c0] = stops[k], [d1, c1] = stops[k + 1], t = Math.max(0, Math.min(1, (db - d0) / (d1 - d0)));
      for (let j = 0; j < 3; j++) lut[b * 3 + j] = c0[j] + (c1[j] - c0[j]) * t;
    }
    return lut;
  })();
  function drawWaterfall(wf) {
    const c = $("qowfc"), ctx = c.getContext("2d");
    if (wf.full) { ctx.fillStyle = "#0b1020"; ctx.fillRect(0, 0, c.width, c.height); }
    wfSeq = wf.seq;
    const rows = wf.rows.slice(-c.height), n = rows.length;
    if (!n) return;
    const w = atob(rows[0]).length;
    if (c.width !== w) { c.width = w; ctx.fillStyle = "#0b1020"; ctx.fillRect(0, 0, w, c.height); }
    if (n < c.height) ctx.putImageData(ctx.getImageData(0, 0, w, c.height - n), 0, n);
    const img = ctx.createImageData(w, n);
    rows.forEach((r, i) => {
      const bytes = atob(r), y = n - 1 - i;      // the last row sent is the newest: top
      for (let x = 0; x < w; x++) {
        const b = bytes.charCodeAt(x), o = (y * w + x) * 4;
        img.data[o] = WF_LUT[b * 3]; img.data[o + 1] = WF_LUT[b * 3 + 1]; img.data[o + 2] = WF_LUT[b * 3 + 2]; img.data[o + 3] = 255;
      }
    });
    ctx.putImageData(img, 0, 0);
  }
  // Tap: tune a signal found there now, as on the spectrum; with nothing
  // found there (a trace from a minute ago, or noise), offer the frequency
  // rather than tune to it unasked.
  $("qowf").addEventListener("click", (ev) => {
    const r = $("qowf").getBoundingClientRect();
    const lo = qo && qo.startMHz || 10490.5, hi = qo && qo.stopMHz || 10499.5;
    const f = lo + (ev.clientX - r.left) / r.width * (hi - lo);
    const sigs = (qo && qo.signals) || [];
    const i = sigs.findIndex((s) => f >= s.lowMHz - 0.1 && f <= s.highMHz + 0.1);
    const ask = $("qowfask");
    if (i >= 0) {
      ask.className = "";
      return qoTune(sigs[i].freqMHz, sigs[i].tuneSymbolRateKs, `${sigs[i].freqMHz.toFixed(3)} · ${sigs[i].tuneSymbolRateKs} kS`);
    }
    // The receiver's own rate, unless it is still on the beacon's 1500 kS: most stations are 333 or less.
    const fr = Math.round(f * 80) / 80, cur = Number(cfg.rxSymbolRateKs) || 333, sr = cur > 1000 ? 333 : cur;
    ask.className = "on";
    ask.innerHTML = `<span>No signal found at <b>${fr.toFixed(3)} MHz</b> now.</span>`
      + `<button class="chip" id="qowfgo"${auth.authorised ? "" : " disabled"}>Receive ${fr.toFixed(3)} at ${sr} kS</button>`
      + `<button class="chip ro" id="qowfno">Cancel</button>`;
    $("qowfgo").onclick = () => { ask.className = ""; qoTune(fr, sr, `${fr.toFixed(3)} · ${sr} kS`); };
    $("qowfno").onclick = () => { ask.className = ""; };
  });
  setInterval(() => { if (qoOpen) loadQo(); }, 1000);

  // Tune the receiver through the LNB. One save, so the daemon restarts the
  // tuner once (apply_config) exactly as for any receive change.
  let qoRx = 1;   // the receiver the page's Receive box follows: the one last tuned from here
  function qoTune(freq, sr, label) {
    pickReceiver(`Receive ${label || Number(freq).toFixed(3)} on`, (id) => {
      qoRx = id;
      qoTuning = { at: Date.now(), text: `Tuning ${label || Number(freq).toFixed(3)}…` };
      if (id !== 1) {
        const r = receivers.find((x) => x.id === id) || {};
        const patch = { frequencyMHz: Number(freq), offsetMHz: Number(r.offsetMHz) > 0 ? Number(r.offsetMHz) : qoLo() };
        if (sr) patch.symbolRateKs = Number(sr);
        return rxTune(id, patch);
      }
      const patch = { rxFrequencyMHz: Number(freq), rxOffsetMHz: onQo() ? Number(cfg.rxOffsetMHz) : qoLo() };
      if (sr) patch.rxSymbolRateKs = Number(sr);
      save(patch);
    });
  }

  document.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    if (b.dataset.page === "qo") {
      for (const m of document.querySelectorAll(".modal.open")) m.classList.remove("open");   // Tune… or a sheet
      return showQo(true);
    }
    if (b.dataset.qorx) return qoTune(b.dataset.qorx, b.dataset.qosr, b.textContent);
    if (b.dataset.sig !== undefined && qo) {
      const s = qo.signals[Number(b.dataset.sig)];
      if (s) qoTune(s.freqMHz, s.tuneSymbolRateKs, `${s.freqMHz.toFixed(3)} · ${s.tuneSymbolRateKs} kS`);
      return;
    }
    if (b.dataset.lnbv) {
      // DC on the coax: a second tap within 4 s, as for TRANSMIT, so a
      // stray touch cannot put 18 V on the cable. Off is immediate.
      const v = b.dataset.lnbv;
      if (v !== "off" && lnbArm !== v) {
        lnbArm = v; paintQo();
        setTimeout(() => { if (lnbArm === v) { lnbArm = null; paintQo(); } }, 4000);
        return;
      }
      lnbArm = null;
      return save({ rxLnbSupply: v });
    }
    if (b.dataset.qotxsr) {
      const sr = Number(b.dataset.qotxsr), chans = qoChannels(sr);
      const patch = { symbolRateKs: sr };
      // Keep the channel if the plan has it at this rate, else move to the
      // rate's first choice: changing rate never leaves the box off-plan.
      if (chans.length && !chans.some((c) => Math.abs(c.uplinkMHz - cfg.frequencyMHz) < 0.001))
        patch.frequencyMHz = (chans.find((c) => c.preferred && sr <= 333) || chans[Math.floor(chans.length / 2)]).uplinkMHz;
      return save(patch);
    }
    if (b.dataset.qotxch) {
      const csr = Number(b.dataset.qotxchsr);
      return save({ frequencyMHz: Number(b.dataset.qotxch), ...(csr && csr !== Number(cfg.symbolRateKs) ? { symbolRateKs: csr } : {}) });
    }
    if (b.id === "qoself") return qoTune(Number(cfg.frequencyMHz) + QO_OFFSET, cfg.symbolRateKs, "own uplink");
    if (b.id === "qoqpsk") return save({ mode: "dvbs2", constellation: "qpsk", fec: FEC.dvbs2.includes(cfg.fec) ? cfg.fec : "2/3" });
  });

  // The band plan's channels come from the daemon (qo100_channels), the
  // same table its transmit guard is tested against, rather than a second
  // copy here that could drift from it.
  function qoChannels(sr) {
    if (qo && qo.channels) return qo.channels[String(sr)] || [];
    return [];
  }

  function drawSpectrum() {
    // T leaves room for three rows of labels: stations 0.5 MHz apart are
    // about 90 px apart here, and a label is twice that.
    // H: a shorter drawing than it was (300), so the downlink and uplink fit
    // under it at 1920; T still leaves the three rows of labels.
    const W = 1600, H = 190, T = 64, B = 32, L = 8, R = 8;
    const lo = qo && qo.startMHz || 10490.5, hi = qo && qo.stopMHz || 10499.5;
    const S = (qo && qo.spectrum) || [], sigs = (qo && qo.signals) || [];
    const top = Math.max(12, ...S.map((v) => v + 1)), bot = -2;
    const x = (f) => L + (f - lo) / (hi - lo) * (W - L - R), y = (v) => T + (top - v) / (top - bot) * (H - T - B);
    const out = [];
    // The band plan's segments: beacon only, wide and narrow, narrow only.
    out.push(`<rect x="${x(lo)}" y="${T}" width="${x(10492.5) - x(lo)}" height="${H - T - B}" fill="var(--chip)"/>`);
    for (const f of [10492.5, 10497])
      out.push(`<line x1="${x(f)}" x2="${x(f)}" y1="${T}" y2="${H - B}" stroke="var(--dim)" stroke-dasharray="6 6"/>`);
    for (let f = Math.ceil(lo); f <= hi; f++)
      out.push(`<text x="${x(f)}" y="${H - 8}" text-anchor="middle" font-size="20" fill="var(--dim)">${f}</text>`);
    if (S.length) {
      const pts = S.map((v, i) => `${x(lo + (i + 0.5) / S.length * (hi - lo)).toFixed(1)},${y(v).toFixed(1)}`);
      out.push(`<path d="M${x(lo)},${H - B} L${pts.join(" L")} L${x(hi)},${H - B} Z" fill="#d6e2f7" stroke="var(--sel)" stroke-width="2"/>`);
    }
    // Channel guides for the transmit symbol rate (the band plan's, from the
    // daemon), and where our own signal would land: the same footprint the
    // busy check at key-up uses, so a clash is visible before keying.
    {
      const tsr = Number(cfg.symbolRateKs), tf = Number(cfg.frequencyMHz);
      for (const c of qoChannels(tsr)) {
        const cx = x(c.downlinkMHz);
        out.push(`<line x1="${cx}" x2="${cx}" y1="${H - B - 14}" y2="${H - B}" stroke="${c.preferred ? "var(--ok)" : "var(--dim)"}" stroke-width="${c.preferred ? 3 : 2}"/>`);
      }
      if (tf >= QO_UP[0] && tf <= QO_UP[1]) {
        const d = tf + QO_OFFSET, hw = tsr * 1.35 / 2000;
        out.push(`<rect x="${x(d - hw)}" y="${T}" width="${Math.max(3, x(d + hw) - x(d - hw))}" height="${H - B - T}" fill="rgba(230,120,20,.12)" stroke="#e67814" stroke-width="2"/>`,
                 `<text x="${x(d)}" y="${H - B - 18}" text-anchor="middle" font-size="18" font-weight="700" fill="#e67814">TX</text>`);
      }
    }
    if (onQo()) {
      const f = Number(cfg.rxFrequencyMHz);
      if (f >= lo && f <= hi) out.push(`<line x1="${x(f)}" x2="${x(f)}" y1="${T - 30}" y2="${H - B}" stroke="var(--ok)" stroke-width="3"/>`);
    }
    const placed = [];  // label boxes already drawn: [left, right, row]
    sigs.forEach((s, i) => {
      const x0 = x(s.lowMHz), x1 = x(s.highMHz), yt = y(s.levelDb), mid = (x0 + x1) / 2;
      const label = s.beacon ? "Beacon" : s.overPower ? "[over-power]" : `${s.symbolRateKs} kS ${s.freqMHz.toFixed(3)}`;
      // Labels of neighbouring stations overlapped into one unreadable line;
      // each goes on the lowest row where it clears those already drawn.
      const half = label.length * 6 + 6, cx = Math.min(W - half, Math.max(half, mid));
      let row = 0;
      while (row < 2 && placed.some(([a, b, r]) => r === row && cx - half < b && cx + half > a)) row++;
      placed.push([cx - half, cx + half, row]);
      const ty = Math.max(T - 6, yt - 8) - row * 24;
      out.push(`<rect x="${x0}" y="${yt}" width="${Math.max(4, x1 - x0)}" height="${H - B - yt}" fill="transparent" stroke="${s.overPower ? "var(--air)" : "var(--ink)"}" stroke-width="2" stroke-dasharray="4 4"/>`,
               `<text x="${cx}" y="${ty}" text-anchor="middle" font-size="20" font-weight="700" fill="${s.overPower ? "var(--air)" : "var(--ink)"}">${esc(label)}</text>`);
    });
    $("qospec").innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="QO-100 wideband spectrum">${out.join("")}</svg>`;
    // Tap anywhere over a signal (its width, generously) to tune it.
    $("qospec").firstChild.addEventListener("click", (ev) => {
      const r = ev.currentTarget.getBoundingClientRect(), f = lo + ((ev.clientX - r.left) / r.width * W - L) / (W - L - R) * (hi - lo);
      const i = sigs.findIndex((s) => f >= s.lowMHz - 0.1 && f <= s.highMHz + 0.1);
      if (i >= 0) qoTune(sigs[i].freqMHz, sigs[i].tuneSymbolRateKs, `${sigs[i].freqMHz.toFixed(3)} · ${sigs[i].tuneSymbolRateKs} kS`);
    });
  }

  function paintQo() {
    if (!qoOpen) return;
    // Spectrum and its signals.
    const st = $("qostate");
    st.className = qo && qo.error && !qo.live ? "err" : "dim";
    st.textContent = !qo ? "Connecting to BATC…"
      : qo.live ? `Live from BATC's monitor at Goonhilly · ${qo.signals.length} signal${qo.signals.length === 1 ? "" : "s"}: tap one to receive it`
        + `${qo.outOfBand ? " · something out of band at the top" : ""}`
      : qo.error ? qo.error + " - the controls below still work."
      : qo.age != null ? `No new spectrum for ${Math.round(qo.age)} s` : "Connecting to BATC…";
    drawSpectrum();
    {
      // The receiver's frequency down the waterfall, as on the spectrum.
      const lo = qo && qo.startMHz || 10490.5, hi = qo && qo.stopMHz || 10499.5, f = Number(cfg.rxFrequencyMHz);
      const mark = $("qowfrx"), show = onQo() && f >= lo && f <= hi;
      mark.style.display = show ? "block" : "none";
      if (show) mark.style.left = ((f - lo) / (hi - lo) * 100).toFixed(2) + "%";
    }
    const sigs = (qo && qo.signals) || [];
    // Each signal says which receivers are on it (with several).
    const tunedOn = (s) => (multiRx() ? receivers.filter((r) => r.enabled)
      : [{ id: 1, frequencyMHz: cfg.rxFrequencyMHz, symbolRateKs: cfg.rxSymbolRateKs, offsetMHz: cfg.rxOffsetMHz }])
      .filter((r) => Number(r.offsetMHz) > 0 && Math.abs(Number(r.frequencyMHz) - s.freqMHz) < 0.02
              && Number(r.symbolRateKs) === s.tuneSymbolRateKs).map((r) => r.id);
    const sigKey = JSON.stringify(sigs.map((s) => [s.freqMHz, s.tuneSymbolRateKs, s.overPower, tunedOn(s)])) + multiRx();
    if ($("qosigs").dataset.sig !== sigKey) {
      $("qosigs").dataset.sig = sigKey;
      $("qosigs").innerHTML = sigs.map((s, i) => {
        const on = tunedOn(s);
        return `<button class="chip${on.length ? " on" : ""}${s.overPower ? " over" : ""}" data-sig="${i}">`
          + `${s.beacon ? "Beacon " : ""}${s.freqMHz.toFixed(3)} · ${s.tuneSymbolRateKs} kS${s.overPower ? " · over-power" : ""}`
          + `${multiRx() && on.length ? " · Rx " + on.join(", ") : ""}</button>`;
      }).join("");
    }

    // Receive: the receiver last tuned from this page.
    const rx = (multiRx() && qoRx !== 1 && receivers.find((r) => r.id === qoRx)) || lastRx || {};
    $("qorx").className = rx.locked ? "locked" : "";
    const waiting = qoTuning && !rx.locked && Date.now() - qoTuning.at < 15000;
    $("qorxlabel").textContent = waiting ? qoTuning.text : rx.label || "—";
    $("qorxdetail").textContent = rx.locked
      ? [multiRx() ? "Receiver " + (rx.id || 1) : "", rx.mer != null ? "MER " + rx.mer.toFixed(1) + " dB" : "", rx.name, rx.provider]
          .filter(Boolean).join("  ·  ") : "";
    if (rx.locked || !onQo()) qoTuning = null;
    $("qoRxFreq").innerHTML = fmt(cfg.rxFrequencyMHz, 3) + "<small>MHz</small>";
    for (const b of $("qochan").children)
      b.classList.toggle("on", onQo() && Number(b.dataset.qorx) === Number(cfg.rxFrequencyMHz));
    const lo = qoLo(), known = lo === 9750;
    $("qoLnbOther").textContent = known ? "Other…" : fmt(lo, 3).replace(/\.?0+$/, "");
    $("qoLnbOther").classList.toggle("on", !known);
    const ifMHz = Number(cfg.rxFrequencyMHz) - Number(cfg.rxOffsetMHz);
    $("qomode").innerHTML = onQo()
      ? `Receiving through the LNB: LO <b>${Number(cfg.rxOffsetMHz)} MHz</b>, tuner at <b>${fmt(ifMHz, 3)} MHz</b>.`
      : "The receiver is on a terrestrial frequency (no LNB). Tap a signal or channel to receive QO-100.";
    const supply = String(cfg.rxLnbSupply || "off");
    for (const b of $("qolnbv").children) {
      const v = b.dataset.lnbv;
      b.classList.toggle("on", v === supply);
      b.classList.toggle("arm", v === lnbArm);
      b.textContent = v === lnbArm ? `Tap again for ${v} V` : v === "off" ? "Off" : `${v} V`;
      b.disabled = v !== "off" && !onQo();
    }
    if (supply !== "off" && cfg.rxTuner === "picotuner")
      $("qomode").innerHTML += " <b>PicoTuner on the network: the LNB supply is not sent; power the LNB another way.</b>";

    // Transmit.
    const f = Number(cfg.frequencyMHz), sr = Number(cfg.symbolRateKs);
    $("qoTxFreq").innerHTML = fmt(f, 3) + "<small>MHz</small>";
    const onSat = f >= QO_UP[0] && f <= QO_UP[1];
    $("qodown").innerHTML = onSat ? `Comes down at <b>${(f + QO_OFFSET).toFixed(3)} MHz</b> (uplink + ${QO_OFFSET}).` : "";
    // Off the transponder is worth an amber box, not a grey line: it is
    // why TRANSMIT will refuse.
    $("qonotup").hidden = onSat;
    $("qonotup").innerHTML = `<b>${fmt(f, 3)} MHz is not a QO-100 uplink</b> (${QO_UP[0].toFixed(1)}-${QO_UP[1].toFixed(1)} MHz).`
      + " Pick a channel below to move onto the transponder.";
    for (const b of $("qotxsr").children) b.classList.toggle("on", Number(b.dataset.qotxsr) === sr);
    // The channels at once, whatever the rate: at a rate the band plan does
    // not use (a terrestrial 2000 kS) they are 333 kS's, and picking one
    // sets 333 kS too. The row used to stay empty until a rate was chosen.
    const gridSr = qoChannels(sr).length ? sr : 333;
    // !!qo in the key: drawn before the band plan arrived, the panel kept
    // "Loading the band plan…" for good when the rate had no channels.
    const chans = qoChannels(gridSr), chKey = gridSr + JSON.stringify(chans.map((c) => c.uplinkMHz)) + f + sr + !!qo;
    if ($("qotxch").dataset.sig !== chKey) {
      $("qotxch").dataset.sig = chKey;
      $("qotxch").innerHTML = chans.length ? chans.map((c) => `<button class="chip${Math.abs(c.uplinkMHz - f) < 0.001 && gridSr === sr ? " on" : ""}${c.preferred && gridSr <= 333 ? " pref" : ""}" `
        + `data-qotxch="${c.uplinkMHz}" data-qotxchsr="${gridSr}">${c.downlinkMHz.toFixed(2)}</button>`).join("")
        : `<span class="dim">${qo ? "No channels in the band plan." : "Loading the band plan…"}</span>`;
    }
    $("qotxnote").innerHTML = (gridSr !== sr && chans.length ? `<b>Shown at ${gridSr} kS</b>: ${sr} kS is not a QO-100 rate, and picking a channel sets ${gridSr} kS. ` : "")
      + (gridSr <= 333 && chans.length ? "<b>Green:</b> the band plan's first choice at this rate (above 10497); move down only when those are busy. " : "")
      + (sr === 500 ? "500 kS stays below 10497. " : "")
      + "BATC asks for DVB-S2, at least 1 dB below the beacon; 8PSK and above only at low rates.";
    const qpsk = cfg.mode === "dvbs2" && cfg.constellation === "qpsk";
    $("qoqpsk").classList.toggle("on", qpsk);
    $("qoqpsk").textContent = qpsk ? `DVB-S2 QPSK ${cfg.fec}` : "Use DVB-S2 QPSK";
    $("qoself").disabled = !onSat;
    $("qoblocked").textContent = txBlocked ? "Can't transmit: " + txBlocked : "";
    paintChat();
  }

  // BATC chat. Polled every 2 s while the page is open; only messages this
  // page has not drawn come back (since). Nothing is posted without the
  // operator seeing the words and tapping Send: a typed message or a
  // drafted signal report sits in the draft box until then.
  let chat = null, chatSeq = 0, chatDraft = null, chatBusy = false;
  async function loadChat() {
    try {
      const c = await (await fetch(`/api/qo100/chat?since=${chatSeq}`)).json();
      const log = $("qochatlog"), atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
      if (c.full) log.innerHTML = "";
      if (c.messages.length) {
        log.insertAdjacentHTML("beforeend", c.messages.map((m) => {
          const t = new Date(m.time), when = isNaN(t) ? "" : t.toTimeString().slice(0, 5);
          return `<div class="${m.name === c.nick ? "me" : ""}"><span class="t">${when}</span><span class="n">${esc(m.name)}</span>${esc(m.message)}</div>`;
        }).join(""));
        while (log.children.length > 200) log.firstChild.remove();
        if (atEnd || c.full) log.scrollTop = log.scrollHeight;
      }
      chatSeq = c.seq;
      chat = c;
    } catch (e) { chat = { error: "OpenHamShack is not answering", connected: false }; }
    paintChat();
  }
  setInterval(() => { if (qoOpen && chatOpen) loadChat(); }, 2000);
  // The chat folds away under the rest, so the page fits a screen; opened,
  // it stays open in this browser (a convenience: blocked storage starts
  // folded). Folded, it is not read at all - BATC's chat connection is the
  // daemon's only while somebody is reading it.
  let chatOpen = false;
  try { chatOpen = localStorage.getItem("lbQoChat") === "open"; } catch (e) { /* folded */ }
  function openChat(open) {
    chatOpen = open;
    try { localStorage.setItem("lbQoChat", open ? "open" : "folded"); } catch (e) { /* this page only */ }
    $("qochat").classList.toggle("folded", !open);
    $("qochatfold").setAttribute("aria-expanded", String(open));
    $("qochatfold").textContent = open ? "Fold the chat away" : "Open the chat";
    if (open && qoOpen) loadChat();
  }
  openChat(chatOpen);
  $("qochatfold").onclick = () => openChat(!chatOpen);

  function paintChat() {
    if (!qoOpen) return;
    const c = chat || {};
    $("qochatstate").className = c.error && !c.connected ? "err" : "dim";
    $("qochatstate").textContent = !chat ? "Connecting to BATC…" : c.connected
      ? `${c.viewers != null ? c.viewers + " watching · " : ""}${(c.nicks || []).length} named` : (c.error || "Connecting to BATC…");
    const can = auth.authorised && c.connected && c.nick;
    $("qochatwrite").disabled = !can;
    // Sorted case-blind, the station's own name marked; redrawn only when
    // the list changes so a tap is not lost to a repaint.
    const nicks = [...(c.nicks || [])].sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
    const nkey = nicks.join("\n") + "|" + (c.nick || "") + "|" + can;
    if ($("qochatuserlist").dataset.k !== nkey) {
      $("qochatuserlist").dataset.k = nkey;
      $("qochatuserhead").textContent = nicks.length ? `In the chat (${nicks.length})` : "No one named yet";
      $("qochatuserlist").innerHTML = nicks.map((n) => `<button data-nick="${esc(n)}"${n === c.nick ? ' class="me"' : ""}`
        + `${can && n !== c.nick ? "" : " disabled"}>${esc(n)}</button>`).join("");
    }
    const rx = lastRx || {};
    $("qoreport").disabled = !can || !(rx.locked && onQo() && cfg.rxTuner !== "picotuner");
    $("qochatname").disabled = !auth.authorised;
    $("qochatname").textContent = cfg.chatName ? `Name: ${cfg.chatName}` : "Name…";
    $("qochatnote").innerHTML = (c.nick ? `Posts go to BATC's public chat as <b>${esc(c.nick)}</b>, only when Send is tapped. `
        : esc(c.postable || "") + ". ")
      + "BATC moderate it under their terms of service; OpenHamShack allows one message per 5 s, four a minute, "
      + "and a report on the same station once a minute. Chat-only page: eshail.batc.org.uk/wb/chat.";
    $("qochatdraft").className = chatDraft ? "on" : "";
    if (chatDraft) {
      $("qochattext").textContent = chatDraft.text;
      $("qochatsend").textContent = chatDraft.armed ? "Tap again to send" : "Send";
      $("qochatsend").classList.toggle("arm", !!chatDraft.armed);
      $("qochatsend").disabled = !can || chatBusy;
    }
  }

  function draftChat(text, report) {
    chatDraft = { text, report: report || null, armed: false };
    $("qochaterr").textContent = "";
    paintChat();
  }
  function editChat() {
    if (!canChange()) return;
    Keyboard.open({ title: `Message to the QO-100 wideband chat, as ${chat && chat.nick || "the station"}`,
      value: chatDraft ? chatDraft.text : "", maxLength: 300, symbols: true,
      onOk: (v) => { v = v.trim(); if (v) draftChat(v, chatDraft && chatDraft.report); } });
  }
  async function reportDraft() {
    $("qochaterr").textContent = "";
    try {
      const d = await (await fetch("/api/qo100/report")).json();
      if (!d.ok) { $("qochaterr").textContent = d.error; return; }
      draftChat(d.text, d.station);
    } catch (e) { $("qochaterr").textContent = "OpenHamShack is not answering"; }
  }
  $("qochatwrite").onclick = () => { chatDraft = null; editChat(); };
  // Tap a name: a message to them, opened on the keyboard to finish.
  $("qochatuserlist").addEventListener("click", (e) => {
    const n = e.target.dataset && e.target.dataset.nick;
    if (!n || e.target.disabled) return;
    chatDraft = { text: `@${n} `, report: null, armed: false };
    editChat();
  });
  $("qochatedit").onclick = editChat;
  $("qochatdrop").onclick = () => { chatDraft = null; paintChat(); };
  $("qoreport").onclick = reportDraft;
  $("qochatname").onclick = () => {
    if (!canChange()) return;
    Keyboard.open({ title: "Your name in the chat, after the callsign (e.g. Brian, for GM4BRI_Brian); empty for none",
      value: cfg.chatName || "", maxLength: 12, shift: true, onOk: (v) => save({ chatName: v.trim() }) });
  };
  // A second tap within 4 s sends, as for TRANSMIT: the words are public.
  $("qochatsend").onclick = async () => {
    if (!chatDraft || chatBusy) return;
    if (!chatDraft.armed) {
      chatDraft.armed = true; paintChat();
      const d = chatDraft;
      setTimeout(() => { if (chatDraft === d && d.armed) { d.armed = false; paintChat(); } }, 4000);
      return;
    }
    chatBusy = true; paintChat();
    try {
      const r = await fetch("/api/qo100/chat", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: chatDraft.text, report: chatDraft.report }) });
      const body = await r.json();
      if (r.ok) chatDraft = null;
      else { chatDraft.armed = false; $("qochaterr").textContent = body.error || "Not sent"; }
    } catch (e) { $("qochaterr").textContent = "OpenHamShack is not answering"; }
    chatBusy = false;
    loadChat();
  };

  // The main page's receive panel offers the same report while it is on
  // QO-100 and locked: it opens this page with the draft ready, so the
  // words are always seen in the one place they are sent from. Added from
  // here rather than written into the panel, which another change is
  // reshaping; with no #rxbox it simply does not appear.
  if ($("rxbox")) {
    const row = document.createElement("div");
    row.id = "rxreportrow";
    row.style.cssText = "display:none;margin-top:6px";
    row.innerHTML = `<button class="chip" id="rxreport">Send report to the QO-100 chat…</button>`;
    $("rxbox").appendChild(row);
    $("rxreport").onclick = () => { showQo(true); openChat(true); reportDraft(); setTimeout(() => $("qochat").scrollIntoView({ block: "end" }), 50); };
    setInterval(() => {
      const rx = lastRx || {};
      row.style.display = auth.authorised && rx.locked && onQo() && cfg.rxTuner !== "picotuner" ? "block" : "none";
    }, 1000);
  }

  // Settings › Transmission: what the busy check's Pluto fallback is doing,
  // in one line after its explanation (status.qo100Pluto, null while off).
  function paintPlutoFft(p) {
    const el = $("plutofftstate");
    if (!el) return;
    el.textContent = !p ? ""
      : p.error ? `Now: ${p.error}.`
      : !p.on ? "Now: waiting to switch the Pluto's receiver over (never while on air)."
      : p.age == null || p.age > 5 ? "Now: switched on, no recent picture from the Pluto yet."
      : `Now: receiving the Pluto's spectrum${p.bins && p.spanHz ? ` (${(p.spanHz / p.bins / 1000).toFixed(1)} kHz bins)` : ""}.`
        + (p.last ? ` Last check: ${p.last}.` : "");
  }
