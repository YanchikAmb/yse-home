/* Штатный распознаватель коротких команд Алисы.
   Модель /system/vendor/quasar/spotter_models/command, один канал 16 кГц
   с выхода шумодава. Слово «джарвис» для этих фраз не нужно.
   Если в settings/wake.txt «алиса» или «яндекс», рядом крутится штатная модель слова
   activation/<alisa|yandex>: срабатывание уходит UDP на base сервера («YSEWAKE <мс>»),
   а раз в 3 с — «YSEMODE <слово>», чтобы сервер не слушал своё «джарвис». */
#define _GNU_SOURCE
#include "clean_ring.h"
#include "host_conf.h"
#include "mini2_led.h"

#include <arpa/inet.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define CHUNK 480

typedef void Error;
typedef void Conf;
typedef void Spotter;
typedef Conf *(*read_fn)(const char *, Error **);
typedef void *(*mcreate_fn)(const char *, long, Error **);
typedef void *(*attach_fn)(void *, Conf *, Error **);
typedef Spotter *(*from_fn)(void *, long, long, int *, long, long, Error **);
typedef int (*submit_fn)(Spotter *, const void *, int, Error **);
typedef int (*event_fn)(Spotter *, Error **);
typedef const char *(*text_fn)(Spotter *, Error **);
typedef const char *(*msg_fn)(Error *);

static volatile sig_atomic_t run = 1;

static void on_stop(int signo) {
    (void)signo;
    run = 0;
}

static void *need(void *lib, const char *name) {
    void *sym = dlsym(lib, name);
    if (!sym) {
        fprintf(stderr, "нет %s\n", name);
        exit(2);
    }
    return sym;
}

static int same(const char *got, const char *word) {
    return got && strcmp(got, word) == 0;
}

static void flash_only(void) {
    if (system(
            "color=$(cat /data/opt/yandex-station/settings/ring.color 2>/dev/null || echo 3aa0ff); "
            "bright=$(cat /data/opt/yandex-station/settings/ring.bright 2>/dev/null || echo 160); "
            "effect=$(cat /data/opt/yandex-station/settings/ring.effect 2>/dev/null || echo wave); "
            "case $effect in wave|breathe|spin|fill|spark|shift|comet|aurora|twinkle) ;; *) effect=wave ;; esac; "
            "/data/opt/yandex-station/bin/glow \"$color\" 450 \"$effect\" \"$bright\" >/dev/null 2>&1 &") != 0) {
        fflush(stderr);
    }
}

static int mixer_now(void) {
    FILE *fp;
    char line[160];
    int value = -1;
    fp = popen("amixer -c 0 sget Master", "r");
    if (!fp) {
        return -1;
    }
    while (fgets(line, sizeof line, fp)) {
        int got = yse_playback_value(line);
        if (got >= 0) {
            value = got;
            break;
        }
    }
    pclose(fp);
    return value;
}

static void apply_percent(int percent) {
    char cmd[96];
    int mixer;
    if (percent < 0) {
        percent = 0;
    }
    if (percent > 100) {
        percent = 100;
    }
    mixer = (percent * 255 + 50) / 100;
    snprintf(cmd, sizeof cmd, "amixer -c 0 set Master %d >/dev/null 2>&1", mixer);
    if (system(cmd) != 0) {
        return;
    }
    fprintf(stderr, "volume %d\n", percent);
    if (system(
            "if [ -f /data/opt/yandex-station/volprev.pid ]; then "
            "kill $(cat /data/opt/yandex-station/volprev.pid) 2>/dev/null; fi; "
            "dev=plughw:0,0; grep -q softvol /etc/asound.conf 2>/dev/null && dev=default; "
            "aplay -D $dev -f S16_LE -r 16000 -c 1 -t raw "
            "/data/opt/yandex-station/volprev.pcm >/dev/null 2>&1 & "
            "echo $! > /data/opt/yandex-station/volprev.pid") != 0) {
        fflush(stderr);
    }
    flash_only();
}

static void player_cmd(const char *name) {
    FILE *fp = fopen("/data/opt/yandex-station/player.cmd", "w");
    if (!fp) {
        return;
    }
    fputs(name, fp);
    fputc('\n', fp);
    fclose(fp);
    fprintf(stderr, "player %s\n", name);
    flash_only();
}

