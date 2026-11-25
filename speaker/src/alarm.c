#define _GNU_SOURCE
#include <fcntl.h>
#include <math.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "alarms.h"
#include "clean_ring.h"
#include "host_conf.h"

#define RATE 16000
#define BURST 15
#define NOTE_LEN 0.3
#define TUNE_LEN 4.2
#define OWN_TONE "/data/opt/yandex-station/alarm.raw"
/* Мини 2 держит hw:0,0 своим dmix: звонок идёт в него, но мимо softvol, чтобы звенел и на нулевой громкости. */
#define ALARM_DEV "dev=plughw:0,0; grep -q dmix_spk_plug /etc/asound.conf 2>/dev/null && dev=dmix_spk_plug; "

/* Мелодия будильника: мягкие «маримба»-ноты, ми мажорная пентатоника вверх и обратно. */
static const double TUNE[] = {659.25, 830.61, 987.77, 1318.51, 987.77, 830.61, 739.99, 987.77,
                              1108.73, 1318.51, 1108.73, 987.77};

static time_t timer_deadline(void) {
    FILE *fp = fopen("/data/opt/yandex-station/timer.txt", "r");
    long long at = 0;
    if (!fp) {
        return 0;
    }
    if (fscanf(fp, "%lld", &at) != 1) {
        at = 0;
    }
    fclose(fp);
    return at > 0 ? (time_t)at : 0;
}

/* Звонок, опоздавший больше чем на столько (колонка была выключена), не звенит, а переносится. */
#define LATE_MAX 600

/* 0 — звонить не нужно, 1 — пора звонить. Наступившие разовые удаляет, повторяющиеся переносит. */
static int take_due(time_t now) {
    struct alarm_item items[ALARMS_MAX];
    int n = alarms_load(items, ALARMS_MAX);
    int tz = alarm_tz();
    int ring_now = 0;
    int changed = 0;
    int kept = 0;
    int i;
    for (i = 0; i < n; i++) {
        struct alarm_item *it = &items[i];
        if (it->on && it->next <= 0 && it->days[0] != '-') {
            it->next = alarm_next_after(now, it->hour, it->minute, it->days, tz);
            changed = 1;
        }
        if (it->on && it->next > 0 && it->next <= (long long)now) {
            if ((long long)now - it->next <= LATE_MAX) {
                ring_now = 1;
            }
            changed = 1;
            if (it->days[0] == '-') {
                continue;
            }
            it->next = alarm_next_after(now, it->hour, it->minute, it->days, tz);
        }
        items[kept++] = *it;
    }
    if (changed) {
        alarms_save(items, kept);
    }
    return ring_now;
}

static int stopped(void) {
    return access("/data/opt/yandex-station/alarm.stop", F_OK) == 0;
}

static int alarm_percent(void) {
    FILE *fp = fopen("/data/opt/yandex-station/settings/alarm.volume", "r");
    int v = 40;
    if (fp) {
        if (fscanf(fp, "%d", &v) != 1) {
            v = 40;
        }
        fclose(fp);
    }
    if (v < 5) {
        v = 5;
    }
    if (v > 100) {
        v = 100;
    }
    return v;
}

/* Своя мелодия лежит полной громкостью. Здесь она становится уровнем будильника. */
static int scale_raw(const char *src, const char *dst, int percent) {
    FILE *in = fopen(src, "rb");
    FILE *out;
    int16_t sample;
    if (!in) {
        return -1;
    }
    out = fopen(dst, "wb");
    if (!out) {
        fclose(in);
        return -1;
    }
    while (fread(&sample, sizeof sample, 1, in) == 1) {
        int v = (int)sample * percent / 100;
        if (v > 32767) {
            v = 32767;
        } else if (v < -32768) {
            v = -32768;
        }
        sample = (int16_t)v;
        if (fwrite(&sample, sizeof sample, 1, out) != 1) {
            fclose(in);
            fclose(out);
            return -1;
        }
    }
    fclose(in);
    fclose(out);
    return 0;
}

/* Одна мелодия в /tmp/alarm.pcm с громкостью gain (0..1). */
static int write_tune(double gain, int percent) {
    int total = (int)(TUNE_LEN * RATE);
    int notes = (int)(sizeof TUNE / sizeof TUNE[0]);
    int16_t *buf = calloc((size_t)total, sizeof(int16_t));
    FILE *fp;
    int n;
    int i;
    if (!buf) {
        return -1;
    }
    for (n = 0; n < notes; n++) {
        int at = (int)(n * NOTE_LEN * RATE);
        int len = (int)(1.2 * RATE);
        for (i = 0; i < len && at + i < total; i++) {
            double t = (double)i / RATE;
            double attack = i < 80 ? (double)i / 80.0 : 1.0;
            double body = exp(-t * 5.0) * sin(2.0 * M_PI * TUNE[n] * t);
            double knock = 0.3 * exp(-t * 22.0) * sin(2.0 * M_PI * TUNE[n] * 4.0 * t);
            double v = buf[at + i] + attack * (body + knock) * 11000.0 * gain * (percent / 100.0);
            buf[at + i] = (int16_t)(v > 32767 ? 32767 : (v < -32768 ? -32768 : v));
        }
    }
    fp = fopen("/tmp/alarm.pcm", "wb");
    if (!fp) {
        free(buf);
        return -1;
    }
    n = fwrite(buf, sizeof(int16_t), (size_t)total, fp) == (size_t)total ? 0 : -1;
    fclose(fp);
    free(buf);
    return n;
}

