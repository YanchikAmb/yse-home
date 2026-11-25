#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/input.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <poll.h>
#include <signal.h>
#include <sys/ioctl.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

#include "host_conf.h"
#include "mini2_led.h"

#ifndef KEY_VOICECOMMAND
#define KEY_VOICECOMMAND 582
#endif

#define STEP 5

static int has_volume(int fd) {
    unsigned char bits[(KEY_MAX / 8) + 1];
    memset(bits, 0, sizeof bits);
    if (ioctl(fd, EVIOCGBIT(EV_KEY, sizeof bits), bits) < 0) {
        return 0;
    }
    return (bits[KEY_VOLUMEUP / 8] & (1u << (KEY_VOLUMEUP % 8)))
        && (bits[KEY_VOLUMEDOWN / 8] & (1u << (KEY_VOLUMEDOWN % 8)));
}

static int open_event(int index, char *name, size_t name_len) {
    char path[80];
    int fd;
    ssize_t n;
    snprintf(path, sizeof path, "/sys/class/input/event%d/device/name", index);
    fd = open(path, O_RDONLY);
    if (fd < 0) {
        return -1;
    }
    n = read(fd, name, name_len - 1);
    close(fd);
    if (n <= 0) {
        return -1;
    }
    name[n] = 0;
    snprintf(path, sizeof path, "/dev/input/event%d", index);
    return open(path, O_RDONLY | O_CLOEXEC);
}

static void print_bits(int fd) {
    unsigned char bits[(KEY_MAX / 8) + 1];
    int code;
    memset(bits, 0, sizeof bits);
    if (ioctl(fd, EVIOCGBIT(EV_KEY, sizeof bits), bits) < 0) {
        fprintf(stderr, "bits: %s\n", strerror(errno));
        return;
    }
    fprintf(stderr, "keycodes");
    for (code = 0; code <= KEY_MAX; code++) {
        if (bits[code / 8] & (1u << (code % 8))) {
            fprintf(stderr, " %d", code);
        }
    }
    fprintf(stderr, "\n");
}

static int read_volume(void) {
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
        }
    }
    pclose(fp);
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
        "dev=plughw:0,0; grep -q softvol /etc/asound.conf 2>/dev/null && dev=default; "
        "aplay -D $dev -f S16_LE -r 16000 -c 1 -t raw "
        "/data/opt/yandex-station/volprev.pcm >/dev/null 2>&1 & "
        "echo $! > /data/opt/yandex-station/volprev.pid");
}

