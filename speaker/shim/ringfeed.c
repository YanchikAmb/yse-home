#define _GNU_SOURCE
#include "live_ring.h"

#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <unistd.h>

/* Кладёт готовый поток 16 кГц int16 в кольцо, в темпе микрофона. */
int main(int argc, char **argv) {
    int ringfd;
    int in;
    struct LiveRing *ring;
    int16_t buf[512];
    if (argc != 2) {
        return 2;
    }
    ringfd = open("/data/opt/yandex-station/live.ring", O_RDWR);
    in = open(argv[1], O_RDONLY);
    if (ringfd < 0 || in < 0) {
        return 1;
    }
    ring = mmap(NULL, sizeof *ring, PROT_READ | PROT_WRITE, MAP_SHARED, ringfd, 0);
    if (ring == MAP_FAILED || ring->magic != LIVE_MAGIC) {
        return 1;
    }
    for (;;) {
        ssize_t n = read(in, buf, sizeof buf);
        uint32_t count;
        uint32_t pos;
        uint32_t i;
        if (n <= 0) {
            break;
        }
        count = (uint32_t)n / 2u;
        pos = atomic_load_explicit(&ring->write_samples, memory_order_relaxed);
        for (i = 0; i < count; i++) {
            ring->pcm[(pos + i) % LIVE_SAMPLES] = buf[i];
        }
        atomic_store_explicit(&ring->write_samples, pos + count, memory_order_release);
        usleep((useconds_t)(count * 1000000u / 16000u));
    }
    return 0;
}
