  // Signal log. The daemon keeps one sample a second for two hours; the
  // graph is drawn here as SVG (no libraries: the box may have no route out).
  // A tap or hover on it reads out that second.
  let sigOpen = false, sigMin = 10, sig = null, sigLoaded = 0, sigRx = 1;
  const hhmm = (t, sec) => new Date(t * 1000).toTimeString().slice(0, sec ? 8 : 5);
  async function loadSig() {
    try { sig = await (await fetch(`/api/rx/log?minutes=${sigMin}&rx=${sigRx}`)).json(); sigLoaded = Date.now(); paintSig(); }
    catch (e) { /* daemon restarting */ }
  }
  function paintSig() {
    for (const b of $("sigspan").children) b.classList.toggle("on", Number(b.dataset.min) === sigMin);
    paintRxChooser("sigrx", sigRx, "sigrx");
    if (!sig) return;
    const S = sig.samples, m = sig.summary, f = (v) => v == null ? "—" : v.toFixed(1);
    // Words first - how much of the time it was locked - then the numbers;
    // it read like a log line ("MER — / — / — dB (min/median/max)").
    const span = sig.minutes >= 60 ? `${sig.minutes / 60} hour${sig.minutes === 60 ? "" : "s"}` : `${sig.minutes} min`;
    $("sigsum").innerHTML = !S.length ? `Nothing received in the last ${span}.`
      : !m.lockedPct ? `<b>Not locked</b> in the last ${span}.`
      : `<b>Locked ${f(m.lockedPct)}%</b> of the last ${span}`
        + `${m.dropouts ? `, ${m.dropouts} dropout${m.dropouts === 1 ? "" : "s"}` : ", no dropouts"}.`
        + ` MER ${f(m.medianMer)} dB typical, ${f(m.minMer)} at its lowest, ${f(m.maxMer)} at its highest.`;
    // Drawn 1400 wide on the touchscreen; on a phone the drawing narrows
    // with the card instead of shrinking whole, so its labels stay readable.
    const cw = $("siggraph").clientWidth || 1400, W = cw >= 1000 ? 1400 : Math.max(500, cw), H = 440, L = 64, R = 16, T = 14, B = 44;
    const t1 = sig.now, t0 = t1 - sig.minutes * 60;
    const vals = S.flatMap((s) => [s.mer, s.needDb]).filter((v) => v != null);
    let lo = vals.length ? Math.floor(Math.min(...vals) - 1) : 0, hi = vals.length ? Math.ceil(Math.max(...vals) + 1) : 10;
    if (hi - lo < 6) { const mid = (hi + lo) / 2; lo = Math.floor(mid - 3); hi = lo + 6; }
    const x = (t) => L + (t - t0) / (t1 - t0) * (W - L - R), y = (v) => T + (hi - v) / (hi - lo) * (H - T - B);
    const out = [];
    // Unlocked seconds shaded; a gap of more than a few seconds is no data.
    for (let i = 0; i < S.length; i++) {
      if (S[i].locked) continue;
      let j = i; while (j + 1 < S.length && !S[j + 1].locked && S[j + 1].t - S[j].t <= 3) j++;
      const end = j + 1 < S.length && S[j + 1].t - S[j].t <= 3 ? S[j + 1].t : S[j].t + 1;
      out.push(`<rect x="${x(S[i].t).toFixed(1)}" y="${T}" width="${Math.max(1, x(end) - x(S[i].t)).toFixed(1)}" height="${H - T - B}" fill="#f6c9c5"/>`);
      i = j;
    }
    const ystep = [1, 2, 5, 10, 20].find((s) => (hi - lo) / s <= 8);
    for (let v = Math.ceil(lo / ystep) * ystep; v <= hi; v += ystep)
      out.push(`<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="var(--line)" stroke-width="1"/>`,
               `<text x="${L - 8}" y="${y(v) + 7}" text-anchor="end" font-size="20" fill="var(--dim)">${v}</text>`);
    const xstep = { 10: 120, 60: 600, 120: 1200 }[sig.minutes] || 600;
    for (let t = Math.ceil(t0 / xstep) * xstep; t <= t1; t += xstep)
      out.push(`<line x1="${x(t)}" x2="${x(t)}" y1="${T}" y2="${H - B}" stroke="var(--line)" stroke-width="1"/>`,
               `<text x="${x(t)}" y="${H - B + 28}" text-anchor="middle" font-size="20" fill="var(--dim)">${hhmm(t)}</text>`);
    out.push(`<text x="${L + 6}" y="${T + 22}" font-size="20" fill="var(--dim)">dB</text>`);
    const path = (key) => {
      let d = "", prev = null;
      for (const s of S) {
        const v = s[key];
        if (v == null) { prev = null; continue; }
        const join = prev && s.t - prev.t <= 3;
        // The threshold is a step: it changes only when the MODCOD does.
        if (key === "needDb" && join && prev.needDb !== v) d += `L${x(s.t).toFixed(1)},${y(prev.needDb).toFixed(1)}`;
        d += `${join ? "L" : "M"}${x(s.t).toFixed(1)},${y(v).toFixed(1)}`;
        prev = s;
      }
      return d;
    };
    out.push(`<path d="${path("needDb")}" fill="none" stroke="var(--warn)" stroke-width="2" stroke-dasharray="10 6"/>`,
             `<path d="${path("mer")}" fill="none" stroke="var(--sel)" stroke-width="2" stroke-linejoin="round"/>`,
             `<line id="sigcur" y1="${T}" y2="${H - B}" stroke="var(--ink)" stroke-width="1" visibility="hidden"/>`,
             `<rect x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="none" stroke="var(--line)" stroke-width="2"/>`);
    $("siggraph").innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="MER over the last ${sig.minutes} minutes">${out.join("")}</svg>`;
    const svg = $("siggraph").firstChild;
    const read = (ev) => {
      if (!S.length) return;
      const r = svg.getBoundingClientRect(), t = t0 + ((ev.clientX - r.left) / r.width * W - L) / (W - L - R) * (t1 - t0);
      let s = S[0];
      for (const c of S) if (Math.abs(c.t - t) < Math.abs(s.t - t)) s = c;
      const cur = $("sigcur"); cur.setAttribute("x1", x(s.t)); cur.setAttribute("x2", x(s.t)); cur.setAttribute("visibility", "visible");
      $("sigread").textContent = hhmm(s.t, true) + "  ·  " + (s.locked
        ? [`MER ${f(s.mer)} dB`, s.modcod && (s.modcod + (s.needDb != null ? ` (needs ${s.needDb.toFixed(1)})` : "")),
           s.name, s.bchErrors != null ? `${s.bchErrors} BCH errors` : ""].filter(Boolean).join("  ·  ")
        : "not locked");
    };
    svg.addEventListener("pointermove", read);
    svg.addEventListener("pointerdown", read);
  }
  $("sigbtn").onclick = () => { sigOpen = true; sig = null; $("sigread").textContent = ""; $("sig").classList.add("open"); paintSig(); loadSig(); };
  $("sigclose").onclick = () => { sigOpen = false; $("sig").classList.remove("open"); };
  $("sigspan").onclick = (e) => {
    const b = e.target.closest("button[data-min]"); if (!b) return;
    sigMin = Number(b.dataset.min); $("sigread").textContent = ""; loadSig();
  };
  // Two hours is ~7200 samples, so the longer spans refresh less often.
  setInterval(() => { if (sigOpen && Date.now() - sigLoaded >= (sigMin > 10 ? 10000 : 2000)) loadSig(); }, 1000);

  // Compact's Signal column on the main page: the margin now, and the last
  // ten minutes' margin as a small trace - the reading the review asked to
  // sit beside Transmit and Receive. Read every 10 s, only while it shows.
  let sigColAt = 0, sigCol = null;
  async function loadSigCol() {
    sigColAt = Date.now();
    try { sigCol = await (await fetch("/api/rx/log?minutes=10&rx=1")).json(); } catch (e) { return; }
    paintSigCol();
  }
  function paintSigCol() {
    const rx = lastRx || {}, m = rx.marginDb;
    $("sigcolnow").innerHTML = rx.locked
      ? `<b class="smargin ${esc(rx.band || "")}">${m != null ? `${m < 0 ? "−" : "+"}${Math.abs(m).toFixed(1)} dB` : "Locked"}</b>`
        + `<span class="dim">${m != null ? ` margin (D${Math.max(0, m).toFixed(1)})` : ""}${rx.mer != null ? ` · MER ${rx.mer.toFixed(1)} dB` : ""}</span>`
      : `<b>${esc(rx.label || "—")}</b>`;
    if (!sigCol) return;
    const S = sigCol.samples || [], sm = sigCol.summary || {};
    const pts = S.filter((s) => s.mer != null && s.needDb != null).map((s) => [s.t, s.mer - s.needDb]);
    const t1 = sigCol.now, t0 = t1 - 600, W = 400, H = 70;
    let svg = "";
    if (pts.length) {
      const vals = pts.map((p) => p[1]), lo = Math.min(-1, ...vals), hi = Math.max(4, ...vals);
      const x = (t) => ((t - t0) / 600 * W).toFixed(1), y = (v) => (H - (v - lo) / (hi - lo) * H).toFixed(1);
      let d = "", prev = null;
      for (const [t, v] of pts) { d += `${prev && t - prev <= 3 ? "L" : "M"}${x(t)},${y(v)}`; prev = t; }
      svg = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Margin, last 10 minutes">`
        + `<line x1="0" x2="${W}" y1="${y(0)}" y2="${y(0)}" stroke="var(--air)" stroke-dasharray="6 4" vector-effect="non-scaling-stroke"/>`
        + `<path d="${d}" fill="none" stroke="var(--sel)" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>`;
    }
    $("sigcolspark").innerHTML = svg;
    $("sigcolsum").textContent = !S.length ? "Nothing received in the last 10 min."
      : `Last 10 min: locked ${Math.round(sm.lockedPct || 0)}%, ${sm.dropouts || 0} dropout${sm.dropouts === 1 ? "" : "s"}. Dashed: the threshold.`;
  }
  setInterval(() => {
    const shown = $("sigcol").offsetParent !== null;
    if (shown) { paintSigCol(); if (Date.now() - sigColAt > 10000) loadSigCol(); }
  }, 1000);
  $("sigcolopen").onclick = () => $("sigbtn").click();
  $("sigcolaim").onclick = () => showAim(true);
