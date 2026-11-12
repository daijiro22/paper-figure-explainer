/* 図を切り替えたときの土台（画像・viewBox・フィルタ・見出し・ドット） */
function ensureFigure(fi) {
  if (S.curFi === fi) return;
  S.curFi = fi; S.ovKey = null; S.panelKey = null;
  var fig = figures[fi], W = fig.width, H = fig.height;
  setFigImage(fig);
  if (S.voice) loadVoice(fig);
  $('grp').style.width = W + 'px'; $('grp').style.height = H + 'px';
  ['svgDim', 'svgBand', 'svgTop'].forEach(function (id) {
    var s = $(id); s.setAttribute('viewBox', '0 0 ' + W + ' ' + H); s.setAttribute('preserveAspectRatio', 'none');
    while (s.firstChild) s.removeChild(s.firstChild);
  });
  var dd = $('svgDim');
  dd.innerHTML = '<defs><filter id="soft" filterUnits="userSpaceOnUse" x="0" y="0" width="' + W + '" height="' + H + '"><feGaussianBlur stdDeviation="16"/></filter>' +
    '<mask id="dimMask" maskUnits="userSpaceOnUse" x="0" y="0" width="' + W + '" height="' + H + '"><rect x="0" y="0" width="' + W + '" height="' + H + '" fill="#fff"/>' +
    '<g id="holes" fill="#000" filter="url(#soft)"></g></mask></defs>' +
    '<rect id="dimRect" x="0" y="0" width="' + W + '" height="' + H + '" fill="#fff" opacity="0" mask="url(#dimMask)"/>';
  // trace / axis の光り（svgTop 内の defs は buildOverlay が消すので別に保持するため、ここでは dim 側 svg に置く）
  dd.querySelector('defs').insertAdjacentHTML('beforeend', '<filter id="glow" filterUnits="userSpaceOnUse" x="0" y="0" width="' + W + '" height="' + H + '"><feGaussianBlur stdDeviation="9"/></filter>');
  var off = figures.length && figures[0].isOverview ? 1 : 0;     // 概要は番号を持たず、図は 01 から
  $('tag').textContent = fig.isOverview ? '概要' : fig.isConclusion ? '結論' : ('0' + (fi + 1 - off)).slice(-2);
  $('chap').textContent = fig.label || ('図 ' + (fi + 1));
  $('chap').style.left = (48 + 28 + ($('tag').offsetWidth || 64) + 16 - 28) + 'px';
  $('sum').textContent = fig.summary_ja || '';
  $('ptitle').textContent = paper.title || '';
  // ステップのドット
  var tl = buildTimeline(fig), dots = $('dots'); dots.innerHTML = '';
  tl.segs.forEach(function (sg, i) {
    var d = document.createElement('i'); d.title = sg.type === 'take' ? 'まとめ' : 'ステップ ' + (sg.idx + 1);
    if (sg.type === 'take') d.className = 'tk';
    d.addEventListener('click', function () { seekSeg(i); });
    dots.appendChild(d);
  });
  Array.prototype.forEach.call(document.querySelectorAll('#menuList .fi'), function (n, i) { n.classList.toggle('on', i === fi); });
  syncDoc();    // 自動で次の図へ進んだときも、開いている原文を追いかける
  updateWait();
}

/* アブストラクトの全文。今のステップの根拠の引用を蛍光ペンで示す（施主 2026-11-05「アブスト全文を表示」） */
function abstractHtml(text, evidence) {
  var t = text.replace(/\s+/g, ' '), ranges = [];
  (evidence || []).forEach(function (e) {
    var q = String(e.quote || '').replace(/\s+/g, ' ').trim(), i = q ? t.indexOf(q) : -1;
    if (i >= 0) ranges.push([i, i + q.length]);
  });
  ranges.sort(function (a, b) { return a[0] - b[0]; });
  var h = '', pos = 0;
  ranges.forEach(function (r) {
    if (r[1] <= pos) return;
    var from = Math.max(r[0], pos);
    h += esc(t.slice(pos, from)) + '<mark>' + esc(t.slice(from, r[1])) + '</mark>';
    pos = r[1];
  });
  return h + esc(t.slice(pos));
}

