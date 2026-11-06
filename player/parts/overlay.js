/* ---------- SVG 小道具 ---------- */
function el(name, attrs, parent) {
  var e = document.createElementNS(NS, name);
  for (var k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}
function estW(s, fs) { var w = 0; for (var i = 0; i < s.length; i++) w += s.charCodeAt(i) > 255 ? fs : fs * 0.58; return w; }
function pathLine(pts) { return pts.map(function (p, i) { return (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1); }).join(' '); }
function setDraw(node, p) {   // pathLength=1 の線を p だけ描く
  node.setAttribute('stroke-dasharray', '1 1');
  node.setAttribute('stroke-dashoffset', String(1 - p));
}
function haloText(parent, x, y, txt, fs, fill, anchor) {
  var t = el('text', {x: x, y: y, 'font-size': fs, fill: fill, 'text-anchor': anchor || 'start',
                      stroke: '#fff', 'stroke-width': Math.round(fs * 0.22), 'stroke-linejoin': 'round', 'paint-order': 'stroke'}, parent);
  t.textContent = txt; return t;
}
/* 手書き風の円: 楕円気味・半径が少しずつ変わり、閉じきらずに 1.07 周 */
function handCircle(cx, cy, r) {
  var pts = [], n = 72, th0 = -1.9, tilt = -0.25, ct = Math.cos(tilt), st = Math.sin(tilt);
  for (var i = 0; i <= n; i++) {
    var q = i / n, th = th0 + q * Math.PI * 2 * 1.07;
    var rho = r * (1 + 0.06 * Math.sin(2 * th + 1) + 0.07 * q);
    var x = rho * 1.12 * Math.cos(th), y = rho * 0.92 * Math.sin(th);
    pts.push([cx + x * ct - y * st, cy + x * st + y * ct]);
  }
  return pathLine(pts);
}

/* ---------- 図形ごとの SVG を作り、更新関数を返す ---------- */
function buildShape(s, hole, W, m) {
  var h = {upd: function () {}};
  var top = $('svgTop'), band = $('svgBand');
  if (s.kind === 'band') {
    var b = s.box, g = el('g', {}, band);
    var rc = el('rect', {x: b[0], y: b[1], width: 0, height: b[3] - b[1], fill: '#ffe14d', 'fill-opacity': .5}, g);
    var gt = el('g', {}, top), lb = null;
    if (s.label) lb = haloText(gt, (b[0] + b[2]) / 2, Math.max(52 * m, b[1] - 18 * m), s.label, 46 * m, '#8a6400', 'middle');
    var ht = hole && el('rect', {x: b[0] - 12 * m, y: b[1] - 12 * m, width: 0, height: b[3] - b[1] + 24 * m, rx: 12 * m}, hole);
    h.upd = function (p, a) {
      var e = easeOut(p);
      rc.setAttribute('width', (b[2] - b[0]) * e);
      g.setAttribute('opacity', a * (p > 0 ? 1 : 0));
      gt.setAttribute('opacity', a * easeOut((p - 0.2) / 0.5));
      if (ht) ht.setAttribute('width', (b[2] - b[0]) * e + 24 * m);
    };
  } else if (s.kind === 'circle') {
    var c = s.center, r = s.r, col = s.color || '#e53935';
    var g2 = el('g', {}, top), d = handCircle(c[0], c[1], r);
    var hp = el('path', {d: d, fill: 'none', stroke: '#fff', 'stroke-width': 13 * m, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', pathLength: 1}, g2);
    var cp = el('path', {d: d, fill: 'none', stroke: '#e53935', 'stroke-width': 6.5 * m, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', pathLength: 1}, g2);
    var lt = null;
    if (s.label) {
      var fs = 46 * m, w = estW(s.label, fs), right = c[0] + r * 1.15 + 14 * m + w < W - 8;
      lt = haloText(g2, right ? c[0] + r * 1.15 + 14 * m : c[0] - r * 1.15 - 14 * m, c[1] - r * 0.5, s.label, fs, '#e53935', right ? 'start' : 'end');
    }
    var he = hole && el('ellipse', {cx: c[0], cy: c[1], rx: 0, ry: 0}, hole);
    h.upd = function (p, a) {
      setDraw(hp, p); setDraw(cp, p);
      g2.setAttribute('opacity', a * (p > 0.001 ? 1 : 0));
      if (lt) lt.setAttribute('opacity', easeOut((p - 0.55) / 0.4));
      if (he) { var rr = r * 2.8 * easeOut(p); he.setAttribute('rx', rr); he.setAttribute('ry', rr); }
    };
  } else if (s.kind === 'trace') {
    var col2 = s.color || '#00e5ff', g3 = el('g', {}, top), dd = pathLine(s.points || []);
    var common = {d: dd, fill: 'none', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', pathLength: 1};
    var glow = el('path', Object.assign({stroke: col2, 'stroke-width': 30 * m, 'stroke-opacity': .5, filter: 'url(#glow)'}, common), g3);
    var halo = el('path', Object.assign({stroke: '#fff', 'stroke-width': 17 * m, 'stroke-opacity': .8}, common), g3);
    var core = el('path', Object.assign({stroke: col2, 'stroke-width': 9 * m}, common), g3);
    h.upd = function (p, a) {
      var e = easeInOut(p);
      [glow, halo, core].forEach(function (n) { setDraw(n, e); });
      g3.setAttribute('opacity', a * (p > 0.001 ? 1 : 0));
    };
  } else if (s.kind === 'axis') {
    var b4 = s.box.map(function (v, i) { return v + (i < 2 ? -8 : 8) * m; }), g4 = el('g', {}, top), CY = '#00b8d9';
    var rf = el('rect', {x: b4[0], y: b4[1], width: b4[2] - b4[0], height: b4[3] - b4[1], rx: 22 * m, fill: '#00e5ff', 'fill-opacity': 0}, g4);
    var ro = el('rect', {x: b4[0], y: b4[1], width: b4[2] - b4[0], height: b4[3] - b4[1], rx: 22 * m, fill: 'none', stroke: CY, 'stroke-width': 6 * m, pathLength: 1}, g4);
    var ln = s.line, dl = pathLine(ln), l1 = null, l2 = null;
    if (ln) {
      l1 = el('path', {d: dl, fill: 'none', stroke: '#00e5ff', 'stroke-width': 24 * m, 'stroke-opacity': .55, 'stroke-linecap': 'round', filter: 'url(#glow)', pathLength: 1}, g4);
      l2 = el('path', {d: dl, fill: 'none', stroke: CY, 'stroke-width': 8 * m, 'stroke-linecap': 'round', pathLength: 1}, g4);
    }
    var tl = null;
    if (s.label) {
      var fs2 = 44 * m, w2 = estW(s.label, fs2), tall = (b4[3] - b4[1]) > (b4[2] - b4[0]) * 1.2;
      if (tall) tl = haloText(g4, (b4[0] + b4[2]) / 2, Math.max(fs2 + 4 * m, b4[1] - 16 * m), s.label, fs2, '#007c91', 'middle');
      else if (b4[2] + 18 * m + w2 < W - 6) tl = haloText(g4, b4[2] + 18 * m, (b4[1] + b4[3]) / 2 + 15 * m, s.label, fs2, '#007c91', 'start');
      else tl = haloText(g4, b4[0] - 18 * m, (b4[1] + b4[3]) / 2 + 15 * m, s.label, fs2, '#007c91', 'end');
    }
    var hr = hole && el('rect', {x: b4[0] - 10 * m, y: b4[1] - 10 * m, width: b4[2] - b4[0] + 20 * m, height: b4[3] - b4[1] + 20 * m, rx: 26 * m}, hole);
    var hl = (hole && ln) ? el('path', {d: dl, fill: 'none', stroke: '#000', 'stroke-width': 60 * m, 'stroke-linecap': 'round'}, hole) : null;
    h.upd = function (p, a) {
      var e = easeOut(p);
      setDraw(ro, e); if (l1) { setDraw(l1, e); setDraw(l2, e); }
      rf.setAttribute('fill-opacity', .10 * easeOut((p - .5) / .5));
      if (tl) tl.setAttribute('opacity', easeOut((p - .4) / .5));
      g4.setAttribute('opacity', a * (p > 0.001 ? 1 : 0));
      if (hr) hr.setAttribute('opacity', e); if (hl) hl.setAttribute('opacity', e);
    };
  } else if (s.kind === 'box') {
    // 枠は箱より一回り外に引き、線は細め・半透明にする（施主 2026-10-30「枠線が文字に囲まれて見づらい。広めか透明度を上げて」）
    var bp = 16 * m, b5 = [s.box[0] - bp, s.box[1] - bp, s.box[2] + bp, s.box[3] + bp], g5 = el('g', {}, top);
    var a5 = {x: b5[0], y: b5[1], width: b5[2] - b5[0], height: b5[3] - b5[1], rx: 18 * m, fill: 'none', pathLength: 1};
    var bh = el('rect', Object.assign({stroke: '#fff', 'stroke-width': 10 * m, 'stroke-opacity': .55}, a5), g5);
    var bo = el('rect', Object.assign({stroke: '#2979ff', 'stroke-width': 5 * m, 'stroke-opacity': .75}, a5), g5);
    var hb = hole && el('rect', {x: b5[0] - 10 * m, y: b5[1] - 10 * m, width: b5[2] - b5[0] + 20 * m, height: b5[3] - b5[1] + 20 * m, rx: 22 * m}, hole);
    h.upd = function (p, a) {
      var e = easeInOut(p); setDraw(bh, e); setDraw(bo, e);
      g5.setAttribute('opacity', a * (p > 0.001 ? 1 : 0)); if (hb) hb.setAttribute('opacity', e);
    };
  } else if (s.kind === 'arrow') {
    var f = s.from, t = s.to, g6 = el('g', {}, top), dx = t[0] - f[0], dy = t[1] - f[1], L = Math.hypot(dx, dy) || 1, ux = dx / L, uy = dy / L;
    var HS = 40 * m, bx = t[0] - ux * HS * 0.8, by = t[1] - uy * HS * 0.8, nx = -uy, ny = ux;
    var dl6 = 'M' + f[0] + ' ' + f[1] + ' L' + bx.toFixed(1) + ' ' + by.toFixed(1);
    var lh = el('path', {d: dl6, stroke: '#fff', 'stroke-width': 18 * m, 'stroke-linecap': 'round', fill: 'none', pathLength: 1}, g6);
    var lc = el('path', {d: dl6, stroke: '#e53935', 'stroke-width': 9 * m, 'stroke-linecap': 'round', fill: 'none', pathLength: 1}, g6);
    var hd = [t[0] + ',' + t[1], (bx + nx * HS * 0.55) + ',' + (by + ny * HS * 0.55), (bx - nx * HS * 0.55) + ',' + (by - ny * HS * 0.55)].join(' ');
    var hh = el('polygon', {points: hd, fill: '#e53935', stroke: '#fff', 'stroke-width': 7 * m, 'stroke-linejoin': 'round', 'paint-order': 'stroke'}, g6);
    h.upd = function (p, a) {
      setDraw(lh, easeOut(p / 0.8)); setDraw(lc, easeOut(p / 0.8));
      var hp6 = easeBack((p - 0.7) / 0.3);
      hh.setAttribute('opacity', p > 0.7 ? 1 : 0);
      hh.setAttribute('transform', 'translate(' + t[0] + ' ' + t[1] + ') scale(' + (0.3 + 0.7 * hp6) + ') translate(' + (-t[0]) + ' ' + (-t[1]) + ')');
      g6.setAttribute('opacity', a * (p > 0.001 ? 1 : 0));
    };
  }
  return h;
}

/* ステップの SVG を作る（ステップが変わったときだけ） */
function buildOverlay(fi, stepIdx, items, view) {
  var fig = figures[fi];
  // 線の太さ・札の字は「画面上の大きさ」で揃える: 寄るほど画像座標では細く・小さくする
  // （Fig.1 の全体表示 k=0.64 のときの見た目が基準。寄った図で字が画面いっぱいに膨らまないように）
  var m = REF_K / (view ? view.k : fitScale(fig));
  ['svgBand', 'svgTop'].forEach(function (id) { var n = $(id); while (n.firstChild) n.removeChild(n.firstChild); });
  var holes = $('holes'); while (holes.firstChild) holes.removeChild(holes.firstChild);
  var o = {handles: []};
  // 帯 → 他の順に描くため、そのまま items 順で作る（band は別 svg）
  items.forEach(function (s) {
    var useHole = ['band', 'circle', 'axis', 'box'].indexOf(s.kind) >= 0;
    o.handles.push(buildShape(s, useHole ? holes : null, fig.width, m));
  });
  return o;
}
