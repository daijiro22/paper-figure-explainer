/* ---------- サイトで別ファイルにした図と声（単体の HTML は全部入りなので、ここは待たない） ---------- */
var LOAD_MAX = 30000;       // 通信が止まっても固まらないよう、待つのは 30 秒まで
var PREFETCH_SEC = 15;      // 図の終わりのこの秒数前に、次の図を読んでおく
function isLazyImg(f) { return !!f.img && !/^data:/.test(f.img); }
/* 今の図を描いて声を鳴らせるか。音声がオフなら声は待たない。取れなかった声も待たない（声なしで進む） */
function figReady() {
  var f = figures[S.fi];
  return !S.imgWait && (!S.voice || !f.voicePack || f._voice === 'ready' || f._voice === 'failed');
}
function updateWait() {
  var f = figures[S.fi], msg = '';
  if (!figReady()) msg = '読み込み中…';
  else if (S.imgFailed) msg = '図を読み込めませんでした。再読み込みしてください';
  else if (S.voice && f._voice === 'failed') msg = '声を読み込めませんでした。再読み込みしてください';
  $('wait').textContent = msg; $('wait').hidden = !msg;
}
function setFigImage(f) {
  var img = $('img'), lazy = isLazyImg(f), tok = ++S.imgTok, timer = 0;
  function done(ok) {
    if (tok !== S.imgTok) return;     // すでに別の図へ移った
    clearTimeout(timer); S.imgWait = false; S.imgFailed = !ok;
    $('card').classList.remove('waiting'); updateWait();
  }
  S.imgWait = lazy; S.imgFailed = false;
  $('card').classList.toggle('waiting', lazy);
  img.onload = lazy ? function () { done(true); } : null;
  img.onerror = lazy ? function () { done(false); } : null;
  if (lazy) timer = setTimeout(function () { done(false); }, LOAD_MAX);
  img.src = f.img; img.alt = f.label || '';
}
/* 図ごとの声の束を取り、各ステップの audio に戻す。あとの鳴らし方は単体の HTML と同じ（data URL なので Range が要らない）。
   今の図でなくても取っておいてよいが、鳴らし始めるのは syncNarration だけ */
function loadVoice(f) {
  if (!f.voicePack || f._voice) return;
  f._voice = 'pending';
  var timer = 0;
  Promise.race([
    fetch(f.voicePack).then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }),
    new Promise(function (ok, ng) { timer = setTimeout(function () { ng(new Error('timeout')); }, LOAD_MAX); })
  ]).then(function (list) {
    f.steps.forEach(function (st, i) { if (list[i]) st.audio = list[i]; });
    f._voice = 'ready';
  }).catch(function () { f._voice = 'failed'; }).then(function () { clearTimeout(timer); updateWait(); });
}
function prefetchNext() {
  var f = figures[S.fi + 1];
  if (!f || f._pre) return;
  f._pre = true;
  if (isLazyImg(f)) new Image().src = f.img;
  if (S.voice) loadVoice(f);
}

/* ---------- 音声（audio が無ければ Web Speech。別ファイルの声が取れなかった図は読み上げに落とさない） ---------- */
function stopNarration() {
  if (audioEl) { try { audioEl.pause(); } catch (e) {} audioEl = null; }
  if (window.speechSynthesis) try { speechSynthesis.cancel(); } catch (e) {}
}
function startNarration(step, offset) {
  stopNarration();
  S.holdSince = 0;
  if (!S.voice || STILL) return;
  if (step.audio) {
    var a = new Audio(step.audio); audioEl = a;
    a.playbackRate = S.rate;                       // 声の高さは変えずに速さだけ変わる（preservesPitch の既定）
    if (offset > 0.3) a.addEventListener('loadedmetadata', function () { try { a.currentTime = offset; } catch (e) {} });
    a.play().catch(function () {});
  } else if (!figures[S.fi].voicePack && window.speechSynthesis && step.say && offset < 1.5) {
    var u = new SpeechSynthesisUtterance(step.say); u.lang = 'ja-JP'; u.rate = Math.min(10, 1.3 * S.rate);
    speechSynthesis.speak(u);
  }
}
function syncNarration() {
  if (!figReady()) { stopNarration(); return; }   // 届いてから鳴らす（narrKey を進めないので、届いた次のフレームで始まる）
  var st = stateAt(S.fi, S.t), key = S.fi + ':' + st.segIndex;
  if (key === S.narrKey) return;
  S.narrKey = key;
  if (st.type === 'step') startNarration(figures[S.fi].steps[st.stepIndex], st.local); else stopNarration();
}

