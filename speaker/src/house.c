#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <dlfcn.h>
#include <math.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "clean_ring.h"
#include "host_conf.h"
#include "mini2_led.h"
#include "play_ring.h"

#define CHANNELS 16
#define RATE 48000
#define WINDOW (RATE / 5)

/* Спальня: hw:0,2 и 16 каналов, пресет yandexmidi. Мини 2 пишет свои значения
   в settings/capture.txt («hw:0,1 8») и settings/vqe.txt. Без файлов — спальня. */
static char cap_dev[32] = "hw:0,2";
static int cap_ch = CHANNELS;
static char vqe_name[32] = "yandexmidi";

static void load_board(void) {
    FILE *file;
    char line[80];
    file = fopen("/data/opt/yandex-station/settings/vqe.txt", "r");
    if (file) {
        if (fgets(line, sizeof line, file)) {
            size_t n = strcspn(line, "\r\n");
            line[n] = 0;
            if (n > 0 && n < sizeof vqe_name &&
                strspn(line, "abcdefghijklmnopqrstuvwxyz0123456789_") == n) {
                snprintf(vqe_name, sizeof vqe_name, "%s", line);
            }
        }
        fclose(file);
    }
    file = fopen("/data/opt/yandex-station/settings/capture.txt", "r");
    if (file) {
        if (fgets(line, sizeof line, file)) {
            char dev[32];
            int ch = 0;
            if (sscanf(line, "%31s %d", dev, &ch) == 2 && strncmp(dev, "hw:", 3) == 0 &&
                ch >= 1 && ch <= CHANNELS) {
                snprintf(cap_dev, sizeof cap_dev, "%s", dev);
                cap_ch = ch;
            }
        }
        fclose(file);
    }
}

static volatile sig_atomic_t run = 1;

static void on_stop(int signo) {
    (void)signo;
    run = 0;
}

static int volume = -1;
static int light = -1;
static const char light_path[] =
    "/sys/devices/platform/ffd13000.spicc_a/spi_master/spi0/spi0.0/backlight/fb0/brightness";
static const char cmd_path[] = "/data/opt/yandex-station/house.cmd";
static int ring_fd = -1;

