  // Transmit needs two taps: a stray touch must not key the radio.
  $("ptt").onclick = async () => {
    if (onAir) { await fetch("/api/ptt", { method: "POST", headers: { "Content-Type": "application/json" }, body: '{"on":false}' }); return refresh(); }
    if (!armed) { armed = setTimeout(() => { armed = null; paintPtt(); }, 4000); return paintPtt(); }
    clearTimeout(armed); armed = null;
    $("ptt").textContent = "Starting…";
    await fetch("/api/ptt", { method: "POST", headers: { "Content-Type": "application/json" },
                              body: JSON.stringify({ on: true, force: !!txCheck }) });
    refresh();
  };
  let txBlocked = "", txCheck = null;
  // The transmit time-out's countdown, in its last minute. Keep transmitting
  // starts a whole new period; a view-only browser sees the bar but not the
  // button, as it cannot key either.
  function paintTxTimeout(t) {
    const on = !!(t && t.warning);
    $("txtimeout").classList.toggle("on", on);
    if (!on) { $("txkeeperr").textContent = ""; return; }
    $("txleft").textContent = t.secondsLeft;
    $("txkeep").style.display = !auth.local && !auth.authorised ? "none" : "";
  }
  // PTT output (Settings › Transmission): the PA/relay pill beside ON AIR,
  // the red bar for a relay that would not let go, and the settings rows.
  let pttOut = null, pttArmed = null;
  const PTT_EXPL = {
    off: "OpenHamShack keys only the radio. Switch any amplifier or antenna relay by hand.",
    plutoGpo: "PlutoDVB2 sets the Pluto's GPO0 high while it transmits (on a Pluto+ also MIO0). It is a logic"
      + " output: drive the PA or relay through a transistor or relay driver, never the coil itself. OpenHamShack"
      + " starts at minimum power, waits the PA delay, then turns the power up; at the end it turns it down,"
      + " waits the tail, then stops. Needs Pluto as the radio, running PlutoDVB2.",
    usbRelay: "On TRANSMIT the relay closes, the PA delay passes, then the signal starts. At the end the signal"
      + " stops, the tail passes, then the relay opens - it never switches with RF on its contacts."
      + " CH340 (LCUS) boards are serial, dcttech boards are HID.",
  };
  function paintPttOut() {
    const p = pttOut || { mode: "off" }, mode = cfg.pttOutput || p.mode || "off";
    const relay = mode === "usbRelay";
    for (const el of document.querySelectorAll(".pttrelay")) el.style.display = relay ? "" : "none";
    for (const el of document.querySelectorAll(".pttseq")) el.style.display = mode === "off" ? "none" : "";
    $("pttexpl").textContent = PTT_EXPL[mode] || "";
    // Header pill and the red bar, on every page.
    const pill = $("papill");
    pill.className = p.mode === "off" ? "" : "show" + (p.stuck ? " bad" : p.on ? " on" : "");
    pill.textContent = p.stuck ? "PA STUCK" : p.on ? "PA on" : "PA off";
    $("pttstuck").classList.toggle("on", !!p.stuck);
    $("pttstuckwhy").textContent = p.stuck ? p.stuck + "." : "";
    if (!relay) return;
    const list = (p.relays || []).slice();
    if (cfg.pttRelay && !list.some((r) => r.id === cfg.pttRelay))
      list.push({ id: cfg.pttRelay, name: cfg.pttRelayName || "USB relay", detail: "not plugged in", gone: true });
    const kind = (list.find((r) => r.id === cfg.pttRelay) || {}).kind || (String(cfg.pttRelay || "").split(":")[0]);
    $("pttbaudrow").style.display = kind === "serial" ? "" : "none";
    const chans = Math.max(p.channels || 4, Number(cfg.pttRelayChannel) || 1);
    const sig = JSON.stringify([list, cfg.pttRelay, chans, cfg.pttRelayChannel]);
    if ($("pttrelays").dataset.sig !== sig) {    // never rebuild a chip under a finger for nothing
      $("pttrelays").dataset.sig = sig;
      $("pttrelays").innerHTML = list.map((r) => `<button class="chip${r.id === cfg.pttRelay ? " on" : ""}"`
        + ` data-relay="${esc(r.id)}" data-relayname="${esc(r.name)}"${r.gone ? " disabled" : ""}>`
        + `${esc(r.name)}<small>${esc(r.detail || "")}</small></button>`).join("")
        || `<span class="dim">None found: plug the relay in</span>`;
      $("pttchans").innerHTML = Array.from({ length: chans }, (_, i) =>
        `<button class="chip${Number(cfg.pttRelayChannel) === i + 1 ? " on" : ""}" data-v="${i + 1}">${i + 1}</button>`).join("");
    }
    const t = $("ptttest");
    t.disabled = !auth.authorised || onAir || !!p.testing || !p.found;
    t.className = "chip" + (pttArmed ? " armed" : "");
    t.textContent = p.testing ? "Testing…" : pttArmed ? "Tap again to test" : "Test relay";
    $("ptttestnote").textContent = onAir ? "Not while transmitting"
      : "clicks the relay for 1 s without transmitting - nothing goes on air";
  }
  // PTT settings go through netPost rather than save(): the daemon refuses
  // them while on air, and that must be said here, not shown as a chip
  // that lit up for a setting that was never stored.
  async function savePtt(patch) {
    if (!canChange()) return;
    Object.assign(cfg, patch);
    editing = Date.now();
    render();
    paintPttOut();
    if (!await netPost("/api/config", patch, (m) => { $("pttmsg").textContent = m; })) { editing = 0; refresh(); }
  }
  $("pttrelays").addEventListener("click", (e) => {
    const b = e.target.closest("[data-relay]");
    if (b) savePtt({ pttRelay: b.dataset.relay, pttRelayName: b.dataset.relayname });
  });
  $("ptttest").onclick = async () => {
    if (!pttArmed) { pttArmed = setTimeout(() => { pttArmed = null; paintPttOut(); }, 4000); return paintPttOut(); }
    clearTimeout(pttArmed); pttArmed = null;
    const j = await netPost("/api/ptt/test", {}, (m) => { $("pttmsg").textContent = m; });
    if (j) { pttOut = j.pttOutput; paintPttOut(); }
    setTimeout(refresh, 1300);
  };
  $("txkeep").onclick = async () => {
    $("txkeep").disabled = true;
    const j = await netPost("/api/ptt/keep", {}, (m) => { $("txkeeperr").textContent = m; });
    $("txkeep").disabled = false;
    if (j) paintTxTimeout(j.txTimeout);
  };
  $("prev").onclick = async () => {
    if (!canChange()) return;
    $("prev").textContent = previewing ? "Stopping…" : "Starting…";
    await fetch("/api/preview", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ on: !previewing }) });
    refresh();
  };
  // Where the preview shows, said on the button: "Preview" beside TRANSMIT
  // with the same weight told a newcomer nothing about what or where.
  let previewWhere = "on TV";
  function paintPtt() {
    // Running is a state, not a choice: the "going" style, not blue.
    $("prev").classList.toggle("going", previewing);
    $("prev").textContent = previewing ? "Stop preview" : `Preview ${previewWhere}`;
    const p = $("ptt");
    if (!auth.local && !auth.authorised) {
      p.className = ""; p.disabled = true; p.textContent = onAir ? "ON AIR (view only)" : "View only";
      return;
    }
    if (!onAir && txBlocked) {
      p.className = ""; p.disabled = true; p.textContent = "Can't transmit: " + txBlocked;
      $("state").className = ""; $("state").textContent = "Stand by";
      return;
    }
    p.disabled = false;
    p.className = onAir ? "air" : armed ? "armed" : "";
    // After a QO-100 busy (or unchecked) refusal the same two taps send it
    // anyway - the station there may be our own, or a test - for a minute.
    const anyway = !onAir && txCheck;
    p.textContent = onAir ? "ON AIR — tap to stop" : armed ? (anyway ? "Tap again to transmit anyway" : "Tap again to transmit")
      : anyway ? (txCheck.kind === "busy" ? "In use — transmit anyway?" : "Not checked — transmit anyway?") : "TRANSMIT";
    $("state").className = onAir ? "air" : "";
    $("state").textContent = onAir ? "ON AIR" : "Stand by";
  }
