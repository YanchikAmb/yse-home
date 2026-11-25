#define _GNU_SOURCE
#include "live_ring.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define WAKE_MAGIC 0x314B5759u

static char bridge_root[512];
static struct LiveRing *ring;
static int ring_fd = -1;

static uint32_t mono_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)(ts.tv_sec * 1000u + ts.tv_nsec / 1000000u);
}

static int read_line(const char *path, char *out, size_t cap) {
    int fd;
    ssize_t n;
    size_t i;
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
    for (i = 0; out[i]; i++) {
        if (out[i] == '\r' || out[i] == '\n') {
            out[i] = 0;
            break;
        }
    }
    return 0;
}

static uint32_t published_mode = 99;
static char published_phrase[LIVE_PHRASE];

static void publish(uint32_t mode, const char *phrase) {
    if (!phrase) {
        phrase = "";
    }
    if (published_mode == mode && strcmp(published_phrase, phrase) == 0) {
        return;
    }
    atomic_fetch_add_explicit(&ring->cfg_seq, 1, memory_order_relaxed);
    atomic_store_explicit(&ring->mode, 0, memory_order_relaxed);
    memset(ring->phrase, 0, LIVE_PHRASE);
    snprintf(ring->phrase, LIVE_PHRASE, "%s", phrase);
    atomic_store_explicit(&ring->mode, mode, memory_order_release);
    atomic_fetch_add_explicit(&ring->cfg_seq, 1, memory_order_release);
    published_mode = mode;
    snprintf(published_phrase, sizeof published_phrase, "%s", phrase);
}

static void reload_cfg(char *phrase, size_t phrase_cap, int *custom) {
    char active[64];
    char path[768];
    char head[8];
    char line[96];
    int fd;
    ssize_t n;
    *custom = 0;
    phrase[0] = 0;
    snprintf(path, sizeof path, "%s/active", bridge_root);
    if (read_line(path, active, sizeof active) != 0 || active[0] == 0 || strcmp(active, "yandex") == 0) {
        publish(0, "алиса");
        return;
    }
    snprintf(path, sizeof path, "%s/phrases/%s/model.bin", bridge_root, active);
    fd = open(path, O_RDONLY);
    if (fd < 0) {
        snprintf(path, sizeof path, "%s/presets/%s/model.yvk", bridge_root, active);
        fd = open(path, O_RDONLY);
    }
    if (fd < 0) {
        publish(0, "алиса");
        return;
    }
    n = read(fd, head, 5);
    if (n != 5 || memcmp(head, "YVK1", 4) != 0 || head[4] != '\n') {
        close(fd);
        publish(0, "алиса");
        return;
    }
    n = read(fd, line, sizeof line - 1);
    close(fd);
    if (n < 1) {
        publish(0, "алиса");
        return;
    }
    line[n] = 0;
    {
        char *cut = strpbrk(line, "\r\n");
        if (cut) {
            *cut = 0;
        }
    }
    if (line[0] == 0) {
        publish(0, "алиса");
        return;
    }
    snprintf(phrase, phrase_cap, "%s", line);
    *custom = 1;
    publish(1, phrase);
}

static int open_ring(void) {
    char path[640];
    struct LiveRing *mem;
    snprintf(path, sizeof path, "%s/live.ring", bridge_root);
    ring_fd = open(path, O_RDWR | O_CREAT, 0666);
    if (ring_fd < 0) {
        return -1;
    }
    if (ftruncate(ring_fd, (off_t)sizeof(struct LiveRing)) != 0) {
        return -1;
    }
    fchmod(ring_fd, 0666);
    mem = mmap(NULL, sizeof(struct LiveRing), PROT_READ | PROT_WRITE, MAP_SHARED, ring_fd, 0);
    if (mem == MAP_FAILED) {
        return -1;
    }
    memset(mem, 0, sizeof *mem);
    mem->magic = LIVE_MAGIC;
    ring = mem;
    return 0;
}

