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

#include "clean_ring.h"
#include "wake_model.h"
#include "wake_templates.h"

static volatile sig_atomic_t run = 1;
static int ring_fd = -1;
static float wake_mel[WAKE_NMEL * WAKE_NBINS];
static float wake_tmpl[WAKE_NTEMP * WAKE_MAXF * WAKE_NMEL];
static float wake_neg[WAKE_NNEG * WAKE_MAXF * WAKE_NMEL];

static int read_exact(const char *path, void *dst, size_t n) {
    int fd = open(path, O_RDONLY);
    size_t got = 0;
    if (fd < 0) {
        return 0;
    }
    while (got < n) {
        ssize_t r = read(fd, (char *)dst + got, n - got);
        if (r <= 0) {
            close(fd);
            return 0;
        }
        got += (size_t)r;
    }
    close(fd);
    return 1;
}

static int load_models(void) {
    const char *dir = getenv("WAKE_DIR");
    char path[256];
    if (!dir || !dir[0]) {
        dir = "/data/opt/yandex-station";
    }
    snprintf(path, sizeof path, "%s/wake_mel.bin", dir);
    if (!read_exact(path, wake_mel, sizeof wake_mel)) {
        return 0;
    }
    snprintf(path, sizeof path, "%s/wake_tmpl.bin", dir);
    if (!read_exact(path, wake_tmpl, sizeof wake_tmpl)) {
        return 0;
    }
    snprintf(path, sizeof path, "%s/wake_neg.bin", dir);
    if (!read_exact(path, wake_neg, sizeof wake_neg)) {
        return 0;
    }
    (void)wake_pos_max;
    (void)wake_nlen[0];
    return 1;
}

static void on_stop(int signo) {
    (void)signo;
    run = 0;
}

static void close_ring(void) {
    if (ring_fd >= 0) {
        close(ring_fd);
        ring_fd = -1;
    }
}

static void paint_ring(int on) {
    unsigned char buf[89 * 4];
    close_ring();
    ring_fd = open("/dev/fb0", O_RDWR | O_CLOEXEC);
    if (ring_fd < 0) {
        printf("light fail\n");
        fflush(stdout);
        return;
    }
    memset(buf, on ? 0xff : 0x00, sizeof buf);
    if (lseek(ring_fd, 0, SEEK_SET) < 0) {
        return;
    }
    if (write(ring_fd, buf, sizeof buf) < 0) {
        printf("light fail\n");
        fflush(stdout);
    }
}

