// Music tab: the beds and sound-effect sets the session offers (music.json).
// This version lists them; playing and picking them in the page comes in a later version.
"use strict";

(() => {
  const { L } = App;
  let notice, body;

  function render(music) {
    App.notice(notice, L.inputNotice("music", music));
    body.replaceChildren();
    body.hidden = !music || music.status !== "ok";
    if (body.hidden) return;
    for (const [key, title] of [["beds", "Music beds"], ["sfx_sets", "Sound-effect sets"]]) {
      const items = music.data[key] || [];
      if (!items.length) continue;
      const h = document.createElement("h2");
      h.textContent = title;
      const ul = document.createElement("ul");
      ul.className = "plain";
      for (const it of items) {
        const li = document.createElement("li");
        li.textContent = it.files ? `${it.name} (${it.files.length} sound${it.files.length === 1 ? "" : "s"})` : it.name;
        ul.append(li);
      }
      body.append(h, ul);
    }
    const hint = document.createElement("p");
    hint.className = "hint";
    hint.textContent = "Playing and picking music here come in a later version of this page. For now, tell the session which ones you want in chat.";
    body.append(hint);
  }

  App.registerTab("music", {
    init(panel) {
      notice = panel.querySelector(".notice");
      body = panel.querySelector(".body");
    },
    isOpen: () => false,
    update(s, changed) { if (changed.music) render(s.music); },
  });
})();
