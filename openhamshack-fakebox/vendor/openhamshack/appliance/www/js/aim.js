  // Aiming page. /api/rx/aim is polled five times a second while the page
  // is open; the daemon samples the receiver at that rate whether or not
  // anyone is looking, so the 30 s trace is there the moment it opens.
  // Aiming mode itself (the pad's keys, the TV readout) is the daemon's:
  // opening the page here turns it on when this browser may change things,
  // and turning it on anywhere else (the pad's K6) opens the page here.
  let aimOpen = false, aimFlag = false, aimData = null, aimAt = 0, aimBusy = false;
  function showAim(open, tell = true) {
    if (open === aimOpen) return;
    aimOpen = open;
    if (open) { showSettings(false); showQo(false); showHam(false); if (typeof showLog === "function") showLog(false); }
    document.body.classList.toggle("inaim", open);
    if (tell && auth.authorised)
      fetch("/api/rx/aim", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ on: open }) });
    if (open) { $("aim").scrollTop = 0; paintAim(); loadAim(); }
    else { toneOff(); if (document.fullscreenElement) document.exitFullscreen().catch(() => {}); }
  }
  // Called with /api/status's "aiming" each second: follow it when it
  // changes, so a pad press shows the page and stopping it anywhere leaves it.
  function followAim(flag) {
    if (flag !== aimFlag) showAim(flag, false);
    aimFlag = flag;
  }
  $("aimbtn").onclick = () => showAim(true);
  async function loadAim() {
    if (aimBusy) return;
    aimBusy = true;
    try { aimData = await (await fetch("/api/rx/aim")).json(); aimAt = Date.now(); paintAim(); }
    catch (e) { /* daemon restarting */ }
    finally { aimBusy = false; }
  }
  setInterval(() => { if (aimOpen) loadAim(); }, 200);
  $("aimreset").onclick = () => {
    if (!canChange()) return;
    fetch("/api/rx/aim", { method: "POST", headers: { "Content-Type": "application/json" }, body: '{"resetPeak":true}' })
      .then(loadAim);
  };
  // The box's own screen is already full screen, and there an element in
  // full screen would cover TRANSMIT; a phone or a laptop at the mast is
  // where this is for.
  $("aimfull").onclick = () => {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else if ($("aim").requestFullscreen) $("aim").requestFullscreen().catch(() => {});
  };

  const sgn = (v) => (v < 0 ? "−" : "+") + Math.abs(v).toFixed(1);
  const one = (v) => v == null ? "—" : v.toFixed(1);
  // A row of receiver chips (signal log, aiming), only with several.
  function paintRxChooser(box, current, attr) {
    const list = multiRx() ? receivers.filter((r) => r.enabled) : [];
    const sig = JSON.stringify(list.map((r) => r.id)) + current;
    if ($(box).dataset.sig === sig) return;
    $(box).dataset.sig = sig;
    $(box).innerHTML = list.map((r) => `<button class="chip${r.id === current ? " on" : ""}" data-${attr}="${r.id}">Rx ${r.id}</button>`).join("");
  }
  document.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    if (b.dataset.sigrx) { sigRx = Number(b.dataset.sigrx); sig = null; paintSig(); loadSig(); return; }
    if (b.dataset.aimrx && auth.authorised) {
      fetch("/api/rx/aim", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rx: Number(b.dataset.aimrx) }) }).then(loadAim);
    }
  });

  function paintAim() {
    paintRxChooser("aimrx", aimData ? aimData.rx || 1 : 1, "aimrx");
    $("aimfull").style.display = auth.local || !document.fullscreenEnabled ? "none" : "";
    $("aimfull").textContent = document.fullscreenElement ? "Exit full screen" : "Full screen";
    $("aimonair").classList.toggle("on", !!onAir);
    $("aimtone").textContent = Tone.on ? "Tone on" : "Tone off";
    $("aimtone").classList.toggle("on", Tone.on);
    const a = aimData;
    if (!a) return;
    const margin = a.locked && a.marginDb != null;
    $("aimstate").textContent = [a.label, a.name, a.modcod].filter(Boolean).join("  ·  ") || "—";
    $("aimbig").className = margin ? a.band : "none";
    $("aimnum").textContent = margin ? sgn(a.marginDb) : a.locked ? (a.merDb != null ? a.merDb.toFixed(1) : "Locked") : "No lock";
    $("aimunit").textContent = margin ? "dB over the decode threshold"
      : a.locked ? "dB MER: the MODCOD is not one OpenHamShack knows"
      : a.level != null ? "turn for the highest signal level below: higher is better" : "nothing from the tuner to aim by";
    const peak = a.peakMarginDb != null ? sgn(a.peakMarginDb) : "—";
    const cells = a.locked
      ? [["MER", one(a.merDb)], ["Needs", one(a.needDb)], ["Peak", peak]]
      : [["Signal level (rough)", one(a.level)], ["Best so far", one(a.peakLevel)], ["Peak margin", peak]];
    cells.forEach(([k, v], i) => { $("aimk" + (i + 1)).textContent = k; $("aimv" + (i + 1)).textContent = v; });
    // Honest about what AGC can and cannot say: see agc_level in the daemon.
    $("aimhint").innerHTML = margin
      ? `Margin is MER now less the ${esc(a.modcod)} threshold. <b>Red</b> below it, <b>amber</b> under 2.5 dB (drops out on the next fade), <b>green</b> above.`
      : !a.locked && a.level != null
        ? `<b>Rough hint, not a measurement:</b> the AGC level rises with the power in the channel, signal or noise alike, and jumps when the tuner switches its own gain. Turn for the highest level, then for lock and the margin.`
          + ` <span style="white-space:nowrap">AGC1 ${a.agc1 ?? "—"} · AGC2 ${a.agc2 ?? "—"} · LNA ${a.lna ?? "—"}</span>`
        : "";
    aimSpark(a.history || [], a.seconds || 30, margin);
  }

  function aimSpark(h, secs, locked) {
    // The margin while there is one, else the rough AGC level - the trace
    // follows the big number. Never both on one scale: not the same unit.
    const useM = locked && h.some((p) => p[1] != null), k = useM ? 1 : 2;
    const vals = h.map((p) => p[k]).filter((v) => v != null);
    if (!vals.length) { $("aimsvg").innerHTML = ""; $("aimsparkcap").textContent = "Nothing received in the last " + secs + " s."; return; }
    let lo = Math.min(...vals), hi = Math.max(...vals);
    if (useM) { lo = Math.min(lo, -1); hi = Math.max(hi, 3.5); }
    const pad = (hi - lo) * 0.08 || 1; lo -= pad; hi += pad;
    const W = 1000, H = 200, x = (age) => ((age + secs) / secs * W).toFixed(1), y = (v) => (H - (v - lo) / (hi - lo) * H).toFixed(1);
    let d = "", up = false;
    for (const p of h) {
      if (p[k] == null) { up = false; continue; }
      d += `${up ? "L" : "M"}${x(p[0])},${y(p[k])}`; up = true;
    }
    const line = (v, colour) => `<line x1="0" x2="${W}" y1="${y(v)}" y2="${y(v)}" stroke="${colour}" stroke-width="2" stroke-dasharray="8 6" vector-effect="non-scaling-stroke"/>`;
    $("aimsvg").innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Last ${secs} seconds">`
      + (useM ? line(0, "var(--air)") + line(2.5, "var(--warn)") : "")
      + `<path d="${d}" fill="none" stroke="var(--sel)" stroke-width="3" stroke-linejoin="round" vector-effect="non-scaling-stroke"/></svg>`;
    const f = useM ? sgn : one;
    $("aimsparkcap").textContent = useM
      ? `Margin, last ${secs} s: ${f(Math.min(...vals))} to ${f(Math.max(...vals))} dB · dashed: 0 and 2.5 dB`
      : `Signal level (rough, higher is better), last ${secs} s: ${f(Math.min(...vals))} to ${f(Math.max(...vals))} · no lock`;
  }

  // Tone for aiming without looking: beeps whose pitch rises with the
  // margin (about a semitone per dB, 440 Hz at the threshold), four a
  // second while locked; below lock a slower, softer triangle following the
  // rough AGC level; silence with neither or when the box stops answering.
  // Off by default and per browser: a browser may only start sound from a
  // tap, and the shack may not want it.
  const Tone = { on: false, ctx: null, osc: null, gain: null, next: 0, timer: null };
  function toneOff() {
    Tone.on = false;
    clearInterval(Tone.timer); Tone.timer = null;
    try { Tone.osc && Tone.osc.stop(); } catch (e) { /* already stopped */ }
    Tone.osc = null;
    paintAim();
  }
  $("aimtone").onclick = () => {
    if (Tone.on) return toneOff();
    try {
      Tone.ctx = Tone.ctx || new (window.AudioContext || window.webkitAudioContext)();
      Tone.ctx.resume();
      Tone.osc = Tone.ctx.createOscillator(); Tone.gain = Tone.ctx.createGain();
      Tone.gain.gain.value = 0;
      Tone.osc.connect(Tone.gain).connect(Tone.ctx.destination);
      Tone.osc.start();
      Tone.next = 0; Tone.on = true;
      Tone.timer = setInterval(toneBeat, 50);
    } catch (e) { Tone.on = false; }
    paintAim();
  };
  function toneBeat() {
    const a = aimData, c = Tone.ctx;
    if (!Tone.on || !a || !c || c.currentTime < Tone.next || Date.now() - aimAt > 1500) return;
    const clamp = (v, l, h) => Math.max(l, Math.min(h, v));
    let f, period, type = "sine", vol = 0.25;
    if (a.locked && (a.marginDb != null || a.merDb != null)) {
      const m = a.marginDb != null ? a.marginDb : a.merDb - 5;
      f = 440 * 2 ** (clamp(m, -10, 24) / 12); period = 0.25;
    } else if (a.level != null) {
      f = 150 * 2 ** (clamp(a.level, 0, 48) / 24); period = 0.6; type = "triangle"; vol = 0.15;
    } else { Tone.next = c.currentTime + 0.25; return; }
    const t = Math.max(c.currentTime, Tone.next);
    Tone.osc.type = type;
    Tone.osc.frequency.setValueAtTime(f, t);
    Tone.gain.gain.setValueAtTime(0, t);
    Tone.gain.gain.linearRampToValueAtTime(vol, t + 0.01);
    Tone.gain.gain.linearRampToValueAtTime(0, t + 0.12);
    Tone.next = t + period;
  }
  document.addEventListener("fullscreenchange", paintAim);
