// Music tab: the beds and sound-effect sets the session offers (music.json). Every bed and
// sound plays; a slider sets the bed's level (Web Audio gain); "Play with voice" plays the
// voice sample from the source with the chosen bed under it. "Send music" adds one `music` row.
"use strict";

(() => {
  const { L } = App;
  let notice, body, bedList, sfxList, level, levelOut, voiceBtn, voiceHint, noteEl, sendBtn, problemEl, sentEl;
  let music = null, draft = null, draftKey = null;
  // One element per role, each routed through Web Audio once (an element can be routed only once).
  let bedEl, voiceEl, sfxEl, ctx = null, bedGain = null;
  let playing = null;              // {kind: "bed" | "sfx" | "voice", name}
  let voiceUntil = null;

  const save = () => { if (draftKey) App.store.set(draftKey, draft); };

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function radio(name, value, label, checked, onPick) {
    const lab = el("label", "choice");
    const input = el("input");
    input.type = "radio";
    input.name = name;
    input.value = value === null ? "" : value;
    input.checked = checked;
    input.addEventListener("change", () => { if (input.checked) onPick(value); });
    lab.append(input, el("span", "", label));
    return lab;
  }

  function lastSent() { return L.lastOfKind(App.state.rows, "music"); }
  const audioUrl = (path) => App.mediaUrl("/media/audio", { path });
  const baseName = (p) => p.split("/").pop();

  // --- audio -------------------------------------------------------------------------

  /* The Web Audio graph, made on the first click (browsers start audio only after one). */
  function audio() {
    if (!ctx) {
      ctx = new AudioContext();
      bedGain = ctx.createGain();
      ctx.createMediaElementSource(bedEl).connect(bedGain).connect(ctx.destination);
      ctx.createMediaElementSource(voiceEl).connect(ctx.destination);
      const sfxGain = ctx.createGain();
      sfxGain.gain.value = L.dbToGain(L.SFX_VOLUME_DB);
      ctx.createMediaElementSource(sfxEl).connect(sfxGain).connect(ctx.destination);
    }
    if (ctx.state === "suspended") ctx.resume();
    bedGain.gain.value = L.dbToGain(draft.volume_db);
  }

  function stopAll() {
    for (const a of [bedEl, voiceEl, sfxEl]) if (!a.paused) a.pause();
    playing = null;
    voiceUntil = null;
    renderButtons();
  }

  function setSrc(a, url) {
    if (a.dataset.url !== url) { a.dataset.url = url; a.src = url; }
  }

  async function play(kind, name, file) {
    const again = playing && playing.kind === kind && playing.name === name;
    stopAll();
    if (again) return;                       // a second click on the same button stops it
    audio();
    const a = kind === "bed" ? bedEl : sfxEl;
    setSrc(a, audioUrl(file));
    a.currentTime = 0;
    playing = { kind, name };
    renderButtons();
    try { await a.play(); } catch (e) { App.setStatus(`Cannot play ${baseName(file)}: ${e.message}`, true); stopAll(); }
  }

  async function playWithVoice() {
    const again = playing && playing.kind === "voice";
    stopAll();
    if (again) return;
    const vs = music.data.voice_sample;
    const bed = (music.data.beds || []).find((b) => b.name === draft.bed);
    if (!vs) return;
    audio();
    setSrc(voiceEl, App.mediaUrl("/media/source"));
    voiceEl.currentTime = vs.start;
    voiceUntil = vs.end;
    playing = { kind: "voice", name: null };
    renderButtons();
    try {
      const starts = [voiceEl.play()];
      if (bed) {
        setSrc(bedEl, audioUrl(bed.file));
        bedEl.currentTime = 0;
        starts.push(bedEl.play());
      }
      await Promise.all(starts);
    } catch (e) {
      App.setStatus(`Cannot play the voice sample: ${e.message}`, true);
      stopAll();
    }
  }

  // --- the lists ---------------------------------------------------------------------

  function playButton(kind, name, file, label) {
    const b = el("button", "play-btn", label);
    b.type = "button";
    b.dataset.kind = kind;
    b.dataset.name = name;
    b.dataset.label = label;
    b.addEventListener("click", () => play(kind, name, file));
    return b;
  }

  function renderButtons() {
    for (const b of body.querySelectorAll("button.play-btn")) {
      const on = !!playing && playing.kind === b.dataset.kind && playing.name === b.dataset.name;
      b.textContent = on ? "■ Stop" : b.dataset.label;
      b.setAttribute("aria-pressed", String(on));
    }
    const voiceOn = !!playing && playing.kind === "voice";
    voiceBtn.textContent = voiceOn ? "■ Stop" : "▶ Play with voice";
    voiceBtn.setAttribute("aria-pressed", String(voiceOn));
  }

  function pick(field, value) {
    draft[field] = value;
    save();
    renderChoices();
    renderVoice();
    renderSend();
  }

  function renderLists() {
    stopAll();
    bedList.replaceChildren();
    for (const b of music.data.beds || []) {
      const li = el("li", "audio-row");
      li.append(radio("music-bed", b.name, b.name, draft.bed === b.name, (v) => pick("bed", v)),
        playButton("bed", b.name, b.file, "▶ Play"), el("span", "muted file", baseName(b.file)));
      bedList.append(li);
    }
    const noBed = el("li", "audio-row none");
    noBed.append(radio("music-bed", null, "No music bed", draft.bed === null, (v) => pick("bed", v)));
    bedList.append(noBed);

    sfxList.replaceChildren();
    for (const s of music.data.sfx_sets || []) {
      const li = el("li", "audio-row sfx-set");
      const sounds = el("div", "sounds");
      s.files.forEach((f, i) => sounds.append(playButton("sfx", `${s.name}#${i}`, f, `▶ ${baseName(f)}`)));
      li.append(radio("music-sfx", s.name, s.name, draft.sfx_set === s.name, (v) => pick("sfx_set", v)), sounds);
      sfxList.append(li);
    }
    const noSfx = el("li", "audio-row none");
    noSfx.append(radio("music-sfx", null, "No sound effects", draft.sfx_set === null, (v) => pick("sfx_set", v)));
    sfxList.append(noSfx);
    renderButtons();
  }

  function renderChoices() {
    for (const input of body.querySelectorAll("input[type=radio]")) {
      const field = { "music-bed": "bed", "music-sfx": "sfx_set" }[input.name];
      input.checked = (draft[field] === null ? "" : draft[field]) === input.value;
    }
    level.value = String(draft.volume_db);
    levelOut.textContent = L.fmtDb(draft.volume_db);
  }

  function renderVoice() {
    const vs = music.data.voice_sample;
    const job = App.state.job;
    let why = null;
    if (!vs) why = "music.json has no voice_sample, so there is no speech to play the bed under.";
    else if (!job || job.status !== "ok") why = "The voice sample plays from the recording job.json names; job.json is not ready yet.";
    voiceBtn.disabled = !!why;
    voiceHint.textContent = why || `Plays ${L.fmt(vs.start)}–${L.fmt(vs.end)} of the recording with ${
      draft.bed ? `“${draft.bed}”` : "no bed"} under it at this level. Approximate: the render evens out the bed's loudness first, so judge the balance, not the exact level.`;
  }

  // --- sending -----------------------------------------------------------------------

  function renderSend() {
    const row = L.musicRow(draft);
    const problem = L.musicProblem(row);
    const last = lastSent();
    const unchanged = L.sameChoice(row, last, ["bed", "volume_db", "sfx_set"]);
    sendBtn.disabled = !!problem || unchanged;
    problemEl.textContent = problem || (unchanged ? "Sent; change something to send again." : "");
    sentEl.textContent = last
      ? `Sent: bed ${last.bed || "none"} at ${L.fmtDb(last.volume_db)} · sound effects ${last.sfx_set || "none"}`
      : "";
  }

  async function send() {
    const row = L.musicRow(draft);
    if (L.musicProblem(row)) return;
    sendBtn.disabled = true;
    try {
      await App.send([row]);
      App.setStatus("Sent the music. The session uses it in every EDL it writes.");
    } catch (e) {
      App.setStatus(`Not sent: ${e.message}`, true);
    }
    renderSend();
  }

  // --- the tab -----------------------------------------------------------------------

  function update(s, changed) {
    if (!(changed.music || changed.rows || changed.job)) return;
    App.notice(notice, L.inputNotice("music", s.music));
    body.hidden = !s.music || s.music.status !== "ok";
    if (body.hidden) { if (bedEl) stopAll(); return; }
    const musicChanged = changed.music || music === null;
    music = s.music;
    const key = `music:${music.file}`;
    if (key !== draftKey) { draftKey = key; draft = App.store.get(key, null); }
    draft = L.musicDraft(music, draft, lastSent());
    noteEl.value = draft.note;
    if (musicChanged) renderLists();
    renderChoices();
    renderVoice();
    renderSend();
  }

  function init(panel) {
    notice = panel.querySelector(".notice");
    body = panel.querySelector(".body");
    bedEl = el("audio");
    bedEl.loop = true;
    bedEl.preload = "none";
    voiceEl = el("audio");
    voiceEl.preload = "none";
    sfxEl = el("audio");
    sfxEl.preload = "none";
    bedList = el("ol", "audio-list");
    sfxList = el("ol", "audio-list");

    const levelRow = el("div", "row level-row");
    level = el("input");
    level.type = "range";
    level.min = String(L.MIN_VOLUME_DB);
    level.max = String(L.MAX_VOLUME_DB);
    level.step = "1";
    level.id = "music-level";
    level.setAttribute("aria-label", "Music bed level in dB");
    levelOut = el("output", "level-out", L.fmtDb(L.DEFAULT_VOLUME_DB));
    levelOut.htmlFor = "music-level";
    voiceBtn = el("button", "", "▶ Play with voice");
    voiceBtn.type = "button";
    voiceBtn.id = "music-voice";
    levelRow.append(el("label", "", "Bed level"), level, levelOut, voiceBtn);
    voiceHint = el("p", "hint", "");

    const foot = el("div", "send-bar");
    noteEl = el("textarea");
    noteEl.rows = 1;
    noteEl.placeholder = "Note for the session (optional)";
    noteEl.setAttribute("aria-label", "Note on the music");
    sendBtn = el("button", "primary", "Send music");
    sendBtn.type = "button";
    sendBtn.id = "send-music";
    problemEl = el("span", "muted", "");
    sentEl = el("p", "sent-status");
    foot.append(noteEl, sendBtn, problemEl);

    body.append(el("h2", "", "Music bed"), bedList, levelRow, voiceHint,
      el("h2", "", "Sound effects"),
      el("p", "hint", `Sounds play at ${L.fmtDb(L.SFX_VOLUME_DB)}, the level a render gives them unless the EDL says otherwise. The session places them where sfx-suggest proposes, and you review them in the renders.`),
      sfxList, foot, sentEl, bedEl, voiceEl, sfxEl);

    level.addEventListener("input", () => {
      draft.volume_db = L.clampDb(Number(level.value));
      levelOut.textContent = L.fmtDb(draft.volume_db);
      if (bedGain) bedGain.gain.value = L.dbToGain(draft.volume_db);
      save();
      renderSend();
    });
    voiceBtn.addEventListener("click", playWithVoice);
    voiceEl.addEventListener("timeupdate", () => {
      if (voiceUntil !== null && voiceEl.currentTime >= voiceUntil) stopAll();
    });
    for (const a of [bedEl, sfxEl]) {
      a.addEventListener("ended", () => { if (playing && playing.kind !== "voice") stopAll(); });
    }
    noteEl.addEventListener("input", () => { draft.note = noteEl.value; save(); renderSend(); });
    sendBtn.addEventListener("click", send);
  }

  App.registerTab("music", {
    init,
    isOpen: L.musicOpen,
    update,
    hide() { if (bedEl) stopAll(); },
  });
})();
