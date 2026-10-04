/* Keeps display cards up to date: status badges, what is showing, errors and the screenshot.
 * Used by the dashboard and by each display's own page. A card is any element with
 * data-display="<id>"; the parts it updates are found by their js- classes, and are optional. */
(function () {
  function render(d) {
    var card = document.querySelector('[data-display="' + d.id + '"]');
    if (!card) return;
    var part = function (name) { return card.querySelector('.js-' + name); };

    // A manual screen override starting or ending changes which controls apply.
    if ((d.power_override || '') !== card.dataset.powerOverride) { location.reload(); return; }

    var online = part('online');
    if (online) {
      online.textContent = d.online ? 'online' : 'offline';
      online.className = 'badge js-online ' + (d.online ? 'ok' : 'bad');
    }
    if (part('drift')) part('drift').hidden = !d.version_drift;
    if (part('screen')) part('screen').hidden = d.screen_on !== false;
    var power = part('power');
    if (power) {
      power.value = d.screen_on === false ? 'on' : 'off';
      power.textContent = 'Screen ' + power.value;
    }

    var text = '—';
    if (d.browser === 'down') text = 'browser restarting';
    else if (d.current) text = (d.current.override ? 'override: ' : '') + (d.current.name || d.current.url);
    if (d.placement_ok === false) text += ' (window not placed correctly)';
    if (part('current')) part('current').textContent = text;

    var errors = part('errors');
    if (errors) {
      errors.textContent = '';
      (d.errors || []).forEach(function (e) {
        var li = document.createElement('li');
        li.textContent = (e.name || e.url) + ': ' + e.error;
        errors.appendChild(li);
      });
    }
    var img = part('shot');
    if (img && d.screenshot_url && img.getAttribute('src') !== d.screenshot_url) {
      img.src = d.screenshot_url;
      img.hidden = false;
    }
  }
  function renderAgent(a) {
    var card = document.querySelector('[data-agent="' + a.id + '"]');
    if (!card) return;
    var online = card.querySelector('.js-agent-online');
    online.textContent = a.online ? 'online' : 'offline';
    online.className = 'badge js-agent-online ' + (a.online ? 'ok' : 'bad');
    var facts = card.querySelector('.js-facts');
    facts.textContent = '';
    a.facts.forEach(function (f) {
      var label = document.createElement('dt'), value = document.createElement('dd');
      label.textContent = f.label;
      value.textContent = f.value;
      if (f.warn) value.className = 'warn';
      facts.appendChild(label);
      facts.appendChild(value);
    });
  }
  function refresh() {
    fetch('/api/v1/displays').then(function (r) { return r.json(); })
      .then(function (data) { data.displays.forEach(render); }).catch(function () {});
    if (document.querySelector('[data-agent]')) {
      fetch('/api/v1/agents').then(function (r) { return r.json(); })
        .then(function (data) { data.agents.forEach(renderAgent); }).catch(function () {});
    }
  }
  (window.VS_DISPLAYS || []).forEach(render);
  setInterval(refresh, 3000);
})();
