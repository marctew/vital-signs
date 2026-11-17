/* Helpers for Vital Signs local pages.
 *
 *   VS.params            all query parameters as an object
 *   VS.display           { name, width, height, orientation } of the display showing the page
 *   VS.data(source, q)   Promise of JSON from the server's data proxy (/api/data/<source>)
 *   VS.every(seconds, f) run f now and then every `seconds`
 *
 * For modules (pages with a module.json, loaded with ?m=<content id>):
 *
 *   VS.module()          Promise of this instance's options (the manifest defaults without ?m)
 *   VS.moduleData()      Promise of the data the server fetches for this instance
 *   VS.retrySoon(load)   after a failed load, run `load` again in a minute
 *   VS.setActive(bool)   in a split screen, say whether there is anything to show (see "can_hide" in module.json)
 *   VS.fit(el, box)      size el's text as large as fits inside box
 *   VS.fitBlock(el, box) the same for text that wraps over several lines
 *   VS.bestGrid(box, n, ratio)  the grid of n equal tiles that comes out largest: { cols, scale }
 *   VS.clip(list)        hide the trailing children of list that do not fit in it
 *   VS.onResize(f)       run f now and whenever the page's box changes size
 *   VS.message(el, text) show a centred note in el
 *
 * For e-ink displays, where the server draws the page once and sends the picture (?medium=eink):
 *
 *   VS.medium            "screen" or "eink"; on e-ink <html> gets data-medium="eink" and eink.css is loaded
 *   VS.minText           the display's smallest text in pixels (0 for no limit). --s is kept large enough for
 *                        it, and the renderer enlarges anything still smaller before taking the picture
 *   VS.ready()           say the page is fully drawn. Called for you once VS.module(), VS.moduleData()
 *                        and VS.data() have settled, so call it only from a page that draws later than that
 *   VS.sourceTime(label, unixSeconds)   add "label HH:MM" to the strip along the bottom of the picture
 */