/* 声が鳴っている間は次のステップへ進まない。ステップの長さは mp3 の長さそのままなので、音声の出力の
   立ち上がり（最初のステップで 0.7 秒ほど）だけ声が時計より遅れ、語尾が切られていた（施主 2026-11-05）。
   読み込みが止まったときに固まらないよう、待つのは HOLD_MAX 秒まで。再生できなかった声は paused なので待たない */
var HOLD_MAX = 3;
function holdForNarration(nt, now) {
  if (!audioEl || audioEl.ended || audioEl.paused) { S.holdSince = 0; return nt; }
  var tl = buildTimeline(figures[S.fi]), seg = tl.segs[findSeg(tl, S.t)];
  var end = seg.start + seg.dur;
  if (seg.type !== 'step' || nt < end) { S.holdSince = 0; return nt; }
  if (!S.holdSince) S.holdSince = now;
  return (now - S.holdSince) / 1000 > HOLD_MAX ? nt : end - 1e-3;
}

/* ---------- 再生制御 ---------- */
function setPlaying(p) {
  S.playing = p; $('btnPlay').textContent = p ? '一時停止' : '再生';
  if (p) { S.last = performance.now(); S.narrKey = null; requestAnimationFrame(tick); } else stopNarration();
}
function tick(now) {
  if (!S.playing) return;
  if (!figReady()) { S.last = now; requestAnimationFrame(tick); return; }   // 図と声が届くまで時計を止める
  // 再生スピード S.rate 倍で時計を進める（図形の動き・字幕の切り替えも同じ倍率で速くなる）
  S.t = holdForNarration(S.t + Math.min(0.1, (now - S.last) / 1000) * S.rate, now); S.last = now;
  var total = totalDuration(S.fi);
  if (total - S.t < PREFETCH_SEC) prefetchNext();
  if (S.t >= total) {
    if (S.fi < figures.length - 1) { S.fi++; S.t = 0; S.narrKey = null; }
    else { S.t = total; render(); setPlaying(false); return; }
  }
  render(); syncNarration();
  requestAnimationFrame(tick);
}
function seekTime(t) { S.t = clamp(t, 0, totalDuration(S.fi) - 1e-6); S.narrKey = null; render(); if (S.playing) syncNarration(); }
function seekSeg(i) { var tl = buildTimeline(figures[S.fi]); seekTime(tl.segs[clamp(i, 0, tl.segs.length - 1)].start + 0.001); }
function gotoFig(fi) { S.fi = clamp(fi, 0, figures.length - 1); seekTime(0); }
function nextStep() {
  var tl = buildTimeline(figures[S.fi]), i = findSeg(tl, S.t);
  if (i < tl.segs.length - 1) seekSeg(i + 1); else if (S.fi < figures.length - 1) gotoFig(S.fi + 1);
}
function prevStep() {
  var tl = buildTimeline(figures[S.fi]), i = findSeg(tl, S.t);
  if (S.t - tl.segs[i].start > 1.0 || i === 0) seekSeg(i); else seekSeg(i - 1);
}

