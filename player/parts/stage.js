var $ = function (id) { return document.getElementById(id); };
var NS = 'http://www.w3.org/2000/svg';
var Q = new URLSearchParams(location.search);
var CAPTURE = Q.get('capture') === '1', STILL = CAPTURE || Q.get('still') === '1';
var CTRL_H = CAPTURE ? 0 : 60;
if (CAPTURE) document.body.classList.add('capture');

/* UI の状態（描画結果は stateAt だけで決まる。ここは再生位置など操作の状態のみ） */
var S = {fi: 0, t: 0, playing: false, voice: true, panel: true, last: 0, narrKey: null, curFi: -1, ovKey: null, panelKey: null,
         rate: 1,    // 再生スピード（操作バーで選ぶ。?speed=1.5 でも指定できる）
         imgTok: 0, imgWait: false, imgFailed: false};   // サイトで別ファイルにした図の読み込みの状態
var RATES = [0.75, 1, 1.25, 1.5, 1.75, 2];
var ov = null;           // 現在ステップの SVG 要素群 {handles:[]}
var audioEl = null;

/* ---------- 舞台の縮尺（レターボックス） ---------- */
function fit() {
  if (CAPTURE) { $('stage').style.transform = 'none'; return; }   // 書き出し: 縮尺 1・左上固定・レターボックス無し
  var w = window.innerWidth - $('docPane').offsetWidth, h = window.innerHeight - CTRL_H;   // 原文を開いている分だけ舞台を縮める
  var s = Math.min(w / 1920, h / 1080);
  $('stage').style.transform = 'translate(' + ((w - 1920 * s) / 2) + 'px,' + ((h - 1080 * s) / 2) + 'px) scale(' + s + ')';
}
window.addEventListener('resize', fit);
