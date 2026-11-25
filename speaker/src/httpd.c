#define _GNU_SOURCE
#include <arpa/inet.h>
#include <ctype.h>
#include <dirent.h>
#include <strings.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <linux/input.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

#include "live_ring.h"
#include "alarms.h"
#include "host_conf.h"
#include "mini2_led.h"

static char murl[200];

#define MAX_HDR 16384
#define MAX_BODY (64 * 1024 * 1024)
#define LIST_CAP (256 * 1024)
/* 3000 символов в UI ≈ до 9000 байт UTF-8 (кириллица). */
#define PERSONA_CAP 9001
#define PERSONA_JSON 18001

static char root_dir[512];
static char token[128];

static void ignore_pipe(void) {
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = SIG_IGN;
    sigaction(SIGPIPE, &sa, NULL);
}

static int write_all(int fd, const void *buf, size_t n) {
    const char *p = buf;
    while (n > 0) {
        ssize_t w = write(fd, p, n);
        if (w < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
        p += w;
        n -= (size_t)w;
    }
    return 0;
}

static int read_full(int fd, void *buf, size_t n) {
    char *p = buf;
    while (n > 0) {
        ssize_t r = read(fd, p, n);
        if (r < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
        if (r == 0) {
            return -1;
        }
        p += r;
        n -= (size_t)r;
    }
    return 0;
}

static void load_token(void) {
    char path[640];
    int fd;
    ssize_t n;
    token[0] = 0;
    snprintf(path, sizeof path, "%s/token", root_dir);
    fd = open(path, O_RDONLY);
    if (fd < 0) {
        return;
    }
    n = read(fd, token, sizeof token - 1);
    close(fd);
    if (n < 0) {
        token[0] = 0;
        return;
    }
    token[n] = 0;
    while (n > 0 && (token[n - 1] == '\n' || token[n - 1] == '\r' || token[n - 1] == ' ')) {
        token[--n] = 0;
    }
}

static int token_ok(const char *got) {
    size_t i = 0;
    size_t j = 0;
    load_token();
    if (token[0] == 0) {
        return 1;
    }
    if (!got) {
        return 0;
    }
    while (got[j] == ' ' || got[j] == '\t') {
        j++;
    }
    for (;;) {
        unsigned char a = (unsigned char)token[i];
        unsigned char b = (unsigned char)got[j];
        if (b == ' ' || b == '\t') {
            j++;
            continue;
        }
        if (a == 0 && b == 0) {
            return 1;
        }
        if (a == 0 || b == 0 || tolower(a) != tolower(b)) {
            return 0;
        }
        i++;
        j++;
    }
}

static int hex_nibble(int c) {
    if (c >= '0' && c <= '9') {
        return c - '0';
    }
    if (c >= 'a' && c <= 'f') {
        return c - 'a' + 10;
    }
    if (c >= 'A' && c <= 'F') {
        return c - 'A' + 10;
    }
    return -1;
}

static void url_decode(char *s) {
    char *r = s;
    char *w = s;
    while (*r) {
        if (*r == '%' && hex_nibble((unsigned char)r[1]) >= 0 && hex_nibble((unsigned char)r[2]) >= 0) {
            *w++ = (char)((hex_nibble((unsigned char)r[1]) << 4) | hex_nibble((unsigned char)r[2]));
            r += 3;
        } else if (*r == '+') {
            *w++ = ' ';
            r++;
        } else {
            *w++ = *r++;
        }
    }
    *w = 0;
}

static int safe_name(const char *s) {
    size_t n;
    const unsigned char *p;
    if (!s || !s[0] || strcmp(s, ".") == 0 || strcmp(s, "..") == 0) {
        return 0;
    }
    n = strlen(s);
    if (n > 80) {
        return 0;
    }
    for (p = (const unsigned char *)s; *p; p++) {
        if (*p < 32 || *p == '/' || *p == '\\' || *p == '"' ) {
            return 0;
        }
    }
    return 1;
}

static int safe_id(const char *s) {
    const unsigned char *p;
    size_t n;
    if (!s || !s[0]) {
        return 0;
    }
    n = strlen(s);
    if (n > 48) {
        return 0;
    }
    for (p = (const unsigned char *)s; *p; p++) {
        if (!((*p >= 'a' && *p <= 'z') || (*p >= '0' && *p <= '9') || *p == '_' || *p == '-')) {
            return 0;
        }
    }
    return 1;
}

static void json_escape(const char *s, char *out, size_t cap) {
    size_t o = 0;
    const unsigned char *p = (const unsigned char *)s;
    if (cap == 0) {
        return;
    }
    while (*p && o + 6 < cap) {
        unsigned char c = *p++;
        if (c == '"' || c == '\\') {
            out[o++] = '\\';
            out[o++] = (char)c;
        } else if (c < 32) {
            o += (size_t)snprintf(out + o, cap - o, "\\u%04x", c);
        } else {
            out[o++] = (char)c;
        }
    }
    out[o] = 0;
}

static int path_join(char *out, size_t cap, const char *a, const char *b) {
    int n = snprintf(out, cap, "%s/%s", a, b);
    return n > 0 && (size_t)n < cap;
}

static int mkdir_p(const char *path) {
    char tmp[768];
    size_t len;
    size_t i;
    if (strlen(path) >= sizeof tmp) {
        return -1;
    }
    snprintf(tmp, sizeof tmp, "%s", path);
    len = strlen(tmp);
    if (len == 0) {
        return -1;
    }
    for (i = 1; i < len; i++) {
        if (tmp[i] == '/') {
            tmp[i] = 0;
            if (mkdir(tmp, 0755) != 0 && errno != EEXIST) {
                return -1;
            }
            tmp[i] = '/';
        }
    }
    if (mkdir(tmp, 0755) != 0 && errno != EEXIST) {
        return -1;
    }
    return 0;
}

static int write_file(const char *path, const void *data, size_t n) {
    char tmp[768];
    int fd;
    if (snprintf(tmp, sizeof tmp, "%s.tmp", path) >= (int)sizeof tmp) {
        return -1;
    }
    fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) {
        return -1;
    }
    if (write_all(fd, data, n) != 0) {
        close(fd);
        unlink(tmp);
        return -1;
    }
    if (close(fd) != 0) {
        unlink(tmp);
        return -1;
    }
    if (rename(tmp, path) != 0) {
        unlink(tmp);
        return -1;
    }
    return 0;
}

static int read_line_file(const char *path, char *out, size_t cap);
static void send_json(int fd, int code, const char *reason, const char *json);

static int setting_path(char *out, size_t cap, const char *name) {
    int n = snprintf(out, cap, "%s/settings/%s", root_dir, name);
    return n > 0 && (size_t)n < cap;
}

static int ensure_settings_dir(void) {
    char path[640];
    snprintf(path, sizeof path, "%s/settings", root_dir);
    return mkdir_p(path);
}

static int read_setting(const char *name, char *out, size_t cap) {
    char path[768];
    out[0] = 0;
    if (!setting_path(path, sizeof path, name)) {
        return -1;
    }
    return read_line_file(path, out, cap);
}

static int write_setting(const char *name, const char *value, mode_t mode) {
    char path[768];
    if (strchr(value, '\n') || strchr(value, '\r')) {
        return -1;
    }
    if (ensure_settings_dir() != 0) {
        return -1;
    }
    if (!setting_path(path, sizeof path, name)) {
        return -1;
    }
    if (value[0] == 0) {
        unlink(path);
        return 0;
    }
    if (write_file(path, value, strlen(value)) != 0) {
        return -1;
    }
    return chmod(path, mode);
}

static int setting_set(const char *name) {
    char buf[8];
    return read_setting(name, buf, sizeof buf) == 0 && buf[0] != 0;
}

static const char *skip_ws(const char *p) {
    while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') {
        p++;
    }
    return p;
}

static int hex_byte(const char *p, unsigned *out) {
    int hi = hex_nibble((unsigned char)p[0]);
    int lo = hex_nibble((unsigned char)p[1]);
    if (hi < 0 || lo < 0) {
        return -1;
    }
    *out = ((unsigned)hi << 4) | (unsigned)lo;
    return 0;
}

static int append_utf8(char *out, size_t cap, size_t *o, unsigned cp) {
    if (cp > 0xFFFF || (cp >= 0xD800 && cp <= 0xDFFF)) {
        return -1;
    }
    if (cp < 0x80) {
        if (*o + 1 >= cap) {
            return -1;
        }
        out[(*o)++] = (char)cp;
        return 0;
    }
    if (cp < 0x800) {
        if (*o + 2 >= cap) {
            return -1;
        }
        out[(*o)++] = (char)(0xC0 | (cp >> 6));
        out[(*o)++] = (char)(0x80 | (cp & 0x3F));
        return 0;
    }
    if (*o + 3 >= cap) {
        return -1;
    }
    out[(*o)++] = (char)(0xE0 | (cp >> 12));
    out[(*o)++] = (char)(0x80 | ((cp >> 6) & 0x3F));
    out[(*o)++] = (char)(0x80 | (cp & 0x3F));
    return 0;
}

static int json_string_field(const char *body, const char *key, char *out, size_t cap, int *present) {
    char needle[80];
    const char *p = body;
    size_t nlen;
    size_t o = 0;
    *present = 0;
    out[0] = 0;
    if (snprintf(needle, sizeof needle, "\"%s\"", key) >= (int)sizeof needle) {
        return -1;
    }
    nlen = strlen(needle);
    while ((p = strstr(p, needle)) != NULL) {
        const char *q;
        if (p > body) {
            char prev = p[-1];
            if (prev != '{' && prev != ',' && prev != ' ' && prev != '\n' && prev != '\r' && prev != '\t') {
                p += nlen;
                continue;
            }
        }
        q = skip_ws(p + nlen);
        if (*q != ':') {
            p += nlen;
            continue;
        }
        q = skip_ws(q + 1);
        if (*q != '"') {
            return -1;
        }
        q++;
        while (*q && *q != '"') {
            unsigned char c = 0;
            if (*q == '\\') {
                unsigned cp = 0;
                q++;
                if (*q == '"' || *q == '\\' || *q == '/') {
                    c = (unsigned char)*q++;
                } else if (*q == 'n' || *q == 'r' || *q == 't') {
                    return -1;
                } else if (*q == 'u') {
                    unsigned b0, b1;
                    q++;
                    if (hex_byte(q, &b0) != 0 || hex_byte(q + 2, &b1) != 0) {
                        return -1;
                    }
                    cp = (b0 << 8) | b1;
                    q += 4;
                    if (append_utf8(out, cap, &o, cp) != 0) {
                        return -1;
                    }
                    continue;
                } else {
                    return -1;
                }
            } else {
                c = (unsigned char)*q++;
                if (c < 32) {
                    return -1;
                }
            }
            if (o + 1 >= cap) {
                return -1;
            }
            out[o++] = (char)c;
        }
        if (*q != '"') {
            return -1;
        }
        out[o] = 0;
        *present = 1;
        return 0;
    }
    return 0;
}

static int json_scalar(const char *body, const char *key, char *out, size_t cap, int *present) {
    char needle[80];
    const char *p = body;
    size_t nlen;
    size_t o = 0;
    *present = 0;
    out[0] = 0;
    if (!body) {
        return 0;
    }
    if (snprintf(needle, sizeof needle, "\"%s\"", key) >= (int)sizeof needle) {
        return -1;
    }
    nlen = strlen(needle);
    while ((p = strstr(p, needle)) != NULL) {
        const char *q;
        if (p > body) {
            char prev = p[-1];
            if (prev != '{' && prev != ',' && prev != ' ' && prev != '\n' && prev != '\r' && prev != '\t') {
                p += nlen;
                continue;
            }
        }
        q = skip_ws(p + nlen);
        if (*q != ':') {
            p += nlen;
            continue;
        }
        q = skip_ws(q + 1);
        if (*q == '"') {
            return json_string_field(body, key, out, cap, present);
        }
        while (*q && *q != ',' && *q != '}' && *q != ' ' && *q != '\n' && *q != '\r' && *q != '\t') {
            if (o + 1 >= cap) {
                return -1;
            }
            out[o++] = *q++;
        }
        if (o == 0) {
            return -1;
        }
        out[o] = 0;
        *present = 1;
        return 0;
    }
    return 0;
}

static int flag_text(const char *text, int *out) {
    if (strcmp(text, "1") == 0 || strcmp(text, "true") == 0) {
        *out = 1;
        return 0;
    }
    if (strcmp(text, "0") == 0 || strcmp(text, "false") == 0) {
        *out = 0;
        return 0;
    }
    return -1;
}

static void current_ssid(char *out, size_t cap) {
    FILE *fp;
    char line[256];
    out[0] = 0;
    fp = popen("wpa_cli -i wlan0 status 2>/dev/null", "r");
    if (!fp) {
        return;
    }
    while (fgets(line, sizeof line, fp)) {
        if (strncmp(line, "ssid=", 5) == 0) {
            size_t n = strcspn(line + 5, "\r\n");
            if (n >= cap) {
                n = cap - 1;
            }
            memcpy(out, line + 5, n);
            out[n] = 0;
            break;
        }
    }
    pclose(fp);
}

static int wpa_quote(const char *in, char *out, size_t cap) {
    size_t o = 0;
    while (*in) {
        unsigned char c = (unsigned char)*in++;
        if (c < 32 || c == 127) {
            return -1;
        }
        if (c == '"' || c == '\\') {
            if (o + 2 >= cap) {
                return -1;
            }
            out[o++] = '\\';
        }
        if (o + 1 >= cap) {
            return -1;
        }
        out[o++] = (char)c;
    }
    out[o] = 0;
    return 0;
}

static int write_wifi_block(void) {
    char ssid[40];
    char psk[80];
    char qssid[96];
    char qpsk[160];
    char block[512];
    const char *path = "/bkupgrade/data/wpa_supplicant.conf";
    const char *bak = "/bkupgrade/data/wpa_supplicant.conf.ystation.bak";
    const char *mark = "# ystation-net\n";
    const char *tail = "# ystation-end\n";
    char *file;
    char *outbuf;
    char *start;
    char *end;
    int fd;
    int blen;
    struct stat st;
    size_t n = 0;
    size_t o = 0;
    if (read_setting("wifi.ssid", ssid, sizeof ssid) != 0 || ssid[0] == 0) {
        return -1;
    }
    if (read_setting("wifi.psk", psk, sizeof psk) != 0 || strlen(psk) < 8 || strlen(psk) > 63) {
        return -1;
    }
    if (strlen(ssid) > 32) {
        return -1;
    }
    if (wpa_quote(ssid, qssid, sizeof qssid) != 0 || wpa_quote(psk, qpsk, sizeof qpsk) != 0) {
        return -1;
    }
    blen = snprintf(block, sizeof block,
                    "%snetwork={\n\tssid=\"%s\"\n\tpsk=\"%s\"\n\tkey_mgmt=WPA-PSK\n\tpriority=5000\n}\n%s",
                    mark, qssid, qpsk, tail);
    if (blen < 0 || (size_t)blen >= sizeof block) {
        return -1;
    }
    fd = open(path, O_RDONLY);
    if (fd < 0 || fstat(fd, &st) != 0 || st.st_size < 0 || st.st_size > 16384) {
        if (fd >= 0) {
            close(fd);
        }
        return -1;
    }
    file = malloc((size_t)st.st_size + 1);
    if (!file) {
        close(fd);
        return -1;
    }
    while (n < (size_t)st.st_size) {
        ssize_t r = read(fd, file + n, (size_t)st.st_size - n);
        if (r < 0) {
            if (errno == EINTR) {
                continue;
            }
            free(file);
            close(fd);
            return -1;
        }
        if (r == 0) {
            break;
        }
        n += (size_t)r;
    }
    close(fd);
    file[n] = 0;
    if (write_file(bak, file, n) != 0) {
        free(file);
        return -1;
    }
    chmod(bak, 0600);
    outbuf = malloc(n + (size_t)blen + 2);
    if (!outbuf) {
        free(file);
        return -1;
    }
    start = strstr(file, mark);
    end = start ? strstr(start, tail) : NULL;
    if (start && end) {
        end += strlen(tail);
        o = (size_t)(start - file);
        memcpy(outbuf, file, o);
        memcpy(outbuf + o, block, (size_t)blen);
        o += (size_t)blen;
        memcpy(outbuf + o, end, n - (size_t)(end - file));
        o += n - (size_t)(end - file);
    } else {
        memcpy(outbuf, file, n);
        o = n;
        if (o == 0 || outbuf[o - 1] != '\n') {
            outbuf[o++] = '\n';
        }
        memcpy(outbuf + o, block, (size_t)blen);
        o += (size_t)blen;
    }
    free(file);
    if (write_file(path, outbuf, o) != 0) {
        free(outbuf);
        return -1;
    }
    free(outbuf);
    chmod(path, 0600);
    sync();
    return 0;
}

static int apply_bluetooth(void) {
    int rc = system("/data/opt/yandex-station/bin/bthead apply >/data/opt/yandex-station/bt.log 2>&1");
    return rc == 0 ? 0 : -1;
}

static int mixer_live(void) {
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
    if (pclose(fp) != 0 && value < 0) {
        return -1;
    }
    if (value < 0 || value > 255) {
        return -1;
    }
    return value;
}

static int master_volume(void) {
    FILE *fp;
    char line[96];
    int value = mixer_live();
    if (value >= 0) {
        return value;
    }
    value = 26;
    fp = fopen("/data/opt/yandex-station/house.status", "r");
    if (!fp) {
        return value;
    }
    while (fgets(line, sizeof line, fp)) {
        if (strncmp(line, "volume=", 7) == 0) {
            value = atoi(line + 7);
        }
    }
    fclose(fp);
    if (value < 0 || value > 255) {
        value = 26;
    }
    return value;
}

static int mixer_from_percent(int percent) {
    if (percent < 0) {
        percent = 0;
    }
    if (percent > 100) {
        percent = 100;
    }
    return (percent * 255 + 50) / 100;
}

static int percent_from_mixer(int mixer) {
    if (mixer < 0) {
        mixer = 0;
    }
    if (mixer > 255) {
        mixer = 255;
    }
    return (mixer * 100 + 127) / 255;
}

