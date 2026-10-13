// Clips tab: the session's clip candidates (candidates.json).
"use strict";

(() => {
  const { L } = App;
  let notice;

  function render(s) {
    const reels = L.isReels(s.job);
    if (reels === false) return App.notice(notice, { text: "Not used for demos: this job is a screencast demo." });
    App.notice(notice, L.inputNotice("job", s.job) || L.inputNotice("candidates", s.candidates));
  }

  App.registerTab("clips", {
    init(panel) { notice = panel.querySelector(".notice"); },
    isOpen: L.clipsOpen,
    update(s, changed) { if (changed.job || changed.candidates || changed.rows) render(s); },
  });
})();
