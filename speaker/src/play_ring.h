#ifndef YSE_PLAY_RING_H
#define YSE_PLAY_RING_H

#include <stdint.h>

#define PLAY_MAGIC 0x59414c50u
#define PLAY_CAP 65536u

/* Что ушло в динамик, 48 кГц, два одинаковых канала. Пишет aplay, читает housed. */
struct PlayRing {
    uint32_t magic;
    uint32_t capacity;
    volatile uint32_t write_pos;
    uint32_t channels;
    int16_t pcm[PLAY_CAP * 2];
};

#endif
