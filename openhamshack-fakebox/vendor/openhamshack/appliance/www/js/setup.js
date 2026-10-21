  // First-run setup. A new box (status.setupNeeded) opens this on its own
  // screen, or on a browser signed in with the PIN - a view-only browser
  // could answer nothing, so it never sees it. One question a page; every
  // answer is saved as it is given, through save() and the editors Settings
  // uses, so there is one set of rules. "Set up later" and Finish both store
  // setupDone; Settings > Station opens it again whenever wanted.
  const BASE_STEPS = ["call", "loc", "radio", "tuner", "licence", "test", "done"];
  let STEPS = BASE_STEPS;
  let setupStep = 0, setupShown = false, setupTested = false, selfTest = null, stSig = "", stMsg = "";
  // The start-up update as the wizard's first step: only when the wizard
  // opens by itself on a new box while the update is still being looked
  // for or installed (status.autoUpdate), so a tester sets up on the
  // newest version. Up to date, off or trying from the stick: no step.
  // Reopened from Settings it never appears - updates are in Software &
  // power then.
  const AUTO_LOOKING = ["idle", "waiting", "network", "checking", "installing"];
  let autoNow = {}, wupdLeft = false;
  function openSetup(step, first) {
    if (!auth.authorised) return;
    setupTested = false;
    STEPS = first && AUTO_LOOKING.includes(autoNow.phase) ? ["update", ...BASE_STEPS] : BASE_STEPS;
    wupdLeft = false;
    $("setup").classList.add("open");
    goStep(step || 0);
  }
  function goStep(n) {
    setupStep = Math.max(0, Math.min(STEPS.length - 1, n));
    $("setupbody").scrollTop = 0;
    // The self-test starts by itself the first time its page is reached:
    // it is the point of the page, and nothing it does goes on air.
    if (STEPS[setupStep] === "test" && !setupTested && !(selfTest && selfTest.state === "running")) {
      setupTested = true;
      runSelfTest("start");
    }
    paintSetup();
  }
  $("setupback").onclick = () => goStep(setupStep - 1);
  $("setupnext").onclick = () => {
    if (STEPS[setupStep] === "update" && AUTO_LOOKING.includes(autoNow.phase) && autoNow.phase !== "installing") {
      // Skip: stop looking at this start. Once installing it cannot be
      // stopped; setup carries on and the console restarts when it is done.
      fetch("/api/autoupdate/skip", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
        .catch(() => null);
    }
    if (STEPS[setupStep] === "update") wupdLeft = true;
    if (STEPS[setupStep] !== "done") return goStep(setupStep + 1);
    if (!hasCall()) return;
    $("setup").classList.remove("open");
    save({ setupDone: true });
  };
  $("setuplater").onclick = () => {
    $("setup").classList.remove("open");
    save({ setupDone: true });
  };
  $("wcall").onclick = () => editCallsign((m) => { $("wcallerr").textContent = m; });
  $("wloc").onclick = () => editLocator(cfg.locator, (m) => { $("wlocerr").textContent = m; });
  $("wplutoip").onclick = () => save({ host: "192.168.2.1" });
  $("wtuner").addEventListener("click", (e) => {
    const b = e.target.closest("[data-wt]"); if (!b) return;
    save(b.dataset.wt === "none" ? { rxEnabled: false }
      : { rxEnabled: true, rxTuner: { network: "picotuner", pluto: "pluto" }[b.dataset.wt] || "minitiouner" });
  });
  const LICENCE_WORDS = () => licRegion() === "iaru1" ? "IARU Region 1"
    : licRegion() === "other" ? "Other: no band check"
    : cfg.nov146 === false ? "UK, without the 146 MHz NoV"
    : licRegion() === "uk" ? "Rest of UK" : "Scotland";
  const TUNER_WORDS = () => cfg.rxEnabled === false ? "None (transmit only)"
    : cfg.rxTuner === "picotuner" ? `PicoTuner network at ${cfg.picoHost || "no address"}`
    : cfg.rxTuner === "pluto" ? `Tuner on the Pluto at ${cfg.host || "no address"}` : "USB tuner";
  function selfTestWords(st) {
    if (!st || !st.checks || !st.checks.length) return "Not run";
    if (st.state === "running") return "Running…";
    const n = (r) => st.checks.filter((c) => c.result === r).length;
    const fails = n("fail"), warns = n("warn");
    if (st.state === "cancelled") return "Cancelled";
    return fails ? `${fails} to fix${warns ? `, ${warns} to look at` : ""}` : warns ? `Passed, ${warns} to look at` : "All passed";
  }
  // The update step's words for each phase of status.autoUpdate.
  const LATER = "You can update later from Settings › Software & power.";
  function wupdWords(a) {
    const v = a.version, pct = /(\d+)%/.exec(a.message || "");
    switch (a.phase) {
      case "idle": case "waiting": return ["Looking for updates…", "", null];
      case "network": return ["Waiting for an internet connection…",
        "Plug in a network cable, or set up Wi-Fi in Settings › Network, or tap Skip.", null];
      case "checking": return ["Looking for the latest version…", "", null];
      case "installing": return [`Getting the latest version (${v})…`,
        (a.message && a.message !== "Starting" ? a.message + ". " : "")
        + "The screen goes blank for a moment while OpenHamShack restarts, then setup carries on here.", pct ? Number(pct[1]) : null];
      case "current": return [`OpenHamShack ${a.current || ""} is the latest version.`, "", 100];
      case "offline": return ["No internet connection, so OpenHamShack could not look for a newer version.", LATER, 0];
      case "failed": case "held": return [`OpenHamShack could not update to ${v}: ${a.message || "it did not install"}.`,
        "Setup carries on with this version. " + LATER, 0];
      case "busy": return [a.message || "Put off.", LATER, 0];
      case "declined": return ["Skipped.", LATER, 0];
      default: return ["Updates are off.", LATER, 0];
    }
  }
  function paintUpdateStep() {
    const a = autoNow, [msg, note, pct] = wupdWords(a);
    $("wupdmsg").textContent = msg;
    $("wupdnote").textContent = note;
    const looking = AUTO_LOOKING.includes(a.phase);
    $("wupdbar").classList.toggle("spin", looking && pct === null);
    $("wupdbar").firstElementChild.style.width = pct === null ? "" : `${pct}%`;
    $("wupdbar").style.visibility = looking || a.phase === "current" ? "" : "hidden";
    $("setupnext").textContent = looking ? "Skip ›" : "Next ›";
  }
  function paintSetup() {
    if (!$("setup").classList.contains("open")) return;
    const id = STEPS[setupStep];
    for (const s of document.querySelectorAll("#setupbody .sstep")) s.classList.toggle("cur", s.dataset.step === id);
    $("setupstep").textContent = `Step ${setupStep + 1} of ${STEPS.length}`;
    $("setupback").style.visibility = setupStep ? "" : "hidden";
    const last = id === "done";
    $("setupnext").textContent = last ? "Finish" : "Next ›";
    if (id === "update") paintUpdateStep();
    $("setupnext").disabled = last && !hasCall();
    $("wcall").textContent = hasCall() ? cfg.callsign : "Tap to enter";
    $("wloc").textContent = cfg.locator || "Tap to enter";
    $("whost").textContent = cfg.host || "address";
    $("wplutoip").style.display = cfg.radio === "pluto" && cfg.host !== "192.168.2.1" ? "" : "none";
    const wt = cfg.rxEnabled === false ? "none" : cfg.rxTuner === "picotuner" ? "network"
      : cfg.rxTuner === "pluto" ? "pluto" : "usb";
    for (const b of $("wtuner").children) b.classList.toggle("on", b.dataset.wt === wt);
    $("wpicorow").style.display = wt === "network" ? "" : "none";
    $("wpico").textContent = cfg.picoHost || "address";
    if (last) {
      const st = selfTest;
      const bad = st && st.worst === "fail";
      $("wdonelead").textContent = !hasCall() ? "OpenHamShack needs your callsign before it can finish."
        : bad ? "Set up, with something the self-test says to fix before transmitting. Tap a line to change it."
        : "OpenHamShack is set up. Tap a line to change it, or Finish.";
      const rows = [
        ["Callsign", hasCall() ? cfg.callsign : "Not set: OpenHamShack will not transmit", "call", !hasCall()],
        ["Locator", cfg.locator || "Not set", "loc", false],
        ["Radio", `${cfg.radio === "pluto" ? "Pluto" : "LibreSDR"} at ${cfg.host || "no address"}`, "radio", false],
        ["Tuner", TUNER_WORDS(), "tuner", false],
        ["Licence", LICENCE_WORDS(), "licence", false],
        ["Self-test", selfTestWords(st), "test", bad],
      ];
      // What the tap does, said: a self-test not run yet is run, not "changed".
      const verb = (step) => step !== "test" ? "Change" : st && st.checks && st.checks.length ? "Results" : "Run now";
      $("wsum").innerHTML = rows.map(([k, v, step, flag]) =>
        `<button data-goto="${step}"${flag ? ' class="bad"' : ""}><span>${esc(k)}</span><b>${esc(v)}</b><em>${verb(step)}</em></button>`).join("");
    }
  }
  // A first task: finish, then open the page for it. Transmitting is set
  // up at the least power, never keyed - TRANSMIT still takes two taps.
  function finishSetup() {
    if (!hasCall()) { goStep(STEPS.indexOf("call")); return false; }
    $("setup").classList.remove("open");
    save({ setupDone: true });
    return true;
  }
  $("wtryrx").onclick = () => { if (finishSetup()) { showQo(true); qoTune(10491.5, 1500, "Beacon 10491.50"); } };
  $("wtrytx").onclick = () => {
    if (!finishSetup()) return;
    save({ frequencyMHz: 437, source: "testcard", gainDb: -20 });
    showSettings(false); showQo(false); showAim(false); showHam(false); showLog(false);
  };
  $("wsum").addEventListener("click", (e) => {
    const b = e.target.closest("[data-goto]"); if (b) goStep(STEPS.indexOf(b.dataset.goto));
  });

  // Self-test: started and cancelled here, followed through /api/status
  // (selfTest) like everything else, so the wizard and Settings show the
  // same run whichever started it - and a remote browser watches it too.
  const ST_BADGE = { pass: "Pass", warn: "Warning", fail: "Fail", skip: "Not run", pending: "Waiting", running: "Checking" };
  async function runSelfTest(action) {
    if (!canChange()) return;
    const r = await fetch("/api/selftest", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }) }).catch(() => null);
    const j = r ? await r.json().catch(() => ({})) : {};
    stMsg = r && r.ok ? "" : (j.error || "OpenHamShack did not answer");
    if (r && r.ok) selfTest = j;
    stSig = "";
    paintSelfTests();
  }
  function selfTestHtml(st) {
    if (!st || !st.checks || !st.checks.length)
      return `<div class="expl">Not run yet. It checks the video encoder this PC transmits with, the radio, the tuner,
        sound, disk space and the clock, and says what to do about anything wrong.</div>`;
    return st.checks.map((c) => `<div class="strow ${esc(c.result)}"><span class="stbadge">${esc(ST_BADGE[c.result] || c.result)}</span>`
      + `<b>${esc(c.name)}</b>${c.detail ? `<div class="stdet">${esc(c.detail)}</div>` : ""}`
      + `${c.fix ? `<div class="stfix">${esc(c.fix)}</div>` : ""}</div>`).join("");
  }
  function paintSelfTests() {
    const st = selfTest, running = !!st && st.state === "running";
    const sig = JSON.stringify(st) + stMsg;
    for (const b of [$("strun"), $("wtestrun")]) {
      b.textContent = running ? "Cancel" : st && st.checks && st.checks.length ? "Run again" : "Run self-test";
      b.disabled = !auth.authorised || (!running && onAir);
    }
    $("stmsg").textContent = $("wtestmsg").textContent = stMsg || (onAir && !running ? "Not while transmitting" : "");
    const fin = st && st.finished ? new Date(st.finished * 1000) : null;
    $("stwhen").textContent = running ? "Checking… up to half a minute; nothing is transmitted"
      : fin ? `${st.state === "cancelled" ? "Cancelled" : "Checked"} at ${p2(fin.getHours())}:${p2(fin.getMinutes())}` : "";
    if (sig === stSig) return;
    stSig = sig;
    $("stlist").innerHTML = $("wtest").innerHTML = selfTestHtml(st);
    paintSetup();
  }
  for (const b of [$("strun"), $("wtestrun")])
    b.onclick = () => runSelfTest(selfTest && selfTest.state === "running" ? "cancel" : "start");

  // Clock: a warning across the top on any page; dismissed for this page.
  let clockDismissed = false;
  $("clockok").onclick = () => { clockDismissed = true; $("clockbanner").classList.remove("on"); };

  function paintSetupStatus(body) {
    selfTest = body.selfTest || selfTest;
    const ck = body.clock || {};
    $("clockbanner").classList.toggle("on", ck.state === "warn" && !clockDismissed);
    $("clockmsg").textContent = ck.message || "";
    $("clockwhy").textContent = ck.detail ? `(${ck.detail})` : "";
    autoNow = body.autoUpdate || {};
    // Opened once per page, and only for a box nobody has set up yet.
    if (body.setupNeeded && auth.authorised && !setupShown) {
      setupShown = true;
      openSetup(0, true);
    }
    // Nothing to install: straight on to the first question, once.
    if (STEPS[setupStep] === "update" && !wupdLeft && ["current", "declined"].includes(autoNow.phase)
        && $("setup").classList.contains("open")) {
      wupdLeft = true;
      setTimeout(() => { if (STEPS[setupStep] === "update") goStep(setupStep + 1); }, 1200);
    }
    paintSelfTests();
    paintSetup();
  }
