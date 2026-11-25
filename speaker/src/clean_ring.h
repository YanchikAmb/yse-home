#ifndef YSE_CLEAN_RING_H
#define YSE_CLEAN_RING_H

#include <stdint.h>

#define CLEAN_MAGIC 0x524e4759u
#define CLEAN_CAP 131072u

/* Очищенные 16 кГц, один канал. Пишет housed, читает ухо. */
struct CleanRing {
    uint32_t magic;
    uint32_t capacity;
    volatile uint32_t write_pos;
    uint32_t pad;
    int16_t pcm[CLEAN_CAP];
};

#endif
