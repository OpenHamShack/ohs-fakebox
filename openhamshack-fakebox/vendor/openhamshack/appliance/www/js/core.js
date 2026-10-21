  // Shared by every other file: $, the config (cfg) and the state the
  // status poll fills in, save(), render() and refresh() itself. First in
  // index.html's list, so the rest may use all of it at load time.
  const $ = (id) => document.getElementById(id);
  const FEC = {
    dvbs: ["1/2", "2/3", "3/4", "5/6", "7/8"],
    dvbs2: ["1/4", "1/3", "2/5", "1/2", "3/5", "2/3", "3/4", "4/5", "5/6", "8/9", "9/10"],
  };
  // The code rates each modulation really has (status.fecChoices, from the
  // daemon's DVB-S2 table); FEC above stays the full lists, for the log,
  // which records whatever another station sent.
  let fecChoices = null, modcodNeeds = null, netBps = 0, onAirFor = null, txTimeoutLeft = null, logPendingN = 0;
  function fecsFor(mode, constellation) {
    if (mode === "dvbs") return (fecChoices && fecChoices.dvbs) || FEC.dvbs;
    return (fecChoices && fecChoices.dvbs2[constellation]) || FEC.dvbs2;
  }
  // The rate nearest the one asked for that the mode and modulation have:
  // changing modulation never leaves a FEC that cannot be sent.
  function nearestFec(mode, constellation, fec) {
    const list = fecsFor(mode, constellation), val = (f) => { const [n, d] = String(f).split("/").map(Number); return n / d; };
    if (list.includes(fec)) return fec;
    return list.reduce((best, f) => Math.abs(val(f) - val(fec)) < Math.abs(val(best) - val(fec)) ? f : best, list[0]);
  }
  let cfg = {}, onAir = false, armed = null, editing = 0, tx = null, hint = null, previewing = false, lastRx = {};
  let receivers = [], rxView = { selected: 1, enlarged: false };
  let camera = null;   // status.camera: the camera picker (paintCamera)
  let testcard = null; // status.testcard: the cards there are and the one chosen (paintTestcard)

  let auth = { local: true, authorised: true, pinSet: false };
  async function save(patch) {
    if (!canChange()) return;   // a view-only browser is told why (notices.js)
    Object.assign(cfg, patch);
    render();
    editing = Date.now();
    await fetch("/api/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) });
  }

  const fmt = (v, d) => (v === undefined || v === null || v === "") ? "—" : Number(v).toFixed(d);

  function render() {
    $("call").textContent = cfg.callsign || "—";
    $("frequencyMHz").innerHTML = fmt(cfg.frequencyMHz, 3) + "<small>MHz</small>";
    $("gainDb").innerHTML = (cfg.gainDb ?? "—") + "<small>dB</small>";
    $("host").textContent = cfg.host || "address";
    $("picoHost").textContent = cfg.picoHost || "address";
    $("netStreamHost").textContent = cfg.netStreamHost || "address";
    $("netStreamPort").textContent = cfg.netStreamPort || 5004;
    {
      const p = Number(cfg.netStreamPort) || 5004;
      $("netstreamhint").textContent = cfg.netStreamOn && cfg.netStreamHost
        ? `On the PC, in VLC: Media → Open Network Stream → udp://@:${p}` + " (receiver 2: " + (p + 2) + ", 3: " + (p + 4) + ", 4: " + (p + 6) + "). The stream goes out exactly as received."
        : "Sends each received picture, exactly as received, to another computer for VLC or OBS.";
    }
    $("picorow").style.display = cfg.rxTuner === "picotuner" ? "" : "none";
    $("plutorxrow").style.display = $("plutorxexpl").style.display = cfg.rxTuner === "pluto" ? "" : "none";
    const fecs = fecsFor(cfg.mode, cfg.constellation);
    const fecBox = $("fec");
    if (fecBox.dataset.list !== fecs.join()) {
      fecBox.dataset.list = fecs.join();
      fecBox.innerHTML = fecs.map((f) => `<button class="chip" data-v="${f}">${f}</button>`).join("");
    }
    for (const b of $("constellation").children) b.disabled = cfg.mode === "dvbs" && b.dataset.v !== "qpsk";
    for (const box of document.querySelectorAll(".chips[data-key]")) {
      const key = box.dataset.key, num = box.dataset.num;
      for (const b of box.querySelectorAll("[data-v]")) {
        const v = b.dataset.v;
        b.classList.toggle("on", num ? Number(v) === Number(cfg[key]) : v === String(cfg[key] ?? (box.dataset.bool ? true : "")));
      }
    }
    const auto = cfg.autoSize !== false;
    for (const b of $("sizes").children) b.classList.toggle("on", auto ? b.dataset.size === "auto" : b.dataset.size === `${cfg.width}x${cfg.height}`);
    for (const b of $("rates").querySelectorAll("[data-fps]")) {
      b.classList.toggle("on", !auto && Number(b.dataset.fps) === Number(cfg.fps));
      b.disabled = auto;
    }
    $("rateswhy").hidden = !auto;   // a greyed chip says why it is greyed
    // The channel's net rate and the share the picture gets, in one line.
    const rate = (bps) => bps >= 1e6 ? `${(bps / 1e6).toFixed(1)} Mb/s` : `${Math.round(bps / 1000)} kb/s`;
    if (tx) $("txplan").textContent = `This setting carries ${rate(netBps)}: video ${rate(tx.video)} at `
      + `${tx.width}×${tx.height}, ${tx.fps} frames a second${auto ? " (Auto size)" : ""}.`;
    const lic = licRegion(), ukLic = lic === "uk" || lic === "scotland";
    for (const b of document.querySelectorAll("[data-lic]")) b.classList.toggle("on",
      b.dataset.lic === "nonov" ? ukLic && cfg.nov146 === false
        : ukLic && b.dataset.lic !== "iaru1" && b.dataset.lic !== "other" ? cfg.nov146 !== false && lic === b.dataset.lic
        : lic === b.dataset.lic);
    // The same limits the daemon enforces (NOV146_TOP, IARU1_BANDS), shown
    // so the choice reads as what it does rather than as a region.
    if (lic === "iaru1") {
      $("lictop").innerHTML = "Bands from the IARU Region 1 band plan: 2 m 144-146, 70 cm 430-440, 23 cm 1240-1300,"
        + " 13 cm 2300-2450, 9 cm 3400-3410, 6 cm 5650-5850 MHz, 3 cm 10.0-10.5 GHz."
        + " <b>Allocations vary by country: check your own licence.</b>";
    } else if (lic === "other") {
      $("lictop").innerHTML = "OpenHamShack still keeps a QO-100 uplink off the beacon and inside the wideband transponder.";
    } else {
      const top = cfg.nov146 === false ? "146.000" : lic === "uk" ? "147.000" : "146.9375";
      $("lictop").innerHTML = `2 m transmit allowed up to <b>${top} MHz</b>.`
        + (cfg.nov146 === false ? " Without the NoV, 2 m ends at 146." : lic === "uk" ? " The NoV's limit elsewhere in the UK." : " The NoV's limit in Scotland and near it.");
    }
    $("wlictop").innerHTML = $("lictop").innerHTML;
    // "Other" switches the band check off, so it says so wherever the
    // operator looks: here, in the wizard, and under TRANSMIT, for as long
    // as it is chosen - not only at the moment of choosing it.
    for (const id of ["licother", "wlicother"]) {
      $(id).hidden = lic !== "other";
      $(id).innerHTML = "<b>No band check.</b> OpenHamShack will transmit on any frequency the radio can reach."
        + " You are responsible for staying inside your licence.";
    }
    paintPttNote(lic);
    $("setcall").textContent = hasCall() ? cfg.callsign : "Set…";
    $("setloc").textContent = cfg.locator || "Set…";
    // The main page's summaries of what the windows behind them hold.
    if (typeof paintSheets === "function") paintSheets();   // sheets.js
    if (typeof paintQo === "function") paintQo();   // defined in the QO-100 script below
    paintSetup();   // the wizard shows the same settings; returns at once while it is closed
    paintCamera();  // a tapped camera or sound chip lights at once, not at the next poll
    if (typeof paintTestcard === "function") paintTestcard();   // and a tapped card
  }

  // The line under TRANSMIT. Off air, how to key it (or, when it cannot,
  // a way to the setting that stops it); on air, what is going out and for
  // how long - it used to keep saying "tap TRANSMIT twice" while on air.
  // A view-only browser does not see it (openhamshack.css): it cannot key.
  const onAirClock = (s) => { s = Math.max(0, Math.round(s)); const m = Math.floor(s / 60);
    return m >= 60 ? `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`
      : `${m}:${String(s % 60).padStart(2, "0")}`; };
  const BLOCK_GOES = [[/callsign/i, "station", "Station"], [/licen|NoV|amateur band|2 m /i, "station", "Station"],
                      [/PTT|relay|GPO/i, "transmission", "Transmission"]];
  function paintPttNote(lic) {
    // Held by a phone or tablet app's lease (docs/REMOTE-API.md): said, so
    // nobody at the box wonders who keyed it or why it may stop by itself.
    const remote = tx && tx.remote ? tx.remote.id : "";
    const n = $("pttnote"), sig = [onAir, onAirFor, txTimeoutLeft, txBlocked, lic, cfg.frequencyMHz, cfg.symbolRateKs, cfg.gainDb, remote].join("|");
    if (n.dataset.sig === sig) return;
    n.dataset.sig = sig;
    n.classList.toggle("licoff", lic === "other" && !onAir);
    n.classList.toggle("onair", onAir);
    if (onAir) {
      n.textContent = [`On air ${onAirFor != null ? onAirClock(onAirFor) : ""}`.trim(), `${fmt(cfg.frequencyMHz, 3)} MHz`,
        `${cfg.symbolRateKs} kS`, `${cfg.gainDb} dB`, txTimeoutLeft != null ? `time-out in ${onAirClock(txTimeoutLeft)}` : "",
        remote ? "keyed from a remote" : ""]
        .filter(Boolean).join("  ·  ");
      return;
    }
    const go = txBlocked && BLOCK_GOES.find(([re]) => re.test(txBlocked));
    if (go) {
      n.innerHTML = `${esc(txBlocked)} <button class="chip" data-setgo="${go[1]}">Settings › ${go[2]}</button>`;
      return;
    }
    n.textContent = lic === "other"
      ? "Tap TRANSMIT, then tap again within 4 s. No band check (licence: Other): you are responsible for the frequency."
      : "Tap TRANSMIT, then tap again within 4 s to go on air. Transmit only where your licence allows.";
  }

  document.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.setgo) return showSettings(true, b.dataset.setgo);
    const box = b.closest(".chips[data-key]");
    if (box && b.dataset.v !== undefined) {
      const key = box.dataset.key;
      const patch = { [key]: box.dataset.num ? Number(b.dataset.v) : box.dataset.bool ? b.dataset.v === "true" : b.dataset.v };
      if (key === "mode" && b.dataset.v === "dvbs") { patch.constellation = "qpsk"; if (!FEC.dvbs.includes(cfg.fec)) patch.fec = "2/3"; }
      if (key === "mode" && b.dataset.v === "dvbs2") {
        const fec = nearestFec("dvbs2", cfg.constellation, cfg.fec);
        if (fec !== cfg.fec) patch.fec = fec;
      }
      if (key === "constellation") {
        const fec = nearestFec(cfg.mode, b.dataset.v, cfg.fec);
        if (fec !== cfg.fec) patch.fec = fec;
      }
      if (key === "radio") patch.host = b.dataset.v === "pluto" ? "192.168.2.1" : (cfg.host === "192.168.2.1" ? "198.51.100.20" : cfg.host);
      if (b.dataset.lnb !== undefined) patch.rxOffsetMHz = Number(b.dataset.lnb);
      if (key.startsWith("ptt")) return savePtt(patch);
      save(patch);
      return;
    }
    if (b.dataset.size) {
      if (b.dataset.size === "auto") return save({ autoSize: true });
      const [width, height] = b.dataset.size.split("x").map(Number);
      return save({ autoSize: false, width, height, fps: cfg.autoSize === false ? cfg.fps : (tx ? tx.fps : 25) });
    }
    if (b.dataset.fps) return save({ fps: Number(b.dataset.fps) });
    if (b.dataset.cam) return save({ videoDevice: b.dataset.cam });
    if (b.dataset.tc) return save({ testCard: b.dataset.tc });
    if (b.dataset.snd) return save({ audioDevice: b.dataset.snd });
    if (b.id === "rec") return save({ rxRecord: !cfg.rxRecord });
    if (b.dataset.recto) return save({ recordTo: b.dataset.recto });
    if (b.dataset.eject && auth.authorised) {
      b.disabled = true; b.textContent = "Ejecting…";
      fetch("/api/storage/eject", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: b.dataset.eject }) }).then(refresh);
      return;
    }
    // The NoV is a UK matter: "No 146 NoV" from Region 1 or Other means the
    // UK without it, and those two leave nov146 as it was for a return to the UK.
    if (b.dataset.lic) return save(b.dataset.lic === "nonov"
      ? { nov146: false, ...(["uk", "scotland"].includes(licRegion()) ? {} : { licenceRegion: "uk" }) }
      : b.dataset.lic === "iaru1" || b.dataset.lic === "other" ? { licenceRegion: b.dataset.lic }
      : { licenceRegion: b.dataset.lic, nov146: true });
    if (b.id === "usehint" && hint) return save({ mode: "dvbs2", constellation: hint.constellation, fec: hint.fec });
    if (b.dataset.step) save({ gainDb: Math.max(-70, Math.min(0, Number(cfg.gainDb || 0) + Number(b.dataset.step))) });
  });

  async function refresh() {
    try {
      const body = await (await fetch("/api/status")).json();
      onAir = body.onAir;
      // Don't overwrite a value the operator changed a moment ago.
      if (Date.now() - editing > 1500) cfg = body.config;
      netBps = body.netBps || 0;
      fecChoices = body.fecChoices || fecChoices;
      modcodNeeds = body.modcodNeedDb || modcodNeeds;
      logPendingN = body.logPending || 0;
      onAirFor = body.onAirSeconds ?? null;
      txTimeoutLeft = body.onAir && body.txTimeout ? body.txTimeout.secondsLeft : null;
      tx = body.tx || null;
      previewing = !!body.previewing;
      txBlocked = body.txBlocked || "";
      txCheck = body.txCheck || null;
      const rx = body.rx || {};
      lastRx = rx;
      receivers = body.receivers || [];
      rxView = body.rxView || rxView;
      paintReceivers();
      $("rxbox").className = rx.locked ? "locked" : "";
      $("rxlabel").textContent = rx.label || "—";
      $("usbchip").textContent = rx.usbTuner ? `USB: ${rx.usbTuner}` : "USB tuner";
      paintPlutoRx(rx);
      $("rxdetail").textContent = rx.locked
        ? [rx.mer != null ? "MER " + rx.mer.toFixed(1) + " dB" : "",
           rx.video ? `${rx.video.width}×${rx.video.height} ${rx.video.codec}${rx.video.fps ? " " + rx.video.fps + " fps" : ""}` : "",
           rx.name, rx.provider].filter(Boolean).join("  ·  ")
        : "";
      const adv = rx.locked ? rx.advice : null;
      $("rxadvice").textContent = adv ? adv.text : "";
      $("rxadvice").className = adv && adv.options.length ? "warn" : "";
      hint = body.linkHint || null;
      const same = hint && cfg.mode === "dvbs2" && cfg.constellation === hint.constellation && cfg.fec === hint.fec;
      $("hint").style.display = hint && !same ? "flex" : "none";
      if (hint) $("hinttext").textContent =
        `This link carries ${hint.constellation.toUpperCase()} ${hint.fec} (${(hint.netBps / 1e6).toFixed(2)} Mb/s, 3 dB margin)`;
      paintTxError(body);
      paintPlutoFft(body.qo100Pluto);
      paintTxTimeout(body.onAir ? body.txTimeout : null);
      pttOut = body.pttOutput || null;
      paintPttOut();
      paintBattery(body);
      media = body.media || null;
      camera = body.camera || null;
      testcard = body.testcard || null;
      stream = body.stream || null;
      auth = body.auth || auth;
      if (typeof followAim === "function") followAim(!!body.aiming);   // defined in the aiming script below
      if (typeof paintLogBadge === "function") paintLogBadge(body.logPending || 0);   // the Log script below
      const remoteView = !auth.local && !auth.authorised;
      $("remotebar").style.display = remoteView ? "flex" : "none";
      if (remoteView && !$("remotemsg").textContent) $("remotemsg").textContent = auth.pinSet
        ? "Enter the PIN to control this OpenHamShack." : "Set a remote PIN on the box's touchscreen (Settings → Security) to allow control from here.";
      $("remotelogin").style.display = auth.pinSet ? "" : "none";
      document.body.classList.toggle("remoteview", remoteView);
      paintSettings(body);
      paintNet(body.network);
      paintDispPending(body.display || { pending: null });
      // Diagnostics: the last reference sent (from any browser), and the
      // buttons once this browser signs in - repainted only on a change, so
      // the poll never rebuilds a button under a finger.
      const diagSig = JSON.stringify([body.diagSent, auth.local || auth.authorised]);
      if (typeof paintDiag === "function" && diagView && diagSig !== paintDiag.sig) {
        paintDiag.sig = diagSig; diagView.sent = body.diagSent; paintDiag();
      }
      const gb = (n) => n == null ? "" : `${(n / 1e9).toFixed(n < 1e10 ? 1 : 0)} GB free`;
      const st = body.storage || [];
      const sig = JSON.stringify(st.map((d) => [d.name, d.label, Math.round((d.free || 0) / 1e8)])) + cfg.recordTo;
      if ($("recto").dataset.sig !== sig) {
        $("recto").dataset.sig = sig;
        const want = cfg.recordTo || "internal", here = st.find((d) => d.name === want) || st[0];
        // The drive in use as words; the others as plain chips to move to.
        $("recto").innerHTML = (here ? `<span class="recwhere">Records to <b>${esc(here.label)}</b>`
            + `${here.free != null ? ` (${gb(here.free)})` : ""}</span>` : "")
          + st.filter((d) => d !== here).map((d) => `<button class="chip" data-recto="${esc(d.name)}">Record to ${esc(d.label)}`
            + `${d.free != null ? ` <small>${gb(d.free)}</small>` : ""}</button>`).join("")
          + (want !== "internal" && here && here.name === want ? `<button class="chip" data-eject="${esc(want)}">Eject</button>` : "");
      }
      const rec = rx.recording;
      // Recording is a state, not a choice: red, as ON AIR is, not blue.
      $("rec").classList.toggle("rec", !!cfg.rxRecord);
      $("rec").textContent = cfg.rxRecord ? "■ Stop recording" : "● Record";
      $("recinfo").textContent = rec && rec.file ? `REC ${rec.file} · ${(rec.bytes / 1e6).toFixed(1)} MB${rec.note ? " · " + rec.note : ""}`
        : rec && rec.note ? rec.note : cfg.rxRecord ? "Waiting for a signal…" : "";
      render(); paintPtt(); paintMedia(); paintCamera(); paintStream(); paintTestcard();
      paintSetupStatus(body);
      paintLiveOs(body);
      paintAutoUpdate(body);
      paintUpdateBadge(body.updateOffered || null);
      paintStrips();
      paintScreen(body.screens || null);
      paintDensity();
      paintNotices();
    } catch (e) { /* daemon restarting */ }
  }
  // On a phone the page buttons fold into one Pages button beside the
  // page's name: as two rows of eight small chips they took a fifth of the
  // screen before anything else. Choosing a page folds them away again.
  function navOpen(open) {
    document.body.classList.toggle("navopen", open);
    $("navtoggle").setAttribute("aria-expanded", String(open));
  }
  $("navtoggle").onclick = () => navOpen(!document.body.classList.contains("navopen"));
  $("nav").addEventListener("click", (e) => { if (e.target.closest("button")) navOpen(false); });
  // A transmitter or preview error: a notice, with the guide's "If it does
  // not work" a tap away. It used to be red text squeezed into the header,
  // where a long one pushed the page buttons about. Dismissed per message,
  // so the next one still shows.
  let txErrGone = "";
  function paintTxError(body) {
    // The encoder note: transmitting on the processor because the GPU failed its test encode.
    const encNote = (body.encoder && body.encoder.note) || "";
    const err = body.txError || (body.onAir && (body.txMonitor || body.txNote || encNote)) || "", text = err || body.previewNote || "";
    $("txerrhead").textContent = err ? "Transmitter:" : "Preview:";
    $("txerr").textContent = text;
    $("txerrbar").classList.toggle("on", !!text && text !== txErrGone);
  }
  $("txerrok").onclick = () => { txErrGone = $("txerr").textContent; $("txerrbar").classList.remove("on"); paintNotices(); };
  // A newer OpenHamShack the daemon's last look found: a dot on Settings, on its
  // Software & power category and on Updates…, so an update no longer needs
  // finding five taps deep.
  function paintUpdateBadge(ver) {
    const t = ver ? `Update available: OpenHamShack ${ver}` : "";
    for (const id of ["setbadge", "updbadge"]) { $(id).classList.toggle("on", !!ver); $(id).title = t; }
    $("setbtn").title = t;
    const cat = document.querySelector('#setcats [data-cat="software"]');
    if (cat) cat.classList.toggle("hasbadge", !!ver);
    $("updline").textContent = ver ? `OpenHamShack ${ver} is available` : "OpenHamShack, Pluto and PicoTuner firmware";
    $("updline").classList.toggle("news", !!ver);
  }