(function () {
  var params = {};
  new URLSearchParams(location.search).forEach(function (value, key) { params[key] = value; });
  var root = document.documentElement;

  // Sizing for modules: --s is 1% of the geometric mean of the box's sides.
  // On e-ink a display may set a smallest text size (?mintext=<pixels>). The smallest text modules use is
  // 2.6 times --s (titles), so --s is not allowed to shrink below what keeps that readable: in a small pane
  // the text stays legible and the module fits in less of it instead.
  var minText = params.medium === 'eink' ? Math.max(0, Math.min(40, Number(params.mintext) || 0)) : 0;
  function measure() {
    var w = window.innerWidth, h = window.innerHeight;
    root.style.setProperty('--s', Math.max(Math.sqrt(w * h) / 100, minText / 2.6) + 'px');
    root.dataset.shape = w / h > 1.35 ? 'wide' : (h / w > 1.35 ? 'tall' : 'square');
  }
  measure();
  window.addEventListener('resize', measure);

  // E-ink: the server's renderer waits for window.__vsReady before it takes the picture.
  var medium = params.medium === 'eink' ? 'eink' : 'screen';
  var pending = 0, readyTimer = null, isReady = false;
  function markReady() {
    if (isReady) return;
    var waiting = Array.prototype.some.call(document.images, function (img) { return !img.complete; });
    if (waiting) { readyTimer = setTimeout(markReady, 200); return; }
    isReady = true;
    requestAnimationFrame(function () { requestAnimationFrame(function () { window.__vsReady = true; }); });
  }
  function settle() {
    if (isReady) return;
    clearTimeout(readyTimer);
    readyTimer = setTimeout(function () { if (pending === 0) markReady(); }, 400);
  }
  function track(promise) {
    pending++;
    var done = function () { pending--; settle(); };
    promise.then(done, done);
    return promise;
  }
  if (medium === 'eink') {
    root.dataset.medium = 'eink';
    root.dataset.palette = params.palette || 'bw';
    track(new Promise(function (resolve) {
      var link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = '/pages/_shared/eink.css';
      link.onload = link.onerror = resolve;
      document.head.appendChild(link);
    }));
  }
  window.addEventListener('load', function () { setTimeout(settle, 1000); });   // a page that fetches nothing

  window.VS = {
    params: params,
    medium: medium,
    minText: minText,
    ready: markReady,
    sourceTime: function (label, unixSeconds) {
      if (!unixSeconds) return;
      var list = (window.__vsSources = window.__vsSources || []);
      for (var i = 0; i < list.length; i++) if (list[i].label === label) { list[i].at = unixSeconds; return; }
      list.push({ label: label, at: unixSeconds });
    },
    display: {
      name: params.display || '',
      width: Number(params.width) || window.innerWidth,
      height: Number(params.height) || window.innerHeight,
      orientation: params.orientation || (window.innerHeight > window.innerWidth ? 'portrait' : 'landscape')
    },
    data: function (source, query) {
      var qs = query ? '?' + new URLSearchParams(query).toString() : '';
      return track(fetch('/api/data/' + encodeURIComponent(source) + qs).then(function (r) {
        if (!r.ok) throw new Error('Data source ' + source + ' failed: ' + r.status);
        return r.json();
      }));
    },
    every: function (seconds, fn) {
      fn();
      return setInterval(fn, seconds * 1000);
    },

    module: function () {
      var load = params.m
        ? fetch('/api/module/' + encodeURIComponent(params.m) + '/config').then(function (r) {
            if (!r.ok) throw new Error('This module item no longer exists.');
            return r.json();
          }).then(function (j) { return j.options; })
        : fetch('module.json').then(function (r) { return r.json(); }).then(function (manifest) {
            var options = {};
            (manifest.options || []).forEach(function (o) { options[o.key] = o.default; });
            return options;
          });
      return track(load.then(function (options) {
        if (medium !== 'eink') {      // on e-ink the colours are the panel's inks (eink.css)
          if (options.background) root.style.setProperty('--bg', options.background);
          if (options.accent) root.style.setProperty('--accent', options.accent);
        }
        return options;
      }));
    },
    moduleData: function () {
      if (!params.m) return Promise.reject(new Error('Add this module on the Content page to set it up.'));
      return track(fetch('/api/module/' + encodeURIComponent(params.m) + '/data').then(function (r) {
        return r.json().then(function (j) {
          if (!r.ok) throw new Error(j.error || 'No data');
          return j;
        });
      }));
    },
    // Tell a split screen whether this page has anything worth showing. A pane set to hide when
    // empty disappears while this is false, and the other panes take its space.
    setActive: function (active) {
      active = !!active;
      if (window.parent === window || active === VS._active) return;
      VS._active = active;
      window.parent.postMessage({ vitalsigns: 'pane', active: active }, '*');
    },
    // After a failed load, call `load` again in a minute (once, however often this is called).
    retrySoon: function (load) {
      if (load._retry) return;
      load._retry = setTimeout(function () { load._retry = null; load(); }, 60000);
    },
    fit: function (el, box) {
      var lo = 4, hi = Math.max(8, box.clientHeight);
      el.style.whiteSpace = 'nowrap';
      el.style.display = 'inline-block';
      el.style.lineHeight = '1';
      for (var i = 0; i < 14; i++) {
        var mid = (lo + hi) / 2;
        el.style.fontSize = mid + 'px';
        if (el.offsetWidth <= box.clientWidth && el.offsetHeight <= box.clientHeight) lo = mid; else hi = mid;
      }
      el.style.fontSize = Math.floor(lo) + 'px';
      return Math.floor(lo);
    },
    // Like fit, for text that wraps: the largest size at which el's lines fit inside box.
    fitBlock: function (el, box) {
      var lo = 6, hi = Math.max(12, box.clientHeight);
      for (var i = 0; i < 14; i++) {
        var mid = (lo + hi) / 2;
        el.style.fontSize = mid + 'px';
        if (el.scrollHeight <= box.clientHeight && el.scrollWidth <= box.clientWidth) lo = mid; else hi = mid;
      }
      el.style.fontSize = Math.floor(lo) + 'px';
    },
    // The grid of `count` equal tiles that comes out largest in box, for tiles that need to be
    // `ratio` times as wide as their scale. Returns { cols, scale }; size tile contents from scale.
    bestGrid: function (box, count, ratio) {
      var gap = parseFloat(getComputedStyle(root).getPropertyValue('--s')) * 1.2;
      var best = { cols: 1, scale: 0 };
      for (var cols = 1; cols <= count; cols++) {
        var rows = Math.ceil(count / cols);
        var w = (box.clientWidth - gap * (cols - 1)) / cols, h = (box.clientHeight - gap * (rows - 1)) / rows;
        var scale = Math.min(w / ratio, h);
        if (scale > best.scale) best = { cols: cols, scale: scale };
      }
      return best;
    },
    clip: function (list) {
      var items = Array.prototype.slice.call(list.children);
      items.forEach(function (item) { item.hidden = false; });
      var limit = list.getBoundingClientRect().bottom + 0.5;
      items.forEach(function (item) {
        if (item.getBoundingClientRect().bottom > limit) item.hidden = true;
      });
      // Never show a heading with nothing under it.
      for (var i = items.length - 1; i >= 0; i--) {
        if (items[i].hidden) continue;
        if (items[i].dataset.heading !== undefined) items[i].hidden = true; else break;
      }
    },
    onResize: function (fn) {
      var timer;
      window.addEventListener('resize', function () {
        clearTimeout(timer);
        timer = setTimeout(fn, 100);
      });
      fn();
    },
    message: function (el, text) {
      el.textContent = '';
      var note = document.createElement('div');
      note.className = 'm-message';
      note.textContent = text;
      el.appendChild(note);
    }
  };
  root.dataset.orientation = window.VS.display.orientation;
})();
