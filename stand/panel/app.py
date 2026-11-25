# -*- coding: utf-8 -*-
"""Веб-панель yse-stand: OTA pause/resume, no_lock, статус прошивки, хвост логов."""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RUN = Path(os.environ.get("STAND_RUN", "/stand/run"))
FIRMWARE = Path(os.environ.get("STAND_FIRMWARE", "/stand/firmware"))
LOG_DIR = Path(os.environ.get("STAND_LOG", "/stand/log"))
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
CONTAINER = os.environ.get("STAND_CONTAINER", "yse-stand")
QUASAR = os.environ.get("STAND_QUASAR_URL", "http://127.0.0.1").rstrip("/")
PORT = int(os.environ.get("PANEL_PORT", "8090"))
CHECK_QS = os.environ.get(
    "STAND_CHECK_UPDATES_QS",
    "device_id=test&version=1.56.1.32.929922494.20210326.22&platform=yandexstation_2&revision=rev2",
)
POLICY_FILE = RUN / "quasar-policy.json"
TAB_ORDER = ("yandexstation_2", "yandexmini_2", "yandexmidi")

PLATFORM_DEFAULTS: dict[str, dict] = {
    "yandexstation_2": {
        "title": "Макс",
        "log_tokens": ["yandexstation_2", "SYNC-PLAIN", "REGISTER", "SYNC-REAL"],
        "check_version": "1.56.1.32.929922494.20210326.22",
        "adbd_enabled": True,
        "adbd_network": None,
        "tv_adb_enabled": True,
        "register_honest": True,
        "bind_plain_sync": True,
        "sync_local_only": False,
    },
    "yandexmini_2": {
        "title": "Мини 2",
        "log_tokens": ["yandexmini_2", "yandexmini", "LOCK"],
        "check_version": "0.369.1.46.4778262943.20261013.299",
        "adbd_enabled": True,
        "adbd_network": None,
        "tv_adb_enabled": True,
        "register_honest": False,
        "bind_plain_sync": False,
        "sync_local_only": False,
    },
    "yandexmidi": {
        "title": "Станция 2",
        "log_tokens": ["yandexmidi", "yandexmicro"],
        "check_version": "0.0.0",
        "adbd_enabled": True,
        "adbd_network": None,
        "tv_adb_enabled": True,
        "register_honest": False,
        "bind_plain_sync": False,
        "sync_local_only": False,
    },
}

PLATFORM_PRESETS: dict[str, dict[str, dict]] = {
    "yandexstation_2": {
        "our_firmware": {
            "patch": {
                "adbd_enabled": True,
                "tv_adb_enabled": True,
                "bind_plain_sync": False,
                "register_honest": False,
                "sync_local_only": False,
            },
            "ota_resume": True,
            "lock_disable": True,
        },
        "bind_home": {
            "patch": {
                "adbd_enabled": False,
                "tv_adb_enabled": False,
                "bind_plain_sync": True,
                "register_honest": True,
                "sync_local_only": False,
            },
            "ota_resume": False,
            "lock_disable": False,
        },
    },
}


def apply_platform_preset(platform: str, preset: str) -> dict:
    plats = PLATFORM_PRESETS.get(platform) or {}
    cfg = plats.get(preset)
    if not cfg:
        raise KeyError(f"unknown preset: {platform}/{preset}")
    root = patch_platform(platform, dict(cfg.get("patch") or {}))
    root["ui_active"] = platform
    write_root(root)
    if cfg.get("ota_resume"):
        (RUN / "no_downgrade").unlink(missing_ok=True)
    if cfg.get("lock_disable"):
        (RUN / "no_lock").touch()
    return {
        "ok": True,
        "platform": platform,
        "preset": preset,
        "ota_paused": flag(RUN / "no_downgrade"),
        "lock_disabled": flag(RUN / "no_lock"),
        "config": read_root()["platforms"].get(platform, {}),
    }


def _default_root() -> dict:
    import copy

    return {
        "ui_active": "yandexstation_2",
        "platforms": copy.deepcopy(PLATFORM_DEFAULTS),
    }


