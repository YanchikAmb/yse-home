# Установка

Новый дом целиком — [home.md](home.md): сервис, DNS стенда, сброс колонки, `column --ip --model`. Первый раз и простыми словами, если root уже есть — [install-beginner.md](install-beginner.md). Здесь все шаги и что они делают.

Всё делает один скрипт `install/yse_station_setup.py`. Он работает с вашего компьютера (Windows, Linux, macOS) по SSH к серверу. Нужны Python 3.10+ и `pip install paramiko`. Команды можно запускать и прямо на сервере. Связку с Home Assistant делает `install/yse_ha_link.py`.

## Порядок

1. Колонкам — root (SSH dropbear на порту 2222, вход root без пароля). Здесь не описан.
2. `init` — один раз ответить на вопросы про дом.
3. `all --yes` — подготовить сервер, собрать программы, включить сторожа, поставить компонент HA.
4. Каждая колонка — `column --ip IP --model max|station2|mini2`. Станция 2 и Мини 2 после сброса на DNS стенда; Макс — `--apk`. Либо кнопка в HA, когда SSH `:2222` уже есть. Вручную то же самое делают `speaker` и `host`.

## 1. Конфиг дома: `init`

```
python install/yse_station_setup.py init
```

Пишет `install/site.json` (в git не входит). Его можно заполнить и руками по образцу `install/site.example.json`:

| Поле | Что это |
| --- | --- |
| `server.ip`, `server.user` | домашний сервер и пользователь SSH с правом `sudo` |
| `server.key_file` / `server.password` | SSH-ключ или пароль. Пароль можно не писать: его возьмёт переменная `YSE_SERVER_PASSWORD` или скрипт спросит при запуске. Пароль нужен и для `sudo` |
| `service_user` | от кого работают службы на сервере, обычно тот же пользователь |
| `network` | подсеть колонок, например `192.168.1.0/24`. Сторож ищет колонки в ней |
| `ha.url` | адрес Home Assistant, например `http://192.168.1.10:8123` |
| `ha.ssh`, `ha.ssh_port`, `ha.login`, `ha.password` | SSH дополнения HA «Advanced SSH & Web Terminal» (protection mode выключен). Пароль можно не писать: `YSE_HA_PASSWORD` или вопрос при запуске. Без `ha.ssh` компонент ставится вручную |
| `ref_speaker` | IP колонки с нашей оболочкой, у которой уже есть `/data/opt/yandex-station/soda`. Оттуда он один раз копируется на сервер |
| `soda_tar` | вместо `ref_speaker`: архив `soda.tar` с каталогом `soda/` на вашем компьютере |