/* Модель команд «стоп» поверх звонка не слышит. Раз в ~1,6 с последние 2 с микрофона
   уходят на сервер: ответ «stop» гасит звонок. */
static void ask_stop(void) {
    int fd;
    struct stat st;
    struct CleanRing *ring;
    int16_t *buf;
    uint32_t at;
    uint32_t n;
    FILE *out;
    char url[160];
    char cmd[640];
    fd = open("/data/opt/yandex-station/clean.ring", O_RDONLY);
    if (fd < 0) {
        return;
    }
    if (fstat(fd, &st) != 0 || st.st_size < (off_t)sizeof(struct CleanRing)) {
        close(fd);
        return;
    }
    ring = mmap(NULL, sizeof *ring, PROT_READ, MAP_SHARED, fd, 0);
    close(fd);
    if (ring == MAP_FAILED || ring->magic != CLEAN_MAGIC || ring->write_pos < 16000) {
        if (ring != MAP_FAILED) {
            munmap(ring, sizeof *ring);
        }
        return;
    }
    n = 16000 * 2;
    buf = malloc((size_t)n * sizeof(int16_t));
    if (!buf) {
        munmap(ring, sizeof *ring);
        return;
    }
    for (at = 0; at < n; at++) {
        buf[at] = ring->pcm[(ring->write_pos - n + at) & (CLEAN_CAP - 1u)];
    }
    munmap(ring, sizeof *ring);
    out = fopen("/tmp/alarm-hear.raw", "wb");
    if (!out) {
        free(buf);
        return;
    }
    if (fwrite(buf, sizeof(int16_t), n, out) != n) {
        fclose(out);
        free(buf);
        return;
    }
    fclose(out);
    free(buf);
    yse_url(url, sizeof url, YSE_TALK, "/v1/alarm-hear");
    snprintf(cmd, sizeof cmd,
             "wget -q -T 2 -O /tmp/alarm-hear.out --post-file=/tmp/alarm-hear.raw "
             "--header='Content-Type: application/octet-stream' '%s' "
             "&& grep -qx stop /tmp/alarm-hear.out "
             "&& touch /data/opt/yandex-station/alarm.stop",
             url);
    if (system(cmd) != 0 && !stopped()) {
        fflush(stdout);
    }
}

static int play_file(const char *path) {
    char cmd[320];
    pid_t pid;
    FILE *mark;
    snprintf(cmd, sizeof cmd,
             ALARM_DEV "exec aplay -D \"$dev\" -f S16_LE -r 16000 -c 1 -t raw '%s' >/dev/null 2>&1",
             path);
    pid = fork();
    if (pid < 0) {
        return -1;
    }
    if (pid == 0) {
        execl("/bin/sh", "sh", "-c", cmd, (char *)NULL);
        _exit(127);
    }
    mark = fopen("/tmp/alarm.play", "w");
    if (mark) {
        fprintf(mark, "%d\n", (int)pid);
        fclose(mark);
    }
    return (int)pid;
}

static int ring_seconds(const char *path) {
    struct stat st;
    if (stat(path, &st) != 0 || st.st_size < RATE) {
        return 0;
    }
    return (int)(st.st_size / (RATE * 2));
}

/* 0 — мелодия доиграла, 1 — её остановили. Пока играет, ловим «стоп». */
static int wait_play(int pid) {
    int ticks = 0;
    int status = 0;
    pid_t got;
    while ((got = waitpid((pid_t)pid, &status, WNOHANG)) == 0) {
        if (stopped()) {
            kill((pid_t)pid, SIGTERM);
            waitpid((pid_t)pid, &status, 0);
            return 1;
        }
        if (++ticks == 8) {
            ticks = 0;
            ask_stop();
            if (stopped()) {
                kill((pid_t)pid, SIGTERM);
                waitpid((pid_t)pid, &status, 0);
                return 1;
            }
        }
        usleep(200000);
    }
    if (got > 0 && ticks == 0 && WIFEXITED(status) && WEXITSTATUS(status) != 0) {
        return 1;
    }
    return stopped();
}

