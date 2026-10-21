"""The user guide's Markdown as the box's /help page: a renderer for exactly
the subset USERGUIDE.md uses, the GitHub-style anchors the console's "?"
buttons link to, and the page around it (HELP_PAGE).

Why it is rendered on the box rather than by pandoc at release time, and
where the guide is found, is in openhamshack.py's comment above HELP_DIR; it
also caches the page and serves the screenshots. test_help.py renders the
real guide and fails on any Markdown left showing, so a construct the
guide starts using is taught here, not worked round in the guide.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path


HELP_SHOT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\.png")
_GUIDE_LIST = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_GUIDE_HEAD = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_GUIDE_IMG = re.compile(r"^!\[([^\]]*)\]\(([^)\s]+)\)\s*$")
_GUIDE_TABLE_RULE = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
# A paragraph that opens with a bold phrase ending in a full stop
# ("**Busy check.** On a QO-100 ...") is the guide's sub-section without a
# heading; list items opening "**LNB supply**:" likewise. Both get an id of
# their own so a "?" can land on the paragraph rather than the top of a long
# section.
_GUIDE_LEAD_P = re.compile(r"^\*\*([^*]+?)\.\*\*")
_GUIDE_LEAD_LI = re.compile(r"^\*\*([^*]+?)\*\*:")


def guide_slug(text: str) -> str:
    """"Software & power" -> "software-power", "QO-100" -> "qo-100": the
    GitHub-style id, so a link copied from the repository's own rendering of
    the guide works here too."""
    s = re.sub(r"[^a-z0-9\s-]", "", re.sub(r"[`*_]", "", text).lower())
    return re.sub(r"[\s-]+", "-", s.strip()).strip("-") or "section"


def _png_size(path: Path | None) -> tuple[int, int] | None:
    try:
        with open(path, "rb") as f:
            head = f.read(24)
    except (OSError, TypeError):
        return None
    if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", head[16:24])


def _guide_blocks(md: str) -> list[dict]:
    """The guide as a flat list of blocks; lists keep their nesting."""
    lines = md.replace("\t", "    ").splitlines()
    blocks: list[dict] = []
    i, n = 0, len(lines)

    def starts_block(line: str) -> bool:
        return bool(_GUIDE_HEAD.match(line) or _GUIDE_IMG.match(line) or line.startswith("|")
                    or line.startswith("```") or (_GUIDE_LIST.match(line) and not line[:1].isspace()))

    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith("```"):
            j = i + 1
            while j < n and not lines[j].startswith("```"):
                j += 1
            blocks.append({"t": "pre", "text": "\n".join(lines[i + 1:j])})
            i = j + 1
            continue
        m = _GUIDE_HEAD.match(line)
        if m:
            blocks.append({"t": "h", "level": len(m.group(1)), "text": m.group(2)})
            i += 1
            continue
        m = _GUIDE_IMG.match(line)
        if m:
            blocks.append({"t": "img", "alt": m.group(1), "src": m.group(2)})
            i += 1
            continue
        if line.startswith("|") and i + 1 < n and _GUIDE_TABLE_RULE.match(lines[i + 1]):
            def cells(row: str) -> list[str]:
                row = row.strip()
                row = row[1:] if row.startswith("|") else row
                row = row[:-1] if row.endswith("|") else row
                return [c.strip() for c in row.split("|")]
            head = cells(line)
            rows = []
            i += 2
            while i < n and lines[i].startswith("|"):
                row = cells(lines[i])
                rows.append(row + [""] * (len(head) - len(row)))
                i += 1
            blocks.append({"t": "table", "head": head, "rows": rows})
            continue
        if _GUIDE_LIST.match(line):
            root: dict | None = None
            stack: list[tuple[int, dict]] = []
            while i < n and lines[i].strip():
                m = _GUIDE_LIST.match(lines[i])
                if m:
                    ind, ordered = len(m.group(1)), m.group(2)[0].isdigit()
                    while len(stack) > 1 and ind < stack[-1][0]:
                        stack.pop()
                    if not stack or ind > stack[-1][0]:
                        new = {"ordered": ordered, "start": int(m.group(2)[:-1]) if ordered else 1, "items": []}
                        if stack:
                            stack[-1][1]["items"][-1]["sub"].append(new)
                        else:
                            root = new
                        stack.append((ind, new))
                    stack[-1][1]["items"].append({"text": m.group(3), "sub": []})
                elif stack:
                    stack[-1][1]["items"][-1]["text"] += " " + lines[i].strip()
                i += 1
            blocks.append({"t": "list", **root})
            continue
        para = [line.strip()]
        i += 1
        while i < n and lines[i].strip() and not starts_block(lines[i]):
            para.append(lines[i].strip())
            i += 1
        blocks.append({"t": "p", "text": " ".join(para)})
    return blocks


def guide_render(md: str, shots: Path | None = None) -> dict:
    """{"title", "html", "toc": [(level, id, text)], "anchors": {id: text}}
    for the guide. Every heading has an id (its slug), and so does each
    bold-led paragraph or list item; *Italic* naming one of those becomes a
    link to it, which is how the guide already writes "see *Busy check*"."""
    import html as htmlmod
    blocks = _guide_blocks(md)
    used: dict[str, str] = {}
    refs: dict[str, str] = {}

    def claim(text: str) -> str:
        base = guide_slug(text)
        sid, k = base, 2
        while sid in used:
            sid, k = f"{base}-{k}", k + 1
        used[sid] = text
        refs.setdefault(re.sub(r"[*`]", "", text).strip().lower(), sid)
        return sid

    for b in blocks:
        if b["t"] == "h":
            b["id"] = claim(b["text"])
        elif b["t"] == "p" and (m := _GUIDE_LEAD_P.match(b["text"])):
            b["id"] = claim(m.group(1))
        elif b["t"] == "list":
            for it in b["items"]:
                if m := _GUIDE_LEAD_LI.match(it["text"]):
                    it["id"] = claim(m.group(1))

    def esc(s: str) -> str:
        return htmlmod.escape(s, quote=True)

    def inline(text: str) -> str:
        codes: list[str] = []

        def keep(m: re.Match) -> str:
            codes.append(m.group(1))
            return f"\x00{len(codes) - 1}\x00"
        t = esc(re.sub(r"`([^`]+)`", keep, text)).replace("&#x27;", "'")
        t = re.sub(r"\[([^\]]+)\]\(((?:https?://|#)[^)\s]+)\)", r'<a href="\2">\1</a>', t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)

        def italic(m: re.Match) -> str:
            inner = m.group(1)
            sid = refs.get(re.sub(r"<[^>]+>", "", inner).strip().lower())
            return f'<a class="xref" href="#{sid}"><em>{inner}</em></a>' if sid else f"<em>{inner}</em>"
        t = re.sub(r"(?<![\w*])\*(?![\s*])(.+?)(?<![\s*])\*(?![\w*])", italic, t)
        return re.sub("\x00(\\d+)\x00", lambda m: f"<code>{esc(codes[int(m.group(1))])}</code>", t)

    def plain(text: str) -> str:
        return re.sub(r"[*`]", "", text)

    def idattr(d: dict) -> str:
        return f' id="{d["id"]}"' if "id" in d else ""

    def render_list(lst: dict) -> str:
        tag = "ol" if lst["ordered"] else "ul"
        start = f' start="{lst["start"]}"' if lst["ordered"] and lst["start"] != 1 else ""
        items = "".join(f'<li{idattr(it)}>{inline(it["text"])}{"".join(render_list(s) for s in it["sub"])}</li>'
                        for it in lst["items"])
        return f"<{tag}{start}>{items}</{tag}>"

    out: list[str] = []
    toc: list[tuple[int, str, str]] = []
    title = "OpenHamShack user guide"
    for b in blocks:
        t = b["t"]
        if t == "h":
            if b["level"] == 1:
                title = plain(b["text"])
            else:
                toc.append((b["level"], b["id"], plain(b["text"])))
            out.append(f'<h{b["level"]} id="{b["id"]}">{inline(b["text"])}</h{b["level"]}>')
        elif t == "p":
            out.append(f'<p{idattr(b)}>{inline(b["text"])}</p>')
        elif t == "list":
            out.append(render_list(b))
        elif t == "table":
            head = "".join(f"<th>{inline(c)}</th>" for c in b["head"])
            rows = "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in b["rows"])
            out.append(f'<div class="tablewrap"><table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></div>')
        elif t == "img":
            m = re.fullmatch(r"docs/screenshots/(.+)", b["src"])
            name = m.group(1) if m and HELP_SHOT_RE.fullmatch(m.group(1)) else None
            src = f"help/screenshots/{name}" if name else b["src"]
            size = _png_size(shots / name) if shots and name else None
            dims = f' width="{size[0]}" height="{size[1]}"' if size else ""
            out.append(f'<figure><img src="{esc(src)}" alt="{esc(plain(b["alt"]))}"{dims} loading="lazy">'
                       f"<figcaption>{inline(b['alt'])}</figcaption></figure>")
        elif t == "pre":
            out.append(f"<pre>{esc(b['text'])}</pre>")
    return {"title": title, "html": "\n".join(out), "toc": toc, "anchors": used}


# The page's look is the console's: the same colour tokens (light only, as
# the console is), system fonts - nothing fetched, the box may be offline -
# and touch-sized controls. Its own bar (back to the console, search) shows
# only when it is opened on its own in a browser; inside the console's help
# viewer the console's bar does both, so the on-screen keyboard can type the
# search. helpFilter() and helpGo() are what that viewer calls.
HELP_PAGE = r"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>@@TITLE@@</title>
<script>if (window.parent !== window) document.documentElement.classList.add("embedded");</script>
<style>
  :root {
    --bg: #eef1f5; --card: #ffffff; --ink: #0f1720; --dim: #4a5565; --line: #c9d1dc;
    --chip: #e3e8ef; --sel: #1554c0; --sel-ink: #ffffff; --ok: #137a3c; --warn: #b26a00; --air: #c4231b;
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body { margin: 0; background: var(--bg); color: var(--ink);
               font: 21px/1.5 system-ui, "DejaVu Sans", sans-serif; }
  html { scroll-padding-top: 12px; }
  html:not(.embedded) { scroll-padding-top: 100px; }
  #bar { position: sticky; top: 0; z-index: 2; display: flex; flex-wrap: wrap; gap: 10px 16px; align-items: center;
         padding: 10px 24px; background: var(--card); border-bottom: 2px solid var(--line); }
  .embedded #bar { display: none; }
  #bar a.back { min-height: 56px; min-width: 140px; display: inline-flex; align-items: center; justify-content: center;
                padding: 0 18px; border-radius: 12px; border: 2px solid var(--line); background: var(--chip);
                color: var(--ink); font: 700 24px system-ui, "DejaVu Sans", sans-serif; text-decoration: none; }
  #bar b { font-size: 26px; }
  #q { flex: 1 1 260px; min-width: 0; min-height: 56px; padding: 0 16px; border-radius: 12px; border: 2px solid var(--line);
       background: #f8fafc; color: var(--ink); font: 600 22px system-ui, "DejaVu Sans", sans-serif; }
  #count { color: var(--dim); font-size: 19px; }
  #guide { max-width: 1180px; margin: 0 auto; padding: 8px 28px 60px; overflow-wrap: anywhere; }
  h1 { font-size: 36px; margin: 18px 0 2px; }
  #ver { color: var(--dim); margin: 0 0 14px; font-size: 19px; }
  h2 { font-size: 30px; margin: 34px 0 8px; padding-top: 14px; border-top: 2px solid var(--line); }
  h3 { font-size: 25px; margin: 26px 0 6px; }
  h4 { font-size: 22px; margin: 22px 0 4px; }
  p, li { max-width: 72ch; }
  li { margin: 4px 0; }
  code { font: 600 .92em ui-monospace, "DejaVu Sans Mono", monospace; background: var(--chip); padding: 1px 6px;
         border-radius: 6px; }
  pre { background: var(--card); border: 2px solid var(--line); border-radius: 12px; padding: 12px 16px; overflow-x: auto; }
  a { color: var(--sel); }
  a.xref { text-decoration: none; border-bottom: 2px solid currentColor; }
  .tablewrap { overflow-x: auto; margin: 10px 0; }
  table { border-collapse: collapse; background: var(--card); border: 2px solid var(--line); font-size: 19px; }
  th, td { text-align: left; vertical-align: top; padding: 8px 12px; border-top: 1px solid var(--line); }
  th { color: var(--dim); font-weight: 700; border-top: 0; }
  figure { margin: 16px 0; }
  figure img { display: block; max-width: 100%; height: auto; border: 2px solid var(--line); border-radius: 12px;
               background: var(--card); }
  figcaption { color: var(--dim); font-size: 18px; margin-top: 6px; }
  #contents { background: var(--card); border: 2px solid var(--line); border-radius: 18px; padding: 10px 18px 14px;
              margin: 10px 0 6px; }
  #contents h2 { margin: 0 0 8px; padding: 0; border: 0; font-size: 22px; text-transform: uppercase; letter-spacing: 1.5px;
                 color: var(--dim); }
  #contents div { display: flex; flex-wrap: wrap; gap: 8px; }
  #contents a { min-height: 52px; display: inline-flex; align-items: center; padding: 0 14px; border-radius: 12px;
                border: 2px solid var(--line); background: var(--chip); color: var(--ink); font-weight: 600;
                font-size: 19px; text-decoration: none; }
  mark { background: #ffe066; color: inherit; border-radius: 3px; }
  .hid { display: none !important; }
  .flash { animation: flash 2.2s ease-out; border-radius: 8px; }
  @keyframes flash { 0%, 40% { background: #ffe9a8; box-shadow: 0 0 0 8px #ffe9a8; }
                     100% { background: transparent; box-shadow: 0 0 0 8px transparent; } }
  #none { color: var(--dim); font-size: 22px; padding: 30px 0; }
  #missing { background: #fff4d6; border: 2px solid var(--warn); border-radius: 14px; padding: 14px 18px; }
  @media (max-width: 700px) {
    html, body { font-size: 17px; }
    #guide { padding: 4px 14px 40px; }
    #bar { padding: 8px 14px; }
    h1 { font-size: 28px; } h2 { font-size: 24px; } h3 { font-size: 21px; } h4 { font-size: 19px; }
    table { font-size: 16px; } th, td { padding: 6px 8px; }
    #contents a { font-size: 16px; min-height: 46px; padding: 0 10px; }
  }
</style>
</head>
<body>
<div id="bar"><a class="back" href="./">&lsaquo; Console</a><b>User guide</b>
  <input id="q" type="search" placeholder="Search the guide" aria-label="Search the guide" autocomplete="off">
  <span id="count"></span></div>
<main id="guide">
@@BODY@@
<p id="none" class="hid">Nothing in the guide matches that.</p>
</main>
<script>
(function () {
  "use strict";
  const art = document.getElementById("guide");
  const blocks = Array.from(art.children).filter((b) => b.id !== "none");
  const level = (b) => +((/^H([1-6])$/.exec(b.tagName) || [0, 0])[1]);
  // What a search shows or hides: a list item, a table row, otherwise the
  // whole block - so a long list shows only the items that match.
  const units = (b) => b.matches("ul, ol") ? Array.from(b.children)
    : b.matches(".tablewrap") ? Array.from(b.querySelectorAll("tbody tr")) : [b];
  function unmark() {
    art.querySelectorAll("mark").forEach((m) => m.replaceWith(document.createTextNode(m.textContent)));
    art.normalize();
  }
  function mark(el, re) {
    const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walk.nextNode()) nodes.push(walk.currentNode);
    for (const node of nodes) {
      const parts = node.nodeValue.split(re);
      if (parts.length < 2) continue;
      const frag = document.createDocumentFragment();
      parts.forEach((p, i) => {
        if (!p) return;
        if (i % 2) { const m = document.createElement("mark"); m.textContent = p; frag.appendChild(m); }
        else frag.appendChild(document.createTextNode(p));
      });
      node.replaceWith(frag);
    }
  }
  // Every word typed must appear, in any order, so "lnb volt" finds the LNB
  // supply item. Returns how many things matched; -1 for no search at all.
  window.helpFilter = function (q) {
    unmark();
    art.querySelectorAll(".hid").forEach((e) => e.classList.remove("hid"));
    const words = String(q || "").toLowerCase().split(/\s+/).filter(Boolean);
    const none = document.getElementById("none");
    none.classList.add("hid");
    if (!words.length) return -1;
    const hit = (el) => { const t = el.textContent.toLowerCase(); return words.every((w) => t.includes(w)); };
    const re = new RegExp("(" + words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")", "gi");
    let count = 0;
    const heads = [];          // the headings above the current block, one per level
    const shown = new Set();
    for (const b of blocks) {
      const lv = level(b);
      if (lv) {
        heads.length = lv - 1;
        heads[lv - 1] = b;
        if (lv > 1 && hit(b)) { count++; heads.forEach((h) => h && shown.add(h)); mark(b, re); }
        continue;
      }
      if (b.id === "contents" || b.id === "ver") { b.classList.add("hid"); continue; }
      let any = false;
      for (const u of units(b)) {
        if (hit(u)) { any = true; count++; mark(u, re); } else u.classList.add("hid");
      }
      if (any) heads.forEach((h) => h && shown.add(h)); else b.classList.add("hid");
    }
    for (const b of blocks) if (level(b) > 1 && !shown.has(b)) b.classList.add("hid");
    if (!count) none.classList.remove("hid");
    window.scrollTo(0, 0);
    return count;
  };
  // Open at a section: the search is cleared first, or the section could be
  // hidden. location.replace, not a new history entry: inside the console
  // the frame shares the browser's history, and Back there must not step
  // through every "?" tapped. The URL is made whole here: called from the
  // console, a bare "#id" would be resolved against the console's address
  // (the caller's), and the frame would load the console inside itself.
  window.helpGo = function (id) {
    window.helpFilter("");
    const q = document.getElementById("q");
    q.value = "";
    const el = document.getElementById(id);
    if (!el) return false;
    location.replace(new URL("#" + id, location.href).href);
    el.scrollIntoView({ block: "start" });
    el.classList.remove("flash");
    void el.offsetWidth;
    el.classList.add("flash");
    return true;
  };
  if (document.documentElement.classList.contains("embedded")) {
    // Keys typed while the guide has focus belong to the console's search
    // (the frame would otherwise swallow them). Space still scrolls.
    // Handed over by a direct call, not postMessage, which this used to be:
    // the first key moves the focus out to the console, so the keys after
    // it arrive there as ordinary keydowns, while any typed before the move
    // were still queued as messages - and a busy browser ran input ahead of
    // them, so "over-power" came out "orv-epower". A call runs inside this
    // keydown, before the browser routes the next key anywhere, so the
    // order is the order typed. The frame is always the console's own
    // origin (it calls helpFilter and helpGo the same way).
    document.addEventListener("keydown", (e) => {
      if (e.ctrlKey || e.metaKey || e.altKey || e.key === " ") return;
      if (e.key.length !== 1 && e.key !== "Escape") return;
      e.preventDefault();
      if (typeof window.parent.helpKey === "function") window.parent.helpKey(e.key);
    });
  } else {
    const q = document.getElementById("q"), count = document.getElementById("count");
    q.addEventListener("input", () => {
      const n = window.helpFilter(q.value);
      count.textContent = n < 0 ? "" : n === 1 ? "1 match" : n + " matches";
    });
  }
  const start = location.hash.length > 1 && document.getElementById(decodeURIComponent(location.hash.slice(1)));
  if (start) start.classList.add("flash");
})();
</script>
</body>
</html>
"""