def read_root() -> dict:
    root = _default_root()
    raw = read_json(POLICY_FILE)
    if not isinstance(raw, dict):
        return root
    if raw.get("platforms"):
        for pid, cfg in raw["platforms"].items():
            if pid in root["platforms"] and isinstance(cfg, dict):
                root["platforms"][pid].update(cfg)
    else:
        probe = raw.get("ota_probe_platform") or "yandexstation_2"
        if probe in root["platforms"]:
            p = root["platforms"][probe]
            for key in (
                "adbd_enabled",
                "adbd_network",
                "tv_adb_enabled",
                "sync_local_only",
            ):
                if key in raw:
                    p[key] = raw[key]
    root["ui_active"] = raw.get("ui_active") or root["ui_active"]
    return root


def write_root(root: dict) -> dict:
    RUN.mkdir(parents=True, exist_ok=True)
    POLICY_FILE.write_text(
        json.dumps(root, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return root


def patch_platform(platform: str, patch: dict) -> dict:
    root = read_root()
    if platform not in root["platforms"]:
        raise KeyError(platform)
    root["platforms"][platform].update(patch)
    return write_root(root)


def read_columns() -> list:
    import columns
    return columns.for_page(columns.read_columns())


def probe_column(ip: str) -> dict:
    for folder in ("/opt/yse-lock",):
        if os.path.isdir(folder) and folder not in sys.path:
            sys.path.insert(0, folder)
    import lock_speaker
    row = lock_speaker.probe((ip or "").strip())
    return {"ok": True, "column": row}


def confirm_column(ip: str) -> dict:
    for folder in ("/opt/yse-lock",):
        if os.path.isdir(folder) and folder not in sys.path:
            sys.path.insert(0, folder)
    import columns
    row = columns.confirm((ip or "").strip())
    return {"ok": True, "column": row}


def convert_column(ip: str) -> dict:
    for folder in ("/opt/yse-lock",):
        if os.path.isdir(folder) and folder not in sys.path:
            sys.path.insert(0, folder)
    import lock_speaker
    row = lock_speaker.request((ip or "").strip())
    return {"ok": True, "column": row}


def flag(path: Path) -> bool:
    return path.is_file()


def read_json(path: Path) -> dict | list | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def manifest_for(platform: str) -> dict | None:
    for p in (
        FIRMWARE / platform / "manifest.json",
        FIRMWARE / "manifest.json",
    ):
        if p.is_file():
            m = read_json(p)
            if isinstance(m, dict) and (not m.get("platform") or m.get("platform") == platform):
                return m
    return None


def platform_log_filter(platform: str) -> list[str]:
    cfg = read_root()["platforms"].get(platform, {})
    return list(cfg.get("log_tokens") or [platform])


def _recent_client_ips(platform: str, tail: int = 600) -> list[str]:
    """IP из хвоста логов, где уже есть признаки этой платформы (без ручного ввода)."""
    tokens = platform_log_filter(platform)
    found: set[str] = set()
    ip_re = re.compile(
        r"\[(\d{1,3}(?:\.\d{1,3}){3})\]|(?:от |from )(\d{1,3}(?:\.\d{1,3}){3})",
        re.I,
    )
    for name in ("local.log", "yandex.log"):
        p = LOG_DIR / name
        if not p.is_file():
            continue
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for ln in lines[-tail:]:
            if not any(t in ln for t in tokens):
                continue
            for m in ip_re.finditer(ln):
                found.add(m.group(1) or m.group(2))
    return sorted(found)


def file_logs_platform(platform: str, tail_lines: int = 200) -> str:
    needles = platform_log_filter(platform)
    parts: list[str] = []
    for name in ("local.log", "yandex.log"):
        p = LOG_DIR / name
        if not p.is_file():
            continue
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            chunk = [ln for ln in lines[-tail_lines:] if any(n in ln for n in needles)]
            parts.append(f"--- {name} ({len(chunk)} строк, {platform}) ---")
            parts.extend(chunk[-tail_lines:])
        except OSError as e:
            parts.append(f"({name}: {e})")
    if not parts:
        auto = _recent_client_ips(platform)
        hint = f" (видели IP: {', '.join(auto)})" if auto else ""
        return f"(пока нет строк для {platform}{hint})"
    return "\n".join(parts)[-80000:]


def file_logs(tail_lines: int = 200, grep: str = "") -> str:
    """Быстрее и надёжнее, чем docker logs (там огромный журнал → timeout)."""
    parts: list[str] = []
    for name in ("local.log", "yandex.log"):
        p = LOG_DIR / name
        if not p.is_file():
            parts.append(f"({name}: нет файла в {LOG_DIR})")
            continue
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            chunk = lines[-tail_lines:]
            if grep:
                chunk = [ln for ln in chunk if grep.upper() in ln.upper() or grep in ln]
            parts.append(f"--- {name} (последние {len(chunk)} строк) ---\n" + "\n".join(chunk))
        except OSError as e:
            parts.append(f"({name}: {e})")
    if not any("---" in p for p in parts):
        parts.append(
            "Файлы local.log / yandex.log пустые или каталог log не смонтирован в панель."
        )
    return "\n\n".join(parts)[-80000:]


def _docker_api_version() -> str:
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(4)
        sock.connect(DOCKER_SOCK)
    except OSError:
        return "1.41"
    req = "GET /version HTTP/1.1\r\nHost: localhost\r\n\r\n"
    sock.sendall(req.encode())
    raw = sock.recv(4096)
    sock.close()
    if b"\r\n\r\n" in raw:
        body = raw.split(b"\r\n\r\n", 1)[1]
    else:
        body = raw
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
        api = str(data.get("ApiVersion") or "1.41")
        return api.split(".")[0] + "." + (api.split(".")[1] if "." in api else "41")
    except (json.JSONDecodeError, IndexError, ValueError):
        return "1.41"


def _decode_docker_log_body(body: bytes) -> str:
    if not body:
        return ""
    # multiplexed: 8-byte header per chunk (stream type + size)
    if len(body) > 8 and body[0] in (0, 1, 2):
        lines: list[str] = []
        i = 0
        while i + 8 <= len(body):
            ln = int.from_bytes(body[i + 4 : i + 8], "big")
            i += 8
            if ln <= 0 or i + ln > len(body):
                break
            lines.append(body[i : i + ln].decode("utf-8", errors="replace"))
            i += ln
        if lines:
            return "".join(lines)
    return body.decode("utf-8", errors="replace")


def docker_logs(tail: int = 80, grep: str = "") -> str:
    """Сначала файлы log/ (быстро); docker.sock — короткий timeout, иначе панель «зависает»."""
    files = file_logs(tail, grep)
    if not os.path.exists(DOCKER_SOCK):
        return files
    try:
        api = _docker_api_version()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(4)
        sock.connect(DOCKER_SOCK)
        q = urllib.parse.urlencode(
            {"stdout": 1, "stderr": 1, "tail": max(tail, 20), "timestamps": 0}
        )
        req = (
            f"GET /v{api}/containers/{urllib.parse.quote(CONTAINER)}/logs?{q} HTTP/1.1\r\n"
            f"Host: localhost\r\n\r\n"
        )
        sock.sendall(req.encode())
        chunks = []
        while True:
            buf = sock.recv(65536)
            if not buf:
                break
            chunks.append(buf)
        sock.close()
        raw = b"".join(chunks)
        if raw.startswith(b"HTTP/"):
            if b"\r\n\r\n" not in raw:
                return f"(docker API: нет тела ответа)\n\n" + file_logs(tail, grep)
            head, body = raw.split(b"\r\n\r\n", 1)
            if b" 404 " in head or b" 500 " in head:
                return head.decode("utf-8", errors="replace") + "\n\n" + file_logs(tail, grep)
        else:
            body = raw
        text = _decode_docker_log_body(body)
        if grep:
            text = "\n".join(ln for ln in text.splitlines() if grep in ln)
        if not text.strip():
            return files
        if grep:
            text = "\n".join(ln for ln in text.splitlines() if grep in ln)
        combined = files + "\n\n--- docker ---\n" + text
        return combined[-80000:]
    except OSError as e:
        return files + f"\n\n(docker API: {e})\n"


def check_updates_for(platform: str) -> str:
    cfg = read_root()["platforms"].get(platform, {})
    q = {
        "device_id": "test",
        "version": cfg.get("check_version") or "0.0.0",
        "platform": platform,
        "revision": "rev2",
    }
    url = f"{QUASAR}/{platform}/ota/check_updates?{urllib.parse.urlencode(q)}"
    ver = q["version"]
    ua = f"{platform}/{ver}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", errors="replace")[:2000]
    except urllib.error.URLError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def status_dict() -> dict:
    root = read_root()
    platforms: dict = {}
    for pid in TAB_ORDER:
        cfg = root["platforms"].get(pid, {})
        m = manifest_for(pid) or {}
        platforms[pid] = {
            "title": cfg.get("title", pid),
            "config": cfg,
            "seen_ips": _recent_client_ips(pid),
            "firmware_version": m.get("version"),
            "firmware_size": m.get("size"),
            "check_updates": check_updates_for(pid),
        }
    return {
        "ota_paused": flag(RUN / "no_downgrade"),
        "lock_disabled": flag(RUN / "no_lock"),
        "ui_active": root.get("ui_active"),
        "platforms": platforms,
        "lock_state": read_json(RUN / "lock-state.json"),
        "columns": read_columns(),
    }


PAGE = """<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>yse-stand</title>
<style>
  body{font-family:system-ui,sans-serif;margin:1rem 1.5rem;background:#0f1115;color:#e6e8ec}
  h1{font-size:1.35rem} .row{display:flex;flex-wrap:wrap;gap:.5rem;margin:.6rem 0;align-items:center}
  button{padding:.45rem .85rem;border-radius:6px;border:1px solid #445;cursor:pointer;background:#1c2333;color:#e6e8ec}
  button:hover{background:#2a3550}
  button.tab.active{background:#2a3550;border-color:#6a8}
  .ok{border-color:#2d6a4f} .warn{border-color:#b08900}
  pre{background:#181b22;padding:.75rem;border-radius:8px;overflow:auto;min-height:6rem;max-height:360px;font-size:12px;white-space:pre-wrap}
  .badge{display:inline-block;padding:.15rem .5rem;border-radius:4px;font-size:.85rem;margin-right:.5rem}
  .on{background:#3d1f1f} .off{background:#1f3d28}
  .hint{font-size:.8rem;color:#7a8499}
  #tabs{display:flex;gap:.35rem;margin:1rem 0 .5rem}
  .pane{border:1px solid #333;border-radius:8px;padding:.75rem 1rem;margin-top:.5rem}
  .pane h2{font-size:1.1rem;margin:.2rem 0 .6rem}
  dl.meta dt{font-weight:600;margin-top:.35rem} dl.meta dd{margin:0;color:#aab;font-size:.9rem}
  .help{font-size:.85rem;color:#9aa3b5;margin:.5rem 0 1rem;line-height:1.5;max-width:56rem}
  .help li{margin:.2rem 0}
  .btn-row{align-items:flex-start}
  .btnwrap{display:flex;flex-direction:column;max-width:14rem}
  .btnwrap .hint{margin-top:.25rem;line-height:1.25}
  .pane-help{background:#141820;border-radius:6px;padding:.5rem .75rem;margin-bottom:.75rem;font-size:.85rem;color:#aab}
</style></head>
<body>
<h1>yse-stand</h1>
<p class="hint">251 · LAN · настройки в <code>run/quasar-policy.json</code></p>
<div id="badges"></div>
<ul class="help">
  <li><b>OTA пауза</b> — файл <code>no_downgrade</code>: колонка видит <code>hasUpdate:false</code>, zip не качает (если зациклилась).</li>
  <li><b>OTA раздать</b> — снова отдавать прошивку из <code>firmware/&lt;платформа&gt;/</code>, пока версия на колонке ≠ zip.</li>
  <li><b>Закрепитель выкл</b> — <code>no_lock</code>: после OTA <em>не</em> ставить SSH и noupdate (для экспериментального Макса).</li>
  <li><b>Закрепитель вкл</b> — автомат SSH + закрытие облачных обновлений после успешной нашей прошивки (Мини 2 и т.п.).</li>
</ul>
<div class="row btn-row">
  <div class="btnwrap"><button class="warn" title="hasUpdate false — остановить скачивание прошивки" onclick="api('ota/pause')">OTA пауза</button><span class="hint">стоп zip</span></div>
  <div class="btnwrap"><button class="ok" title="Убрать no_downgrade — снова предлагать прошивку со стенда" onclick="api('ota/resume')">OTA раздать</button><span class="hint">как обычно</span></div>
  <div class="btnwrap"><button title="Не запускать lock после OTA" onclick="api('lock/disable')">Закрепитель выкл</button><span class="hint">no_lock</span></div>
  <div class="btnwrap"><button title="Разрешить автозакрепление" onclick="api('lock/enable')">Закрепитель вкл</button><span class="hint">SSH после OTA</span></div>
  <div class="btnwrap"><button title="Обновить статус и логи" onclick="refreshAll(true)">Обновить</button></div>
</div>
<h2>Колонки</h2>
<p class="hint">Колонка появляется сама, когда её DNS указывает на этот сервис. «Проверить» читает модель и ключ с колонки. «Прошита» ставится только после подтверждения. «Переделать» ставит оболочку на заводскую колонку.</p>
<ul id="col-list"></ul>
<div id="tabs"></div>
<div id="pane" class="pane">ждём ответ /api/status… (если зависло — Ctrl+F5)</div>
<script>
let ST=null, TAB=localStorage.getItem('yse_tab')||'yandexstation_2';
let paneBuiltFor=null, logFollow=true, logScrollTop=0;
async function api(path){ await fetch('/api/'+path,{method:'POST'}); await refreshAll(true); }
async function preset(name){ await fetch('/api/platform/'+encodeURIComponent(TAB)+'/preset/'+name,{method:'POST'}); await refreshAll(true); }
async function platApi(pid,body){
  await fetch('/api/platform/'+encodeURIComponent(pid),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  await refreshAll(true);
}
function setTab(id){ TAB=id; localStorage.setItem('yse_tab',id); refreshAll(true); }
function renderTabs(){
  const el=document.getElementById('tabs');
  const kids=[...el.querySelectorAll('button.tab')];
  const byId=Object.fromEntries(kids.map(b=>[b.dataset.pid,b]));
  if(!ST||!ST.platforms) return;
  for(const [id,p] of Object.entries(ST.platforms)){
    let b=byId[id];
    if(!b){ b=document.createElement('button'); b.className='tab'; b.dataset.pid=id; b.onclick=()=>setTab(id); el.appendChild(b); }
    b.textContent=p.title||id;
    b.title='Платформа quasar: '+id;
    b.classList.toggle('active',id===TAB);
  }
}
function fmtJson(x){ try{return JSON.stringify(typeof x==='string'?JSON.parse(x):x,null,2);}catch(_){return String(x);} }
function esc(s){ return String(s==null?'':s); }
const TAB_HELP={
  yandexstation_2:'Макс: одна кнопка «Наша прошивка» — OTA 0896 + ADB (plain/register выкл, пауза снята). Привязка в Дом — «Режим Дома».',
  yandexmini_2:'Мини 2 (Buildroot): после OTA на наш zip — ADB вкл, закрепитель вкл. Sync — ответ Яндекса + вписанный adbd.',
  yandexmidi:'Станция 2: как Buildroot — ADB вкл для lock-скрипта, register без подмены allvalid при необходимости.'
};
const BTN_HINT={
  adb_on:'В get_sync_info подмешать adbd.remoteActivation и (на TV) adbEnabled → колонка поднимет сеть :5555.',
  adb_off:'Не трогать adbd в sync — удобно, пока привязываешь колонку в приложении Дом.',
  plain_on:'get_sync_info как у Яндекса, без нашей инъекции ADB (режим Макс + Дом).',
  plain_off:'get_sync_info: ответ облака + наш ADB (если adb вкл).',
  reg_on:'/register отдаём ответ Яндекса как есть (устройство появится в Доме).',
  reg_off:'/register → allvalid (старый обман; для привязки не подходит).',
  sync_cloud:'get_sync_info ходит в quasar.yandex.net (нужен интернет / passthrough).',
  sync_local:'get_sync_info только локальный шаблон со стенда, без облака.'
};
function bindPaneButtons(){
  const p=ST.platforms[TAB], c=p.config||{};
  const adbOn=!!c.adbd_enabled;
  const adb=document.getElementById('btn-adb');
  if(!adb) return;
  adb.textContent=adbOn?'Выключить ADB в sync':'Включить ADB в sync';
  adb.className=adbOn?'warn':'ok';
  adb.title=adbOn?BTN_HINT.adb_off:BTN_HINT.adb_on;
  document.getElementById('hint-adb').textContent=adbOn
    ?'Сейчас: в get_sync_info подмешиваем adbd (порт :5555).'
    :'Сейчас: adbd не трогаем — для привязки в Дом.';
  adb.onclick=()=>platApi(TAB,{adbd_enabled:!adbOn,tv_adb_enabled:!adbOn});
  const sp=document.getElementById('btn-sync-plain');
  const plain=!!c.bind_plain_sync;
  sp.textContent=plain?'Выключить plain sync':'Включить plain sync';
  sp.title=plain?BTN_HINT.plain_on:BTN_HINT.plain_off;
  document.getElementById('hint-sync-plain').textContent=plain
    ?'Сейчас: get_sync_info = ответ Яндекса как есть, без adbd (режим Дом).'
    :'Сейчас: get_sync_info = облако + наша инъекция adbd (если ADB вкл).';
  sp.onclick=()=>platApi(TAB,{bind_plain_sync:!plain});
  const rg=document.getElementById('btn-reg');
  const honest=!!c.register_honest;
  rg.textContent=honest?'Register: выкл честный → allvalid':'Register: включить честный';
  rg.title=honest?BTN_HINT.reg_on:BTN_HINT.reg_off;
  document.getElementById('hint-reg').textContent=honest
    ?'Сейчас: /register отдаём тело ответа Яндекса (видно в Доме).'
    :'Сейчас: /register → {"allvalid"} (обход, не для привязки).';
  rg.onclick=()=>platApi(TAB,{register_honest:!honest});
  const sl=document.getElementById('btn-sync-loc');
  const local=!!c.sync_local_only;
  sl.textContent=local?'Sync: снова через облако':'Sync: только шаблон стенда';
  sl.title=local?BTN_HINT.sync_cloud:BTN_HINT.sync_local;
  document.getElementById('hint-sync-loc').textContent=local
    ?'Сейчас: get_sync_info только локальный JSON, quasar не дергаем.'
    :'Сейчас: get_sync_info идёт в quasar.yandex.net (+ adbd при включённом ADB).';
  sl.onclick=()=>platApi(TAB,{sync_local_only:!local});
  const pr=document.getElementById('preset-row');
  if(pr){
    pr.style.display=TAB==='yandexstation_2'?'flex':'none';
    const bfw=document.getElementById('btn-preset-fw');
    if(bfw) bfw.onclick=()=>preset('our_firmware');
    const bhm=document.getElementById('btn-preset-home');
    if(bhm) bhm.onclick=()=>preset('bind_home');
  }
  document.getElementById('pane-help').textContent=TAB_HELP[TAB]||'';
}
function buildPaneShell(){
  const pane=document.getElementById('pane');
  pane.innerHTML='<h2 id="pane-title"></h2><div id="pane-help" class="pane-help"></div>'
    +'<div class="row btn-row" id="preset-row" style="display:none">'
    +'<div class="btnwrap"><button type="button" class="ok" id="btn-preset-fw">Наша прошивка (0896)</button>'
    +'<span class="hint" id="hint-preset-fw">OTA + ADB: plain/register выкл, no_downgrade снят</span></div>'
    +'<div class="btnwrap"><button type="button" id="btn-preset-home">Режим «Дом»</button>'
    +'<span class="hint" id="hint-preset-home">plain sync + честный register, ADB выкл</span></div>'
    +'</div>'
    +'<div class="row btn-row">'
    +'<div class="btnwrap"><button type="button" id="btn-adb"></button><span class="hint" id="hint-adb"></span></div>'
    +'<div class="btnwrap"><button type="button" id="btn-sync-plain"></button><span class="hint" id="hint-sync-plain"></span></div>'
    +'<div class="btnwrap"><button type="button" id="btn-reg"></button><span class="hint" id="hint-reg"></span></div>'
    +'<div class="btnwrap"><button type="button" id="btn-sync-loc"></button><span class="hint" id="hint-sync-loc"></span></div>'
    +'</div>'
    +'<dl class="meta"><dt>прошивка</dt><dd id="meta-fw"></dd><dt>IP в логах</dt><dd id="meta-ips"></dd></dl>'
    +'<h3>check_updates</h3><pre id="pre-manifest"></pre>'
    +'<h3>логи <span class="hint">(как в терминале: вниз — новые)</span></h3>'
    +'<pre id="pre-logs"></pre><h3 class="hint">policy</h3><pre id="pre-policy" style="max-height:160px"></pre>';
  const lg=document.getElementById('pre-logs');
  lg.addEventListener('scroll',()=>{
    logScrollTop=lg.scrollTop;
    logFollow=lg.scrollHeight-lg.scrollTop-lg.clientHeight<48;
  });
  paneBuiltFor=TAB;
}
async function renderPane(rebuild){
  if(!ST||!ST.platforms||!ST.platforms[TAB]) return;
  const p=ST.platforms[TAB], c=p.config||{};
  if(rebuild||paneBuiltFor!==TAB||!document.getElementById('pre-logs')) buildPaneShell();
  document.getElementById('pane-title').textContent=(p.title||TAB)+' · '+TAB;
  bindPaneButtons();
  document.getElementById('meta-fw').textContent=p.firmware_version||'—';
  document.getElementById('meta-ips').textContent=(p.seen_ips||[]).join(', ')||'—';
  document.getElementById('pre-manifest').textContent=fmtJson(p.check_updates);
  document.getElementById('pre-policy').textContent=JSON.stringify(c,null,2);
  const lg=document.getElementById('pre-logs');
  let text='…';
  try{
    const lr=await fetch('/api/platform/'+encodeURIComponent(TAB)+'/logs?tail=150');
    text=lr.ok?await lr.text():'HTTP '+lr.status;
  }catch(e){ text=String(e); }
  if(text!==lg.textContent){
    const wasBottom=logFollow;
    const prevTop=logScrollTop;
    lg.textContent=text;
    if(wasBottom) lg.scrollTop=lg.scrollHeight;
    else lg.scrollTop=prevTop;
  }
}
function adbSyncLabel(c){
  const on=!!c.adbd_enabled&&!c.bind_plain_sync&&!c.sync_local_only;
  if(on) return 'да';
  if(c.bind_plain_sync) return 'нет (plain)';
  if(c.sync_local_only) return 'нет (локальный sync)';
  if(!c.adbd_enabled) return 'нет (выкл)';
  return 'нет';
}
async function refreshAll(forceRebuild){
  const pane=document.getElementById('pane');
  const badges=document.getElementById('badges');
  if(badges) badges.textContent='статус…';
  try{
    const r=await fetch('/api/status');
    if(!r.ok) throw new Error('HTTP '+r.status);
    ST=await r.json();
  }catch(e){
    if(pane) pane.textContent='Ошибка: '+e+'. Обнови страницу (Ctrl+F5) или нажми «Обновить».';
    if(badges) badges.textContent='';
    return;
  }
  if(!ST.platforms[TAB]&&ST.ui_active) TAB=ST.ui_active;
  const c=ST.platforms[TAB]&&ST.platforms[TAB].config||{};
  if(badges){
    badges.innerHTML='<span class="badge '+(ST.ota_paused?'on':'off')+'">OTA '+(ST.ota_paused?'пауза':'ok')+'</span>'
      +'<span class="badge '+(ST.lock_disabled?'on':'off')+'">LOCK '+(ST.lock_disabled?'off':'on')+'</span>'
      +'<span class="badge off">ADB в sync: '+adbSyncLabel(c)+'</span>';
  }
  renderTabs();
  const list=document.getElementById('col-list');
  if(list){
    const rows=ST.columns||[];
    list.innerHTML=rows.map(c=>{
      const ip=esc(c.ip);
      const name=esc(c.title||c.platform||'колонка');
      const detail=esc(c.detail||'');
      const busy=c.job==='work'||c.job==='armed';
      let buttons='<button type="button" onclick="probeColumn(\\''+ip+'\\')">Проверить</button>';
      if(c.shell&&!c.flashed) buttons+=' <button type="button" onclick="confirmColumn(\\''+ip+'\\')">Прошита</button>';
      if(c.flashed) buttons+=' <span class="badge off">прошита</span>';
      if(c.platform&&!c.flashed) buttons+=' <button type="button" '+(busy?'disabled':'')+' onclick="convertColumn(\\''+ip+'\\')">Переделать</button>';
      const key=c.page_key?' Ключ страницы: <code>'+esc(c.page_key)+'</code>':'';
      return '<li>'+ip+' — '+name+' '+buttons+' <span class="hint">'+detail+'</span>'+key+'</li>';
    }).join('')||'<li class="hint">пока никто не обращался</li>';
  }
  await renderPane(!!forceRebuild);
}
async function postColumn(path, ip){
  const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ip})});
  if(!r.ok){ alert(await r.text()); return; }
  await refreshAll(true);
}
async function probeColumn(ip){ await postColumn('/api/columns/probe', ip); }
async function confirmColumn(ip){ await postColumn('/api/columns/confirm', ip); }
async function convertColumn(ip){
  const r=await fetch('/api/columns/convert',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ip})});
  if(!r.ok){ alert(await r.text()); return; }
  await refreshAll(true);
}
refreshAll(true); setInterval(()=>refreshAll(false),8000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, code: int, obj: object) -> None:
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code: int, text: str, ctype: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._text(200, PAGE, "text/html; charset=utf-8")
            return
        if path == "/api/status":
            self._json(200, status_dict())
            return
        if path.startswith("/api/platform/") and path.endswith("/logs"):
            pid = path.split("/")[3]
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            tail = int((qs.get("tail") or ["150"])[0])
            if pid not in TAB_ORDER:
                self._json(404, {"error": "unknown platform"})
                return
            self._text(200, file_logs_platform(pid, tail))
            return
        if path == "/api/logs":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            tail = int((qs.get("tail") or ["120"])[0])
            grep = (qs.get("grep") or [""])[0]
            text = file_logs(tail_lines=max(tail, 120), grep=grep)
            if not grep and len(text) < 80:
                text = file_logs(tail_lines=tail, grep="OTA") + "\n" + file_logs(
                    tail_lines=tail, grep="LOCK"
                )
            self._text(200, text)
            return
        if path == "/api/logs/docker":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            tail = int((qs.get("tail") or ["60"])[0])
            grep = (qs.get("grep") or [""])[0]
            text = docker_logs(tail=tail, grep=grep)
            self._text(200, text)
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        RUN.mkdir(parents=True, exist_ok=True)
        if path == "/api/columns/probe":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8") if length else "{}")
            except json.JSONDecodeError:
                body = {}
            try:
                self._json(200, probe_column(str(body.get("ip", ""))))
            except (ValueError, RuntimeError) as exc:
                self._json(400, {"error": str(exc)})
            return
        if path == "/api/columns/confirm":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8") if length else "{}")
            except json.JSONDecodeError:
                body = {}
            try:
                self._json(200, confirm_column(str(body.get("ip", ""))))
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            return
        if path == "/api/columns/convert":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8") if length else "{}")
            except json.JSONDecodeError:
                body = {}
            try:
                self._json(200, convert_column(str(body.get("ip", ""))))
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            return
        if path == "/api/ota/pause":
            (RUN / "no_downgrade").touch()
            self._json(200, {"ok": True, "ota_paused": True})
            return
        if path == "/api/ota/resume":
            (RUN / "no_downgrade").unlink(missing_ok=True)
            self._json(200, {"ok": True, "ota_paused": False})
            return
        if path == "/api/lock/disable":
            (RUN / "no_lock").touch()
            self._json(200, {"ok": True, "lock_disabled": True})
            return
        if path == "/api/lock/enable":
            (RUN / "no_lock").unlink(missing_ok=True)
            self._json(200, {"ok": True, "lock_disabled": False})
            return
        if path.startswith("/api/platform/") and "/preset/" in path:
            parts = path.strip("/").split("/")
            # api, platform, <pid>, preset, <name>
            if len(parts) < 5 or parts[2] not in TAB_ORDER:
                self._json(404, {"error": "unknown platform or preset"})
                return
            pid, preset = parts[2], parts[4]
            try:
                self._json(200, apply_platform_preset(pid, preset))
            except KeyError as e:
                self._json(404, {"error": str(e)})
            return
        if path.startswith("/api/platform/"):
            pid = path.rsplit("/", 1)[-1]
            if pid not in TAB_ORDER:
                self._json(404, {"error": "unknown platform"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b"{}"
            try:
                patch = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                patch = {}
            if not isinstance(patch, dict):
                patch = {}
            root = patch_platform(pid, patch)
            root["ui_active"] = pid
            write_root(root)
            self._json(200, root)
            return
        self._json(404, {"error": "not found"})


def main() -> None:
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"yse-panel http://0.0.0.0:{PORT} run={RUN} container={CONTAINER}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()