static void paint_ring(int on) {
    unsigned char buf[89 * 4];
    if (mini2_present()) {
        mini2_show(on ? 255 : 0, on ? 255 : 0, on ? 255 : 0);
        return;
    }
    if (ring_fd >= 0) {
        close(ring_fd);
        ring_fd = -1;
    }
    ring_fd = open("/dev/fb0", O_RDWR | O_CLOEXEC);
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
static int vqe_mics = -1;
static int vqe_speakers = -1;
static int vqe_in = -1;
static int vqe_out = -1;
static int vqe_chunk = -1;
static int vqe_level = -1;
static int vqe_fill;
static void *vqe_eng;
static float *vqe_mic;
static float *vqe_spk;
static int (*vqe_process)(void *eng, void *mic, int mic_n, void *spk, int spk_n);
static int (*vqe_output)(void *eng, int type, int channel, void **data, int *count);
static struct CleanRing *clean;
static int hold_clean;
/* Какой выход шумодава идёт в clean.ring: settings/vqe.out «тип канал».
   На Мини 2 тип 6 музыку из микрофона не убирает, тип 8 убирает.
   Поэтому на Мини 2 в файле стоит «8 0», а не этот запасной номер. */
static int vqe_type = 6;
static int vqe_chan;
static unsigned long vqe_calls;

#define DUMP_MAX 6
static int dump_n;
static int dump_type[DUMP_MAX];
static int dump_chan[DUMP_MAX];
static struct CleanRing *dump_ring[DUMP_MAX];
static void load_vqe_choice(void);

#define MIC_CHUNK 320

static int mic_sock = -1;
static int mic_ok;
static uint32_t mic_seq;
static int16_t mic_pkt[MIC_CHUNK];
static int mic_fill;
static int mic_part;
static int mic_acc;
static float mic_prev;
static struct CleanRing *mic_ring;
/* Ровно то, что уходит серверному слову: preemph микрофона или AEC при звуке. Читает jarvisd. */
static struct CleanRing *wake_ring;
static struct PlayRing *play;
static uint32_t play_read;
static uint32_t play_mark;
static int play_left;
static int play_delay = 5376;
static int ref_level;
static uint64_t play_acc;
static int play_n;
static int play_hot;
static int wake_aec;
static int vol_gain = 255;
static int vol_wait;
static int read_volume(void);
static int read_int_file(const char *path);
static int wake_logged = -1;

#define CLEAN_Q 4096
static int16_t clean_q[CLEAN_Q];
static uint32_t clean_w;
static uint32_t clean_r;
static float aec_prev;

static void open_play(void) {
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
    play = map;
    if (play->magic != PLAY_MAGIC || play->capacity != PLAY_CAP) {
        memset(play, 0, sizeof *play);
        play->capacity = PLAY_CAP;
        play->channels = 2;
        __sync_synchronize();
        play->magic = PLAY_MAGIC;
    }
    play_read = play->write_pos;
    play_mark = play->write_pos;
}

static void refresh_delay(void) {
    FILE *fp;
    char line[80];
    fp = fopen("/proc/asound/card0/pcm0p/sub0/status", "r");
    if (!fp) {
        return;
    }
    while (fgets(line, sizeof line, fp)) {
        char *mark;
        int delay;
        if (strncmp(line, "delay", 5) != 0) {
            continue;
        }
        mark = strchr(line, ':');
        if (!mark) {
            continue;
        }
        delay = atoi(mark + 1);
        if (delay >= 480 && delay <= 24000) {
            play_delay = delay;
        }
    }
    fclose(fp);
    {
        FILE *lagf = fopen("/data/opt/yandex-station/settings/aec.lag", "r");
        if (lagf) {
            int forced = -1;
            if (fscanf(lagf, "%d", &forced) == 1 && forced >= 0 && forced <= 24000) {
                play_delay = forced;
            }
            fclose(lagf);
        }
    }
}

static void read_play(int16_t *left, int16_t *right) {
    uint32_t w;
    uint32_t lag;
    uint32_t want;
    *left = 0;
    *right = 0;
    if (!play || play->magic != PLAY_MAGIC) {
        return;
    }
    w = play->write_pos;
    if (w != play_mark) {
        play_mark = w;
        play_left = RATE;
    } else if (play_left > 0) {
        play_left--;
    }
    if (play_left <= 0 && w == play_read) {
        return;
    }
    want = (uint32_t)play_delay;
    lag = w - play_read;
    if (lag > want + 4000u && w > want) {
        play_read = w - want;
    }
    if (w == play_read) {
        return;
    }
    {
        uint32_t at = (play_read & (PLAY_CAP - 1u)) * 2u;
        *left = play->pcm[at];
        *right = play->pcm[at + 1];
        play_read++;
    }
}

static void clean_push(int16_t sample) {
    if (clean_w - clean_r >= CLEAN_Q - 1u) {
        clean_r++;
    }
    clean_q[clean_w & (CLEAN_Q - 1u)] = sample;
    clean_w++;
}

static int clean_pop(int16_t *sample) {
    if (clean_r == clean_w) {
        return 0;
    }
    *sample = clean_q[clean_r & (CLEAN_Q - 1u)];
    clean_r++;
    return 1;
}

static void open_mic_send(void) {
    struct sockaddr_in addr;
    int flags;
    mic_sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (mic_sock < 0) {
        fprintf(stderr, "mic socket: %s\n", strerror(errno));
        return;
    }
    flags = fcntl(mic_sock, F_GETFL, 0);
    if (flags >= 0) {
        fcntl(mic_sock, F_SETFL, flags | O_NONBLOCK);
    }
    struct yse_host conf;
    yse_host_load(&conf);
    memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET;
    addr.sin_port = htons((uint16_t)conf.base);
    if (inet_pton(AF_INET, conf.ip, &addr.sin_addr) != 1) {
        close(mic_sock);
        mic_sock = -1;
        return;
    }
    if (connect(mic_sock, (struct sockaddr *)&addr, sizeof addr) != 0) {
        fprintf(stderr, "mic connect: %s\n", strerror(errno));
        close(mic_sock);
        mic_sock = -1;
        return;
    }
    mic_ok = 1;
    fprintf(stderr, "mic send %s:%d ch2\n", conf.ip, conf.base);
    fflush(stderr);
}

static void push_mic_sample(int16_t sample) {
    float x;
    float y;
    int s;
    unsigned char buf[4 + MIC_CHUNK * 2];
    mic_acc += sample;
    mic_part++;
    if (mic_part < 3) {
        return;
    }
    x = (float)mic_acc / 3.0f;
    y = x - 0.97f * mic_prev;
    mic_prev = x;
    mic_acc = 0;
    mic_part = 0;
    s = (int)(x >= 0.0f ? x + 0.5f : x - 0.5f);
    if (s > 32767) {
        s = 32767;
    } else if (s < -32768) {
        s = -32768;
    }
    {
        int16_t clean_s = 0;
        int use_clean = wake_aec && clean_pop(&clean_s);
        if (mic_ring) {
            uint32_t w = mic_ring->write_pos;
            mic_ring->pcm[w & (CLEAN_CAP - 1u)] = (int16_t)s;
            __sync_synchronize();
            mic_ring->write_pos = w + 1;
        }
        if (use_clean) {
            float cy = (float)clean_s - 0.97f * aec_prev;
            aec_prev = (float)clean_s;
            s = (int)(cy >= 0.0f ? cy + 0.5f : cy - 0.5f);
        } else {
            s = (int)(y >= 0.0f ? y + 0.5f : y - 0.5f);
        }
    }
    if (s > 32767) {
        s = 32767;
    } else if (s < -32768) {
        s = -32768;
    }
    if (wake_ring) {
        uint32_t w = wake_ring->write_pos;
        wake_ring->pcm[w & (CLEAN_CAP - 1u)] = (int16_t)s;
        __sync_synchronize();
        wake_ring->write_pos = w + 1;
    }
    mic_pkt[mic_fill++] = (int16_t)s;
    if (mic_fill < MIC_CHUNK || mic_sock < 0) {
        if (mic_fill >= MIC_CHUNK) {
            mic_fill = 0;
            mic_seq++;
        }
        return;
    }
    memcpy(buf, &mic_seq, 4);
    mic_seq++;
    memcpy(buf + 4, mic_pkt, sizeof mic_pkt);
    mic_fill = 0;
    if (send(mic_sock, buf, sizeof buf, MSG_DONTWAIT) < 0) {
        /* пакет можно потерять, микрофон не останавливаем */
    }
}

static void push_clean(const float *samples, int count);

static uint32_t isqrt(uint32_t x) {
    uint32_t bit = 1u << 30;
    uint32_t root = 0;
    while (bit > x) {
        bit >>= 2;
    }
    while (bit) {
        if (x >= root + bit) {
            x -= root + bit;
            root = (root >> 1) + bit;
        } else {
            root >>= 1;
        }
        bit >>= 2;
    }
    return root;
}

static int read_temp(const char *path, int *value, time_t *stamp) {
    struct stat st;
    FILE *file;
    double v;
    int ok = 0;
    if (stat(path, &st) != 0) {
        return 0;
    }
    file = fopen(path, "r");
    if (!file) {
        return 0;
    }
    if (fscanf(file, "%lf", &v) == 1 && v > -100 && v < 100) {
        *value = (int)(v >= 0 ? v + 0.5 : v - 0.5);
        ok = 1;
    }
    fclose(file);
    *stamp = st.st_mtime;
    return ok;
}

/* temp.now — температура комнаты, сервер обновляет раз в 5 минут: 300 с часы, 30 с она.
   temp.show — ответ на вопрос о температуре: сразу и на 30 с. */
static int face_temp(int *value) {
    static time_t show_seen;
    static time_t show_until;
    static int show_value;
    time_t now = time(NULL);
    time_t stamp = 0;
    int v;
    if (read_temp("/data/opt/yandex-station/temp.show", &v, &stamp) && stamp != show_seen) {
        show_seen = stamp;
        if (now - stamp < 30) {
            show_until = now + 30;
            show_value = v;
        }
    }
    if (now < show_until) {
        *value = show_value;
        return 1;
    }
    if (now % 330 < 300) {
        return 0;
    }
    if (!read_temp("/data/opt/yandex-station/temp.now", &v, &stamp) || now - stamp > 1800) {
        return 0;
    }
    *value = v;
    return 1;
}

/* timer.txt — срок. Пока он впереди, на циферблате обратный отсчёт.
   timer.ring — время вышло, на циферблате ноль, пока писк не остановят. */
static int timer_show(int *left, int *right, int *colon, int *bright) {
    FILE *fp;
    long long at = 0;
    time_t now = time(NULL);
    long long left_s;
    if (access("/tmp/timer.ring", F_OK) == 0) {
        *left = 0;
        *right = 0;
        *colon = (now % 2) == 0;
        *bright = 170;
        return 1;
    }
    fp = fopen("/data/opt/yandex-station/timer.txt", "r");
    if (!fp) {
        return 0;
    }
    if (fscanf(fp, "%lld", &at) != 1) {
        fclose(fp);
        return 0;
    }
    fclose(fp);
    left_s = at - (long long)now;
    if (left_s < 0) {
        left_s = 0;
    }
    if (left_s >= 100 * 60) {
        *left = (int)(left_s / 3600);
        *right = (int)((left_s / 60) % 60);
    } else {
        *left = (int)(left_s / 60);
        *right = (int)(left_s % 60);
    }
    *colon = (now % 2) == 0;
    *bright = 170;
    return 1;
}

static void mini2_face(void) {
    time_t now;
    struct tm parts;
    static int shown_sec = -1;
    static int shown_light = -2;
    static int shown_bright = -1;
    static int clock_off = 3;
    static int clock_off_read = 0;
    int bright;
    int colon;
    int idle_bright = 50;
    int file_bright = 0;
    FILE *file;
    int off;
    if (!mini2_present()) {
        return;
    }
    if (!clock_off_read) {
        clock_off_read = 1;
        file = fopen("/data/opt/yandex-station/settings/clock.off", "r");
        if (file) {
            if (fscanf(file, "%d", &off) == 1 && off >= -12 && off <= 14) {
                clock_off = off;
            }
            fclose(file);
        }
    }
    file = fopen("/data/opt/yandex-station/settings/ring.bright", "r");
    if (file) {
        if (fscanf(file, "%d", &file_bright) == 1 && file_bright >= 0 && file_bright <= 255) {
            idle_bright = file_bright * 170 / 255;
        }
        fclose(file);
    }
    now = time(NULL) + (time_t)clock_off * 3600;
    if (!gmtime_r(&now, &parts)) {
        return;
    }
    if (light > 0) {
        bright = 170;
        colon = 1;
    } else {
        bright = idle_bright;
        colon = (parts.tm_sec % 2) == 0;
    }
    if (parts.tm_sec == shown_sec && light == shown_light && bright == shown_bright) {
        return;
    }
    shown_sec = parts.tm_sec;
    shown_light = light;
    shown_bright = bright;
    {
        int t_left = 0;
        int t_right = 0;
        int t_colon = 0;
        int t_bright = 0;
        if (timer_show(&t_left, &t_right, &t_colon, &t_bright)) {
            mini2_digits(t_left, t_right, t_colon, t_bright);
            return;
        }
    }
    if (light <= 0 && face_temp(&off)) {
        mini2_temp(off, bright);
        return;
    }
    mini2_clock(parts.tm_hour, parts.tm_min, colon, bright);
}

static void set_led(const char *name) {
    if (strcmp(name, "listening") == 0) {
        paint_ring(1);
        if (mini2_present()) {
            light = 170;
            mini2_face();
        }
    } else if (strcmp(name, "idle") == 0) {
        paint_ring(0);
        if (mini2_present()) {
            light = 0;
            mini2_face();
        }
    }
}

static void open_vqe(void) {
    void *lib;
    void *cfg = 0;
    int (*create)(const char *, void **);
    int (*mics)(void *);
    int (*speakers)(void *);
    int (*in_rate)(void *);
    int (*out_rate)(void *);
    int (*chunk)(void *);
    int rc;
    lib = dlopen("/system/vendor/quasar/libs/libyandex_evil.so", RTLD_NOW);
    if (!lib) {
        fprintf(stderr, "vqe open: %s\n", dlerror());
        return;
    }
    create = dlsym(lib, "YandexVqeC_Config_CreateFromPreset");
    mics = dlsym(lib, "YandexVqeC_Config_GetMicsCount");
    speakers = dlsym(lib, "YandexVqeC_Config_GetSpeakersCount");
    in_rate = dlsym(lib, "YandexVqeC_Config_GetInputSamplingRate");
    out_rate = dlsym(lib, "YandexVqeC_Config_GetOutputSamplingRate");
    chunk = dlsym(lib, "YandexVqeC_Config_GetInputChunkSize");
    if (!create || !mics || !speakers || !in_rate || !out_rate || !chunk) {
        return;
    }
    rc = create(vqe_name, &cfg);
    if (rc != 0 || !cfg) {
        fprintf(stderr, "vqe preset %s rc=%d\n", vqe_name, rc);
        return;
    }
    vqe_mics = mics(cfg);
    vqe_speakers = speakers(cfg);
    vqe_in = in_rate(cfg);
    vqe_out = out_rate(cfg);
    vqe_chunk = chunk(cfg);
    {
        int (*make)(void *, const char *, int, void *, void **);
        void *eng = 0;
        int mrc;
        make = dlsym(lib, "YandexVqeC_Create");
        vqe_process = dlsym(lib, "YandexVqeC_Process");
        vqe_output = dlsym(lib, "YandexVqeC_GetOutputChannelData");
        if (!make || !vqe_process || !vqe_output) {
            fprintf(stderr, "vqe symbols missing\n");
            return;
        }
        if (vqe_mics < 1 || vqe_mics > 8 || vqe_speakers < 0 || vqe_speakers > 8 || vqe_chunk < 1 || vqe_chunk > 8192) {
            return;
        }
        mrc = make(cfg, "/system/vendor/quasar/vqe_models", 0, 0, &eng);
        if (mrc != 0 || !eng) {
            fprintf(stderr, "vqe engine rc=%d\n", mrc);
            return;
        }
        vqe_mic = calloc((size_t)vqe_mics * (size_t)vqe_chunk, sizeof(float));
        vqe_spk = calloc((size_t)(vqe_speakers > 0 ? vqe_speakers : 1) * (size_t)vqe_chunk, sizeof(float));
        if (!vqe_mic || !vqe_spk) {
            return;
        }
        vqe_eng = eng;
    }
    load_vqe_choice();
}

static struct CleanRing *map_ring(const char *path) {
    struct CleanRing *ring;
    void *map;
    int fd = open(path, O_RDWR | O_CREAT, 0644);
    if (fd < 0) {
        return NULL;
    }
    if (ftruncate(fd, (off_t)sizeof(struct CleanRing)) != 0) {
        close(fd);
        return NULL;
    }
    map = mmap(NULL, sizeof(struct CleanRing), PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    close(fd);
    if (map == MAP_FAILED) {
        return NULL;
    }
    ring = map;
    memset(ring, 0, sizeof *ring);
    ring->capacity = CLEAN_CAP;
    __sync_synchronize();
    ring->magic = CLEAN_MAGIC;
    return ring;
}

/* vqe.dump: пары «тип канал» через пробел. Эти выходы пишутся в /tmp/vqe-T-C.ring,
   ухо сохраняет из них то же окно, что и фразу, — чтобы сравнить очистку. */
static void load_vqe_choice(void) {
    FILE *file = fopen("/data/opt/yandex-station/settings/vqe.out", "r");
    int t;
    int c;
    if (file) {
        if (fscanf(file, "%d %d", &t, &c) == 2 && t >= 0 && t < 32 && c >= 0 && c < 8) {
            vqe_type = t;
            vqe_chan = c;
        }
        fclose(file);
    }
    fprintf(stderr, "vqe out type %d ch %d\n", vqe_type, vqe_chan);
    file = fopen("/data/opt/yandex-station/settings/vqe.dump", "r");
    if (!file) {
        return;
    }
    while (dump_n < DUMP_MAX && fscanf(file, "%d %d", &t, &c) == 2) {
        char path[64];
        if (t < 0 || t >= 32 || c < 0 || c >= 8) {
            continue;
        }
        snprintf(path, sizeof path, "/tmp/vqe-%d-%d.ring", t, c);
        dump_ring[dump_n] = map_ring(path);
        if (dump_ring[dump_n]) {
            dump_type[dump_n] = t;
            dump_chan[dump_n] = c;
            dump_n++;
        }
    }
    fclose(file);
    fprintf(stderr, "vqe dump %d outputs\n", dump_n);
}

/* Номера выходов в библиотеке не подписаны. Перебор в копии процесса:
   чужой номер может уронить её, но не housed. */
static void probe_outputs(void) {
    pid_t pid = fork();
    if (pid < 0) {
        return;
    }
    if (pid > 0) {
        waitpid(pid, NULL, 0);
        return;
    }
    if (fork() != 0) {
        _exit(0);
    }
    alarm(10);
    {
        int t;
        int c;
        for (t = 0; t < 16; t++) {
            for (c = 0; c < 6; c++) {
                void *data = 0;
                int count = 0;
                int rc = vqe_output(vqe_eng, t, c, &data, &count);
                double acc = 0;
                int k;
                if (rc != 0 || !data || count <= 0 || count > 8192) {
                    if (c == 0) {
                        fprintf(stderr, "vqe probe type %d rc %d\n", t, rc);
                    }
                    break;
                }
                for (k = 0; k < count; k++) {
                    float v = ((float *)data)[k];
                    acc += (double)v * (double)v;
                }
                fprintf(stderr, "vqe probe type %d ch %d count %d rms %.1f\n", t, c, count,
                        sqrt(acc / count) * 32768.0);
            }
        }
    }
    fflush(stderr);
    _exit(0);
}

static void dump_outputs(void) {
    int d;
    for (d = 0; d < dump_n; d++) {
        void *data = 0;
        int count = 0;
        struct CleanRing *ring = dump_ring[d];
        uint32_t w;
        int i;
        if (vqe_output(vqe_eng, dump_type[d], dump_chan[d], &data, &count) != 0 || !data ||
            count <= 0 || count > 8192) {
            continue;
        }
        w = ring->write_pos;
        for (i = 0; i < count; i++) {
            float x = ((float *)data)[i] * 32768.0f;
            int s = x > 32767.0f ? 32767 : (x < -32768.0f ? -32768 : (int)x);
            ring->pcm[(w + (uint32_t)i) & (CLEAN_CAP - 1u)] = (int16_t)s;
        }
        __sync_synchronize();
        ring->write_pos = w + (uint32_t)count;
    }
}

static void scale_ref(int16_t *left, int16_t *right) {
    int got;
    /* Кольцо хранит звук до ручки громкости. Эхо в микрофоне — уже после неё. */
    if (play_left <= 0 && play_hot <= 0) {
        return;
    }
    if (vol_wait > 0) {
        vol_wait--;
    } else {
        vol_wait = RATE * 2;
        got = read_volume();
        if (got > 0 && got <= 255) {
            vol_gain = got;
        }
    }
    *left = (int16_t)((int)*left * vol_gain / 255);
    *right = (int16_t)((int)*right * vol_gain / 255);
}

static void feed_vqe(const int16_t *frame) {
    int c;
    int16_t left = 0;
    int16_t right = 0;
    read_play(&left, &right);
    scale_ref(&left, &right);
    if (play_hot > 0) {
        play_hot--;
    }
    wake_aec = play_hot > 0 || play_left > 0;
    if (!wake_aec) {
        clean_w = 0;
        clean_r = 0;
    }
    if (!vqe_eng) {
        return;
    }
    for (c = 0; c < vqe_mics && c < CHANNELS; c++) {
        vqe_mic[(size_t)vqe_fill * (size_t)vqe_mics + (size_t)c] = (float)frame[c] / 32768.0f;
    }
    if (vqe_speakers >= 1) {
        vqe_spk[vqe_fill] = (float)left / 32768.0f;
    }
    if (vqe_speakers >= 2) {
        vqe_spk[vqe_chunk + vqe_fill] = (float)right / 32768.0f;
    }
    play_acc += (uint64_t)((int32_t)left * (int32_t)left + (int32_t)right * (int32_t)right);
    play_n++;
    vqe_fill++;
    if (vqe_fill < vqe_chunk) {
        return;
    }
    vqe_fill = 0;
    if (play_n > 0) {
        ref_level = (int)isqrt((uint32_t)(play_acc / (uint64_t)play_n));
        if (play_acc / (uint64_t)play_n > 12800ull) {
            play_hot = RATE;
        }
    }
    play_acc = 0;
    play_n = 0;
    wake_aec = play_hot > 0 || play_left > 0;
    if (wake_logged != wake_aec) {
        wake_logged = wake_aec;
        fprintf(stderr, "wake %s\n", wake_aec ? "aec" : "raw");
        fflush(stderr);
    }
    if (vqe_process(vqe_eng, vqe_mic, vqe_mics * vqe_chunk, vqe_speakers ? vqe_spk : 0, vqe_speakers * vqe_chunk) != 0) {
        return;
    }
    {
        void *data = 0;
        int count = 0;
        double acc = 0;
        float *samples;
        int k;
        uint32_t mean;
        vqe_calls++;
        if (vqe_calls == 300) {
            probe_outputs();
        }
        dump_outputs();
        if (vqe_output(vqe_eng, vqe_type, vqe_chan, &data, &count) != 0 || !data || count <= 0 || count > 8192) {
            return;
        }
        samples = data;
        if (wake_aec) {
            int n = count > 1024 ? 1024 : count;
            for (k = 0; k < n; k++) {
                float x = samples[k] * 32768.0f;
                int s;
                if (x > 32767.0f) {
                    s = 32767;
                } else if (x < -32768.0f) {
                    s = -32768;
                } else {
                    s = (int)x;
                }
                clean_push((int16_t)s);
            }
        }
        for (k = 0; k < count; k++) {
            acc += (double)samples[k] * (double)samples[k];
        }
        acc = acc / count * 32768.0 * 32768.0;
        mean = acc > 4294967295.0 ? 4294967295u : (uint32_t)acc;
        vqe_level = (int)isqrt(mean);
        push_clean(samples, count);
    }
}

static struct CleanRing *open_ring(const char *path) {
    int fd;
    void *map;
    struct CleanRing *ring;
    fd = open(path, O_RDWR | O_CREAT, 0644);
    if (fd < 0) {
        fprintf(stderr, "%s open: %s\n", path, strerror(errno));
        return NULL;
    }
    if (ftruncate(fd, (off_t)sizeof(struct CleanRing)) != 0) {
        fprintf(stderr, "%s size: %s\n", path, strerror(errno));
        close(fd);
        return NULL;
    }
    map = mmap(NULL, sizeof(struct CleanRing), PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    close(fd);
    if (map == MAP_FAILED) {
        fprintf(stderr, "%s map: %s\n", path, strerror(errno));
        return NULL;
    }
    ring = map;
    memset(ring, 0, sizeof *ring);
    ring->capacity = CLEAN_CAP;
    __sync_synchronize();
    ring->magic = CLEAN_MAGIC;
    return ring;
}

static void open_mic_ring(void) {
    mic_ring = open_ring("/data/opt/yandex-station/mic.ring");
    wake_ring = open_ring("/data/opt/yandex-station/wake.ring");
}

/* Мини 2: PDM Gain по умолчанию 0, голос с метра — единицы отсчётов. settings/pdm_gain.txt, 0..48. */
static void apply_pdm_gain(void) {
    char cmd[96];
    int gain = read_int_file("/data/opt/yandex-station/settings/pdm_gain.txt");
    if (gain < 0 || gain > 48) {
        return;
    }
    snprintf(cmd, sizeof cmd, "amixer -c0 cset name='PDM Gain' %d >/dev/null 2>&1", gain);
    fprintf(stderr, "pdm gain %d %s\n", gain, system(cmd) == 0 ? "ok" : "fail");
}

static void open_clean(void) {
    int fd;
    void *map;
    fd = open("/data/opt/yandex-station/clean.ring", O_RDWR | O_CREAT, 0644);
    if (fd < 0) {
        fprintf(stderr, "ring open: %s\n", strerror(errno));
        return;
    }
    if (ftruncate(fd, (off_t)sizeof(struct CleanRing)) != 0) {
        fprintf(stderr, "ring size: %s\n", strerror(errno));
        close(fd);
        return;
    }
    map = mmap(NULL, sizeof(struct CleanRing), PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    close(fd);
    if (map == MAP_FAILED) {
        fprintf(stderr, "ring map: %s\n", strerror(errno));
        return;
    }
    clean = map;
    memset(clean, 0, sizeof *clean);
    clean->capacity = CLEAN_CAP;
    __sync_synchronize();
    clean->magic = CLEAN_MAGIC;
}

static void push_clean(const float *samples, int count) {
    uint32_t w;
    int i;
    if (!clean || hold_clean || count <= 0 || count > 8192) {
        return;
    }
    w = clean->write_pos;
    for (i = 0; i < count; i++) {
        float x = samples[i] * 32768.0f;
        int s;
        if (x > 32767.0f) {
            s = 32767;
        } else if (x < -32768.0f) {
            s = -32768;
        } else {
            s = (int)x;
        }
        clean->pcm[(w + (uint32_t)i) & (CLEAN_CAP - 1u)] = (int16_t)s;
    }
    __sync_synchronize();
    clean->write_pos = w + (uint32_t)count;
}

static int read_int_file(const char *path) {
    char buf[32];
    int fd;
    ssize_t n;
    fd = open(path, O_RDONLY);
    if (fd < 0) {
        return -1;
    }
    n = read(fd, buf, sizeof buf - 1);
    close(fd);
    if (n <= 0) {
        return -1;
    }
    buf[n] = 0;
    return atoi(buf);
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
            break;
        }
    }
    pclose(fp);
    return value;
}

static void apply_cmd(void) {
    char line[80];
    char cmd[80];
    int fd;
    ssize_t n;
    fd = open(cmd_path, O_RDONLY);
    if (fd < 0) {
        return;
    }
    n = read(fd, line, sizeof line - 1);
    close(fd);
    unlink(cmd_path);
    if (n <= 0) {
        return;
    }
    line[n] = 0;
    if (strncmp(line, "volume ", 7) == 0) {
        int v = atoi(line + 7);
        int rc;
        if (v < 0 || v > 255) {
            return;
        }
        snprintf(cmd, sizeof cmd, "amixer -c 0 set Master %d >/dev/null 2>&1", v);
        rc = system(cmd);
        if (rc == 0) {
            volume = v;
        }
    } else if (strncmp(line, "light ", 6) == 0) {
        int v = atoi(line + 6);
        char buf[16];
        int wr;
        int out;
        if (v < 0 || v > 255) {
            return;
        }
        wr = snprintf(buf, sizeof buf, "%d", v);
        out = open(light_path, O_WRONLY);
        if (out >= 0 && wr > 0 && write(out, buf, (size_t)wr) == wr) {
            light = v;
        }
        if (out >= 0) {
            close(out);
        }
        if (mini2_present()) {
            light = v;
            mini2_face();
        }
    } else if (strncmp(line, "hold", 4) == 0 && (line[4] == 0 || line[4] == '\n' || line[4] == '\r')) {
        hold_clean = 1;
    } else if (strncmp(line, "resume", 6) == 0 && (line[6] == 0 || line[6] == '\n' || line[6] == '\r')) {
        hold_clean = 0;
    } else if (strncmp(line, "led ", 4) == 0) {
        char *end = strchr(line, '\n');
        if (end) {
            *end = 0;
        }
        end = strchr(line, '\r');
        if (end) {
            *end = 0;
        }
        set_led(line + 4);
    }
}

static int write_status(const char *path, const uint64_t *acc, uint32_t frames) {
    char tmp[768];
    char body[1400];
    size_t used = 0;
    int fd;
    int i;
    if (snprintf(tmp, sizeof tmp, "%s.tmp", path) >= (int)sizeof tmp) {
        return -1;
    }
    used += (size_t)snprintf(body + used, sizeof body - used,
                             "device=%s\nrate=%d\nchannels=%d\nframes=%u\nvolume=%d\nlight=%d\n"
                             "vqe=%s\nvqe_mics=%d\nvqe_speakers=%d\nvqe_in=%d\nvqe_out=%d\nvqe_chunk=%d\nvqe_level=%d\nref=%d\nclean=%u\nhold=%d\nmic=%d\nwake=%s\n",
                             cap_dev, RATE, cap_ch, frames, volume, light, vqe_name,
                             vqe_mics, vqe_speakers, vqe_in, vqe_out, vqe_chunk, vqe_level, ref_level,
                             clean ? (unsigned)clean->write_pos : 0u, hold_clean, mic_ok,
                             wake_aec ? "aec" : "raw");
    for (i = 0; i < cap_ch && used + 32 < sizeof body; i++) {
        uint32_t rms = 0;
        uint32_t bit = 1u << 15;
        uint32_t mean = frames ? (uint32_t)(acc[i] / frames) : 0;
        while (bit) {
            uint32_t next = rms + bit;
            if (next <= 65535u && next * next <= mean) {
                rms = next;
            }
            bit >>= 1;
        }
        used += (size_t)snprintf(body + used, sizeof body - used, "ch%02d=%u\n", i, rms);
    }
    fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) {
        return -1;
    }
    if (write(fd, body, used) != (ssize_t)used) {
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

int main(int argc, char **argv) {
    const char *path = argc > 1 ? argv[1] : "/data/opt/yandex-station/house.status";
    FILE *pipe;
    int fd;
    int16_t frame[CHANNELS];
    uint64_t acc[CHANNELS];
    uint32_t frames = 0;
    uint32_t windows = 0;
    char rec[96];
    size_t need;
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_stop;
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);
    signal(SIGPIPE, SIG_IGN);
    memset(acc, 0, sizeof acc);
    load_board();
    apply_pdm_gain();
    if (snprintf(rec, sizeof rec, "arecord -D %s -r %d -f S16_LE -c %d -t raw -",
                 cap_dev, RATE, cap_ch) >= (int)sizeof rec) {
        return 1;
    }
    pipe = popen(rec, "r");
    if (!pipe) {
        perror("arecord");
        return 1;
    }
    fd = fileno(pipe);
    need = (size_t)cap_ch * sizeof(int16_t);
    open_vqe();
    open_clean();
    open_mic_ring();
    open_play();
    open_mic_send();
    set_led("idle");
    fprintf(stderr, "house listening %s channels %d vqe %s\n", cap_dev, cap_ch, vqe_name);
    fflush(stderr);
    while (run) {
        size_t got = 0;
        memset(frame, 0, sizeof frame);
        while (got < need) {
            ssize_t n = read(fd, (char *)frame + got, need - got);
            if (n < 0) {
                if (errno == EINTR && run) {
                    continue;
                }
                run = 0;
                break;
            }
            if (n == 0) {
                run = 0;
                break;
            }
            got += (size_t)n;
        }
        if (!run || got < need) {
            break;
        }
        {
            int i;
            for (i = 0; i < cap_ch; i++) {
                int32_t s = frame[i];
                acc[i] += (uint64_t)((int64_t)s * s);
            }
            feed_vqe(frame);
            push_mic_sample(frame[2]);
        }
        frames++;
        if (frames >= WINDOW) {
            windows++;
            refresh_delay();
            apply_cmd();
            if ((windows % 5u) == 0u) {
                int v = read_volume();
                int b = read_int_file(light_path);
                if (v >= 0) {
                    volume = v;
                }
                if (b >= 0) {
                    light = b;
                }
            }
            write_status(path, acc, frames);
            mini2_face();
            memset(acc, 0, sizeof acc);
            frames = 0;
        }
    }
    if (frames > 0) {
        write_status(path, acc, frames);
    }
    pclose(pipe);
    return 0;
}