static void music_duck(const char *what) {
    char line[280];
    char url[160];
    /* В фоне: пока wget ходит на сервер, модель не должна ждать. */
    snprintf(line, sizeof line,
             "wget -q -T 2 -O /dev/null --post-data='%s' %s >/dev/null 2>&1 &",
             what, yse_url(url, sizeof url, YSE_MUSIC, "/v1/duck"));
    if (system(line) != 0) {
        fflush(stderr);
    }
}

static void music_hear(void) {
    music_duck("up");
}

static void music_ctrl(const char *cmd) {
    char line[260];
    char url[160];
    snprintf(line, sizeof line,
             "wget -q -T 3 -O /dev/null --post-data='%s' %s",
             cmd, yse_url(url, sizeof url, YSE_MUSIC, "/v1/ctrl"));
    if (system(line) != 0) {
        fflush(stderr);
    }
}

static const char *home_object(const char *text) {
    const char *obj = NULL;
    if (strncmp(text, "включи ", strlen("включи ")) == 0) {
        obj = text + strlen("включи ");
    } else if (strncmp(text, "выключи ", strlen("выключи ")) == 0) {
        obj = text + strlen("выключи ");
    }
    if (!obj) {
        return NULL;
    }
    if (strcmp(obj, "свет") != 0 && strcmp(obj, "телевизор") != 0 && strcmp(obj, "блютуз") != 0) {
        return NULL;
    }
    return text;
}

static void home_ask(const char *phrase) {
    char line[420];
    char url[160];
    /* В фоне: ответ дома и голос не должны держать модель команд. */
    snprintf(line, sizeof line,
             "wget -q -T 20 -O /dev/null --post-data='%s' %s >/dev/null 2>&1 &",
             phrase, yse_url(url, sizeof url, YSE_TALK, "/v1/ask"));
    if (system(line) != 0) {
        fflush(stderr);
    }
    fprintf(stderr, "home %s\n", phrase);
    flash_only();
}

static void music_mark(const char *cmd) {
    char line[260];
    char url[160];
    snprintf(line, sizeof line,
             "wget -q -T 4 -O /dev/null --post-data='%s' %s",
             cmd, yse_url(url, sizeof url, YSE_MUSIC, "/v1/mark"));
    if (system(line) != 0) {
        fflush(stderr);
    }
}

static void stop_ringing(void) {
    FILE *fp = fopen("/data/opt/yandex-station/alarm.stop", "w");
    char pid[24];
    long n;
    if (fp) {
        fclose(fp);
    }
    fp = fopen("/tmp/alarm.play", "r");
    if (!fp) {
        return;
    }
    if (fgets(pid, sizeof pid, fp)) {
        n = strtol(pid, NULL, 10);
        if (n > 1) {
            kill((pid_t)n, SIGTERM);
        }
    }
    fclose(fp);
}

static int alarm_ringing(void) {
    return access("/tmp/alarm.ring", F_OK) == 0;
}

static void act_run(const char *word) {
    int mixer;
    int percent;
    if (same(word, "тише")) {
        music_hear();
        mixer = mixer_now();
        percent = mixer < 0 ? 30 : (mixer * 100 + 127) / 255;
        apply_percent(percent - 10);
        return;
    }
    if (same(word, "громче")) {
        music_hear();
        mixer = mixer_now();
        percent = mixer < 0 ? 30 : (mixer * 100 + 127) / 255;
        apply_percent(percent + 10);
        return;
    }
    if (same(word, "дальше") || same(word, "следующий")) {
        music_ctrl("next");
        player_cmd("next");
        return;
    }
    if (same(word, "назад")) {
        music_ctrl("prev");
        player_cmd("prev");
        return;
    }
    if (same(word, "играй") || same(word, "продолжи")) {
        music_ctrl("play");
        player_cmd("play");
        return;
    }
    if (same(word, "стоп") || same(word, "хватит")) {
        music_ctrl("stop");
        player_cmd("stop");
        stop_ringing();
        return;
    }
    if (same(word, "пауза")) {
        music_ctrl("pause");
        player_cmd("pause");
        stop_ringing();
        return;
    }
    if (same(word, "лайк")) {
        music_mark("like");
        flash_only();
        return;
    }
    if (same(word, "дизлайк")) {
        music_mark("dislike");
        flash_only();
    }
}

