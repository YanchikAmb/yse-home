# -*- coding: utf-8 -*-
"""Дообучение слова на живых записях дома (служба yse-train).

  wake_train.py serve               ждёт real/job.request и выполняет задания по одному
  wake_train.py start <слово>       признаки, обучение от текущих весов, выгрузка, проверка
  wake_train.py eval <слово> <файл> проверка модели на отложенных записях, печатает JSON
  wake_train.py apply <слово>       кандидат — рабочей моделью; старая уходит в models/before-*
  wake_train.py rollback <слово>    вернуть модель, что была до последнего apply

Записи копит wake_collect.py (yse-wake@). Каждая пятая запись (по имени файла) в обучение
не идёт: на ней старая и новая модели проверяются теми же правилами, что в mww_wake_listen.py.
«Слово» — сколько отложенных повторов модель поймала; «фон» — сколько раз сработала на
отложенном фоне за час; «фразы» — на скольких обычных фразах сработала.

Фон после обучения удаляется. Остаются только окна признаков (real/bg_feats.npy, 40 полос
по 10 мс) — из них разборчивую речь не восстановить, а следующему обучению они нужны.
Работающая модель меняется только по apply; yse-wake@ сами подхватывают новый файл и
кладут его на свои колонки.
"""
import hashlib
import json
import os
import pathlib
import random
import shutil
import subprocess
import sys
import time
import wave

import numpy as np

ROOT = pathlib.Path(os.environ.get("YSE_REAL", "/storage/yse-mww/real"))
TRAINER = pathlib.Path(os.environ.get("YSE_TRAINER", "/storage/yse-mww/trainer"))
APP = pathlib.Path(__file__).resolve().parent
RATE = 16000
PRE = int(RATE * 1.5)
STEPS = 3000
BATCH = 128
LEARNING_RATE = 1e-4
SYN_POS = 20000
SYN_NEG = 40000
BG_WINDOWS = 40000
BG_KEEP = 80000
BG_STRIDE = 20
TRAIN_BASE = {
    "ru_jarvis": {
        "config": "ru_jarvis_hardneg.yaml",
        "weights": "training/output/ru_jarvis/model/ru_jarvis_mww.weights.h5",
    },
}

sys.path.insert(0, str(APP))
import wake_collect  # noqa: E402

read_json = wake_collect.read_json
write_json = wake_collect.write_json
load_words = wake_collect.load_words


def log(line):
    print(time.strftime("%H:%M:%S ") + line, flush=True)


def job_update(**fields):
    job = read_json(ROOT / "job.json", {}) or {}
    job.update(fields)
    write_json(ROOT / "job.json", job)


def step(text):
    log(text)
    job = read_json(ROOT / "job.json", {}) or {}
    lines = (job.get("log") or [])[-30:] + [time.strftime("%H:%M ") + text]
    job_update(step=text, log=lines)


def held_out(path):
    return int(hashlib.md5(path.name.encode("utf-8")).hexdigest(), 16) % 5 == 0


def read_wav(path):
    with wave.open(str(path), "rb") as src:
        return np.frombuffer(src.readframes(src.getnframes()), dtype="<i2").copy()


def gather(slug):
    word = ROOT / "words" / slug
    data = {"say": sorted((word / "say").glob("*/*/*.wav")),
            "other": sorted((ROOT / "other").glob("*/*/*.wav")),
            "bg": sorted((ROOT / "bg").glob("*/*.wav")),
            "hard_yes": [], "hard_no": []}
    for meta_path in sorted((word / "hard").glob("*/*.json")):
        label = (read_json(meta_path, {}) or {}).get("label")
        wav_path = meta_path.with_suffix(".wav")
        if label in ("yes", "no") and wav_path.is_file():
            data["hard_" + label].append(wav_path)
    return data


def split(paths):
    train = [p for p in paths if not held_out(p)]
    test = [p for p in paths if held_out(p)]
    return train, test


