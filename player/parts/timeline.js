/* =====================================================================
 * 論文の図 解説プレーヤー
 *  - 前半: DOM に触れない純粋部分（時間割 timeline / stateAt）。node の vm でも動く。
 *  - 後半: DOM 描画（apply）と再生ループ。
 *  描画は「stateAt(fig, t) の結果を DOM に反映する」だけで、前フレームの状態に依存しない。
 *  （将来のヘッドレス書き出し用: window.__player.renderAt(fi, t) を t 刻みで呼ぶ）
 * ===================================================================== */
var G = (typeof window !== 'undefined') ? window : globalThis;

/* ---------- 定数 ---------- */
var CPS = 7, PAUSE = 0.8;                 // 音声が無いときの見積り: 7 文字/秒 + 0.8 秒
var CARD = {w: 1200, h: 720, pad: 20};    // 図のカード（論理 px）
var VIEW_T = 0.9;                         // 寄り・引きにかける秒数
var REF_K = 0.64;                         // 注記の線・札の大きさの基準になる表示倍率（buildOverlay）
var ANIM = {band: 1.1, circle: 0.9, trace: 1.6, axis: 0.9, box: 0.7, arrow: 0.8};
var figures = [];
var paper = {};

/* ---------- 小道具 ---------- */
function clamp(x, a, b) { return x < a ? a : (x > b ? b : x); }
function easeOut(p) { p = clamp(p, 0, 1); return 1 - Math.pow(1 - p, 3); }
function easeInOut(p) { p = clamp(p, 0, 1); return p < .5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2; }
function easeBack(p) { p = clamp(p, 0, 1); var c = 1.70158; return 1 + (c + 1) * Math.pow(p - 1, 3) + c * Math.pow(p - 1, 2); }
function esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
/* 字幕 HTML: <em> だけ通す（それ以外のタグは文字として表示） */
function capHtml(step) {
  if (step.caption_html) return esc(step.caption_html).replace(/&lt;(\/?)em&gt;/g, '<$1em>');
  return esc(step.say || '');
}
function fmtTime(s) { s = Math.max(0, Math.round(s)); return Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2); }

/* ---------- 図ごとの寄り（view）: {cx,cy,k} = 画像 px の中心と、1 画像 px あたりの論理 px ---------- */
function fitScale(fig) { return Math.min((CARD.w - 2 * CARD.pad) / fig.width, (CARD.h - 2 * CARD.pad) / fig.height); }
function fullView(fig) { return {cx: fig.width / 2, cy: fig.height / 2, k: fitScale(fig)}; }
// 寄りの上限は全体表示の 2 倍。寄る範囲は画面の 7 割 5 分ほどに収め、周りも少し見せる
// （施主 2026-10-30「ズームインが過剰なときがある」→ 3 倍にしたあと「上限 2 倍にしよう」。
//   もとは 8 倍まで寄って図の 2% しか映らないステップがあった）
var ZOOM_MAX = 2, ZOOM_FILL = 0.75;
function zoomView(fig, b) {
  var bw = Math.max(20, b[2] - b[0]), bh = Math.max(20, b[3] - b[1]), k0 = fitScale(fig);
  var k = Math.min((CARD.w - 2 * CARD.pad) / bw, (CARD.h - 2 * CARD.pad) / bh) * ZOOM_FILL;
  k = Math.max(Math.min(k, k0 * ZOOM_MAX), k0);
  // 中心は図の内側に収める（端の部分に寄ったとき、図の外の空白が映らないように）
  var vw = CARD.w / k, vh = CARD.h / k;
  var cx = vw >= fig.width ? fig.width / 2 : clamp((b[0] + b[2]) / 2, vw / 2, fig.width - vw / 2);
  var cy = vh >= fig.height ? fig.height / 2 : clamp((b[1] + b[3]) / 2, vh / 2, fig.height - vh / 2);
  return {cx: cx, cy: cy, k: k};
}
function lerpView(a, b, p) {   // 中心は線形・倍率は対数で補間（寄り引きが自然）
  return {cx: a.cx + (b.cx - a.cx) * p, cy: a.cy + (b.cy - a.cy) * p, k: Math.exp(Math.log(a.k) + (Math.log(b.k) - Math.log(a.k)) * p)};
}
function zoomBoxOf(step) {
  var s = (step.shapes || []).filter(function (x) { return x.kind === 'zoom'; })[0];
  return s ? s.box : null;
}
function bboxOf(s) {
  if (s.box) return s.box;
  if (s.kind === 'circle') return [s.center[0] - s.r, s.center[1] - s.r, s.center[0] + s.r, s.center[1] + s.r];
  if (s.kind === 'arrow') return [Math.min(s.from[0], s.to[0]), Math.min(s.from[1], s.to[1]), Math.max(s.from[0], s.to[0]), Math.max(s.from[1], s.to[1])];
  if (s.points) { var xs = s.points.map(function (p) { return p[0]; }), ys = s.points.map(function (p) { return p[1]; });
    return [Math.min.apply(0, xs), Math.min.apply(0, ys), Math.max.apply(0, xs), Math.max.apply(0, ys)]; }
  return [0, 0, 0, 0];
}
/* 縦長の図は全体表示だと小さい（Hololignin Fig.5）。zoom の指示が無いステップでも、注記の範囲へ
   横幅いっぱいまで寄り、注記が真ん中に来るよう上下に動かす。図全体を囲む注記なら全体表示のまま。 */
