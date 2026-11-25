#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dirent.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "clean_ring.h"
#include "host_conf.h"
#include "mini2_led.h"

static char wake_word[80] = "джарвис";
static volatile sig_atomic_t run = 1;
static int heard;
static time_t block_until;
static time_t light_until;
static char last_hear[480];
static char committed[400];
static int text_only;

static void publish_hear(const char *text) {
    int fd;
    size_t n = strlen(text);
    fd = open("/data/opt/yandex-station/heard.txt.tmp", O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) {
        return;
    }
    if (n > 0 && write(fd, text, n) != (ssize_t)n) {
        close(fd);
        return;
    }
    if (close(fd) != 0) {
        return;
    }
    rename("/data/opt/yandex-station/heard.txt.tmp", "/data/opt/yandex-station/heard.txt");
}

static void beep(void);

static void on_stop(int signo) {
    (void)signo;
    run = 0;
}

static int put_varint(uint8_t *out, uint32_t value) {
    int n = 0;
    while (value > 127u) {
        out[n++] = (uint8_t)((value & 127u) | 128u);
        value >>= 7;
    }
    out[n++] = (uint8_t)value;
    return n;
}

static int put_key(uint8_t *out, int field, int wire) {
    return put_varint(out, (uint32_t)((field << 3) | wire));
}

static int build_config(uint8_t *out, int cap, const char *model_dir) {
    int n = 0;
    int len = (int)strlen(model_dir);
    const char *key = "dummy_api_key";
    int key_len = (int)strlen(key);
    const char *cfg = "/data/opt/yandex-station/soda/model/configs/ONDEVICE_MEDIUM_SHORT.config";
    int cfg_len = (int)strlen(cfg);
    if (cap < len + key_len + cfg_len + 48) {
        return -1;
    }
    n += put_key(out + n, 1, 0);
    n += put_varint(out + n, 1);
    n += put_key(out + n, 2, 0);
    n += put_varint(out + n, 16000);
    n += put_key(out + n, 5, 2);
    n += put_varint(out + n, (uint32_t)cfg_len);
    memcpy(out + n, cfg, (size_t)cfg_len);
    n += cfg_len;
    n += put_key(out + n, 6, 2);
    n += put_varint(out + n, (uint32_t)key_len);
    memcpy(out + n, key, (size_t)key_len);
    n += key_len;
    n += put_key(out + n, 7, 2);
    n += put_varint(out + n, (uint32_t)len);
    memcpy(out + n, model_dir, (size_t)len);
    n += len;
    n += put_key(out + n, 8, 0);
    n += put_varint(out + n, 1);
    return n;
}

static void load_wake(const char *path) {
    char buf[80];
    int fd = open(path, O_RDONLY);
    ssize_t n;
    if (fd < 0) {
        return;
    }
    n = read(fd, buf, sizeof buf - 1);
    close(fd);
    if (n <= 0) {
        return;
    }
    while (n > 0 && (buf[n - 1] == '\n' || buf[n - 1] == '\r' || buf[n - 1] == ' ')) {
        n--;
    }
    buf[n] = 0;
    if (n > 0) {
        memcpy(wake_word, buf, (size_t)n + 1);
    }
}

static int same_word(const uint8_t *data, int len, const char *word) {
    size_t wlen = strlen(word);
    size_t i;
    if (wlen == 0 || len < (int)wlen) {
        return 0;
    }
    for (i = 0; i + wlen <= (size_t)len; i++) {
        size_t k;
        int match = 1;
        for (k = 0; k < wlen; k++) {
            unsigned char a = data[i + k];
            unsigned char b = (unsigned char)word[k];
            if (a == b) {
                continue;
            }
            if (k == 1 && data[i] == 0xd0 && a + 0x20 == b) {
                continue;
            }
            match = 0;
            break;
        }
        if (match) {
            return 1;
        }
    }
    return 0;
}

static int ring_fd = -1;

static void paint_ring(int on) {
    unsigned char buf[89 * 4];
    if (mini2_present()) {
        mini2_show(on ? 255 : 0, on ? 255 : 0, on ? 255 : 0);
        return;
    }
    if (ring_fd < 0) {
        ring_fd = open("/dev/fb0", O_RDWR);
    }
    if (ring_fd < 0) {
        return;
    }
    memset(buf, on ? 0xff : 0x00, sizeof buf);
    if (lseek(ring_fd, 0, SEEK_SET) < 0) {
        return;
    }
    if (write(ring_fd, buf, sizeof buf) < 0) {
        /* the ring stays as it was */
    }
}

static void light_cmd(const char *name) {
    char line[40];
    int n;
    int fd;
    paint_ring(strcmp(name, "listening") == 0);
    n = snprintf(line, sizeof line, "led %s\n", name);
    if (n <= 0 || n >= (int)sizeof line) {
        return;
    }
    fd = open("/data/opt/yandex-station/house.cmd", O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) {
        return;
    }
    if (write(fd, line, (size_t)n) < 0) {
        /* the ring command is best-effort */
    }
    close(fd);
}

static int strip_zh(const char *s, char *out, size_t cap) {
    size_t n = 0;
    size_t i = 0;
    while (s[i] && n + 1 < cap) {
        if ((unsigned char)s[i] == 0xd0 && ((unsigned char)s[i + 1] == 0xb6 || (unsigned char)s[i + 1] == 0x96)) {
            i += 2;
            continue;
        }
        out[n++] = s[i++];
    }
    out[n] = 0;
    return (int)n;
}

static int prefix_eq(const char *token, const char *word, size_t min_bytes) {
    size_t tlen = strlen(token);
    size_t wlen = strlen(word);
    size_t i;
    if (tlen < min_bytes || (tlen % 2) != 0 || tlen > wlen) {
        return 0;
    }
    for (i = 0; i < tlen; i++) {
        unsigned char a = (unsigned char)token[i];
        unsigned char b = (unsigned char)word[i];
        if (a == b) {
            continue;
        }
        if (i == 1 && (unsigned char)token[0] == 0xd0 && (unsigned char)(a + 0x20) == b) {
            continue;
        }
        return 0;
    }
    return 1;
}

static int has_core(const char *token) {
    char buf[80];
    size_t n = 0;
    size_t i = 0;
    static const unsigned char core[] = {0xd0, 0xb0, 0xd1, 0x80, 0xd0, 0xb2, 0xd0, 0xb8};
    if (strlen(token) < 8) {
        return 0;
    }
    while (token[i] && n + 2 < sizeof buf) {
        buf[n++] = token[i];
        if ((unsigned char)token[i] == 0xd0 && (unsigned char)token[i + 1] == 0xb1) {
            buf[n - 1] = (char)0xd0;
            buf[n++] = (char)0xb2;
            i += 2;
            continue;
        }
        if ((unsigned char)token[i] == 0xd0 && (unsigned char)token[i + 1] == 0x91) {
            buf[n - 1] = (char)0xd0;
            buf[n++] = (char)0x92;
            i += 2;
            continue;
        }
        i++;
    }
    buf[n] = 0;
    if (n < sizeof core) {
        return 0;
    }
    for (i = 0; i + sizeof core <= n; i++) {
        if (memcmp(buf + i, core, sizeof core) == 0) {
            return 1;
        }
    }
    return 0;
}

static int word_hit(const char *token) {
    char token_norm[80];
    char word_norm[80];
    if (prefix_eq(token, wake_word, 6)) {
        return 1;
    }
    strip_zh(token, token_norm, sizeof token_norm);
    strip_zh(wake_word, word_norm, sizeof word_norm);
    if (prefix_eq(token_norm, word_norm, 8)) {
        return 1;
    }
    if (((unsigned char)wake_word[0] == 0xd0 || (unsigned char)wake_word[0] == 0xd1) && wake_word[1] &&
        prefix_eq(token, wake_word + 2, 6)) {
        return 1;
    }
    return has_core(token);
}