def trim_word(audio):
    """Самый громкий кусок речи с полями 0.15 с, не длиннее 1.6 с. Тишина — None."""
    size = 160
    n = len(audio) // size
    if n < 10:
        return None
    block = audio[: n * size].astype(np.float64).reshape(n, size)
    rms = np.sqrt((block * block).mean(axis=1))
    floor = float(np.percentile(rms, 20))
    loud = rms >= 3.0 * floor + 3.0
    if not loud.any():
        return None
    runs = []
    start = None
    gap = 0
    for i, flag in enumerate(loud):
        if flag:
            if start is None:
                start = i
            gap = 0
            end = i
        elif start is not None:
            gap += 1
            if gap > 15:
                runs.append((start, end))
                start = None
    if start is not None:
        runs.append((start, end))
    best = max(runs, key=lambda r: rms[r[0]:r[1] + 1].sum())
    a = max(0, best[0] - 15)
    b = min(n, best[1] + 16)
    if b - a > 160:
        centre = best[0] + int(np.argmax(rms[best[0]:best[1] + 1]))
        a = max(0, centre - 80)
        b = min(n, a + 160)
    return audio[a * size:b * size]


def write_wav(path, audio):
    wake_collect.write_wav(path, audio)


def bg_windows(tm, paths, spec, cap):
    """Окна фона без аугментации, шаг 0.2 с. Окна у всплесков модели (там могло быть слово) — мимо."""
    out = []
    for path in paths:
        meta = read_json(path.with_suffix(".json"), {}) or {}
        hot = [float(t) for t, _peak in meta.get("hot") or []]
        frames = tm.extract_all_frames(read_wav(path))
        for start in range(0, max(1, len(frames) - spec + 1), BG_STRIDE):
            t0 = start / 100.0
            t1 = (start + spec) / 100.0
            if any(t0 - 1.0 <= t <= t1 + 1.0 for t in hot):
                continue
            out.append(tm._get_window(frames, start, spec))
    if not out:
        return np.zeros((0, spec, 40), dtype=np.float32)
    if len(out) > cap:
        out = random.sample(out, cap)
    return np.asarray(out, dtype=np.float32)


def subset(path, count):
    data = np.load(str(path), mmap_mode="r")
    if len(data) <= count:
        return np.asarray(data, dtype=np.float32)
    idx = np.sort(np.random.choice(len(data), count, replace=False))
    return np.asarray(data[idx], dtype=np.float32)


