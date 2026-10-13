// Speakers tab: draw a box per speaker on a frame of the source and map transcript labels to boxes.
"use strict";

(() => {
  const { L } = App;
  let notice;

  function render(s) {
    const reels = L.isReels(s.job);
    if (reels === false) return App.notice(notice, { text: "Not used for demos: this job is a screencast demo." });
    App.notice(notice, L.inputNotice("job", s.job));
  }

  App.registerTab("speakers", {
    init(panel) { notice = panel.querySelector(".notice"); },
    isOpen: L.speakersOpen,
    update(s, changed) { if (changed.job || changed.rows) render(s); },
  });
})();