static void play_preview(void) {
    system(
        "if [ -f /data/opt/yandex-station/volprev.pid ]; then "
        "kill $(cat /data/opt/yandex-station/volprev.pid) 2>/dev/null; "
        "fi; "
        /* Master на Мини 2 — softvol в default: мимо него писк всегда одной громкости. */
        "dev=plughw:0,0; grep -q softvol /etc/asound.conf 2>/dev/null && dev=default; "
        "aplay -D $dev -f S16_LE -r 16000 -c 1 -t raw "
        "/data/opt/yandex-station/volprev.pcm >/dev/null 2>&1 & "
        "echo $! > /data/opt/yandex-station/volprev.pid");
}

static void flash_ring_ms(int ms) {
    char cmd[768];
    if (ms < 200) {
        ms = 200;
    }
    if (ms > 20000) {
        ms = 20000;
    }
    snprintf(cmd, sizeof cmd,
             "color=$(cat /data/opt/yandex-station/settings/ring.color 2>/dev/null || echo 3aa0ff); "
             "bright=$(cat /data/opt/yandex-station/settings/ring.bright 2>/dev/null || echo 160); "
             "effect=$(cat /data/opt/yandex-station/settings/ring.effect 2>/dev/null || echo wave); "
             "case $effect in wave|breathe|spin|fill|spark|shift|comet|aurora|twinkle) ;; *) effect=wave ;; esac; "
             "/data/opt/yandex-station/bin/glow \"$color\" %d \"$effect\" \"$bright\" >/dev/null 2>&1 &",
             ms);
    if (system(cmd) != 0) {
        fflush(stdout);
    }
}

static void flash_ring(void) {
    flash_ring_ms(700);
}

static int known_effect(const char *name) {
    return strcmp(name, "wave") == 0 || strcmp(name, "breathe") == 0 || strcmp(name, "spin") == 0
        || strcmp(name, "fill") == 0 || strcmp(name, "spark") == 0 || strcmp(name, "shift") == 0
        || strcmp(name, "comet") == 0 || strcmp(name, "aurora") == 0 || strcmp(name, "twinkle") == 0;
}

static int set_master_volume(int percent) {
    char cmd[96];
    char line[32];
    int fd;
    int n;
    int mixer;
    if (percent < 0 || percent > 100) {
        return -1;
    }
    mixer = mixer_from_percent(percent);
    snprintf(cmd, sizeof cmd, "amixer -c 0 set Master %d >/dev/null 2>&1", mixer);
    if (system(cmd) != 0) {
        return -1;
    }
    fd = open("/data/opt/yandex-station/house.cmd", O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd >= 0) {
        n = snprintf(line, sizeof line, "volume %d\n", mixer);
        if (n > 0 && write(fd, line, (size_t)n) != n) {
            /* the mixer is already at this level */
        }
        close(fd);
    }
    play_preview();
    flash_ring_ms(450);
    return 0;
}

static int hex_color(const char *text) {
    size_t i;
    if (strlen(text) != 6) {
        return 0;
    }
    for (i = 0; text[i]; i++) {
        unsigned char c = (unsigned char)text[i];
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F'))) {
            return 0;
        }
    }
    return 1;
}

static int decimal_range(const char *text, int *out, int max) {
    char *end = NULL;
    long value;
    if (!text || !text[0]) {
        return -1;
    }
    value = strtol(text, &end, 10);
    if (end == text || *end != 0 || value < 0 || value > max) {
        return -1;
    }
    *out = (int)value;
    return 0;
}

static void read_lamp(char *on, size_t on_cap, char *color, size_t color_cap, int *bright) {
    char bright_text[8];
    read_setting("lamp.txt", on, on_cap);
    read_setting("lamp.color", color, color_cap);
    read_setting("lamp.bright", bright_text, sizeof bright_text);
    if (on[0] != '0' && on[0] != '1') {
        snprintf(on, on_cap, "0");
    } else {
        on[1] = 0;
    }
    if (!hex_color(color)) {
        snprintf(color, color_cap, "ffd8a8");
    }
    if (decimal_range(bright_text, bright, 255) != 0) {
        *bright = 180;
    }
}

static void apply_lamp(void) {
    char on[8];
    char color[16];
    char cmd[320];
    int bright = 180;
    read_lamp(on, sizeof on, color, sizeof color, &bright);
    if (on[0] != '1' || bright == 0) {
        if (system("if [ -f /data/opt/yandex-station/glow.pid ]; then "
                   "kill $(cat /data/opt/yandex-station/glow.pid) 2>/dev/null; fi") != 0) {
            fflush(stdout);
        }
        return;
    }
    snprintf(cmd, sizeof cmd,
             "/data/opt/yandex-station/bin/glow %s hold %d >/dev/null 2>&1 &",
             color, bright);
    if (system(cmd) != 0) {
        fflush(stdout);
    }
}

static const char *const STT_ENGINES[] = {"vosk-small", "vosk", "whisper", "gigaam", "tone", "yandex", "ha", NULL};

static int known_news(const char *name) {
    return strcmp(name, "yandex") == 0 || strcmp(name, "openai") == 0 || strcmp(name, "google") == 0;
}

static int known_stt(const char *name) {
    int i;
    for (i = 0; STT_ENGINES[i]; i++) {
        if (strcmp(name, STT_ENGINES[i]) == 0) {
            return 1;
        }
    }
    return 0;
}

/* Разбор на самой колонке есть только у спальни: soda/ и не Мини 2. */
static int local_possible(void) {
    char path[640];
    snprintf(path, sizeof path, "%s/soda/libsoda.so", root_dir);
    return !mini2_present() && access(path, R_OK) == 0;
}

static int ear_local(void) {
    char engine[16];
    read_setting("ear.engine", engine, sizeof engine);
    return local_possible() && strncmp(engine, "cloud", 5) != 0;
}

static void restart_ear(void) {
    char path[640];
    char pid[24];
    long n;
    snprintf(path, sizeof path, "%s/ear.pid", root_dir);
    if (read_line_file(path, pid, sizeof pid) == 0 && (n = strtol(pid, NULL, 10)) > 1) {
        kill((pid_t)n, SIGTERM);
    }
}

static int save_brain_fields(int fd, const char *body) {
    char stt[24];
    char local[8];
    char url[201];
    char model[81];
    char key[201];
    int present = 0;
    if (json_string_field(body, "stt", stt, sizeof stt, &present) != 0 || (present && !known_stt(stt))) {
        send_json(fd, 400, "Bad Request", "{\"error\":\"stt\"}");
        return -1;
    }
    if (present && write_setting("stt.txt", stt, 0644) != 0) {
        send_json(fd, 500, "Error", "{\"error\":\"write\"}");
        return -1;
    }
    if (json_string_field(body, "local", local, sizeof local, &present) != 0) {
        send_json(fd, 400, "Bad Request", "{\"error\":\"local\"}");
        return -1;
    }
    if (present) {
        int want = local[0] == '1' && local_possible();
        if (want != ear_local()) {
            if (write_setting("ear.engine", want ? "local" : "cloud", 0644) != 0) {
                send_json(fd, 500, "Error", "{\"error\":\"write\"}");
                return -1;
            }
            restart_ear();
        }
    }
    if (json_string_field(body, "llm_url", url, sizeof url, &present) != 0 ||
        (present && url[0] && strncmp(url, "http://", 7) != 0 && strncmp(url, "https://", 8) != 0)) {
        send_json(fd, 400, "Bad Request", "{\"error\":\"llm_url\"}");
        return -1;
    }
    if (present && write_setting("llm.url", url, 0644) != 0) {
        send_json(fd, 500, "Error", "{\"error\":\"write\"}");
        return -1;
    }
    if (json_string_field(body, "llm_model", model, sizeof model, &present) != 0) {
        send_json(fd, 400, "Bad Request", "{\"error\":\"llm_model\"}");
        return -1;
    }
    if (present && write_setting("llm.model", model, 0644) != 0) {
        send_json(fd, 500, "Error", "{\"error\":\"write\"}");
        return -1;
    }
    /* Пустой ключ — оставить прежний, «-» — стереть. */
    if (json_string_field(body, "llm_key", key, sizeof key, &present) != 0) {
        send_json(fd, 400, "Bad Request", "{\"error\":\"llm_key\"}");
        return -1;
    }
    if (present && key[0] && write_setting("llm.key", strcmp(key, "-") == 0 ? "" : key, 0600) != 0) {
        send_json(fd, 500, "Error", "{\"error\":\"write\"}");
        return -1;
    }
    if (json_string_field(body, "news", url, sizeof url, &present) != 0 ||
        (present && url[0] && !known_news(url))) {
        send_json(fd, 400, "Bad Request", "{\"error\":\"news\"}");
        return -1;
    }
    if (present && write_setting("news.txt", url[0] ? url : "yandex", 0644) != 0) {
        send_json(fd, 500, "Error", "{\"error\":\"write\"}");
        return -1;
    }
    if (json_string_field(body, "news_url", url, sizeof url, &present) != 0 ||
        (present && url[0] && strncmp(url, "http://", 7) != 0 && strncmp(url, "https://", 8) != 0)) {
        send_json(fd, 400, "Bad Request", "{\"error\":\"news_url\"}");
        return -1;
    }
    if (present && write_setting("news.url", url, 0644) != 0) {
        send_json(fd, 500, "Error", "{\"error\":\"write\"}");
        return -1;
    }
    if (json_string_field(body, "news_model", model, sizeof model, &present) != 0) {
        send_json(fd, 400, "Bad Request", "{\"error\":\"news_model\"}");
        return -1;
    }
    if (present && write_setting("news.model", model, 0644) != 0) {
        send_json(fd, 500, "Error", "{\"error\":\"write\"}");
        return -1;
    }
    if (json_string_field(body, "news_key", key, sizeof key, &present) != 0) {
        send_json(fd, 400, "Bad Request", "{\"error\":\"news_key\"}");
        return -1;
    }
    if (present && key[0] && write_setting("news.key", strcmp(key, "-") == 0 ? "" : key, 0600) != 0) {
        send_json(fd, 500, "Error", "{\"error\":\"write\"}");
        return -1;
    }
    return 0;
}

static void brain_json(char *out, size_t cap) {
    char stt[24];
    char url[201];
    char model[81];
    char news[16];
    char nurl[201];
    char nmodel[81];
    char eurl[420];
    char emodel[180];
    char enurl[420];
    char enmodel[180];
    read_setting("stt.txt", stt, sizeof stt);
    read_setting("llm.url", url, sizeof url);
    read_setting("llm.model", model, sizeof model);
    read_setting("news.txt", news, sizeof news);
    read_setting("news.url", nurl, sizeof nurl);
    read_setting("news.model", nmodel, sizeof nmodel);
    if (!known_stt(stt)) {
        snprintf(stt, sizeof stt, "vosk-small");
    }
    if (!known_news(news)) {
        snprintf(news, sizeof news, "yandex");
    }
    json_escape(url, eurl, sizeof eurl);
    json_escape(model, emodel, sizeof emodel);
    json_escape(nurl, enurl, sizeof enurl);
    json_escape(nmodel, enmodel, sizeof enmodel);
    snprintf(out, cap,
             "\"stt\":\"%s\",\"local\":\"%s\",\"local_ok\":%s,\"llm_url\":\"%s\",\"llm_model\":\"%s\",\"llm_key_set\":%s,"
             "\"news\":\"%s\",\"news_url\":\"%s\",\"news_model\":\"%s\",\"news_key_set\":%s",
             stt, ear_local() ? "1" : "0", local_possible() ? "true" : "false", eurl, emodel,
             setting_set("llm.key") ? "true" : "false",
             news, enurl, enmodel, setting_set("news.key") ? "true" : "false");
}

#define EQ_BANDS 5
#define EQ_LIMIT 12

/* «0 3 -2 0 1»: пять полос в дБ от -12 до 12. Пишет в out ту же строку без лишних пробелов. */
static int eq_normal(const char *text, char *out, size_t cap) {
    int gains[EQ_BANDS];
    int count = 0;
    const char *p = text;
    while (*p && count < EQ_BANDS) {
        char *end;
        long v;
        while (*p == ' ') {
            p++;
        }
        if (!*p) {
            break;
        }
        v = strtol(p, &end, 10);
        if (end == p || v < -EQ_LIMIT || v > EQ_LIMIT || (*end && *end != ' ')) {
            return -1;
        }
        gains[count++] = (int)v;
        p = end;
    }
    while (*p == ' ') {
        p++;
    }
    if (count != EQ_BANDS || *p) {
        return -1;
    }
    snprintf(out, cap, "%d %d %d %d %d", gains[0], gains[1], gains[2], gains[3], gains[4]);
    return 0;
}

static void read_eq(char *out, size_t cap) {
    char raw[64];
    read_setting("eq.txt", raw, sizeof raw);
    if (eq_normal(raw, out, cap) != 0) {
        snprintf(out, cap, "0 0 0 0 0");
    }
}

/* Поле "eq" из тела. 0 — сохранено или его нет, -1 — ответ с ошибкой уже ушёл. */
static int save_eq_field(int fd, const char *body) {
    char eq_in[64];
    char eq[64];
    int present = 0;
    if (json_string_field(body, "eq", eq_in, sizeof eq_in, &present) != 0
        || (present && eq_normal(eq_in, eq, sizeof eq) != 0)) {
        send_json(fd, 400, "Bad Request", "{\"error\":\"eq\"}");
        return -1;
    }
    if (present && write_setting("eq.txt", strcmp(eq, "0 0 0 0 0") == 0 ? "" : eq, 0644) != 0) {
        send_json(fd, 500, "Error", "{\"error\":\"write\"}");
        return -1;
    }
    return 0;
}

static int read_first_line(const char *path, char *out, size_t cap) {
    FILE *fp = fopen(path, "r");
    size_t n;
    if (!fp || !fgets(out, (int)cap, fp)) {
        if (fp) {
            fclose(fp);
        }
        out[0] = 0;
        return -1;
    }
    fclose(fp);
    n = strlen(out);
    while (n > 0 && (out[n - 1] == '\n' || out[n - 1] == '\r')) {
        out[--n] = 0;
    }
    return 0;
}

static int iio_file(const char *chip, const char *leaf, char *out, size_t cap) {
    DIR *dir = opendir("/sys/bus/iio/devices");
    struct dirent *ent;
    if (!dir) {
        return -1;
    }
    while ((ent = readdir(dir)) != NULL) {
        char path[256];
        char name[64];
        if (strncmp(ent->d_name, "iio:device", 10) != 0) {
            continue;
        }
        snprintf(path, sizeof path, "/sys/bus/iio/devices/%s/name", ent->d_name);
        if (read_first_line(path, name, sizeof name) != 0 || strcmp(name, chip) != 0) {
            continue;
        }
        snprintf(out, cap, "/sys/bus/iio/devices/%s/%s", ent->d_name, leaf);
        closedir(dir);
        return 0;
    }
    closedir(dir);
    return -1;
}

static double soc_temp(void) {
    int zone;
    for (zone = 0; zone < 8; zone++) {
        char path[80];
        char text[32];
        snprintf(path, sizeof path, "/sys/class/thermal/thermal_zone%d/type", zone);
        if (read_first_line(path, text, sizeof text) != 0 || strcmp(text, "soc_thermal") != 0) {
            continue;
        }
        snprintf(path, sizeof path, "/sys/class/thermal/thermal_zone%d/temp", zone);
        if (read_first_line(path, text, sizeof text) != 0) {
            return -1;
        }
        return atof(text) / 1000.0;
    }
    return -1;
}

static int open_input_name(const char *needle) {
    int index;
    for (index = 0; index < 16; index++) {
        char path[80];
        char name[64];
        int fd;
        ssize_t n;
        snprintf(path, sizeof path, "/sys/class/input/event%d/device/name", index);
        fd = open(path, O_RDONLY);
        if (fd < 0) {
            continue;
        }
        n = read(fd, name, sizeof name - 1);
        close(fd);
        if (n <= 0) {
            continue;
        }
        name[n] = 0;
        if (!strstr(name, needle)) {
            continue;
        }
        snprintf(path, sizeof path, "/dev/input/event%d", index);
        return open(path, O_RDONLY | O_CLOEXEC | O_NONBLOCK);
    }
    return -1;
}

static int headphone_in(void) {
    int fd = open_input_name("Headphone");
    unsigned char bits[(SW_MAX / 8) + 1];
    int on;
    if (fd < 0) {
        return -1;
    }
    memset(bits, 0, sizeof bits);
    if (ioctl(fd, EVIOCGSW(sizeof bits), bits) < 0) {
        close(fd);
        return -1;
    }
    on = (bits[SW_HEADPHONE_INSERT / 8] >> (SW_HEADPHONE_INSERT % 8)) & 1;
    close(fd);
    return on;
}

static double supply_unit(double raw) {
    if (raw > 100000.0) {
        return raw / 1000000.0;
    }
    if (raw > 50.0) {
        return raw / 1000.0;
    }
    return raw;
}

static void num_field(char *out, size_t cap, double value) {
    if (value < 0) {
        snprintf(out, cap, "null");
        return;
    }
    snprintf(out, cap, "%.1f", value);
}

static void board_json(char *out, size_t cap) {
    char path[256];
    char text[64];
    char lux_s[16];
    char chip_s[16];
    char volt_s[16];
    char amp_s[16];
    char watt_s[16];
    double lux = -1;
    double chip = soc_temp();
    double volts = -1;
    double amps = -1;
    double watts = -1;
    int hp = headphone_in();
    if (iio_file("ltr308als01", "in_intensity_input", path, sizeof path) == 0
        && read_first_line(path, text, sizeof text) == 0) {
        lux = atof(text);
    }
    if (read_first_line("/sys/class/power_supply/ya_pd/voltage_now", text, sizeof text) == 0) {
        volts = supply_unit(atof(text));
    }
    if (read_first_line("/sys/class/power_supply/ya_pd/current_now", text, sizeof text) == 0) {
        amps = supply_unit(atof(text));
    }
    if (volts >= 0 && amps >= 0) {
        watts = volts * amps;
    }
    num_field(lux_s, sizeof lux_s, lux);
    num_field(chip_s, sizeof chip_s, chip);
    num_field(volt_s, sizeof volt_s, volts);
    num_field(amp_s, sizeof amp_s, amps);
    num_field(watt_s, sizeof watt_s, watts);
    snprintf(out, cap,
             "\"lux\":%s,\"chip_c\":%s,\"volts\":%s,\"amps\":%s,\"watts\":%s,\"headphones\":%s",
             lux_s, chip_s, volt_s, amp_s, watt_s,
             hp < 0 ? "null" : (hp ? "true" : "false"));
}

