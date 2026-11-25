/* Отдаёт очищенный звук колонки (clean.ring, 16 кГц, один канал) в stdout.
   ringcat BACK_MS MAX_SEC: начать на BACK_MS раньше текущего места,
   остановиться через MAX_SEC или когда читатель закрыл трубу. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "clean_ring.h"

static long long now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

int main(int argc, char **argv) {
    int back_ms = argc > 1 ? atoi(argv[1]) : 0;
    int max_sec = argc > 2 ? atoi(argv[2]) : 20;
    struct CleanRing *ring;
    struct stat st;
    uint32_t read_pos;
    uint32_t back;
    long long stop_at;
    int16_t out[1024];
    int fd;
    if (back_ms < 0 || back_ms > 6000) {
        back_ms = 0;
    }
    if (max_sec < 1 || max_sec > 120) {
        max_sec = 20;
    }
    signal(SIGPIPE, SIG_IGN);
    fd = open("/data/opt/yandex-station/clean.ring", O_RDONLY);
    if (fd < 0 || fstat(fd, &st) != 0 || st.st_size < (off_t)sizeof(struct CleanRing)) {
        fprintf(stderr, "ring\n");
        return 1;
    }
    ring = mmap(NULL, sizeof(struct CleanRing), PROT_READ, MAP_SHARED, fd, 0);
    close(fd);
    if (ring == MAP_FAILED || ring->magic != CLEAN_MAGIC || ring->capacity != CLEAN_CAP) {
        fprintf(stderr, "ring\n");
        return 1;
    }
    back = (uint32_t)back_ms * 16u;
    __sync_synchronize();
    read_pos = ring->write_pos - back;
    stop_at = now_ms() + (long long)max_sec * 1000;
    while (now_ms() < stop_at) {
        uint32_t w = ring->write_pos;
        uint32_t avail;
        uint32_t i;
        __sync_synchronize();
        avail = w - read_pos;
        if (avail > CLEAN_CAP) {
            read_pos = w;
            continue;
        }
        if (avail < 320u) {
            usleep(10000);
            continue;
        }
        if (avail > 1024u) {
            avail = 1024u;
        }
        for (i = 0; i < avail; i++) {
            out[i] = ring->pcm[(read_pos + i) & (CLEAN_CAP - 1u)];
        }
        read_pos += avail;
        {
            const char *p = (const char *)out;
            size_t left = (size_t)avail * sizeof(int16_t);
            while (left > 0) {
                ssize_t n = write(1, p, left);
                if (n < 0) {
                    if (errno == EINTR) {
                        continue;
                    }
                    return 0;
                }
                p += n;
                left -= (size_t)n;
            }
        }
    }
    return 0;
}
