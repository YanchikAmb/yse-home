# -*- coding: utf-8 -*-
"""Связывает колонку с Home Assistant.

  python yse_ha_link.py look
  python yse_ha_link.py component
  python yse_ha_link.py install --conf ...\\bedroom.json
  python yse_ha_link.py state

Доступ — install/site.json: ha.ssh, ha.ssh_port, ha.login, пароль ha.password или
YSE_HA_PASSWORD (иначе спросит). Старый вариант — файл ha.req или YSE_HA_REQ с блоком
«homeassistant:» (ssh / login / password через табуляцию).
Нужен Home Assistant OS с дополнением SSH (Advanced SSH & Web Terminal, protection mode
выключен). Пароли и токены не печатаются.
state     показывает сущности колонки и спутника в HA.

look      только смотрит: тип установки HA, каталог config, есть ли компонент и Wyoming.
component кладёт ha/custom_components/yse_station в config HA с адресом сервера из
          site.json и перезапускает HA. Дальше колонки находит сам компонент.
install   то же и сразу добавляет интеграции Wyoming (спутник на сервере) и yse_station
          (колонка) для уже поставленной колонки.
"""
import argparse
import json
import os
import pathlib
import re
import sys
import time

import paramiko

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent / "yandex_station" if not (HERE.parent / "src").exists() else HERE.parent
sys.path.insert(0, str(HERE))
import yse_station_setup as st  # noqa: E402


def ha_access():
    req = os.environ.get("YSE_HA_REQ") or st.site_get("ha.req")
    if not req:
        if not st.site_get("ha.ssh"):
            raise SystemExit("нет доступа к HA: заполните ha.ssh в install/site.json (команда init)")
        found = {"ssh": st.site_get("ha.ssh"), "port": int(st.site_get("ha.ssh_port", default=22)),
                 "login": st.site_get("ha.login", default="root"),
                 "password": st.site_get("ha.password", "YSE_HA_PASSWORD")}
        if not found["password"]:
            import getpass
            found["password"] = getpass.getpass("пароль SSH дополнения HA %s@%s: " % (found["login"], found["ssh"]))
            os.environ["YSE_HA_PASSWORD"] = found["password"]
        return found
    raw = st.site_path(req).read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1251")
    section = None
    found = {}
    for line in text.splitlines():
        if line.strip().endswith(":") and "\t" not in line:
            section = line.strip().rstrip(":").lower()
            continue
        if section == "homeassistant" and "\t" in line:
            key, value = line.split("\t", 1)
            found[key.strip()] = value.strip()
    if not all(found.get(k) for k in ("ssh", "login", "password")):
        raise SystemExit("в req.txt нет блока homeassistant с ssh/login/password")
    return found


class Ssh:
    def __init__(self, host, user, pw, port=22):
        self.pw = pw
        self.cli = paramiko.SSHClient()
        self.cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.cli.connect(host, port=port, username=user, password=pw, timeout=15,
                         allow_agent=False, look_for_keys=False)

    def run(self, cmd, timeout=120, sudo=False, data=None):
        stdin, stdout, stderr = self.cli.exec_command(cmd, timeout=timeout)
        if sudo:
            stdin.write(self.pw + "\n")
        if data is not None:
            stdin.write(data)
        stdin.flush()
        stdin.channel.shutdown_write()
        out = (stdout.read() + stderr.read()).decode("utf-8", "replace")
        return stdout.channel.recv_exit_status(), out.replace(self.pw, "***")


def shq(text):
    return "'" + text.replace("'", "'\\''") + "'"


def connect():
    acc = ha_access()
    return Ssh(acc["ssh"], acc["login"], acc["password"], int(acc.get("port", 22))), acc


def server_ip():
    return st.site_get("server.ip") or st.creds()[0]


LOOK = r"""
echo "user $(id -un) uid $(id -u)"
uname -a | cut -c1-120
command -v ha >/dev/null && echo "ha-cli yes" || echo "ha-cli no"
command -v docker >/dev/null && echo "docker yes" || echo "docker no"
for d in /homeassistant /config /root/config; do
  [ -f "$d/configuration.yaml" ] && echo "config $d"
done
ls /homeassistant/custom_components 2>/dev/null | tr '\n' ' '; echo
[ -n "$SUPERVISOR_TOKEN" ] && echo "supervisor token yes" || echo "supervisor token no"
curl -s -m 5 -o /dev/null -w 'core api %{http_code}\n' -H "Authorization: Bearer $SUPERVISOR_TOKEN" http://supervisor/core/api/
curl -s -m 5 -H "Authorization: Bearer $SUPERVISOR_TOKEN" http://supervisor/core/api/config/config_entries/entry \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('entries', len(d), 'wyoming', [e['title'] for e in d if e['domain']=='wyoming'], 'yse', [e['title'] for e in d if e['domain']=='yse_station'])" 2>&1 | head -c 600
"""


STATES = r'''
import json, os, urllib.request
T = os.environ["SUPERVISOR_TOKEN"]
def call(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request("http://supervisor/core/api" + path, data=data, method=method,
                                 headers={"Authorization": "Bearer " + T, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()
tpl = ("{% for d in ['yse_station','wyoming'] %}{% for e in integration_entities(d) %}"
       "{{ d }} {{ e }} = {{ states(e) }}\n{% endfor %}{% endfor %}")
for line in call("POST", "/template", {"template": tpl}).splitlines():
    print("MARK " + line[:120])
for e in json.loads(call("GET", "/config/config_entries/entry")):
    if e["domain"] in ("wyoming", "yse_station"):
        print("MARK entry", e["domain"], e["title"], e["state"])
'''