static void send_settings(int fd) {
    char nn[240];
    char voice[80];
    char wake[80];
    char bt[8];
    char login[120];
    char ha[240];
    char saved[40];
    char now[40];
    char ring[8];
    char color[16];
    char bright[8];
    char sound[8];
    char effect[16];
    char lamp[8];
    char lamp_color[16];
    char persona[PERSONA_CAP];
    char vol[8];
    char eq[64];
    int lamp_bright = 180;
    char enn[520];
    char evoice[200];
    char ewake[200];
    char elogin[280];
    char eha[520];
    char epersona[PERSONA_JSON];
    char esaved[120];
    char enow[120];
    char json[32768];
    char brain[2400];
    char board[320];
    int bright_n = 160;
    brain_json(brain, sizeof brain);
    read_setting("nn.txt", nn, sizeof nn);
    read_setting("voice.txt", voice, sizeof voice);
    read_setting("persona.txt", persona, sizeof persona);
    read_setting("wake.txt", wake, sizeof wake);
    read_setting("bt.txt", bt, sizeof bt);
    read_setting("yandex.login", login, sizeof login);
    read_setting("ha.url", ha, sizeof ha);
    read_setting("wifi.ssid", saved, sizeof saved);
    read_setting("ring.txt", ring, sizeof ring);
    read_setting("ring.color", color, sizeof color);
    read_setting("ring.bright", bright, sizeof bright);
    read_setting("wake.sound", sound, sizeof sound);
    read_setting("ring.effect", effect, sizeof effect);
    read_lamp(lamp, sizeof lamp, lamp_color, sizeof lamp_color, &lamp_bright);
    if (ring[0] == '0') {
        snprintf(ring, sizeof ring, "0");
    } else {
        snprintf(ring, sizeof ring, "1");
    }
    if (!hex_color(color)) {
        snprintf(color, sizeof color, "3aa0ff");
    }
    if (decimal_range(bright, &bright_n, 255) != 0) {
        bright_n = 160;
    }
    if (sound[0] == '0') {
        snprintf(sound, sizeof sound, "0");
    } else {
        snprintf(sound, sizeof sound, "1");
    }
    if (!known_effect(effect)) {
        snprintf(effect, sizeof effect, "wave");
    }
    snprintf(vol, sizeof vol, "%d", percent_from_mixer(master_volume()));
    read_eq(eq, sizeof eq);
    current_ssid(now, sizeof now);
    json_escape(nn, enn, sizeof enn);
    json_escape(voice, evoice, sizeof evoice);
    json_escape(persona, epersona, sizeof epersona);
    json_escape(wake, ewake, sizeof ewake);
    json_escape(login, elogin, sizeof elogin);
    json_escape(ha, eha, sizeof eha);
    json_escape(saved, esaved, sizeof esaved);
    json_escape(now, enow, sizeof enow);
    board_json(board, sizeof board);
    snprintf(json, sizeof json,
             "{\"nn\":\"%s\",\"voice\":\"%s\",\"persona\":\"%s\",\"wake\":\"%s\",\"bt\":\"%s\",\"yandex_login\":\"%s\",\"yandex_set\":%s,"
             "\"ha_url\":\"%s\",\"ha_set\":%s,\"wifi_ssid\":\"%s\",\"wifi_set\":%s,\"wifi_now\":\"%s\","
             "\"ring\":\"%s\",\"ring_color\":\"%s\",\"ring_bright\":\"%d\",\"ring_effect\":\"%s\",\"wake_sound\":\"%s\",\"volume\":\"%s\","
             "\"lamp\":\"%s\",\"lamp_color\":\"%s\",\"lamp_bright\":\"%d\",\"eq\":\"%s\",%s,\"noise\":\"stock\",%s}",
             enn, evoice, epersona, ewake, bt[0] == '1' ? "1" : "0", elogin, setting_set("yandex.secret") ? "true" : "false",
             eha, setting_set("ha.token") ? "true" : "false",
             esaved, setting_set("wifi.psk") ? "true" : "false", enow,
             ring, color, bright_n, effect, sound, vol, lamp, lamp_color, lamp_bright, eq, board, brain);
    send_json(fd, 200, "OK", json);
}

static void send_scan(int fd) {
    struct Net {
        char ssid[33];
        int dbm;
        int band;
    } nets[32];
    int count = 0;
    char now[40];
    char enow[120];
    char line[512];
    char *json;
    FILE *fp;
    size_t used;
    int i;
    current_ssid(now, sizeof now);
    fp = popen("wpa_cli -i wlan0 scan >/dev/null 2>&1; sleep 4; wpa_cli -i wlan0 scan_results 2>/dev/null", "r");
    if (!fp) {
        send_json(fd, 500, "Error", "{\"error\":\"scan\"}");
        return;
    }
    while (fgets(line, sizeof line, fp)) {
        char *p = line;
        char *bssid;
        char *freq;
        char *level;
        int dbm;
        int mhz;
        int band;
        char ssid[33];
        size_t slen;
        while (*p == ' ' || *p == '\t') {
            p++;
        }
        if (*p == 0 || *p == '\n' || strncmp(p, "bssid", 5) == 0) {
            continue;
        }
        bssid = p;
        while (*p && *p != ' ' && *p != '\t') {
            p++;
        }
        if (*p == 0 || strchr(bssid, ':') == NULL) {
            continue;
        }
        *p++ = 0;
        while (*p == ' ' || *p == '\t') {
            p++;
        }
        freq = p;
        while (*p && *p != ' ' && *p != '\t') {
            p++;
        }
        if (*p == 0) {
            continue;
        }
        *p++ = 0;
        while (*p == ' ' || *p == '\t') {
            p++;
        }
        level = p;
        while (*p && *p != ' ' && *p != '\t') {
            p++;
        }
        if (*p) {
            *p++ = 0;
        }
        mhz = atoi(freq);
        dbm = atoi(level);
        if (mhz < 2000 || dbm >= 0 || dbm < -120) {
            continue;
        }
        while (*p == ' ' || *p == '\t' || *p == '[') {
            if (*p == '[') {
                while (*p && *p != ' ' && *p != '\t' && *p != '\n' && *p != '\r') {
                    p++;
                }
            }
            while (*p == ' ' || *p == '\t') {
                p++;
            }
        }
        slen = strcspn(p, "\r\n");
        while (slen > 0 && (p[slen - 1] == ' ' || p[slen - 1] == '\t')) {
            slen--;
        }
        if (slen == 0 || slen > 32) {
            continue;
        }
        memcpy(ssid, p, slen);
        ssid[slen] = 0;
        band = mhz >= 3000 ? 5 : 2;
        if (count < 32) {
            snprintf(nets[count].ssid, sizeof nets[count].ssid, "%s", ssid);
            nets[count].dbm = dbm;
            nets[count].band = band;
            count++;
        }
    }
    pclose(fp);
    for (i = 1; i < count; i++) {
        struct Net key = nets[i];
        int j = i;
        while (j > 0 && nets[j - 1].dbm < key.dbm) {
            nets[j] = nets[j - 1];
            j--;
        }
        nets[j] = key;
    }
    json = malloc(8192);
    if (!json) {
        send_json(fd, 500, "Error", "{\"error\":\"scan\"}");
        return;
    }
    json_escape(now, enow, sizeof enow);
    used = (size_t)snprintf(json, 8192, "{\"now\":\"%s\",\"nets\":[", enow);
    for (i = 0; i < count; i++) {
        char essid[160];
        char chunk[240];
        int n;
        json_escape(nets[i].ssid, essid, sizeof essid);
        n = snprintf(chunk, sizeof chunk, "%s{\"ssid\":\"%s\",\"dbm\":%d,\"band\":%d,\"current\":%s}",
                     i ? "," : "", essid, nets[i].dbm, nets[i].band,
                     strcmp(nets[i].ssid, now) == 0 ? "true" : "false");
        if (n < 0 || used + (size_t)n + 3 >= 8192) {
            break;
        }
        memcpy(json + used, chunk, (size_t)n);
        used += (size_t)n;
        json[used] = 0;
    }
    memcpy(json + used, "]}", 3);
    send_json(fd, 200, "OK", json);
    free(json);
}

static int play_pcm(const void *data, size_t n) {
    char path[768];
    char cmd[900];
    int rc;
    if (snprintf(path, sizeof path, "%s/play.raw", root_dir) >= (int)sizeof path) {
        return -1;
    }
    if (write_file(path, data, n) != 0) {
        return -1;
    }
    if (snprintf(cmd, sizeof cmd, "aplay -D hw:0,0 -r 32000 -f S16_LE -c 1 -q %s", path) >= (int)sizeof cmd) {
        return -1;
    }
    rc = system(cmd);
    return rc == 0 ? 0 : -1;
}

static void alarm_left(time_t delta, char *out, size_t cap) {
    long mins;
    long hours;
    if (delta <= 0) {
        snprintf(out, cap, "сейчас");
        return;
    }
    mins = (long)((delta + 59) / 60);
    if (mins < 1) {
        mins = 1;
    }
    if (mins < 60) {
        snprintf(out, cap, "через %ld мин", mins);
        return;
    }
    hours = mins / 60;
    mins %= 60;
    if (mins == 0) {
        snprintf(out, cap, "через %ld ч", hours);
    } else {
        snprintf(out, cap, "через %ld ч %ld мин", hours, mins);
    }
}

static const char *const WEEKDAY_SHORT[7] = {"пн", "вт", "ср", "чт", "пт", "сб", "вс"};

static void alarm_repeat_text(const char *days, char *out, size_t cap) {
    size_t used = 0;
    int i;
    out[0] = 0;
    if (days[0] == '-') {
        snprintf(out, cap, "один раз");
        return;
    }
    if (strcmp(days, "1111111") == 0) {
        snprintf(out, cap, "каждый день");
        return;
    }
    if (strcmp(days, "1111100") == 0) {
        snprintf(out, cap, "по будням");
        return;
    }
    if (strcmp(days, "0000011") == 0) {
        snprintf(out, cap, "по выходным");
        return;
    }
    for (i = 0; i < 7 && used < cap; i++) {
        if (days[i] == '1') {
            int n = snprintf(out + used, cap - used, "%s%s", used ? ", " : "", WEEKDAY_SHORT[i]);
            if (n < 0) {
                break;
            }
            used += (size_t)n;
        }
    }
}

static void alarm_when(const struct alarm_item *it, time_t now, int tz, char *out, size_t cap) {
    long long day;
    long long today;
    if (!it->on || it->next <= 0) {
        snprintf(out, cap, "%02d:%02d", it->hour, it->minute);
        return;
    }
    day = (it->next + tz) / 86400;
    today = ((long long)now + tz) / 86400;
    if (day == today) {
        snprintf(out, cap, "%02d:%02d", it->hour, it->minute);
    } else if (day == today + 1) {
        snprintf(out, cap, "%02d:%02d завтра", it->hour, it->minute);
    } else {
        snprintf(out, cap, "%02d:%02d, %s", it->hour, it->minute, WEEKDAY_SHORT[(day + 3) % 7]);
    }
}

static int format_alarms(char *json, size_t cap) {
    struct alarm_item items[ALARMS_MAX];
    time_t now = time(NULL);
    int tz = alarm_tz();
    int n = alarms_load(items, ALARMS_MAX);
    size_t used = 0;
    int i;
    int wrote = snprintf(json, cap, "{\"alarms\":[");
    if (wrote < 0 || (size_t)wrote >= cap) {
        return -1;
    }
    used = (size_t)wrote;
    alarms_sort(items, n);
    for (i = 0; i < n; i++) {
        const struct alarm_item *it = &items[i];
        int live = it->on && it->next > 0;
        char when[40];
        char left[48];
        char repeat[48];
        char piece[512];
        int piece_n;
        alarm_when(it, now, tz, when, sizeof when);
        if (live) {
            alarm_left((time_t)(it->next - now), left, sizeof left);
        } else {
            snprintf(left, sizeof left, "выключен");
        }
        alarm_repeat_text(it->days, repeat, sizeof repeat);
        piece_n = snprintf(piece, sizeof piece,
                           "%s{\"id\":\"%lld\",\"at\":\"%lld\",\"time\":\"%02d:%02d\",\"days\":\"%s\","
                           "\"repeat\":\"%s\",\"on\":%s,\"label\":\"%s\",\"when\":\"%s\",\"left\":\"%s\"}",
                           i ? "," : "", it->id, live ? it->next : 0LL, it->hour, it->minute, it->days,
                           repeat, it->on ? "true" : "false", it->label, when, left);
        if (piece_n < 0 || used + (size_t)piece_n >= cap) {
            return -1;
        }
        memcpy(json + used, piece, (size_t)piece_n);
        used += (size_t)piece_n;
    }
    if (used + 3 >= cap) {
        return -1;
    }
    memcpy(json + used, "]}", 3);
    return 0;
}

static void send_alarms(int fd) {
    char json[ALARMS_MAX * 512 + 64];
    if (format_alarms(json, sizeof json) != 0) {
        send_json(fd, 500, "Error", "{\"error\":\"alarms\"}");
        return;
    }
    send_json(fd, 200, "OK", json);
}

static void alarm_stop_ring(void) {
    FILE *stop = fopen("/data/opt/yandex-station/alarm.stop", "w");
    if (stop) {
        fclose(stop);
    }
}

static int clock_hm(const char *text, int *hour, int *minute) {
    char *end = NULL;
    const char *minute_at;
    if (!text || !text[0]) {
        return -1;
    }
    *hour = (int)strtol(text, &end, 10);
    if (end == text || *end != ':') {
        return -1;
    }
    minute_at = end + 1;
    *minute = (int)strtol(minute_at, &end, 10);
    if (end == minute_at || (*end != 0 && *end != ':')) {
        return -1;
    }
    return *hour < 0 || *hour > 23 || *minute < 0 || *minute > 59 ? -1 : 0;
}

static int alarm_find(const struct alarm_item *items, int n, long long id) {
    int i;
    for (i = 0; i < n; i++) {
        if (items[i].id == id) {
            return i;
        }
    }
    return -1;
}

/* Новый будильник: на часы hour:minute в дни days ("-" — разовый) или ровно на stamp, если он > 0. */
static int alarm_add(int hour, int minute, const char *days, long long stamp, const char *label) {
    struct alarm_item items[ALARMS_MAX];
    struct alarm_item it;
    time_t now = time(NULL);
    int tz = alarm_tz();
    int n = alarms_load(items, ALARMS_MAX);
    int i;
    memset(&it, 0, sizeof it);
    if (stamp > 0) {
        long long local = stamp + tz;
        if (stamp <= (long long)now) {
            return -1;
        }
        hour = (int)((local % 86400) / 3600);
        minute = (int)((local % 3600) / 60);
        days = "-";
    }
    if (!alarm_days_ok(days)) {
        return -1;
    }
    it.hour = hour;
    it.minute = minute;
    it.on = 1;
    snprintf(it.days, sizeof it.days, "%s", days);
    snprintf(it.label, sizeof it.label, "%s", label ? label : "");
    alarm_clean_label(it.label);
    it.next = stamp > 0 ? stamp : alarm_next_after(now, hour, minute, days, tz);
    if (it.next <= 0) {
        return -1;
    }
    for (i = 0; i < n; i++) {
        if (items[i].hour == hour && items[i].minute == minute && strcmp(items[i].days, it.days) == 0
            && (it.days[0] != '-' || items[i].next == it.next)) {
            items[i].on = 1;
            items[i].next = it.next;
            if (it.label[0]) {
                snprintf(items[i].label, sizeof items[i].label, "%s", it.label);
            }
            return alarms_save(items, n);
        }
    }
    if (n >= ALARMS_MAX) {
        return -1;
    }
    it.id = (long long)now;
    while (alarm_find(items, n, it.id) >= 0) {
        it.id++;
    }
    items[n++] = it;
    unlink("/data/opt/yandex-station/alarm.stop");
    return alarms_save(items, n);
}

/* op: del, on, off, edit (новые hour/minute/days/label). */
static int alarm_change(long long id, const char *op, int hour, int minute, const char *days, const char *label) {
    struct alarm_item items[ALARMS_MAX];
    time_t now = time(NULL);
    int tz = alarm_tz();
    int n = alarms_load(items, ALARMS_MAX);
    int i = alarm_find(items, n, id);
    struct alarm_item *it;
    if (i < 0) {
        return -1;
    }
    it = &items[i];
    if (strcmp(op, "del") == 0) {
        if (it->on && it->next > 0 && it->next <= (long long)now) {
            alarm_stop_ring();
        }
        memmove(it, it + 1, (size_t)(n - i - 1) * sizeof *it);
        return alarms_save(items, n - 1);
    }
    if (strcmp(op, "edit") == 0) {
        if (days && days[0]) {
            if (!alarm_days_ok(days)) {
                return -1;
            }
            snprintf(it->days, sizeof it->days, "%s", days);
        }
        if (hour >= 0) {
            it->hour = hour;
            it->minute = minute;
        }
        if (label) {
            snprintf(it->label, sizeof it->label, "%s", label);
            alarm_clean_label(it->label);
        }
        it->on = 1;
    } else if (strcmp(op, "on") == 0) {
        it->on = 1;
    } else if (strcmp(op, "off") == 0) {
        it->on = 0;
        it->next = 0;
        return alarms_save(items, n);
    } else {
        return -1;
    }
    it->next = alarm_next_after(now, it->hour, it->minute, it->days, tz);
    return it->next > 0 ? alarms_save(items, n) : -1;
}

static void alarms_clear(void) {
    alarms_save(NULL, 0);
    alarm_stop_ring();
}

static void drop_alarm_at(long long at) {
    struct alarm_item items[ALARMS_MAX];
    int n = alarms_load(items, ALARMS_MAX);
    int i;
    for (i = 0; i < n; i++) {
        if (items[i].on && items[i].next == at) {
            alarm_change(items[i].id, "del", -1, 0, NULL, NULL);
            return;
        }
    }
}

static void send_heard(int fd) {
    char text[240];
    char escaped[520];
    char json[640];
    int file;
    ssize_t n;
    text[0] = 0;
    file = open("/data/opt/yandex-station/heard.txt", O_RDONLY);
    if (file >= 0) {
        n = read(file, text, sizeof text - 1);
        close(file);
        if (n < 0) {
            n = 0;
        }
        text[n] = 0;
    }
    json_escape(text, escaped, sizeof escaped);
    snprintf(json, sizeof json, "{\"text\":\"%s\"}", escaped);
    send_json(fd, 200, "OK", json);
}

