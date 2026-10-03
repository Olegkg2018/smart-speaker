// Активационное слово прямо в браузере сателлита — openWakeWord
// (github.com/dscripka/openWakeWord) поверх onnxruntime-web.
//
// Конвейер повторяет openwakeword/utils.py::AudioFeatures:
//   16 кГц int16 -> куски по 1280 сэмплов (80 мс) -> melspectrogram.onnx
//   (кусок + 480 сэмплов перекрытия, выход x/10 + 2, по 32 полосы) ->
//   embedding_model.onnx на последних 76 мел-кадрах (вход [1,76,32,1],
//   выход 96 чисел) -> классификатор слова на последних 16 эмбеддингах
//   (вход [1,16,96], выход — вероятность).
//
// Файл один и тот же для страницы /satellite и для проверки в Node.

class WakeWord {
  static CHUNK = 1280;
  static OVERLAP = 160 * 3;
  static MEL_WINDOW = 76;
  static MEL_BINS = 32;
  static EMB_FRAMES = 16;

  constructor(ort, mel, emb, cls) {
    this.ort = ort;
    this.mel = mel;
    this.emb = emb;
    this.cls = cls;
    this.pending = [];  // сэмплы, ещё не собранные в кусок
    this.tail = new Float32Array(WakeWord.OVERLAP);
    // Как в openWakeWord: стартовый буфер мел-кадров — единицы, чтобы
    // первое окно в 76 кадров было полным с самого начала.
    this.melFrames = Array.from({length: WakeWord.MEL_WINDOW},
                                () => new Float32Array(WakeWord.MEL_BINS).fill(1));
    this.embeddings = [];
  }

  static async load(ort, baseUrl, wordFile) {
    const opts = {executionProviders: ['wasm']};
    const [mel, emb, cls] = await Promise.all([
      ort.InferenceSession.create(baseUrl + 'melspectrogram.onnx', opts),
      ort.InferenceSession.create(baseUrl + 'embedding_model.onnx', opts),
      ort.InferenceSession.create(baseUrl + wordFile, opts),
    ]);
    return new WakeWord(ort, mel, emb, cls);
  }

  async _run(session, data, dims) {
    const feeds = {[session.inputNames[0]]: new this.ort.Tensor('float32', data, dims)};
    const out = await session.run(feeds);
    return out[session.outputNames[0]].data;
  }

  // Принимает int16-сэмплы 16 кГц; возвращает вероятность слова для
  // последнего полного куска или null, если кусок ещё не набрался или
  // эмбеддингов пока мало для оценки.
  async push(samples) {
    for (let i = 0; i < samples.length; i++) this.pending.push(samples[i]);
    let score = null;
    while (this.pending.length >= WakeWord.CHUNK) {
      const chunk = this.pending.splice(0, WakeWord.CHUNK);
      score = await this._step(chunk);
    }
    return score;
  }

  async _step(chunk) {
    // Модель ждёт значения int16 как есть (не нормированные в -1..1).
    const input = new Float32Array(WakeWord.OVERLAP + WakeWord.CHUNK);
    input.set(this.tail, 0);
    for (let i = 0; i < chunk.length; i++) input[WakeWord.OVERLAP + i] = chunk[i];
    this.tail = input.slice(input.length - WakeWord.OVERLAP);

    const spec = await this._run(this.mel, input, [1, input.length]);
    for (let f = 0; f + WakeWord.MEL_BINS <= spec.length; f += WakeWord.MEL_BINS) {
      const row = new Float32Array(WakeWord.MEL_BINS);
      for (let b = 0; b < WakeWord.MEL_BINS; b++) row[b] = spec[f + b] / 10 + 2;
      this.melFrames.push(row);
    }
    if (this.melFrames.length > WakeWord.MEL_WINDOW) {
      this.melFrames.splice(0, this.melFrames.length - WakeWord.MEL_WINDOW);
    }

    const window = new Float32Array(WakeWord.MEL_WINDOW * WakeWord.MEL_BINS);
    this.melFrames.forEach((row, i) => window.set(row, i * WakeWord.MEL_BINS));
    const embedding = await this._run(
      this.emb, window, [1, WakeWord.MEL_WINDOW, WakeWord.MEL_BINS, 1]);
    this.embeddings.push(Float32Array.from(embedding));
    if (this.embeddings.length > WakeWord.EMB_FRAMES) this.embeddings.shift();
    if (this.embeddings.length < WakeWord.EMB_FRAMES) return null;

    const feats = new Float32Array(WakeWord.EMB_FRAMES * 96);
    this.embeddings.forEach((e, i) => feats.set(e, i * 96));
    const prob = await this._run(this.cls, feats, [1, WakeWord.EMB_FRAMES, 96]);
    return prob[0];
  }
}

if (typeof module !== 'undefined') module.exports = {WakeWord};