/* Слово из модели команд выполняется сразу. Раньше оно ждало 3 секунды и
   пропадало, если ухо уже было открыто. */
static void act(const char *word) {
    static time_t last;
    static char prev[32];
    time_t now = time(NULL);
    if (word[0] == 0) {
        return;
    }
    if (now - last < 1 && strcmp(prev, word) == 0) {
        return;
    }
    last = now;
    snprintf(prev, sizeof prev, "%s", word);
    /* Модель слышит начало речи раньше самого слова. Музыка в этот момент
       приглушается, иначе при громкости выше половины слово до неё не доходит. */
    if (same(word, "<speech_started>")) {
        fprintf(stderr, "hear\n");
        music_duck("down");
        return;
    }
    if (same(word, "<speech_finished>") || same(word, "SIL")) {
        music_duck("up");
        return;
    }
    fprintf(stderr, "phrase %s\n", word);
    if (home_object(word)) {
        home_ask(word);
        return;
    }
    if (alarm_ringing() && same(word, "джарвис")) {
        fprintf(stderr, "alarm stop %s\n", word);
        stop_ringing();
        return;
    }
    if (same(word, "замолчи") || same(word, "отбой")) {
        act_run("стоп");
        return;
    }
    act_run(word);
}

static read_fn conf_read;
static mcreate_fn mcreate;
static attach_fn attach;
static from_fn create;
static submit_fn submit;
static event_fn next_event;
static text_fn phrase;
static msg_fn message;
static char spotter_name[64] = "Yandex yandexmidi";

/* В прошивке Станции 2 файл activation/alisa — модель Мини со словом «джарвис»,
   она не создаётся (в конфиге 1 канал, вызов даёт 0). Рабочая модель — quasar-alisa
   со словом «алиса», как на Мини 2. Смена имени устройства это не лечит. */
static const char *spotter_device(const char *model) {
    char path[160];
    char line[180];
    FILE *fp;
    snprintf(path, sizeof path, "%s/flags.txt", model);
    fp = fopen(path, "r");
    if (!fp) {
        return spotter_name;
    }
    while (fgets(line, sizeof line, fp)) {
        if (strncmp(line, "version ", 8) == 0 && strstr(line, "minispeakers")) {
            fclose(fp);
            return "Yandex yandexmini_2";
        }
    }
    fclose(fp);
    return spotter_name;
}

static Spotter *make_spotter(const char *model, const char *device) {
    Error *err = NULL;
    Conf *conf;
    void *multi;
    Spotter *spot;
    int types[1];
    conf = conf_read(model, &err);
    if (!conf) {
        fprintf(stderr, "модель %s: %s\n", model, (err && message) ? message(err) : "?");
        return NULL;
    }
    err = NULL;
    multi = mcreate(device, 1, &err);
    if (!multi) {
        fprintf(stderr, "имя: %s\n", (err && message) ? message(err) : "?");
        return NULL;
    }
    err = NULL;
    attach(multi, conf, &err);
    if (err) {
        fprintf(stderr, "attach: %s\n", message ? message(err) : "?");
        return NULL;
    }
    types[0] = 0;
    err = NULL;
    spot = create(multi, 16000, 1, types, 1, 0, &err);
    if (!spot) {
        fprintf(stderr, "create: %s\n", (err && message) ? message(err) : "?");
    }
    return spot;
}

static struct {
    Spotter *spot;
    char word[16];
    time_t checked;
    time_t told;
    int sock;
    struct sockaddr_in to;
} wake = {NULL, "", 0, 0, -1, {0}};

static void wake_send(const char *text) {
    if (wake.sock < 0) {
        struct yse_host conf;
        yse_host_load(&conf);
        wake.sock = socket(AF_INET, SOCK_DGRAM, 0);
        memset(&wake.to, 0, sizeof wake.to);
        wake.to.sin_family = AF_INET;
        wake.to.sin_port = htons((unsigned short)conf.base);
        inet_pton(AF_INET, conf.ip, &wake.to.sin_addr);
    }
    if (wake.sock >= 0) {
        sendto(wake.sock, text, strlen(text), 0, (struct sockaddr *)&wake.to, sizeof wake.to);
    }
}