/* ---------- 初期化 ---------- */
function buildMenu() {
  var m = $('menuList'); m.innerHTML = '';
  figures.forEach(function (f, i) {
    var d = document.createElement('div'); d.className = 'fw';
    d.innerHTML = '<div class="fi"><img alt=""><div class="fl">' + esc(f.label || ('図 ' + (i + 1))) +
                  (f.page ? ' · p.' + esc(f.page) : '') + '</div></div>';
    var im = d.firstChild.firstChild;
    if (isLazyImg(f)) { im.loading = 'lazy'; im.dataset.src = f.img; } else im.src = f.img;   // サイトでは一覧を開いたときに読む
    d.addEventListener('click', function () { gotoFig(i); $('menu').classList.remove('open'); });
    m.appendChild(d);
  });
}
function start(b) {
  setBundle(b);
  if (!figures.length) { $('loadMsg').textContent = 'figures が空です'; $('loader').style.display = 'flex'; return; }
  $('loader').style.display = 'none';
  buildMenu(); fit();
  // ?fig=2（何枚目か）か ?fig=fig3（図の id。原文の画面から来たとき）でその図から始める
  // 指定が無ければ先頭（概要があれば概要）から。?fig=2 は今まで通り図の 3 枚目（概要の分を足す）
  var qf = Q.get('fig'), byId = figures.findIndex(function (f) { return f.id === qf; });
  var off = figures[0].isOverview ? 1 : 0;
  var fi = byId >= 0 ? byId : (qf == null ? 0 : clamp((parseInt(qf, 10) || 0) + off, 0, figures.length - 1));
  var t = parseFloat(Q.get('t') || '0') || 0;
  renderAt(fi, t);
  probeDoc();
  // 全図の先読みは全部入りの HTML だけ。別ファイルのときは開いている図の分だけ読む
  G.__player.ready = figures.some(isLazyImg) ? Promise.resolve() : G.__player.preload().then(function () { renderAt(S.fi, S.t); });
}
/* 「原文」ボタン: 手元の Web 版（/p/<pid> で配っていて、原文の API が答える）のときだけ出す。
   公開サイトや単体の HTML では API が無いので出ない（ページ全体の画像は公開しない）。
   「論文一覧」: Web 版で開いているときだけ出す。単体の HTML には戻る先が無い。
   戻る先は /make に入れる人は /make、他は /（棚のスクロール位置は棚の側が覚えている） */
var DOC_PID = null;
var DOC_FI = -1;      // 原文のサイドバーがいま追いかけている図
function probeDoc() {
  var m = location.pathname.match(/^\/p\/([A-Za-z0-9_\-]+?)(?:\.html)?\/?$/);
  if (!m || CAPTURE || !/^https?:$/.test(location.protocol)) return;
  $('lnkBack').href = '/'; $('lnkBack').hidden = false;
  fetch('/make/api/papers/' + m[1] + '/pages').then(function (r) {
    if (!r.ok) return null;
    DOC_PID = m[1]; $('btnDoc').hidden = false; $('lnkBack').href = '/make';
    return r.json();
  }).then(function (info) {
    // Notion に登録してある本だけ出す。URL はサーバが /make の守りの内側で解決する
    if (info && info.notion) { $('lnkNotion').href = '/make/notion/' + m[1]; $('lnkNotion').hidden = false; }
  }).catch(function () {});
}
/* 今の図の原文の URL。embed は右のサイドバー用（doc.html が見出しと縮小一覧を隠す） */
function docUrl(embed) {
  var cur = figures[S.fi];        // 概要ならアブストラクトのページへ
  return '/make/doc/' + DOC_PID + (embed ? '?embed=1' : '') +
         (cur.isOverview ? '#p=' + (cur.page || 1) : '#fig=' + encodeURIComponent(cur.id));
}
/* サイドバーを今の図のページへ。図が変わったときだけ動かす（読んでいる位置を勝手に動かさない）。
   location.replace はブラウザの「戻る」の履歴を増やさない */
function syncDoc() {
  if (!$('docPane').classList.contains('open') || DOC_FI === S.fi) return;
  DOC_FI = S.fi;
  $('docFrame').contentWindow.location.replace(docUrl(true));
  $('docTab').href = docUrl(false);
}
function setDocOpen(on) {
  $('docPane').classList.toggle('open', on);
  $('btnDoc').classList.toggle('on', on);
  fit();
  syncDoc();
}
function tryLoad(txt) {
  try { start(JSON.parse(txt)); } catch (e) { $('loadMsg').textContent = '読み込めませんでした: ' + e.message; $('loader').style.display = 'flex'; }
}

