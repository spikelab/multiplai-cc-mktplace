// Review tab, style panel: the selected render's caption values, brand colours and the
// values of each overlay style it uses (GET /api/style, read from the render's .edl.json,
// its brand file and the style files' frontmatter). The user edits them, sees caption
// edits as an approximate HTML preview over the picture (drawn from the sidecar's
// captions_events), and "Send style changes" adds one `style` row per group changed, with
// only the changed keys. The session applies them; the page never edits a file.
// Builds on tab-review.js through App.review.
"use strict";

(() => {
  const { L } = App;
  const R = App.review;
  let box, notice, groupsEl, noteEl, sendBtn, statusEl, sentEl, captionEl, approxEl;
  const st = { key: null, info: null, groups: [], edits: {}, saveAs: {} };

  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  };

  // --- edits survive a reload (per viewer and render, best effort) ---------------------------

  const draftKey = () => `style:${st.key}`;
  const saveDraft = () => App.store.set(draftKey(), { edits: st.edits, saveAs: st.saveAs, note: noteEl.value });
  function loadDraft() {
    const d = App.store.get(draftKey(), null) || {};
    st.edits = d.edits && typeof d.edits === "object" ? d.edits : {};
    st.saveAs = d.saveAs && typeof d.saveAs === "object" ? d.saveAs : {};
    noteEl.value = typeof d.note === "string" ? d.note : "";
  }

  // --- loading what the panel shows ------------------------------------------------------------

  async function load() {
    const r = R.render(), side = R.sidecar();
    const key = r ? `${r.clip}@${r.version}` : null;
    if (key !== st.key) {
      st.key = key;
      st.info = null;
      st.groups = [];
      if (key) loadDraft();
    }
    if (!r || !side) {
      box.hidden = !r;
      App.notice(notice, r ? { text: "No style panel for this render: it has no .edl.json (see the timeline above)." } : null);
      groupsEl.replaceChildren();
      update();
      return;
    }
    box.hidden = false;
    try {
      const q = new URLSearchParams({ video: r.clip, version: String(r.version) });
      const info = await App.api(`/api/style?${q}`);
      if (st.key !== key) return;
      st.info = info;
      st.groups = L.styleGroups(info);
      App.notice(notice, null);
    } catch (e) {
      if (st.key !== key) return;
      st.info = null;
      st.groups = [];
      App.notice(notice, { text: `Cannot load this render's styles: ${e.message}`, error: true });
    }
    renderGroups();
  }

  // --- the groups and their fields -------------------------------------------------------------

  function edit(target, key, raw) {
    st.edits[target] = { ...(st.edits[target] || {}), [key]: raw };
    saveDraft();
    update();
  }

  function fieldEl(g, f) {
    const id = `style-${g.target}-${f.key}`.replace(/[^\w-]/g, "_");
    const edits = st.edits[g.target] || {};
    const has = f.key in edits;
    const row = el("div", "style-field");
    const label = el("label", "", f.label);
    label.htmlFor = id;
    row.append(label);
    let input;
    if (f.type === "bool") {
      input = el("input");
      input.type = "checkbox";
      input.checked = has ? !!edits[f.key] : !!f.value;
      input.addEventListener("change", () => edit(g.target, f.key, input.checked));
    } else if (f.type === "colour") {
      const swatch = el("input");
      swatch.type = "color";
      swatch.setAttribute("aria-label", `${f.label}: pick a colour`);
      input = el("input");
      input.type = "text";
      input.spellcheck = false;
      input.value = has ? String(edits[f.key]) : f.value;
      swatch.value = L.isColour(input.value) ? input.value.toLowerCase() : "#000000";
      swatch.addEventListener("input", () => { input.value = swatch.value.toUpperCase(); edit(g.target, f.key, input.value); });
      input.addEventListener("input", () => {
        if (L.isColour(input.value)) swatch.value = input.value.toLowerCase();
        edit(g.target, f.key, input.value);
      });
      row.append(swatch);
    } else {
      input = el("input");
      input.type = f.type === "text" ? "text" : "number";
      if (f.type !== "text") { input.min = String(f.min); input.max = String(f.max); input.step = String(f.step); }
      input.value = has ? String(edits[f.key]) : String(f.value ?? "");
      input.addEventListener("input", () => edit(g.target, f.key, input.value));
    }
    input.id = id;
    row.append(input);
    const was = el("span", "was muted");
    was.dataset.target = g.target;
    was.dataset.key = f.key;
    row.append(was);
    return row;
  }

  function renderGroups() {
    groupsEl.replaceChildren();
    for (const g of st.groups) {
      const fs = el("fieldset", "style-group");
      const legend = el("legend", "", g.title);
      fs.append(legend);
      if (g.error) fs.append(el("p", "error-text", g.error));
      for (const f of g.fields) fs.append(fieldEl(g, f));
      const tools = el("div", "row");
      if (g.saveAs) {
        const lab = el("label", "save-as");
        lab.append(el("span", "", "Save as a new file (optional)"));
        const inp = el("input");
        inp.type = "text";
        inp.placeholder = g.target === "brand" ? "brand name" : "style name";
        inp.value = st.saveAs[g.target] || "";
        inp.addEventListener("input", () => { st.saveAs[g.target] = inp.value; saveDraft(); update(); });
        lab.append(inp);
        tools.append(lab);
      }
      if (g.fields.length) {
        const reset = el("button", "ghost", "Reset");
        reset.type = "button";
        reset.addEventListener("click", () => {
          delete st.edits[g.target];
          delete st.saveAs[g.target];
          saveDraft();
          renderGroups();
        });
        tools.append(reset);
      }
      fs.append(tools);
      groupsEl.append(fs);
    }
    update();
  }

  // What changed, what is wrong, the send button and the preview.
  function update() {
    for (const w of groupsEl.querySelectorAll(".was")) {
      const g = st.groups.find((x) => x.target === w.dataset.target);
      const f = g && g.fields.find((x) => x.key === w.dataset.key);
      const c = g ? L.groupChanges(g, st.edits[g.target]).changes[w.dataset.key] : null;
      w.textContent = c ? `was ${c[0]}` : "";
      w.closest(".style-field").classList.toggle("changed", !!c && !!f);
    }
    const r = R.render();
    const { rows, problem } = r && st.groups.length
      ? L.styleRows(r.clip, r.version, st.groups, st.edits, st.saveAs, noteEl.value)
      : { rows: [], problem: null };
    sendBtn.disabled = !rows.length || !!problem || !r;
    statusEl.textContent = problem && problem !== "Change a value to send it." ? problem : "";
    statusEl.classList.toggle("error-text", !!statusEl.textContent);
    renderSent();
    renderPreview();
  }

  function renderSent() {
    const r = R.render();
    sentEl.replaceChildren();
    for (const row of r ? L.styleSent(App.state.rows, r.clip).filter((x) => x.kind === "style") : []) {
      const li = el("li");
      li.append(el("div", "meta", `v${row.version} · ${row.target}${row.save_as ? ` → new file ${row.save_as}` : ""}`),
        el("p", "", L.changeSummary(row.changes)));
      if (row.text) li.append(el("p", "muted", row.text));
      sentEl.append(li);
    }
    sentEl.hidden = !sentEl.children.length;
  }

  // --- the approximate caption preview over the picture ---------------------------------------

  function renderPreview() {
    const caps = st.groups.find((g) => g.target === "captions");
    const brandG = st.groups.find((g) => g.target === "brand");
    const side = R.sidecar();
    const on = !!caps && !!side && (L.groupEdited(caps, st.edits.captions) || L.groupEdited(brandG, st.edits.brand));
    captionEl.hidden = approxEl.hidden = !on;
    captionEl.replaceChildren();
    if (!on) return;
    const video = R.video(), stage = R.stage();
    const values = L.editedValues(caps, st.edits.captions);
    const original = Object.fromEntries(caps.fields.map((f) => [f.key, f.value]));
    const colours = { ...L.BRAND_DEFAULTS, ...(brandG && brandG.fields.length ? L.editedValues(brandG, st.edits.brand) : {}) };
    const out = st.info && st.info.output && st.info.output.width ? st.info.output : L.outputSize(App.state.job);
    const p = R.pictureRect(), s = stage.getBoundingClientRect();
    const m = L.captionMetrics(values, out, p.height || 1);
    captionEl.style.left = `${p.left - s.left + m.sideMargin * p.width}px`;
    captionEl.style.width = `${p.width * (1 - 2 * m.sideMargin)}px`;
    captionEl.style.top = `${p.top - s.top + m.centerY * p.height}px`;
    captionEl.style.fontSize = `${m.fontPx}px`;
    captionEl.style.webkitTextStroke = `${m.outlinePx * 2}px ${colours.caption_outline}`;
    approxEl.style.left = `${p.left - s.left + 6}px`;
    approxEl.style.top = `${p.top - s.top + 6}px`;
    approxEl.style.maxWidth = `${Math.max(0, p.width - 12)}px`;   // one line, cut short on a narrow picture
    const line = L.captionAt(side.data.captions_events, video.currentTime, values, original);
    if (!line) return;
    line.words.forEach((w, i) => {
      const span = el("span", "", w);
      span.style.color = i === line.spoken ? colours.accent : colours.primary;
      captionEl.append(span, document.createTextNode(i < line.words.length - 1 ? " " : ""));
    });
  }

  // --- sending ----------------------------------------------------------------------------------

  async function send() {
    const r = R.render();
    if (!r) return;
    const { rows, problem } = L.styleRows(r.clip, r.version, st.groups, st.edits, st.saveAs, noteEl.value);
    if (problem || !rows.length) return;
    sendBtn.disabled = true;
    const key = st.key;
    try {
      await App.send(rows);
      App.setStatus(`Sent ${rows.length} style change${rows.length === 1 ? "" : "s"}. The next version appears here when it is rendered.`);
      if (st.key === key) {
        st.edits = {};
        st.saveAs = {};
        noteEl.value = "";
        saveDraft();
        renderGroups();
      }
    } catch (e) {
      App.setStatus(`Not sent: ${e.message}`, true);
      update();
    }
  }

  function init() {
    box = document.getElementById("style-panel");
    notice = el("p", "notice");
    notice.hidden = true;
    groupsEl = el("div", "style-groups");
    noteEl = el("textarea");
    noteEl.rows = 2;
    noteEl.placeholder = "Note for these changes (optional)";
    noteEl.setAttribute("aria-label", "Note for the style changes");
    noteEl.addEventListener("input", () => { saveDraft(); update(); });
    sendBtn = el("button", "primary", "Send style changes");
    sendBtn.type = "button";
    sendBtn.disabled = true;
    sendBtn.addEventListener("click", send);
    statusEl = el("p", "sent-status");
    const bar = el("div", "send-bar");
    bar.append(noteEl, sendBtn);
    sentEl = el("ol", "style-sent");
    sentEl.hidden = true;
    box.append(el("h2", "", "Style"), notice, groupsEl, bar, statusEl, sentEl);

    const stage = R.stage();
    captionEl = el("div", "caption-preview live-caption");
    captionEl.hidden = true;
    captionEl.setAttribute("aria-live", "polite");
    approxEl = el("span", "approx live-approx", "Approximate caption preview: the render is exact");
    approxEl.hidden = true;
    stage.append(captionEl, approxEl);

    R.on("render", load);
    R.on("time", renderPreview);
    R.on("resize", renderPreview);
    R.on("rows", renderSent);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
