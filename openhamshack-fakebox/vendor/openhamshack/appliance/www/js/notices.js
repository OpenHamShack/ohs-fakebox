  // The notice line, the toast, and what a view-only browser is told.
  //
  // Notices. Every banner the console has lives in #notices (index.html).
  // The urgent ones - the PA relay that would not let go, the time-out's last
  // minute, an address or display change waiting for Keep - always show:
  // each counts down or is about what is on air. Of the rest, only the one
  // with the lowest data-rank shows, with "+N more" to open the others. A
  // fresh box used to stack four of them (no network, the clock, trying from
  // USB, an update result) above the panels, 230 px of a laptop's 768. Each
  // banner's own code still decides whether it is on; this only decides
  // which of those are shown.
  let noticesAll = false;
  function paintNotices() {
    const box = $("notices"), more = $("noticemore");
    const on = [...box.querySelectorAll(".notice:not(.urgent)")].filter((n) => n.classList.contains("on"))
      .sort((a, b) => Number(a.dataset.rank) - Number(b.dataset.rank));
    if (on.length < 2) noticesAll = false;
    box.classList.toggle("all", noticesAll);
    on.forEach((n, i) => n.classList.toggle("behind", i > 0 && !noticesAll));
    more.hidden = on.length < 2;
    if (on.length < 2) return;
    more.textContent = noticesAll ? "Fewer" : `+${on.length - 1} more`;
    // In the first one's line, at its end, where a thumb finds it.
    if (more.parentElement !== on[0]) on[0].appendChild(more);
  }
  $("noticemore").onclick = () => { noticesAll = !noticesAll; paintNotices(); };
  // A notice dismissed by its own button: the next one moves up at once,
  // not at the next poll.
  $("notices").addEventListener("click", () => setTimeout(paintNotices, 0));

  // A short line near the bottom of the screen that goes by itself: for a
  // tap that was refused, which used to do nothing at all.
  let toastTimer = null;
  function toast(text) {
    const t = $("toast");
    t.textContent = text;
    t.classList.add("on");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("on"), 3500);
  }

  // View only: a browser elsewhere without the PIN. It sees everything and
  // can change nothing; the daemon refuses any change from it regardless.
  // Every control on the pages counts as one that changes something unless
  // it says otherwise - class "ro" (Close, the page buttons, the signal
  // log's spans...), a "?", or inside a part that only looks (the Settings
  // menu, the guide). So a control added later is refused, and looks it,
  // until someone decides it is safe. The one selector both dims controls
  // and decides what a tap may do, so the two cannot drift apart. Readouts
  // (a frequency is both a readout and a button) keep full strength: they
  // were greyed with everything else and hard to read on a phone.
  const VIEW_ONLY_REFUSED = ":is(:is(main, #settings, #qo, #ham, #aim, #logp, .modal, #ov, #notices, footer)"
    + " :is(.chip, .rxcard, .tap, .stepper button):not(.ro):not([data-help])"
    + ":not(:is(#setcats, #sigrx, #sigspan, #logbands, #helpv, #kbd, #pad) *), #call)";
  {
    const st = document.createElement("style");
    st.textContent = `body.remoteview ${VIEW_ONLY_REFUSED}:not(.big):not(#call) { opacity: .45; cursor: not-allowed; }`;
    document.head.appendChild(st);
  }
  const viewOnly = () => !auth.authorised;
  function refuseViewOnly() {
    toast(auth.pinSet ? "View only: enter the PIN to change this."
      : "View only: set a remote PIN on the box (Settings › Security) to control it from here.");
  }
  // May this browser change things? Says why not if it may not.
  function canChange() {
    if (!viewOnly()) return true;
    refuseViewOnly();
    return false;
  }
  // Capture, so a refused tap never reaches the control's own handler.
  document.addEventListener("click", (e) => {
    if (!viewOnly() || !e.target.closest) return;
    const el = e.target.closest("button, .tap, label, a");
    if (!el || !el.matches(VIEW_ONLY_REFUSED)) return;
    e.preventDefault();
    e.stopPropagation();
    refuseViewOnly();
  }, true);
