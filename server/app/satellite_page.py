"""Сателлит для телефона — страница, а не приложение.

Ставить Android-приложение ради проверки идеи дорого: нужен SDK, Gradle,
JDK, и всё это ради того, чтобы гнать звук в сокет. Браузер умеет ровно
то же самое и открывается на любом телефоне, включая старый.

Важнее удобства: `getUserMedia` с `echoCancellation` включает аппаратное
эхоподавление телефона — тот самый тракт, что делают для громкой связи.
Именно ради него сателлит и задумывался: у нашей платы опорного канала
нет, а у любого телефона он есть.

Ограничение честное: браузер требует защищённого контекста, поэтому по
голому http микрофон не откроется. Как это обойти — написано прямо на
странице, чтобы не искать.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app import webstyle
from app.config import settings

router = APIRouter()

_PAGE = """<!doctype html>
<html lang="ru">
<meta charset="utf-8">
<title>Сателлит</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>📡</text></svg>">
<style>
__BASE_CSS__
  .panel { text-align: center; }
  #state { font-size: 1.9rem; font-weight: 700; margin: 6px 0 4px; min-height: 2.3rem; }
  #text { color: var(--muted); min-height: 2.6rem; font-size: .95rem; }
  #meter { height: 12px; background: var(--surface2); border-radius: 7px;
           overflow: hidden; margin: 18px 0; }
  #bar { height: 100%; width: 0; background: linear-gradient(90deg, var(--good), var(--accent));
         transition: width .1s; }
  #go { width: 100%; padding: 15px; border-radius: 14px; border: 0;
        background: var(--surface2); color: var(--text); font-weight: 700; font-size: 1rem; }
  #go:disabled { opacity: .5; }
  #talk { display: block; width: 100%; margin: 12px 0 0; padding: 22px;
          border-radius: 16px; border: 0; font-size: 1.25rem; font-weight: 700;
          background: linear-gradient(135deg, var(--accent), var(--accent2)); color: #fff; }
  #talk.active { background: linear-gradient(135deg, #ff8a5c, var(--danger)); }
  .hint { color: var(--muted); font-size: .85rem; text-align: left;
          background: var(--surface2); padding: 14px; border-radius: var(--radius-sm);
          margin-top: 18px; }
  code { color: var(--accent); word-break: break-all; }
  .err { color: var(--danger); min-height: 1.2em; margin-top: 10px; font-size: .9rem; }
</style>

__NAV__
<div class="wrap">

  <div class="hero">
    <h1>Сателлит</h1>
    <p>Ещё один микрофон для той же колонки — разговор, память и ответ общие.</p>
  </div>

  <div class="card panel">
    <div id="state">—</div>
    <div id="text"></div>
    <div id="meter"><div id="bar"></div></div>
    <button id="go">Слушать</button>
    <button id="talk" hidden>🎤 Спросить</button>
    <div id="err" class="err"></div>

    <div class="hint" id="hint" hidden>
      <b>Микрофон недоступен.</b> Браузер открывает его только на защищённой
      странице.
      <span id="hintAndroid">На Android это лечится так: открой
      <code>chrome://flags/#unsafely-treat-insecure-origin-as-secure</code>,
      впиши туда адрес этой страницы, включи и перезапусти браузер.</span>
      <span id="hintIOS" hidden>На iPhone такого флага нет — там любой браузер,
      включая Chrome, работает на системном WebKit, а не Chromium. Нужен
      настоящий HTTPS: открой <code id="hintIOSUrl"></code> вместо этой
      страницы (сертификат самоподписанный — один раз подтверди «всё равно
      открыть»).</span>
    </div>
  </div>

</div>

<script>
const RATE = 16000, FRAME = 320, FRAME_MIC = 0x01;
const $ = (id) => document.getElementById(id);
const STATES = {idle: 'Готова', listening: 'Слушаю', thinking: 'Думаю',
                speaking: 'Отвечает', playing: 'Играет'};

let ws, ctx, node, stream, running = false;

function show(err) { $('err').textContent = err || ''; }

