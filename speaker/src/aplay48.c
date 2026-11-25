/* Подмена aplay: любой сырой S16 отдаётся в динамик на 48000 Гц.
   Такты микрофона и динамика на этой колонке общие. Прошлый звук на
   16000 или 32000 оставлял микрофон на 32000, и слово до модели доходило
   замедленным. Громкость микшера здесь не трогается.
   Здесь же эквалайзер settings/eq.txt: через обёртку идёт весь звук колонки. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#include <sys/wait.h>

#include "play_ring.h"

#define OUT_RATE 48000
#define HOLD_MAX 8192
#define CH_MAX 2

static const char *real_aplay(void) {
    if (access("/usr/bin/aplay.real", X_OK) == 0) {
        return "/usr/bin/aplay.real";
    }
    return "/usr/bin/aplay";
}

static int takes_arg(const char *s) {
    return strcmp(s, "-D") == 0 || strcmp(s, "-d") == 0 || strcmp(s, "-f") == 0
        || strcmp(s, "-c") == 0 || strcmp(s, "-r") == 0 || strcmp(s, "-t") == 0
        || strcmp(s, "-B") == 0 || strcmp(s, "-F") == 0
        || strcmp(s, "--device") == 0 || strcmp(s, "--duration") == 0
        || strcmp(s, "--format") == 0 || strcmp(s, "--channels") == 0
        || strcmp(s, "--rate") == 0 || strcmp(s, "--file-type") == 0;
}

static int is_opt(const char *s) {
    return s[0] == '-' && s[1] != '\0' && strcmp(s, "-") != 0;
}

static const char *opt_val(int argc, char **argv, const char *opt) {
    int i;
    for (i = 1; i < argc - 1; i++) {
        if (strcmp(argv[i], opt) == 0) {
            return argv[i + 1];
        }
    }
    return NULL;
}

static void passthrough(char **argv) {
    const char *real = real_aplay();
    char self[256];
    ssize_t n = readlink("/proc/self/exe", self, sizeof self - 1);
    if (n > 0) {
        self[n] = '\0';
        if (strcmp(self, real) == 0) {
            _exit(127);
        }
    }
    argv[0] = (char *)real;
    execv(real, argv);
    _exit(127);
}

static struct PlayRing *play_ring;

static void open_play_ring(void) {
    int fd;
    struct stat st;
    void *map;
    fd = open("/data/opt/yandex-station/play.ring", O_RDWR | O_CREAT, 0644);
    if (fd < 0) {
        return;
    }
    if (fstat(fd, &st) != 0) {
        close(fd);
        return;
    }
    if (st.st_size != (off_t)sizeof(struct PlayRing)) {
        if (ftruncate(fd, (off_t)sizeof(struct PlayRing)) != 0) {
            close(fd);
            return;
        }
    }
    map = mmap(NULL, sizeof(struct PlayRing), PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    close(fd);
    if (map == MAP_FAILED) {
        return;
    }
    play_ring = map;
    if (play_ring->magic != PLAY_MAGIC || play_ring->capacity != PLAY_CAP) {
        memset(play_ring, 0, sizeof *play_ring);
        play_ring->capacity = PLAY_CAP;
        play_ring->channels = 2;
        __sync_synchronize();
        play_ring->magic = PLAY_MAGIC;
    }
}

static void tee_play(const int16_t *in, int frames, int channels) {
    uint32_t w;
    int i;
    if (!play_ring) {
        open_play_ring();
    }
    if (!play_ring || frames <= 0) {
        return;
    }
    w = play_ring->write_pos;
    for (i = 0; i < frames; i++) {
        int l = in[i * channels];
        int r = channels > 1 ? in[i * channels + 1] : l;
        int m = (l + r) / 2;
        uint32_t at = (w & (PLAY_CAP - 1u)) * 2u;
        play_ring->pcm[at] = (int16_t)m;
        play_ring->pcm[at + 1] = (int16_t)m;
        w++;
    }
    __sync_synchronize();
    play_ring->write_pos = w;
}

#define EQ_N 5
#define EQ_PATH "/data/opt/yandex-station/settings/eq.txt"

/* Полосы как у эквалайзера Станции в приложении: полка 60, пики 230/910/3600, полка 14000. */
static const double eq_freq[EQ_N] = {60.0, 230.0, 910.0, 3600.0, 14000.0};

struct Biquad {
    float b0, b1, b2, a1, a2;
    float z1[CH_MAX], z2[CH_MAX];
};