/* Раз в 2 с сверяет settings/wake.txt; штатная модель грузится один раз на слово. */
static void wake_check(void) {
    static Spotter *cache[3];
    char line[64] = "";
    const char *word = "";
    time_t now = time(NULL);
    FILE *fp;
    if (now - wake.checked < 2) {
        return;
    }
    wake.checked = now;
    fp = fopen("/data/opt/yandex-station/settings/wake.txt", "r");
    if (fp) {
        if (!fgets(line, sizeof line, fp)) {
            line[0] = 0;
        }
        fclose(fp);
    }
    line[strcspn(line, "\r\n")] = 0;
    {
        size_t n = strlen(line);
        while (n && (line[n - 1] == ' ' || line[n - 1] == '\t')) {
            line[--n] = 0;
        }
    }
    if (strcmp(line, "алиса") == 0 || strcmp(line, "alisa") == 0) {
        word = "alisa";
    } else if (strcmp(line, "яндекс") == 0 || strcmp(line, "yandex") == 0) {
        word = "yandex";
    } else if (strcmp(line, "джарвис-локально") == 0 || strcmp(line, "джарвис локально") == 0) {
        word = "jarvis";
    }
    if (strcmp(word, wake.word) != 0) {
        int slot = strcmp(word, "alisa") == 0 ? 0 : strcmp(word, "yandex") == 0 ? 1 : 2;
        char model[128];
        wake.spot = NULL;
        snprintf(wake.word, sizeof wake.word, "%s", word);
        if (word[0]) {
            if (!cache[slot]) {
                char cmd[512];
                /* Штатной модели нужен второй канал «сырой микрофон» для частотного фильтра;
                   у нас один очищенный канал, поэтому копия с выключенным фильтром.
                   «джарвис» локально — свой каталог, его не копируют из прошивки. */
                if (strcmp(word, "jarvis") == 0) {
                    snprintf(model, sizeof model, "/data/opt/yandex-station/spotter/jarvis");
                } else {
                    snprintf(model, sizeof model, "/data/opt/yandex-station/spotter/%s", word);
                    snprintf(cmd, sizeof cmd,
                             "d=%s; [ -f $d/flags.txt ] || { mkdir -p $d && "
                             "cp -r /system/vendor/quasar/spotter_models/activation/%s/. $d/ && "
                             "sed -i -e 's/^raw-mic-channels .*/raw-mic-channels 0/' "
                             "-e 's/^freq-filter-enabled .*/freq-filter-enabled false/' $d/flags.txt; }",
                             model, word);
                    if (system(cmd) != 0) {
                        fprintf(stderr, "wake copy %s fail\n", word);
                    }
                }
                if (access(model, R_OK) == 0) {
                    cache[slot] = make_spotter(model, spotter_device(model));
                }
            }
            wake.spot = cache[slot];
        }
        fprintf(stderr, "wake %s %s\n", word[0] ? word : "server", wake.spot ? "on" : "off");
        wake.told = 0;
    }
    if (wake.word[0] && wake.spot && now - wake.told >= 3) {
        char text[48];
        snprintf(text, sizeof text, "YSEMODE %s", wake.word);
        wake_send(text);
        wake.told = now;
    }
}

static void wake_feed(const int16_t *chunk, int bytes) {
    Error *err = NULL;
    int off = 0;
    struct timeval tv;
    static long long last_ms;
    while (off < bytes) {
        int ate;
        err = NULL;
        ate = submit(wake.spot, (const char *)chunk + off, bytes - off, &err);
        if (err) {
            fprintf(stderr, "wake submit: %s\n", message ? message(err) : "?");
            wake.spot = NULL;
            return;
        }
        if (ate <= 0) {
            break;
        }
        off += ate;
    }
    for (;;) {
        int event;
        err = NULL;
        event = next_event(wake.spot, &err);
        if (err || event == 0) {
            return;
        }
        if (event != 2) {
            continue;
        }
        gettimeofday(&tv, NULL);
        {
            long long ms = (long long)tv.tv_sec * 1000 + tv.tv_usec / 1000;
            char text[64];
            const char *said = phrase(wake.spot, &err);
            if (ms - last_ms < 1500) {
                continue;
            }
            last_ms = ms;
            snprintf(text, sizeof text, "YSEWAKE %lld %s", ms, wake.word);
            wake_send(text);
            fprintf(stderr, "wake hit %s\n", said ? said : "?");
        }
    }
}

