// Loop listener for overlay sample pages in the job page's Look tab.
//
// stages/review_server.py injects this into every sample page, right after
// seek-shim.js and before the page's own scripts. The page runs in an
// <iframe sandbox="allow-scripts"> with an opaque origin, so the Look tab
// cannot reach into it; the two talk over postMessage instead:
//   - when the page has loaded and its fonts are ready, this calls
//     __veReady() (seek-shim.js) and tells the parent the page's size and how
//     long its animations run: {type: "ready", width, height, duration};
//   - the parent then sends {type: "seek", t} on every frame it shows, and
//     this calls __veFrame(t), the same seek the renderer uses.
// Messages from anything but the parent window are ignored.
(function () {
  if (window.__veSampleLoop) return;
  window.__veSampleLoop = true;
  var DEFAULT_S = 4, MAX_S = 30, MAX_PX = 4096;

  // How long the page's animations run, in seconds: the latest end of a Web
  // or CSS animation, GSAP's global timeline or a lottie animation.
  function duration() {
    var end = 0;
    try {
      document.getAnimations().forEach(function (a) {
        var ct = a.effect && a.effect.getComputedTiming ? a.effect.getComputedTiming() : null;
        if (ct && isFinite(ct.endTime)) end = Math.max(end, ct.endTime / 1000);
      });
    } catch (e) {}
    var g = window.gsap;
    try {
      if (g && g.globalTimeline && typeof g.globalTimeline.duration === "function") {
        var d = g.globalTimeline.duration();
        if (isFinite(d)) end = Math.max(end, d);
      }
    } catch (e) {}
    var lottie = window.lottie;
    try {
      if (lottie && typeof lottie.getRegisteredAnimations === "function") {
        lottie.getRegisteredAnimations().forEach(function (an) {
          if (an.totalFrames && an.frameRate) end = Math.max(end, an.totalFrames / an.frameRate);
        });
      }
    } catch (e) {}
    return end > 0 ? Math.min(end, MAX_S) : DEFAULT_S;
  }

  // The page's own size: overlay pages set html and body to their region's
  // pixels (references/styles/README.md).
  function size() {
    var h = document.documentElement.getBoundingClientRect();
    var b = document.body ? document.body.getBoundingClientRect() : { width: 0, height: 0 };
    function px(v) { return Math.max(1, Math.min(MAX_PX, Math.ceil(v) || 1)); }
    return { width: px(Math.max(h.width, b.width)), height: px(Math.max(h.height, b.height)) };
  }

  function ready() {
    var mode = typeof window.__veReady === "function" ? window.__veReady() : null;
    var s = size();
    window.parent.postMessage({ type: "ready", width: s.width, height: s.height,
                                duration: duration(), seek: mode }, "*");
  }

  window.addEventListener("message", function (ev) {
    if (ev.source !== window.parent) return;
    var d = ev.data;
    if (!d || d.type !== "seek" || typeof d.t !== "number" || !isFinite(d.t)) return;
    if (typeof window.__veFrame === "function") window.__veFrame(Math.max(0, d.t));
  });

  window.addEventListener("load", function () {
    var fonts = document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve();
    fonts.then(ready, ready);
  });
})();