static int cyr_ok(unsigned char a, unsigned char b) {
    if (a == 0xd0) {
        return b == 0x81 || (b >= 0x90 && b <= 0xbf);
    }
    if (a == 0xd1) {
        return (b >= 0x80 && b <= 0x8f) || b == 0x91;
    }
    return 0;
}

static int read_varint(const uint8_t *p, int len, int *used, uint32_t *value) {
    uint32_t v = 0;
    int shift = 0;
    int i = 0;
    while (i < len && i < 5) {
        uint8_t b = p[i++];
        v |= (uint32_t)(b & 127u) << shift;
        if ((b & 128u) == 0) {
            *used = i;
            *value = v;
            return 1;
        }
        shift += 7;
    }
    return 0;
}

static int take_hyp(const uint8_t *s, int n, char *out, int cap) {
    int i;
    int letters = 0;
    if (n <= 0 || cap < 2) {
        return 0;
    }
    if (n >= cap) {
        n = cap - 1;
    }
    for (i = 0; i + 1 < n; i++) {
        if (cyr_ok(s[i], s[i + 1])) {
            letters++;
            i++;
        }
    }
    if (letters < 1) {
        return 0;
    }
    memcpy(out, s, (size_t)n);
    out[n] = 0;
    return letters;
}

static int best_phrase(const uint8_t *data, int len, char *out, int cap, int *final_result) {
    int i = 0;
    *final_result = 0;
    out[0] = 0;
    while (i < len) {
        int used = 0;
        uint32_t key = 0;
        uint32_t n = 0;
        int field;
        int wire;
        if (!read_varint(data + i, len - i, &used, &key)) {
            return 0;
        }
        i += used;
        field = (int)(key >> 3);
        wire = (int)(key & 7u);
        if (wire == 0) {
            if (!read_varint(data + i, len - i, &used, &n)) {
                return 0;
            }
            i += used;
            continue;
        }
        if (wire == 5) {
            i += 4;
            continue;
        }
        if (wire == 1) {
            i += 8;
            continue;
        }
        if (wire != 2 || !read_varint(data + i, len - i, &used, &n) || i + used + (int)n > len) {
            return 0;
        }
        i += used;
        if (field == 2) {
            const uint8_t *msg = data + i;
            int mlen = (int)n;
            int j = 0;
            int got = 0;
            while (j < mlen) {
                uint32_t inner = 0;
                uint32_t ln = 0;
                int in_field;
                int in_wire;
                if (!read_varint(msg + j, mlen - j, &used, &inner)) {
                    break;
                }
                j += used;
                in_field = (int)(inner >> 3);
                in_wire = (int)(inner & 7u);
                if (in_wire == 0) {
                    if (!read_varint(msg + j, mlen - j, &used, &ln)) {
                        break;
                    }
                    j += used;
                    if (in_field == 2 && ln == 2u) {
                        *final_result = 1;
                    }
                    continue;
                }
                if (in_wire == 5) {
                    j += 4;
                    continue;
                }
                if (in_wire == 1) {
                    j += 8;
                    continue;
                }
                if (in_wire != 2 || !read_varint(msg + j, mlen - j, &used, &ln) || j + used + (int)ln > mlen) {
                    break;
                }
                j += used;
                if (in_field == 1 && !got) {
                    got = take_hyp(msg + j, (int)ln, out, cap);
                }
                j += (int)ln;
            }
            return got;
        }
        i += (int)n;
    }
    return 0;
}

static void show_phrase(const char *extra) {
    char line[480];
    int n = 0;
    size_t a = strlen(committed);
    size_t b = extra ? strlen(extra) : 0;
    if (a + b + 2 >= sizeof line) {
        return;
    }
    if (a > 0) {
        memcpy(line, committed, a);
        n = (int)a;
    }
    if (b > 0) {
        if (n > 0) {
            line[n++] = ' ';
        }
        memcpy(line + n, extra, b);
        n += (int)b;
    }
    line[n] = 0;
    if (n < 2 || strcmp(line, last_hear) == 0) {
        return;
    }
    snprintf(last_hear, sizeof last_hear, "%s", line);
    publish_hear(line);
    printf("hear %s\n", line);
    fflush(stdout);
}

static void lower_ru(char *s);

