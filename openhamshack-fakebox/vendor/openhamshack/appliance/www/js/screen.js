  // One screen and no TV: Split, Picture and Console (docs/ui-review.md,
  // "Proposed one-screen layout"; screens.py has the other half). Only on
  // the box's own screen, which is the only one with a picture to place:
  // this page lays itself out for the mode the arranger says is in force
  // (status.screens), keeps #picslot clear, and reports where it is so mpv
  // is put exactly there. Nothing floats over the console any more.
  //
  // Picture's bar hides itself after BAR_HIDE_MS (never on air, nor with an
  // urgent notice up); the picture then fills the screen, and a tap on it -
  // which mpv has, not this page - comes back as status.screens.poke.
  const BAR_HIDE_MS = 8000;
  let screenView = { single: null, mode: null, chosen: "split", poke: 0 };
  let screenMode = null, barTimer = null, lastPoke = null, slotSent = "";
  const oneScreen = () => auth.local && screenView.single === true && !!screenView.mode;

  function paintScreen(view) {
    if (view) screenView = view;
    const mode = oneScreen() ? screenView.mode : null;
    if (mode !== screenMode) {
      screenMode = mode;
      document.body.classList.toggle("onescreen", !!mode);
      document.body.classList.toggle("split", mode === "split");
      document.body.classList.toggle("picmode", mode === "picture");
      if (mode === "picture") showBar(); else document.body.classList.remove("barhidden");
    }
    for (const b of document.querySelectorAll("[data-mode]"))
      b.classList.toggle("on", b.closest("#modesw") ? b.dataset.mode === (mode || screenView.chosen) : false);
    // A tap on the picture in Picture mode: the bar comes back.
    if (lastPoke !== null && screenView.poke !== lastPoke && mode === "picture") showBar();
    lastPoke = screenView.poke;
    // Where the preview shows, said on its button: here when there is no TV.
    const where = screenView.single === true ? "here" : "on TV";
    if (where !== previewWhere) { previewWhere = where; paintPtt(); }
    $("piclabel").textContent = previewing ? "TX PREVIEW: what is going out" : "Received picture";
    $("pictcol").classList.toggle("preview", previewing);
    $("picrec").classList.toggle("rec", !!cfg.rxRecord);
    $("picrec").textContent = cfg.rxRecord ? "■ Stop recording" : "● Record";
    if (mode === "picture") {
      // Never hidden on air, with something urgent to say, or with no
      // picture: on workshop a box with nothing locked hid its bar after
      // 8 s and left a black screen with nothing to tap.
      const keep = barMustStay();
      if (keep) showBar();
      $("picbar").classList.toggle("air", onAir);
    }
    reportSlot();
  }

  // The slot's rectangle, in this page's pixels with its viewport, so the
  // arranger can scale it to the screen's. Sent only when it moves.
  function reportSlot() {
    if (!screenMode || screenMode === "console") return;
    const r = $("picslot").getBoundingClientRect();
    if (!r.width || !r.height) return;
    const slot = { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height),
                   vw: innerWidth, vh: innerHeight, mode: screenMode };
    const sig = JSON.stringify(slot);
    if (sig === slotSent) return;
    slotSent = sig;
    fetch("/api/screens/slot", { method: "POST", headers: { "Content-Type": "application/json" }, body: sig })
      .catch(() => { slotSent = ""; });
  }
  addEventListener("resize", () => setTimeout(reportSlot, 100));

  function barMustStay() {
    const picture = previewing || !!stripRx().locked;
    return onAir || !picture || !!document.querySelector("#notices .notice.urgent.on");
  }

  function showBar() {
    document.body.classList.remove("barhidden");
    clearTimeout(barTimer);
    barTimer = setTimeout(() => {
      if (screenMode !== "picture" || barMustStay()) return showBar();
      document.body.classList.add("barhidden");
      reportSlot();
    }, BAR_HIDE_MS);
    requestAnimationFrame(reportSlot);
  }
  $("barhide").onclick = () => {
    if (onAir) return toast("The bar stays while on air: TRANSMIT must be in reach.");
    clearTimeout(barTimer);
    document.body.classList.add("barhidden");
    reportSlot();
  };
  // Any touch on the bar keeps it a while longer.
  $("picbar").addEventListener("pointerdown", () => { if (screenMode === "picture") showBar(); });
  $("ptt").addEventListener("pointerdown", () => { if (screenMode === "picture") showBar(); });

  async function setScreenMode(mode) {
    if (!canChange()) return;
    const r = await fetch("/api/screens/mode", { method: "POST", headers: { "Content-Type": "application/json" },
                                                 body: JSON.stringify({ mode }) }).catch(() => null);
    if (!r || !r.ok) return toast("OpenHamShack did not change the screen");
    // What is in force comes from the arranger at the next status; the
    // choice shows at once.
    screenView = { ...screenView, chosen: (await r.json()).chosen };
    paintScreen();
  }
  document.addEventListener("click", (e) => {
    const b = e.target.closest && e.target.closest("button[data-mode]");
    if (b) setScreenMode(b.dataset.mode);
  });
  $("picrec").onclick = () => save({ rxRecord: !cfg.rxRecord });

  // A keyboard on the box: Esc leaves Picture, F goes between Picture and
  // Split - the keys mpv takes too when it has the focus. Nothing keys
  // TRANSMIT: going on air stays two deliberate taps.
  document.addEventListener("keydown", (e) => {
    if (!screenMode || e.ctrlKey || e.metaKey || e.altKey || e.repeat) return;
    if ($("kbd").classList.contains("open") || $("pad").classList.contains("open") || Help.isOpen()) return;
    if (e.target.matches && e.target.matches("input, textarea, select")) return;
    if (e.key === "Escape" && screenMode === "picture") { e.preventDefault(); setScreenMode("split"); }
    else if (e.key === "f" || e.key === "F") { e.preventDefault(); setScreenMode("toggle"); }
  });
