  // HamTV page and the header's pass countdown. The daemon has already
  // worked out the passes; this only counts down to them. The page polls
  // /api/hamtv every 10 s while open (which also tells the daemon the
  // elements are wanted); every other page peeks once a minute, without
  // that, for the header pill.
  let hamOpen = false, ham = null, hamAt = 0;
  const hamIsTuned = () => ham && ham.channels.some((c) => Math.abs(Number(cfg.rxFrequencyMHz) - c.frequencyMHz) < 0.5);

  function showHam(open) {
    if (open === hamOpen) return;
    hamOpen = open;
    if (open) { showSettings(false); showQo(false); showAim(false); if (typeof showLog === "function") showLog(false); }
    document.body.classList.toggle("inham", open);
    if (open) { $("ham").scrollTop = 0; paintHam(); loadHam(); }
  }
  $("hambtn").onclick = () => showHam(true);
  $("isspill").onclick = () => showHam(true);

  async function loadHam() {
    try { ham = await (await fetch("/api/hamtv" + (hamOpen ? "" : "?peek=1"))).json(); hamAt = Date.now(); }
    catch (e) { /* keep the last answer; the countdown runs on the clock */ }
    paintHam();
  }
  loadHam();
  setInterval(() => { if (hamOpen || Date.now() - hamAt > 60000) loadHam(); }, 10000);
  setInterval(() => paintHam(), 1000);

  const dayName = (t) => {
    const d = new Date(t * 1000), today = new Date();
    const days = Math.round((new Date(d).setHours(0, 0, 0, 0) - new Date(today).setHours(0, 0, 0, 0)) / 864e5);
    return days === 0 ? "Today" : days === 1 ? "Tomorrow" : d.toLocaleDateString([], { weekday: "short", day: "numeric" });
  };
  function hamSpan(s) {
    s = Math.max(0, Math.round(s));
    const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, ss = s % 60;
    return h ? `${h} h ${String(m).padStart(2, "0")} min` : `${m}:${String(ss).padStart(2, "0")}`;
  }
  const compass = (az) => ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round(az / 45) % 8];
  // The downlink's Doppler shift for a range rate (km/s, + receding).
  const dopplerKHz = (rate, f) => -rate / 299792.458 * f * 1000;

  function paintHam() {
    const now = Date.now() / 1000;
    const passes = (ham && ham.passes || []).filter((p) => p.los > now);
    // Header pill: only a pass worth pointing at (ham.goodEl), and only
    // within ten minutes of it or during it.
    const good = passes.find((p) => p.good);
    const pill = $("isspill");
    if (good && good.aos - now <= 600) {
      const up = good.aos <= now;
      pill.className = "on" + (up ? " up" : "");
      pill.textContent = up ? `ISS up · sets ${hamSpan(good.los - now)}` : `ISS in ${hamSpan(good.aos - now)}`;
    } else pill.className = "";
    if (!hamOpen || !ham) return;

    // Next pass.
    const next = passes[0], box = $("hamnext");
    if (!ham.lat) {
      box.className = "";
      $("hamcount").textContent = "Where is the station?";
      $("hamnextd").innerHTML = `Set the locator to predict passes. <button class="chip" id="hamloc">Set locator…</button>`;
      $("hamloc").onclick = () => editLocator(cfg.locator, (m) => { $("hamstate").textContent = m; }, loadHam);
    } else if (!ham.tle.epoch) {
      box.className = "";
      $("hamcount").textContent = "No orbit yet";
      $("hamnextd").textContent = ham.error || "Fetching the ISS's elements from CelesTrak…";
    } else if (!next) {
      box.className = "";
      $("hamcount").textContent = "No pass in the next 3 days";
      $("hamnextd").textContent = "";
    } else {
      const up = next.aos <= now;
      box.className = up ? "up" : next.aos - now <= 600 ? "soon" : "";
      const iss = ham.iss && up ? ` Now <b>${ham.iss.el.toFixed(0)}°</b> at <b>${ham.iss.az}° ${compass(ham.iss.az)}</b>.` : "";
      $("hamcount").textContent = up ? `ISS up · sets in ${hamSpan(next.los - now)}` : `Next pass in ${hamSpan(next.aos - now)}`;
      $("hamnextd").innerHTML = `${up ? "Rose at" : dayName(next.aos)} <b>${hhmm(next.aos)}</b>${up ? " in" : ", rises in"} the <b>${compass(next.aosAz)}</b> (${next.aosAz}°), `
        + `highest <b>${next.maxEl.toFixed(0)}°</b> at ${hhmm(next.tca)} toward ${compass(next.maxAz)}, sets in the ${compass(next.losAz)} (${next.losAz}°) at ${hhmm(next.los)}.`
        + iss + (next.good ? "" : ` <b>Low pass</b>: under ${ham.goodEl}°, hard to hear.`);
    }

    // Pass table. Rebuilt only when the list changes, not every second.
    const key = JSON.stringify(passes.map((p) => p.aos)) + ham.lat;
    if ($("hamtable").dataset.key !== key) {
      $("hamtable").dataset.key = key;
      const tz = new Date().toLocaleTimeString([], { timeZoneName: "short" }).split(" ").pop();
      const f = ham.channels[0].frequencyMHz;
      $("hamtable").innerHTML = !passes.length ? "" : `<table id="hampasses"><tr><th>Day</th><th>Rises (${esc(tz)})</th><th>Highest</th><th>Sets</th>`
        + `<th class="az">Direction</th><th class="az">Doppler</th></tr>` + passes.map((p) =>
          `<tr class="${p.good ? "good" : "low"}${p.aos <= now ? " now" : ""}"><td>${dayName(p.aos)}</td><td>${hhmm(p.aos)}</td>`
          + `<td class="el">${p.maxEl.toFixed(0)}° <small>${hhmm(p.tca)}</small></td><td>${hhmm(p.los)}</td>`
          + `<td class="az">${compass(p.aosAz)} → ${compass(p.maxAz)} → ${compass(p.losAz)}</td>`
          + `<td class="az">±${Math.round(Math.max(Math.abs(dopplerKHz(p.aosRateKms, f)), Math.abs(dopplerKHz(p.losRateKms, f))))} kHz</td></tr>`).join("")
        + "</table>";
    }
    const t = ham.tle;
    $("hamstate").textContent = ham.lat ? `${ham.locator} · passes above ${ham.minEl}°, green above ${ham.goodEl}°` : "";
    $("hamtle").innerHTML = t.epoch
      ? `Orbit: ${esc(t.name)} elements from CelesTrak, epoch ${new Date(t.epoch * 1000).toISOString().slice(0, 16).replace("T", " ")} UTC `
        + `(<b>${t.ageHours < 48 ? t.ageHours.toFixed(1) + " h" : (t.ageHours / 24).toFixed(1) + " days"} old</b>${t.ageHours > 72 ? ": times may be off by minutes; a reboost moves them more" : ""}). `
        + `Refreshed every 12 hours.${ham.error ? " " + esc(ham.error) : ""}`
      : esc(ham.error || "");

    // Receive.
    const rx = lastRx || {}, tuned = hamIsTuned();
    $("hamrx").style.background = tuned && rx.locked ? "#dff3e6" : "";
    $("hamrxlabel").textContent = tuned ? (rx.label || "—") : `Not tuned: the ISS is on ${ham.channels[0].frequencyMHz} MHz, tap it below`;
    $("hamrxdetail").textContent = tuned && rx.locked
      ? [rx.mer != null ? "MER " + rx.mer.toFixed(1) + " dB" : "", rx.name, rx.provider].filter(Boolean).join("  ·  ") : "";
    const chKey = JSON.stringify([cfg.rxFrequencyMHz, cfg.rxSymbolRateKs, auth.authorised]);
    if ($("hamch").dataset.key !== chKey) {
      $("hamch").dataset.key = chKey;
      $("hamch").innerHTML = ham.channels.map((c) => `<button class="chip${tuned && Math.abs(Number(cfg.rxFrequencyMHz) - c.frequencyMHz) < 0.5
        && Number(cfg.rxSymbolRateKs) === ham.symbolRateKs ? " on" : ""}" data-hamf="${c.frequencyMHz}"${auth.authorised ? "" : " disabled"}>${esc(c.label)}</button>`).join("");
    }
    const off = Number(cfg.hamtvOffsetMHz) || 0;
    $("hamoff").textContent = off ? `${fmt(off, 3).replace(/\.?0+$/, "")} MHz` : "None";
    $("hamtune").innerHTML = `Tunes the receiver to ${ham.symbolRateKs} kS with no LNB supply`
      + (off ? `, through the downconverter: tuner at <b>${fmt(ham.channels[0].frequencyMHz - off, 3)} MHz</b> for 2395.`
             : `; the MiniTiouner and PicoTuner tune 2395 MHz directly (their NIM reaches about 2450).`)
      + ` Doppler moves the signal by up to ±55 kHz over a pass (the table's last column).`;
    $("hamstatus").innerHTML = `<b>Status, checked ${esc(ham.status.checked)}:</b> ${esc(ham.status.summary)} `
      + `Sources: ${ham.status.sources.map((u) => esc(u.replace(/^https:\/\//, ""))).join(", ")}.`;
    $("hamparams").innerHTML = `<b>${ham.channels.map((c) => c.frequencyMHz).join(" / ")} MHz</b>, ${esc(ham.mode)}, `
      + `<b>${ham.symbolRateKs} kS</b>, FEC <b>${esc(ham.fec)}</b>, MPEG-2 video with MP2 sound, RHCP.`;
  }

  document.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-hamf]"); if (!b || !ham) return;
    const off = Number(cfg.hamtvOffsetMHz) || 0;
    // One save, so the receiver restarts once. No DC up the coax: an LNA
    // or downconverter at 2.4 GHz is powered its own way (a bias-T), and
    // whatever was right for a QO-100 LNB is wrong here.
    save({ rxFrequencyMHz: Number(b.dataset.hamf), rxSymbolRateKs: ham.symbolRateKs, rxOffsetMHz: off, rxLnbSupply: "off" });
    $("hamch").dataset.key = "";
    paintHam();
  });