def run_start(word):
    words = load_words()
    info = words[word]
    slug = info["slug"]
    base = TRAIN_BASE.get(slug)
    if base is None:
        raise SystemExit("для слова нет первичной модели: сначала синтетическое обучение")
    os.chdir(TRAINER)
    sys.path.insert(0, str(TRAINER))
    import tensorflow as tf
    for gpu in tf.config.list_physical_devices("GPU"):
        tf.config.experimental.set_memory_growth(gpu, True)
    import train_mww as tm

    cfg = tm.load_config(str(TRAINER / base["config"]))
    spec = cfg["spectrogram_length"]
    data = gather(slug)
    say_train, _ = split(data["say"])
    yes_train, _ = split(data["hard_yes"])
    no_train, _ = split(data["hard_no"])
    other_train, _ = split(data["other"])
    bg_train, _ = split(data["bg"])
    step("записи: слово %d, фразы %d, фон %d мин, трудные да %d / нет %d"
         % (len(data["say"]), len(data["other"]), len(data["bg"]), len(data["hard_yes"]), len(data["hard_no"])))
    if len(say_train) < 20:
        raise SystemExit("мало записей слова для обучения")

    work = ROOT / "work" / slug
    shutil.rmtree(work, ignore_errors=True)
    for name in ("pos", "other", "hard", "bgmix"):
        (work / name).mkdir(parents=True)
    kept = 0
    for path in say_train:
        piece = trim_word(read_wav(path)[PRE:])
        if piece is not None:
            write_wav(work / "pos" / path.name, piece)
            kept += 1
    for path in yes_train:
        piece = trim_word(read_wav(path))
        if piece is not None:
            write_wav(work / "pos" / path.name, piece)
            kept += 1
    for path in other_train:
        shutil.copy2(path, work / "other" / path.name)
    for path in no_train:
        shutil.copy2(path, work / "hard" / path.name)
    for path in sorted((TRAINER / "training" / "background").rglob("*.wav")):
        shutil.copy2(path, work / "bgmix" / ("base_" + path.name))
    for path in bg_train:
        os.link(path, work / "bgmix" / (path.parent.name + "_" + path.name))
    step("признаки: слово %d кусков" % kept)

    tm.configure_channel_augmentation({"augmentation": {
        "enabled": True, "rir_dir": "training/mit_rirs/16khz", "background_dir": str(work / "bgmix"),
        "rir_probability": 0.15, "background_probability": 0.7, "background_snr_db": [0, 25],
    }})
    stride = max(1, spec // 12)
    real_pos = tm.extract_features_from_dir(work / "pos", "real_pos", spec,
                                            augment_stride=stride, noise_augments=6)
    groups = []
    if any((work / "other").glob("*.wav")):
        groups.append(("other", tm.extract_features_from_dir(work / "other", "other", spec,
                                                             augment_stride=stride, noise_augments=2), 0.25))
    if any((work / "hard").glob("*.wav")):
        groups.append(("hard", tm.extract_features_from_dir(work / "hard", "hard", spec,
                                                            augment_stride=max(1, stride // 2), noise_augments=4), 0.3))
    bg_new = bg_windows(tm, bg_train, spec, BG_WINDOWS)
    cache = ROOT / "bg_feats.npy"
    bg_old = np.load(str(cache)) if cache.is_file() else np.zeros((0, spec, 40), dtype=np.float32)
    bg_all = np.concatenate([bg_old, bg_new]) if len(bg_old) else bg_new
    if len(bg_all):
        groups.append(("bg", bg_all, 0.45))
    syn_dir = TRAINER / "training" / "output" / slug / "mww_features"
    syn_pos = subset(syn_dir / "positive_train.npy", SYN_POS)
    syn_neg = subset(syn_dir / "negative_train.npy", SYN_NEG)
    step("признаки готовы: слово %d окон, %s, синтетика %d/%d"
         % (len(real_pos), ", ".join("%s %d" % (name, len(arr)) for name, arr, _w in groups),
            len(syn_pos), len(syn_neg)))

    weights = info.get("weights") or str(TRAINER / base["weights"])
    model = tm._build_model(cfg)
    model.load_weights(weights)
    model.compile(optimizer=tf.keras.optimizers.Adam(LEARNING_RATE), loss="binary_crossentropy",
                  metrics=["accuracy"])
    share = sum(w for _n, _a, w in groups) or 1.0
    probs = [w / share for _n, _a, w in groups]

    def sample_gen():
        rng = np.random.default_rng()
        while True:
            if rng.random() < 0.5 or not len(real_pos):
                yield syn_pos[rng.integers(len(syn_pos))], np.float32(1.0)
            else:
                yield real_pos[rng.integers(len(real_pos))], np.float32(1.0)
            if rng.random() < 0.5 or not groups:
                yield syn_neg[rng.integers(len(syn_neg))], np.float32(0.0)
            else:
                arr = groups[rng.choice(len(groups), p=probs)][1]
                yield arr[rng.integers(len(arr))], np.float32(0.0)

    spec_augment = tm._make_spec_augment_fn(spec, cfg)
    ds = (tf.data.Dataset.from_generator(sample_gen, output_signature=(
        tf.TensorSpec(shape=(spec, 40), dtype=tf.float32), tf.TensorSpec(shape=(), dtype=tf.float32)))
        .map(spec_augment, num_parallel_calls=tf.data.AUTOTUNE).batch(BATCH).prefetch(tf.data.AUTOTUNE))
    epochs = 10
    per_epoch = STEPS // epochs
    step("обучение: %d шагов" % STEPS)

    def progress(epoch, logs):
        job_update(progress=round((epoch + 1) / epochs, 2), loss=round(float(logs.get("loss", 0)), 4))

    model.fit(ds, steps_per_epoch=per_epoch, epochs=epochs, verbose=2,
              callbacks=[tf.keras.callbacks.LambdaCallback(on_epoch_end=progress)])

    ft = slug + "_ft"
    out = TRAINER / "training" / "output" / ft
    (out / "model").mkdir(parents=True, exist_ok=True)
    (out / "mww_features").mkdir(parents=True, exist_ok=True)
    link = out / "mww_features" / "positive_train.npy"
    if not link.exists():
        link.symlink_to(syn_dir / "positive_train.npy")
    (out / "model" / (ft + "_mww.keras")).unlink(missing_ok=True)
    model.save_weights(str(out / "model" / (ft + "_mww.weights.h5")))
    step("выгрузка модели")
    tm.phase_export(dict(cfg, model_name=ft))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    models = ROOT / "words" / slug / "models"
    models.mkdir(parents=True, exist_ok=True)
    candidate = models / (stamp + ".tflite")
    shutil.copy2(out / (ft + "_mww.tflite"), candidate)
    cand_weights = models / (stamp + ".weights.h5")
    model.save_weights(str(cand_weights))

    step("проверка старой и новой модели")
    old = evaluate_sub(word, info["model"])
    new = evaluate_sub(word, str(candidate))
    better = (new["say_hit"] >= old["say_hit"] and new["bg_fires"] <= old["bg_fires"]
              and new["other_fires"] + new["hard_fires"] <= old["other_fires"] + old["hard_fires"])
    if len(bg_new):
        keep = np.concatenate([bg_old, bg_new]) if len(bg_old) else bg_new
        if len(keep) > BG_KEEP:
            keep = keep[np.random.choice(len(keep), BG_KEEP, replace=False)]
        np.save(str(ROOT / "bg_feats.tmp.npy"), keep)
        os.replace(ROOT / "bg_feats.tmp.npy", cache)
    for path in data["bg"]:
        path.unlink(missing_ok=True)
        path.with_suffix(".json").unlink(missing_ok=True)
    shutil.rmtree(work, ignore_errors=True)
    job_update(result={"candidate": str(candidate), "weights": str(cand_weights), "old": old, "new": new,
                       "better": better, "at": time.time()})
    step("готово: новая модель %s" % ("лучше" if better else "не лучше старой"))


def evaluate_sub(word, model):
    env = dict(os.environ, CUDA_VISIBLE_DEVICES="", PYTHONIOENCODING="utf-8")
    slug = load_words()[word]["slug"]
    proc = subprocess.run([sys.executable, str(pathlib.Path(__file__).resolve()), "eval", slug, model],
                          capture_output=True, text=True, env=env, timeout=3600)
    lines = [line for line in proc.stdout.splitlines() if line.startswith("{")]
    if proc.returncode or not lines:
        raise SystemExit("проверка упала: %s" % (proc.stderr.strip().splitlines() or ["?"])[-1])
    return json.loads(lines[-1])


def run_eval(word, model):
    info = load_words()[word]
    import mww_wake_listen as mw
    mw.CUTOFF = float(info.get("cutoff", 0.85))
    ear = mw.Ear(model)
    data = gather(info["slug"])

    def fired(audio):
        ear.reset()
        return any(isinstance(item[0], float) for item in ear.feed(audio))

    _, say_test = split(data["say"])
    _, yes_test = split(data["hard_yes"])
    _, no_test = split(data["hard_no"])
    _, other_test = split(data["other"])
    _, bg_test = split(data["bg"])
    say = say_test + yes_test
    result = {
        "say_total": len(say), "say_hit": sum(1 for p in say if fired(read_wav(p))),
        "other_total": len(other_test), "other_fires": sum(1 for p in other_test if fired(read_wav(p))),
        "hard_total": len(no_test), "hard_fires": sum(1 for p in no_test if fired(read_wav(p))),
    }
    fires = 0
    samples = 0
    rooms = {}
    for path in bg_test:
        rooms.setdefault(path.parent.name, []).append(path)
    for paths in rooms.values():
        ear.reset()
        for path in sorted(paths):
            audio = read_wav(path)
            samples += len(audio)
            fires += sum(1 for item in ear.feed(audio) if isinstance(item[0], float))
    result["bg_hours"] = round(samples / RATE / 3600.0, 2)
    result["bg_fires"] = fires
    print(json.dumps(result), flush=True)


def run_apply(word):
    words = load_words()
    info = words[word]
    job = read_json(ROOT / "job.json", {}) or {}
    result = job.get("result") or {}
    candidate = result.get("candidate")
    if not candidate or not os.path.isfile(candidate):
        raise SystemExit("нечего применять")
    models = ROOT / "words" / info["slug"] / "models"
    models.mkdir(parents=True, exist_ok=True)
    backup = models / ("before-%s.tflite" % time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(info["model"], backup)
    prev_weights = info.get("weights") or str(TRAINER / TRAIN_BASE[info["slug"]]["weights"])
    tmp = info["model"] + ".new"
    shutil.copy2(candidate, tmp)
    os.replace(tmp, info["model"])
    info["weights"] = result.get("weights") or prev_weights
    history = info.get("history") or []
    history.append({"at": time.time(), "model": candidate, "backup": str(backup), "prev_weights": prev_weights})
    info["history"] = history
    write_json(ROOT / "words.json", words)
    step("новая модель работает, колонки получат её в течение минуты")


def run_rollback(word):
    words = load_words()
    info = words[word]
    history = info.get("history") or []
    if not history:
        raise SystemExit("возвращать нечего")
    last = history.pop()
    tmp = info["model"] + ".new"
    shutil.copy2(last["backup"], tmp)
    os.replace(tmp, info["model"])
    info["weights"] = last.get("prev_weights")
    info["history"] = history
    write_json(ROOT / "words.json", words)
    step("вернули прошлую модель")


def serve():
    ROOT.mkdir(parents=True, exist_ok=True)
    job = read_json(ROOT / "job.json", {}) or {}
    if job.get("state") == "running":
        job_update(state="failed", error="прервано перезапуском службы", finished=time.time())
    log("yse-train ready")
    while True:
        req_path = ROOT / "job.request"
        req = read_json(req_path, None) if req_path.exists() else None
        if req_path.exists():
            req_path.unlink(missing_ok=True)
        if not isinstance(req, dict) or req.get("action") not in ("start", "apply", "rollback"):
            time.sleep(3)
            continue
        action, word = req["action"], req.get("word", "")
        prev = read_json(ROOT / "job.json", {}) or {}
        job = {"state": "running", "action": action, "word": word, "started": time.time(),
               "step": "старт", "log": [], "progress": 0}
        if action != "start" and prev.get("word") == word:
            job["result"] = prev.get("result")
        write_json(ROOT / "job.json", job)
        log("job %s %s" % (action, word))
        slug = (load_words().get(word) or {}).get("slug", "")
        with open(ROOT / "job.log", "w", encoding="utf-8") as out:
            proc = subprocess.run([sys.executable, "-u", str(pathlib.Path(__file__).resolve()), action, slug],
                                  cwd=str(TRAINER), stdout=out, stderr=subprocess.STDOUT,
                                  env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        job = read_json(ROOT / "job.json", {}) or {}
        if proc.returncode:
            tail = (ROOT / "job.log").read_text(encoding="utf-8", errors="replace").strip().splitlines()
            job.update(state="failed", error=(tail or ["?"])[-1][-300:])
        else:
            job.update(state="done")
        job["finished"] = time.time()
        write_json(ROOT / "job.json", job)
        log("job %s %s %s" % (action, word, job["state"]))


def word_of(name):
    """Слово по самому слову или по slug: в командной строке кириллица может не пережить локаль."""
    words = load_words()
    if name in words:
        return name
    for word, info in words.items():
        if info.get("slug") == name:
            return word
    raise SystemExit("нет такого слова")


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    cmd = sys.argv[1]
    if cmd == "serve":
        serve()
        return 0
    word = word_of(sys.argv[2] if len(sys.argv) > 2 else "")
    if cmd == "start":
        run_start(word)
    elif cmd == "eval":
        run_eval(word, sys.argv[3])
    elif cmd == "apply":
        run_apply(word)
    elif cmd == "rollback":
        run_rollback(word)
    else:
        raise SystemExit(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