function autoView(fig, items) {
  var full = fullView(fig), kw = (CARD.w - 2 * CARD.pad) / fig.width;
  if (!items.length || kw < full.k * 1.3) return full;     // 横長の図はそのまま
  var u = items.map(bboxOf).reduce(function (a, b) {
    return [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[2], b[2]), Math.max(a[3], b[3])];
  });
  var pad = Math.max(40, 0.05 * fig.height);
  var k = Math.max(full.k, Math.min(kw, full.k * ZOOM_MAX, (CARD.h - 2 * CARD.pad) / (u[3] - u[1] + 2 * pad)));
  var vw = CARD.w / k, vh = CARD.h / k;                    // 見えている範囲（画像 px）
  var cx = vw >= fig.width ? fig.width / 2 : clamp((u[0] + u[2]) / 2, vw / 2, fig.width - vw / 2);
  var cy = vh >= fig.height ? fig.height / 2 : clamp((u[1] + u[3]) / 2, vh / 2, fig.height - vh / 2);
  return {cx: cx, cy: cy, k: k};
}

/* ---------- 時間割 ----------
 * 各ステップ: 長さ = step.duration（音声の長さ）か、say の文字数/7 + 0.8 秒。
 * ステップ内で図形は順に始まる: 開始 = lead + 0.15·j·(ナレーションの長さ)（j は zoom を除く図形の通し番号）。
 * 短すぎて最後の図形が描き終わらない場合はステップを延ばす。
 * lead は寄り・引きがあるとき 1.0 秒、無ければ 0.4 秒。 */
function buildTimeline(fig) {
  if (fig._tl) return fig._tl;
  var steps = fig.steps || [], segs = [], t = 0, prevView = fullView(fig);
  for (var i = 0; i < steps.length; i++) {
    var st = steps[i], zb = zoomBoxOf(st);
    var items = (st.shapes || []).filter(function (s) { return s.kind !== 'zoom'; });
    var curView = zb ? zoomView(fig, zb) : autoView(fig, items);
    var changed = JSON.stringify(curView) !== JSON.stringify(prevView);
    var lead = changed ? 1.0 : 0.4;
    var base = st.duration > 0 ? st.duration : (String(st.say || '').length / CPS + PAUSE);
    var maxAnim = 0.5;
    items.forEach(function (s) { maxAnim = Math.max(maxAnim, ANIM[s.kind] || 0.9); });
    var d0 = Math.max(base, changed ? VIEW_T + 0.3 : 0.5, items.length ? lead + maxAnim + 0.5 : 0);
    var stagger = 0.15 * d0;
    var dur = Math.max(d0, items.length ? lead + stagger * (items.length - 1) + maxAnim + 0.5 : d0);
    segs.push({type: 'step', idx: i, start: t, dur: dur, lead: lead, stagger: stagger, items: items,
               viewFrom: prevView, viewTo: curView});
    t += dur; prevView = curView;
  }
  var tk = fig.takeaways_ja || [];
  if (tk.length) {
    var d = 1.0 + 1.4 * tk.length + 2.0;
    segs.push({type: 'take', idx: steps.length, start: t, dur: d, items: [], viewFrom: prevView, viewTo: fullView(fig)});
    t += d;
  }
  fig._tl = {segs: segs, total: t};
  return fig._tl;
}
function totalDuration(fi) { return buildTimeline(figures[fi]).total; }
function findSeg(tl, t) {
  var s = tl.segs, i = 0;
  for (var j = 0; j < s.length; j++) { if (s[j].start <= t + 1e-9) i = j; else break; }
  return i;
}

