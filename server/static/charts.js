/* Charts for the admin UI: Apache ECharts with the Design Uniformity theme (DESIGN-SPEC.md 11), as in
 * Binge Stats' charts.js. One chart so far: an e-ink display's battery level over time.
 *
 *   <section data-battery="/api/v1/displays/4/battery"> ... </section>   (markup in templates/display.html)
 *
 * It fetches its readings, draws them, and shows an empty state when there are none. Colours come from
 * the validated categorical palette (slot 0: one series, so no legend), text and grid from the theme
 * tokens, read when it draws; it draws again when the theme flips or the box changes size.
 */
(function () {
  'use strict';

  // Validated against the card surfaces (dark #11141d, light #fff). Slot 0 of the shared palette.
  var SERIES = { dark: '#3987e5', light: '#2a78d6' };
  var LOW = 20;       // percent: where the display's own strip and Home Assistant call the battery low

  function mode() { return document.documentElement.dataset.theme === 'light' ? 'light' : 'dark'; }
  function cssVar(name) {
    var v = getComputedStyle(document.documentElement).getPropertyValue('--' + name).trim();
    return v ? 'rgb(' + v.split(/\s+/).join(',') + ')' : '#888';
  }
  function tokens() {
    return { text: cssVar('text'), muted: cssVar('muted'), faint: cssVar('faint'), border: cssVar('border'), surface: cssVar('surface') };
  }
  function clock(ms) { return new Date(ms).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }); }
  function day(ms) { return new Date(ms).toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' }); }
  function number(v, places) { return Number(v).toLocaleString('en-GB', { minimumFractionDigits: places, maximumFractionDigits: places }); }
  function text(tag, cls, value) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    node.textContent = value;
    return node;
  }

  function showEmpty(box, title, line) {
    box.textContent = '';
    var wrap = document.createElement('div'), icon = document.createElement('div');
    wrap.className = 'empty';
    icon.className = 'empty-icon';
    icon.innerHTML = '<svg aria-hidden="true"><use href="/static/design/icons.svg#chart"></use></svg>';
    wrap.appendChild(icon);
    wrap.appendChild(text('strong', '', title));
    wrap.appendChild(text('p', '', line));
    box.appendChild(wrap);
  }

  function option(data) {
    var t = tokens(), colour = SERIES[mode()];
    var points = data.samples.map(function (s) { return [s.at * 1000, s.percent, s.volts]; });
    var spanDays = (data.to - data.from) / 86400;
    return {
      animationDuration: 400,
      textStyle: { fontFamily: getComputedStyle(document.body).fontFamily, color: t.muted, fontSize: 11 },
      grid: { left: 8, right: 16, top: 16, bottom: 4, containLabel: true },
      tooltip: {
        trigger: 'axis', backgroundColor: t.surface, borderColor: t.border, borderWidth: 1, padding: [8, 10],
        textStyle: { color: t.text, fontSize: 12 }, extraCssText: 'border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.35);',
        axisPointer: { type: 'line', lineStyle: { color: t.faint } },
        formatter: function (params) {
          var p = params[0];
          return '<div style="margin-bottom:4px;font-weight:600">' + day(p.value[0]) + ', ' + clock(p.value[0]) + '</div>' +
                 '<div style="display:flex;gap:12px;justify-content:space-between"><span>' + p.marker + 'Battery</span>' +
                 '<b style="font-variant-numeric:tabular-nums">' + p.value[1] + '%</b></div>' +
                 '<div style="display:flex;gap:12px;justify-content:space-between;color:' + t.muted + '"><span>Voltage</span>' +
                 '<span style="font-variant-numeric:tabular-nums">' + number(p.value[2], 2) + ' V</span></div>';
        }
      },
      xAxis: {
        type: 'time', min: data.from * 1000, max: data.to * 1000, axisTick: { show: false },
        axisLine: { lineStyle: { color: t.border } }, splitLine: { show: false },
        axisLabel: { color: t.muted, hideOverlap: true,
                     formatter: function (ms) { return spanDays <= 2 ? clock(ms) : new Date(ms).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }); } }
      },
      yAxis: {
        type: 'value', min: 0, max: 100, interval: 25,
        splitLine: { lineStyle: { color: t.border, width: 1, opacity: 0.6 } },
        axisLabel: { color: t.muted, formatter: function (v) { return v + '%'; } }
      },
      series: [{
        name: 'Battery', type: 'line', data: points, smooth: false, connectNulls: false,
        showSymbol: points.length <= 40, symbolSize: 8,
        lineStyle: { width: 2, color: colour, cap: 'round', join: 'round' },
        itemStyle: { color: colour, borderColor: t.surface, borderWidth: 2 },
        areaStyle: { color: colour, opacity: 0.1 },
        // Where "low" starts, as a quiet reference line labelled in text, not in a status colour.
        markLine: { silent: true, symbol: 'none', animation: false, data: [{ yAxis: LOW }],
                    lineStyle: { color: t.faint, type: [4, 4], width: 1 },
                    label: { formatter: 'Low', position: 'insideEndTop', color: t.muted, fontSize: 11 } }
      }]
    };
  }

  function stats(root, data) {
    var set = function (name, value, sub) {
      var tile = root.querySelector('[data-stat="' + name + '"]');
      if (!tile) return;
      tile.querySelector('.stat-value').textContent = value;
      tile.querySelector('.stat-sub').textContent = sub;
    };
    var last = data.samples[data.samples.length - 1], e = data.estimate;
    if (data.mains) set('now', 'Mains', last ? number(last.volts, 2) + ' V at the last wake' : 'No readings yet');
    else set('now', last ? last.percent + '%' : '–', last ? number(last.volts, 2) + ' V at ' + clock(last.at * 1000) + ', ' + day(last.at * 1000) : 'No readings in this range');
    if (e && e.days_left !== null) {
      set('rate', number(e.per_day, 1) + '% a day', 'Since ' + day(e.since * 1000) + ', over ' + (e.hours >= 48 ? number(e.hours / 24, 1) + ' days' : Math.round(e.hours) + ' hours'));
      set('left', e.days_left >= 2 ? Math.round(e.days_left) + ' days' : Math.round(e.days_left * 24) + ' hours',
          'Empty around ' + day(e.empty_at * 1000) + ' · a full charge lasts about ' + Math.round(e.full_charge_days) + ' days');
    } else if (e) {
      set('rate', 'Steady', 'Not fallen enough yet to tell, over ' + Math.round(e.hours) + ' hours');
      set('left', '–', 'An estimate appears once the level has dropped a little');
    } else {
      set('rate', '–', 'Needs a day of readings since the last charge');
      set('left', '–', 'An estimate appears after a day of readings');
    }
  }

  function table(root, data) {
    var body = root.querySelector('[data-readings] tbody');
    if (!body) return;
    body.textContent = '';
    data.samples.slice(-12).reverse().forEach(function (s) {
      var row = document.createElement('tr');
      row.appendChild(text('td', '', day(s.at * 1000) + ', ' + clock(s.at * 1000)));
      row.appendChild(text('td', '', s.percent + '%'));
      row.appendChild(text('td', '', number(s.volts, 2) + ' V'));
      body.appendChild(row);
    });
    root.querySelector('[data-readings]').hidden = !data.samples.length;
  }

  function battery(root) {
    var box = root.querySelector('[data-chart]'), tabs = root.querySelectorAll('[data-days]');
    var chart = null, data = null, days = Number(root.dataset.days) || 7;

    function draw() {
      if (!data) return;
      stats(root, data);
      table(root, data);
      if (!data.samples.length) {
        if (chart) { chart.dispose(); chart = null; }
        return showEmpty(box, data.first_reading ? 'No readings in this range' : 'No battery readings yet',
                         data.first_reading ? 'The device has not checked in during this time. Try a longer range.'
                                            : 'A reading is kept each time the device wakes. The first appears at its next check-in.');
      }
      if (!chart) { box.textContent = ''; chart = echarts.init(box); }
      chart.setOption(option(data), true);
    }
    function load() {
      tabs.forEach(function (tab) { tab.classList.toggle('active', Number(tab.dataset.days) === days); });
      fetch(root.dataset.battery + '?days=' + days).then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      }).then(function (d) { data = d; draw(); })
        .catch(function () { showEmpty(box, 'The readings could not be loaded', 'Reload the page to try again.'); });
    }
    tabs.forEach(function (tab) {
      tab.addEventListener('click', function (e) { e.preventDefault(); days = Number(tab.dataset.days); load(); });
    });
    new ResizeObserver(function () { if (chart) chart.resize(); }).observe(box);
    new MutationObserver(draw).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    load();
    setInterval(load, 5 * 60 * 1000);
  }

  document.querySelectorAll('[data-battery]').forEach(battery);
})();