static void send_ear(int fd) {
    char path[768];
    int file;
    struct LiveRing ring;
    uint32_t pos;
    uint32_t n;
    uint32_t i;
    uint64_t acc = 0;
    uint32_t rms = 0;
    uint32_t bit;
    char json[128];
    snprintf(path, sizeof path, "%s/live.ring", root_dir);
    file = open(path, O_RDONLY);
    if (file < 0 || read_full(file, &ring, sizeof ring) != 0 || ring.magic != LIVE_MAGIC) {
        if (file >= 0) {
            close(file);
        }
        send_json(fd, 200, "OK", "{\"write\":0,\"rms\":0}");
        return;
    }
    close(file);
    pos = atomic_load_explicit(&ring.write_samples, memory_order_acquire);
    n = pos < 1600u ? pos : 1600u;
    for (i = 0; i < n; i++) {
        int32_t sample = ring.pcm[(pos - n + i) % LIVE_SAMPLES];
        acc += (uint64_t)((int64_t)sample * sample);
    }
    if (n > 0) {
        uint32_t mean = (uint32_t)(acc / n);
        bit = 1u << 15;
        while (bit) {
            uint32_t next = rms + bit;
            if (next <= 65535u && next * next <= mean) {
                rms = next;
            }
            bit >>= 1;
        }
    }
    snprintf(json, sizeof json, "{\"write\":%u,\"rms\":%u}", pos, rms);
    send_json(fd, 200, "OK", json);
}

static int read_line_file(const char *path, char *out, size_t cap) {
    int fd;
    ssize_t n;
    out[0] = 0;
    fd = open(path, O_RDONLY);
    if (fd < 0) {
        return -1;
    }
    n = read(fd, out, cap - 1);
    close(fd);
    if (n < 0) {
        out[0] = 0;
        return -1;
    }
    out[n] = 0;
    while (n > 0 && (out[n - 1] == '\n' || out[n - 1] == '\r')) {
        out[--n] = 0;
    }
    return 0;
}

static void lower_ru(const char *in, char *out, size_t cap) {
    size_t o = 0;
    const unsigned char *p = (const unsigned char *)in;
    if (cap == 0) {
        return;
    }
    while (*p && o + 4 < cap) {
        if (*p >= 'A' && *p <= 'Z') {
            out[o++] = (char)(*p + ('a' - 'A'));
            p++;
        } else if (p[0] == 0xD0 && p[1] == 0x81) {
            out[o++] = (char)0xD1;
            out[o++] = (char)0x91;
            p += 2;
        } else if (p[0] == 0xD0 && p[1] >= 0x90 && p[1] <= 0x9F) {
            out[o++] = (char)0xD0;
            out[o++] = (char)(p[1] + 0x20);
            p += 2;
        } else if (p[0] == 0xD0 && p[1] >= 0xA0 && p[1] <= 0xAF) {
            out[o++] = (char)0xD1;
            out[o++] = (char)(p[1] - 0x20);
            p += 2;
        } else {
            out[o++] = (char)*p++;
        }
    }
    out[o] = 0;
}

static void translit_id(const char *title, char *id, size_t cap) {
    static const char *map[] = {
        "a", "b", "v", "g", "d", "e", "zh", "z", "i", "i", "k", "l", "m", "n", "o",
        "p", "r", "s", "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"
    };
    size_t o = 0;
    const unsigned char *p = (const unsigned char *)title;
    id[0] = 0;
    while (*p && o + 8 < cap) {
        if ((*p >= 'a' && *p <= 'z') || (*p >= '0' && *p <= '9')) {
            id[o++] = (char)*p++;
        } else if (*p >= 'A' && *p <= 'Z') {
            id[o++] = (char)(*p + 32);
            p++;
        } else if (*p == ' ' || *p == '-' || *p == '_') {
            if (o > 0 && id[o - 1] != '-') {
                id[o++] = '-';
            }
            p++;
        } else if (p[0] == 0xD0 && p[1] == 0x81) {
            id[o++] = 'e';
            p += 2;
        } else if (p[0] == 0xD1 && p[1] == 0x91) {
            id[o++] = 'e';
            p += 2;
        } else if (p[0] == 0xD0 && p[1] >= 0x90 && p[1] <= 0xAF) {
            int idx = p[1] - 0x90;
            const char *s = map[idx];
            p += 2;
            while (*s && o + 2 < cap) {
                id[o++] = *s++;
            }
        } else if (p[0] == 0xD0 && p[1] >= 0xA0 && p[1] <= 0xAF) {
            int idx = 16 + (p[1] - 0xA0);
            const char *s = map[idx];
            p += 2;
            while (*s && o + 2 < cap) {
                id[o++] = *s++;
            }
        } else if (p[0] == 0xD0 && p[1] >= 0xB0 && p[1] <= 0xBF) {
            int idx = p[1] - 0xB0;
            const char *s = map[idx];
            p += 2;
            while (*s && o + 2 < cap) {
                id[o++] = *s++;
            }
        } else if (p[0] == 0xD1 && p[1] >= 0x80 && p[1] <= 0x8F) {
            int idx = 16 + (p[1] - 0x80);
            const char *s = map[idx];
            p += 2;
            while (*s && o + 2 < cap) {
                id[o++] = *s++;
            }
        } else {
            p++;
        }
    }
    while (o > 0 && id[o - 1] == '-') {
        o--;
    }
    id[o] = 0;
    if (id[0] == 0) {
        snprintf(id, cap, "phrase");
    }
}

static int phrase_dir(const char *id, char *out, size_t cap) {
    return snprintf(out, cap, "%s/phrases/%s", root_dir, id) > 0 && strlen(out) < cap;
}

static int id_taken(const char *id) {
    char path[768];
    struct stat st;
    if (!phrase_dir(id, path, sizeof path)) {
        return 1;
    }
    if (stat(path, &st) == 0 && S_ISDIR(st.st_mode)) {
        return 1;
    }
    snprintf(path, sizeof path, "%s/presets/%s", root_dir, id);
    return stat(path, &st) == 0 && S_ISDIR(st.st_mode);
}

static void unique_id(char *id, size_t cap) {
    char base[64];
    int n = 2;
    snprintf(base, sizeof base, "%s", id);
    while (id_taken(id)) {
        snprintf(id, cap, "%s-%d", base, n++);
        if (n > 50) {
            break;
        }
    }
}

static void active_id(char *out, size_t cap) {
    char path[640];
    snprintf(path, sizeof path, "%s/active", root_dir);
    if (read_line_file(path, out, cap) != 0) {
        out[0] = 0;
    }
}

static int set_active(const char *id) {
    char path[640];
    snprintf(path, sizeof path, "%s/active", root_dir);
    if (!id || !id[0]) {
        unlink(path);
        return 0;
    }
    return write_file(path, id, strlen(id));
}

void start_bridge(const char *root);

static int model_ready(const char *id) {
    char path[768];
    struct stat st;
    if (!safe_id(id)) {
        return 0;
    }
    if (phrase_dir(id, path, sizeof path) && stat(path, &st) == 0 && S_ISDIR(st.st_mode)) {
        return 1;
    }
    snprintf(path, sizeof path, "%s/presets/%s", root_dir, id);
    return stat(path, &st) == 0 && S_ISDIR(st.st_mode);
}

static int parse_field(const char *body, const char *key, char *out, size_t cap) {
    char needle[32];
    const char *p;
    const char *e;
    size_t n;
    snprintf(needle, sizeof needle, "\"%s\"", key);
    p = strstr(body, needle);
    if (!p) {
        return -1;
    }
    p = strchr(p, ':');
    if (!p) {
        return -1;
    }
    p++;
    while (*p == ' ' || *p == '\t') {
        p++;
    }
    if (*p != '"') {
        return -1;
    }
    p++;
    e = p;
    while (*e && *e != '"') {
        if (*e == '\\' && e[1]) {
            e += 2;
        } else {
            e++;
        }
    }
    n = (size_t)(e - p);
    if (n == 0 || n >= cap) {
        return -1;
    }
    memcpy(out, p, n);
    out[n] = 0;
    return 0;
}

static int parse_yvk(const char *body, size_t n, char *phrase, size_t cap) {
    const char *p;
    const char *e;
    size_t len;
    if (n < 6 || (memcmp(body, "YVK0\n", 5) != 0 && memcmp(body, "YVK1\n", 5) != 0)) {
        return -1;
    }
    p = body + 5;
    e = p;
    while ((size_t)(e - body) < n && *e && *e != '\n' && *e != '\r') {
        e++;
    }
    len = (size_t)(e - p);
    if (len == 0 || len >= cap) {
        return -1;
    }
    memcpy(phrase, p, len);
    phrase[len] = 0;
    return 0;
}

static int append_model(char *buf, size_t cap, int *first, const char *id, const char *title,
                        const char *phrase, const char *kind, int active) {
    char et[256];
    char ep[256];
    char chunk[900];
    int k;
    json_escape(title ? title : "", et, sizeof et);
    json_escape(phrase ? phrase : "", ep, sizeof ep);
    k = snprintf(chunk, sizeof chunk,
                 "%s{\"id\":\"%s\",\"title\":\"%s\",\"phrase\":\"%s\",\"kind\":\"%s\",\"active\":%s}",
                 *first ? "" : ",", id, et, ep, kind, active ? "true" : "false");
    if (k < 0 || (size_t)k >= sizeof chunk || strlen(buf) + (size_t)k + 2 >= cap) {
        return -1;
    }
    memcpy(buf + strlen(buf), chunk, (size_t)k + 1);
    *first = 0;
    return 0;
}

static void scan_models(char *buf, size_t cap, int *first, const char *subdir, const char *kind,
                        const char *active) {
    char dirpath[640];
    DIR *dir;
    struct dirent *de;
    snprintf(dirpath, sizeof dirpath, "%s/%s", root_dir, subdir);
    dir = opendir(dirpath);
    if (!dir) {
        return;
    }
    while ((de = readdir(dir)) != NULL) {
        char base[768];
        char path[800];
        char title[192];
        char phrase[192];
        if (!safe_id(de->d_name)) {
            continue;
        }
        snprintf(base, sizeof base, "%s/%s", dirpath, de->d_name);
        snprintf(path, sizeof path, "%s/phrase.txt", base);
        title[0] = 0;
        read_line_file(path, title, sizeof title);
        snprintf(path, sizeof path, "%s/return.txt", base);
        phrase[0] = 0;
        read_line_file(path, phrase, sizeof phrase);
        if (title[0] == 0) {
            snprintf(title, sizeof title, "%s", de->d_name);
        }
        if (append_model(buf, cap, first, de->d_name, title, phrase, kind,
                         strcmp(active, de->d_name) == 0) != 0) {
            break;
        }
    }
    closedir(dir);
}

static int list_models(char **out) {
    char *buf;
    char active[64];
    int first = 1;
    buf = malloc(16384);
    if (!buf) {
        return -1;
    }
    buf[0] = '[';
    buf[1] = 0;
    active_id(active, sizeof active);
    if (active[0] == 0) {
        snprintf(active, sizeof active, "yandex");
    }
    scan_models(buf, 16384, &first, "presets", "preset", active);
    scan_models(buf, 16384, &first, "phrases", "upload", active);
    if (strlen(buf) + 2 >= 16384) {
        free(buf);
        return -1;
    }
    memcpy(buf + strlen(buf), "]", 2);
    *out = buf;
    return 0;
}

typedef struct {
    char *data;
    size_t len;
    size_t cap;
} Buf;

static int buf_add(Buf *b, const char *s) {
    size_t n = strlen(s);
    if (b->len + n + 1 > b->cap) {
        return -1;
    }
    memcpy(b->data + b->len, s, n);
    b->len += n;
    b->data[b->len] = 0;
    return 0;
}

static int list_phrases(char **out) {
    char phrases[640];
    char active[64];
    DIR *dir;
    struct dirent *de;
    Buf b;
    int first = 1;
    snprintf(phrases, sizeof phrases, "%s/phrases", root_dir);
    mkdir_p(phrases);
    active_id(active, sizeof active);
    b.cap = LIST_CAP;
    b.data = malloc(b.cap);
    b.len = 0;
    if (!b.data) {
        return -1;
    }
    b.data[0] = 0;
    if (buf_add(&b, "[") != 0) {
        free(b.data);
        return -1;
    }
    dir = opendir(phrases);
    if (!dir) {
        buf_add(&b, "]");
        *out = b.data;
        return 0;
    }
    while ((de = readdir(dir)) != NULL) {
        char pdir[768];
        char title_path[800];
        char ret_path[800];
        char model_path[800];
        char name_path[800];
        char title[256];
        char ret[256];
        char model_name[192];
        char etitle[512];
        char eret[512];
        char ename[256];
        char chunk[1600];
        struct stat st;
        if (!safe_id(de->d_name)) {
            continue;
        }
        if (!path_join(pdir, sizeof pdir, phrases, de->d_name)) {
            continue;
        }
        snprintf(title_path, sizeof title_path, "%s/phrase.txt", pdir);
        snprintf(ret_path, sizeof ret_path, "%s/return.txt", pdir);
        snprintf(model_path, sizeof model_path, "%s/model.bin", pdir);
        snprintf(name_path, sizeof name_path, "%s/model.name", pdir);
        title[0] = 0;
        ret[0] = 0;
        model_name[0] = 0;
        read_line_file(title_path, title, sizeof title);
        read_line_file(ret_path, ret, sizeof ret);
        read_line_file(name_path, model_name, sizeof model_name);
        json_escape(title, etitle, sizeof etitle);
        json_escape(ret, eret, sizeof eret);
        json_escape(model_name, ename, sizeof ename);
        if (!first && buf_add(&b, ",") != 0) {
            closedir(dir);
            free(b.data);
            return -1;
        }
        first = 0;
        if (stat(model_path, &st) == 0 && S_ISREG(st.st_mode)) {
            snprintf(chunk, sizeof chunk,
                     "{\"id\":\"%s\",\"title\":\"%s\",\"returns\":\"%s\",\"active\":%s,"
                     "\"model\":{\"name\":\"%s\",\"bytes\":%lld}}",
                     de->d_name, etitle, eret,
                     strcmp(active, de->d_name) == 0 ? "true" : "false",
                     ename[0] ? ename : "model.bin", (long long)st.st_size);
        } else {
            snprintf(chunk, sizeof chunk,
                     "{\"id\":\"%s\",\"title\":\"%s\",\"returns\":\"%s\",\"active\":%s,\"model\":null}",
                     de->d_name, etitle, eret,
                     strcmp(active, de->d_name) == 0 ? "true" : "false");
        }
        if (buf_add(&b, chunk) != 0) {
            closedir(dir);
            free(b.data);
            return -1;
        }
    }
    closedir(dir);
    if (buf_add(&b, "]") != 0) {
        free(b.data);
        return -1;
    }
    *out = b.data;
    return 0;
}

static int parse_title(const char *body, char *title, size_t cap) {
    const char *p = strstr(body, "\"title\"");
    const char *e;
    size_t n;
    if (!p) {
        return -1;
    }
    p = strchr(p, ':');
    if (!p) {
        return -1;
    }
    p++;
    while (*p == ' ' || *p == '\t') {
        p++;
    }
    if (*p != '"') {
        return -1;
    }
    p++;
    e = p;
    while (*e && *e != '"') {
        if (*e == '\\' && e[1]) {
            e += 2;
        } else {
            e++;
        }
    }
    n = (size_t)(e - p);
    if (n == 0 || n >= cap) {
        return -1;
    }
    memcpy(title, p, n);
    title[n] = 0;
    if (strchr(title, '\n') || strchr(title, '\r')) {
        return -1;
    }
    return 0;
}

static int create_phrase(const char *title, char *id_out, size_t id_cap) {
    char phrases[640];
    char dir[768];
    char path[800];
    char lowered[256];
    char id[64];
    snprintf(phrases, sizeof phrases, "%s/phrases", root_dir);
    if (mkdir_p(phrases) != 0) {
        return -1;
    }
    translit_id(title, id, sizeof id);
    unique_id(id, sizeof id);
    if (!safe_id(id) || !phrase_dir(id, dir, sizeof dir)) {
        return -1;
    }
    if (mkdir_p(dir) != 0) {
        return -1;
    }
    snprintf(path, sizeof path, "%s/phrase.txt", dir);
    if (write_file(path, title, strlen(title)) != 0) {
        return -1;
    }
    lower_ru(title, lowered, sizeof lowered);
    snprintf(path, sizeof path, "%s/return.txt", dir);
    if (write_file(path, lowered, strlen(lowered)) != 0) {
        return -1;
    }
    snprintf(id_out, id_cap, "%s", id);
    return 0;
}

static int delete_phrase(const char *id) {
    char dir[768];
    char path[800];
    char active[64];
    if (!phrase_dir(id, dir, sizeof dir)) {
        return -1;
    }
    snprintf(path, sizeof path, "%s/phrase.txt", dir);
    unlink(path);
    snprintf(path, sizeof path, "%s/return.txt", dir);
    unlink(path);
    snprintf(path, sizeof path, "%s/model.bin", dir);
    unlink(path);
    snprintf(path, sizeof path, "%s/model.name", dir);
    unlink(path);
    rmdir(dir);
    active_id(active, sizeof active);
    if (strcmp(active, id) == 0) {
        set_active("");
    }
    return 0;
}

static void send_response(int fd, int code, const char *reason, const char *type, const char *body, size_t n) {
    char head[512];
    int h = snprintf(head, sizeof head,
                     "HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %u\r\n"
                     "Connection: close\r\nCache-Control: no-store\r\n\r\n",
                     code, reason, type, (unsigned)n);
    if (h > 0) {
        write_all(fd, head, (size_t)h);
    }
    if (n > 0 && body) {
        write_all(fd, body, n);
    }
}

static void send_json(int fd, int code, const char *reason, const char *json) {
    send_response(fd, code, reason, "application/json; charset=utf-8", json, strlen(json));
}

