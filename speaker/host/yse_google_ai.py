#!/usr/bin/env python3
# Прокладка к чату «Режим ИИ» в поиске Google (google.com/search?udm=50).
# Для колонки это обычный адрес нейросети: POST /v1/chat/completions.
# Браузер — Chromium с постоянным профилем. В Google входят один раз через VNC.

import json
import os
import queue
import re
import socket
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("YSE_GOOGLE_AI_PORT", "18955"))
PROFILE = os.environ.get("YSE_GOOGLE_AI_PROFILE", "/storage/yse/google-ai/profile")
SHOT = os.environ.get("YSE_GOOGLE_AI_SHOT", "/storage/yse/google-ai/last.png")
DISPLAY = os.environ.get("DISPLAY", ":99")
VNC_PORT = int(os.environ.get("YSE_GOOGLE_AI_VNC", "5900"))
WAIT_S = float(os.environ.get("YSE_GOOGLE_AI_WAIT", "26"))

jobs = queue.Queue()
state = {"ready": False, "signed_in": False, "problem": "браузер ещё запускается", "vnc": ""}
state_lock = threading.Lock()


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def lan_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.168.209.1", 1))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def ensure_display():
    """Xvfb и VNC, чтобы в Google можно было войти с другого компьютера в сети."""
    os.environ["DISPLAY"] = DISPLAY
    if not os.path.exists("/tmp/.X11-unix/X" + DISPLAY.lstrip(":")):
        subprocess.Popen(
            ["Xvfb", DISPLAY, "-screen", "0", "1280x900x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(20):
            if os.path.exists("/tmp/.X11-unix/X" + DISPLAY.lstrip(":")):
                break
            time.sleep(0.1)
    ip = lan_ip()
    with state_lock:
        state["vnc"] = "%s:%d" % (ip, VNC_PORT)
    if subprocess.run(["ss", "-ltn"], capture_output=True, text=True).stdout.find(":%d" % VNC_PORT) < 0:
        subprocess.Popen(
            ["x11vnc", "-display", DISPLAY, "-forever", "-shared", "-nopw",
             "-listen", ip, "-rfbport", str(VNC_PORT), "-noxdamage", "-bg",
             "-o", "/storage/yse/google-ai/vnc.log"])
        log("vnc %s:%d" % (ip, VNC_PORT))


CHROME = {
    "режим ии", "все", "картинки", "видео", "новости", "ещё", "еще",
    "войти", "поиск", "поиск в google",
}


def reply_only(raw, query):
    """Убирает шапку страницы и облачко с нашим вопросом. Остаётся текст ответа."""
    needle = " ".join((query or "").split())[:48]
    lines = []
    for raw_line in (raw or "").splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue
        low = line.lower().strip(" .…")
        if low in CHROME or low.startswith("задайте вопрос"):
            continue
        if needle and needle[:24] in line:
            continue
        if "не пересказывай" in line or "двумя предложениями, без списков" in line:
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def answer_after(raw, question, before=""):
    """Текст после нового вопроса, которого не было на странице до отправки.
    Поле ввода внизу тоже содержит вопрос, но после него ответа нет — такое место пропускаем."""
    q = " ".join((question or "").split())
    if len(q) < 2:
        return ""
    marks = [q] if len(q) <= 40 else [q, q[:40]]
    for mark in marks:
        start = 0
        spots = []
        while True:
            at = raw.find(mark, start)
            if at < 0:
                break
            spots.append(at)
            start = at + len(mark)
        for at in reversed(spots):
            tail = reply_only(raw[at + len(mark):].lstrip(" \n:.—…"), "")
            if len(tail) > 15 and tail not in (before or ""):
                return tail
        if spots:
            break
    return ""


def spoken(text):
    """Текст для колонки: абзацы ответа, без меню-списка в конце и без ссылок."""
    lines = []
    bullets = []
    hit_bullet = False
    for raw in (text or "").splitlines():
        line = " ".join(raw.split())
        if not line or line in ("Режим ИИ", "Показать все", "Показать ещё", "Ещё"):
            continue
        if line[:1] in "•-*·" or re.match(r"^\d+[.)]\s", line):
            hit_bullet = True
            bullets.append(re.sub(r"^[•\-*·]\s*|^\d+[.)]\s*", "", line))
            continue
        if hit_bullet:
            break
        lines.append(line)
    body = " ".join(lines) if lines else " ".join(bullets[:3])
    body = re.sub(r"https?://\S+", " ", body)
    body = re.sub(r"\[\d+\]", " ", body)
    body = " ".join(body.split()).lstrip("\"'«»“” ")
    parts = [p for p in re.split(r"(?<=[.!?…])\s+", body) if p]
    if len(parts) > 1 and parts[-1].rstrip().endswith("?"):
        parts = parts[:-1]
    return " ".join(parts[:2]).strip()


def pack_query(messages):
    """Вопрос для строки чата и текст самой реплики, по которому узнаём новый ответ на странице.
    Стиль из system уходит в ту же строку перед вопросом, чтобы характер не потерялся."""
    style = ""
    address = ""
    turns = []
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = " ".join(str(item.get("content") or "").split())
        if not content:
            continue
        if role == "system":
            if "Стиль: " in content:
                style = "Стиль, не пересказывай и не отвечай на него: " + content.split("Стиль: ", 1)[1][:2200]
            mark = "Обращайся к собеседнику только"
            if mark in content:
                start = content.find(mark)
                end = content.find(".", start)
                if end > start:
                    address = content[start:end + 1]
            continue
        if role == "user":
            turns.append(("user", content[:400]))
        elif role == "assistant":
            turns.append(("assistant", content[:300]))
    if not turns or turns[-1][0] != "user":
        return "", "", []
    users = [text for role, text in turns if role == "user"]
    last = users[-1]
    bits = [("Человек: " if role == "user" else "Колонка: ") + text for role, text in turns[:-1][-4:]]
    tail = "Вопрос: " + last
    head = "Ответь по-русски, коротко, двумя предложениями, без списков. "
    if address and address in style:
        style = " ".join(style.replace(address, " ").split())
    if style:
        head += style + " "
    if address:
        head += address + " "
    if bits:
        head += "Учти разговор. " + " ".join(bits) + " "
    packed = (head + tail).strip()
    if last not in packed or len(packed) > 3500:
        room = 3500 - len(tail) - 1
        packed = ((head[:room] + " " + tail).strip() if room > 0 else tail[:1800])
    return packed, last, users


class Chat:
    def __init__(self):
        self.play = None
        self.ctx = None
        self.page = None
        self.sent = []
        self.last_text = ""

    def start(self):
        from playwright.sync_api import sync_playwright
        os.makedirs(PROFILE, exist_ok=True)
        self.play = sync_playwright().start()
        self.ctx = self.play.chromium.launch_persistent_context(
            PROFILE,
            headless=False,
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            viewport={"width": 1280, "height": 900},
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.goto("https://www.google.com/?hl=ru", wait_until="domcontentloaded", timeout=30000)
        self.accept_cookies()
        self.refresh_state("")
        log("browser ready signed_in=%s" % state["signed_in"])

    def signed_in(self):
        try:
            names = {item["name"] for item in self.ctx.cookies("https://www.google.com")}
        except Exception:
            return False
        return "SAPISID" in names or "SID" in names

    def refresh_state(self, problem):
        with state_lock:
            state["ready"] = self.page is not None
            state["signed_in"] = self.signed_in()
            state["problem"] = problem

    def accept_cookies(self):
        for label in ("Принять все", "Accept all", "Я согласен"):
            try:
                btn = self.page.get_by_role("button", name=label)
                if btn.count():
                    btn.first.click(timeout=1500)
                    return
            except Exception:
                continue

    def problem(self):
        url = self.page.url or ""
        if "google.com/sorry" in url or "/sorry/" in url:
            return "captcha"
        if "accounts.google.com" in url:
            return "login"
        try:
            head = self.page.inner_text("body")[:1500].lower()
        except Exception:
            return ""
        if "необычн" in head and "трафик" in head:
            return "captcha"
        if "unusual traffic" in head:
            return "captcha"
        return ""

    def extract(self):
        try:
            return self.page.evaluate("""() => {
                const main = document.querySelector('[role="main"]')
                    || document.querySelector('main')
                    || document.querySelector('#rso')
                    || document.body;
                return main.innerText || '';
            }""") or ""
        except Exception:
            return ""

    def shot(self):
        try:
            self.page.screenshot(path=SHOT, full_page=False)
        except Exception as exc:
            log("shot %s" % type(exc).__name__)

    def wait_answer(self, question, before):
        """Ждём, пока страница изменится и после нового вопроса появится устойчивый текст."""
        deadline = time.time() + WAIT_S
        last = ""
        stable = 0
        while time.time() < deadline:
            found = self.problem()
            if found:
                self.shot()
                return found, ""
            self.accept_cookies()
            raw = self.extract()
            if raw == before:
                time.sleep(0.4)
                continue
            text = answer_after(raw, question, before)
            if len(text) > 15:
                if text == last:
                    stable += 1
                    if stable >= 3:
                        return "", text
                else:
                    last = text
                    stable = 0
            else:
                last = ""
                stable = 0
            time.sleep(0.4)
        self.shot()
        return ("", last) if last else ("empty", "")

    def type_follow(self, text):
        for finder in (
            lambda: self.page.get_by_placeholder("Задайте вопрос"),
            lambda: self.page.get_by_role("textbox", name="Задайте вопрос"),
            lambda: self.page.locator("textarea, div[contenteditable='true']"),
        ):
            try:
                box = finder().last
                box.click(timeout=2000)
                box.fill(text, timeout=2000)
                box.press("Enter")
                return True
            except Exception:
                continue
        return False

    def debug(self):
        self.shot()
        info = {"url": "", "title": "", "text": "", "problem": "no page"}
        if self.page is None:
            return info
        try:
            info["url"] = self.page.url
            info["title"] = self.page.title()
            info["text"] = (self.extract() or "")[:1500]
            info["problem"] = self.problem()
        except Exception as exc:
            info["problem"] = type(exc).__name__
        return info

    def search(self, packed, question):
        before = self.extract()
        url = "https://www.google.com/search?hl=ru&gl=ru&udm=50&q=" + urllib.parse.quote(packed)
        log("search %s" % question[:80])
        self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        self.accept_cookies()
        return self.wait_answer(question, before)

    def ask(self, messages):
        packed, question, users = pack_query(messages)
        if not packed:
            return "Повторите вопрос."
        if self.page is None:
            self.start()
        found, text = "", ""
        same = users[:-1] == self.sent and "udm=50" in (self.page.url or "")
        if same:
            before = self.extract()
            if self.type_follow(question):
                log("follow %s" % question[:80])
                found, text = self.wait_answer(question, before)
        if not text:
            found, text = self.search(packed, question)
        if found == "captcha":
            self.refresh_state("captcha")
            return "Google просит проверку. Откройте экран сервера, порт %d." % VNC_PORT
        if found == "login":
            self.refresh_state("login")
            return "Нужно войти в Google. Экран сервера открыт на порту %d." % VNC_PORT
        if not text:
            self.refresh_state(found or "empty")
            return "Google ничего не ответил."
        self.sent = users
        self.last_text = text
        self.refresh_state("")
        said = spoken(text)
        log("answer %s" % said[:120])
        return said or "Google ничего не ответил."


def worker():
    chat = Chat()
    try:
        chat.start()
    except Exception as exc:
        log("browser fail %s" % exc)
        with state_lock:
            state["problem"] = "браузер не запустился"
    while True:
        job = jobs.get()
        try:
            if job.get("op") == "debug":
                job["out"] = chat.debug()
            else:
                job["out"] = chat.ask(job["messages"]) if chat.page is not None else "Нейросеть не отвечает."
        except Exception as exc:
            log("ask fail %s" % exc)
            job["out"] = "Нейросеть не отвечает."
            try:
                chat.shot()
            except Exception:
                pass
        job["done"].set()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def send_json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/v1/debug", "/debug"):
            job = {"op": "debug", "done": threading.Event(), "out": {}}
            jobs.put(job)
            job["done"].wait(15)
            self.send_json(200, job["out"] if isinstance(job["out"], dict) else {"error": job["out"]})
            return
        if path not in ("/health", "/v1/health"):
            self.send_error(404)
            return
        with state_lock:
            self.send_json(200, dict(state, ok=True))

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self.send_error(404)
            return
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            self.send_json(403, {"error": "only local"})
            return
        length = int(self.headers.get("Content-Length") or "0")
        try:
            payload = json.loads(self.rfile.read(length) if 0 < length < 200000 else b"{}")
        except ValueError:
            self.send_json(400, {"error": "json"})
            return
        job = {"messages": payload.get("messages") or [], "done": threading.Event(), "out": ""}
        jobs.put(job)
        if not job["done"].wait(WAIT_S + 12):
            self.send_json(200, completion("Google не успел ответить."))
            return
        self.send_json(200, completion(job["out"]))


def completion(text):
    return {
        "id": "chatcmpl-google-ai",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
    }


def main():
    os.makedirs(os.path.dirname(PROFILE), exist_ok=True)
    ensure_display()
    threading.Thread(target=worker, daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    log("listen 127.0.0.1:%d" % PORT)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
