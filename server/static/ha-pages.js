/* Page editor for the Home Assistant sensors module (option type "ha_pages").
 *
 * The pages live as JSON in the option's textarea, which is what the form submits. This turns it
 * into cards (one per page, one row per reading) and writes the JSON back before the form is sent.
 * Without JavaScript the textarea stays visible and can be edited by hand. The server checks it
 * either way (modules.clean_ha_pages).
 */
(function () {
  var box = document.querySelector('.ha-pages');
  if (!box) return;
  var field = document.querySelector('textarea[name="' + box.dataset.field + '"]');
  var form = field.form;
  var MAX_PAGES = 20, MAX_TILES = 24;
  var known = {};
  document.querySelectorAll('#ha-entities option').forEach(function (o) {
    known[o.value] = { name: o.dataset.name || '', unit: o.dataset.unit || '' };
  });

  var pages;
  try { pages = JSON.parse(field.value || '[]'); } catch (e) { pages = []; }
  if (!Array.isArray(pages)) pages = [];
  if (!pages.length) pages.push(newPage());
  field.hidden = true;

  function newPage() { return { title: '', tiles: [newTile()] }; }
  function newTile() { return { entity: '', display: 'value' }; }
  function text(v) { return v === null || v === undefined ? '' : String(v); }

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === 'className') node.className = attrs[k];
      else if (k === 'text') node.textContent = attrs[k];
      else node.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { if (c) node.appendChild(c); });
    return node;
  }
  function icon(name) {
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'icon');
    svg.setAttribute('aria-hidden', 'true');
    var use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '/static/design/icons.svg#' + name);
    svg.appendChild(use);
    return svg;
  }
  function iconButton(name, label, onClick, disabled) {
    var b = el('button', { type: 'button', className: 'btn-icon', 'aria-label': label, title: label }, [icon(name)]);
    b.disabled = !!disabled;
    b.addEventListener('click', onClick);
    return b;
  }
  function move(list, i, by) {
    var item = list.splice(i, 1)[0];
    list.splice(i + by, 0, item);
    render();
  }
  // A labelled input bound to obj[key]. `wide` lets it take the rest of the row.
  function input(labelText, obj, key, attrs, wide) {
    var inp = el('input', attrs || {});
    inp.dataset.key = key;
    inp.value = text(obj[key]);
    inp.addEventListener('input', function () { obj[key] = inp.value; });
    return { label: el('label', { className: wide ? 'grow' : '' }, [document.createTextNode(labelText), inp]), input: inp };
  }

  function tileRow(page, tile, i) {
    var entity = input('Entity', tile, 'entity', { list: 'ha-entities', placeholder: 'sensor.kitchen_temperature',
                                                   autocomplete: 'off', spellcheck: 'false' }, true);
    var name = input('Label', tile, 'label', { placeholder: 'From Home Assistant' });
    var unit = input('Unit', tile, 'unit', { placeholder: '' });
    function hints() {
      var k = known[entity.input.value.trim()];
      name.input.placeholder = (k && k.name) || 'From Home Assistant';
      unit.input.placeholder = tile.attribute ? '' : ((k && k.unit) || '');
    }
    entity.input.addEventListener('input', hints);
    hints();

    var show = el('select', {}, [['value', 'Number'], ['gauge', 'Gauge'], ['bar', 'Bar']].map(function (c) {
      var o = el('option', { value: c[0], text: c[1] });
      o.selected = (tile.display || 'value') === c[0];
      return o;
    }));
    var range = el('div', { className: 'ha-range' }, [
      input('Minimum', tile, 'min', { type: 'number', step: 'any', placeholder: '0' }).label,
      input('Maximum', tile, 'max', { type: 'number', step: 'any', placeholder: '100' }).label
    ]);
    function showRange() { range.hidden = show.value === 'value'; }
    show.addEventListener('change', function () { tile.display = show.value; showRange(); });
    showRange();
    var attribute = input('Attribute (optional)', tile, 'attribute', { placeholder: 'temperature', spellcheck: 'false' });
    attribute.input.addEventListener('input', hints);

    var tiles = page.tiles;
    return el('div', { className: 'ha-tile' }, [
      el('div', { className: 'row ha-tile-main' }, [
        entity.label,
        el('label', {}, [document.createTextNode('Show as'), show]),
        el('div', { className: 'ha-buttons' }, [
          iconButton('chevron-up', 'Move up', function () { move(tiles, i, -1); }, i === 0),
          iconButton('chevron-down', 'Move down', function () { move(tiles, i, 1); }, i === tiles.length - 1),
          iconButton('x', 'Remove this reading', function () { tiles.splice(i, 1); render(); })
        ])
      ]),
      el('div', { className: 'ha-tile-more' }, [
        name.label, unit.label, range,
        input('Amber from', tile, 'warn', { type: 'number', step: 'any' }).label,
        input('Red from', tile, 'bad', { type: 'number', step: 'any' }).label,
        input('Decimals', tile, 'decimals', { type: 'number', min: '0', max: '4', step: '1', placeholder: 'Auto' }).label,
        attribute.label
      ])
    ]);
  }

  function pageCard(page, i) {
    var title = input('Page title', page, 'title', { placeholder: 'Page ' + (i + 1) }, true);
    var add = el('button', { type: 'button' }, [icon('plus'), document.createTextNode('Add a reading')]);
    add.disabled = page.tiles.length >= MAX_TILES;
    add.addEventListener('click', function () { page.tiles.push(newTile()); render(); focusLast(i); });
    var remove = iconButton('trash', 'Remove this page', function () {
      var go = page.tiles.some(function (t) { return (t.entity || '').trim(); }) && window.duConfirm
        ? window.duConfirm(remove, 'Remove this page and its readings?', 'Remove page') : Promise.resolve(true);
      go.then(function (yes) { if (yes) { pages.splice(i, 1); if (!pages.length) pages.push(newPage()); render(); } });
    });
    return el('div', { className: 'card ha-page', 'data-page': i }, [
      el('div', { className: 'row ha-page-head' }, [
        title.label,
        el('div', { className: 'ha-buttons' }, [
          iconButton('chevron-up', 'Move page up', function () { move(pages, i, -1); }, i === 0),
          iconButton('chevron-down', 'Move page down', function () { move(pages, i, 1); }, i === pages.length - 1),
          remove
        ])
      ]),
      el('div', { className: 'ha-tiles' }, page.tiles.map(function (t, n) { return tileRow(page, t, n); })),
      el('div', { className: 'row' }, [add])
    ]);
  }

  function focusLast(pageIndex) {
    var inputs = box.querySelectorAll('[data-page="' + pageIndex + '"] input[list]');
    if (inputs.length) inputs[inputs.length - 1].focus();
  }

  function render() {
    box.textContent = '';
    pages.forEach(function (p, i) { if (!Array.isArray(p.tiles)) p.tiles = []; box.appendChild(pageCard(p, i)); });
    var add = el('button', { type: 'button' }, [icon('plus'), document.createTextNode('Add a page')]);
    add.disabled = pages.length >= MAX_PAGES;
    add.addEventListener('click', function () {
      pages.push(newPage());
      render();
      box.querySelectorAll('.ha-page')[pages.length - 1].scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      focusLast(pages.length - 1);
    });
    box.appendChild(el('div', { className: 'row' }, [add]));
    box.appendChild(el('p', { className: 'muted help', text:
      'Amber from and Red from colour a reading once it reaches them. Make Red lower than Amber when low is bad, ' +
      'such as a battery. Attribute shows one of the entity\'s attributes instead of its state, for example a ' +
      'thermostat\'s current_temperature.' }));
  }

  // Catch what the server would refuse here, so a typo does not send the form back with the edits lost.
  var ENTITY = /^[a-z0-9_]{1,64}\.[a-z0-9_]{1,200}$/;
  function problem(t) {
    var id = (t.entity || '').trim().toLowerCase();
    if (!ENTITY.test(id)) return ['entity', 'Entity ids look like sensor.kitchen_temperature'];
    if (t.display === 'gauge' || t.display === 'bar') {
      var low = text(t.min) === '' ? 0 : Number(t.min), high = text(t.max) === '' ? 100 : Number(t.max);
      if (!(high > low)) return ['max', 'The maximum must be more than the minimum'];
    }
    if (t.attribute && !/^[A-Za-z0-9_]{1,64}$/.test(t.attribute.trim())) return ['attribute', 'Attribute names use letters, digits and _'];
    return null;
  }
  box.addEventListener('input', function (e) { if (e.target.setCustomValidity) e.target.setCustomValidity(''); });

  form.addEventListener('submit', function (e) {
    var cards = box.querySelectorAll('.ha-page');
    for (var i = 0; i < pages.length; i++) {
      for (var n = 0; n < pages[i].tiles.length; n++) {
        var t = pages[i].tiles[n], bad = (t.entity || '').trim() && problem(t);
        if (!bad) continue;
        e.preventDefault();
        var target = cards[i].querySelectorAll('.ha-tile')[n].querySelector('[data-key="' + bad[0] + '"]');
        target.setCustomValidity(bad[1]);
        target.reportValidity();
        return;
      }
    }
    field.value = JSON.stringify(pages.map(function (p) {
      return { title: p.title || '', tiles: p.tiles.filter(function (t) { return (t.entity || '').trim(); }) };
    }).filter(function (p) { return p.tiles.length; }));
  });
  render();
})();
