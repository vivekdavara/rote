// rote's in-page helper library. It is installed in every frame as window.__rote,
// through an init script plus a lazy install fallback.
//
// Everything that needs page structure goes through this one file, so discovery
// (indexing), replay (label/table resolution, visibility, hit-testing) and the
// operator relay (element facts under the pointer) agree on the same semantics.
// Role and accessible name for *locators* come from Playwright's own engine; the
// approximations here are for what the model sees and for scoping inside rows.
(() => {
  if (window.__rote && window.__rote.version === 1) return;

  const norm = (s) =>
    (s || "")
      .replace(/ /g, " ")
      .replace(/\s+/g, " ")
      .trim()
      .replace(/[\s:*]+$/, "")
      .trim();

  const isVisible = (el) => {
    if (!el || !el.isConnected) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return false;
    const style = getComputedStyle(el);
    return style.visibility !== "hidden" && style.display !== "none";
  };

  const role = (el) => {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "text").toLowerCase();
    if (tag === "a" && el.hasAttribute("href")) return "link";
    if (tag === "button") return "button";
    if (tag === "input") {
      if (["submit", "button", "reset", "image"].includes(type)) return "button";
      if (type === "checkbox" || type === "radio") return type;
      if (type === "password") return "password";
      return "textbox";
    }
    if (tag === "select") return el.multiple || el.size > 1 ? "listbox" : "combobox";
    if (tag === "textarea") return "textbox";
    if (tag === "td" || tag === "th") return "cell";
    if (el.hasAttribute("onclick")) return "clickable";
    return tag;
  };

  const name = (el) => {
    const aria = el.getAttribute("aria-label");
    if (aria && norm(aria)) return norm(aria);
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (el.labels && el.labels.length) return norm(Array.from(el.labels, (l) => l.innerText).join(" "));
    if (tag === "input" && ["submit", "button", "reset"].includes(type)) return norm(el.value);
    if (tag === "input" && type === "image") return norm(el.getAttribute("alt") || el.getAttribute("title") || "");
    if (tag === "a" || tag === "button" || el.getAttribute("role")) return norm(el.innerText);
    return norm(el.getAttribute("title") || "");
  };

  const siblingText = (cell, direction) => {
    let current = direction < 0 ? cell.previousElementSibling : cell.nextElementSibling;
    while (current) {
      const text = norm(current.innerText);
      if (text) return text;
      current = direction < 0 ? current.previousElementSibling : current.nextElementSibling;
    }
    return "";
  };

  // The visible label for a control or a value cell.
  const label = (el) => {
    if (el.labels && el.labels.length) {
      const text = norm(Array.from(el.labels, (l) => l.innerText).join(" "));
      if (text) return { text, source: "label" };
    }
    const aria = el.getAttribute("aria-label");
    if (aria && norm(aria)) return { text: norm(aria), source: "aria" };
    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const text = norm(labelledBy.split(/\s+/).map((id) => (document.getElementById(id) || {}).innerText || "").join(" "));
      if (text) return { text, source: "aria" };
    }
    const cell = el.tagName === "TD" || el.tagName === "TH" ? el : el.closest("td,th");
    if (cell) {
      const toggle = el.type === "checkbox" || el.type === "radio";
      const before = siblingText(cell, -1);
      const after = siblingText(cell, 1);
      if (toggle && after) return { text: after, source: "row" };
      if (before) return { text: before, source: "row" };
      if (toggle && after) return { text: after, source: "row" };
    }
    return geometricLabel(el);
  };

  // Fallback: the nearest short text to the left on the same line, else directly above.
  const geometricLabel = (el) => {
    const box = el.getBoundingClientRect();
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let best = null;
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const text = norm(node.textContent);
      if (!text || text.length > 60 || !node.parentElement || !isVisible(node.parentElement)) continue;
      if (node.parentElement.closest("select,option,button,a")) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      const r = range.getBoundingClientRect();
      const sameLine = r.bottom > box.top + 2 && r.top < box.bottom - 2 && r.right <= box.left + 2;
      const above = r.bottom <= box.top + 2 && r.left < box.right && r.right > box.left - 40;
      if (!sameLine && !above) continue;
      const distance = sameLine ? box.left - r.right : 1000 + (box.top - r.bottom);
      if (distance < 0 || distance > 1400) continue;
      if (!best || distance < best.distance) best = { text, distance };
    }
    return best ? { text: best.text, source: "geometry" } : null;
  };

  const headerRow = (table) => {
    for (const row of Array.from(table.rows).slice(0, 2)) {
      const cells = Array.from(row.cells);
      const texts = cells.map((c) => norm(c.innerText));
      if (!texts.some(Boolean)) continue;
      const headerish = cells.every((c) => {
        const text = norm(c.innerText);
        if (!text || c.tagName === "TH") return true;
        const bold = c.querySelector("b,strong");
        return bold !== null && norm(bold.innerText) === text;
      });
      return headerish ? row : null;
    }
    return null;
  };

  // Header cells are column titles, never label/value pairs ("Name | Branch" is not "Name: Branch").
  const inHeaderRow = (cell) => {
    const table = cell.closest("table");
    return table !== null && headerRow(table) === cell.parentElement;
  };

  const tableContext = (el) => {
    const cell = el.tagName === "TD" || el.tagName === "TH" ? el : el.closest("td,th");
    if (!cell) return null;
    const table = cell.closest("table");
    const header = headerRow(table);
    if (!header || cell.parentElement === header) return null;
    const headers = Array.from(header.cells, (c) => norm(c.innerText));
    const row = cell.parentElement;
    return {
      headers,
      column: headers[cell.cellIndex] || null,
      row: Array.from(row.cells, (c) => norm(c.innerText)),
      rowIndex: row.rowIndex,
    };
  };

  const cssPath = (el) => {
    const parts = [];
    for (let node = el; node && node.nodeType === 1 && node !== document.body; node = node.parentElement) {
      const tag = node.tagName.toLowerCase();
      const siblings = node.parentElement ? Array.from(node.parentElement.children).filter((c) => c.tagName === node.tagName) : [];
      parts.unshift(siblings.length > 1 ? `${tag}:nth-of-type(${siblings.indexOf(node) + 1})` : tag);
    }
    return "body > " + parts.join(" > ");
  };

  const facts = (el) => {
    const rect = el.getBoundingClientRect();
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    const out = {
      tag,
      type: type || null,
      role: role(el),
      name: name(el),
      label: label(el),
      table: tableContext(el),
      text: norm(el.innerText || "").slice(0, 120),
      bbox: { x: Math.round(rect.left), y: Math.round(rect.top), w: Math.round(rect.width), h: Math.round(rect.height) },
      visible: isVisible(el),
      enabled: !el.disabled,
      css: cssPath(el),
      frame: window.name || null,
    };
    if (tag === "input" || tag === "textarea") out.value = type === "password" ? (el.value ? "********" : "") : el.value;
    if (type === "checkbox" || type === "radio") out.checked = el.checked;
    if (tag === "select") {
      out.options = Array.from(el.options, (o) => ({ label: norm(o.text), value: o.value, selected: o.selected }));
      out.value = el.value;
    }
    return out;
  };

  const INTERACTIVE =
    "a[href], button, input:not([type=hidden]), select, textarea, [onclick], [role=button], [role=link], [role=checkbox], [role=menuitem], [role=tab]";

  const headings = () =>
    Array.from(document.querySelectorAll("h1,h2,h3,h4,font[size='3'] b,font[size='4'] b,b > font[size='3']"))
      .filter(isVisible)
      .map((e) => norm(e.innerText))
      .filter(Boolean);

  const RED = /^(#c00|#cc0000|#990000|#ff0000|#f00|red)$/i;
  const messages = () =>
    Array.from(document.querySelectorAll("font[color]"))
      .filter((f) => RED.test(f.getAttribute("color") || "") && isVisible(f))
      .map((f) => norm(f.innerText))
      .filter(Boolean);

  // Cells worth reading: data cells of tables with a header row, and value cells in
  // label/value layouts ("Member #: | 100234"). Cells holding controls are skipped
  // because the controls themselves are indexed.
  const readableCells = (limit) => {
    const out = [];
    for (const cell of document.querySelectorAll("td")) {
      if (out.length >= limit) break;
      if (!isVisible(cell) || cell.querySelector(INTERACTIVE) || cell.querySelector("table") || inHeaderRow(cell)) continue;
      const text = norm(cell.innerText);
      if (!text) continue;
      const context = tableContext(cell);
      const prev = cell.previousElementSibling;
      const prevText = prev ? (prev.innerText || "").replace(/ /g, " ").trim() : "";
      const labelled = prev !== null && prevText !== "" && (/:$/.test(prevText) || prev.querySelector("b,strong") !== null);
      if ((context && context.column) || labelled) out.push(cell);
    }
    return out;
  };

  const index = ({ tag, maxText, prefix, start }) => {
    if (tag) document.querySelectorAll("[data-rote-ref]").forEach((e) => e.removeAttribute("data-rote-ref"));
    const nodes = Array.from(document.querySelectorAll(INTERACTIVE)).filter(isVisible);
    const cells = readableCells(80);
    const elements = nodes.concat(cells).map((el, i) => {
      const ref = `${prefix}${start + i}`;
      if (tag) el.setAttribute("data-rote-ref", ref);
      return { ref, ...facts(el) };
    });
    const body = document.body ? document.body.innerText || "" : "";
    return {
      url: location.href,
      title: document.title,
      name: window.name || null,
      frameset: document.querySelector("frameset") !== null,
      elements,
      headings: headings(),
      messages: messages(),
      text: body.length > maxText ? body.slice(0, maxText) + "\n[truncated]" : body,
    };
  };

  // Controls identified by a label. Buttons are identified by role + name instead.
  const CONTROLS =
    "input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]):not([type=image]), select, textarea";

  const resolveLabel = (text, wantedRole) => {
    const target = norm(text);
    const matches = (el) => {
      const found = label(el);
      return found !== null && found.text === target;
    };
    if (wantedRole !== "cell") {
      const controls = Array.from(document.querySelectorAll(CONTROLS))
        .filter(isVisible)
        .filter((el) => !wantedRole || role(el) === wantedRole)
        .filter(matches);
      if (controls.length || wantedRole) return controls;
    }
    // A value cell: the cell immediately after the label cell on the same row.
    return Array.from(document.querySelectorAll("td,th"))
      .filter(isVisible)
      .filter((cell) => !cell.querySelector(CONTROLS) && norm(cell.innerText) !== "" && !inHeaderRow(cell))
      .filter((cell) => cell.previousElementSibling !== null && norm(cell.previousElementSibling.innerText) === target);
  };

  const resolveTableCell = (spec) => {
    const out = [];
    for (const table of Array.from(document.querySelectorAll("table"))) {
      const header = headerRow(table);
      if (!header) continue;
      const headers = Array.from(header.cells, (c) => norm(c.innerText));
      if (!spec.headers.every((h) => headers.includes(norm(h)))) continue;
      const column = (h) => headers.indexOf(norm(h));
      for (const row of Array.from(table.rows)) {
        if (row === header) continue;
        const cells = Array.from(row.cells);
        const selected = Object.entries(spec.row).every(([h, value]) => {
          const i = column(h);
          return i >= 0 && cells[i] !== undefined && norm(cells[i].innerText) === norm(value);
        });
        if (!selected) continue;
        if (spec.column) {
          const i = column(spec.column);
          if (i >= 0 && cells[i]) out.push(cells[i]);
        } else if (spec.then) {
          for (const el of row.querySelectorAll(INTERACTIVE)) {
            if (el.closest("tr") !== row || !isVisible(el)) continue;
            if (role(el) !== spec.then.role) continue;
            if (spec.then.name != null && name(el) !== norm(spec.then.name)) continue;
            out.push(el);
          }
        }
      }
    }
    return Array.from(new Set(out));
  };

  const bodyText = () => norm(document.body ? document.body.innerText || "" : "");

  const textVisible = (text) => bodyText().includes(norm(text));

  // Is the element's center actually reachable, or does something cover it?
  const hitTest = (el) => {
    el.scrollIntoView({ block: "center", inline: "center" });
    const r = el.getBoundingClientRect();
    const x = r.left + r.width / 2;
    const y = r.top + r.height / 2;
    const top = document.elementFromPoint(x, y);
    if (top === null) return { ok: false, covering: null };
    if (top === el || el.contains(top)) return { ok: true, covering: null };
    let cover = top;
    while (cover.parentElement && !["fixed", "absolute"].includes(getComputedStyle(cover).position)) {
      cover = cover.parentElement;
    }
    return { ok: false, covering: norm(cover.innerText).slice(0, 240) };
  };

  const elementAt = (x, y) => {
    const el = document.elementFromPoint(x, y);
    if (!el) return null;
    const actionable = el.closest(INTERACTIVE) || el;
    return facts(actionable);
  };

  // Value cells whose label is in `labels`: on-screen PII the product profile declares.
  const labeledValues = (labels) => {
    const wanted = new Set(labels.map(norm));
    const out = [];
    for (const cell of document.querySelectorAll("td")) {
      const prev = cell.previousElementSibling;
      if (!prev || !wanted.has(norm(prev.innerText)) || inHeaderRow(cell)) continue;
      const text = norm(cell.innerText);
      if (text) out.push({ label: norm(prev.innerText), text });
    }
    return out;
  };

  const PII = [/\b\d{3}-\d{2}-\d{4}\b/, /[\w.+-]+@[\w-]+\.[\w.-]+/, /\(?\b\d{3}\)?[-. ]?\d{3}[-. ]\d{4}\b/];

  // Mark what a saved screenshot must hide: passwords, fields holding sensitive
  // values, value cells next to declared labels, declared columns, and PII-looking text.
  const markForMasking = ({ labels, columns, values }) => {
    const wantedLabels = new Set(labels.map(norm));
    const wantedColumns = new Set(columns.map(norm));
    const sensitive = values.filter((v) => v && v.length >= 3);
    let count = 0;
    const mark = (el) => {
      el.setAttribute("data-rote-mask", "1");
      count += 1;
    };
    for (const input of document.querySelectorAll("input, textarea")) {
      if (input.type === "password" || sensitive.some((v) => (input.value || "").includes(v))) mark(input);
    }
    for (const cell of document.querySelectorAll("td")) {
      const text = cell.innerText || "";
      const prev = cell.previousElementSibling;
      const context = tableContext(cell);
      if (prev && wantedLabels.has(norm(prev.innerText)) && !inHeaderRow(cell)) mark(cell);
      else if (context && context.column && wantedColumns.has(context.column)) mark(cell);
      else if (!cell.querySelector("td") && (sensitive.some((v) => text.includes(v)) || PII.some((re) => re.test(text)))) mark(cell);
    }
    return count;
  };

  const clearMasks = () => document.querySelectorAll("[data-rote-mask]").forEach((e) => e.removeAttribute("data-rote-mask"));

  const summary = () => ({ url: location.href, headings: headings(), messages: messages() });

  window.__rote = {
    version: 1,
    norm,
    isVisible,
    role,
    name,
    label,
    tableContext,
    facts,
    index,
    headings,
    messages,
    resolveLabel,
    resolveTableCell,
    textVisible,
    bodyText,
    hitTest,
    elementAt,
    labeledValues,
    markForMasking,
    clearMasks,
    summary,
  };
})();
