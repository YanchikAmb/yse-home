/*
 * Звук первичной настройки Станции: телефон проигрывает его рядом с микрофонами,
 * колонка в режиме ожидания забирает имя сети и пароль. Пароль в этот файл
 * и на сервер не записывается — кодирует сам браузер.
 *
 * Формат, который слушает прошивка в режиме настройки (описание приложения «Дом»):
 * чистый синус 40 мс, 16000 Гц, без огибающей.
 * Метки: 6192 6192 5120 5120, затем версия 0 двумя тонами 1024,
 * затем полубайты как 1024 + значение * 256, в конце восемь тонов 5632.
 * Видимая сеть: байт режима 3, хеш имени как у Java String.hashCode по модулю 32767
 * (младший байт первый), длина и пароль, длина и код привязки.
 * Дальше младшие 16 бит CRC-32 и код Рида-Соломона над GF(16):
 * блоки до 11 полубайт и 4 полубайта чётности. Короткий хвост не дополняется.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.YseSound = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var SAMPLE_RATE = 16000;
  var TONE_SEC = 0.04;
  var START = [6192, 6192, 5120, 5120];
  var VERSION = [1024, 1024];
  var END = [5632, 5632, 5632, 5632, 5632, 5632, 5632, 5632];
  var GF = galois();

  function encode(config) {
    config = config || {};
    var ssid = String(config.ssid == null ? "" : config.ssid);
    var password = String(config.password == null ? "" : config.password);
    var token = String(config.token == null ? "" : config.token);
    var hidden = Boolean(config.hidden);
    var volume = config.volume == null ? 1 : Number(config.volume);
    if (!ssid) throw new Error("Нужно имя сети");
    if (password.length < 8 || password.length > 63) throw new Error("Пароль сети — от 8 до 63 символов");
    if (!Number.isFinite(volume) || volume <= 0 || volume > 1) throw new Error("Громкость от 0 до 1");
    var body = hidden ? hiddenBody(ssid, password, token) : visibleBody(ssid, password, token);
    var payload = crc16(body);
    var symbols = reedSolomon(payload);
    var tones = START.concat(VERSION, tonesOf(symbols), END);
    var samples = render(tones, volume);
    return {
      wav: wavBlob(samples),
      samples: samples,
      tones: tones,
      payload: payload,
      sampleRate: SAMPLE_RATE,
      duration: samples.length / SAMPLE_RATE
    };
  }

  function visibleBody(ssid, password, token) {
    var hash = javaHash(ssid) % 32767;
    var out = [3, hash & 255, (hash >> 8) & 255];
    pushText(out, password);
    pushText(out, token);
    return out;
  }

  function hiddenBody(ssid, password, token) {
    var out = [2];
    pushText(out, ssid);
    pushText(out, password);
    pushText(out, token);
    return out;
  }

  function pushText(out, value) {
    var bytes = new TextEncoder().encode(value);
    if (bytes.length > 255) throw new Error("Слишком длинное поле");
    out.push(bytes.length);
    for (var i = 0; i < bytes.length; i++) out.push(bytes[i]);
  }

  function javaHash(value) {
    var hash = 0;
    for (var i = 0; i < value.length; i++) hash = (Math.imul(hash, 31) + value.charCodeAt(i)) | 0;
    return hash;
  }

  function crc16(bytes) {
    var crc = 0xffffffff;
    for (var i = 0; i < bytes.length; i++) {
      crc ^= bytes[i];
      for (var bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1));
    }
    crc = (crc ^ 0xffffffff) >>> 0;
    return bytes.concat([crc & 255, (crc >>> 8) & 255]);
  }

  function reedSolomon(bytes) {
    var symbols = [];
    for (var i = 0; i < bytes.length; i++) symbols.push(bytes[i] >>> 4, bytes[i] & 15);
    var out = [];
    for (var offset = 0; offset < symbols.length; offset += 11) {
      var block = symbols.slice(offset, offset + 11);
      out = out.concat(block, parity(block, 4));
    }
    return out;
  }

  function parity(data, degree) {
    var generator = rsGenerator(degree);
    var acc = [];
    var i;
    for (i = 0; i < degree; i++) acc.push(0);
    for (i = 0; i < data.length; i++) {
      var factor = data[i] ^ acc[0];
      acc.shift();
      acc.push(0);
      for (var k = 0; k < degree; k++) acc[k] ^= GF.mul(generator[k], factor);
    }
    return acc;
  }

  function rsGenerator(degree) {
    var poly = [1];
    for (var i = 0; i < degree; i++) poly = mulPoly(poly, [1, GF.exp(i + 1)]);
    return poly.slice(1);
  }

  function mulPoly(left, right) {
    var out = [];
    var n = left.length + right.length - 1;
    var i;
    for (i = 0; i < n; i++) out.push(0);
    for (i = 0; i < left.length; i++) {
      for (var j = 0; j < right.length; j++) out[i + j] ^= GF.mul(left[i], right[j]);
    }
    return out;
  }

  function galois() {
    var exp = [];
    var log = [];
    var i;
    var value = 1;
    for (i = 0; i < 30; i++) exp.push(0);
    for (i = 0; i < 16; i++) log.push(0);
    for (i = 0; i < 15; i++) {
      exp[i] = value;
      log[value] = i;
      value <<= 1;
      if (value >= 16) value = (value ^ 0x13) & 15;
    }
    for (i = 15; i < exp.length; i++) exp[i] = exp[i - 15];
    return {
      exp: function (power) { return exp[power % 15]; },
      mul: function (left, right) {
        if (left === 0 || right === 0) return 0;
        return exp[(log[left] + log[right]) % 15];
      }
    };
  }

  function tonesOf(symbols) {
    var tones = [];
    for (var i = 0; i < symbols.length; i++) tones.push(1024 + symbols[i] * 256);
    return tones;
  }

  function render(tones, volume) {
    var per = Math.round(SAMPLE_RATE * TONE_SEC);
    var samples = new Float32Array(tones.length * per);
    var amp = Math.min(1, volume) * 0.95;
    var cursor = 0;
    for (var t = 0; t < tones.length; t++) {
      var freq = tones[t];
      for (var i = 0; i < per; i++) {
        samples[cursor++] = Math.sin((2 * Math.PI * freq * i) / SAMPLE_RATE) * amp;
      }
    }
    return samples;
  }

  function wavBlob(samples) {
    var buffer = new ArrayBuffer(44 + samples.length * 2);
    var view = new DataView(buffer);
    function text(offset, value) {
      for (var i = 0; i < value.length; i++) view.setUint8(offset + i, value.charCodeAt(i));
    }
    text(0, "RIFF");
    view.setUint32(4, 36 + samples.length * 2, true);
    text(8, "WAVE");
    text(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, SAMPLE_RATE, true);
    view.setUint32(28, SAMPLE_RATE * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    text(36, "data");
    view.setUint32(40, samples.length * 2, true);
    for (var i = 0; i < samples.length; i++) {
      var s = samples[i];
      if (s > 1) s = 1;
      if (s < -1) s = -1;
      view.setInt16(44 + i * 2, s * 32767, true);
    }
    return new Blob([buffer], { type: "audio/wav" });
  }

  function selfCheck() {
    var result = encode({ ssid: "test", password: "012345678", token: "", volume: 1 });
    var head = result.tones.slice(0, 6).join(",");
    var tail = result.tones.slice(-8).join(",");
    if (head !== "6192,6192,5120,5120,1024,1024") throw new Error("head " + head);
    if (tail !== "5632,5632,5632,5632,5632,5632,5632,5632") throw new Error("tail");
    if (result.payload[0] !== 3 || result.payload[1] !== 254 || result.payload[2] !== 68) {
      throw new Error("hash");
    }
    if (result.payload[3] !== 9) throw new Error("password length");
    if (result.samples.length !== result.tones.length * 640) throw new Error("length");
    return result.tones.length;
  }

  return { encode: encode, selfCheck: selfCheck, sampleRate: SAMPLE_RATE };
});