/* 根拠パネル（ステップが変わったときだけ作り直す） */
function buildPanel(fi, st) {
  var fig = figures[fi], h = '';
  var step = st.type === 'step' ? fig.steps[st.stepIndex] : null;
  // 概要と結論: 根拠の抜き書きの代わりに原文（アブストラクト・結論の節）を出す（照合できない引用だけ別に出す）
  var absText = fig.isOverview ? fig.abstract_text : fig.isConclusion ? fig.source_text : '';
  var isAbs = !!absText;
  if (step) {
    h += '<div class="ph">ステップ ' + (st.stepIndex + 1) + ' / ' + fig.steps.length;
    if (step.kind === 'background') h += '<span class="tg bg">一般知識</span>';
    if (step.kind === 'unverified') h += '<span class="tg un">[要確認]</span>';
    if (step.kind === 'figure_read') h += '<span class="tg fr">図から読んだ数値</span>';
    h += '</div>';
    // 本文に無い数値（図の中の文字から読んだもの、または作られた疑いのあるもの）を明示する
    if ((step.unsupported_numbers || []).length) {
      h += '<div class="none">本文・キャプションに無い数値: ' + step.unsupported_numbers.map(esc).join('、') +
           (step.kind === 'figure_read' ? '（図の中の文字から読んだ値。図で確かめてください）' : '') + '</div>';
    }
    // 原文を出す画面では、原文の中で蛍光ペンを引けない引用（照合できない・結論の節の外から引いた）だけを別に出す
    var flat = isAbs ? absText.replace(/\s+/g, ' ') : '';
    var ev = (step.evidence || []).filter(function (e) {
      return !isAbs || e.verified === false || flat.indexOf(String(e.quote || '').replace(/\s+/g, ' ').trim()) < 0;
    });
    if (!isAbs || ev.length) h += '<div class="sec">根拠</div>';
    if (!ev.length && !isAbs) h += '<div class="none">' + (step.kind === 'background' ? '本文の根拠なし（一般知識として説明）' : '根拠の引用はありません') + '</div>';
    ev.forEach(function (e) {
      h += '<div class="ev' + (e.verified === false ? ' bad' : '') + '"><div class="where">' + esc(e.where || e.source || '') +
        (e.verified === false ? ' <span class="tg un">[要確認]</span>' : '') + '</div><blockquote>' + esc(e.quote) + '</blockquote></div>';
    });
    (step.warnings || []).forEach(function (w) { h += '<div class="wn">注意: ' + esc(w) + '</div>'; });
  }
  if (isAbs) {
    h += '<div class="sec">' + (fig.isConclusion ? '原文（' + esc(fig.source_where || '本文') + '）' : 'アブストラクト全文') +
         '</div><div class="abs">' + abstractHtml(absText, step && step.evidence) + '</div>';
    $('panel').innerHTML = h;
    var mk = $('panel').querySelector('mark');
    $('panel').scrollTop = mk ? Math.max(0, mk.offsetTop - 60) : 0;
    return;
  }
  h += '<div class="sec">この図で引用された文献</div>';
  var refs = fig.refs || [];
  if (!refs.length) h += '<div class="none">なし</div>';
  refs.forEach(function (r) {
    var ttl = r.title || String(r.raw || '').slice(0, 120);
    var url = r.url || (r.doi ? 'https://doi.org/' + r.doi : '');
    h += '<div class="rf"><span class="num">' + esc(r.label || '[' + r.num + ']') + '</span> <span class="tt">' + esc(ttl) + '</span> ' +
      (r.year ? '<span class="yr">(' + esc(r.year) + ')</span>' : '') +
      (url ? '<a href="' + esc(url) + '" target="_blank" rel="noopener">' + (r.doi ? 'DOI ' + esc(r.doi) : '論文を開く') + '</a>' : '') +
      (r.context ? '<span class="cx">' + esc(r.context) + '</span>' : '') + '</div>';
  });
  $('panel').innerHTML = h; $('panel').scrollTop = 0;
}

