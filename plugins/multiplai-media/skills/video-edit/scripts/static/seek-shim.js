// Seek shim for overlay pages (stages/overlay.py). Installed with
// page.addInitScript, so it runs before any script on the page.
//
// It gives the page a clock that only moves when the renderer says so, then
// seeks every animation it can find to that time:
//   - Date, performance.now and requestAnimationFrame read a virtual clock;
//     __veFrame(t) sets it and runs the queued rAF callbacks once;
//   - Web Animations: Element.prototype.animate is wrapped once, so animations
//     made later are tracked without calling document.getAnimations() per frame;
//   - CSS animations: collected once by __veReady(), after fonts load. An element
//     with no live animation then (display:none) is posed with a negative
//     animation-delay and animation-play-state: paused instead;
//   - GSAP: window.gsap's global timeline is paused and set with totalTime(t, true);
//   - Lottie (lottie-web): every lottie.getRegisteredAnimations() item is set
//     with goToAndStop(frame, true).
// A page that defines its own window.__seek(t) gets that called instead of
// the default seek; the clock and rAF still advance.
//
// Adapted from HyperFrames (heygen-com/hyperframes @ bb9e507, Apache-2.0):
// packages/producer/src/services/fileServer.ts (virtual clock),
// packages/core/src/runtime/adapters/{waapi,css,gsap,lottie}.ts (seeking).
(function () {
  if (window.__veFrame) return;

  var nowMs = 0;
  var rafId = 1;
  var rafQueue = [];
  var OriginalDate = Date;

  function VirtualDate() {
    var args = Array.prototype.slice.call(arguments);
    if (!(this instanceof VirtualDate)) {
      return OriginalDate.apply(null, args.length ? args : [nowMs]);
    }
    var d = args.length
      ? new (Function.prototype.bind.apply(OriginalDate, [null].concat(args)))()
      : new OriginalDate(nowMs);
    Object.setPrototypeOf(d, VirtualDate.prototype);
    return d;
  }
  VirtualDate.prototype = OriginalDate.prototype;
  Object.setPrototypeOf(VirtualDate, OriginalDate);
  VirtualDate.now = function () { return nowMs; };
  VirtualDate.parse = OriginalDate.parse.bind(OriginalDate);
  VirtualDate.UTC = OriginalDate.UTC.bind(OriginalDate);
  try {
    Object.defineProperty(window, "Date", { configurable: true, writable: true, value: VirtualDate });
  } catch (e) {}
  try {
    Object.defineProperty(window.performance, "now", {
      configurable: true, value: function () { return nowMs; },
    });
  } catch (e) {}

  window.requestAnimationFrame = function (callback) {
    if (typeof callback !== "function") return 0;
    var entry = { id: rafId++, callback: callback, cancelled: false };
    rafQueue.push(entry);
    return entry.id;
  };
  window.cancelAnimationFrame = function (id) {
    for (var i = 0; i < rafQueue.length; i++) {
      if (rafQueue[i].id === id) rafQueue[i].cancelled = true;
    }
  };
  function flushAnimationFrames() {
    var current = rafQueue.slice();
    rafQueue.length = 0;
    for (var i = 0; i < current.length; i++) {
      if (current[i].cancelled) continue;
      try { current[i].callback(nowMs); } catch (e) {}
    }
  }

  var tracked = new Set();
  var proto = Element.prototype;
  var originalAnimate = proto.animate;
  if (typeof originalAnimate === "function") {
    proto.animate = function () {
      var a = originalAnimate.apply(this, arguments);
      tracked.add(a);
      return a;
    };
  }

  // Elements whose CSS animation had no live handle when collected.
  var posed = [];
  function seconds(list) {
    return list.split(",").map(function (s) {
      s = s.trim();
      return s.endsWith("ms") ? parseFloat(s) / 1000 : parseFloat(s) || 0;
    });
  }

  window.__veReady = function () {
    document.getAnimations().forEach(function (a) { tracked.add(a); });
    var all = document.querySelectorAll("*");
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      var cs = getComputedStyle(el);
      if (cs.animationName === "none" || el.getAnimations().length) continue;
      posed.push({ el: el, delays: seconds(cs.animationDelay) });
    }
    return typeof window.__seek === "function" ? "page" : "default";
  };

  function defaultSeek(t) {
    var ms = t * 1000;
    var gsap = window.gsap;
    if (gsap && gsap.globalTimeline) {
      gsap.globalTimeline.pause();
      gsap.globalTimeline.totalTime(t, true);
    }
    var lottie = window.lottie;
    if (lottie && typeof lottie.getRegisteredAnimations === "function") {
      lottie.getRegisteredAnimations().forEach(function (anim) {
        var total = anim.totalFrames || 0;
        var frame = t * (anim.frameRate || 30);
        if (total > 0) frame = anim.loop === true ? frame % total : Math.min(frame, total - 1);
        anim.goToAndStop(frame, true);
      });
    }
    tracked.forEach(function (a) {
      try { a.pause(); a.currentTime = ms; } catch (e) {}
    });
    for (var i = 0; i < posed.length; i++) {
      var p = posed[i];
      p.el.style.animationPlayState = "paused";
      p.el.style.animationDelay = p.delays.map(function (d) {
        return Number((d - t).toFixed(3)) + "s";
      }).join(", ");
    }
    return tracked.size;
  }

  window.__veFrame = function (t) {
    nowMs = Math.max(0, t * 1000);
    var r = typeof window.__seek === "function" ? window.__seek(t) : defaultSeek(t);
    flushAnimationFrames();
    return r;
  };
})();
