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

import time

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from app import webstyle
from app.config import settings
from app.tools.weather import current_conditions

router = APIRouter()

_STATIC = Path(__file__).resolve().parent / "static"
# onnxruntime-web с CDN: собственная копия wasm весит ~10 МБ, а телефону
# всё равно нужен интернет для облачного ответа. Версия закреплена — с ней
# конвейер проверен на синтезированных фразах (app/static/wakeword.js).
_ORT_VERSION = "1.22.0"

# Кэш на весь процесс, не на сессию: у киоска локация всегда одна и та же
# (default_city из настроек), несколько устройств могут спрашивать погоду
# одновременно — незачем дёргать Open-Meteo на каждое обновление вкладки.
_weather_cache: dict | None = None
_weather_cache_at: float = 0.0
_WEATHER_TTL_S = 600  # 10 минут — для виджета этого достаточно

_PAGE = """<!doctype html>
<html lang="ru">
<meta charset="utf-8">
<title>Сателлит</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>📡</text></svg>">
<style>
__BASE_CSS__
  .kiosk { text-align: center; }
  #clock { font-size: 3.6rem; font-weight: 700; line-height: 1; letter-spacing: .02em; }
  #date { color: var(--muted); margin: 6px 0 14px; text-transform: capitalize; }
  #weather { font-size: 1.3rem; }
  #weather .icon { font-size: 1.6rem; vertical-align: -3px; margin-right: 6px; }
  #weather .desc { color: var(--muted); font-size: 1rem; }
  .panel { text-align: center; }
  .panel[hidden] { display: none; }
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
  .vol { display: flex; align-items: center; gap: 12px; margin-top: 18px;
         padding-top: 16px; border-top: 1px solid var(--line); }
  .vol span:first-child { font-size: 1.2rem; }
  .vol input[type=range] { flex: 1; accent-color: var(--accent); height: 20px; }
  .vol .pct { min-width: 3.2em; text-align: right; font-variant-numeric: tabular-nums;
              font-weight: 700; color: var(--muted); font-size: .88rem; }
  .hint { color: var(--muted); font-size: .85rem; text-align: left;
          background: var(--surface2); padding: 14px; border-radius: var(--radius-sm);
          margin-top: 18px; }
  code { color: var(--accent); word-break: break-all; }
  .hint-line { color: var(--muted); font-size: .85rem; margin-top: 8px; }
  .err { color: var(--danger); min-height: 1.2em; margin-top: 10px; font-size: .9rem; }
</style>

__NAV__
<div class="wrap">

  <div class="hero">
    <h1>Сателлит</h1>
    <p>Ещё один микрофон для той же колонки — разговор, память и ответ общие.</p>
  </div>

  <div class="card kiosk">
    <div id="clock">--:--</div>
    <div id="date"></div>
    <div id="weather"></div>
  </div>

  <div class="card panel">
    <div id="state" hidden>—</div>
    <div id="text"></div>
    <div id="meter"><div id="bar"></div></div>
    <button id="go">Слушать</button>
    <button id="talk" hidden>🎤 Спросить</button>

    <div class="vol" id="volBox" hidden>
      <span>🔉</span>
      <input type="range" id="volSlider" min="0" max="100" value="70">
      <span class="pct" id="volPct">70%</span>
    </div>

    <div id="wake" class="hint-line" hidden></div>
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
      открыть»). Для киоска, который стоит на подставке постоянно, этот же
      HTTPS-адрес нужен и затем, чтобы экран надёжно не гас (Wake Lock).</span>
    </div>
  </div>

</div>

<script src="https://cdn.jsdelivr.net/npm/onnxruntime-web@__ORT_VERSION__/dist/ort.min.js"></script>
<script src="/wakeword/wakeword.js"></script>
<script>
const RATE = 16000, FRAME = 320, FRAME_MIC = 0x01;
const WAKE_MODEL = '__WAKE_MODEL__', WAKE_THRESHOLD = __WAKE_THRESHOLD__;
const $ = (id) => document.getElementById(id);
const STATES = {idle: 'Готова', listening: 'Слушаю', thinking: 'Думаю',
                speaking: 'Отвечает', playing: 'Играет'};

let ws, ctx, node, stream, running = false;
let curState = 'idle';

// --- активационное слово прямо на телефоне (openWakeWord) ---
// Звук то же, что уходит на сервер; инференс — асинхронно, вне
// onaudioprocess: не успели — лишние кадры выбрасываем, а не копим.
let wake = null, wakeQueue = [], wakeBusy = false, wakeCooldownUntil = 0;

async function initWake() {
  if (wake || typeof ort === 'undefined' || typeof WakeWord === 'undefined') {
    if (!wake) $('wake').textContent = 'Слово недоступно: не загрузился onnxruntime';
    $('wake').hidden = false;
    return;
  }
  try {
    ort.env.wasm.wasmPaths = 'https://cdn.jsdelivr.net/npm/onnxruntime-web@__ORT_VERSION__/dist/';
    $('wake').textContent = 'Загружаю слово…';
    $('wake').hidden = false;
    wake = await WakeWord.load(ort, '/wakeword/', WAKE_MODEL);
    $('wake').textContent = 'Скажите «Hey Jarvis»';
  } catch (e) {
    $('wake').textContent = 'Слово недоступно: ' + (e.message || e);
  }
}

async function pumpWake() {
  if (wakeBusy || !wake) return;
  wakeBusy = true;
  try {
    while (wakeQueue.length) {
      const score = await wake.push(wakeQueue.shift());
      if (score === null) continue;
      if (score < WAKE_THRESHOLD) {
        // Почти услышала — показываем, чтобы порог подбирать по цифрам,
        // а не наугад.
        if (score > 0.1 && Date.now() >= wakeCooldownUntil) {
          $('wake').textContent = 'Почти: ' + score.toFixed(2) + ' (порог ' + WAKE_THRESHOLD + ')';
        }
        continue;
      }
      if (Date.now() < wakeCooldownUntil) continue;
      if (curState !== 'idle' && curState !== 'playing') continue;
      wakeCooldownUntil = Date.now() + 2000;
      $('wake').textContent = 'Услышала «Hey Jarvis» (' + score.toFixed(2) + ')';
      if (ws && ws.readyState === 1) {
        ws.send(JSON.stringify({t: 'ptt', state: 'down', source: 'wake',
                                score: Math.round(score * 100) / 100}));
      }
    }
  } catch (e) {
    $('wake').textContent = 'Ошибка распознавания слова: ' + (e.message || e);
    wake = null;
  } finally {
    wakeBusy = false;
  }
}

function show(err) { $('err').textContent = err || ''; }

// --- киоск: часы и погода, не зависят от сокета/микрофона ---

function updateClock() {
  const now = new Date();
  $('clock').textContent = now.toLocaleTimeString('ru-RU', {hour: '2-digit', minute: '2-digit'});
  $('date').textContent = now.toLocaleDateString('ru-RU', {
    weekday: 'long', day: 'numeric', month: 'long',
  });
}
updateClock();
setInterval(updateClock, 1000);

async function updateWeather() {
  try {
    const resp = await fetch('/api/weather');
    const w = await resp.json();
    if (w.temp === undefined) return;  // сервису Open-Meteo сейчас нечего ответить
    $('weather').innerHTML =
      `<span class="icon">${w.icon}</span>${w.temp}°C ` +
      `<span class="desc">${w.description}, ${w.city}</span>`;
  } catch { /* нет сети — оставляем то, что уже показано */ }
}
updateWeather();
setInterval(updateWeather, 10 * 60 * 1000);

// Экран не должен гаснуть — это киоск на подставке, а не разовый визит.
// Best-effort: без HTTPS (или на браузере без поддержки) просто не сработает,
// остальной странице это не мешает. Снимается браузером при уходе вкладки
// в фон и не восстанавливается сам — переприобретаем по возврату.
async function requestWakeLock() {
  if (!('wakeLock' in navigator)) return;
  try { await navigator.wakeLock.request('screen'); } catch { /* не критично */ }
}
requestWakeLock();
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') requestWakeLock();
});

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
    $('volBox').hidden = false;
    show('');
    initWake();
  };

  ws.onmessage = (ev) => {
    if (typeof ev.data !== 'string') return;   // звук и картинки нам не нужны
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.t === 'state') {
      curState = m.value;
      $('state').textContent = STATES[m.value] || m.value;
      // В покое киоск — это просто часы с погодой; статус занимает место
      // на виду только пока что-то реально происходит.
      $('state').hidden = (m.value === 'idle');
      if (m.value === 'idle' || m.value === 'listening') $('text').textContent = '';
      // Слушаю — кнопка красная и подписана «Стоп»: повторный тап обрывает
      // запись раньше тишины (тот же смысл, что у тапа физической кнопки).
      const listening = m.value === 'listening';
      $('talk').textContent = listening ? '⏹ Стоп' : '🎤 Спросить';
      $('talk').classList.toggle('active', listening);
    } else if (m.t === 'text') {
      // Что колонка расслышала и что отвечает — тот же текст, что на её экране.
      $('text').textContent = m.value || '';
    } else if (m.t === 'volume' && !draggingVol) {
      // Громкость может поменять и голос, и другое устройство в той же
      // комнате — не перезаписываем ползунок, пока за него держит палец.
      setVolDisplay(Math.round(m.value * 100));
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
      const samples = new Int16Array(FRAME);
      view.setUint8(0, FRAME_MIC);
      for (let i = 0; i < FRAME; i++) {
        const v = Math.max(-1, Math.min(1, chunk[i]));
        samples[i] = v * 32767;
        view.setInt16(1 + i * 2, samples[i], true);
        peak = Math.max(peak, Math.abs(v));
      }
      ws.send(buf);
      if (wake) {
        wakeQueue.push(samples);
        // Около двух секунд запаса; больше — телефон не успевает, старое
        // уже не нужно.
        if (wakeQueue.length > 100) wakeQueue.splice(0, wakeQueue.length - 100);
      }
    }
    pumpWake();
    $('bar').style.width = Math.min(100, peak * 160) + '%';
  };

  src.connect(node);
  // Без подключения к выходу ScriptProcessor не вызывается вовсе;
  // громкость нулевая, поэтому телефон ничего не играет.
  const mute = ctx.createGain();
  mute.gain.value = 0;
  node.connect(mute);
  mute.connect(ctx.destination);
  // Wake Lock уже запрошен при загрузке страницы (см. requestWakeLock
  // выше) — киоск не должен гаснуть и до первого нажатия «Слушать».
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
  $('volBox').hidden = true;
  $('state').textContent = '—';
  $('state').hidden = true;
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

// Громкость динамика самой колонки — не звука в телефоне, тут его и нет
// (see mute-выход выше). Слать можно с любого устройства, сервер не
// разбирает, откуда команда, и рассылает новое значение всем остальным.
let draggingVol = false, volSendTimer = null;

function setVolDisplay(pct) {
  $('volSlider').value = pct;
  $('volPct').textContent = pct + '%';
}

$('volSlider').addEventListener('pointerdown', () => { draggingVol = true; });
['pointerup', 'pointercancel'].forEach(ev =>
  $('volSlider').addEventListener(ev, () => { draggingVol = false; }));

$('volSlider').addEventListener('input', () => {
  const pct = Number($('volSlider').value);
  $('volPct').textContent = pct + '%';
  // Не долбим сокет на каждый пиксель протяжки — слышно и без этого.
  clearTimeout(volSendTimer);
  volSendTimer = setTimeout(() => {
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({t: 'volume', value: pct / 100}));
    }
  }, 80);
});
</script>
</html>
"""