static void bare_letters(const char *in, char *out, size_t cap) {
    char tmp[480];
    size_t i;
    size_t n = 0;
    snprintf(tmp, sizeof tmp, "%s", in);
    lower_ru(tmp);
    for (i = 0; tmp[i] && n + 2 < cap; i++) {
        unsigned char c = (unsigned char)tmp[i];
        if ((c == 0xd0 || c == 0xd1) && tmp[i + 1]) {
            out[n++] = tmp[i];
            out[n++] = tmp[++i];
        } else if ((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')) {
            out[n++] = (char)c;
        }
    }
    out[n] = 0;
}

static void commit_phrase(const char *text) {
    size_t n = strlen(committed);
    size_t t = strlen(text);
    char have[480];
    char got[480];
    if (t == 0 || n + t + 2 >= sizeof committed) {
        return;
    }
    bare_letters(committed, have, sizeof have);
    bare_letters(text, got, sizeof got);
    if (!got[0] || strstr(have, got)) {
        return;
    }
    if (have[0] && strncmp(got, have, strlen(have)) == 0) {
        snprintf(committed, sizeof committed, "%s", text);
        return;
    }
    if (n > 0) {
        committed[n++] = ' ';
    }
    memcpy(committed + n, text, t + 1);
}

static void lower_ru(char *s) {
    unsigned char *p = (unsigned char *)s;
    while (p[0]) {
        if (p[0] == 0xd0 && p[1]) {
            if (p[1] == 0x81) {
                p[0] = 0xd1;
                p[1] = 0x91;
            } else if (p[1] >= 0x90 && p[1] <= 0x9f) {
                p[1] = (unsigned char)(p[1] + 0x20);
            } else if (p[1] >= 0xa0 && p[1] <= 0xaf) {
                p[0] = 0xd1;
                p[1] = (unsigned char)(p[1] - 0x20);
            }
            p += 2;
            continue;
        }
        if (p[0] == 0xd1 && p[1]) {
            p += 2;
            continue;
        }
        if (*p >= 'A' && *p <= 'Z') {
            *p = (unsigned char)(*p - 'A' + 'a');
        }
        p++;
    }
}

static int has_word(const char *s, const char *word) {
    size_t n = strlen(word);
    const char *p = s;
    while ((p = strstr(p, word)) != NULL) {
        int before = p == s || p[-1] == ' ';
        char after = p[n];
        if (before && (after == 0 || after == ' ' || after == ',' || after == '.' || after == '!')) {
            return 1;
        }
        p += n;
    }
    return 0;
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
    printf("cmd volume %d\n", percent);
    fflush(stdout);
    if (system(
            "if [ -f /data/opt/yandex-station/volprev.pid ]; then "
            "kill $(cat /data/opt/yandex-station/volprev.pid) 2>/dev/null; fi; "
            "dev=plughw:0,0; grep -q softvol /etc/asound.conf 2>/dev/null && dev=default; "
            "aplay -D $dev -f S16_LE -r 16000 -c 1 -t raw "
            "/data/opt/yandex-station/volprev.pcm >/dev/null 2>&1 & "
            "echo $! > /data/opt/yandex-station/volprev.pid; "
            "color=$(cat /data/opt/yandex-station/settings/ring.color 2>/dev/null || echo 3aa0ff); "
            "bright=$(cat /data/opt/yandex-station/settings/ring.bright 2>/dev/null || echo 160); "
            "effect=$(cat /data/opt/yandex-station/settings/ring.effect 2>/dev/null || echo wave); "
            "case $effect in wave|breathe|spin|fill|spark|shift|comet|aurora|twinkle) ;; *) effect=wave ;; esac; "
            "/data/opt/yandex-station/bin/glow \"$color\" 450 \"$effect\" \"$bright\" >/dev/null 2>&1 &") != 0) {
        fflush(stdout);
    }
}

static void flash_only(void) {
    if (system(
            "color=$(cat /data/opt/yandex-station/settings/ring.color 2>/dev/null || echo 3aa0ff); "
            "bright=$(cat /data/opt/yandex-station/settings/ring.bright 2>/dev/null || echo 160); "
            "effect=$(cat /data/opt/yandex-station/settings/ring.effect 2>/dev/null || echo wave); "
            "case $effect in wave|breathe|spin|fill|spark|shift|comet|aurora|twinkle) ;; *) effect=wave ;; esac; "
            "/data/opt/yandex-station/bin/glow \"$color\" 450 \"$effect\" \"$bright\" >/dev/null 2>&1 &") != 0) {
        fflush(stdout);
    }
}

static int clock_alarm(const char *s, time_t now, time_t *when) {
    const char *p = s;
    while ((p = strstr(p, "на ")) != NULL) {
        int hour;
        int minute = 0;
        const char *num;
        p += strlen("на ");
        if (*p < '0' || *p > '9') {
            continue;
        }
        hour = atoi(p);
        num = p;
        while (*num >= '0' && *num <= '9') {
            num++;
        }
        if (*num == ':') {
            minute = atoi(num + 1);
        } else if (*num == ' ' && num[1] >= '0' && num[1] <= '9') {
            minute = atoi(num + 1);
        }
        if (hour < 0 || hour > 23 || minute < 0 || minute > 59) {
            continue;
        }
        if ((strstr(s, "вечер") || strstr(s, "дня")) && hour < 12) {
            hour += 12;
        }
        if (strstr(s, "ноч") && hour == 12) {
            hour = 0;
        }
        {
            time_t local = now + 3 * 3600;
            time_t midnight = local - (local % 86400);
            time_t local_when = midnight + hour * 3600 + minute * 60;
            if (local_when <= local) {
                local_when += 86400;
            }
            *when = local_when - 3 * 3600;
        }
        return 1;
    }
    return 0;
}

static int duration_alarm(const char *s, time_t now, time_t *when) {
    const char *p;
    int n;
    if (strstr(s, "полчаса")) {
        *when = now + 1800;
        return 1;
    }
    p = strstr(s, "через ");
    if (p) {
        n = atoi(p + strlen("через "));
        if (strstr(p, "час")) {
            if (n <= 0) {
                n = 1;
            }
            *when = now + (time_t)n * 3600;
            return 1;
        }
        if (n > 0 && strstr(p, "мин")) {
            *when = now + (time_t)n * 60;
            return 1;
        }
    }
    p = strstr(s, "минут");
    if (p) {
        const char *q = s;
        n = 0;
        while (q < p) {
            if (*q >= '0' && *q <= '9') {
                n = atoi(q);
                while (q < p && *q >= '0' && *q <= '9') {
                    q++;
                }
            } else {
                q++;
            }
        }
        if (n > 0 && n < 24 * 60) {
            *when = now + (time_t)n * 60;
            return 1;
        }
    }
    return 0;
}

static void save_alarm(time_t when) {
    FILE *fp = fopen("/data/opt/yandex-station/alarms.txt", "a");
    if (!fp) {
        return;
    }
    fprintf(fp, "%lld\n", (long long)when);
    fclose(fp);
    unlink("/data/opt/yandex-station/alarm.stop");
    printf("alarm %lld\n", (long long)when);
    fflush(stdout);
    flash_only();
}

static void clear_alarms(void) {
    FILE *fp = fopen("/data/opt/yandex-station/alarms.txt", "w");
    if (fp) {
        fclose(fp);
    }
    {
        FILE *stop = fopen("/data/opt/yandex-station/alarm.stop", "w");
        if (stop) {
            fclose(stop);
        }
    }
    printf("alarm clear\n");
    fflush(stdout);
    flash_only();
}

static void music_say(const char *path, const char *text) {
    char body[120];
    char cmd[360];
    size_t i;
    size_t n = 0;
    if (!text) {
        return;
    }
    while (*text == ' ') {
        text++;
    }
    for (i = 0; text[i] && n + 1 < sizeof body; i++) {
        if (text[i] == '\'' || text[i] == '\n' || text[i] == '\r' || text[i] == '\\') {
            continue;
        }
        body[n++] = text[i];
    }
    body[n] = 0;
    if (!body[0]) {
        return;
    }
    {
        char url[160];
        snprintf(cmd, sizeof cmd,
                 "wget -q -T 12 -O /dev/null --post-data='%s' %s",
                 body, yse_url(url, sizeof url, YSE_MUSIC, path));
    }
    if (system(cmd) != 0) {
        fflush(stdout);
    }
}

static const char *after_word(const char *text, const char *word) {
    const char *p = strstr(text, word);
    if (!p) {
        return "";
    }
    p += strlen(word);
    while (*p == ' ') {
        p++;
    }
    return p;
}

static int mine_word(const char *name) {
    size_t n = 0;
    if (strncmp(name, "мою", 3) == 0 || strncmp(name, "мой", 3) == 0 || strncmp(name, "моя", 3) == 0) {
        n = 3;
    } else if (strncmp(name, "мне", 3) == 0) {
        n = 3;
    }
    return n && (name[n] == 0 || name[n] == ' ' || name[n] == '.' || name[n] == ',');
}

static const char *skip_mine(const char *name) {
    while (*name == ' ' || *name == '.' || *name == ',') {
        name++;
    }
    if (mine_word(name)) {
        name += 3;
        while (*name == ' ' || *name == '.' || *name == ',') {
            name++;
        }
        if (*name) {
            return skip_mine(name);
        }
        return name;
    }
    return name;
}

static int play_request(const char *buf) {
    const char *p;
    if (strstr(buf, "свет") || strstr(buf, "телевизор") || strstr(buf, "блютуз")) {
        return 0;
    }
    p = strstr(buf, "включи");
    if (!p) {
        p = strstr(buf, "поставь");
    }
    if (!p) {
        p = strstr(buf, "запусти");
    }
    if (!p) {
        return 0;
    }
    while (*p && *p != ' ') {
        p++;
    }
    while (*p == ' ') {
        p++;
    }
    return strlen(p) >= 3;
}

static int took_action;
static int alarm_wait;

static void talk_say(const char *path, const char *body) {
    char url[160];
    char cmd[360];
    snprintf(cmd, sizeof cmd, "wget -q -T 30 -O /dev/null --post-data='%s' %s >/dev/null 2>&1 &",
             body, yse_url(url, sizeof url, YSE_TALK, path));
    if (system(cmd) != 0) {
        printf("say fail\n");
        fflush(stdout);
    }
}

static const char *nn_brain(void) {
    FILE *file = fopen("/data/opt/yandex-station/settings/nn.txt", "r");
    char buf[16];
    size_t n;
    if (!file) {
        return NULL;
    }
    if (!fgets(buf, sizeof buf, file)) {
        fclose(file);
        return NULL;
    }
    fclose(file);
    n = strcspn(buf, "\r\n");
    buf[n] = 0;
    if (strcmp(buf, "yandex") == 0) {
        return "yandex";
    }
    if (strcmp(buf, "ha") == 0) {
        return "ha";
    }
    if (strcmp(buf, "openai") == 0) {
        return "openai";
    }
    return NULL;
}

static int ask_cloud(const char *text, int follow) {
    char low[400];
    char body[400];
    char cmd[700];
    char reply[128];
    size_t i;
    size_t n = 0;
    size_t skip = 0;
    size_t word = strlen(wake_word);
    FILE *pipe;
    int ok = 0;
    const char *brain = nn_brain();
    if (!text || !brain) {
        return 0;
    }
    while (*text == ' ') {
        text++;
    }
    snprintf(low, sizeof low, "%s", text);
    lower_ru(low);
    if (word > 0 && strncmp(low, wake_word, word) == 0) {
        skip = word;
        while (text[skip] == ' ' || text[skip] == ',' || text[skip] == '.') {
            skip++;
        }
    }
    text += skip;
    for (i = 0; text[i] && n + 1 < sizeof body; i++) {
        if (text[i] == '\'' || text[i] == '\n' || text[i] == '\r' || text[i] == '\\') {
            continue;
        }
        body[n++] = text[i];
    }
    body[n] = 0;
    if (n < 2) {
        return 0;
    }
    {
        char url[160];
        char path[48];
        snprintf(path, sizeof path, "/v1/ask?nn=%s%s", brain, follow ? "&follow=1" : "");
        snprintf(cmd, sizeof cmd,
                 "wget -q -T 90 -O - --post-data='%s' '%s' 2>/dev/null",
                 body, yse_url(url, sizeof url, YSE_TALK, path));
    }
    printf("ask %s%s\n", brain, follow ? " follow" : "");
    fflush(stdout);
    pipe = popen(cmd, "r");
    if (!pipe) {
        return 0;
    }
    while (fgets(reply, sizeof reply, pipe)) {
        if (strstr(reply, "\"ok\":true")) {
            ok = 1;
        }
    }
    pclose(pipe);
    return ok;
}

static int act_on(const char *text, int final) {
    char buf[160];
    time_t now = time(NULL);
    static time_t last_act;
    static char last_key[32];
    time_t when = 0;
    int act = 0;
    const char *key = NULL;
    if (!text || !text[0]) {
        return 0;
    }
    snprintf(buf, sizeof buf, "%s", text);
    lower_ru(buf);
    if (has_word(buf, "отмени будильник") || has_word(buf, "убери будильник")) {
        act = 1;
        key = "aoff";
    } else if (strstr(buf, "будильник") || strstr(buf, "разбуди")) {
        act = 2;
        key = "alarm";
    } else if (strstr(buf, "выключи звук") || has_word(buf, "без звука")) {
        act = 5;
        key = "mute";
    } else if (strstr(buf, "громкост")) {
        act = 6;
        key = "level";
    } else if (strstr(buf, "музык")) {
        const char *name = strstr(buf, "музыку") ? after_word(buf, "музыку") : after_word(buf, "музыка");
        if (skip_mine(name)[0]) {
            act = 11;
            key = "who";
        } else {
            act = 12;
            key = "list";
        }
    } else if (strstr(buf, "плейлист") || strstr(buf, "волн")) {
        act = 12;
        key = "list";
    } else if (has_word(buf, "дизлайк") || strstr(buf, "не нравится")) {
        act = 14;
        key = "down";
    } else if (has_word(buf, "лайк") || has_word(buf, "нравится")) {
        act = 13;
        key = "up";
    } else if (final && play_request(buf)) {
        act = 15;
        key = "ask";
    }
    if (!act || !key) {
        return 0;
    }
    if (act == 2 && !final) {
        if (!(now - last_act < 20 && strcmp(last_key, "alarm") == 0)) {
            alarm_wait = 1;
        }
        return 1;
    }
    if (act == 6) {
        const char *p = strstr(buf, "громкост");
        int level = -1;
        char tag[32];
        while (p && *p && (*p < '0' || *p > '9')) {
            p++;
        }
        if (p && *p) {
            level = atoi(p);
        }
        if (level < 0 || level > 100) {
            return 0;
        }
        snprintf(tag, sizeof tag, "level-%d", level);
        if (now - last_act < 2 && strcmp(last_key, tag) == 0) {
            return 1;
        }
        last_act = now;
        snprintf(last_key, sizeof last_key, "%s", tag);
        apply_percent(level);
        return 1;
    }
    if ((now - last_act < 2 || (act == 2 && now - last_act < 20)) && strcmp(last_key, key) == 0) {
        return 1;
    }
    last_act = now;
    snprintf(last_key, sizeof last_key, "%s", key);
    if (act == 1) {
        clear_alarms();
        talk_say("/v1/alarm", "clear");
        return 1;
    }
    if (act == 2) {
        char stamp[24];
        alarm_wait = 0;
        if ((strstr(buf, "через") || strstr(buf, "минут") || strstr(buf, "полчаса") || strstr(buf, "час")) &&
            duration_alarm(buf, now, &when)) {
            save_alarm(when);
        } else if (clock_alarm(buf, now, &when)) {
            save_alarm(when);
        } else {
            last_key[0] = 0;
            talk_say("/v1/alarm", "none");
            return 1;
        }
        snprintf(stamp, sizeof stamp, "%lld", (long long)when);
        talk_say("/v1/alarm", stamp);
        return 1;
    }
    if (act == 5) {
        apply_percent(0);
        return 1;
    }
    if (act == 11) {
        const char *name = strstr(buf, "музыку") ? after_word(buf, "музыку") : after_word(buf, "музыка");
        music_say("/v1/who", skip_mine(name));
        flash_only();
        return 1;
    }
    if (act == 12 || act == 15) {
        flash_only();
        music_say("/v1/playlist", buf);
        return 1;
    }
    if (act == 13) {
        music_say("/v1/mark", "like");
        flash_only();
        return 1;
    }
    if (act == 14) {
        music_say("/v1/mark", "dislike");
        flash_only();
        return 1;
    }
    return 0;
}

static time_t duck_at;

static void duck_hold(void) {
    time_t now = time(NULL);
    char url[160];
    char cmd[260];
    if (now - duck_at < 3) {
        return;
    }
    duck_at = now;
    snprintf(cmd, sizeof cmd, "wget -q -T 3 -O /dev/null --post-data=down %s >/dev/null 2>&1 &",
             yse_url(url, sizeof url, YSE_MUSIC, "/v1/duck"));
    if (system(cmd) != 0) {
        fflush(stdout);
    }
}

static void on_audio(const uint8_t *data, int len, void *handle) {
    char text[160];
    int j = 0;
    int i;
    int letters = 0;
    (void)handle;
    if (text_only) {
        char hyp[160];
        int final_result = 0;
        if (best_phrase(data, len, hyp, (int)sizeof hyp, &final_result) >= 1) {
            if (!took_action) {
                duck_hold();
            }
            if (act_on(hyp, final_result)) {
                took_action = 1;
            }
            if (final_result) {
                commit_phrase(hyp);
                show_phrase(NULL);
            } else {
                show_phrase(hyp);
            }
        } else if (len >= 16) {
            char hex[160];
            int n = len < 40 ? len : 40;
            int p = 0;
            int k;
            for (k = 0; k < n && p + 3 < (int)sizeof hex; k++) {
                p += snprintf(hex + p, sizeof hex - (size_t)p, "%02x", data[k]);
            }
            printf("ear cb %d %s\n", len, hex);
            fflush(stdout);
        }
        return;
    }
    for (i = 0; i < len && j < (int)sizeof text - 3; i++) {
        unsigned char c = data[i];
        if ((c == 0xd0 || c == 0xd1) && i + 1 < len) {
            text[j++] = (char)c;
            text[j++] = (char)data[++i];
            letters++;
        } else if (letters > 0 && j > 0 && text[j - 1] != ' ') {
            text[j++] = ' ';
        }
    }
    while (j > 0 && text[j - 1] == ' ') {
        j--;
    }
    text[j] = 0;
    if (text_only) {
        if (letters >= 1 && strcmp(text, last_hear) != 0) {
            snprintf(last_hear, sizeof last_hear, "%s", text);
            publish_hear(text);
            printf("hear %s\n", text);
            fflush(stdout);
        }
        return;
    }
    {
        int hit = same_word(data, len, wake_word);
        if (!hit && letters >= 3) {
            char *p = text;
            while (*p) {
                char *sp = strchr(p, ' ');
                if (sp) {
                    *sp = 0;
                }
                if (word_hit(p)) {
                    hit = 1;
                }
                if (!sp) {
                    break;
                }
                *sp = ' ';
                p = sp + 1;
            }
        }
        if (hit) {
            time_t now = time(NULL);
            light_until = now + 8;
            if (now >= block_until) {
                block_until = now + 2;
                heard = 1;
                light_cmd("listening");
                beep();
                printf("wake %s\n", wake_word);
                fflush(stdout);
            }
        }
    }
    if (letters >= 3 && strcmp(text, last_hear) != 0) {
        snprintf(last_hear, sizeof last_hear, "%s", text);
        printf("hear %s\n", text);
        fflush(stdout);
    }
}

static void beep(void) {
    pid_t pid = fork();
    int volume = 26;
    char line[160];
    int16_t tone[6400];
    int pipes[2] = {-1, -1};
    int i;
    if (pid != 0) {
        return;
    }
    if (system("amixer -c 0 set Master 110 >/dev/null 2>&1") != 0) {
        /* the chirp still plays at the current volume */
    }
    for (i = 0; i < 6400; i++) {
        int on = ((i * 880) / 8000) & 1;
        int amp = i < 6000 ? 22000 : 22000 * (6400 - i) / 400;
        tone[i] = (int16_t)(on ? amp : -amp);
    }
    if (pipe(pipes) == 0 && fork() == 0) {
        int fd = open("/dev/null", O_WRONLY);
        dup2(pipes[0], 0);
        if (fd >= 0) {
            dup2(fd, 1);
            dup2(fd, 2);
        }
        close(pipes[1]);
        execlp("aplay", "aplay", "-D", "plughw:0,0", "-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "raw", "-", (char *)NULL);
        _exit(1);
    }
    if (pipes[1] >= 0) {
        close(pipes[0]);
        if (write(pipes[1], tone, sizeof tone) < 0) {
            /* the player may have exited */
        }
        close(pipes[1]);
        waitpid(-1, NULL, 0);
    }
    snprintf(line, sizeof line, "amixer -c 0 set Master %d >/dev/null 2>&1", volume > 0 ? volume : 26);
    if (system(line) != 0) {
        /* leave the mixer where the chirp set it */
    }
    _exit(0);
}

static long long first_wait_ms = 7000;
static long long skip_ms = 1200;

static long long now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static void follow_ring(void *handle, void (*add)(void *, const void *, int), void (*done)(void *), void (*start)(void *)) {
    time_t next_word = 0;
    long long session_ms = 0;
    long long voice_ms = 0;
    double dc = 0;
    int dc_ready = 0;
    double agc = 1.0;
    double level_peak = 0;
    uint32_t vad_floor = 100u;
    int32_t vad_prev = 0;
    uint32_t read_pos = 0;
    int ready = 0;
    const char *ring_path = "/data/opt/yandex-station/clean.ring";
    (void)done;
    (void)start;
    struct CleanRing *ring = NULL;
    if (text_only) {
        FILE *src = fopen("/data/opt/yandex-station/settings/ear.src", "r");
        char kind[16] = "";
        if (src) {
            if (!fgets(kind, sizeof kind, src)) {
                kind[0] = 0;
            }
            fclose(src);
        }
        if (strncmp(kind, "mic", 3) == 0) {
            ring_path = "/data/opt/yandex-station/mic.ring";
            vad_floor = 250u;
        }
        printf("ear source %s\n", strncmp(kind, "mic", 3) == 0 ? "mic" : "clean");
        fflush(stdout);
    }
    while (run && !ready) {
        int fd = open(ring_path, O_RDONLY);
        struct stat st;
        void *map;
        if (fd < 0) {
            usleep(200000);
            continue;
        }
        if (fstat(fd, &st) != 0 || st.st_size < (off_t)sizeof(struct CleanRing)) {
            close(fd);
            usleep(200000);
            continue;
        }
        map = mmap(NULL, sizeof(struct CleanRing), PROT_READ, MAP_SHARED, fd, 0);
        close(fd);
        if (map == MAP_FAILED) {
            usleep(200000);
            continue;
        }
        ring = map;
        if (ring->magic == CLEAN_MAGIC && ring->capacity == CLEAN_CAP) {
            ready = 1;
            break;
        }
        munmap(map, sizeof(struct CleanRing));
        ring = NULL;
        usleep(200000);
    }
    if (!run || !ring) {
        return;
    }
    __sync_synchronize();
    read_pos = ring->write_pos;
    printf("ear live\n");
    fflush(stdout);
    while (run) {
        uint32_t w;
        uint32_t avail;
        uint32_t i;
        int16_t chunk[1024];
        time_t now = time(NULL);
        waitpid(-1, NULL, WNOHANG);
        if (now >= next_word) {
            load_wake("/data/opt/yandex-station/settings/wake.txt");
            next_word = now + 2;
        }
        if (text_only) {
            long long ms = now_ms();
            if (session_ms == 0) {
                session_ms = ms;
            }
            if ((!voice_ms && ms - session_ms >= first_wait_ms) ||
                (voice_ms && ms - voice_ms >= 900) ||
                ms - session_ms >= 15000) {
                printf("ear level peak=%d gain=%.1f\n", (int)level_peak, agc);
                printf("ear silence\n");
                fflush(stdout);
                break;
            }
            /* The chime plays into the mic for the first second. Drop that audio
               or soda transcribes the beep and misses the phrase after it. */
            if (ms - session_ms < skip_ms) {
                __sync_synchronize();
                read_pos = ring->write_pos;
                usleep(20000);
                continue;
            }
        }
        if (!text_only && light_until && now >= light_until) {
            light_until = 0;
            light_cmd("idle");
        }
        w = ring->write_pos;
        __sync_synchronize();
        avail = w - read_pos;
        if (avail > CLEAN_CAP) {
            read_pos = w;
            usleep(20000);
            continue;
        }
        if (avail < 1024u) {
            usleep(20000);
            continue;
        }
        for (i = 0; i < 1024u; i++) {
            chunk[i] = ring->pcm[(read_pos + i) & (CLEAN_CAP - 1u)];
        }
        read_pos += 1024u;
        if (text_only) {
            uint64_t acc = 0;
            uint32_t mean;
            for (i = 0; i < 1024u; i++) {
                int32_t sample = chunk[i];
                int32_t y = sample - vad_prev;
                vad_prev = sample;
                acc += (uint64_t)((int64_t)y * (int64_t)y);
            }
            mean = (uint32_t)(acc / 1024u);
            if (mean > vad_floor && now_ms() - session_ms >= skip_ms) {
                voice_ms = now_ms();
            }
            {
                double sum = 0;
                double pow2 = 0;
                double rms;
                double want;
                for (i = 0; i < 1024u; i++) {
                    sum += chunk[i];
                }
                if (!dc_ready) {
                    dc = sum / 1024.0;
                    dc_ready = 1;
                } else {
                    dc = dc * 0.9 + (sum / 1024.0) * 0.1;
                }
                for (i = 0; i < 1024u; i++) {
                    double v = (double)chunk[i] - dc;
                    pow2 += v * v;
                }
                rms = sqrt(pow2 / 1024.0);
                if (rms > level_peak) {
                    level_peak = rms;
                }
                if (rms > 12.0) {
                    want = 3000.0 / rms;
                    if (want > 24.0) {
                        want = 24.0;
                    }
                    if (want < 1.0) {
                        want = 1.0;
                    }
                    if (want < agc) {
                        agc = want;
                    } else {
                        agc += (want - agc) * 0.5;
                    }
                }
                for (i = 0; i < 1024u; i++) {
                    double v = ((double)chunk[i] - dc) * agc;
                    if (v > 32767.0) {
                        v = 32767.0;
                    } else if (v < -32768.0) {
                        v = -32768.0;
                    }
                    chunk[i] = (int16_t)v;
                }
            }
        }
        if (!text_only) {
            double acc = 0;
            for (i = 0; i < 1024u; i++) {
                acc += (double)chunk[i] * (double)chunk[i];
            }
            if (acc > 15.0 * 15.0 * 1024.0 && acc <= 400.0 * 400.0 * 1024.0) {
                int gain = 2;
                if (acc <= 50.0 * 50.0 * 1024.0) {
                    gain = 8;
                } else if (acc <= 150.0 * 150.0 * 1024.0) {
                    gain = 4;
                }
                for (i = 0; i < 1024u; i++) {
                    int s = (int)chunk[i] * gain;
                    if (s > 32767) {
                        s = 32767;
                    } else if (s < -32768) {
                        s = -32768;
                    }
                    chunk[i] = (int16_t)s;
                }
            }
        }
        add(handle, chunk, (int)sizeof chunk);
    }
    munmap(ring, sizeof(struct CleanRing));
}

struct soda_config {
    const char *bytes;
    int size;
    void (*callback)(const uint8_t *, int, void *);
    void *callback_handle;
};

static int engine_is_cloud(void) {
    char buf[16];
    int fd = open("/data/opt/yandex-station/settings/ear.engine", O_RDONLY);
    ssize_t n;
    if (fd < 0) {
        return 0;
    }
    n = read(fd, buf, sizeof buf - 1);
    close(fd);
    if (n <= 0) {
        return 0;
    }
    buf[n] = 0;
    return strncmp(buf, "cloud", 5) == 0;
}

static void listen_set(int on) {
    if (on) {
        int fd = open("/data/opt/yandex-station/listen.on", O_CREAT | O_WRONLY | O_TRUNC, 0644);
        if (fd >= 0) {
            close(fd);
        }
        return;
    }
    unlink("/data/opt/yandex-station/listen.on");
}

static long long wall_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (long long)ts.tv_sec * 1000LL + ts.tv_nsec / 1000000;
}

/* hear.go holds the wake moment in ms since epoch. The file reaches the speaker
   up to a second later, and people do not pause after the word. */
static int wake_lag_ms(void) {
    char buf[32];
    long long at;
    long long lag;
    int fd = open("/data/opt/yandex-station/hear.go", O_RDONLY);
    ssize_t n;
    if (fd < 0) {
        return 0;
    }
    n = read(fd, buf, sizeof buf - 1);
    close(fd);
    if (n <= 0) {
        return 0;
    }
    buf[n] = 0;
    at = atoll(buf);
    if (at <= 0) {
        return 0;
    }
    lag = wall_ms() - at + 300;
    if (lag < 0 || lag > 2500) {
        return 0;
    }
    return (int)lag;
}

static int setting_on(const char *name) {
    char path[128];
    char c = '1';
    int fd;
    snprintf(path, sizeof path, "/data/opt/yandex-station/settings/%s", name);
    fd = open(path, O_RDONLY);
    if (fd < 0) {
        return 1;
    }
    if (read(fd, &c, 1) != 1) {
        c = '1';
    }
    close(fd);
    return c != '0';
}

static void save_ring_window(const char *ring_path, const char *out_path, uint32_t back, int samples) {
    struct CleanRing *ring;
    int16_t *pcm;
    uint32_t start;
    void *map;
    int fd = open(ring_path, O_RDONLY);
    int i;
    if (fd < 0) {
        return;
    }
    map = mmap(NULL, sizeof(struct CleanRing), PROT_READ, MAP_SHARED, fd, 0);
    close(fd);
    if (map == MAP_FAILED) {
        return;
    }
    ring = map;
    pcm = malloc((size_t)samples * sizeof(int16_t));
    if (ring->magic == CLEAN_MAGIC && ring->capacity == CLEAN_CAP && pcm) {
        start = ring->write_pos - back - (uint32_t)samples;
        for (i = 0; i < samples; i++) {
            pcm[i] = ring->pcm[(start + (uint32_t)i) & (CLEAN_CAP - 1u)];
        }
        fd = open(out_path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
        if (fd >= 0) {
            if (write(fd, pcm, (size_t)samples * sizeof(int16_t)) < 0) {
                /* сравнение очистки не обязательно */
            }
            close(fd);
        }
    }
    free(pcm);
    munmap(map, sizeof(struct CleanRing));
}

/* Окно фразы из всех колец: сырой микрофон, выбранная очистка и выходы из vqe.dump. */
static void save_windows(uint32_t back, int samples) {
    DIR *dir;
    struct dirent *ent;
    save_ring_window("/data/opt/yandex-station/clean.ring", "/tmp/ear-clean.raw", back, samples);
    save_ring_window("/data/opt/yandex-station/mic.ring", "/tmp/ear-mic.raw", back, samples);
    dir = opendir("/tmp");
    if (!dir) {
        return;
    }
    while ((ent = readdir(dir)) != NULL) {
        char in[300];
        char out[300];
        size_t n = strlen(ent->d_name);
        if (strncmp(ent->d_name, "vqe-", 4) != 0 || n < 9 || strcmp(ent->d_name + n - 5, ".ring") != 0) {
            continue;
        }
        snprintf(in, sizeof in, "/tmp/%s", ent->d_name);
        snprintf(out, sizeof out, "/tmp/ear-%.*s.raw", (int)(n - 5), ent->d_name);
        save_ring_window(in, out, back, samples);
    }
    closedir(dir);
}

static int capture_phrase(int16_t *dst, int max_samples, int lag_ms) {
    const char *ring_path = "/data/opt/yandex-station/clean.ring";
    char kind[16];
    FILE *src = fopen("/data/opt/yandex-station/settings/ear.src", "r");
    struct CleanRing *ring = NULL;
    uint32_t read_pos = 0;
    int got = 0;
    long long start;
    long long blind;
    int fd;
    void *map;
    struct stat st;
    if (src) {
        if (!fgets(kind, sizeof kind, src)) {
            kind[0] = 0;
        }
        fclose(src);
        if (strncmp(kind, "mic", 3) == 0) {
            ring_path = "/data/opt/yandex-station/mic.ring";
        }
    }
    fd = open(ring_path, O_RDONLY);
    if (fd < 0) {
        return 0;
    }
    if (fstat(fd, &st) != 0 || st.st_size < (off_t)sizeof(struct CleanRing)) {
        close(fd);
        return 0;
    }
    map = mmap(NULL, sizeof(struct CleanRing), PROT_READ, MAP_SHARED, fd, 0);
    close(fd);
    if (map == MAP_FAILED) {
        return 0;
    }
    ring = map;
    if (ring->magic != CLEAN_MAGIC || ring->capacity != CLEAN_CAP) {
        munmap(map, sizeof(struct CleanRing));
        return 0;
    }
    double floor_rms = 0;
    int speech = 0;
    long long quiet = -1;
    __sync_synchronize();
    read_pos = ring->write_pos - (uint32_t)(lag_ms * 16);
    start = now_ms() - lag_ms;
    /* The chime starts about 0.35 s after hear.go and lasts 0.7 s. It stays in
       the recording, the speaker's echo canceller takes most of it out, but it
       must not count as speech. */
    blind = setting_on("wake.sound") ? lag_ms + 1200 : 0;
    while (run && got + 1024 <= max_samples) {
        uint32_t w;
        uint32_t avail;
        uint32_t i;
        double pow2 = 0;
        double rms;
        double gate;
        long long heard = got / 16;
        if ((!speech && heard >= blind + 1500) || heard >= 7500 || now_ms() - start >= 9000) {
            break;
        }
        __sync_synchronize();
        w = ring->write_pos;
        avail = w - read_pos;
        if (avail > CLEAN_CAP) {
            read_pos = w;
            continue;
        }
        if (avail < 1024u) {
            usleep(20000);
            continue;
        }
        for (i = 0; i < 1024u; i++) {
            int16_t sample = ring->pcm[(read_pos + i) & (CLEAN_CAP - 1u)];
            dst[got + (int)i] = sample;
            pow2 += (double)sample * (double)sample;
        }
        read_pos += 1024u;
        got += 1024;
        rms = sqrt(pow2 / 1024.0);
        if (floor_rms == 0 || rms < floor_rms) {
            floor_rms = rms;
        }
        if (heard < blind) {
            continue;
        }
        gate = floor_rms * 2.4;
        if (gate < 30.0) {
            gate = 30.0;
        }
        if (gate > 450.0) {
            gate = 450.0;
        }
        if (rms > gate) {
            speech = 1;
            quiet = -1;
        } else if (speech) {
            if (quiet < 0) {
                quiet = heard;
            }
            if (heard - quiet >= 650) {
                break;
            }
        }
    }
    __sync_synchronize();
    {
        uint32_t back = ring->write_pos - read_pos;
        munmap(map, sizeof(struct CleanRing));
        if (got > 0 && back < CLEAN_CAP / 2u) {
            save_windows(back, got);
        }
    }
    return got;
}

static int post_hear(const int16_t *pcm, int samples) {
    struct yse_host conf;
    int fd;
    int bytes;
    int left;
    int got = 0;
    struct sockaddr_in addr;
    struct timeval tv;
    char head[320];
    char reply[512];
    const char *body;
    yse_host_load(&conf);
    bytes = samples * (int)sizeof(int16_t);
    fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        return 0;
    }
    memset(&tv, 0, sizeof tv);
    tv.tv_sec = 70;
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
    memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET;
    addr.sin_port = htons((unsigned short)(conf.base + YSE_TALK));
    if (inet_pton(AF_INET, conf.ip, &addr.sin_addr) != 1 ||
        connect(fd, (struct sockaddr *)&addr, sizeof addr) != 0) {
        close(fd);
        return 0;
    }
    snprintf(head, sizeof head,
             "POST /v1/hear HTTP/1.0\r\nHost: %s\r\nContent-Length: %d\r\nConnection: close\r\n\r\n",
             conf.ip, bytes);
    if (send(fd, head, strlen(head), MSG_NOSIGNAL) != (ssize_t)strlen(head)) {
        close(fd);
        return 0;
    }
    body = (const char *)pcm;
    left = bytes;
    while (left > 0) {
        ssize_t wrote = send(fd, body, (size_t)left, MSG_NOSIGNAL);
        if (wrote <= 0) {
            close(fd);
            return 0;
        }
        body += wrote;
        left -= (int)wrote;
    }
    while (got < (int)sizeof reply - 1) {
        ssize_t n = recv(fd, reply + got, sizeof reply - 1 - (size_t)got, 0);
        if (n <= 0) {
            break;
        }
        got += (int)n;
    }
    reply[got] = 0;
    close(fd);
    return strstr(reply, "\"ok\":true") != NULL;
}

/* Поток после слова: с момента слова очищенный звук идёт на сервер (base+5), пока сервер
   не ответит «stop». Конец фразы решает сервер. 0 — сервер не принял, нужен старый путь. */
static int stream_phrase(int lag_ms, int follow) {
    const char *ring_path = "/data/opt/yandex-station/clean.ring";
    struct yse_host conf;
    struct sockaddr_in addr;
    struct timeval tv;
    struct CleanRing *ring;
    struct pollfd pfd;
    int16_t chunk[1600];
    char kind[16];
    char head[96];
    uint32_t read_pos;
    long long start;
    int sent = 0;
    int fd;
    int rfd;
    void *map;
    FILE *src = fopen("/data/opt/yandex-station/settings/ear.src", "r");
    if (src) {
        if (fgets(kind, sizeof kind, src) && strncmp(kind, "mic", 3) == 0) {
            ring_path = "/data/opt/yandex-station/mic.ring";
        }
        fclose(src);
    }
    rfd = open(ring_path, O_RDONLY);
    if (rfd < 0) {
        return 0;
    }
    map = mmap(NULL, sizeof(struct CleanRing), PROT_READ, MAP_SHARED, rfd, 0);
    close(rfd);
    if (map == MAP_FAILED) {
        return 0;
    }
    ring = map;
    if (ring->magic != CLEAN_MAGIC || ring->capacity != CLEAN_CAP) {
        munmap(map, sizeof(struct CleanRing));
        return 0;
    }
    yse_host_load(&conf);
    fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        munmap(map, sizeof(struct CleanRing));
        return 0;
    }
    memset(&tv, 0, sizeof tv);
    tv.tv_sec = 2;
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
    memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET;
    addr.sin_port = htons((unsigned short)(conf.base + YSE_LISTEN));
    if (inet_pton(AF_INET, conf.ip, &addr.sin_addr) != 1 ||
        connect(fd, (struct sockaddr *)&addr, sizeof addr) != 0) {
        close(fd);
        munmap(map, sizeof(struct CleanRing));
        return 0;
    }
    snprintf(head, sizeof head, "YSE1 lag=%d chime=%d follow=%d\n", lag_ms,
             follow ? 0 : setting_on("wake.sound"), follow);
    if (send(fd, head, strlen(head), MSG_NOSIGNAL) != (ssize_t)strlen(head)) {
        close(fd);
        munmap(map, sizeof(struct CleanRing));
        return 0;
    }
    __sync_synchronize();
    read_pos = ring->write_pos - (uint32_t)(lag_ms * 16);
    start = now_ms();
    while (run && now_ms() - start < 12000) {
        uint32_t avail;
        uint32_t n;
        uint32_t i;
        pfd.fd = fd;
        pfd.events = POLLIN;
        pfd.revents = 0;
        if (poll(&pfd, 1, 0) > 0) {
            char reply[16];
            ssize_t got = recv(fd, reply, sizeof reply, MSG_DONTWAIT);
            if (got <= 0 || memchr(reply, 's', (size_t)got)) {
                break;
            }
        }
        __sync_synchronize();
        avail = ring->write_pos - read_pos;
        if (avail > CLEAN_CAP) {
            read_pos = ring->write_pos;
            continue;
        }
        if (avail < 320u) {
            usleep(10000);
            continue;
        }
        n = avail > 1600u ? 1600u : avail;
        for (i = 0; i < n; i++) {
            chunk[i] = ring->pcm[(read_pos + i) & (CLEAN_CAP - 1u)];
        }
        read_pos += n;
        if (send(fd, chunk, n * sizeof(int16_t), MSG_NOSIGNAL) != (ssize_t)(n * sizeof(int16_t))) {
            break;
        }
        sent += (int)n;
    }
    __sync_synchronize();
    {
        uint32_t back = ring->write_pos - read_pos;
        if (sent > 0 && back < CLEAN_CAP / 2u) {
            save_windows(back, sent);
        }
    }
    close(fd);
    munmap(map, sizeof(struct CleanRing));
    printf("ear streamed %d samples in %lld ms\n", sent, now_ms() - start);
    fflush(stdout);
    return 1;
}