def cmd_state(_args):
    ssh, _acc = connect()
    ssh.run("cat > /tmp/yse_states.py", data=STATES)
    _c, out = ssh.run("bash -lc 'python3 /tmp/yse_states.py; rm -f /tmp/yse_states.py'")
    for line in out.splitlines():
        if line.startswith("MARK ") or "Traceback" in line or "Error" in line:
            print(line.replace("MARK ", ""))
    return 0


def cmd_look(_args):
    ssh, acc = connect()
    print("ssh ok")
    _c, out = ssh.run("bash -lc " + shq(LOOK))
    print(out)
    _c, out = ssh.run("sudo -S -p '' docker ps --format '{{.Names}} {{.Image}}' 2>/dev/null | head -n 20", sudo=True)
    print(out)
    return 0


FLOWS = r'''
import json, os, sys, time, urllib.request, urllib.error
T = os.environ["SUPERVISOR_TOKEN"]
B = "http://supervisor/core/api"
ARGS = json.loads(sys.stdin.readline())

def call(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(B + path, data=data, method=method,
                                 headers={"Authorization": "Bearer " + T, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return {"type": "http", "reason": e.code, "body": e.read()[:200].decode("utf-8", "replace")}

for _ in range(60):
    try:
        if call("GET", "/config").get("state") == "RUNNING":
            break
    except Exception:
        pass
    time.sleep(5)
print("MARK core running")

def flow(handler, data):
    f = call("POST", "/config/config_entries/flow", {"handler": handler, "show_advanced_options": False})
    steps = 0
    while f.get("type") == "form" and steps < 3:
        f = call("POST", "/config/config_entries/flow/" + f["flow_id"], data)
        steps += 1
    print("MARK", handler, f.get("type"), f.get("reason") or f.get("errors") or f.get("title"))

if any(e["domain"] == "wyoming" and e["title"] == ARGS["name"] for e in call("GET", "/config/config_entries/entry")):
    print("MARK wyoming already", ARGS["name"])
else:
    flow("wyoming", {"host": ARGS["sat_host"], "port": ARGS["sat_port"]})
flow("yse_station", {"name": ARGS["name"], "host": ARGS["speaker"], "port": 18080, "token": ARGS["token"],
                     "area": ARGS.get("area", "")})
entries = call("GET", "/config/config_entries/entry")
for e in entries:
    if e["domain"] in ("wyoming", "yse_station"):
        print("MARK entry", e["domain"], e["title"], e["state"])
'''


def component_tar(server):
    import io
    import tarfile
    src = REPO / "ha" / "custom_components" / "yse_station"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for path in sorted(src.rglob("*")):
            if "__pycache__" in path.parts or not path.is_file():
                continue
            data = path.read_bytes().replace(b"\r\n", b"\n")
            if path.name == "const.py":
                data = re.sub(rb'(?m)^DEFAULT_SERVER = ".*"$', b'DEFAULT_SERVER = "' + server.encode() + b'"', data)
            info = tarfile.TarInfo("yse_station/" + path.relative_to(src).as_posix())
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def push_component(ssh, server):
    print("1. компонент в /homeassistant/custom_components/yse_station (сервер %s)" % server)
    stdin, stdout, stderr = ssh.cli.exec_command(
        "rm -rf /homeassistant/custom_components/yse_station && mkdir -p /homeassistant/custom_components && "
        "tar -C /homeassistant/custom_components -xf - && ls /homeassistant/custom_components/yse_station | wc -l")
    stdin.write(component_tar(server))
    stdin.channel.shutdown_write()
    print("файлов:", (stdout.read() + stderr.read()).decode().strip())

    print("2. проверка конфигурации")
    code, out = ssh.run("bash -lc 'ha core check'", timeout=300)
    print(out.strip().splitlines()[-1] if out.strip() else code)
    if code:
        raise SystemExit("конфигурация HA не проходит проверку, перезапуск отменён")

    print("3. перезапуск HA")
    code, out = ssh.run("bash -lc 'ha core restart'", timeout=600)
    print(out.strip().splitlines()[-1] if out.strip() else code)


def cmd_component(_args):
    ssh, _acc = connect()
    push_component(ssh, server_ip())
    return 0


def cmd_install(args):
    conf = st.load_conf(args.conf)
    host = st.Host()
    code, out = st.speaker_run(host, conf["speaker"], "cat %s/token\n" % st.DIR, timeout=20)
    token = out.strip()
    if code or not token.isalnum():
        raise SystemExit("нет ключа страницы на колонке")
    ssh, _acc = connect()
    push_component(ssh, host.host)

    print("4. интеграции")
    params = {"sat_host": host.host, "sat_port": int(conf["sat_port"]), "name": conf["name"],
              "speaker": conf["speaker"], "token": token, "area": conf.get("area", "")}
    ssh.run("cat > /tmp/yse_flows.py", data=FLOWS)
    stdin, stdout, stderr = ssh.cli.exec_command("bash -lc 'python3 /tmp/yse_flows.py; rm -f /tmp/yse_flows.py'", timeout=600)
    stdin.write(json.dumps(params) + "\n")
    stdin.channel.shutdown_write()
    out = (stdout.read() + stderr.read()).decode("utf-8", "replace").replace(token, "***")
    for line in out.splitlines():
        if line.startswith("MARK ") or "Error" in line or "Traceback" in line:
            print(line.replace("MARK ", ""))
    return 0


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("look")
    sub.add_parser("state")
    sub.add_parser("component")
    p = sub.add_parser("install")
    p.add_argument("--conf", required=True)
    args = ap.parse_args()
    return {"look": cmd_look, "install": cmd_install, "state": cmd_state, "component": cmd_component}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