`soda/` — Google SODA для aarch64: `libsoda.so`, русская модель `model/` и загрузчик glibc aarch64 в `lib/`. Это не часть проекта и не распространяется с ним; на заводской колонке его тоже нет. Он нужен только для «Распознавания на колонке» на Станции 2. Без него Станция 2 отдаёт фразу серверу, как Мини 2. Мини 2 получает статическое ухо `ear64s` и `soda/` не использует.
| `skip` | IP колонок, которые сторож не должен трогать |
| `clock_offset` | часовой пояс, часов от UTC (Москва — 3). Нужен будильнику и часам |
| `wake_model` | модель слова `.tflite`: путь на вашем компьютере или URL. Пусто — «джарвис» из [Releases](https://github.com/YanchikAmb/yandex-station/releases) |
| `yandex_token_file` | файл с OAuth-токеном аккаунта Яндекса, одной строкой. Им колонка говорит голосом Алисы, пока на ней никто не вошёл |
| `rvc_from` | каталог на сервере с голосами RVC (`<имя>/<имя>.pth`) для команды `voice` |

Старый формат: `"server": {"req": "путь/к/.req"}` и `"ha": {"req": "путь/к/req.txt"}` берут доступ из файлов `.req` (cp1251: строки «хост …», «логин: …», «пароль: …») и `req.txt` (блок `homeassistant:` со строками `ssh`, `login`, `password` через табуляцию).

## 2. Сервер: `all`

```
python install/yse_station_setup.py all --yes [--stt] [--voice]
```

Подряд: `prepare`, `build`, `server`, компонент HA. Повтор безопасен. Каждую часть можно запустить отдельно.

### prepare

Готовит сервер Ubuntu 22.04. Уже готовое пропускает.

- apt: `docker.io`, `docker-compose-v2` (если docker ещё нет), `gcc-aarch64-linux-gnu`, `ffmpeg`, `libva2`, `python3-venv`, `git`, `curl`;
- каталоги `/storage/yse`, `/storage/yse-music`, `/storage/yse-mww`, владелец — `service_user`;
- библиотеки служб в `pip --user`: `wyoming`, `vosk`, `numpy`, `onnxruntime`, `websockets`;
- окружение музыки `/storage/yse-music/.venv` (`yandex-music`);
- тренер слова [microwakeword-trainer](https://github.com/interkelstar/microwakeword-trainer) в `/storage/yse-mww/trainer`, в его `.venv` — TensorFlow и microWakeWord (10–20 минут в первый раз);
- малая модель Vosk `vosk-model-small-ru-0.22` и Silero VAD;
- образ `spotter-tap-build` (armhf gcc с glibc 2.28) из `Dockerfile`;
- вход на колонки `/storage/yse/bin/yse-ssh`;
- модель слова в `/storage/yse-mww/trainer/ru_jarvis_mww.tflite`, токен Яндекса в `/storage/yse/demo.token` (600), `soda/` в `/storage/yse/soda` — из полей `site.json`.

### build

Собирает программы колонки на сервере в `/tmp/yse-build/out`: armhf — в контейнере `spotter-tap-build`, aarch64 — кросс-gcc сервера, `jarvisd` (C++ с TFLite Micro) — в контейнере `yse-armcxx`. armhf проверяются на glibc не новее 2.30.

### server

Кладёт пакет (`src`, `www`, `init`, `install`, `host`) в `/storage/yse/pkg`, пишет `/storage/yse/discover.env` (адреса сервера и HA, `skip`, колонка-образец, пользователь служб, часовой пояс), ставит и перезапускает `yse-discover` (сторож) и `yse-train` (дообучение слова).

После обновления репозитория `server --yes` повторяют: сторож сам копирует изменившиеся скрипты в `/storage/yse/app` и перезапускает только их службы. Программы колонки из `/storage/yse/build/out`, страницу и опрос обновлений он раздаёт колонкам. Колонка раз в 3 минуты сравнивает номер версии с `GET /v1/update` и качает только изменившиеся файлы. Дом, ухо и музыка при этом не перезапускаются.

### stt и voice (нужна NVIDIA)

- `stt --yes` — `docker compose` в `/storage/yse/stt`: Whisper `large-v3-turbo` (`127.0.0.1:18950`) и GigaAM v3 (`127.0.0.1:18953`). Крупный Vosk `vosk-model-ru-0.42` (`127.0.0.1:18951`, нужно 6+ ГБ ОЗУ) — только с `--with-vosk`, когда модель докачалась. Протокол — OpenAI `POST /v1/audio/transcriptions`.
- `voice --yes` — свой голос `yse-voice` (`127.0.0.1:18952`): Silero `v5_6_ru` и RVC. Torch и RVC (около 5 ГБ) ставятся в `/storage/yse/voice/venv`, первый раз 10–30 минут. Модели RVC — из `rvc_from`. Около 2 ГБ ОЗУ и 0,9 ГБ видеопамяти.

### Компонент HA

```
python install/yse_ha_link.py component
```

Кладёт `ha/custom_components/yse_station` в `/homeassistant/custom_components`, вписывает в него адрес сервера, проверяет конфигурацию (`ha core check`) и, только если она проходит, перезапускает HA.

Без SSH к HA: скопировать папку `yse_station` в `config/custom_components/`, в `const.py` вписать `DEFAULT_SERVER = "IP сервера"`, перезапустить HA.

## 3. Колонка кнопкой в HA

1. Сторож раз в минуту проверяет порт 2222 по всей подсети. Через вход root без пароля он ищет `station-midi` или `station-mini2` в `/proc/device-tree/compatible` и `libyandex_evil.so`. Колонки с уже работающей оболочкой и из `skip` пропускает.
2. HA раз в минуту читает `GET http://сервер:18760/v1/found` и на каждую новую колонку показывает «Обнаружено». «Игнорировать» HA помнит.
3. «Подтвердить» → `POST /v1/install`. Принимается только с адреса HA. Сторож выбирает id `st<конец IP>`, имя «Колонка <конец IP>», свободный блок портов и запускает `build` (если ещё не собрано), `speaker`, `host`, `key`.
4. По окончании HA сам добавляет устройство `yse_station` с ключом страницы и голосовой спутник Wyoming.
5. Имя и комнату колонки задают в HA. Комната важна: команды «включи свет», «закрой карниз» идут в комнату той колонки, которая услышала.

Журнал установки — `/storage/yse/jobs/<id>.log`, журнал сторожа — `journalctl -u yse-discover`.

## 4. Колонка вручную

Конфиг колонки — копия `install/speakers/example.json`:

| Поле | Что это |
| --- | --- |
| `id` | короткое имя латиницей, из него имена служб `yse-*@<id>` |
| `name`, `area` | имя и комната |
| `speaker` | IP колонки |
| `host` | IP сервера |
| `base` | первый порт колонки на сервере: слово `base`, музыка `+1`, сбор записей `+2`, ответы `+3`, спутник `+4`, фраза `+5`. У каждой колонки свой, шаг 10 |
| `sat_port` | порт Wyoming для HA, у каждой свой |
| `cutoff` | порог слова, у Станции 2 0.9, у Мини 2 0.85 |
| `defaults` | начальные настройки колонки. Для Мини 2: `"vqe.txt": "yandexmini_2"`, `"capture.txt": "hw:0,1 8"`, `"spotter.txt": "Yandex yandexmini_2"` |

```
python install/yse_station_setup.py build
python install/yse_station_setup.py speaker --conf install/speakers/kitchen.json --yes [--noupdate]
python install/yse_station_setup.py host    --conf install/speakers/kitchen.json --yes
python install/yse_station_setup.py check   --conf install/speakers/kitchen.json
python install/yse_ha_link.py install       --conf install/speakers/kitchen.json
```

`speaker`:

- снимает Quasar с загрузки, его старт хранится в `/data/opt/yandex-station/S99quasar.saved`;
- Станции 2 копирует `soda/` (около 125 МБ) с сервера или с колонки `--from`. Мини 2 вместо этого получает статическое ухо `bin/ear64s`: оно только отдаёт звук фразы серверу, `S99house` запускает его, если файл есть;
- кладёт программы, модель слова, страницу, `S99house`, `S99ystation`, опрос обновлений;
- делает `/usr/bin/aplay` обёрткой 48 кГц (настоящий — `aplay.real`), она же эквалайзер и опора для шумодава;
- пишет настройки по умолчанию и `settings/host.conf` (адрес сервера и `base`);
- создаёт ключ страницы `token` (один раз, не печатается).

Уже работающую колонку `speaker` не трогает. `--upgrade` меняет программы и страницу. `--noupdate` закрывает обновления прошивки Яндекса скриптом с колонки `--from`.

`host` ставит `yse-wake@<id>`, `yse-music@<id>`, `yse-talk@<id>`, `yse-sat@<id>`. Окружение — `/storage/yse/<id>.env`, скрипты — `/storage/yse/app`. Если порт занят, отказывается.

`check` только читает: процессы колонки, `host.conf`, обёртку `aplay`, Quasar, службы и порты сервера.

`yse_ha_link.py install` ставит компонент и сразу добавляет Wyoming (сервер, `sat_port`) и `yse_station` (колонка, 18080, ключ). Повторный запуск дубли отклоняет.

## Ключ страницы

Страница колонки `http://IP_колонки:18080/` без ключа ничего не меняет и ничего не показывает. Ключ — 16 знаков hex в `/data/opt/yandex-station/token` на колонке (права 600), создаётся при установке и повторной установкой не меняется. Он же нужен компоненту HA.

```
python install/yse_station_setup.py key --ip IP_колонки
```

Ключ вставить в поле вверху страницы и нажать «Открыть», браузер его запомнит. Не пересылать в чаты и не класть в git. Если ключ утёк, на колонке:

```
od -An -tx1 -N8 /dev/urandom | tr -d ' \n' > /data/opt/yandex-station/token
chmod 600 /data/opt/yandex-station/token
/etc/init.d/S99ystation restart
```

и ввести новый ключ на странице и в HA.

Один раз страница может забрать ключ сама: на колонке создать пустой `/data/opt/yandex-station/settings/panel.claim`. Первое открытие страницы заберёт ключ и сотрёт файл.

## Первая сеть звуком

Сброшенная колонка ещё не в Wi‑Fi. Телефон в домашней сети открывает `http://сервер:18760/netsound`, вводит имя сети и пароль и играет тон в 20–30 см от микрофонов колонки. Пароль не уходит на сервер, но в тоне он открыт. Код привязки Яндекса можно не вводить. На сброшенной Станции 2 этот путь ещё не проверен. Подробно — [netsound-receiver.md](netsound-receiver.md).

## Что ставится где

На колонке — `/data/opt/yandex-station`:

| Путь | Что |
| --- | --- |
| `bin/` | `housed` (микрофоны, шумодав, кольцо, громкость), `ear64` (фраза после слова), `ystationd` (страница), `alarmd`, `commandd` (команды без слова), `keys`, `eqd`, `glow`, `bthead`, `aplay48`, `ringcat`, `jarvisd`, `yse-update` |
| `www/index.html` | страница |
| `settings/` | все настройки страницы. `*.secret`, `ha.token`, `wifi.psk`, `llm.key` — пароли, страница их не отдаёт |
| `alarms.txt` | будильники |
| `token` | ключ страницы |
| `soda/` | распознавание на Станции 2, необязательно |

На сервере:

| Путь | Что |
| --- | --- |
| `/storage/yse/app` | скрипты служб |
| `/storage/yse/<id>.env` | окружение колонки |
| `/storage/yse/pkg`, `discover.env` | пакет и настройки сторожа |
| `/storage/yse/build/out` | собранные программы для раздачи |
| `/storage/yse/soda` | копия `soda/` для новых Станций 2 |
| `/storage/yse/demo.token` | токен Яндекса для голоса |
| `/storage/yse/music-account.json` | вход в Яндекс Музыку, общий для колонок |
| `/storage/yse/stt`, `/storage/yse/voice` | распознавание и голос |
| `/storage/yse-mww` | модель слова, тренер, записи для дообучения |

Службы: `yse-discover`, `yse-train`, на каждую колонку `yse-wake@`, `yse-music@`, `yse-talk@`, `yse-sat@`.

## Неполадки

| Что | Где смотреть |
| --- | --- |
| Колонка не появилась в HA | `curl http://сервер:18760/v1/found`; `ssh -p 2222 root@IP echo ok` с сервера; колонка не в `skip`; `journalctl -u yse-discover` |
| Установка из HA не удалась | `/storage/yse/jobs/st<NNN>.log` |
| Не откликается на слово | `check --conf …`; `journalctl -u yse-wake@<id>`; модель `/storage/yse-mww/trainer/ru_jarvis_mww.tflite` на месте |
| Слышит слово, но фраза пропадает | на колонке `tail /data/opt/yandex-station/ear.log`, процесс `ear64s` (Мини 2) или `ear64` (Станция 2); на сервере `journalctl -u yse-talk@<id>` |
| Нет голоса ответа | `/storage/yse/demo.token` или вход в Яндекс на странице колонки; `journalctl -u yse-talk@<id>` |
| Команды дома не выполняются | у колонки в HA указана комната; в HA есть устройство с таким именем |
| Музыка не играет | на странице войти в Яндекс заново; `journalctl -u yse-music@<id>` |
| Будильник не звонит вовремя | часовой пояс `settings/clock.off` на колонке |

## Как вернуть колонку к Яндексу

```
mount -o remount,rw /
for s in S99house S99ystation; do /etc/init.d/$s stop; rm -f /etc/init.d/$s; done
cp /usr/bin/aplay.real /usr/bin/aplay
mv /data/opt/yandex-station/S99quasar.saved /etc/init.d/S99quasar
reboot
```

Сервер колонку забудет после `systemctl disable --now yse-{wake,music,talk,sat}@<id>` и удаления `/storage/yse/<id>.env`.