static void cloud_live(void) {
    int16_t *buf;
    int pfd = open("/data/opt/yandex-station/ear.pid", O_WRONLY | O_CREAT | O_TRUNC, 0644);
    char pbuf[32];
    int pn = snprintf(pbuf, sizeof pbuf, "%d\n", (int)getpid());
    signal(SIGPIPE, SIG_IGN);
    if (pfd >= 0 && pn > 0) {
        if (write(pfd, pbuf, (size_t)pn) < 0) {
            /* pid file is only a hint */
        }
        close(pfd);
    } else if (pfd >= 0) {
        close(pfd);
    }
    buf = malloc(16000 * 8 * sizeof(int16_t));
    if (!buf) {
        return;
    }
    printf("ear cloud\n");
    fflush(stdout);
    while (run) {
        int samples;
        while (run && access("/data/opt/yandex-station/hear.go", F_OK) != 0) {
            usleep(50000);
        }
        if (!run) {
            break;
        }
        {
            /* follow.go кладёт сервер после ответа: окно продолжения без слова и без сигнала. */
            int follow = access("/data/opt/yandex-station/follow.go", F_OK) == 0;
            int lag = follow ? 0 : wake_lag_ms();
            long long began;
            unlink("/data/opt/yandex-station/hear.go");
            unlink("/data/opt/yandex-station/follow.go");
            listen_set(1);
            printf("ear session%s lag %d\n", follow ? " follow" : "", lag);
            fflush(stdout);
            if (follow) {
                if (system("color=$(cat /data/opt/yandex-station/settings/ring.color 2>/dev/null || echo 3aa0ff); "
                           "bright=$(cat /data/opt/yandex-station/settings/ring.bright 2>/dev/null || echo 160); "
                           "/data/opt/yandex-station/bin/glow \"$color\" 5000 breathe \"$bright\" >/dev/null 2>&1 &") != 0) {
                    fflush(stdout);
                }
            } else {
                duck_hold();
            }
            if (stream_phrase(lag, follow) || follow) {
                listen_set(0);
                continue;
            }
            began = now_ms();
            samples = capture_phrase(buf, 16000 * 8, lag);
            printf("ear samples %d wait %lld\n", samples, now_ms() - began);
        }
        fflush(stdout);
        if (samples >= 1600 && post_hear(buf, samples)) {
            printf("ear sent\n");
        } else {
            printf("ear miss\n");
        }
        listen_set(0);
        fflush(stdout);
    }
    free(buf);
}

