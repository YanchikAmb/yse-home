# -*- coding: utf-8 -*-
"""Вписывает вызов закрепителя в equasar_server.py. Повторный запуск ничего не меняет."""

from pathlib import Path

MARKER = "# yse-lock-hook"

HELPER = '''
# yse-lock-hook: нет модуля — стенд работает как раньше
_YSE_LOCK = {"mod": None, "miss": False}


def _yse_lock(method, *args):
    if _YSE_LOCK["miss"]:
        return
    if _YSE_LOCK["mod"] is None:
        try:
            import lock_speaker
        except ImportError:
            _YSE_LOCK["miss"] = True
            return
        _YSE_LOCK["mod"] = lock_speaker
    try:
        getattr(_YSE_LOCK["mod"], method)(*args)
    except Exception as e:
        print(f"  !!! [LOCK] {method}: {e!r}")
        sys.stdout.flush()

'''

ANCHORS = (
    (
        "            already = _ota_already_installed(cur, _ver)\n"
        "            ua = self.headers.get(\"User-Agent\", \"\")\n"
        "            sess_key = _ota_sess_key(dev_id, lip)\n"
        "            if already:\n",
        "            already = _ota_already_installed(cur, _ver)\n"
        "            if fw and already:\n"
        "                _yse_lock(\"note_and_arm\", self.client_address[0], plat or (fw.get(\"platform\") or \"\"), cur)\n"
        "            ua = self.headers.get(\"User-Agent\", \"\")\n"
        "            sess_key = _ota_sess_key(dev_id, lip)\n"
        "            if already:\n",
    ),
    (
        "                    self._log_payload(\"SYNC-REAL→\", payload, \"настоящий ответ Яндекса\")\n"
        "                    return\n",
        "                    self._log_payload(\"SYNC-REAL→\", payload, \"настоящий ответ Яндекса\")\n"
        "                    _yse_lock(\"on_adb\", self.client_address[0])\n"
        "                    return\n",
    ),
    (
        "            self._log_payload(\"SYNC→\", _p, \"локальный шаблон\")\n"
        "            return\n",
        "            self._log_payload(\"SYNC→\", _p, \"локальный шаблон\")\n"
        "            _yse_lock(\"on_adb\", self.client_address[0])\n"
        "            return\n",
    ),
    (
        "            self._log_payload(\"SYNC→\", payload, \"локальный шаблон (fallback)\")\n",
        "            self._log_payload(\"SYNC→\", payload, \"локальный шаблон (fallback)\")\n"
        "            _yse_lock(\"on_adb\", self.client_address[0])\n",
    ),
)


def install(path):
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    if MARKER in text:
        return False
    anchor = "except Exception:\n    pass\n\nUPSTREAM_HOST"
    if anchor not in text:
        raise SystemExit("не нашёл место для закрепителя в equasar_server.py")
    text = text.replace(anchor, "except Exception:\n    pass\n" + HELPER + "\nUPSTREAM_HOST", 1)
    for old, new in ANCHORS:
        if old not in text:
            raise SystemExit("не нашёл точку вызова в equasar_server.py")
        text = text.replace(old, new, 1)
    file.write_text(text, encoding="utf-8", newline="\n")
    return True


if __name__ == "__main__":
    import sys
    changed = install(sys.argv[1])
    print("hook", "installed" if changed else "already")