static void stop_alarm(void) {
    FILE *fp;
    char pid[24];
    long n;
    fp = fopen("/data/opt/yandex-station/alarm.stop", "w");
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

static void set_volume(int percent, int beep) {
    char cmd[96];
    int mixer = mixer_from_percent(percent);
    snprintf(cmd, sizeof cmd, "amixer -c 0 set Master %d >/dev/null 2>&1", mixer);
    if (system(cmd) != 0) {
        fprintf(stderr, "volume %d failed\n", percent);
        return;
    }
    fprintf(stderr, "volume %d%% mixer %d\n", percent, mixer);
    if (!beep) {
        return;
    }
    play_preview();
    system(
        "color=$(cat /data/opt/yandex-station/settings/ring.color 2>/dev/null || echo 3aa0ff); "
        "bright=$(cat /data/opt/yandex-station/settings/ring.bright 2>/dev/null || echo 160); "
        "effect=$(cat /data/opt/yandex-station/settings/ring.effect 2>/dev/null || echo wave); "
        "case $effect in wave|breathe|spin|fill|spark|shift|comet|aurora|twinkle) ;; *) effect=wave ;; esac; "
        "/data/opt/yandex-station/bin/glow \"$color\" 450 \"$effect\" \"$bright\" >/dev/null 2>&1 &");
}

static int is_talk(int code) {
    return code == KEY_VOICECOMMAND || code == KEY_MUTE;
}

static void music_toggle(void) {
    char url[200];
    char cmd[420];
    yse_url(url, sizeof url, YSE_MUSIC, "/v1/ctrl");
    snprintf(cmd, sizeof cmd, "wget -q -T 3 -O /dev/null --post-data='toggle' '%s'", url);
    if (system(cmd) != 0) {
        fprintf(stderr, "music toggle failed\n");
    }
}

static void listen_now(void) {
    struct timespec ts;
    FILE *fp;
    clock_gettime(CLOCK_REALTIME, &ts);
    fp = fopen("/data/opt/yandex-station/hear.go", "w");
    if (!fp) {
        fprintf(stderr, "hear.go: %s\n", strerror(errno));
        return;
    }
    fprintf(fp, "%lld\n", (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000);
    fclose(fp);
    fprintf(stderr, "listen\n");
    system(
        "color=$(cat /data/opt/yandex-station/settings/ring.color 2>/dev/null || echo 3aa0ff); "
        "bright=$(cat /data/opt/yandex-station/settings/ring.bright 2>/dev/null || echo 160); "
        "/data/opt/yandex-station/bin/glow \"$color\" 5000 breathe \"$bright\" >/dev/null 2>&1 &");
}

static int bit_on(const unsigned char *bits, int code) {
    return (bits[code / 8] >> (code % 8)) & 1;
}

static int has_talk(int fd) {
    unsigned char bits[(KEY_MAX / 8) + 1];
    memset(bits, 0, sizeof bits);
    if (ioctl(fd, EVIOCGBIT(EV_KEY, sizeof bits), bits) < 0) {
        return 0;
    }
    return bit_on(bits, KEY_VOICECOMMAND) || bit_on(bits, KEY_MUTE);
}

static int open_named(const char *needle) {
    int index;
    char name[64];
    int fd;
    for (index = 0; index < 16; index++) {
        fd = open_event(index, name, sizeof name);
        if (fd < 0) {
            continue;
        }
        if (strstr(name, needle)) {
            fprintf(stderr, "keys /dev/input/event%d %s", index, name);
            return fd;
        }
        close(fd);
    }
    return -1;
}

static int hp_saved = -1;
static int hp_now = -1;

static void apply_headphones(int in) {
    int now;
    if (in == hp_now) {
        return;
    }
    if (in) {
        now = read_volume();
        if (now >= 0) {
            hp_saved = percent_from_mixer(now);
        }
        set_volume(0, 0);
    } else if (hp_saved >= 0) {
        set_volume(hp_saved, 0);
        hp_saved = -1;
    }
    hp_now = in;
    fprintf(stderr, "headphones %s\n", in ? "in" : "out");
}

static int switch_on(int fd, int code) {
    unsigned char bits[(SW_MAX / 8) + 1];
    memset(bits, 0, sizeof bits);
    if (ioctl(fd, EVIOCGSW(sizeof bits), bits) < 0) {
        return 0;
    }
    return bit_on(bits, code);
}

int main(int argc, char **argv) {
    int fds[8];
    int count = 0;
    int index;
    int hp;
    struct pollfd pf[9];
    int up = KEY_VOLUMEUP;
    int down = KEY_VOLUMEDOWN;
    struct timeval last;
    struct timeval talk_at;
    int talk_down = 0;
    for (index = 0; index < 16 && count < 8; index++) {
        char name[64];
        int fd = open_event(index, name, sizeof name);
        if (fd < 0) {
            continue;
        }
        if (!has_volume(fd) && !has_talk(fd)) {
            close(fd);
            continue;
        }
        fprintf(stderr, "keys /dev/input/event%d %s", index, name);
        print_bits(fd);
        fds[count++] = fd;
    }
    hp = open_named("Headphone");
    if (count == 0 && hp < 0) {
        fprintf(stderr, "keys: buttons missing\n");
        return 1;
    }
    fprintf(stderr, "event %u\n", (unsigned)sizeof(struct input_event));
    if (argc > 1 && strcmp(argv[1], "bits") == 0) {
        for (index = 0; index < count; index++) {
            close(fds[index]);
        }
        if (hp >= 0) {
            close(hp);
        }
        return 0;
    }
    if (hp >= 0) {
        apply_headphones(switch_on(hp, SW_HEADPHONE_INSERT));
    }
    last.tv_sec = 0;
    last.tv_usec = 0;
    talk_at = last;
    for (;;) {
        int ready;
        int slot;
        for (slot = 0; slot < count; slot++) {
            pf[slot].fd = fds[slot];
            pf[slot].events = POLLIN;
            pf[slot].revents = 0;
        }
        if (hp >= 0) {
            pf[count].fd = hp;
            pf[count].events = POLLIN;
            pf[count].revents = 0;
        }
        ready = poll(pf, (nfds_t)(count + (hp >= 0)), -1);
        if (ready < 0) {
            if (errno == EINTR) {
                continue;
            }
            fprintf(stderr, "poll: %s\n", strerror(errno));
            return 1;
        }
        for (slot = 0; slot < count + (hp >= 0); slot++) {
            struct input_event ev;
            ssize_t n;
            long long delta;
            int now;
            int next;
            if (!(pf[slot].revents & POLLIN)) {
                continue;
            }
            n = read(pf[slot].fd, &ev, sizeof ev);
            if (n != (ssize_t)sizeof ev) {
                continue;
            }
            if (ev.type == EV_SW && ev.code == SW_HEADPHONE_INSERT) {
                apply_headphones(ev.value ? 1 : 0);
                continue;
            }
            if (ev.type != EV_KEY) {
                continue;
            }
            if (ev.value == 1 && access("/tmp/alarm.ring", F_OK) == 0) {
                fprintf(stderr, "alarm stop key\n");
                stop_alarm();
                continue;
            }
            if (is_talk(ev.code) && ev.value == 1) {
                talk_down = 1;
                talk_at = ev.time;
                continue;
            }
            if (is_talk(ev.code) && ev.value == 0 && talk_down) {
                delta = (long long)(ev.time.tv_sec - talk_at.tv_sec) * 1000
                    + (ev.time.tv_usec - talk_at.tv_usec) / 1000;
                talk_down = 0;
                if (delta >= 550) {
                    listen_now();
                } else {
                    music_toggle();
                }
                continue;
            }
            if (ev.code != up && ev.code != down) {
                continue;
            }
            if (ev.value != 1 && ev.value != 2) {
                continue;
            }
            delta = (long long)(ev.time.tv_sec - last.tv_sec) * 1000
                + (ev.time.tv_usec - last.tv_usec) / 1000;
            if (ev.value == 2 && delta >= 0 && delta < 140) {
                continue;
            }
            last = ev.time;
            now = read_volume();
            if (now < 0) {
                continue;
            }
            next = percent_from_mixer(now);
            next = ev.code == up ? next + STEP : next - STEP;
            set_volume(next, 1);
        }
    }
}