static struct CleanRing *open_ring(void) {
    int fd;
    struct stat st;
    void *map;
    struct CleanRing *ring;
    fd = open("/data/opt/yandex-station/clean.ring", O_RDONLY);
    if (fd < 0) {
        return NULL;
    }
    if (fstat(fd, &st) != 0 || st.st_size < (off_t)sizeof(struct CleanRing)) {
        close(fd);
        return NULL;
    }
    map = mmap(NULL, sizeof(struct CleanRing), PROT_READ, MAP_SHARED, fd, 0);
    close(fd);
    if (map == MAP_FAILED) {
        return NULL;
    }
    ring = map;
    if (ring->magic != CLEAN_MAGIC || ring->capacity != CLEAN_CAP) {
        munmap(map, sizeof(struct CleanRing));
        return NULL;
    }
    return ring;
}

int main(void) {
    void *lib;
    Error *err = NULL;
    Spotter *spotter;
    struct CleanRing *ring = NULL;
    uint32_t read_pos = 0;
    struct sigaction sa;

    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_stop;
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);
    signal(SIGPIPE, SIG_IGN);
    lib = dlopen("/system/vendor/quasar/libs/libyandex_evil.so", RTLD_NOW);
    if (!lib) {
        fprintf(stderr, "dlopen: %s\n", dlerror());
        return 2;
    }
    conf_read = need(lib, "spotter_conf_read");
    mcreate = need(lib, "spotter_multi_conf_create");
    attach = need(lib, "spotter_multi_conf_attach_spotter_conf");
    create = need(lib, "spotter_create_from_multi_conf");
    submit = need(lib, "spotter_submit_raw_data");
    next_event = need(lib, "spotter_get_next_event");
    phrase = need(lib, "spotter_get_current_phrase");
    message = dlsym(lib, "spotter_error_get_message");
    {
        FILE *name_file = fopen("/data/opt/yandex-station/settings/spotter.txt", "r");
        if (name_file) {
            if (fgets(spotter_name, sizeof spotter_name, name_file)) {
                size_t n = strcspn(spotter_name, "\r\n");
                spotter_name[n] = 0;
                if (n == 0 || strspn(spotter_name,
                        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_ ") != n) {
                    snprintf(spotter_name, sizeof spotter_name, "Yandex yandexmidi");
                }
            }
            fclose(name_file);
        }
    }
    spotter = make_spotter("/system/vendor/quasar/spotter_models/command", spotter_name);
    if (!spotter) {
        return 1;
    }
    fprintf(stderr, "command listening\n");
    fflush(stderr);
    while (run && !ring) {
        ring = open_ring();
        if (!ring) {
            usleep(200000);
        }
    }
    if (!ring) {
        return 0;
    }
    __sync_synchronize();
    read_pos = ring->write_pos;
    while (run) {
        uint32_t w;
        uint32_t avail;
        uint32_t i;
        int16_t chunk[CHUNK];
        int ate;
        w = ring->write_pos;
        __sync_synchronize();
        avail = w - read_pos;
        if (avail > CLEAN_CAP) {
            read_pos = w;
            usleep(20000);
            continue;
        }
        if (avail < CHUNK) {
            usleep(20000);
            continue;
        }
        for (i = 0; i < CHUNK; i++) {
            chunk[i] = ring->pcm[(read_pos + i) & (CLEAN_CAP - 1u)];
        }
        err = NULL;
        ate = submit(spotter, chunk, (int)sizeof chunk, &err);
        if (err) {
            fprintf(stderr, "submit: %s\n", message ? message(err) : "?");
            break;
        }
        if (ate <= 0 || ate > (int)sizeof chunk) {
            ate = (int)sizeof chunk;
        }
        read_pos += (uint32_t)ate / 2u;
        wake_check();
        if (wake.spot) {
            wake_feed(chunk, ate);
        }
        for (;;) {
            int event;
            const char *text;
            char word[64];
            err = NULL;
            event = next_event(spotter, &err);
            if (err) {
                fprintf(stderr, "event: %s\n", message ? message(err) : "?");
                run = 0;
                break;
            }
            if (event == 0) {
                break;
            }
            if (event != 2) {
                continue;
            }
            err = NULL;
            text = phrase(spotter, &err);
            if (!text) {
                continue;
            }
            snprintf(word, sizeof word, "%s", text);
            act(word);
        }
    }
    munmap(ring, sizeof(struct CleanRing));
    return 0;
}