static struct Biquad eq_bq[EQ_N];
static int eq_on;
static float eq_pre = 1.0f;
static ino_t eq_ino;
static struct timespec eq_mtime;
static int eq_seen;
static int eq_frames;

static void eq_design(struct Biquad *f, int band, double db) {
    double a = pow(10.0, db / 40.0);
    double w = 2.0 * M_PI * eq_freq[band] / OUT_RATE;
    double cw = cos(w);
    double sw = sin(w);
    double b0, b1, b2, a0, a1, a2;
    if (band == 0 || band == EQ_N - 1) {
        double sa = 2.0 * sqrt(a) * (sw / 2.0 * sqrt(2.0));
        double s = band == 0 ? 1.0 : -1.0;
        b0 = a * ((a + 1) - s * (a - 1) * cw + sa);
        b1 = s * 2.0 * a * ((a - 1) - s * (a + 1) * cw);
        b2 = a * ((a + 1) - s * (a - 1) * cw - sa);
        a0 = (a + 1) + s * (a - 1) * cw + sa;
        a1 = -s * 2.0 * ((a - 1) + s * (a + 1) * cw);
        a2 = (a + 1) + s * (a - 1) * cw - sa;
    } else {
        double alpha = sw / 2.0;
        b0 = 1 + alpha * a;
        b1 = -2 * cw;
        b2 = 1 - alpha * a;
        a0 = 1 + alpha / a;
        a1 = -2 * cw;
        a2 = 1 - alpha / a;
    }
    f->b0 = (float)(b0 / a0);
    f->b1 = (float)(b1 / a0);
    f->b2 = (float)(b2 / a0);
    f->a1 = (float)(a1 / a0);
    f->a2 = (float)(a2 / a0);
}

/* Перечитывает eq.txt, если файл сменился. Битый файл оставляет прежнюю настройку. */
static void eq_load(void) {
    struct stat st;
    FILE *fp;
    int g[EQ_N];
    int i;
    int top = 0;
    int any = 0;
    if (stat(EQ_PATH, &st) != 0) {
        eq_on = 0;
        eq_seen = 0;
        return;
    }
    if (eq_seen && st.st_ino == eq_ino && st.st_mtim.tv_sec == eq_mtime.tv_sec
        && st.st_mtim.tv_nsec == eq_mtime.tv_nsec) {
        return;
    }
    fp = fopen(EQ_PATH, "r");
    if (!fp) {
        return;
    }
    i = fscanf(fp, "%d %d %d %d %d", &g[0], &g[1], &g[2], &g[3], &g[4]);
    fclose(fp);
    eq_seen = 1;
    eq_ino = st.st_ino;
    eq_mtime = st.st_mtim;
    if (i != EQ_N) {
        return;
    }
    for (i = 0; i < EQ_N; i++) {
        if (g[i] < -12 || g[i] > 12) {
            return;
        }
        any |= g[i] != 0;
        if (g[i] > top) {
            top = g[i];
        }
    }
    if (!any) {
        eq_on = 0;
        return;
    }
    if (!eq_on) {
        memset(eq_bq, 0, sizeof eq_bq);
    }
    for (i = 0; i < EQ_N; i++) {
        eq_design(&eq_bq[i], i, (double)g[i]);
    }
    /* подъём полосы не должен упираться в потолок int16 */
    eq_pre = (float)pow(10.0, -(double)top / 20.0);
    eq_on = 1;
}

static void eq_apply(int16_t *pcm, int frames, int ch) {
    int f;
    int c;
    int b;
    eq_frames += frames;
    if (eq_frames >= OUT_RATE / 5) {
        eq_frames = 0;
        eq_load();
    }
    if (!eq_on) {
        return;
    }
    for (f = 0; f < frames; f++) {
        for (c = 0; c < ch; c++) {
            float x = (float)pcm[f * ch + c] * eq_pre;
            int s;
            for (b = 0; b < EQ_N; b++) {
                struct Biquad *q = &eq_bq[b];
                float y = q->b0 * x + q->z1[c];
                q->z1[c] = q->b1 * x - q->a1 * y + q->z2[c];
                q->z2[c] = q->b2 * x - q->a2 * y;
                x = y;
            }
            s = (int)lrintf(x);
            pcm[f * ch + c] = (int16_t)(s > 32767 ? 32767 : s < -32768 ? -32768 : s);
        }
    }
    for (b = 0; b < EQ_N; b++) {
        for (c = 0; c < CH_MAX; c++) {
            if (fabsf(eq_bq[b].z1[c]) < 1e-12f) {
                eq_bq[b].z1[c] = 0.0f;
            }
            if (fabsf(eq_bq[b].z2[c]) < 1e-12f) {
                eq_bq[b].z2[c] = 0.0f;
            }
        }
    }
}

