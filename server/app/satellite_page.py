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

from app.config import settings

router = APIRouter()

_PAGE = """<!doctype html>
<html lang="ru">
<meta charset="utf-8">
<title>Сателлит</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { color-scheme: dark; }
  body { font: 16px/1.5 system-ui, sans-serif; margin: 0; padding: 20px;
         background: #111; color: #eee; text-align: center; }
  h1 { font-size: 1.2rem; font-weight: 500; margin: 0 0 20px; color: #999; }
  #state { font-size: 2rem; margin: 24px 0 8px; min-height: 2.4rem; }
  #text { color: #bbb; min-height: 3rem; margin-bottom: 20px; }
  #meter { height: 14px; background: #222; border-radius: 7px;
           overflow: hidden; margin: 20px 0; }
  #bar { height: 100%; width: 0; background: #3c6; transition: width .1s; }
  button { font: inherit; padding: 14px 28px; border-radius: 10px;
           border: 0; background: #3c6; color: #000; font-weight: 600; }
  button:disabled { background: #333; color: #777; }
  .hint { color: #777; font-size: .85rem; text-align: left;
          background: #1a1a1a; padding: 12px; border-radius: 8px;
          margin-top: 24px; }
  code { color: #9cf; word-break: break-all; }
  .err { color: #f77; }
</style>

<h1>Сателлит колонки</h1>
<div id="state">—</div>
<div id="text"></div>
<div id="meter"><div id="bar"></div></div>
<button id="go">Слушать</button>
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
    show('');
  };

  ws.onmessage = (ev) => {
    if (typeof ev.data !== 'string') return;   // звук и картинки нам не нужны
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.t === 'state') {
      $('state').textContent = STATES[m.value] || m.value;
      if (m.value === 'idle' || m.value === 'listening') $('text').textContent = '';
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
  $('state').textContent = '—';
  $('bar').style.width = '0';
  if (msg) show(msg);
}

$('go').onclick = () => (running ? stop() : start());
</script>
</html>
"""


@router.get("/satellite", response_class=HTMLResponse)
async def satellite() -> str:
    return _PAGE.replace("__TLS_PORT__", str(settings.tls_port))