static void *bridge_main(void *arg) {
    int sock;
    struct sockaddr_in dst;
    char host[64];
    char phrase[LIVE_PHRASE];
    char cfgpath[640];
    uint32_t seen = 0;
    uint32_t seq = 1;
    uint32_t next_cfg = 0;
    int custom = 0;
    unsigned char packet[88 + 2048];
    (void)arg;
    sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) {
        fprintf(stderr, "wake socket failed\n");
        return NULL;
    }
    fcntl(sock, F_SETFL, O_NONBLOCK);
    memset(&dst, 0, sizeof dst);
    dst.sin_family = AF_INET;
    dst.sin_port = htons(18181);
    snprintf(host, sizeof host, "192.168.209.50");
    snprintf(cfgpath, sizeof cfgpath, "%s/wake.host", bridge_root);
    {
        char line[80];
        char *colon;
        if (read_line(cfgpath, line, sizeof line) == 0 && line[0]) {
            colon = strchr(line, ':');
            if (colon) {
                *colon = 0;
                dst.sin_port = htons((unsigned short)atoi(colon + 1));
            }
            snprintf(host, sizeof host, "%s", line);
        }
    }
    if (inet_pton(AF_INET, host, &dst.sin_addr) != 1) {
        fprintf(stderr, "wake host bad\n");
        return NULL;
    }
    fprintf(stderr, "wake bridge %s:%u\n", host, (unsigned)ntohs(dst.sin_port));
    fflush(stderr);
    for (;;) {
        uint32_t now = mono_ms();
        uint32_t write_pos;
        uint32_t have;
        if (now >= next_cfg) {
            reload_cfg(phrase, sizeof phrase, &custom);
            next_cfg = now + 500;
        }
        write_pos = atomic_load_explicit(&ring->write_samples, memory_order_acquire);
        if (write_pos - seen > LIVE_SAMPLES) {
            seen = write_pos - LIVE_SAMPLES;
        }
        have = write_pos - seen;
        if (custom && have >= 512) {
            uint32_t n = have > 1024 ? 1024 : have;
            uint32_t i;
            int sent;
            uint32_t hit = 0;
            unsigned char reply[12];
            struct sockaddr_in from;
            socklen_t fromlen = sizeof from;
            {
                uint32_t magic = WAKE_MAGIC;
                memset(packet, 0, 88);
                memcpy(packet, &magic, 4);
                memcpy(packet + 4, &seq, 4);
            }
            snprintf((char *)packet + 8, 80, "%s", phrase);
            {
                static uint32_t noise_rms = 6;
                static uint32_t last_log;
                int64_t acc = 0;
                int32_t peak = 0;
                uint32_t mean;
                uint32_t rms = 0;
                uint32_t bit;
                int gain = 1;
                for (i = 0; i < n; i++) {
                    int32_t sample = ring->pcm[(seen + i) % LIVE_SAMPLES];
                    int32_t absamp = sample < 0 ? -sample : sample;
                    acc += (int64_t)sample * sample;
                    if (absamp > peak) {
                        peak = absamp;
                    }
                }
                mean = (uint32_t)(acc / (n ? n : 1));
                bit = 1u << 15;
                while (bit) {
                    uint32_t next = rms + bit;
                    if (next <= 65535 && next * next <= mean) {
                        rms = next;
                    }
                    bit >>= 1;
                }
                /* Пол тишины около rms 5. Речь чуть громче, для Vosk её нужно поднять. */
                if (rms > noise_rms * 2 && rms >= 8) {
                    gain = (int)(4000 / (rms ? rms : 1));
                    if (gain < 1) {
                        gain = 1;
                    }
                    if (gain > 60) {
                        gain = 60;
                    }
                    if (mono_ms() - last_log > 1000) {
                        fprintf(stderr, "agc rms=%u noise=%u peak=%d gain=%d\n", rms, noise_rms, peak, gain);
                        fflush(stderr);
                        last_log = mono_ms();
                    }
                } else {
                    noise_rms = (noise_rms * 15 + rms) / 16;
                    if (noise_rms < 1) {
                        noise_rms = 1;
                    }
                }
                for (i = 0; i < n; i++) {
                    int32_t sample = (int32_t)ring->pcm[(seen + i) % LIVE_SAMPLES] * gain;
                    if (sample > 32767) {
                        sample = 32767;
                    } else if (sample < -32768) {
                        sample = -32768;
                    }
                    memcpy(packet + 88 + i * 2, &sample, 2);
                }
            }
            seen += n;
            sent = sendto(sock, packet, 88 + n * 2, 0, (struct sockaddr *)&dst, sizeof dst);
            if (sent > 0) {
                uint32_t deadline = mono_ms() + 200;
                while ((int32_t)(deadline - mono_ms()) > 0) {
                    ssize_t r = recvfrom(sock, reply, sizeof reply, 0, (struct sockaddr *)&from, &fromlen);
                    uint32_t magic = 0;
                    uint32_t rseq = 0;
                    if (r < 0) {
                        if (errno == EAGAIN || errno == EWOULDBLOCK) {
                            usleep(2000);
                            continue;
                        }
                        break;
                    }
                    if (r != 12) {
                        continue;
                    }
                    memcpy(&magic, reply, 4);
                    memcpy(&rseq, reply + 4, 4);
                    memcpy(&hit, reply + 8, 4);
                    if (magic == WAKE_MAGIC && rseq == seq) {
                        atomic_store_explicit(&ring->reply_ms, mono_ms(), memory_order_release);
                        if (hit) {
                            atomic_fetch_add_explicit(&ring->hit_seq, 1, memory_order_release);
                            fprintf(stderr, "wake hit %s\n", phrase);
                            fflush(stderr);
                        }
                        break;
                    }
                }
            }
            seq++;
            continue;
        }
        if (!custom) {
            seen = write_pos;
        }
        usleep(10000);
    }
}

void start_bridge(const char *root) {
    pthread_t thread;
    snprintf(bridge_root, sizeof bridge_root, "%s", root);
    if (open_ring() != 0) {
        fprintf(stderr, "wake ring failed\n");
        return;
    }
    if (pthread_create(&thread, NULL, bridge_main, NULL) != 0) {
        fprintf(stderr, "wake thread failed\n");
        return;
    }
    pthread_detach(thread);
}
