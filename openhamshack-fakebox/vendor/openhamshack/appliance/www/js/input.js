  // On-screen pad: numbers (with a decimal point), dotted addresses and PINs.
  // Callsign and locator are text and use the keyboard below.
  let padKey = null, padKind = "num", padText = "", padCb = null;
  function openPad(key, title, kind, cb) {
    padKey = key; padKind = kind; padText = ""; padCb = cb || null;
    $("padtitle").textContent = title;
    const keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", ".", "0", "⌫"];
    $("padkeys").innerHTML = keys.map((k) => `<button data-k="${k}">${k}</button>`).join("");
    $("padval").textContent = key.startsWith("__") || key === "remotePin" ? "" : (cfg[key] ?? "");
    $("pad").classList.add("open");
  }
  for (const el of document.querySelectorAll("[data-pad]"))
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      const key = el.dataset.target || el.id, kind = el.dataset.kind || "num";
      // Compact (density.js): typed straight into a field over it; the pad
      // is a button beside the field for anyone who wants it.
      if (typeof compactNow === "function" && compactNow()) return typeInto(el, key, el.dataset.pad, kind);
      openPad(key, el.dataset.pad, kind);
    });
  $("call").addEventListener("click", () => editCallsign());
  $("remotelogin").onclick = () => openPad("__login", "PIN for remote control", "pin");
  $("setpin").onclick = () => openPad("remotePin", "New remote PIN (4-8 digits)", "pin");
  $("padkeys").addEventListener("click", (e) => {
    const k = e.target.dataset.k; if (!k) return;
    padText = k === "⌫" ? padText.slice(0, -1) : (padText + k).slice(0, 15);
    $("padval").textContent = padText;
  });
  // A laptop's own keyboard types into the pad as well: digits, the point,
  // Backspace, Enter for OK, Escape to cancel. The text keyboard below does
  // the same; without this a laptop had to click every digit.
  document.addEventListener("keydown", (e) => {
    if (!$("pad").classList.contains("open") || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "Enter") { e.preventDefault(); return $("padok").click(); }
    if (e.key === "Escape") { e.preventDefault(); return $("padcancel").click(); }
    const k = e.key === "Backspace" || e.key === "Delete" ? "⌫" : /^[0-9.]$/.test(e.key) ? e.key : e.key === "," ? "." : null;
    if (!k) return;
    e.preventDefault();
    padText = k === "⌫" ? padText.slice(0, -1) : (padText + k).slice(0, 15);
    $("padval").textContent = padText;
  });
  $("padcancel").onclick = () => $("pad").classList.remove("open");
  $("padok").onclick = () => {
    $("pad").classList.remove("open");
    if (!padText) return;
    if (padCb) return padCb(padText);
    padCommit(padKey, padKind, padText);
  };
  // What a value typed for a setting does, from the pad or (Compact) a
  // field typed into: one set of rules for both.
  async function padCommit(key, kind, text) {
    if (key === "__login") {
      const r = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pin: text }) });
      if (!r.ok) $("remotemsg").textContent = (await r.json()).error || "Wrong PIN";
      return refresh();
    }
    if (key === "remotePin") return savePin(text);
    if (kind === "num" && key === "rxFrequencyMHz") {
      // Only the QO-100 page has an LNB, so a typed frequency says which:
      // 10 GHz means through the LNB, anything below its LO means not (or
      // the tuner would be sent a negative IF).
      const n = Number(text); if (!Number.isFinite(n)) return;
      const lnb = Number(cfg.rxOffsetMHz) || 0;
      return save({ rxFrequencyMHz: n, rxOffsetMHz: n >= 10000 ? (lnb || Number(cfg.qo100LnbMHz) || 9750) : n <= lnb ? 0 : lnb });
    }
    if (kind === "num") { const n = Number(text); if (!Number.isFinite(n)) return; save({ [key]: n }); }
    else save({ [key]: text });
  }

  // On-screen keyboard. Keyboard.open({ title, value, maxLength, mask, symbols, caretAtEnd, onOk, onInput, onCancel })
  // - the box has a touchscreen and no keyboard. Shift stays on until tapped
  // again (callsigns and keys are typed in capitals, a word or two at a time).
  // mask shows dots instead of the text, with a Show key to check it (a
  // Wi-Fi passphrase typed blind on glass is wrong more often than right).
  // symbols adds the rest of printable ASCII, which a passphrase may use.
  // A real keyboard types into it too, for a browser elsewhere.
  const Keyboard = (() => {
    const ROWS = [
      "1234567890".split(""),
      "qwertyuiop".split(""),
      "asdfghjkl".split(""),
      ["⇧", ..."zxcvbnm".split(""), "⌫"],
      "-_.,:/@#()!?&+%{}".split(""),
    ];
    const MORE = "~$*=;'\"<>[]\\|^`".split("");
    const attr = (s) => s.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    let value = "", shift = false, reveal = false, opts = {};
    const show = () => { $("kbdval").textContent = opts.mask && !reveal ? "•".repeat(value.length) : value; };
    function paint() {
      const key = (k) => {
        const label = k.length === 1 && shift ? k.toUpperCase() : k;
        const cls = k === "⇧" ? `wide${shift ? " on" : ""}` : k === "⌫" ? "wide" : "";
        return `<button class="${cls}" data-kb="${k === "⇧" ? "shift" : k === "⌫" ? "back" : attr(label)}">${attr(label)}</button>`;
      };
      const rows = opts.symbols ? [...ROWS, MORE] : ROWS;
      $("kbdkeys").innerHTML = rows.map((r) => `<div class="kbrow">${r.map(key).join("")}</div>`).join("")
        + `<div class="kbrow" style="margin-top:4px"><button class="wide" data-kb="cancel">Cancel</button>`
        + (opts.mask ? `<button class="wide${reveal ? " on" : ""}" data-kb="reveal">Show</button>` : "")
        + `<button class="space" data-kb="space">space</button><button class="wide ok" data-kb="ok">OK</button></div>`;
    }
    function press(k) {
      if (k === "shift") { shift = !shift; return paint(); }
      if (k === "reveal") { reveal = !reveal; paint(); return show(); }
      if (k === "cancel") { $("kbd").classList.remove("open"); return opts.onCancel && opts.onCancel(); }
      if (k === "ok") { $("kbd").classList.remove("open"); return opts.onOk && opts.onOk(value); }
      if (k === "back") value = value.slice(0, -1);
      else if (value.length < (opts.maxLength || 120)) value += k === "space" ? " " : k;
      show();
      // Compact: keys that land before the field below has the focus come
      // here, and the field must carry them, or it takes the focus still
      // holding the old text and the next key edits that instead.
      $("kbdin").value = value;
      // onInput: for a search that filters as it is typed (the guide's).
      if (opts.onInput) opts.onInput(value);
    }
    $("kbdkeys").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-kb]"); if (!b) return;
      press(b.dataset.kb);
    });
    document.addEventListener("keydown", (e) => {
      if (!$("kbd").classList.contains("open") || e.ctrlKey || e.metaKey || e.altKey) return;
      // Typing into the real field (Compact): it edits itself - cursor,
      // selection, paste - and only Enter and Esc are the keyboard's.
      if (e.target === $("kbdin")) {
        if (e.key === "Enter" || e.key === "Escape") { e.preventDefault(); press(e.key === "Enter" ? "ok" : "cancel"); }
        return;
      }
      const k = e.key === "Enter" ? "ok" : e.key === "Escape" ? "cancel" : e.key === "Backspace" ? "back"
        : e.key === " " ? "space" : e.key.length === 1 ? e.key : null;
      if (!k) return;
      e.preventDefault();
      press(k);
    });
    $("kbdin").addEventListener("input", () => {
      value = $("kbdin").value.slice(0, opts.maxLength || 120);
      if (opts.onInput) opts.onInput(value);
    });
    return {
      open(o) {
        opts = o || {}; value = String(opts.value ?? ""); shift = !!opts.shift; reveal = false;
        $("kbdtitle").textContent = opts.title || "";
        paint(); show();
        $("kbd").classList.add("open");
        // Compact: a plain field to type into, with the keys folded away.
        if (typeof compactNow === "function" && compactNow()) {
          const f = $("kbdin");
          f.type = opts.mask ? "password" : "text";
          f.maxLength = opts.maxLength || 120;
          f.value = value;
          // Selected, so typing replaces a setting's old value - unless the
          // text is what the operator has just started typing (the guide's
          // search opened by a key), where a selection let the second key
          // wipe out the first.
          setTimeout(() => {
            f.focus();
            if (opts.caretAtEnd) f.setSelectionRange(f.value.length, f.value.length); else f.select();
          }, 0);
        }
      },
    };
  })();