int main(int argc, char **argv) {
    const char *model = argc > 1 ? argv[1] : "/data/opt/yandex-station/soda/model";
    const char *lib = argc > 2 ? argv[2] : "/data/opt/yandex-station/soda/libsoda.so";
    const char *audio_path = argc > 3 ? argv[3] : NULL;
    uint8_t cfg[512];
    int cfg_len;
    void *so;
    void *(*create)(struct soda_config *);
    void (*start)(void *);
    void (*add)(void *, const void *, int);
    void (*done)(void *);
    void (*stop)(void *);
    void *handle;
    struct soda_config config;
    struct sigaction sa;
    int16_t chunk[1024];
    FILE *audio = stdin;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_stop;
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);
    {
        int argi;
        for (argi = 1; argi < argc; argi++) {
            if (strcmp(argv[argi], "text") == 0) {
                text_only = 1;
            }
        }
    }
    if (audio_path && strcmp(audio_path, "live") == 0 && text_only && engine_is_cloud()) {
        cloud_live();
        return 0;
    }
    load_wake("/data/opt/yandex-station/settings/wake.txt");
    cfg_len = build_config(cfg, (int)sizeof cfg, model);
    if (cfg_len < 0) {
        fprintf(stderr, "config\n");
        return 1;
    }
    so = dlopen(lib, RTLD_NOW);
    if (!so) {
        fprintf(stderr, "open %s\n", dlerror());
        return 1;
    }
    create = dlsym(so, "CreateExtendedSodaAsync");
    start = dlsym(so, "ExtendedSodaStart");
    add = dlsym(so, "ExtendedAddAudio");
    done = dlsym(so, "ExtendedSodaMarkDone");
    stop = dlsym(so, "ExtendedSodaStop");
    if (!create || !start || !add || !done || !stop) {
        fprintf(stderr, "symbol %s\n", dlerror());
        return 1;
    }
    memset(&config, 0, sizeof config);
    config.bytes = (char *)cfg;
    config.size = cfg_len;
    config.callback = on_audio;
    printf("ear word=%s\n", wake_word);
    fflush(stdout);
    handle = create(&config);
    if (!handle) {
        fprintf(stderr, "create failed\n");
        return 1;
    }
    if (!(audio_path && strcmp(audio_path, "live") == 0 && text_only)) {
        start(handle);
    }
    printf("ear started\n");
    fflush(stdout);
    if (text_only) {
        int pfd = open("/data/opt/yandex-station/ear.pid", O_WRONLY | O_CREAT | O_TRUNC, 0644);
        char pbuf[32];
        int pn = snprintf(pbuf, sizeof pbuf, "%d\n", (int)getpid());
        if (pfd >= 0 && pn > 0) {
            if (write(pfd, pbuf, (size_t)pn) < 0) {
                /* pid file is only a hint */
            }
            close(pfd);
        } else if (pfd >= 0) {
            close(pfd);
        }
        printf("ear ready\n");
        fflush(stdout);
    } else {
        usleep(500000);
    }
    if (audio_path && strcmp(audio_path, "live") == 0 && text_only) {
        /* CreateExtendedSodaAsync already starts a session, and its clock
           starts during the long init. Audio that arrives later is "late"
           and the recognizer drops the phrase. Stop that session and open
           a fresh one at the wake word. A second Start without Stop hangs. */
        printf("ear stop-init\n");
        fflush(stdout);
        stop(handle);
        printf("ear stopped-init\n");
        fflush(stdout);
        /* Silence between phrases piled up for minutes and the recognizer
           crashed before it reached the words. Each wake opens a new session
           and only that phrase is fed. */
        int engine = 0;
        while (run) {
            int follow = 0;
            while (run && access("/data/opt/yandex-station/hear.go", F_OK) != 0) {
                usleep(50000);
            }
            if (!run) {
                break;
            }
            unlink("/data/opt/yandex-station/hear.go");
            listen_set(1);
            committed[0] = 0;
            last_hear[0] = 0;
            took_action = 0;
            printf("ear session\n");
            fflush(stdout);
            if (engine) {
                printf("ear disarm\n");
                fflush(stdout);
                stop(handle);
                printf("ear disarmed\n");
                fflush(stdout);
            }
            printf("ear arm\n");
            fflush(stdout);
            start(handle);
            printf("ear armed\n");
            fflush(stdout);
            usleep(200000);
            engine = 1;
            first_wait_ms = 7000;
            skip_ms = 1200;
            follow_ring(handle, add, done, start);
            /* A continuous session does not emit the phrase until the mic
               is closed. Closing it also ends the engine, so the process
               exits and the loop loads a fresh one for the next wake. */
            printf("ear mark\n");
            fflush(stdout);
            done(handle);
            printf("ear marked\n");
            fflush(stdout);
            {
                long long until = now_ms() + 150000;
                while (!committed[0] && run && now_ms() < until) {
                    usleep(200000);
                }
            }
            if (!committed[0] && last_hear[0]) {
                snprintf(committed, sizeof committed, "%s", last_hear);
            }
            if (alarm_wait && committed[0]) {
                act_on(committed, 1);
            }
            alarm_wait = 0;
            if (committed[0] && !took_action) {
                ask_cloud(committed, follow);
            }
            listen_set(0);
            printf("ear exit\n");
            fflush(stdout);
            _exit(0);
        }
    } else if (audio_path && strcmp(audio_path, "live") == 0) {
        follow_ring(handle, add, done, start);
    } else {
        if (audio_path && strcmp(audio_path, "-") != 0) {
            audio = fopen(audio_path, "rb");
            if (!audio) {
                perror(audio_path);
                return 1;
            }
        }
        while (run) {
            size_t n = fread(chunk, sizeof(int16_t), 1024, audio);
            if (n == 0) {
                break;
            }
            add(handle, chunk, (int)(n * sizeof(int16_t)));
            usleep(60000);
        }
    }
    if (!text_only) {
        light_cmd("idle");
    }
    done(handle);
    usleep(300000);
    stop(handle);
    printf("ear stop heard=%d\n", heard);
    return heard ? 0 : 2;
}