/* ---------- 状態 → DOM（前フレームに依存しない） ---------- */
function apply(st) {
  var fi = st.fi, fig = figures[fi];
  ensureFigure(fi);
  // 図の寄り（zoom / pan）
  var v = st.view, tx = CARD.w / 2 - v.cx * v.k, ty = CARD.h / 2 - v.cy * v.k;
  $('grp').style.transform = 'translate(' + tx.toFixed(2) + 'px,' + ty.toFixed(2) + 'px) scale(' + v.k + ')';
  // 図形
  var key = fi + ':' + st.segIndex;
  if (S.ovKey !== key) {
    var seg = buildTimeline(fig).segs[st.segIndex];
    ov = buildOverlay(fi, st.stepIndex, seg ? seg.items : [], seg ? seg.viewTo : null); S.ovKey = key;
  }
  st.shapes.forEach(function (sh) { ov.handles[sh.index].upd(sh.p, sh.alpha); });
  $('dimRect').setAttribute('opacity', st.dim);
  // 字幕
  var sub = $('sub');
  if (st.subtitle) {
    if (sub._h !== st.subtitle.html) {
      sub.innerHTML = st.subtitle.html; sub._h = st.subtitle.html;
      sub.style.display = 'block';
      // 字幕は図のカードの下（y=860 より下）に収める。長ければ字を小さくする
      var SUB_MAX = 1080 - 44 - 868;
      [38, 32, 28, 24].some(function (fs) { sub.style.fontSize = fs + 'px'; return sub.offsetHeight <= SUB_MAX; });
    }
    sub.style.display = 'block'; sub.style.opacity = st.subtitle.alpha;
  } else { sub.style.display = 'none'; sub._h = null; }
  // POINT（要約の行と入れ替える）
  var pt = $('point');
  if (st.point) {
    if (pt._t !== st.point.text) { pt.querySelector('.pt').textContent = st.point.text; pt._t = st.point.text; }
    pt.style.display = 'flex'; pt.style.opacity = st.point.alpha;
  } else pt.style.display = 'none';
  $('sum').style.opacity = st.point ? 1 - st.point.alpha : 1;
  // まとめ
  var tk = $('take');
  if (st.take) {
    if (!tk._built || tk._fi !== fi) {
      tk.innerHTML = '<h2>今日のまとめ</h2>' + (fig.takeaways_ja || []).map(function (t, i) {
        return '<div class="it"><span class="nm">' + (i + 1) + '</span><span>' + esc(t) + '</span></div>';
      }).join(''); tk._built = true; tk._fi = fi;
    }
    tk.style.display = 'block'; tk.style.opacity = st.take.bg;
    tk.firstChild.style.opacity = st.take.head;
    Array.prototype.forEach.call(tk.querySelectorAll('.it'), function (n, i) {
      var a = st.take.items[i]; n.style.opacity = a; n.style.transform = 'translateY(' + ((1 - a) * 24).toFixed(1) + 'px)';
    });
  } else tk.style.display = 'none';
  // 根拠パネル
  var pk = fi + ':' + st.segIndex;
  if (S.panelKey !== pk) { buildPanel(fi, st); S.panelKey = pk; }
  // パネルの開閉（開いていれば図は左、閉じていれば中央）
  $('panel').style.display = S.panel ? 'block' : 'none';
  $('card').style.left = (S.panel ? 40 : 360) + 'px';
  // 操作バー
  if (!CAPTURE) {
    var ds = $('dots').children;
    for (var i = 0; i < ds.length; i++) { var c = ds[i].className.replace(/\b(on|done)\b/g, '').trim(); if (i < st.segIndex) c += ' done'; if (i === st.segIndex) c += ' on'; ds[i].className = c.trim(); }
    $('time').textContent = fmtTime(st.t) + ' / ' + fmtTime(totalDuration(fi));
  }
}
function renderAt(fi, t) { S.fi = fi; S.t = t; apply(stateAt(fi, t)); }
function render() { renderAt(S.fi, S.t); }
G.__player.renderAt = renderAt;
/* 動画書き出しモード: 再生ループ・音声を止め、操作部品を隠し、舞台を縮尺 1 で左上に置く */
G.__player.exportMode = function (on) {
  on = !!on; CAPTURE = on; STILL = on || Q.get('still') === '1'; CTRL_H = on ? 0 : 60;
  document.body.classList.toggle('capture', on);
  if (on) { setPlaying(false); stopNarration(); $('menu').classList.remove('open'); setDocOpen(false); }
  fit();
  if (figures.length) { S.ovKey = null; S.panelKey = null; render(); }
};
G.__player.preload = function () {   // 全図の画像を読み込む（ヘッドレス用）
  return Promise.all(figures.map(function (f) { return new Promise(function (ok) { var i = new Image(); i.onload = i.onerror = ok; i.src = f.img; }); }));
};