static void send_file(int fd, const char *path, const char *type) {
    int file;
    struct stat st;
    char *buf;
    file = open(path, O_RDONLY);
    if (file < 0 || fstat(file, &st) != 0 || st.st_size < 0 || st.st_size > 1024 * 1024) {
        if (file >= 0) {
            close(file);
        }
        send_json(fd, 500, "Error", "{\"error\":\"page\"}");
        return;
    }
    buf = malloc((size_t)st.st_size);
    if (!buf || read_full(file, buf, (size_t)st.st_size) != 0) {
        free(buf);
        close(file);
        send_json(fd, 500, "Error", "{\"error\":\"page\"}");
        return;
    }
    close(file);
    send_response(fd, 200, "OK", type, buf, (size_t)st.st_size);
    free(buf);
}

static int header_value(const char *headers, const char *name, char *out, size_t cap) {
    size_t nlen = strlen(name);
    const char *p = headers;
    out[0] = 0;
    while (*p) {
        if (strncasecmp(p, name, nlen) == 0 && p[nlen] == ':') {
            size_t n;
            p += nlen + 1;
            while (*p == ' ' || *p == '\t') {
                p++;
            }
            n = strcspn(p, "\r\n");
            if (n >= cap) {
                n = cap - 1;
            }
            memcpy(out, p, n);
            out[n] = 0;
            return 0;
        }
        p = strchr(p, '\n');
        if (!p) {
            break;
        }
        p++;
    }
    return -1;
}

static int hex_id(const char *text) {
    size_t i;
    if (!text || strlen(text) != 8) {
        return 0;
    }
    for (i = 0; i < 8; i++) {
        char c = text[i];
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) {
            return 0;
        }
    }
    return 1;
}

static int music_kind(const char *kind) {
    size_t i;
    if (!kind || !kind[0]) {
        return 0;
    }
    if (strcmp(kind, "wave") == 0 || strcmp(kind, "likes") == 0) {
        return 1;
    }
    for (i = 0; kind[i]; i++) {
        if (kind[i] < '0' || kind[i] > '9') {
            return 0;
        }
    }
    return 1;
}

static int query_value(const char *query, const char *key, char *out, size_t cap) {
    size_t nlen = strlen(key);
    const char *p = query ? query : "";
    out[0] = 0;
    while (*p) {
        if (strncmp(p, key, nlen) == 0 && p[nlen] == '=') {
            size_t n;
            p += nlen + 1;
            n = strcspn(p, "&");
            if (n >= cap) {
                n = cap - 1;
            }
            memcpy(out, p, n);
            out[n] = 0;
            return 0;
        }
        p = strchr(p, '&');
        if (!p) {
            break;
        }
        p++;
    }
    return -1;
}

static int wget_run(const char *cmd, char *out, size_t cap) {
    FILE *fp;
    size_t n = 0;
    int ch;
    fp = popen(cmd, "r");
    if (!fp) {
        return -1;
    }
    while (n + 1 < cap && (ch = fgetc(fp)) != EOF) {
        out[n++] = (char)ch;
    }
    out[n] = 0;
    if (pclose(fp) != 0 && n == 0) {
        return -1;
    }
    return 0;
}

static int wget_body(const char *url, const char *post, char *out, size_t cap) {
    char cmd[1600];
    char safe[1100];
    safe[0] = 0;
    if (post) {
        size_t o = 0;
        size_t i;
        for (i = 0; post[i] && o + 1 < sizeof safe; i++) {
            if (post[i] == '\'' || post[i] == '\n' || post[i] == '\r' || post[i] == '\\') {
                continue;
            }
            safe[o++] = post[i];
        }
        safe[o] = 0;
        snprintf(cmd, sizeof cmd, "wget -q -T 12 -O - --post-data='%s' '%s'", safe, url);
    } else {
        snprintf(cmd, sizeof cmd, "wget -q -T 12 -O - '%s'", url);
    }
    return wget_run(cmd, out, cap);
}

static int wget_slow(const char *url, const char *post, char *out, size_t cap) {
    char cmd[1600];
    char safe[160];
    size_t o = 0;
    size_t i;
    safe[0] = 0;
    if (post) {
        for (i = 0; post[i] && o + 1 < sizeof safe; i++) {
            if (post[i] == '\'' || post[i] == '\n' || post[i] == '\r' || post[i] == '\\') {
                continue;
            }
            safe[o++] = post[i];
        }
        safe[o] = 0;
    }
    snprintf(cmd, sizeof cmd, "wget -q -T 90 -O - --post-data='%s' '%s'", safe, url);
    return wget_run(cmd, out, cap);
}

/* BusyBox wget умеет --post-data и не умеет тело из файла. Свой файл уходит сокетом. */
static int http_post(const char *ip, int port, const char *path, const void *data, size_t n, char *out, size_t cap) {
    int fd;
    struct sockaddr_in addr;
    struct timeval tv;
    char head[320];
    char buf[1024];
    int head_n;
    size_t got = 0;
    char *split;
    fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        return -1;
    }
    memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET;
    addr.sin_port = htons((unsigned short)port);
    if (inet_pton(AF_INET, ip, &addr.sin_addr) != 1) {
        close(fd);
        return -1;
    }
    tv.tv_sec = 90;
    tv.tv_usec = 0;
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    if (connect(fd, (struct sockaddr *)&addr, sizeof addr) != 0) {
        close(fd);
        return -1;
    }
    head_n = snprintf(head, sizeof head,
                      "POST %s HTTP/1.0\r\nHost: %s:%d\r\nContent-Type: application/octet-stream\r\n"
                      "Content-Length: %u\r\nConnection: close\r\n\r\n",
                      path, ip, port, (unsigned)n);
    if (head_n < 0 || (size_t)head_n >= sizeof head
        || write_all(fd, head, (size_t)head_n) != 0
        || write_all(fd, data, n) != 0) {
        close(fd);
        return -1;
    }
    while (got + 1 < sizeof buf) {
        ssize_t r = read(fd, buf + got, sizeof buf - 1 - got);
        if (r < 0) {
            if (errno == EINTR) {
                continue;
            }
            break;
        }
        if (r == 0) {
            break;
        }
        got += (size_t)r;
    }
    close(fd);
    buf[got] = 0;
    split = strstr(buf, "\r\n\r\n");
    if (!split) {
        return -1;
    }
    snprintf(out, cap, "%s", split + 4);
    return out[0] == '{' ? 0 : -1;
}

/* /api/train/... — на сервер слова (base+2) как есть. Ответ сервера уходит клиенту байт в байт:
   там и JSON, и WAV трудных случаев. Сервер пускает только эту колонку, ключ проверен здесь. */