async function start() {
  $('go').disabled = true;
  try {
    // Ровно эти три флага и включают аппаратный тракт телефона: тот же,
    // что работает при громкой связи. Ради него всё и затевалось.
    stream = await navigator.mediaDevices.getUserMedia({audio: {
      echoCancellation: true, noiseSuppression: true, autoGainControl: true,
      channelCount: 1,
    }});
  } catch (e) {
    show('Не дали микрофон: ' + e.name);
    if (!window.isSecureContext) {
      $('hint').hidden = false;
      const isIOS = /iP(hone|ad|od)/.test(navigator.userAgent);
      $('hintAndroid').hidden = isIOS;
      $('hintIOS').hidden = !isIOS;
      if (isIOS) $('hintIOSUrl').textContent = `https://${location.hostname}:__TLS_PORT__/satellite`;
    }
    $('go').disabled = false;
    return;
  }

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/stream`);
  ws.binaryType = 'arraybuffer';

  ws.onopen = () => {
    ws.send(JSON.stringify({
      t: 'hello', device: 'phone', room: 'home',
      role: 'satellite', codec: 'pcm', screen: true, fw: 'web',
    }));
    running = true;
    $('go').textContent = 'Остановить';
    $('go').disabled = false;
    $('talk').hidden = false;
    show('');
  };

  ws.onmessage = (ev) => {
    if (typeof ev.data !== 'string') return;   // звук и картинки нам не нужны
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.t === 'state') {
      $('state').textContent = STATES[m.value] || m.value;
      if (m.value === 'idle' || m.value === 'listening') $('text').textContent = '';
      // Слушаю — кнопка красная и подписана «Стоп»: повторный тап обрывает
      // запись раньше тишины (тот же смысл, что у тапа физической кнопки).
      const listening = m.value === 'listening';
      $('talk').textContent = listening ? '⏹ Стоп' : '🎤 Спросить';
      $('talk').classList.toggle('active', listening);
    }
  };

  ws.onclose = () => { if (running) stop('Соединение закрыто'); };
  ws.onerror = () => show('Ошибка соединения');

  ctx = new (window.AudioContext || window.webkitAudioContext)();
  const src = ctx.createMediaStreamSource(stream);
  // ScriptProcessor устарел, но жив везде, включая старые телефоны —
  // а AudioWorklet есть не во всех сборках Android.
  node = ctx.createScriptProcessor(4096, 1, 1);

  const ratio = ctx.sampleRate / RATE;
  let acc = [];

  node.onaudioprocess = (e) => {
    if (!running || ws.readyState !== 1) return;
    const input = e.inputBuffer.getChannelData(0);

    // Прореживание до 16 кГц с усреднением: брать каждый N-й отсчёт
    // дало бы призвуки, которые портят распознавание.
    const step = ratio;
    for (let i = 0; i + step <= input.length; i += step) {
      let sum = 0, n = 0;
      for (let j = Math.floor(i); j < Math.floor(i + step); j++) { sum += input[j]; n++; }
      acc.push(n ? sum / n : 0);
    }

    let peak = 0;
    while (acc.length >= FRAME) {
      const chunk = acc.splice(0, FRAME);
      const buf = new ArrayBuffer(1 + FRAME * 2);
      const view = new DataView(buf);
      view.setUint8(0, FRAME_MIC);
      for (let i = 0; i < FRAME; i++) {
        const v = Math.max(-1, Math.min(1, chunk[i]));
        view.setInt16(1 + i * 2, v * 32767, true);
        peak = Math.max(peak, Math.abs(v));
      }
      ws.send(buf);
    }
    $('bar').style.width = Math.min(100, peak * 160) + '%';
  };

  src.connect(node);
  // Без подключения к выходу ScriptProcessor не вызывается вовсе;
  // громкость нулевая, поэтому телефон ничего не играет.
  const mute = ctx.createGain();
  mute.gain.value = 0;
  node.connect(mute);
  mute.connect(ctx.destination);

  // Экран не должен гаснуть: иначе браузер усыпит вкладку и микрофон.
  if ('wakeLock' in navigator) {
    navigator.wakeLock.request('screen').catch(() => {});
  }
}

function stop(msg) {
  running = false;
  try { node && node.disconnect(); } catch {}
  try { ctx && ctx.close(); } catch {}
  try { stream && stream.getTracks().forEach(t => t.stop()); } catch {}
  try { ws && ws.close(); } catch {}
  $('go').textContent = 'Слушать';
  $('go').disabled = false;
  $('talk').hidden = true;
  $('talk').classList.remove('active');
  $('state').textContent = '—';
  $('bar').style.width = '0';
  if (msg) show(msg);
}

$('go').onclick = () => (running ? stop() : start());

// У сателлита нет активационного слова — это его и заменяет. Тот же
// протокол, что у физической кнопки на колонке: "ptt" down — тап,
// не удержание, конец реплики определяет сервер по тишине сам. Слать
// можно с любого подключённого устройства, сервер не привязывает
// команду к конкретному микрофону.
$('talk').onclick = () => {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({t: 'ptt', state: 'down'}));
};
</script>
</html>
"""


@router.get("/satellite", response_class=HTMLResponse)
async def satellite() -> str:
    return (
        _PAGE.replace("__TLS_PORT__", str(settings.tls_port))
        .replace("__BASE_CSS__", webstyle.BASE_CSS)
        .replace("__NAV__", webstyle.nav("satellite"))
    )