/* ---------- stateAt: 時刻だけから決まる純粋関数 ---------- */
function stateAt(fi, t) {
  var fig = figures[fi], tl = buildTimeline(fig);
  t = clamp(t, 0, tl.total);
  var si = findSeg(tl, t), seg = tl.segs[si];
  var out = {fi: fi, t: t, segIndex: si, segCount: tl.segs.length, type: seg ? seg.type : 'empty', stepIndex: seg ? seg.idx : -1,
             local: 0, dur: 0, subtitle: null, shapes: [], dim: 0, view: fullView(fig), point: null, take: null};
  if (!seg) return out;
  var local = clamp(t - seg.start, 0, seg.dur);
  out.local = local; out.dur = seg.dur;
  out.view = lerpView(seg.viewFrom, seg.viewTo, easeInOut(local / VIEW_T));
  var fade = clamp((seg.dur - local) / 0.3, 0, 1);          // ステップの終わり 0.3 秒で図形を消す
  if (seg.type === 'step') {
    var step = fig.steps[seg.idx];
    out.subtitle = {html: capHtml(step), alpha: easeOut(local / 0.25)};
    var anyDim = false;
    seg.items.forEach(function (s, j) {
      var a = ANIM[s.kind] || 0.9, st0 = seg.lead + seg.stagger * j;
      out.shapes.push({shape: s, index: j, p: clamp((local - st0) / a, 0, 1), alpha: fade});
      if (s.kind !== 'arrow') anyDim = true;
    });
    if (anyDim) out.dim = 0.38 * easeOut(local / 0.5) * fade;
    if (step.point) {                                       // POINT: 見出し下の 1 行に 0.4 秒から出し、ステップの終わりで消す
      out.point = {text: step.point, alpha: Math.min(easeOut((local - 0.4) / 0.3), fade)};
    }
  } else {                                                  // まとめ
    var items = fig.takeaways_ja || [];
    out.take = {bg: easeOut(local / 0.5), head: easeOut(local / 0.5),
                items: items.map(function (_, j) { return easeOut((local - (0.9 + 1.4 * j)) / 0.5); })};
  }
  return out;
}

/* ---------- バンドル受け取り（純粋部分） ---------- */
function setBundle(b) {
  paper = b.paper || {};
  // 概要（アブストラクトの解説）があれば、図の列の先頭に置いて最初に流す（施主 2026-10-30「概要を把握してから読みたい」）。
  // bundle の figures は変えない（図の数・?fig=fig3 の指定はそのまま）
  var list = (b.overview && (b.overview.steps || []).length ? [Object.assign({isOverview: true}, b.overview)] : [])
             .concat(b.figures || []);
  figures = list.map(function (f) {
    f.width = f.width || 1800; f.height = f.height || 1080; f._tl = null; return f;
  });
}

G.__player = {
  stateAt: stateAt, totalDuration: totalDuration,   // totalDuration は最後の「まとめ」画面の時間を含む
  // 各ステップの開始秒（stateAt と同じ時間割。まとめ画面の開始は含まない）
  stepStarts: function (fi) { return buildTimeline(figures[fi]).segs.filter(function (s) { return s.type === 'step'; }).map(function (s) { return s.start; }); },
  timeline: function (fi) { return buildTimeline(figures[fi]); },
  setBundle: setBundle,
  get figures() { return figures; },
  renderAt: function () { throw new Error('DOM が初期化されていません'); }
};
