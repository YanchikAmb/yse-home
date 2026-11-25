#ifndef YSE_LIVE_RING_H
#define YSE_LIVE_RING_H

#include <stdatomic.h>
#include <stdint.h>

#define LIVE_MAGIC 0x314B5759u
#define LIVE_SAMPLES 16000u
#define LIVE_PHRASE 80

/* Общая память панели и прокладки. Писатели разные, поля с atomic не рвутся. */
struct LiveRing {
    uint32_t magic;
    _Atomic uint32_t write_samples;
    _Atomic uint32_t hit_seq;
    _Atomic uint32_t mode;
    _Atomic uint32_t cfg_seq;
    _Atomic uint32_t reply_ms;
    char phrase[LIVE_PHRASE];
    int16_t pcm[LIVE_SAMPLES];
};

#endif