static void proxy_train(int fd, const char *method, const char *rest, const char *query,
                        const char *body, size_t body_len) {
    struct yse_host conf;
    struct sockaddr_in addr;
    struct timeval tv;
    char head[900];
    char buf[4096];
    const char *p;
    int up;
    int n;
    if (strcmp(method, "GET") != 0 && strcmp(method, "POST") != 0) {
        send_json(fd, 405, "Method Not Allowed", "{\"error\":\"method\"}");
        return;
    }
    for (p = rest; *p; p++) {
        if (!((*p >= 'a' && *p <= 'z') || *p == '/')) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"path\"}");
            return;
        }
    }
    yse_host_load(&conf);
    up = socket(AF_INET, SOCK_STREAM, 0);
    if (up < 0) {
        send_json(fd, 502, "Bad Gateway", "{\"error\":\"сервер обучения не отвечает\"}");
        return;
    }
    memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET;
    addr.sin_port = htons((unsigned short)(conf.base + YSE_TRAIN));
    tv.tv_sec = 10;
    tv.tv_usec = 0;
    setsockopt(up, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
    setsockopt(up, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    if (inet_pton(AF_INET, conf.ip, &addr.sin_addr) != 1
        || connect(up, (struct sockaddr *)&addr, sizeof addr) != 0) {
        close(up);
        send_json(fd, 502, "Bad Gateway", "{\"error\":\"сервер обучения не отвечает\"}");
        return;
    }
    n = snprintf(head, sizeof head,
                 "%s /v1/train%s%s%s HTTP/1.0\r\nHost: %s:%d\r\nContent-Type: application/json\r\n"
                 "Content-Length: %u\r\nConnection: close\r\n\r\n",
                 method, rest, query ? "?" : "", query ? query : "", conf.ip, conf.base + YSE_TRAIN,
                 (unsigned)body_len);
    if (n < 0 || (size_t)n >= sizeof head || write_all(up, head, (size_t)n) != 0
        || (body_len && write_all(up, body, body_len) != 0)) {
        close(up);
        send_json(fd, 502, "Bad Gateway", "{\"error\":\"сервер обучения не отвечает\"}");
        return;
    }
    for (;;) {
        ssize_t r = read(up, buf, sizeof buf);
        if (r < 0 && errno == EINTR) {
            continue;
        }
        if (r <= 0 || write_all(fd, buf, (size_t)r) != 0) {
            break;
        }
    }
    close(up);
}

static int write_text_mode(const char *path, const char *text, mode_t mode) {
    if (!text || strchr(text, '\n') || strchr(text, '\r')) {
        return -1;
    }
    if (write_file(path, text, strlen(text)) != 0) {
        return -1;
    }
    return chmod(path, mode);
}

static void send_people(int fd) {
    char dir[640];
    char who[32];
    char json[4096];
    size_t used;
    DIR *dp;
    struct dirent *ent;
    int first = 1;
    int n;
    read_setting("music.who", who, sizeof who);
    if (!hex_id(who)) {
        who[0] = 0;
    }
    n = snprintf(json, sizeof json, "{\"who\":\"%s\",\"people\":[", who);
    if (n < 0) {
        send_json(fd, 500, "Internal Server Error", "{\"error\":\"people\"}");
        return;
    }
    used = (size_t)n;
    snprintf(dir, sizeof dir, "%s/people", root_dir);
    dp = opendir(dir);
    if (dp) {
        while ((ent = readdir(dp)) != NULL) {
            char name[80];
            char login[80];
            char path[768];
            char ename[160];
            char elogin[160];
            char piece[420];
            int piece_n;
            if (!hex_id(ent->d_name)) {
                continue;
            }
            snprintf(path, sizeof path, "%s/%s/name", dir, ent->d_name);
            if (read_line_file(path, name, sizeof name) != 0) {
                continue;
            }
            snprintf(path, sizeof path, "%s/%s/login", dir, ent->d_name);
            if (read_line_file(path, login, sizeof login) != 0) {
                login[0] = 0;
            }
            json_escape(name, ename, sizeof ename);
            json_escape(login, elogin, sizeof elogin);
            piece_n = snprintf(piece, sizeof piece, "%s{\"id\":\"%s\",\"name\":\"%s\",\"login\":\"%s\"}",
                               first ? "" : ",", ent->d_name, ename, elogin);
            if (piece_n < 0 || used + (size_t)piece_n + 3 >= sizeof json) {
                break;
            }
            memcpy(json + used, piece, (size_t)piece_n);
            used += (size_t)piece_n;
            first = 0;
        }
        closedir(dp);
    }
    if (used + 3 < sizeof json) {
        memcpy(json + used, "]}", 3);
    }
    send_json(fd, 200, "OK", json);
}

static void color_channels(const char *color, int *red, int *green, int *blue) {
    unsigned int r = 255;
    unsigned int g = 216;
    unsigned int b = 168;
    if (sscanf(color, "%2x%2x%2x", &r, &g, &b) != 3) {
        r = 255;
        g = 216;
        b = 168;
    }
    *red = (int)r;
    *green = (int)g;
    *blue = (int)b;
}

static void write_lamp_json(char *out, size_t cap) {
    char on[8];
    char color[16];
    int bright = 180;
    int red;
    int green;
    int blue;
    read_lamp(on, sizeof on, color, sizeof color, &bright);
    color_channels(color, &red, &green, &blue);
    snprintf(out, cap,
             "{\"on\":%s,\"color\":\"%s\",\"r\":%d,\"g\":%d,\"b\":%d,\"brightness\":%d}",
             on[0] == '1' ? "true" : "false", color, red, green, blue, bright);
}

static void read_heard_text(char *text, size_t cap) {
    int file;
    ssize_t n;
    text[0] = 0;
    if (cap < 2) {
        return;
    }
    file = open("/data/opt/yandex-station/heard.txt", O_RDONLY);
    if (file < 0) {
        return;
    }
    n = read(file, text, cap - 1);
    close(file);
    if (n < 0) {
        n = 0;
    }
    text[n] = 0;
    while (n > 0 && (text[n - 1] == '\n' || text[n - 1] == '\r')) {
        text[--n] = 0;
    }
}

static void send_ha_voice(int fd) {
    char nn[240];
    char voice[80];
    char wake[80];
    char bt[8];
    char sound[8];
    char heard[240];
    char enn[520];
    char evoice[200];
    char ewake[200];
    char eheard[520];
    char persona[PERSONA_CAP];
    char epersona[PERSONA_JSON];
    char brain[2400];
    char json[32768];
    char eq[64];
    read_setting("nn.txt", nn, sizeof nn);
    read_setting("voice.txt", voice, sizeof voice);
    read_setting("wake.txt", wake, sizeof wake);
    read_setting("bt.txt", bt, sizeof bt);
    read_setting("wake.sound", sound, sizeof sound);
    read_setting("persona.txt", persona, sizeof persona);
    read_eq(eq, sizeof eq);
    read_heard_text(heard, sizeof heard);
    brain_json(brain, sizeof brain);
    json_escape(nn, enn, sizeof enn);
    json_escape(voice, evoice, sizeof evoice);
    json_escape(wake, ewake, sizeof ewake);
    json_escape(heard, eheard, sizeof eheard);
    json_escape(persona, epersona, sizeof epersona);
    snprintf(json, sizeof json,
             "{\"volume\":%d,\"wake_sound\":%s,\"wake\":\"%s\",\"heard\":\"%s\","
             "\"nn\":\"%s\",\"nn_ready\":true,\"voice\":\"%s\",\"bluetooth\":%s,\"persona\":\"%s\",\"eq\":\"%s\",%s}",
             percent_from_mixer(master_volume()),
             sound[0] == '0' ? "false" : "true",
             ewake, eheard, enn, evoice, bt[0] == '1' ? "true" : "false", epersona, eq, brain);
    send_json(fd, 200, "OK", json);
}

static void send_ha_device(int fd) {
    char *json;
    char *music;
    char alarms[ALARMS_MAX * 512 + 64];
    char lamp[256];
    char ring[8];
    char color[16];
    char bright_text[8];
    char effect[16];
    char now[40];
    char enow[120];
    char ha[240];
    char board[320];
    const char *alarm_list = "[]";
    int bright_n = 160;
    int wrote;
    json = malloc(32768);
    music = malloc(8192);
    if (!json || !music) {
        free(json);
        free(music);
        send_json(fd, 500, "Error", "{\"error\":\"ha\"}");
        return;
    }
    if (wget_body(yse_url(murl, sizeof murl, YSE_MUSIC, "/v1/now"), NULL, music, 8192) != 0 || music[0] != '{') {
        snprintf(music, 8192, "null");
    }
    if (format_alarms(alarms, sizeof alarms) == 0) {
        char *bracket = strchr(alarms, '[');
        char *end = strrchr(alarms, '}');
        if (bracket && end && end > bracket) {
            *end = 0;
            alarm_list = bracket;
        }
    }
    read_setting("ring.txt", ring, sizeof ring);
    read_setting("ring.color", color, sizeof color);
    read_setting("ring.bright", bright_text, sizeof bright_text);
    read_setting("ring.effect", effect, sizeof effect);
    if (ring[0] == '0') {
        snprintf(ring, sizeof ring, "0");
    } else {
        snprintf(ring, sizeof ring, "1");
    }
    if (!hex_color(color)) {
        snprintf(color, sizeof color, "3aa0ff");
    }
    if (decimal_range(bright_text, &bright_n, 255) != 0) {
        bright_n = 160;
    }
    if (!known_effect(effect)) {
        snprintf(effect, sizeof effect, "wave");
    }
    write_lamp_json(lamp, sizeof lamp);
    current_ssid(now, sizeof now);
    json_escape(now, enow, sizeof enow);
    read_setting("ha.url", ha, sizeof ha);
    board_json(board, sizeof board);
    wrote = snprintf(json, 32768,
                     "{\"ring\":{\"on\":%s,\"color\":\"%s\",\"bright\":%d,\"effect\":\"%s\"},"
                     "\"lamp\":%s,\"music\":%s,\"alarms\":%s,\"wifi_now\":\"%s\","
                     "\"ha_url_set\":%s,\"ha_token_set\":%s,%s}",
                     ring[0] == '1' ? "true" : "false", color, bright_n, effect,
                     lamp, music, alarm_list, enow,
                     ha[0] ? "true" : "false",
                     setting_set("ha.token") ? "true" : "false", board);
    if (wrote < 0 || wrote >= 32768) {
        free(json);
        free(music);
        send_json(fd, 500, "Error", "{\"error\":\"ha\"}");
        return;
    }
    send_json(fd, 200, "OK", json);
    free(json);
    free(music);
}

static int store_ring_fields(const char *body, int *flash, int *effect_flash) {
    char ring[8];
    char color[16];
    char bright[8];
    char effect[16];
    char previous[16];
    int present = 0;
    int number = 0;
    size_t i;
    *flash = 0;
    *effect_flash = 0;
    if (json_scalar(body, "ring", ring, sizeof ring, &present) != 0) {
        return -1;
    }
    if (present) {
        int on = 0;
        if (flag_text(ring, &on) != 0) {
            return -1;
        }
        if (write_setting("ring.txt", on ? "1" : "0", 0644) != 0) {
            return -2;
        }
    }
    if (json_scalar(body, "color", color, sizeof color, &present) != 0) {
        return -1;
    }
    if (!present && json_scalar(body, "ring_color", color, sizeof color, &present) != 0) {
        return -1;
    }
    if (present) {
        if (!hex_color(color)) {
            return -1;
        }
        for (i = 0; color[i]; i++) {
            if (color[i] >= 'A' && color[i] <= 'F') {
                color[i] = (char)(color[i] - 'A' + 'a');
            }
        }
        read_setting("ring.color", previous, sizeof previous);
        if (strcmp(previous, color) != 0) {
            *flash = 1;
        }
        if (write_setting("ring.color", color, 0644) != 0) {
            return -2;
        }
    }
    if (json_scalar(body, "bright", bright, sizeof bright, &present) != 0) {
        return -1;
    }
    if (!present && json_scalar(body, "ring_bright", bright, sizeof bright, &present) != 0) {
        return -1;
    }
    if (present) {
        if (decimal_range(bright, &number, 255) != 0) {
            return -1;
        }
        read_setting("ring.bright", previous, sizeof previous);
        if (strcmp(previous, bright) != 0) {
            *flash = 1;
        }
        if (write_setting("ring.bright", bright, 0644) != 0) {
            return -2;
        }
    }
    if (json_scalar(body, "effect", effect, sizeof effect, &present) != 0) {
        return -1;
    }
    if (!present && json_scalar(body, "ring_effect", effect, sizeof effect, &present) != 0) {
        return -1;
    }
    if (present) {
        if (!known_effect(effect)) {
            return -1;
        }
        read_setting("ring.effect", previous, sizeof previous);
        if (strcmp(previous, effect) != 0) {
            *flash = 1;
            *effect_flash = 1;
        }
        if (write_setting("ring.effect", effect, 0644) != 0) {
            return -2;
        }
    }
    return 0;
}

static int store_lamp_fields(const char *body) {
    char on_text[8];
    char color[16];
    char bright_text[8];
    char red_text[8];
    char green_text[8];
    char blue_text[8];
    char previous[16];
    int present = 0;
    int on = 0;
    int number = 0;
    int red = 0;
    int green = 0;
    int blue = 0;
    int red_set = 0;
    int green_set = 0;
    int blue_set = 0;
    int touch = 0;
    size_t i;
    if (json_scalar(body, "on", on_text, sizeof on_text, &present) != 0) {
        return -1;
    }
    if (!present && json_scalar(body, "lamp", on_text, sizeof on_text, &present) != 0) {
        return -1;
    }
    if (present) {
        if (flag_text(on_text, &on) != 0) {
            return -1;
        }
        read_setting("lamp.txt", previous, sizeof previous);
        if (previous[0] != (on ? '1' : '0')) {
            touch = 1;
        }
        if (write_setting("lamp.txt", on ? "1" : "0", 0644) != 0) {
            return -2;
        }
    }
    if (json_scalar(body, "r", red_text, sizeof red_text, &red_set) != 0
        || json_scalar(body, "g", green_text, sizeof green_text, &green_set) != 0
        || json_scalar(body, "b", blue_text, sizeof blue_text, &blue_set) != 0) {
        return -1;
    }
    if (red_set && green_set && blue_set) {
        if (decimal_range(red_text, &red, 255) != 0 || decimal_range(green_text, &green, 255) != 0
            || decimal_range(blue_text, &blue, 255) != 0) {
            return -1;
        }
        snprintf(color, sizeof color, "%02x%02x%02x", red, green, blue);
        present = 1;
    } else if (json_scalar(body, "color", color, sizeof color, &present) != 0) {
        return -1;
    } else if (!present && json_scalar(body, "lamp_color", color, sizeof color, &present) != 0) {
        return -1;
    }
    if (present) {
        if (!hex_color(color)) {
            return -1;
        }
        for (i = 0; color[i]; i++) {
            if (color[i] >= 'A' && color[i] <= 'F') {
                color[i] = (char)(color[i] - 'A' + 'a');
            }
        }
        read_setting("lamp.color", previous, sizeof previous);
        if (strcmp(previous, color) != 0) {
            touch = 1;
        }
        if (write_setting("lamp.color", color, 0644) != 0) {
            return -2;
        }
    }
    if (json_scalar(body, "brightness", bright_text, sizeof bright_text, &present) != 0) {
        return -1;
    }
    if (!present && json_scalar(body, "lamp_bright", bright_text, sizeof bright_text, &present) != 0) {
        return -1;
    }
    if (present) {
        if (decimal_range(bright_text, &number, 255) != 0) {
            return -1;
        }
        read_setting("lamp.bright", previous, sizeof previous);
        if (strcmp(previous, bright_text) != 0) {
            touch = 1;
        }
        if (write_setting("lamp.bright", bright_text, 0644) != 0) {
            return -2;
        }
    }
    if (touch) {
        apply_lamp();
    }
    return 0;
}

static void handle(int fd, char *req, size_t head_len, const char *body, size_t body_len) {
    char *line_end;
    char method[16];
    char url[512];
    char path_buf[512];
    char *q;
    char *seg[8];
    int nseg = 0;
    char *sp;
    char token_hdr[128];
    char filename[192];
    (void)head_len;
    if (!body) {
        body = "";
    }
    line_end = strstr(req, "\r\n");
    if (!line_end) {
        send_json(fd, 400, "Bad Request", "{\"error\":\"request\"}");
        return;
    }
    *line_end = 0;
    if (sscanf(req, "%15s %511s", method, url) != 2) {
        send_json(fd, 400, "Bad Request", "{\"error\":\"request\"}");
        return;
    }
    snprintf(path_buf, sizeof path_buf, "%s", url);
    q = strchr(path_buf, '?');
    if (q) {
        *q = 0;
        q++;
    }
    token_hdr[0] = 0;
    filename[0] = 0;
    header_value(line_end + 2, "X-Token", token_hdr, sizeof token_hdr);
    header_value(line_end + 2, "X-Filename", filename, sizeof filename);
    url_decode(filename);

    if (strcmp(method, "GET") == 0 && strcmp(path_buf, "/") == 0) {
        char page[640];
        snprintf(page, sizeof page, "%s/www/index.html", root_dir);
        send_file(fd, page, "text/html; charset=utf-8");
        return;
    }
    if (strncmp(path_buf, "/api/", 5) != 0) {
        send_json(fd, 404, "Not Found", "{\"error\":\"not-found\"}");
        return;
    }
    if (strcmp(method, "GET") == 0 && strcmp(path_buf, "/api/models") == 0) {
        char *list = NULL;
        if (list_models(&list) != 0 || !list) {
            send_json(fd, 500, "Error", "{\"error\":\"list\"}");
            return;
        }
        send_json(fd, 200, "OK", list);
        free(list);
        return;
    }
    if (strcmp(method, "GET") == 0 && strcmp(path_buf, "/api/clock") == 0) {
        static const char *months[] = {
            "января", "февраля", "марта", "апреля", "мая", "июня",
            "июля", "августа", "сентября", "октября", "ноября", "декабря"
        };
        time_t local = time(NULL) + 3 * 3600;
        struct tm parts;
        char json[160];
        gmtime_r(&local, &parts);
        if (parts.tm_mon < 0 || parts.tm_mon > 11) {
            parts.tm_mon = 0;
        }
        snprintf(json, sizeof json, "{\"text\":\"%d %s, %02d:%02d:%02d\"}",
                 parts.tm_mday, months[parts.tm_mon], parts.tm_hour, parts.tm_min, parts.tm_sec);
        send_json(fd, 200, "OK", json);
        return;
    }
    if (strcmp(method, "POST") == 0 && strcmp(path_buf, "/api/panel-claim") == 0) {
        char path[640];
        char json[160];
        size_t i;
        struct stat st;
        snprintf(path, sizeof path, "%s/settings/panel.claim", root_dir);
        if (stat(path, &st) != 0) {
            send_json(fd, 404, "Not Found", "{\"error\":\"claim\"}");
            return;
        }
        /* «Открой панель» голосом кладёт файл на 2 минуты; старше — уже не выдаём. */
        if (time(NULL) - st.st_mtime > 120) {
            unlink(path);
            send_json(fd, 404, "Not Found", "{\"error\":\"claim\"}");
            return;
        }
        load_token();
        for (i = 0; token[i]; i++) {
            char c = token[i];
            if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F'))) {
                send_json(fd, 500, "Error", "{\"error\":\"token\"}");
                return;
            }
        }
        if (i < 8 || i > 64) {
            send_json(fd, 500, "Error", "{\"error\":\"token\"}");
            return;
        }
        unlink(path);
        snprintf(json, sizeof json, "{\"token\":\"%s\"}", token);
        send_json(fd, 200, "OK", json);
        return;
    }
    if (!token_ok(token_hdr)) {
        send_json(fd, 401, "Unauthorized", "{\"error\":\"token\"}");
        return;
    }
    if (strncmp(path_buf, "/api/train/", 11) == 0) {
        proxy_train(fd, method, path_buf + 10, q, body, body_len);
        return;
    }

    if (strcmp(method, "POST") == 0 && strcmp(path_buf, "/api/ask") == 0) {
        char raw[128];
        char turl[200];
        if (!body || !body[0] || strlen(body) > 400) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"text\"}");
            return;
        }
        if (wget_body(yse_url(turl, sizeof turl, YSE_TALK, "/v1/ask"), body, raw, sizeof raw) != 0 || raw[0] != '{') {
            send_json(fd, 502, "Bad Gateway", "{\"error\":\"talk\"}");
            return;
        }
        send_json(fd, 200, "OK", raw);
        return;
    }
    if (strcmp(method, "POST") == 0 && strcmp(path_buf, "/api/speak") == 0) {
        char raw[128];
        char turl[200];
        if (!body || !body[0] || strlen(body) > 800) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"text\"}");
            return;
        }
        if (wget_body(yse_url(turl, sizeof turl, YSE_TALK, "/v1/speak"), body, raw, sizeof raw) != 0 || raw[0] != '{') {
            send_json(fd, 502, "Bad Gateway", "{\"error\":\"talk\"}");
            return;
        }
        send_json(fd, 200, "OK", raw);
        return;
    }
    if (strcmp(method, "GET") == 0 && strcmp(path_buf, "/api/people") == 0) {
        send_people(fd);
        return;
    }
    if (strcmp(path_buf, "/api/voices") == 0 && (strcmp(method, "GET") == 0 || strcmp(method, "POST") == 0)) {
        char raw[8192];
        char turl[200];
        int post = strcmp(method, "POST") == 0;
        if (post && (!body || !body[0] || strlen(body) > 230)) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"body\"}");
            return;
        }
        if (wget_body(yse_url(turl, sizeof turl, YSE_TALK, "/v1/voices"), post ? body : NULL, raw, sizeof raw) != 0
            || raw[0] != '{') {
            send_json(fd, 502, "Bad Gateway", "{\"error\":\"talk\"}");
            return;
        }
        send_json(fd, 200, "OK", raw);
        return;
    }
    if (strcmp(path_buf, "/api/message") == 0 && (strcmp(method, "GET") == 0 || strcmp(method, "POST") == 0)) {
        char raw[4096];
        char turl[200];
        int post = strcmp(method, "POST") == 0;
        if (post && (!body || !body[0] || strlen(body) > 1000)) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"body\"}");
            return;
        }
        if (wget_body(yse_url(turl, sizeof turl, YSE_TALK, "/v1/message"), post ? body : NULL, raw, sizeof raw) != 0
            || raw[0] != '{') {
            send_json(fd, 502, "Bad Gateway", "{\"error\":\"talk\"}");
            return;
        }
        send_json(fd, 200, "OK", raw);
        return;
    }
    if (strcmp(method, "POST") == 0 && strcmp(path_buf, "/api/music/login") == 0) {
        char name[80];
        char raw[512];
        char id[16];
        char code[32];
        char url[160];
        char pending[768];
        char json[400];
        char ename[160];
        char eurl[240];
        int present = 0;
        if (json_string_field(body, "name", name, sizeof name, &present) != 0 || !present || !name[0]) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"name\"}");
            return;
        }
        if (wget_body(yse_url(murl, sizeof murl, YSE_MUSIC, "/v1/login/start"), name, raw, sizeof raw) != 0) {
            send_json(fd, 502, "Bad Gateway", "{\"error\":\"music\"}");
            return;
        }
        if (json_string_field(raw, "id", id, sizeof id, &present) != 0 || !present || !hex_id(id)) {
            send_json(fd, 502, "Bad Gateway", "{\"error\":\"music\"}");
            return;
        }
        json_string_field(raw, "user_code", code, sizeof code, &present);
        json_string_field(raw, "verification_url", url, sizeof url, &present);
        snprintf(pending, sizeof pending, "%s/people", root_dir);
        mkdir_p(pending);
        snprintf(pending, sizeof pending, "%s/people/pending-%s", root_dir, id);
        if (write_text_mode(pending, name, 0600) != 0) {
            send_json(fd, 500, "Internal Server Error", "{\"error\":\"name\"}");
            return;
        }
        json_escape(code, ename, sizeof ename);
        json_escape(url, eurl, sizeof eurl);
        snprintf(json, sizeof json, "{\"id\":\"%s\",\"user_code\":\"%s\",\"verification_url\":\"%s\"}", id, ename, eurl);
        send_json(fd, 200, "OK", json);
        return;
    }
    if (strcmp(method, "GET") == 0 && strcmp(path_buf, "/api/music/poll") == 0) {
        char id[16];
        char raw[1600];
        char token[1200];
        char login[80];
        char name[80];
        char ready[16];
        char err[80];
        char dir[768];
        char path[800];
        char url[160];
        char json[240];
        char elogin[160];
        char eerr[160];
        int present = 0;
        query_value(q, "id", id, sizeof id);
        if (!hex_id(id)) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"id\"}");
            return;
        }
        { char murl[200]; snprintf(url, sizeof url, "%s%s", yse_url(murl, sizeof murl, YSE_MUSIC, "/v1/login/poll?id="), id); }
        if (wget_body(url, NULL, raw, sizeof raw) != 0) {
            send_json(fd, 502, "Bad Gateway", "{\"error\":\"music\"}");
            return;
        }
        json_string_field(raw, "ready", ready, sizeof ready, &present);
        if (!present || strcmp(ready, "yes") != 0) {
            json_string_field(raw, "error", err, sizeof err, &present);
            json_escape(err, eerr, sizeof eerr);
            snprintf(json, sizeof json, "{\"ready\":false,\"error\":\"%s\"}", eerr);
            send_json(fd, 200, "OK", json);
            return;
        }
        if (json_string_field(raw, "token", token, sizeof token, &present) != 0 || !present || !token[0]) {
            send_json(fd, 502, "Bad Gateway", "{\"error\":\"music\"}");
            return;
        }
        json_string_field(raw, "login", login, sizeof login, &present);
        snprintf(path, sizeof path, "%s/people/pending-%s", root_dir, id);
        if (read_line_file(path, name, sizeof name) != 0 || !name[0]) {
            send_json(fd, 500, "Internal Server Error", "{\"error\":\"name\"}");
            return;
        }
        snprintf(dir, sizeof dir, "%s/people/%s", root_dir, id);
        if (mkdir_p(dir) != 0) {
            send_json(fd, 500, "Internal Server Error", "{\"error\":\"people\"}");
            return;
        }
        snprintf(path, sizeof path, "%s/name", dir);
        if (write_text_mode(path, name, 0644) != 0) {
            send_json(fd, 500, "Internal Server Error", "{\"error\":\"name\"}");
            return;
        }
        snprintf(path, sizeof path, "%s/login", dir);
        write_text_mode(path, login, 0644);
        snprintf(path, sizeof path, "%s/token", dir);
        if (write_text_mode(path, token, 0600) != 0) {
            send_json(fd, 500, "Internal Server Error", "{\"error\":\"token\"}");
            return;
        }
        write_setting("music.who", id, 0600);
        snprintf(path, sizeof path, "%s/people/pending-%s", root_dir, id);
        unlink(path);
        json_escape(login, elogin, sizeof elogin);
        snprintf(json, sizeof json, "{\"ready\":true,\"login\":\"%s\"}", elogin);
        send_json(fd, 200, "OK", json);
        return;
    }
    if (strcmp(method, "GET") == 0 && strcmp(path_buf, "/api/playlists") == 0) {
        char id[16];
        char raw[8192];
        char url[160];
        query_value(q, "id", id, sizeof id);
        if (!hex_id(id)) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"id\"}");
            return;
        }
        { char murl[200]; snprintf(url, sizeof url, "%s%s", yse_url(murl, sizeof murl, YSE_MUSIC, "/v1/playlists?id="), id); }
        if (wget_body(url, NULL, raw, sizeof raw) != 0) {
            send_json(fd, 502, "Bad Gateway", "{\"error\":\"music\"}");
            return;
        }
        send_json(fd, 200, "OK", raw);
        return;
    }
    if (strcmp(method, "POST") == 0 && strcmp(path_buf, "/api/music/who") == 0) {
        char id[16];
        char raw[128];
        int present = 0;
        if (json_string_field(body, "id", id, sizeof id, &present) != 0 || !present || !hex_id(id)) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"id\"}");
            return;
        }
        if (wget_body(yse_url(murl, sizeof murl, YSE_MUSIC, "/v1/who-id"), id, raw, sizeof raw) != 0) {
            send_json(fd, 502, "Bad Gateway", "{\"error\":\"music\"}");
            return;
        }
        write_setting("music.who", id, 0600);
        send_json(fd, 200, "OK", raw);
        return;
    }
    if (strcmp(method, "POST") == 0 && strcmp(path_buf, "/api/music/play") == 0) {
        char id[16];
        char kind[16];
        char body_line[40];
        char raw[256];
        int present = 0;
        if (json_string_field(body, "id", id, sizeof id, &present) != 0 || !present || !hex_id(id)) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"id\"}");
            return;
        }
        if (json_string_field(body, "kind", kind, sizeof kind, &present) != 0 || !present || !music_kind(kind)) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"kind\"}");
            return;
        }
        snprintf(body_line, sizeof body_line, "%s %s", id, kind);
        if (wget_body(yse_url(murl, sizeof murl, YSE_MUSIC, "/v1/play"), body_line, raw, sizeof raw) != 0) {
            send_json(fd, 502, "Bad Gateway", "{\"error\":\"music\"}");
            return;
        }
        send_json(fd, 200, "OK", raw);
        return;
    }
    if (strcmp(method, "POST") == 0 && strcmp(path_buf, "/api/music/ctrl") == 0) {
        char cmd[16];
        char raw[128];
        int present = 0;
        if (json_string_field(body, "cmd", cmd, sizeof cmd, &present) != 0 || !present) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"cmd\"}");
            return;
        }
        if (strcmp(cmd, "pause") != 0 && strcmp(cmd, "play") != 0 && strcmp(cmd, "next") != 0 &&
            strcmp(cmd, "prev") != 0 && strcmp(cmd, "stop") != 0) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"cmd\"}");
            return;
        }
        if (wget_body(yse_url(murl, sizeof murl, YSE_MUSIC, "/v1/ctrl"), cmd, raw, sizeof raw) != 0) {
            send_json(fd, 502, "Bad Gateway", "{\"error\":\"music\"}");
            return;
        }
        send_json(fd, 200, "OK", raw);
        return;
    }
    if (strcmp(method, "POST") == 0 && strcmp(path_buf, "/api/music/mark") == 0) {
        char cmd[16];
        char raw[128];
        int present = 0;
        if (json_string_field(body, "cmd", cmd, sizeof cmd, &present) != 0 || !present) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"cmd\"}");
            return;
        }
        if (strcmp(cmd, "like") != 0 && strcmp(cmd, "dislike") != 0) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"cmd\"}");
            return;
        }
        if (wget_body(yse_url(murl, sizeof murl, YSE_MUSIC, "/v1/mark"), cmd, raw, sizeof raw) != 0) {
            send_json(fd, 502, "Bad Gateway", "{\"error\":\"music\"}");
            return;
        }
        send_json(fd, 200, "OK", raw);
        return;
    }
    if (strcmp(method, "GET") == 0 && strcmp(path_buf, "/api/music/now") == 0) {
        char raw[8192];
        if (wget_body(yse_url(murl, sizeof murl, YSE_MUSIC, "/v1/now"), NULL, raw, sizeof raw) != 0) {
            send_json(fd, 502, "Bad Gateway", "{\"error\":\"music\"}");
            return;
        }
        send_json(fd, 200, "OK", raw);
        return;
    }

    if (strcmp(method, "GET") == 0 && strcmp(path_buf, "/api/ha/voice") == 0) {
        send_ha_voice(fd);
        return;
    }
    if (strcmp(method, "POST") == 0 && strcmp(path_buf, "/api/ha/voice") == 0) {
        char volume[8];
        char sound[8];
        char wake[41];
        char nn[201];
        char voice[41];
        char persona[PERSONA_CAP];
        char bt_now[8];
        int present = 0;
        int number = 0;
        int flag = 0;
        const char *payload = body ? body : "";
        if (json_scalar(payload, "volume", volume, sizeof volume, &present) != 0) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"volume\"}");
            return;
        }
        if (present && (decimal_range(volume, &number, 100) != 0 || set_master_volume(number) != 0)) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"volume\"}");
            return;
        }
        if (json_scalar(payload, "wake_sound", sound, sizeof sound, &present) != 0 || (present && flag_text(sound, &flag) != 0)) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"sound\"}");
            return;
        }
        if (present && write_setting("wake.sound", flag ? "1" : "0", 0644) != 0) {
            send_json(fd, 500, "Error", "{\"error\":\"write\"}");
            return;
        }
        if (json_scalar(payload, "wake", wake, sizeof wake, &present) != 0) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"wake\"}");
            return;
        }
        if (present && wake[0] && write_setting("wake.txt", wake, 0644) != 0) {
            send_json(fd, 500, "Error", "{\"error\":\"write\"}");
            return;
        }
        if (json_scalar(payload, "nn", nn, sizeof nn, &present) != 0) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"nn\"}");
            return;
        }
        if (present && write_setting("nn.txt", nn, 0644) != 0) {
            send_json(fd, 500, "Error", "{\"error\":\"write\"}");
            return;
        }
        if (json_scalar(payload, "voice", voice, sizeof voice, &present) != 0) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"voice\"}");
            return;
        }
        if (present && write_setting("voice.txt", voice, 0644) != 0) {
            send_json(fd, 500, "Error", "{\"error\":\"write\"}");
            return;
        }
        if (json_string_field(payload, "persona", persona, sizeof persona, &present) != 0) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"persona\"}");
            return;
        }
        if (present && write_setting("persona.txt", persona, 0644) != 0) {
            send_json(fd, 500, "Error", "{\"error\":\"write\"}");
            return;
        }
        if (save_brain_fields(fd, payload) != 0 || save_eq_field(fd, payload) != 0) {
            return;
        }
        if (json_scalar(payload, "bluetooth", bt_now, sizeof bt_now, &present) != 0 || (present && flag_text(bt_now, &flag) != 0)) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"bt\"}");
            return;
        }
        if (present) {
            char previous[8];
            int was;
            read_setting("bt.txt", previous, sizeof previous);
            was = previous[0] == '1';
            if (flag != was) {
                if (write_setting("bt.txt", flag ? "1" : "0", 0644) != 0) {
                    send_json(fd, 500, "Error", "{\"error\":\"write\"}");
                    return;
                }
                if (apply_bluetooth() != 0) {
                    write_setting("bt.txt", was ? "1" : "0", 0644);
                    apply_bluetooth();
                    send_json(fd, 500, "Error", "{\"error\":\"bt\"}");
                    return;
                }
            }
        }
        send_ha_voice(fd);
        return;
    }
    if (strcmp(method, "GET") == 0 && strcmp(path_buf, "/api/ha/light") == 0) {
        char json[256];
        write_lamp_json(json, sizeof json);
        send_json(fd, 200, "OK", json);
        return;
    }
    if (strcmp(method, "POST") == 0 && strcmp(path_buf, "/api/ha/light") == 0) {
        char json[256];
        int rc = store_lamp_fields(body ? body : "");
        if (rc == -1) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"lamp\"}");
            return;
        }
        if (rc == -2) {
            send_json(fd, 500, "Error", "{\"error\":\"write\"}");
            return;
        }
        write_lamp_json(json, sizeof json);
        send_json(fd, 200, "OK", json);
        return;
    }
    if (strcmp(method, "GET") == 0 && strcmp(path_buf, "/api/ha/device") == 0) {
        send_ha_device(fd);
        return;
    }
    if (strcmp(method, "POST") == 0 && strcmp(path_buf, "/api/ha/device") == 0) {
        int flash = 0;
        int effect_flash = 0;
        int rc = store_ring_fields(body ? body : "", &flash, &effect_flash);
        if (rc == -1) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"ring\"}");
            return;
        }
        if (rc == -2) {
            send_json(fd, 500, "Error", "{\"error\":\"write\"}");
            return;
        }
        if (effect_flash) {
            flash_ring_ms(5000);
        } else if (flash) {
            flash_ring();
        }
        send_ha_device(fd);
        return;
    }

    if (strcmp(method, "GET") == 0 && strcmp(path_buf, "/api/settings") == 0) {
        send_settings(fd);
        return;
    }

    if (strcmp(method, "POST") == 0 && strcmp(path_buf, "/api/settings") == 0) {
        char nn[201];
        char voice[41];
        char persona[PERSONA_CAP];
        char wake[41];
        char login[81];
        char password[129];
        char ha_url[201];
        char ha_token[257];
        char wifi_ssid[33];
        char wifi_psk[64];
        int present = 0;
        if (!body) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"body\"}");
            return;
        }
        if (json_string_field(body, "nn", nn, sizeof nn, &present) != 0) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"nn\"}");
            return;
        }
        if (present && write_setting("nn.txt", nn, 0644) != 0) {
            send_json(fd, 500, "Error", "{\"error\":\"write\"}");
            return;
        }
        if (json_string_field(body, "voice", voice, sizeof voice, &present) != 0) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"voice\"}");
            return;
        }
        if (present && write_setting("voice.txt", voice, 0644) != 0) {
            send_json(fd, 500, "Error", "{\"error\":\"write\"}");
            return;
        }
        if (json_string_field(body, "persona", persona, sizeof persona, &present) != 0) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"persona\"}");
            return;
        }
        if (present && write_setting("persona.txt", persona, 0644) != 0) {
            send_json(fd, 500, "Error", "{\"error\":\"write\"}");
            return;
        }
        if (save_brain_fields(fd, body) != 0 || save_eq_field(fd, body) != 0) {
            return;
        }
        if (json_string_field(body, "wake", wake, sizeof wake, &present) != 0) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"wake\"}");
            return;
        }
        if (present && wake[0] && write_setting("wake.txt", wake, 0644) != 0) {
            send_json(fd, 500, "Error", "{\"error\":\"write\"}");
            return;
        }
        if (json_string_field(body, "yandex_login", login, sizeof login, &present) != 0) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"login\"}");
            return;
        }
        if (present) {
            if (write_setting("yandex.login", login, 0600) != 0) {
                send_json(fd, 500, "Error", "{\"error\":\"write\"}");
                return;
            }
            if (login[0] == 0) {
                char secret[768];
                if (setting_path(secret, sizeof secret, "yandex.secret")) {
                    unlink(secret);
                }
            }
        }
        if (json_string_field(body, "yandex_password", password, sizeof password, &present) != 0) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"password\"}");
            return;
        }
        if (present && password[0] && write_setting("yandex.secret", password, 0600) != 0) {
            send_json(fd, 500, "Error", "{\"error\":\"write\"}");
            return;
        }
        if (json_string_field(body, "ha_url", ha_url, sizeof ha_url, &present) != 0) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"ha\"}");
            return;
        }
        if (present && write_setting("ha.url", ha_url, 0644) != 0) {
            send_json(fd, 500, "Error", "{\"error\":\"write\"}");
            return;
        }
        if (json_string_field(body, "ha_token", ha_token, sizeof ha_token, &present) != 0) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"ha\"}");
            return;
        }
        if (present && ha_token[0] && write_setting("ha.token", ha_token, 0600) != 0) {
            send_json(fd, 500, "Error", "{\"error\":\"write\"}");
            return;
        }
        if (json_string_field(body, "wifi_ssid", wifi_ssid, sizeof wifi_ssid, &present) != 0) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"ssid\"}");
            return;
        }
        if (present && write_setting("wifi.ssid", wifi_ssid, 0600) != 0) {
            send_json(fd, 500, "Error", "{\"error\":\"write\"}");
            return;
        }
        if (json_string_field(body, "wifi_psk", wifi_psk, sizeof wifi_psk, &present) != 0) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"psk\"}");
            return;
        }
        if (present && wifi_psk[0] && write_setting("wifi.psk", wifi_psk, 0600) != 0) {
            send_json(fd, 500, "Error", "{\"error\":\"write\"}");
            return;
        }
        {
            char bt_now[8];
            char bt_prev[8];
            int bt_present = 0;
            int now;
            int was;
            if (json_string_field(body, "bt", bt_now, sizeof bt_now, &bt_present) != 0) {
                send_json(fd, 400, "Bad Request", "{\"error\":\"bt\"}");
                return;
            }
            if (bt_present) {
                now = bt_now[0] == '1';
                read_setting("bt.txt", bt_prev, sizeof bt_prev);
                was = bt_prev[0] == '1';
                if (now != was) {
                    if (write_setting("bt.txt", now ? "1" : "0", 0644) != 0) {
                        send_json(fd, 500, "Error", "{\"error\":\"write\"}");
                        return;
                    }
                    if (apply_bluetooth() != 0) {
                        write_setting("bt.txt", was ? "1" : "0", 0644);
                        apply_bluetooth();
                        send_json(fd, 500, "Error", "{\"error\":\"bt\"}");
                        return;
                    }
                }
            }
        }
        {
            char ring[8];
            char color[16];
            char bright[8];
            char sound[8];
            char effect[16];
            char previous[16];
            char volume[8];
            int number = 0;
            int flash = 0;
            int effect_flash = 0;
            int lamp_touch = 0;
            size_t i;
            if (json_string_field(body, "ring", ring, sizeof ring, &present) != 0) {
                send_json(fd, 400, "Bad Request", "{\"error\":\"ring\"}");
                return;
            }
            if (present) {
                if ((ring[0] != '0' && ring[0] != '1') || ring[1] != 0) {
                    send_json(fd, 400, "Bad Request", "{\"error\":\"ring\"}");
                    return;
                }
                if (write_setting("ring.txt", ring, 0644) != 0) {
                    send_json(fd, 500, "Error", "{\"error\":\"write\"}");
                    return;
                }
            }
            if (json_string_field(body, "ring_color", color, sizeof color, &present) != 0) {
                send_json(fd, 400, "Bad Request", "{\"error\":\"color\"}");
                return;
            }
            if (present) {
                if (!hex_color(color)) {
                    send_json(fd, 400, "Bad Request", "{\"error\":\"color\"}");
                    return;
                }
                for (i = 0; color[i]; i++) {
                    if (color[i] >= 'A' && color[i] <= 'F') {
                        color[i] = (char)(color[i] - 'A' + 'a');
                    }
                }
                read_setting("ring.color", previous, sizeof previous);
                if (strcmp(previous, color) != 0) {
                    flash = 1;
                }
                if (write_setting("ring.color", color, 0644) != 0) {
                    send_json(fd, 500, "Error", "{\"error\":\"write\"}");
                    return;
                }
            }
            if (json_string_field(body, "ring_bright", bright, sizeof bright, &present) != 0) {
                send_json(fd, 400, "Bad Request", "{\"error\":\"bright\"}");
                return;
            }
            if (present) {
                if (decimal_range(bright, &number, 255) != 0) {
                    send_json(fd, 400, "Bad Request", "{\"error\":\"bright\"}");
                    return;
                }
                read_setting("ring.bright", previous, sizeof previous);
                if (strcmp(previous, bright) != 0) {
                    flash = 1;
                }
                if (write_setting("ring.bright", bright, 0644) != 0) {
                    send_json(fd, 500, "Error", "{\"error\":\"write\"}");
                    return;
                }
            }
            if (json_string_field(body, "ring_effect", effect, sizeof effect, &present) != 0) {
                send_json(fd, 400, "Bad Request", "{\"error\":\"effect\"}");
                return;
            }
            if (present) {
                if (!known_effect(effect)) {
                    send_json(fd, 400, "Bad Request", "{\"error\":\"effect\"}");
                    return;
                }
                read_setting("ring.effect", previous, sizeof previous);
                if (strcmp(previous, effect) != 0) {
                    flash = 1;
                    effect_flash = 1;
                }
                if (write_setting("ring.effect", effect, 0644) != 0) {
                    send_json(fd, 500, "Error", "{\"error\":\"write\"}");
                    return;
                }
            }
            if (json_string_field(body, "wake_sound", sound, sizeof sound, &present) != 0) {
                send_json(fd, 400, "Bad Request", "{\"error\":\"sound\"}");
                return;
            }
            if (present) {
                if ((sound[0] != '0' && sound[0] != '1') || sound[1] != 0) {
                    send_json(fd, 400, "Bad Request", "{\"error\":\"sound\"}");
                    return;
                }
                if (write_setting("wake.sound", sound, 0644) != 0) {
                    send_json(fd, 500, "Error", "{\"error\":\"write\"}");
                    return;
                }
            }
            {
                char lamp[8];
                char lamp_color[16];
                char lamp_bright[8];
                char previous_lamp[8];
                char previous_color[16];
                char previous_bright[8];
                int lamp_number = 0;
                if (json_string_field(body, "lamp", lamp, sizeof lamp, &present) != 0) {
                    send_json(fd, 400, "Bad Request", "{\"error\":\"lamp\"}");
                    return;
                }
                if (present) {
                    if ((lamp[0] != '0' && lamp[0] != '1') || lamp[1] != 0) {
                        send_json(fd, 400, "Bad Request", "{\"error\":\"lamp\"}");
                        return;
                    }
                    read_setting("lamp.txt", previous_lamp, sizeof previous_lamp);
                    if (strcmp(previous_lamp, lamp) != 0) {
                        lamp_touch = 1;
                    }
                    if (write_setting("lamp.txt", lamp, 0644) != 0) {
                        send_json(fd, 500, "Error", "{\"error\":\"write\"}");
                        return;
                    }
                }
                if (json_string_field(body, "lamp_color", lamp_color, sizeof lamp_color, &present) != 0) {
                    send_json(fd, 400, "Bad Request", "{\"error\":\"lamp\"}");
                    return;
                }
                if (present) {
                    if (!hex_color(lamp_color)) {
                        send_json(fd, 400, "Bad Request", "{\"error\":\"lamp\"}");
                        return;
                    }
                    for (i = 0; lamp_color[i]; i++) {
                        if (lamp_color[i] >= 'A' && lamp_color[i] <= 'F') {
                            lamp_color[i] = (char)(lamp_color[i] - 'A' + 'a');
                        }
                    }
                    read_setting("lamp.color", previous_color, sizeof previous_color);
                    if (strcmp(previous_color, lamp_color) != 0) {
                        lamp_touch = 1;
                    }
                    if (write_setting("lamp.color", lamp_color, 0644) != 0) {
                        send_json(fd, 500, "Error", "{\"error\":\"write\"}");
                        return;
                    }
                }
                if (json_string_field(body, "lamp_bright", lamp_bright, sizeof lamp_bright, &present) != 0) {
                    send_json(fd, 400, "Bad Request", "{\"error\":\"lamp\"}");
                    return;
                }
                if (present) {
                    if (decimal_range(lamp_bright, &lamp_number, 255) != 0) {
                        send_json(fd, 400, "Bad Request", "{\"error\":\"lamp\"}");
                        return;
                    }
                    read_setting("lamp.bright", previous_bright, sizeof previous_bright);
                    if (strcmp(previous_bright, lamp_bright) != 0) {
                        lamp_touch = 1;
                    }
                    if (write_setting("lamp.bright", lamp_bright, 0644) != 0) {
                        send_json(fd, 500, "Error", "{\"error\":\"write\"}");
                        return;
                    }
                }
            }
            if (json_string_field(body, "volume", volume, sizeof volume, &present) != 0) {
                send_json(fd, 400, "Bad Request", "{\"error\":\"volume\"}");
                return;
            }
            if (present && (decimal_range(volume, &number, 100) != 0 || set_master_volume(number) != 0)) {
                send_json(fd, 400, "Bad Request", "{\"error\":\"volume\"}");
                return;
            }
            if (lamp_touch) {
                apply_lamp();
            } else if (effect_flash) {
                flash_ring_ms(5000);
            } else if (flash) {
                flash_ring();
            }
        }
        send_json(fd, 200, "OK", "{\"ok\":true}");
        return;
    }

    if (strcmp(method, "GET") == 0 && strcmp(path_buf, "/api/heard") == 0) {
        send_heard(fd);
        return;
    }

    if (strcmp(method, "GET") == 0 && strcmp(path_buf, "/api/volume") == 0) {
        char json[48];
        snprintf(json, sizeof json, "{\"volume\":\"%d\"}", percent_from_mixer(master_volume()));
        send_json(fd, 200, "OK", json);
        return;
    }

    if (strcmp(method, "GET") == 0 && strcmp(path_buf, "/api/alarm-tone") == 0) {
        char label[192];
        char json[320];
        char tone[768];
        char vol[16];
        int volume = 40;
        label[0] = 0;
        if (read_setting("alarm.volume", vol, sizeof vol) == 0) {
            volume = atoi(vol);
        }
        if (volume < 5) {
            volume = 5;
        }
        if (volume > 100) {
            volume = 100;
        }
        snprintf(tone, sizeof tone, "%s/alarm.raw", root_dir);
        if (access(tone, R_OK) != 0) {
            snprintf(json, sizeof json, "{\"label\":\"Встроенная\",\"volume\":%d}", volume);
            send_json(fd, 200, "OK", json);
            return;
        }
        if (read_setting("alarm.label", label, sizeof label) != 0 || label[0] == 0) {
            snprintf(label, sizeof label, "Своя мелодия");
        }
        {
            char clean[192];
            size_t i;
            size_t o = 0;
            for (i = 0; label[i] && o + 1 < sizeof clean; i++) {
                if (label[i] == '"' || label[i] == '\\') {
                    continue;
                }
                clean[o++] = label[i];
            }
            clean[o] = 0;
            snprintf(json, sizeof json, "{\"label\":\"%s\",\"volume\":%d}", clean[0] ? clean : "Своя мелодия", volume);
        }
        send_json(fd, 200, "OK", json);
        return;
    }

    if (strcmp(method, "POST") == 0 && strcmp(path_buf, "/api/alarm-tone/search") == 0) {
        char raw[4096];
        char murl[200];
        const char *query = body ? body : "";
        if (wget_body(yse_url(murl, sizeof murl, YSE_MUSIC, "/v1/alarm-search"), query, raw, sizeof raw) != 0
            || raw[0] != '{') {
            send_json(fd, 502, "Bad Gateway", "{\"error\":\"music\"}");
            return;
        }
        send_json(fd, 200, "OK", raw);
        return;
    }

    if (strcmp(method, "POST") == 0 && strcmp(path_buf, "/api/alarm-tone/track") == 0) {
        char raw[512];
        char murl[200];
        char id[24];
        size_t i;
        size_t n = 0;
        const char *text = body ? body : "";
        for (i = 0; text[i] && n + 1 < sizeof id; i++) {
            if (text[i] < '0' || text[i] > '9') {
                continue;
            }
            id[n++] = text[i];
        }
        id[n] = 0;
        if (n == 0) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"трек\"}");
            return;
        }
        if (wget_slow(yse_url(murl, sizeof murl, YSE_MUSIC, "/v1/alarm-track"), id, raw, sizeof raw) != 0
            || raw[0] != '{') {
            send_json(fd, 502, "Bad Gateway", "{\"error\":\"music\"}");
            return;
        }
        send_json(fd, 200, "OK", raw);
        return;
    }

    if (strcmp(method, "POST") == 0 && strcmp(path_buf, "/api/alarm-tone/volume") == 0) {
        int volume = body ? atoi(body) : 40;
        char text[8];
        if (volume < 5) {
            volume = 5;
        }
        if (volume > 100) {
            volume = 100;
        }
        snprintf(text, sizeof text, "%d", volume);
        if (write_setting("alarm.volume", text, 0644) != 0) {
            send_json(fd, 500, "Error", "{\"error\":\"write\"}");
            return;
        }
        send_json(fd, 200, "OK", "{\"ok\":true}");
        return;
    }

    if (strcmp(method, "POST") == 0 && strcmp(path_buf, "/api/alarm-tone/builtin") == 0) {
        char raw[256];
        char murl[200];
        if (wget_body(yse_url(murl, sizeof murl, YSE_MUSIC, "/v1/alarm-builtin"), "", raw, sizeof raw) != 0
            || raw[0] != '{') {
            send_json(fd, 502, "Bad Gateway", "{\"error\":\"music\"}");
            return;
        }
        send_json(fd, 200, "OK", raw);
        return;
    }

    if (strcmp(method, "POST") == 0 && strcmp(path_buf, "/api/alarm-tone/file") == 0) {
        char raw[512];
        if (!body || body_len < 1000 || body_len > 8u * 1024u * 1024u) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"Файл больше 8 МБ или пустой\"}");
            return;
        }
        {
            struct yse_host conf;
            yse_host_load(&conf);
            if (http_post(conf.ip, conf.base + YSE_MUSIC, "/v1/alarm-file", body, body_len, raw, sizeof raw) != 0) {
                send_json(fd, 502, "Bad Gateway", "{\"error\":\"music\"}");
                return;
            }
        }
        send_json(fd, 200, "OK", raw);
        return;
    }

    if (strcmp(method, "GET") == 0 && strcmp(path_buf, "/api/alarms") == 0) {
        send_alarms(fd);
        return;
    }

    if (strcmp(method, "POST") == 0 && strcmp(path_buf, "/api/alarms") == 0) {
        char op[16];
        char at_text[32];
        char when_text[16];
        char mins_text[8];
        char id_text[32];
        char days[16];
        char stamp_text[32];
        char label[160];
        int op_set = 0;
        int at_set = 0;
        int when_set = 0;
        int mins_set = 0;
        int id_set = 0;
        int days_set = 0;
        int stamp_set = 0;
        int label_set = 0;
        int hour = -1;
        int minute = 0;
        int rc = -1;
        const char *payload = body ? body : "";
        if (json_string_field(payload, "op", op, sizeof op, &op_set) != 0
            || json_string_field(payload, "at", at_text, sizeof at_text, &at_set) != 0
            || json_string_field(payload, "when", when_text, sizeof when_text, &when_set) != 0
            || json_string_field(payload, "mins", mins_text, sizeof mins_text, &mins_set) != 0
            || json_string_field(payload, "id", id_text, sizeof id_text, &id_set) != 0
            || json_string_field(payload, "days", days, sizeof days, &days_set) != 0
            || json_string_field(payload, "stamp", stamp_text, sizeof stamp_text, &stamp_set) != 0
            || json_string_field(payload, "label", label, sizeof label, &label_set) != 0
            || (when_set && clock_hm(when_text, &hour, &minute) != 0)) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"alarms\"}");
            return;
        }
        if (op_set && strcmp(op, "clear") == 0) {
            alarms_clear();
            send_alarms(fd);
            return;
        }
        if (id_set && op_set) {
            rc = alarm_change(atoll(id_text), op, hour, minute, days_set ? days : NULL, label_set ? label : NULL);
        } else if (stamp_set) {
            rc = alarm_add(0, 0, "-", atoll(stamp_text), label_set ? label : "");
        } else if (when_set) {
            rc = alarm_add(hour, minute, days_set && days[0] ? days : "-", 0, label_set ? label : "");
        } else if (mins_set) {
            int mins = 0;
            if (decimal_range(mins_text, &mins, 24 * 60) == 0 && mins >= 1) {
                rc = alarm_add(0, 0, "-", (long long)time(NULL) + (long long)mins * 60, label_set ? label : "");
            }
        } else if (at_set) {
            drop_alarm_at(atoll(at_text));
            rc = 0;
        }
        if (rc != 0) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"alarms\"}");
            return;
        }
        if (when_set || stamp_set || mins_set || (op_set && strcmp(op, "on") == 0)) {
            flash_ring();
        }
        send_alarms(fd);
        return;
    }

    if (strcmp(method, "GET") == 0 && strcmp(path_buf, "/api/ear") == 0) {
        send_ear(fd);
        return;
    }

    if (strcmp(method, "POST") == 0 && strcmp(path_buf, "/api/play") == 0) {
        int16_t *tone = NULL;
        const void *pcm = body;
        size_t n = body_len;
        int rc;
        if (n == 0) {
            size_t frames = 32000 / 4;
            size_t i;
            tone = malloc(frames * sizeof(int16_t));
            if (!tone) {
                send_json(fd, 500, "Error", "{\"error\":\"play\"}");
                return;
            }
            for (i = 0; i < frames; i++) {
                tone[i] = (i % 72 < 36) ? 1800 : -1800;
            }
            pcm = tone;
            n = frames * sizeof(int16_t);
        }
        if (n < 2 || (n & 1u) || n > 32000u * 2u * 20u) {
            free(tone);
            send_json(fd, 400, "Bad Request", "{\"error\":\"play\"}");
            return;
        }
        rc = play_pcm(pcm, n);
        free(tone);
        if (rc != 0) {
            send_json(fd, 500, "Error", "{\"error\":\"play\"}");
            return;
        }
        send_json(fd, 200, "OK", "{\"ok\":true}");
        return;
    }

    if (strcmp(method, "GET") == 0 && strcmp(path_buf, "/api/wifi") == 0) {
        send_scan(fd);
        return;
    }

    if (strcmp(method, "POST") == 0 && strcmp(path_buf, "/api/wifi") == 0) {
        if (write_wifi_block() != 0) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"wifi\"}");
            return;
        }
        send_json(fd, 200, "OK", "{\"ok\":true,\"reconnect\":true}");
        shutdown(fd, SHUT_WR);
        sleep(1);
        if (system("/etc/init.d/S40wifi restart >/dev/null 2>&1") != 0) {
            fprintf(stderr, "wifi restart failed\n");
        }
        return;
    }

    sp = path_buf;
    if (*sp == '/') {
        sp++;
    }
    while (*sp && nseg < 8) {
        seg[nseg++] = sp;
        sp = strchr(sp, '/');
        if (!sp) {
            break;
        }
        *sp++ = 0;
    }

    if (nseg == 2 && strcmp(seg[1], "status") == 0 && strcmp(method, "GET") == 0) {
        char active[64];
        char json[256];
        char phrases[640];
        DIR *dir;
        struct dirent *de;
        int count = 0;
        active_id(active, sizeof active);
        snprintf(phrases, sizeof phrases, "%s/phrases", root_dir);
        dir = opendir(phrases);
        if (dir) {
            while ((de = readdir(dir)) != NULL) {
                if (safe_id(de->d_name)) {
                    count++;
                }
            }
            closedir(dir);
        }
        snprintf(json, sizeof json,
                 "{\"ok\":true,\"port\":18080,\"active\":\"%s\",\"phrases\":%d,\"engine\":\"%s\"}",
                 active, count,
                 (active[0] == 0 || strcmp(active, "yandex") == 0) ? "yandex" : "wake");
        send_json(fd, 200, "OK", json);
        return;
    }

    if (nseg == 2 && strcmp(seg[1], "models") == 0 && strcmp(method, "GET") == 0) {
        char *list = NULL;
        if (list_models(&list) != 0 || !list) {
            send_json(fd, 500, "Error", "{\"error\":\"list\"}");
            return;
        }
        send_json(fd, 200, "OK", list);
        free(list);
        return;
    }

    if (nseg == 2 && strcmp(seg[1], "apply") == 0 && strcmp(method, "POST") == 0) {
        char id[64];
        if (!body || parse_field(body, "id", id, sizeof id) != 0 || !model_ready(id)) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"id\"}");
            return;
        }
        if (set_active(id) != 0) {
            send_json(fd, 500, "Error", "{\"error\":\"active\"}");
            return;
        }
        send_json(fd, 200, "OK", "{\"ok\":true}");
        return;
    }

    if (nseg == 2 && strcmp(seg[1], "phrases") == 0 && strcmp(method, "GET") == 0) {
        char *list = NULL;
        if (list_phrases(&list) != 0 || !list) {
            send_json(fd, 500, "Error", "{\"error\":\"list\"}");
            return;
        }
        send_json(fd, 200, "OK", list);
        free(list);
        return;
    }

    if (nseg == 2 && strcmp(seg[1], "phrases") == 0 && strcmp(method, "POST") == 0) {
        char title[192];
        char id[64];
        char json[256];
        char e[160];
        if (!body || parse_title(body, title, sizeof title) != 0) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"title\"}");
            return;
        }
        if (create_phrase(title, id, sizeof id) != 0) {
            send_json(fd, 500, "Error", "{\"error\":\"create\"}");
            return;
        }
        json_escape(title, e, sizeof e);
        snprintf(json, sizeof json, "{\"id\":\"%s\",\"title\":\"%s\"}", id, e);
        send_json(fd, 200, "OK", json);
        return;
    }

    if (nseg >= 3 && strcmp(seg[1], "phrases") == 0 && safe_id(seg[2])) {
        char dir[768];
        if (!phrase_dir(seg[2], dir, sizeof dir)) {
            send_json(fd, 400, "Bad Request", "{\"error\":\"id\"}");
            return;
        }
        if (nseg == 3 && strcmp(method, "DELETE") == 0) {
            delete_phrase(seg[2]);
            send_json(fd, 200, "OK", "{\"ok\":true}");
            return;
        }
        if (nseg == 4 && strcmp(seg[3], "activate") == 0 && strcmp(method, "POST") == 0) {
            struct stat st;
            if (stat(dir, &st) != 0) {
                send_json(fd, 404, "Not Found", "{\"error\":\"phrase\"}");
                return;
            }
            if (set_active(seg[2]) != 0) {
                send_json(fd, 500, "Error", "{\"error\":\"active\"}");
                return;
            }
            send_json(fd, 200, "OK", "{\"ok\":true}");
            return;
        }
        if (nseg == 4 && strcmp(seg[3], "model") == 0 && strcmp(method, "POST") == 0) {
            char path[900];
            char ename[256];
            char json[320];
            struct stat st;
            if (stat(dir, &st) != 0) {
                send_json(fd, 404, "Not Found", "{\"error\":\"phrase\"}");
                return;
            }
            char spoken[192];
            char lowered[192];
            if (!body || body_len == 0 || body_len > MAX_BODY) {
                send_json(fd, 400, "Bad Request", "{\"error\":\"body\"}");
                return;
            }
            if (parse_yvk(body, body_len, spoken, sizeof spoken) != 0) {
                send_json(fd, 400, "Bad Request", "{\"error\":\"format\"}");
                return;
            }
            if (filename[0] == 0) {
                snprintf(filename, sizeof filename, "model.yvk");
            }
            if (!safe_name(filename)) {
                send_json(fd, 400, "Bad Request", "{\"error\":\"name\"}");
                return;
            }
            snprintf(path, sizeof path, "%s/model.bin", dir);
            if (write_file(path, body, body_len) != 0) {
                send_json(fd, 500, "Error", "{\"error\":\"write\"}");
                return;
            }
            snprintf(path, sizeof path, "%s/model.name", dir);
            if (write_file(path, filename, strlen(filename)) != 0) {
                send_json(fd, 500, "Error", "{\"error\":\"write\"}");
                return;
            }
            lower_ru(spoken, lowered, sizeof lowered);
            snprintf(path, sizeof path, "%s/return.txt", dir);
            if (write_file(path, lowered, strlen(lowered)) != 0) {
                send_json(fd, 500, "Error", "{\"error\":\"write\"}");
                return;
            }
            json_escape(filename, ename, sizeof ename);
            snprintf(json, sizeof json, "{\"name\":\"%s\",\"bytes\":%u}", ename, (unsigned)body_len);
            send_json(fd, 200, "OK", json);
            return;
        }
    }
    send_json(fd, 404, "Not Found", "{\"error\":\"not-found\"}");
}

