  // Help: the user guide in a frame, loaded once (the first time it is
  // opened) and kept, so going back and forth to it is instant and it
  // keeps its place. The guide page does the filtering (helpFilter) and the
  // jumping (helpGo); this bar holds the search so the on-screen keyboard
  // can type into it, as it types everywhere else on the console.
  const Help = (() => {
    const frame = $("helpframe");
    let loaded = false, pending = null, query = "";
    const guide = () => (loaded ? frame.contentWindow : null);
    const isOpen = () => document.body.classList.contains("inhelp");
    function paint(n) {
      $("helpsearch").textContent = query ? `Search: ${query}` : "Search the guide…";
      $("helpsearch").classList.toggle("q", !!query);
      $("helpclear").hidden = !query;
      $("helpcount").textContent = !query || n < 0 ? "" : n === 0 ? "Nothing matches" : n === 1 ? "1 match" : `${n} matches`;
    }
    function filter(q) {
      query = q;
      const g = guide();
      paint(g && g.helpFilter ? g.helpFilter(q) : -1);
    }
    function go(id) {
      query = "";
      paint(-1);
      const g = guide();
      if (g && g.helpGo) g.helpGo(id);
    }
    frame.addEventListener("load", () => {
      loaded = true;
      if (pending) go(pending); else if (query) filter(query);
      pending = null;
    });
    function open(id) {
      document.body.classList.add("inhelp");
      if (!frame.getAttribute("src")) { pending = id || null; frame.src = "help"; return; }
      if (!id) return;
      if (loaded) go(id); else pending = id;
    }
    function close() { document.body.classList.remove("inhelp"); }
    // typed: started by a key, so (Compact) the field keeps the cursor
    // after it rather than selecting it for the next key to replace.
    function search(value, typed) {
      const before = query;
      // Focus out of the frame first: keys typed with it focused go to the
      // guide, not to this keyboard.
      $("helpsearch").focus();
      Keyboard.open({ title: "Search the guide", value, maxLength: 60, caretAtEnd: !!typed, onInput: filter, onOk: filter,
                      onCancel: () => filter(before) });
      if (value !== query) filter(value);
    }
    // A key typed while the guide is showing starts (or carries on) the
    // search; Escape clears it, then closes the guide.
    function key(k) {
      if (k === "Escape") { if (query) filter(""); else close(); return true; }
      if (k.length === 1 && k !== " ") { search(query + k, true); return true; }
      return false;
    }
    $("helpsearch").onclick = () => search(query);
    $("helpclear").onclick = () => filter("");
    $("helptoc").onclick = () => open("contents");
    $("helpbtn").onclick = () => (isOpen() ? close() : open());
    // "?" anywhere: capture, so the click never reaches a chip row's own
    // handler underneath it.
    document.addEventListener("click", (e) => {
      const b = e.target.closest && e.target.closest("[data-help]");
      if (!b) return;
      e.preventDefault();
      e.stopPropagation();
      open(b.dataset.help);
    }, true);
    // The header's Back leaves the guide first, as the guide's own does;
    // another page from the header closes it, or that page would open
    // hidden underneath.
    $("setback").addEventListener("click", (e) => {
      if (!isOpen()) return;
      close();
      e.stopImmediatePropagation();
    }, true);
    $("nav").addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (b && b.id !== "helpbtn") close();
    }, true);
    document.addEventListener("keydown", (e) => {
      if (!isOpen() || $("kbd").classList.contains("open") || $("pad").classList.contains("open")) return;
      if (e.ctrlKey || e.metaKey || e.altKey || (e.target.matches && e.target.matches("input, textarea"))) return;
      if (key(e.key)) e.preventDefault();
    });
    // Keys typed while the guide itself has focus, handed up by the page
    // (guide.py: a direct call, in order - see there for why not a
    // message). The first opens the keyboard and takes the focus back; any
    // that still come this way go to the keyboard as if typed on this page,
    // or a fast typist loses all but the first.
    window.helpKey = (k) => {
      if (!isOpen()) return;
      if ($("kbd").classList.contains("open")) document.dispatchEvent(new KeyboardEvent("keydown", { key: k }));
      else key(k);
    };
    return { open, close, isOpen };
  })();
