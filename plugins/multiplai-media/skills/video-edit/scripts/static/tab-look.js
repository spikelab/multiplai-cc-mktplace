// Look tab: the overlay styles, brands and caption options the session offers (looks.json).
// Each style's sample page loops in a sandboxed frame; brands show their colours and font;
// caption options draw over a frame of the source. "Send look" adds one `look` row.
"use strict";

(() => {
  const { L } = App;
  let notice, body, styleList, brandList, captionList, stage, video, captionEl, scrub, timeEl, noteEl, sendBtn,
    problemEl, sentEl, approxEl;
  let looks = null, brands = [], draft = null, draftKey = null;
  let frames = [];                 // [{iframe, box, ready: {width, height, duration} | null, t0}]
  // Sample frames are made only while the tab shows: a page loaded under a hidden panel
  // has no layout, so it would report no size and no animations to loop.
  let stylesStale = true;
  let words = [], wordsAt = null, wordsTimer = null, rafId = null, lastTick = 0;

  const save = () => { if (draftKey) App.store.set(draftKey, draft); };

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  /* A labelled radio button in group `name`; `checked` and `onPick` as given. */
  function radio(name, value, label, checked, onPick, disabled) {
    const lab = el("label", "choice");
    const input = el("input");
    input.type = "radio";
    input.name = name;
    input.value = value === null ? "" : value;
    input.checked = checked;
    input.disabled = !!disabled;
    input.addEventListener("change", () => { if (input.checked) onPick(value); });
    lab.append(input, el("span", "", label));
    return lab;
  }

  function lastSent() { return L.lastOfKind(App.state.rows, "look"); }

  function pick(field, value) {
    draft[field] = value;
    save();
    renderChoices();
    renderCaption();
    renderSend();
  }

  // --- overlay styles: sample pages in sandboxed frames ------------------------------

  function sampleUrl(path) {
    const key = looks.sample_keys && looks.sample_keys[path];
    return key ? `/media/sample?${new URLSearchParams({ path, k: key })}` : null;
  }

  function renderStyles() {
    stylesStale = false;
    stopLoop();
    styleList.replaceChildren();
    frames = [];
    const out = L.outputSize(App.state.job);
    for (const s of looks.data.styles || []) {
      const li = el("li", draft.style === s.name ? "sample-card selected" : "sample-card");
      li.dataset.name = s.name;
      const box = el("div", "sample-box");
      const url = sampleUrl(s.sample_html);
      if (url) {
        const iframe = el("iframe");
        // Scripts only: the page gets an opaque origin, so it cannot reach this page,
        // its storage or the server's API (and its CSP lets it fetch nothing).
        iframe.setAttribute("sandbox", "allow-scripts");
        iframe.setAttribute("referrerpolicy", "no-referrer");
        iframe.title = `Sample of the ${s.name} style`;
        iframe.style.width = `${out.width}px`;
        iframe.style.height = `${out.height}px`;
        iframe.src = url;
        box.append(iframe);
        frames.push({ iframe, box, ready: null, t0: 0 });
      }
      li.append(box, radio("look-style", s.name, s.name, draft.style === s.name, (v) => pick("style", v)),
        el("p", "muted file", s.file.split("/").pop()));
      styleList.append(li);
    }
    const none = el("li", "sample-card none");
    none.append(radio("look-style", null, "No overlay style", draft.style === null, (v) => pick("style", v)));
    styleList.append(none);
    if (App.current() === "look") startLoop();
  }

  function fitFrame(f) {
    if (!f.ready) return;
    const scale = L.fitScale(f.ready.width, f.ready.height, f.box.clientWidth, f.box.clientHeight);
    f.iframe.style.width = `${f.ready.width}px`;
    f.iframe.style.height = `${f.ready.height}px`;
    f.iframe.style.transform = `scale(${scale})`;
    f.iframe.style.left = `${(f.box.clientWidth - f.ready.width * scale) / 2}px`;
    f.iframe.style.top = `${(f.box.clientHeight - f.ready.height * scale) / 2}px`;
  }

  // Only "ready" messages, only from a sample frame this tab made (event.source), and
  // only from an opaque origin: a sandboxed frame without same-origin rights.
  function onMessage(ev) {
    if (ev.origin !== "null") return;
    const f = frames.find((x) => x.iframe.contentWindow === ev.source);
    const ready = f && L.readyMessage(ev.data);
    if (!ready) return;
    f.ready = ready;
    f.t0 = performance.now();
    f.box.classList.add("ready");
    fitFrame(f);
  }

  function tick(now) {
    rafId = requestAnimationFrame(tick);
    if (now - lastTick < 1000 / 30) return;        // the renderer's 30 fps is plenty
    lastTick = now;
    for (const f of frames) {
      if (!f.ready || !f.iframe.contentWindow) continue;
      f.iframe.contentWindow.postMessage({ type: "seek", t: L.loopTime((now - f.t0) / 1000, f.ready.duration) }, "*");
    }
  }

  function startLoop() { if (rafId === null && frames.length) rafId = requestAnimationFrame(tick); }
  function stopLoop() { if (rafId !== null) cancelAnimationFrame(rafId); rafId = null; }

  // --- brands ------------------------------------------------------------------------

  async function loadBrands() {
    try { brands = (await App.api("/api/brands")).brands; } catch (e) { brands = []; }
  }

  function chip(label, colour) {
    const c = el("span", "chip");
    const sw = el("span", "swatch");
    sw.style.background = colour;
    c.append(sw, el("span", "", `${label} ${colour}`));
    return c;
  }

  function renderBrands() {
    brandList.replaceChildren();
    for (const b of looks.data.brands || []) {
      const info = brands.find((x) => x.name === b.name);
      const li = el("li", "brand-card");
      const bad = info && info.status !== "ok";
      li.append(radio("look-brand", b.name, b.name, draft.brand === b.name, (v) => pick("brand", v), bad));
      if (bad) {
        li.append(el("p", "error-text", `Cannot use this brand file: ${info.error}`));
      } else if (info) {
        const chips = el("div", "chips");
        const c = L.brandColours(info);
        chips.append(chip("Text", c.primary), chip("Spoken word", c.accent), chip("Background", c.background),
          chip("Outline", c.caption_outline));
        li.append(chips, el("p", "muted", `Font: ${info.font || "the default caption font"}`));
      }
      brandList.append(li);
    }
    const none = el("li", "brand-card none");
    none.append(radio("look-brand", null, "No brand (white captions, yellow spoken word)", draft.brand === null,
      (v) => pick("brand", v)));
    brandList.append(none);
  }

  // --- caption options over a source frame ------------------------------------------

  function summary(values) {
    const parts = [];
    if (values.size !== undefined) parts.push(`${values.size} px`);
    if (values.position_y !== undefined) parts.push(`at ${Math.round(values.position_y * 100)}% height`);
    if (values.words_per_line !== undefined) parts.push(`${values.words_per_line} word${values.words_per_line === 1 ? "" : "s"} a line`);
    if (values.highlight !== undefined) parts.push(values.highlight ? "spoken word coloured" : "no highlight");
    return parts.join(" · ") || "the defaults";
  }

  function renderCaptionOptions() {
    captionList.replaceChildren();
    for (const o of looks.data.captions || []) {
      const li = el("li", "caption-option");
      li.append(radio("look-caption", o.name, o.name, draft.caption === o.name, (v) => pick("caption", v)),
        el("span", "muted", summary(o.values)));
      captionList.append(li);
    }
    const none = el("li", "caption-option none");
    none.append(radio("look-caption", null, "Keep the EDL's captions", draft.caption === null, (v) => pick("caption", v)));
    captionList.append(none);
  }

  function chosenBrand() {
    return draft.brand === null ? null : brands.find((b) => b.name === draft.brand) || null;
  }

  function renderCaption() {
    const out = L.outputSize(App.state.job);
    stage.style.setProperty("--ar", String(out.width / out.height));
    const opt = (looks.data.captions || []).find((o) => o.name === draft.caption);
    captionEl.replaceChildren();
    captionEl.hidden = !opt || !App.state.job || App.state.job.status !== "ok";
    if (captionEl.hidden) return;
    const m = L.captionMetrics(opt.values, out, stage.clientHeight || 1);
    const c = L.brandColours(chosenBrand());
    const line = L.captionLine(words.length ? words : [{ text: "Captions show here" }], opt.values);
    captionEl.style.top = `${m.centerY * 100}%`;
    captionEl.style.left = captionEl.style.right = `${m.sideMargin * 100}%`;
    captionEl.style.fontSize = `${m.fontPx}px`;
    captionEl.style.webkitTextStroke = `${m.outlinePx * 2}px ${c.caption_outline}`;
    line.words.forEach((w, i) => {
      const span = el("span", "", w);
      span.style.color = i === line.spoken ? c.accent : c.primary;
      captionEl.append(span, document.createTextNode(i < line.words.length - 1 ? " " : ""));
    });
    const b = chosenBrand();
    approxEl.textContent = `Approximate: the render burns captions in with ffmpeg${b && b.font ? `, in ${b.font}` : ""}.`;
  }

  function seek(t) {
    if (!Number.isFinite(video.duration)) return;
    video.currentTime = Math.max(0, Math.min(video.duration, t));
  }

  // The words under the frame, for the sample caption line (one request per pause in scrubbing).
  function loadWords() {
    clearTimeout(wordsTimer);
    wordsTimer = setTimeout(async () => {
      const t = Math.round(video.currentTime * 10) / 10;
      if (t === wordsAt) return;
      wordsAt = t;
      try { words = (await App.api(`/api/transcript?start=${t}&end=${t + 10}`)).words; } catch (e) { words = []; }
      renderCaption();
    }, 250);
  }

  // --- sending -----------------------------------------------------------------------

  function renderChoices() {
    for (const input of body.querySelectorAll("input[type=radio]")) {
      const field = { "look-style": "style", "look-brand": "brand", "look-caption": "caption" }[input.name];
      input.checked = (draft[field] === null ? "" : draft[field]) === input.value;
    }
    for (const li of styleList.children) li.classList.toggle("selected", li.dataset.name === draft.style);
  }

  function renderSend() {
    const row = L.lookRow(draft, looks);
    const problem = L.lookProblem(row);
    const last = lastSent();
    const unchanged = L.sameChoice(row, last, ["style", "brand", "captions"]);
    sendBtn.disabled = !!problem || unchanged;
    problemEl.textContent = problem || (unchanged ? "Sent; change something to send again." : "");
    sentEl.textContent = last
      ? `Sent: style ${last.style || "none"} · brand ${last.brand || "none"} · captions ${last.captions ? summary(last.captions) : "unchanged"}`
      : "";
  }

  async function send() {
    const row = L.lookRow(draft, looks);
    if (L.lookProblem(row)) return;
    sendBtn.disabled = true;
    try {
      await App.send([row]);
      App.setStatus("Sent the look. The session uses it in every EDL and overlay page it writes.");
    } catch (e) {
      App.setStatus(`Not sent: ${e.message}`, true);
    }
    renderSend();
  }

  // --- the tab -----------------------------------------------------------------------

  async function update(s, changed) {
    if (!(changed.looks || changed.rows || changed.job)) return;
    App.notice(notice, L.inputNotice("looks", s.looks));
    body.hidden = !s.looks || s.looks.status !== "ok";
    if (body.hidden) { stopLoop(); return; }
    const looksChanged = changed.looks || looks === null;
    looks = s.looks;
    const key = `look:${looks.file}`;
    if (key !== draftKey) { draftKey = key; draft = App.store.get(key, null); }
    draft = L.lookDraft(looks, draft, lastSent());
    noteEl.value = draft.note;
    if (looksChanged) {
      await loadBrands();
      if (App.current() === "look") renderStyles(); else stylesStale = true;
      renderBrands();
      renderCaptionOptions();
    }
    // Whichever of job.json and looks.json came first: the source is set once both are valid.
    if (s.job && s.job.status === "ok" && !video.src) video.src = App.mediaUrl("/media/source");
    renderChoices();
    renderCaption();
    renderSend();
  }

  function init(panel) {
    notice = panel.querySelector(".notice");
    body = panel.querySelector(".body");
    styleList = el("ol", "sample-cards");
    brandList = el("ol", "brand-cards");
    captionList = el("ol", "caption-options");

    stage = el("div", "caption-stage");
    video = el("video");
    video.id = "look-video";
    video.preload = "metadata";
    video.muted = true;
    video.playsInline = true;
    captionEl = el("div", "caption-preview");
    captionEl.setAttribute("aria-live", "polite");
    approxEl = el("span", "approx", "Approximate");
    stage.append(video, captionEl, approxEl);
    const controls = el("div", "row");
    scrub = el("input");
    scrub.type = "range";
    scrub.min = "0";
    scrub.step = "0.1";
    scrub.value = "0";
    scrub.id = "look-scrub";
    scrub.setAttribute("aria-label", "Frame time");
    timeEl = el("span", "muted", "0:00.0");
    controls.append(scrub, timeEl);
    const capCol = el("div", "caption-col");
    capCol.append(stage, controls, captionList);

    const grid = el("div", "look-grid");
    const left = el("div", "");
    left.append(el("h2", "", "Overlay style"),
      el("p", "hint", "Each sample loops as the renderer would play it. A keyed style shows its key colour here; the render removes it. The renders use the style for every overlay page."),
      styleList, el("h2", "", "Brand"), brandList);
    const right = el("div", "");
    right.append(el("h2", "", "Captions"), capCol);
    grid.append(left, right);

    const foot = el("div", "send-bar");
    noteEl = el("textarea");
    noteEl.rows = 1;
    noteEl.placeholder = "Note for the session (optional)";
    noteEl.setAttribute("aria-label", "Note on the look");
    sendBtn = el("button", "primary", "Send look");
    sendBtn.type = "button";
    sendBtn.id = "send-look";
    problemEl = el("span", "muted", "");
    sentEl = el("p", "sent-status");
    foot.append(noteEl, sendBtn, problemEl);
    body.append(grid, foot, sentEl);

    video.addEventListener("loadedmetadata", () => {
      scrub.max = String(video.duration);
      seek(video.duration * 0.1);
    });
    video.addEventListener("seeked", () => {
      scrub.value = String(video.currentTime);
      timeEl.textContent = `${L.fmt(video.currentTime)} / ${L.fmt(video.duration || 0)}`;
      loadWords();
    });
    scrub.addEventListener("input", () => seek(Number(scrub.value)));
    noteEl.addEventListener("input", () => { draft.note = noteEl.value; save(); renderSend(); });
    sendBtn.addEventListener("click", send);
    window.addEventListener("message", onMessage);
    window.addEventListener("resize", () => {
      if (App.current() !== "look" || !looks) return;
      frames.forEach(fitFrame);
      renderCaption();
    });
  }

  App.registerTab("look", {
    init,
    isOpen: L.lookOpen,
    update,
    show() {
      if (looks && stylesStale) renderStyles();
      startLoop();
      if (looks) { frames.forEach(fitFrame); renderCaption(); }
    },
    hide: stopLoop,
  });
})();
