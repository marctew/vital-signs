/* Helpers for Vital Signs local pages.
 *
 *   VS.params            all query parameters as an object
 *   VS.display           { name, width, height, orientation } of the display showing the page
 *   VS.data(source, q)   Promise of JSON from the server's data proxy (/api/data/<source>)
 *   VS.every(seconds, f) run f now and then every `seconds`
 */
(function () {
  var params = {};
  new URLSearchParams(location.search).forEach(function (value, key) { params[key] = value; });

  window.VS = {
    params: params,
    display: {
      name: params.display || '',
      width: Number(params.width) || window.innerWidth,
      height: Number(params.height) || window.innerHeight,
      orientation: params.orientation || (window.innerHeight > window.innerWidth ? 'portrait' : 'landscape')
    },
    data: function (source, query) {
      var qs = query ? '?' + new URLSearchParams(query).toString() : '';
      return fetch('/api/data/' + encodeURIComponent(source) + qs).then(function (r) {
        if (!r.ok) throw new Error('Data source ' + source + ' failed: ' + r.status);
        return r.json();
      });
    },
    every: function (seconds, fn) {
      fn();
      return setInterval(fn, seconds * 1000);
    }
  };
  document.documentElement.dataset.orientation = window.VS.display.orientation;
})();