@router.get("/satellite", response_class=HTMLResponse)
async def satellite() -> str:
    return (
        _PAGE.replace("__TLS_PORT__", str(settings.tls_port))
        .replace("__ORT_VERSION__", _ORT_VERSION)
        .replace("__WAKE_MODEL__", settings.wakeword_model)
        .replace("__WAKE_THRESHOLD__", str(float(settings.wakeword_threshold)))
        .replace("__BASE_CSS__", webstyle.BASE_CSS)
        .replace("__NAV__", webstyle.nav("satellite"))
    )


@router.get("/wakeword/{name}")
async def wakeword_file(name: str) -> FileResponse:
    """Модели слова и сам детектор — только по белому списку имён."""
    if name == "wakeword.js":
        return FileResponse(_STATIC / "wakeword.js", media_type="text/javascript")
    allowed = {"melspectrogram.onnx", "embedding_model.onnx", settings.wakeword_model}
    path = settings.wakeword_dir / name
    if name not in allowed or not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="application/octet-stream")


@router.get("/api/weather")
async def weather() -> dict:
    """Погода для виджета киоска на /satellite. Молчаливо отдаёт то, что
    получилось (в том числе устаревший кэш при сбое Open-Meteo) — виджету
    важнее не мигать пустотой, чем быть идеально свежим."""
    global _weather_cache, _weather_cache_at
    if _weather_cache is None or time.monotonic() - _weather_cache_at > _WEATHER_TTL_S:
        fresh = await current_conditions(
            settings.default_city, settings.default_latitude, settings.default_longitude
        )
        if fresh is not None:
            _weather_cache = fresh
            _weather_cache_at = time.monotonic()
    return _weather_cache or {}