static void tell_house(const char *line) {
    int fd = open("/data/opt/yandex-station/house.cmd", O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
    size_t n = strlen(line);
    if (fd < 0) {
        return;
    }
    if (write(fd, line, n) < 0) {
        /* housed picks the command up on its next pass */
    }
    close(fd);
}

static void beep(void) {
    pid_t pid;
    close_ring();
    pid = fork();
    int16_t tone[4800];
    int pipes[2] = {-1, -1};
    int i;
    if (pid != 0) {
        return;
    }
    if (system("amixer -c 0 set Master 110 >/dev/null 2>&1") != 0) {
        /* the chirp still plays at the current volume */
    }
    for (i = 0; i < 4800; i++) {
        int on = ((i * 880) / 8000) & 1;
        int amp = i < 4200 ? 22000 : 22000 * (4800 - i) / 600;
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
    if (system("amixer -c 0 set Master 26 >/dev/null 2>&1") != 0) {
        /* leave the mixer where the chirp set it */
    }
    _exit(0);
}

static void bitrev(float *re, float *im) {
    int i;
    int j = 0;
    for (i = 1; i < WAKE_NFFT; i++) {
        int bit = WAKE_NFFT >> 1;
        for (; j & bit; bit >>= 1) {
            j ^= bit;
        }
        j ^= bit;
        if (i < j) {
            float tr = re[i];
            float ti = im[i];
            re[i] = re[j];
            im[i] = im[j];
            re[j] = tr;
            im[j] = ti;
        }
    }
}

static void fft512(float *re, float *im) {
    int len;
    bitrev(re, im);
    for (len = 2; len <= WAKE_NFFT; len <<= 1) {
        int half = len >> 1;
        float ang = -2.0f * 3.141592653589793f / (float)len;
        float wlen_re = cosf(ang);
        float wlen_im = sinf(ang);
        int i;
        for (i = 0; i < WAKE_NFFT; i += len) {
            float wr = 1.0f;
            float wi = 0.0f;
            int k;
            for (k = 0; k < half; k++) {
                int a = i + k;
                int b = a + half;
                float tr = wr * re[b] - wi * im[b];
                float ti = wr * im[b] + wi * re[b];
                float nwr;
                re[b] = re[a] - tr;
                im[b] = im[a] - ti;
                re[a] += tr;
                im[a] += ti;
                nwr = wr * wlen_re - wi * wlen_im;
                wi = wr * wlen_im + wi * wlen_re;
                wr = nwr;
            }
        }
    }
}

static float ham(int i) {
    return 0.54f - 0.46f * cosf(2.0f * 3.141592653589793f * (float)i / (float)(WAKE_WIN - 1));
}

static void mel_frame(const float *x, float *out) {
    float re[WAKE_NFFT];
    float im[WAKE_NFFT];
    int i;
    int m;
    memset(re, 0, sizeof re);
    memset(im, 0, sizeof im);
    for (i = 0; i < WAKE_WIN; i++) {
        float s = x[i];
        if (i > 0) {
            s -= 0.97f * x[i - 1];
        }
        re[i] = s * ham(i);
    }
    fft512(re, im);
    for (m = 0; m < WAKE_NMEL; m++) {
        float e = 0;
        int k;
        const float *row = wake_mel + m * WAKE_NBINS;
        for (k = 0; k < WAKE_NBINS; k++) {
            float mag = sqrtf(re[k] * re[k] + im[k] * im[k]);
            e += row[k] * mag;
        }
        if (e < 1e-6f) {
            e = 1e-6f;
        }
        out[m] = logf(e);
    }
}

#define WAKE_BUF 25600
#define WAKE_STEP 2560

static float seq_buf[WAKE_BUF];
static float seq_mels[WAKE_MAXF * WAKE_NMEL];

static int loud_span(const float *x, int n, int *from, int *to) {
    float peak = 0;
    float thr;
    int i;
    int first = -1;
    int last = -1;
    for (i = 0; i < n; i++) {
        float a = fabsf(x[i]);
        if (a > peak) {
            peak = a;
        }
    }
    thr = 0.004f;
    if (peak * wake_peak_frac > thr) {
        thr = peak * wake_peak_frac;
    }
    for (i = 0; i < n; i++) {
        if (fabsf(x[i]) > thr) {
            if (first < 0) {
                first = i;
            }
            last = i;
        }
    }
    if (first < 0 || last - first < 10) {
        return 0;
    }
    *from = first - 640;
    if (*from < 0) {
        *from = 0;
    }
    *to = last + 960;
    if (*to > n) {
        *to = n;
    }
    return 1;
}

static int mel_seq(const float *x, int n, float *mels) {
    int frames;
    int i;
    if (n < WAKE_WIN) {
        return 0;
    }
    frames = 1 + (n - WAKE_WIN) / WAKE_HOP;
    if (frames > WAKE_MAXF) {
        x += (frames - WAKE_MAXF) * WAKE_HOP;
        frames = WAKE_MAXF;
    }
    for (i = 0; i < frames; i++) {
        mel_frame(x + i * WAKE_HOP, mels + i * WAKE_NMEL);
    }
    return frames;
}

static double dtw(const float *a, int ta, const float *b, int tb) {
    double prev[WAKE_MAXF + 1];
    double cur[WAKE_MAXF + 1];
    int i;
    int j;
    if (ta > WAKE_MAXF) {
        ta = WAKE_MAXF;
    }
    if (tb > WAKE_MAXF) {
        tb = WAKE_MAXF;
    }
    for (j = 0; j <= tb; j++) {
        prev[j] = 1e9;
    }
    prev[0] = 0;
    for (i = 1; i <= ta; i++) {
        const float *ai = a + (i - 1) * WAKE_NMEL;
        cur[0] = 1e9;
        for (j = 1; j <= tb; j++) {
            const float *bj = b + (j - 1) * WAKE_NMEL;
            double cost = 0;
            double best;
            int k;
            for (k = 0; k < WAKE_NMEL; k++) {
                double diff = (double)ai[k] - (double)bj[k];
                cost += diff * diff;
            }
            cost /= (double)WAKE_NMEL;
            best = prev[j];
            if (cur[j - 1] < best) {
                best = cur[j - 1];
            }
            if (prev[j - 1] < best) {
                best = prev[j - 1];
            }
            cur[j] = cost + best;
        }
        memcpy(prev, cur, (size_t)(tb + 1) * sizeof(double));
    }
    return prev[tb] / (double)(ta + tb);
}

static void cmn(float *mels, int frames) {
    int k;
    int i;
    for (k = 0; k < WAKE_NMEL; k++) {
        double acc = 0;
        float mean;
        for (i = 0; i < frames; i++) {
            acc += mels[i * WAKE_NMEL + k];
        }
        mean = (float)(acc / (double)frames);
        for (i = 0; i < frames; i++) {
            mels[i * WAKE_NMEL + k] -= mean;
        }
    }
}

/* Короткий шорох и низкий гул не похожи на слово: гул почти не
   пересекает ноль, щелчок короче трети секунды. */
static int speech_like(const float *x, int n) {
    float peak = 0;
    float thr;
    int i;
    int first = -1;
    int last = -1;
    int crosses = 0;
    for (i = 0; i < n; i++) {
        float a = fabsf(x[i]);
        if (a > peak) {
            peak = a;
        }
    }
    if (peak < 1e-4f) {
        return 0;
    }
    thr = 0.004f;
    if (peak * wake_peak_frac > thr) {
        thr = peak * wake_peak_frac;
    }
    for (i = 0; i < n; i++) {
        if (fabsf(x[i]) > thr) {
            if (first < 0) {
                first = i;
            }
            last = i;
        }
    }
    if (first < 0 || last - first < 3200 || last - first > 8000) {
        return 0;
    }
    for (i = first + 1; i <= last; i++) {
        if ((x[i - 1] < 0.f) != (x[i] < 0.f)) {
            crosses++;
        }
    }
    return (double)crosses / (double)(last - first) >= (double)wake_zcr_min;
}

static void score_of(const float *x, int n, double *pos, double *neg) {
    int from;
    int to;
    int len;
    int frames;
    int i;
    int t;
    double acc = 0;
    double rms;
    float gain;
    *pos = 1e9;
    *neg = 0;
    if (!speech_like(x, n)) {
        return;
    }
    if (!loud_span(x, n, &from, &to)) {
        return;
    }
    len = to - from;
    if (len > WAKE_BUF) {
        from += len - WAKE_BUF;
        len = WAKE_BUF;
    }
    for (i = 0; i < len; i++) {
        seq_buf[i] = x[from + i];
        acc += (double)seq_buf[i] * (double)seq_buf[i];
    }
    rms = sqrt(acc / (double)len + 1e-8);
    gain = (float)(0.12 / rms);
    for (i = 0; i < len; i++) {
        seq_buf[i] *= gain;
    }
    frames = mel_seq(seq_buf, len, seq_mels);
    if (frames < 20) {
        return;
    }
    cmn(seq_mels, frames);
    {
        double best = 1e9;
        double second = 1e9;
        for (t = 0; t < WAKE_NTEMP; t++) {
            double dist = dtw(seq_mels, frames, wake_tmpl + t * WAKE_MAXF * WAKE_NMEL, wake_tlen[t]);
            if (dist < best) {
                second = best;
                best = dist;
            } else if (dist < second) {
                second = dist;
            }
        }
        *pos = best;
        *neg = second;
    }
}

static void selftest(const char *path) {
    int fd = open(path, O_RDONLY);
    int16_t *pcm;
    float *samples;
    int n;
    int i;
    double score_pos;
    double score_neg;
    if (fd < 0) {
        perror(path);
        return;
    }
    pcm = malloc(160000 * sizeof(int16_t));
    samples = malloc(160000 * sizeof(float));
    if (!pcm || !samples) {
        fprintf(stderr, "nomem\n");
        close(fd);
        free(pcm);
        free(samples);
        return;
    }
    n = (int)read(fd, pcm, 160000 * sizeof(int16_t));
    close(fd);
    n /= (int)sizeof(int16_t);
    for (i = 0; i < n; i++) {
        samples[i] = pcm[i] / 32768.0f;
    }
    score_of(samples, n, &score_pos, &score_neg);
    printf("pos %.6f neg %.6f\n", score_pos, score_neg);
    fflush(stdout);
    free(pcm);
    free(samples);
}

static void follow_ring(void) {
    struct CleanRing *ring = NULL;
    uint32_t read_pos = 0;
    time_t light_until = 0;
    time_t block_until = 0;
    time_t sec_at = 0;
    double sec_pos = 1e9;
    double sec_neg = 0;
    int ready = 0;
    int fired = 0;
    static float window[WAKE_BUF];
    paint_ring(0);
    while (run && !ready) {
        int fd = open("/data/opt/yandex-station/clean.ring", O_RDONLY);
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
        munmap(map, sizeof *ring);
        ring = NULL;
        usleep(200000);
    }
    if (!run || !ring) {
        return;
    }
    __sync_synchronize();
    read_pos = ring->write_pos;
    printf("wake live\n");
    fflush(stdout);
    while (run) {
        uint32_t w;
        uint32_t avail;
        time_t now;
        int i;
        double pos;
        double neg;
        waitpid(-1, NULL, WNOHANG);
        now = time(NULL);
        if (light_until && now >= light_until) {
            light_until = 0;
            paint_ring(0);
            tell_house("led idle\n");
        }
        w = ring->write_pos;
        __sync_synchronize();
        avail = w - read_pos;
        if (avail < (uint32_t)WAKE_BUF) {
            usleep(20000);
            continue;
        }
        if (avail > CLEAN_CAP || avail > (uint32_t)(WAKE_BUF + 16000)) {
            read_pos = w - (uint32_t)WAKE_BUF;
        }
        for (i = 0; i < WAKE_BUF; i++) {
            int16_t s = ring->pcm[(read_pos + (uint32_t)i) & (CLEAN_CAP - 1u)];
            window[i] = s / 32768.0f;
        }
        read_pos += (uint32_t)WAKE_STEP;
        score_of(window, WAKE_BUF, &pos, &neg);
        if (now != sec_at) {
            if (sec_at && !fired && sec_pos < 1.80) {
                printf("near %.2f %.2f\n", sec_pos, sec_neg);
                fflush(stdout);
            }
            sec_at = now;
            sec_pos = 1e9;
            fired = 0;
        }
        if (pos < sec_pos) {
            sec_pos = pos;
            sec_neg = neg;
        }
        if (pos <= (double)wake_fire && neg <= (double)wake_second && now >= block_until) {
            block_until = now + 2;
            light_until = now + 8;
            fired = 1;
            beep();
            paint_ring(1);
            tell_house("led listening\n");
            printf("wake %.2f %.2f\n", pos, neg);
            fflush(stdout);
        }
    }
}

int main(int argc, char **argv) {
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_stop;
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);
    if (!load_models()) {
        fprintf(stderr, "model\n");
        return 1;
    }
    if (argc > 1 && strcmp(argv[1], "demo") == 0) {
        paint_ring(1);
        beep();
        sleep(2);
        paint_ring(0);
        return 0;
    }
    if (argc > 1) {
        selftest(argv[1]);
        return 0;
    }
    follow_ring();
    paint_ring(0);
    return 0;
}