static void serve_client(int fd) {
    char *buf;
    size_t used = 0;
    char *split;
    size_t head_len;
    int content_length = 0;
    char lenbuf[32];
    char *body;
    buf = malloc(MAX_HDR + 1);
    if (!buf) {
        close(fd);
        return;
    }
    while (used < MAX_HDR) {
        ssize_t r = read(fd, buf + used, MAX_HDR - used);
        if (r < 0) {
            if (errno == EINTR) {
                continue;
            }
            free(buf);
            return;
        }
        if (r == 0) {
            break;
        }
        used += (size_t)r;
        buf[used] = 0;
        split = strstr(buf, "\r\n\r\n");
        if (split) {
            head_len = (size_t)(split - buf);
            if (header_value(buf, "Content-Length", lenbuf, sizeof lenbuf) == 0) {
                content_length = atoi(lenbuf);
            }
            if (content_length < 0 || content_length > MAX_BODY) {
                send_json(fd, 413, "Payload Too Large", "{\"error\":\"size\"}");
                free(buf);
                return;
            }
            {
                size_t already = used - (head_len + 4);
                body = NULL;
                if (content_length > 0) {
                    body = malloc((size_t)content_length + 1);
                    if (!body) {
                        free(buf);
                        return;
                    }
                    if (already > (size_t)content_length) {
                        already = (size_t)content_length;
                    }
                    memcpy(body, split + 4, already);
                    if (already < (size_t)content_length) {
                        if (read_full(fd, body + already, (size_t)content_length - already) != 0) {
                            free(body);
                            free(buf);
                            return;
                        }
                    }
                    body[content_length] = 0;
                }
                handle(fd, buf, head_len, body, (size_t)content_length);
                free(body);
                free(buf);
                return;
            }
        }
    }
    send_json(fd, 400, "Bad Request", "{\"error\":\"headers\"}");
    free(buf);
}

