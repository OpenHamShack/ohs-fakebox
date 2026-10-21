  // Several receivers. Each has a card in the Receive section: tapping it
  // makes it the one heard and fills the TV with it, tapping it again goes
  // back to the grid (the daemon's select_receiver). Tune… opens that
  // receiver's own settings. With one receiver the section is as it was.
  const rxPost = async (path, body) => {
    if (!canChange()) return null;
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    return r.ok ? j : { error: j.error || "Not done" };
  };
  const multiRx = () => receivers.length > 1;
  function rxLine(r) {
    if (!r.enabled) return "Off";
    if (!r.locked) return r.label || "—";
    return [r.name || "Locked", r.mer != null ? "MER " + r.mer.toFixed(1) + " dB" : ""].filter(Boolean).join(" · ");
  }
  function rxWhere(r) {
    return [r.tuner, fmt(r.frequencyMHz, 3) + " MHz" + (Number(r.offsetMHz) ? " (LNB)" : ""), r.symbolRateKs + " kS",
            r.video && r.locked ? `${r.video.width}×${r.video.height} ${r.video.codec}` : "",
            r.recording && r.recording.file ? "REC" : ""].filter(Boolean).join(" · ");
  }
  function paintReceivers() {
    $("rxsec").classList.toggle("multi", multiRx());
    if (!multiRx()) { $("rxcards").dataset.sig = ""; $("rxcards").innerHTML = ""; return; }
    const sig = JSON.stringify(receivers.map((r) => [r.id, r.enabled, r.locked, r.label, r.name, r.mer, r.frequencyMHz,
      r.symbolRateKs, r.offsetMHz, r.heard, r.enlarged, r.tuner, r.video, r.recording && r.recording.file]));
    if ($("rxcards").dataset.sig !== sig) {
      $("rxcards").dataset.sig = sig;
      $("rxcards").innerHTML = receivers.map((r) => `<div class="rxrow">
        <button class="rxcard${r.locked ? " locked" : ""}${r.heard ? " heard" : ""}${r.enabled ? "" : " off"}" data-rxsel="${r.id}"${r.enabled ? "" : " disabled"}>
          <span class="rxn">${r.id}</span>
          <span class="rxmain"><b>${esc(rxLine(r) ?? "")}</b><small>${esc(rxWhere(r) ?? "")}</small></span>
          <span class="rxbadge">${r.heard ? "♪ heard" : ""}${r.enlarged ? " · full screen" : ""}</span></button>
        <button class="chip ro" data-rxtune="${r.id}">Tune…</button></div>`).join("");
    }
    if (rxtId) paintRxt();
  }

  let rxtId = 0, rxtArm = null;
  const rxtR = () => receivers.find((r) => r.id === rxtId);
  function openRxt(id) { rxtId = id; rxtArm = null; $("rxterr").textContent = ""; paintRxt(); $("rxt").classList.add("open"); }
  function paintRxt() {
    const r = rxtR();
    if (!r) { $("rxt").classList.remove("open"); rxtId = 0; return; }
    $("rxttitle").textContent = `Receiver ${r.id} · ${r.tuner}`;
    $("rxtstate").textContent = rxLine(r);
    $("rxtfreq").innerHTML = fmt(r.frequencyMHz, 3) + "<small>MHz" + (Number(r.offsetMHz) ? " · LNB " + fmt(r.offsetMHz, 0) : "") + "</small>";
    for (const b of $("rxtpresets").children)
      b.classList.toggle("on", Math.abs(Number(b.dataset.rxtf) - Number(r.frequencyMHz)) < 0.001);
    // The band's rates (sheets.js): QO-100's through an LNB, terrestrial otherwise.
    const band = Number(r.offsetMHz) > 0 ? "qo" : "terrestrial", ssig = band + r.symbolRateKs;
    if ($("rxtsr").dataset.sig !== ssig) { $("rxtsr").dataset.sig = ssig; $("rxtsr").innerHTML = srChips(band, r.symbolRateKs, "data-rxtsr"); }
    // A network PicoTuner has no LNB supply to switch, and DC never goes
    // up a terrestrial antenna's cable (the daemon forces it off).
    $("rxtlnbrow").style.display = Number(r.offsetMHz) && r.kind !== "picotuner-net" ? "" : "none";
    for (const b of $("rxtlnb").children) {
      b.classList.toggle("on", b.dataset.rxtlnb === (r.lnbSupply || "off"));
      b.classList.toggle("arm", b.dataset.rxtlnb === rxtArm);
    }
    $("rxtonrow").style.display = r.id === 1 ? "none" : "";
    for (const b of $("rxton").children) b.classList.toggle("on", (b.dataset.rxton === "1") === !!r.enabled);
  }
  async function rxTune(id, patch) {
    const out = await rxPost("/api/rx/tune", { id, ...patch });
    if (!out) return;
    if (out.error) { $("rxterr").textContent = out.error; return; }
    $("rxterr").textContent = "";
    receivers = out.receivers || receivers; rxView = out.rxView || rxView;
    if (out.config) cfg = out.config;
    paintReceivers(); render();
  }
  // Which LNB a typed frequency means, as for receiver 1 (padok): 10 GHz is
  // through the LNB, anything below its LO is not.
  function rxOffsetFor(n, current) {
    const lnb = Number(current) || 0;
    return n >= 10000 ? (lnb || qoLo()) : n <= lnb ? 0 : lnb;
  }
  $("rxtclose").onclick = () => { $("rxt").classList.remove("open"); rxtId = 0; };
  $("rxtfreq").onclick = () => {
    const r = rxtR(); if (!r || !auth.authorised) return;
    openPad("__rxt", `Receiver ${r.id} frequency (MHz)`, "num", (text) => {
      const n = Number(text); if (!Number.isFinite(n) || n <= 0) return;
      rxTune(r.id, { frequencyMHz: n, offsetMHz: rxOffsetFor(n, r.offsetMHz) });
    });
  };

  // Which receiver takes a signal chosen on the QO-100 page: asked only
  // when there is more than one switched on.
  let rxPickCb = null;
  function pickReceiver(title, cb) {
    const on = receivers.filter((r) => r.enabled);
    if (on.length <= 1) return cb(1);
    rxPickCb = cb;
    $("rxpicktitle").textContent = title;
    $("rxpicklist").innerHTML = on.map((r) => `<button class="rxcard${r.locked ? " locked" : ""}${r.heard ? " heard" : ""}" data-rxpick="${r.id}">
      <span class="rxn">${r.id}</span><span class="rxmain"><b>${esc(rxLine(r) ?? "")}</b><small>${esc(rxWhere(r) ?? "")}</small></span>
      <span class="rxbadge">${r.heard ? "♪ heard" : ""}</span></button>`).join("");
    $("rxpick").classList.add("open");
  }
  $("rxpickclose").onclick = () => { $("rxpick").classList.remove("open"); rxPickCb = null; };

  document.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    if (b.dataset.rxsel) {
      rxPost("/api/rx/select", { id: Number(b.dataset.rxsel) }).then((v) => {
        if (v && !v.error) { rxView = v; for (const r of receivers) { r.heard = r.id === v.selected; r.enlarged = v.enlarged && r.heard; } paintReceivers(); }
      });
      return;
    }
    if (b.dataset.rxtune) return openRxt(Number(b.dataset.rxtune));
    if (b.dataset.rxpick) {
      $("rxpick").classList.remove("open");
      const cb = rxPickCb; rxPickCb = null;
      if (cb) cb(Number(b.dataset.rxpick));
      return;
    }
    const r = rxtR();
    if (!r || !b.closest("#rxt")) return;
    if (b.dataset.rxtf) {
      const f = Number(b.dataset.rxtf);
      return rxTune(r.id, { frequencyMHz: f, offsetMHz: b.dataset.rxtlo === "qo" ? (Number(r.offsetMHz) || qoLo()) : 0 });
    }
    if (b.dataset.rxtsr) return rxTune(r.id, { symbolRateKs: Number(b.dataset.rxtsr) });
    if (b.dataset.rxton) return rxTune(r.id, { enabled: b.dataset.rxton === "1" });
    if (b.dataset.rxtlnb) {
      // DC on the coax: a second tap within 4 s, as on the QO-100 page.
      const v = b.dataset.rxtlnb;
      if (v !== "off" && rxtArm !== v) {
        rxtArm = v; paintRxt();
        setTimeout(() => { if (rxtArm === v) { rxtArm = null; paintRxt(); } }, 4000);
        return;
      }
      rxtArm = null;
      return rxTune(r.id, { lnbSupply: v });
    }
  });

  // A tuner on the Pluto (rxTuner "pluto"). The one thing the console does
  // for it beyond choosing it: give the Pluto its own callsign when it has
  // none, which the daemon will not work around (a Pluto with none listens
  // for its longmynd under an empty callsign it does not show). Setting it
  // restarts the Pluto, so the button arms on the first tap like Shut down.
  let plutoCallArmed = false;
  function paintPlutoRx(rx) {
    const mine = cfg.rxTuner === "pluto";
    $("plutocall").hidden = !(mine && rx.plutoNeedsCall);
    if (!plutoCallArmed) $("plutocall").textContent = `Set the Pluto's callsign to ${cfg.callsign || "?"}`;
    $("plutorxnote").textContent = mine ? (rx.plutoNeedsCall ? "the Pluto restarts (about a minute)" : rx.label || "") : "";
  }
  $("plutocall").onclick = async () => {
    if (!canChange()) return;
    const b = $("plutocall");
    if (!plutoCallArmed) {
      plutoCallArmed = true; b.classList.add("arm"); b.textContent = "Tap again: the Pluto restarts";
      setTimeout(() => { if (plutoCallArmed) { plutoCallArmed = false; b.classList.remove("arm"); paintPlutoRx(lastRx || {}); } }, 5000);
      return;
    }
    plutoCallArmed = false; b.classList.remove("arm");
    const j = await rxPost("/api/pluto/callsign", {});
    $("plutocallmsg").textContent = j ? (j.error || j.message || "") : "";
  };