{   // 操作部品のイベント（exportMode(false) で戻しても動くよう常に登録）
  $('btnPlay').addEventListener('click', function () { setPlaying(!S.playing); });
  $('btnNext').addEventListener('click', nextStep);
  $('btnPrev').addEventListener('click', prevStep);
  $('btnNextFig').addEventListener('click', function () { gotoFig(S.fi + 1); });
  $('btnPrevFig').addEventListener('click', function () { gotoFig(S.fi - 1); });
  $('btnMenu').addEventListener('click', function () {
    $('menu').classList.toggle('open');
    Array.prototype.forEach.call($('menuList').querySelectorAll('img[data-src]'), function (im) { im.src = im.dataset.src; im.removeAttribute('data-src'); });
  });
  $('btnDoc').addEventListener('click', function () {
    if (!DOC_PID) return;
    // スマホなど狭い画面では並べると両方読めないので、別のタブで開く
    if (window.innerWidth < 900) { setPlaying(false); window.open(docUrl(false), '_blank'); return; }
    setDocOpen(!$('docPane').classList.contains('open'));
  });
  $('btnDocClose').addEventListener('click', function () { setDocOpen(false); });
  $('docTab').addEventListener('click', function () { setPlaying(false); });   // 別のタブで読む間は声を止める
  $('lnkNotion').addEventListener('click', function () { setPlaying(false); });
  // サイドバーの原文で図を押したら、その図の解説へ（同じ画面の中のサイドバーからだけ受ける）
  window.addEventListener('message', function (e) {
    if (e.origin !== location.origin || e.source !== $('docFrame').contentWindow || !e.data || e.data.pfe !== 'gotoFig') return;
    var i = figures.findIndex(function (f) { return f.id === e.data.fig; });
    if (i < 0) return;
    DOC_FI = i;      // 押された場所をそのまま読めるよう、サイドバーは動かさない
    gotoFig(i);
    window.focus();  // Space などのキー操作を解説の側へ戻す
  });
  // 再生スピード: ?speed= があればそれ、無ければこのブラウザで前に選んだ速さ（保存できない環境でも動く）
  var saved = null;
  try { saved = parseFloat(localStorage.getItem('pfe.rate')); } catch (e) {}
  var want = parseFloat(Q.get('speed')) || saved || 1;
  S.rate = RATES.reduce(function (b, r) { return Math.abs(r - want) < Math.abs(b - want) ? r : b; }, 1);
  $('selRate').innerHTML = RATES.map(function (r) {
    return '<option value="' + r + '"' + (r === S.rate ? ' selected' : '') + '>' + r + '×</option>';
  }).join('');
  $('selRate').addEventListener('change', function (e) {
    S.rate = parseFloat(e.target.value) || 1;
    if (audioEl) audioEl.playbackRate = S.rate;
    if (!audioEl && window.speechSynthesis && speechSynthesis.speaking) S.narrKey = null;   // Web Speech は読み直す
    try { localStorage.setItem('pfe.rate', String(S.rate)); } catch (e2) {}
  });
  $('chkVoice').addEventListener('change', function (e) {
    S.voice = e.target.checked;
    if (!S.voice) stopNarration(); else S.narrKey = null;
    if (figures.length) { if (S.voice) loadVoice(figures[S.fi]); updateWait(); }
  });
  $('chkPanel').addEventListener('change', function (e) { S.panel = e.target.checked; render(); });
  $('btnFull').addEventListener('click', function () {
    if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen();
  });
}
document.addEventListener('keydown', function (e) {
  if ($('loader').style.display === 'flex' || !figures.length) return;
  if (e.code === 'Space') { e.preventDefault(); setPlaying(!S.playing); }
  else if (e.code === 'ArrowRight') { e.preventDefault(); nextStep(); }
  else if (e.code === 'ArrowLeft') { e.preventDefault(); prevStep(); }
  else if (e.code === 'ArrowDown') { e.preventDefault(); gotoFig(S.fi + 1); }
  else if (e.code === 'ArrowUp') { e.preventDefault(); gotoFig(S.fi - 1); }
});
/* ファイル選択・ドラッグ＆ドロップ */
function readFile(f) { var r = new FileReader(); r.onload = function () { tryLoad(r.result); }; r.readAsText(f, 'utf-8'); }
$('fileIn').addEventListener('change', function (e) { if (e.target.files[0]) readFile(e.target.files[0]); });
['dragover', 'dragenter'].forEach(function (n) { document.addEventListener(n, function (e) { e.preventDefault(); $('dz').classList.add('over'); }); });
['dragleave', 'drop'].forEach(function (n) { document.addEventListener(n, function (e) { e.preventDefault(); $('dz').classList.remove('over'); }); });
document.addEventListener('drop', function (e) { if (e.dataTransfer.files[0]) readFile(e.dataTransfer.files[0]); });

fit();
var raw = $('bundle').textContent.trim();
if (raw && /^[\[{]/.test(raw)) tryLoad(raw); else $('loader').style.display = 'flex';