int main(int argc, char **argv) {
    int port = 18080;
    int srv;
    int opt = 1;
    struct sockaddr_in addr;
    const char *root_arg = argc > 1 ? argv[1] : "/data/opt/yandex-station";
    if (argc > 2) {
        port = atoi(argv[2]);
    }
    if (port <= 0 || port > 65535) {
        return 2;
    }
    if (strlen(root_arg) >= sizeof root_dir) {
        return 2;
    }
    snprintf(root_dir, sizeof root_dir, "%s", root_arg);
    ignore_pipe();
    mkdir_p(root_dir);
    {
        char sub[640];
        snprintf(sub, sizeof sub, "%s/phrases", root_dir);
        mkdir_p(sub);
        snprintf(sub, sizeof sub, "%s/www", root_dir);
        mkdir_p(sub);
        snprintf(sub, sizeof sub, "%s/presets", root_dir);
        mkdir_p(sub);
    }
    load_token();
    start_bridge(root_dir);
    srv = socket(AF_INET, SOCK_STREAM, 0);
    if (srv < 0) {
        perror("socket");
        return 1;
    }
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof opt);
    memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET;
    addr.sin_port = htons((unsigned short)port);
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    if (bind(srv, (struct sockaddr *)&addr, sizeof addr) != 0) {
        perror("bind");
        return 1;
    }
    if (listen(srv, 8) != 0) {
        perror("listen");
        return 1;
    }
    fprintf(stderr, "ystationd root=%s port=%d\n", root_dir, port);
    fflush(stderr);
    for (;;) {
        int client = accept(srv, NULL, NULL);
        struct timeval tv;
        if (client < 0) {
            if (errno == EINTR) {
                continue;
            }
            perror("accept");
            continue;
        }
        tv.tv_sec = 20;
        tv.tv_usec = 0;
        setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
        serve_client(client);
        close(client);
    }
}
