// Look tab: the overlay styles, brands and caption options the session offers (looks.json).
// This version lists them; picking one in the page comes in a later version.
"use strict";

(() => {
  const { L } = App;
  let notice, body;

  function render(looks) {
    App.notice(notice, L.inputNotice("looks", looks));
    body.replaceChildren();
    body.hidden = !looks || looks.status !== "ok";
    if (body.hidden) return;
    for (const [key, title] of [["styles", "Overlay styles"], ["brands", "Brands"], ["captions", "Caption options"]]) {
      const items = looks.data[key] || [];
      if (!items.length) continue;
      const h = document.createElement("h2");
      h.textContent = title;
      const ul = document.createElement("ul");
      ul.className = "plain";
      for (const it of items) {
        const li = document.createElement("li");
        li.textContent = it.name;
        ul.append(li);
      }
      body.append(h, ul);
    }
    const hint = document.createElement("p");
    hint.className = "hint";
    hint.textContent = "Previews and picking a look here come in a later version of this page. For now, tell the session which ones you want in chat.";
    body.append(hint);
  }

  App.registerTab("look", {
    init(panel) {
      notice = panel.querySelector(".notice");
      body = panel.querySelector(".body");
    },
    isOpen: () => false,
    update(s, changed) { if (changed.looks) render(s.looks); },
  });
})();