/* Короткий писк таймера: три импульса и пауза, как у Яндекса, не мелодия будильника. */
static int write_beeps(int percent) {
    int total = (int)(0.9 * RATE);
    int16_t *buf = calloc((size_t)total, sizeof(int16_t));
    FILE *fp;
    int pulse;
    int n;
    if (!buf) {
        return -1;
    }
    for (pulse = 0; pulse < 3; pulse++) {
        int at = (int)(pulse * 0.16 * RATE);
        int len = (int)(0.08 * RATE);
        int i;
        for (i = 0; i < len && at + i < total; i++) {
            double t = (double)i / RATE;
            double env = i < 40 ? (double)i / 40.0 : 1.0;
            double v = env * sin(2.0 * M_PI * 988.0 * t) * 14000.0 * (percent / 100.0);
            buf[at + i] = (int16_t)(v > 32767 ? 32767 : (v < -32768 ? -32768 : v));
        }
    }
    fp = fopen("/tmp/alarm.pcm", "wb");
    if (!fp) {
        free(buf);
        return -1;
    }
    n = fwrite(buf, sizeof(int16_t), (size_t)total, fp) == (size_t)total ? 0 : -1;
    fclose(fp);
    free(buf);
    return n;
}

static void timer_ring(void) {
    time_t until = time(NULL) + 3 * 60;
    FILE *flag = fopen("/tmp/alarm.ring", "w");
    FILE *mark = fopen("/tmp/timer.ring", "w");
    if (flag) {
        fclose(flag);
    }
    if (mark) {
        fclose(mark);
    }
    unlink("/data/opt/yandex-station/alarm.stop");
    unlink("/data/opt/yandex-station/timer.txt");
    printf("timer ring\n");
    fflush(stdout);
    while (!stopped() && time(NULL) < until) {
        int pid;
        if (write_beeps(alarm_percent()) != 0) {
            break;
        }
        pid = play_file("/tmp/alarm.pcm");
        if (pid < 0 || wait_play(pid)) {
            break;
        }
    }
    unlink("/data/opt/yandex-station/alarm.stop");
    unlink("/tmp/alarm.play");
    unlink("/tmp/alarm.pcm");
    unlink("/tmp/alarm.ring");
    unlink("/tmp/timer.ring");
}

static void ring(void) {
    int burst;
    int seconds;
    int own = access(OWN_TONE, R_OK) == 0;
    FILE *flag = fopen("/tmp/alarm.ring", "w");
    if (flag) {
        fclose(flag);
    }
    unlink("/data/opt/yandex-station/alarm.stop");
    if (system(
            "color=$(cat /data/opt/yandex-station/settings/ring.color 2>/dev/null || echo 3aa0ff); "
            "bright=$(cat /data/opt/yandex-station/settings/ring.bright 2>/dev/null || echo 160); "
            "effect=$(cat /data/opt/yandex-station/settings/ring.effect 2>/dev/null || echo wave); "
            "case $effect in wave|breathe|spin|fill|spark|shift|comet|aurora|twinkle) ;; *) effect=wave ;; esac; "
            "/data/opt/yandex-station/bin/glow \"$color\" 8000 \"$effect\" \"$bright\" >/dev/null 2>&1 &") != 0) {
        fflush(stdout);
    }
    seconds = own ? ring_seconds(OWN_TONE) : 0;
    if (own && seconds > 0) {
        /* Короткий сигнал крутим три минуты. Песню длиннее 20 с играем один раз. */
        time_t until = time(NULL) + (seconds > 20 ? seconds + 2 : 3 * 60);
        while (!stopped() && time(NULL) < until) {
            int pid;
            if (scale_raw(OWN_TONE, "/tmp/alarm.pcm", alarm_percent()) != 0) {
                break;
            }
            pid = play_file("/tmp/alarm.pcm");
            if (pid < 0 || wait_play(pid)) {
                break;
            }
            if (seconds > 20) {
                break;
            }
        }
    } else {
        for (burst = 0; burst < BURST && !stopped(); burst++) {
            int pid;
            double gain = 0.3 + 0.7 * (burst < 5 ? burst / 5.0 : 1.0);
            if (write_tune(gain, alarm_percent()) != 0) {
                break;
            }
            pid = play_file("/tmp/alarm.pcm");
            if (pid < 0 || wait_play(pid)) {
                break;
            }
        }
    }
    unlink("/data/opt/yandex-station/alarm.stop");
    unlink("/tmp/alarm.play");
    unlink("/tmp/alarm.pcm");
    unlink("/tmp/alarm.ring");
}

int main(void) {
    for (;;) {
        time_t now = time(NULL);
        if (take_due(now)) {
            printf("alarm ring\n");
            fflush(stdout);
            ring();
        } else {
            time_t timer_at = timer_deadline();
            if (timer_at > 0 && timer_at <= now) {
                timer_ring();
            }
        }
        sleep(1);
    }
    return 0;
}
