  // A laptop's battery (status.battery, openhamshacklib/power.py): the header's
  // meter and its notices. The N150 boxes have no battery and see neither.
  //
  // Only warnings: OpenHamShack never unkeys for the battery. An over cut at 10%
  // is one the operator may have been about to finish; told, they can plug
  // in or stop. Three cases, as the notice line ranks them:
  //  - low (20%, or 20 min left, on battery): a notice that can be
  //    dismissed, second only to a transmitter error. Dismissed until the
  //    level changes or the laptop is plugged in.
  //  - critical (10%, or 10 min): urgent, always shown, red.
  //  - the charger gone during this over: urgent until the over ends or it
  //    is plugged in again. An over begun on battery was the operator's
  //    choice and is left to low and critical; the daemon's onBatterySeconds
  //    against the over's own length tells the two apart, the same in every
  //    browser and after a reload.
  // The meter's colours follow the header's: neutral when all is well,
  // amber for low and red for critical, as the ISS and PA pills are. Not
  // blue, which means only the choice in force, and not green for charging:
  // the bolt says that, and a green battery would read as "good to go" at 5%.
  let battLowGone = false;
  function battLeft(m) {
    if (m == null) return "";
    return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h${m % 60 ? ` ${m % 60} min` : ""}`;
  }
  // Drawn rather than a character: no font on the box is sure to have a
  // battery or a bolt, and the fill shows the level at a glance.
  function battIcon(pct, charging) {
    const w = Math.round(20 * Math.max(0, Math.min(100, pct)) / 100);
    return `<svg viewBox="0 0 30 16" width="30" height="16" aria-hidden="true">`
      + `<rect x="1" y="1" width="25" height="14" rx="3" fill="none" stroke="currentColor" stroke-width="2"/>`
      + `<rect x="27" y="5" width="2.5" height="6" rx="1" fill="currentColor"/>`
      + (w ? `<rect x="3.5" y="3.5" width="${w}" height="9" rx="1" fill="currentColor"/>` : "")
      + (charging ? `<path d="M15.5 1.5 L9 9 H13.5 L11.5 14.5 L18 7 H13.5 Z" fill="currentColor" stroke="var(--card)"`
        + ` stroke-width="1.4" stroke-linejoin="round"/>` : "")
      + `</svg>`;
  }
  const BATT_STATE = { charging: "charging", discharging: "on battery", full: "full", notCharging: "plugged in, not charging",
                       unknown: "" };
  function paintBattery(body) {
    const b = body.battery || null, pill = $("battpill");
    if (!b) {
      pill.className = ""; pill.innerHTML = ""; pill.removeAttribute("title"); pill.removeAttribute("aria-label");
      for (const id of ["battlow", "battcrit"]) $(id).classList.remove("on");
      return;
    }
    // The bolt is "on the charger", full or not: what an operator wants
    // from the header is whether the laptop is running down.
    const charging = !b.onBattery;
    const left = battLeft(b.minutesLeft);
    const words = [`Battery ${b.percent}%`, BATT_STATE[b.state] || "",
                   left ? (b.state === "charging" ? `full in about ${left}` : `about ${left} left`) : ""].filter(Boolean).join(", ");
    const sig = [b.percent, b.level, charging].join("|");
    if (pill.dataset.sig !== sig) {
      pill.dataset.sig = sig;
      pill.innerHTML = battIcon(b.percent, charging) + `<span>${b.percent}%</span>`;
    }
    pill.className = "show " + (b.onBattery ? b.level : "ok");
    pill.title = words;
    pill.setAttribute("aria-label", words);

    // The notices.
    const lost = !!(body.onAir && b.onBattery && b.onBatterySeconds != null && onAirFor != null
                    && b.onBatterySeconds < onAirFor);
    const crit = b.onBattery && b.level === "critical";
    const el = $("battcrit");
    el.classList.toggle("on", crit || lost);
    el.classList.toggle("crit", crit);
    const about = left ? `, about ${left} left` : "";
    if (crit || lost) {
      // In a span: the notice is a flex row, which would set a bare <b>
      // and its sentence side by side as two columns.
      const text = "<span>" + (crit
        ? `<b>Battery critical: ${b.percent}%${about}.</b> `
          + (body.onAir ? "Plug the laptop in, or end the over: it may switch itself off mid-transmission."
                        : "Plug the laptop in now: it may switch itself off.")
        : `<b>Mains power lost while transmitting.</b> The laptop is on its battery (${b.percent}%${about}).`
          + " Plug it back in, or end the over in good time.") + "</span>";
      if (el.innerHTML !== text) el.innerHTML = text;
    }
    // Low: a notice of its own while not critical. Not shown under the
    // urgent one, which already says it.
    const isLow = b.onBattery && b.level === "low";
    if (!isLow) battLowGone = false;
    const low = isLow && !crit && !lost && !battLowGone;
    $("battlow").classList.toggle("on", low);
    if (low) {
      $("battlowmsg").textContent = `Battery low: ${b.percent}%${about}.`;
      $("battlowwhy").textContent = body.onAir
        ? "Plug the laptop in before this over runs long: it switches itself off when the battery runs out."
        : "Plug the laptop in before a long over: it switches itself off when the battery runs out.";
    }
  }
  $("battlowok").onclick = () => { battLowGone = true; $("battlow").classList.remove("on"); };