static int write_all(int fd, const void *data, size_t n) {
    const char *p = data;
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

struct State {
    int ch;
    double step;
    double pos;
    int frames;
    int16_t hold[HOLD_MAX * CH_MAX];
};

static int emit(struct State *st, int fd) {
    int16_t out[512 * CH_MAX];
    int n = 0;
    while (st->pos + 1.0 < (double)st->frames) {
        int i = (int)st->pos;
        double frac = st->pos - (double)i;
        int c;
        for (c = 0; c < st->ch; c++) {
            int a = st->hold[i * st->ch + c];
            int b = st->hold[(i + 1) * st->ch + c];
            double mixed = (double)a + ((double)b - (double)a) * frac;
            int s = (int)mixed;
            if (s > 32767) {
                s = 32767;
            }
            if (s < -32768) {
                s = -32768;
            }
            out[n * st->ch + c] = (int16_t)s;
        }
        n++;
        st->pos += st->step;
        if (n == 512) {
            eq_apply(out, n, st->ch);
            tee_play(out, n, st->ch);
            if (write_all(fd, out, (size_t)n * (size_t)st->ch * sizeof(int16_t)) != 0) {
                return -1;
            }
            n = 0;
        }
    }
    if (n > 0) {
        eq_apply(out, n, st->ch);
        tee_play(out, n, st->ch);
        if (write_all(fd, out, (size_t)n * (size_t)st->ch * sizeof(int16_t)) != 0) {
            return -1;
        }
    }
    {
        int drop = (int)st->pos;
        if (drop > 0) {
            int left = st->frames - drop;
            if (left > 0) {
                memmove(st->hold, st->hold + drop * st->ch,
                        (size_t)left * (size_t)st->ch * sizeof(int16_t));
            }
            st->frames = left > 0 ? left : 0;
            st->pos -= drop;
        }
    }
    return 0;
}

static int append_and_emit(struct State *st, const int16_t *in, int frames, int fd) {
    if (frames < 0 || st->frames + frames > HOLD_MAX) {
        return -1;
    }
    if (frames > 0) {
        memcpy(st->hold + st->frames * st->ch, in,
               (size_t)frames * (size_t)st->ch * sizeof(int16_t));
        st->frames += frames;
    }
    return emit(st, fd);
}

static int resample_fd(int in_fd, int out_fd, int channels, int in_rate) {
    struct State st;
    int16_t buf[1024 * CH_MAX];
    memset(&st, 0, sizeof st);
    st.ch = channels == 1 ? 2 : channels;
    st.step = (double)in_rate / (double)OUT_RATE;
    eq_load();
    for (;;) {
        size_t want = sizeof buf;
        size_t got = 0;
        ssize_t n;
        while (got < want) {
            n = read(in_fd, (char *)buf + got, want - got);
            if (n < 0) {
                if (errno == EINTR) {
                    continue;
                }
                return -1;
            }
            if (n == 0) {
                break;
            }
            got += (size_t)n;
        }
        {
            int samples = (int)(got / sizeof(int16_t));
            int whole = samples - (samples % channels);
            int frames = whole / channels;
            if (frames > 0) {
                const int16_t *pcm = buf;
                int16_t stereo[2048 * 2];
                if (channels == 1) {
                    int f;
                    if (frames > 2048) {
                        frames = 2048;
                    }
                    for (f = 0; f < frames; f++) {
                        stereo[f * 2] = buf[f];
                        stereo[f * 2 + 1] = buf[f];
                    }
                    pcm = stereo;
                }
                if (append_and_emit(&st, pcm, frames, out_fd) != 0) {
                    return errno == EPIPE ? 0 : -1;
                }
            }
        }
        if (got < want) {
            return 0;
        }
    }
}

static int run_resample(int argc, char **argv, int channels, int in_rate, const char *path) {
    const char *real = real_aplay();
    char *neu[64];
    int n = 0;
    int i;
    int seen = 0;
    int in_fd = 0;
    int pipes[2];
    pid_t pid;
    int status;
    if (argc >= 60) {
        passthrough(argv);
    }
    neu[n++] = (char *)real;
    for (i = 1; i < argc; i++) {
        if ((strcmp(argv[i], "-r") == 0 || strcmp(argv[i], "--rate") == 0) && i + 1 < argc) {
            neu[n++] = argv[i];
            neu[n++] = "48000";
            i++;
            continue;
        }
        /* Мини 2 кормит стереокодек парами отсчётов. Моно без дубля
           канала звучит вдвое быстрее и выше. */
        if ((strcmp(argv[i], "-c") == 0 || strcmp(argv[i], "--channels") == 0) && i + 1 < argc) {
            neu[n++] = argv[i];
            neu[n++] = channels == 1 ? "2" : argv[i + 1];
            i++;
            continue;
        }
        if (!is_opt(argv[i]) && !seen) {
            neu[n++] = "-";
            seen = 1;
            continue;
        }
        if (takes_arg(argv[i]) && i + 1 < argc) {
            neu[n++] = argv[i];
            neu[n++] = argv[i + 1];
            i++;
            continue;
        }
        neu[n++] = argv[i];
    }
    if (channels == 1 && opt_val(argc, argv, "-c") == NULL && opt_val(argc, argv, "--channels") == NULL) {
        neu[n++] = "-c";
        neu[n++] = "2";
    }
    if (n < 58 && opt_val(argc, argv, "-B") == NULL) {
        neu[n++] = "-B";
        neu[n++] = "80000";
        neu[n++] = "-F";
        neu[n++] = "20000";
    }
    neu[n] = NULL;
    if (path != NULL && strcmp(path, "-") != 0) {
        in_fd = open(path, O_RDONLY);
        if (in_fd < 0) {
            return 1;
        }
    }
    if (pipe(pipes) != 0) {
        if (in_fd > 0) {
            close(in_fd);
        }
        return 1;
    }
#ifdef F_SETPIPE_SZ
    if (fcntl(pipes[0], F_SETPIPE_SZ, 4096) < 0) {
        /* короткая очередь держит опору рядом со звуком из динамика */
        (void)0;
    }
#endif
    pid = fork();
    if (pid < 0) {
        if (in_fd > 0) {
            close(in_fd);
        }
        close(pipes[0]);
        close(pipes[1]);
        return 1;
    }
    if (pid == 0) {
        if (dup2(pipes[0], STDIN_FILENO) < 0) {
            _exit(127);
        }
        close(pipes[0]);
        close(pipes[1]);
        if (in_fd > 0) {
            close(in_fd);
        }
        execv(real, neu);
        _exit(127);
    }
    close(pipes[0]);
    signal(SIGPIPE, SIG_IGN);
    if (resample_fd(in_fd, pipes[1], channels, in_rate) != 0) {
        close(pipes[1]);
        if (in_fd > 0) {
            close(in_fd);
        }
        waitpid(pid, &status, 0);
        return 1;
    }
    close(pipes[1]);
    if (in_fd > 0) {
        close(in_fd);
    }
    if (waitpid(pid, &status, 0) < 0) {
        return 1;
    }
    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    return 1;
}

int main(int argc, char **argv) {
    const char *rate_s;
    const char *fmt;
    const char *kind;
    const char *ch_s;
    const char *path = NULL;
    int files = 0;
    int rate;
    int channels;
    int i;
    int skip = 0;
    if (argc < 2) {
        passthrough(argv);
    }
    for (i = 1; i < argc; i++) {
        if (skip) {
            skip = 0;
            continue;
        }
        if (takes_arg(argv[i])) {
            skip = 1;
            continue;
        }
        if (!is_opt(argv[i])) {
            files++;
            path = argv[i];
        }
    }
    rate_s = opt_val(argc, argv, "-r");
    if (rate_s == NULL) {
        rate_s = opt_val(argc, argv, "--rate");
    }
    fmt = opt_val(argc, argv, "-f");
    if (fmt == NULL) {
        fmt = opt_val(argc, argv, "--format");
    }
    kind = opt_val(argc, argv, "-t");
    if (kind == NULL) {
        kind = opt_val(argc, argv, "--file-type");
    }
    ch_s = opt_val(argc, argv, "-c");
    if (ch_s == NULL) {
        ch_s = opt_val(argc, argv, "--channels");
    }
    if (files > 1 || rate_s == NULL || fmt == NULL || kind == NULL || strcmp(fmt, "S16_LE") != 0
        || strcmp(kind, "raw") != 0) {
        passthrough(argv);
    }
    rate = atoi(rate_s);
    channels = ch_s != NULL ? atoi(ch_s) : 1;
    if (rate < 8000 || rate > 192000 || channels < 1 || channels > CH_MAX) {
        passthrough(argv);
    }
    return run_resample(argc, argv, channels, rate, path);
}
