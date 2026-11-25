#define _GNU_SOURCE
#include "live_ring.h"

#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#define FN_HOT 1
#define FN_EVENT 2
#define DEPTH 16
#define LINE_CAP 2000

typedef struct {
    const char *name;
    unsigned flags;
} FnInfo;

#include "names.inc"

#define NFN (sizeof(fns) / sizeof(fns[0]))

enum { F_R4 = 4, F_LR = 11 };

typedef struct {
    uint32_t r4;
    uint32_t lr;
} Restore;

static int logfd = -1;
static int id_phrase = -1;
static int id_phrase_id = -1;
static int id_event = -1;
static int id_submit = -1;
static int id_errmsg = -1;
static unsigned phrase_dumps;
static unsigned errmsg_dumps;
static unsigned pagesz = 4096;
static unsigned lines;
static unsigned calls[NFN];
static unsigned leaves[NFN];
static unsigned event_logs[NFN];
static uint32_t prev_r0[NFN];
static void *realp[NFN];
static char tried[NFN];

static __thread int sptr;
static __thread uint32_t stack_r4[DEPTH];
static __thread uint32_t stack_lr[DEPTH];
static __thread int stack_id[DEPTH];
static __thread const void *pending_pcm;
static __thread uint32_t pending_len;

static struct LiveRing *ring;
static uint32_t ring_next_try;
static int inject_left;
static int last_was_inject;
static uint32_t last_hit;
static uint32_t cur_id;
static char word[LIVE_PHRASE];
static const char speech_started[] = "<speech_started>";
static const char *cur_phrase;

static uint32_t mono_ms(void) {
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) {
        return 0;
    }
    return (uint32_t)(ts.tv_sec * 1000u + (uint32_t)(ts.tv_nsec / 1000000u));
}

static void log_line(const char *s) {
    size_t n;
    if (logfd < 0 || !s) {
        return;
    }
    n = strlen(s);
    if (n > 0) {
        (void)write(logfd, s, n);
    }
}

static void tap_log(const char *fmt, uint32_t a, uint32_t b, uint32_t c, uint32_t d,
                    const char *name, unsigned ncall) {
    char buf[240];
    unsigned ms = mono_ms();
    int k;
    if (logfd < 0 || lines >= LINE_CAP) {
        return;
    }
    k = snprintf(buf, sizeof buf, "%u %s %s #%u r0=%08x r1=%08x r2=%08x r3=%08x\n",
                 ms % 100000000u, fmt, name, ncall, a, b, c, d);
    if (k > 0) {
        lines++;
        (void)write(logfd, buf, (size_t)k);
    }
}

static void find_ids(void) {
    unsigned i;
    for (i = 0; i < NFN; i++) {
        if (strcmp(fns[i].name, "spotter_get_current_phrase") == 0) {
            id_phrase = (int)i;
        } else if (strcmp(fns[i].name, "spotter_get_current_phrase_id") == 0) {
            id_phrase_id = (int)i;
        } else if (strcmp(fns[i].name, "spotter_get_next_event") == 0) {
            id_event = (int)i;
        } else if (strcmp(fns[i].name, "spotter_submit_raw_data") == 0) {
            id_submit = (int)i;
        } else if (strcmp(fns[i].name, "spotter_error_get_message") == 0) {
            id_errmsg = (int)i;
        }
    }
}

static int page_ok(const void *ptr, uint32_t nbytes);
static void peek_create_args(const char *name, uint32_t r0, uint32_t r2, uint32_t r3);
static void peek_stack_args(uint32_t a, uint32_t b);

static void peek_errmsg(uint32_t addr) {
    unsigned char raw[700];
    unsigned char child[220];
    char line[780];
    char asc[701];
    uint32_t ptr;
    unsigned i;
    unsigned n;
    int k;

    if (errmsg_dumps >= 8 || logfd < 0 || lines >= LINE_CAP || addr < pagesz) {
        return;
    }
    errmsg_dumps++;
    if (!page_ok((void *)(uintptr_t)addr, sizeof raw)) {
        k = snprintf(line, sizeof line, "errmsg addr=%08x unreadable\n", addr);
        if (k > 0) {
            lines++;
            (void)write(logfd, line, (size_t)k);
        }
        return;
    }
    memcpy(raw, (const void *)(uintptr_t)addr, sizeof raw);
    for (i = 0; i < sizeof raw; i++) {
        asc[i] = (raw[i] >= 32 && raw[i] < 127) ? (char)raw[i] : '.';
    }
    asc[sizeof raw] = 0;
    k = snprintf(line, sizeof line, "errmsg addr=%08x %s\n", addr, asc);
    if (k > 0) {
        lines++;
        (void)write(logfd, line, (size_t)k);
    }
    memcpy(&ptr, raw, sizeof ptr);
    if (ptr < pagesz || !page_ok((void *)(uintptr_t)ptr, 32)) {
        return;
    }
    memcpy(child, (const void *)(uintptr_t)ptr, sizeof child - 1);
    n = sizeof child - 1;
    for (i = 0; i < sizeof child - 1; i++) {
        if (child[i] == 0) {
            n = i;
            break;
        }
        child[i] = (child[i] >= 32 && child[i] < 127) ? child[i] : '.';
    }
    child[n] = 0;
    k = snprintf(line, sizeof line, "errmsg via=%08x %s\n", ptr, (char *)child);
    if (k > 0) {
        lines++;
        (void)write(logfd, line, (size_t)k);
    }
}

static void peek_phrase(uint32_t addr) {
    unsigned char raw[32];
    unsigned char vec;
    char hex[sizeof raw * 2 + 1];
    char asc[sizeof raw + 1];
    char line[220];
    uint32_t page;
    unsigned n;
    unsigned i;
    int k;

    if (phrase_dumps >= 8 || logfd < 0 || lines >= LINE_CAP) {
        return;
    }
    phrase_dumps++;
    if (addr < pagesz) {
        return;
    }
    page = addr & ~(pagesz - 1u);
    if (mincore((void *)(uintptr_t)page, pagesz, &vec) != 0) {
        return;
    }
    n = sizeof raw;
    if ((addr - page) + n > pagesz) {
        if (mincore((void *)(uintptr_t)(page + pagesz), pagesz, &vec) != 0) {
            n = pagesz - (addr - page);
        }
    }
    memcpy(raw, (const void *)(uintptr_t)addr, n);
    for (i = 0; i < n; i++) {
        static const char *digits = "0123456789abcdef";
        hex[i * 2] = digits[raw[i] >> 4];
        hex[i * 2 + 1] = digits[raw[i] & 15];
        asc[i] = (raw[i] >= 32 && raw[i] < 127) ? (char)raw[i] : '.';
        if (raw[i] == 0) {
            n = i;
            break;
        }
    }
    hex[n * 2] = 0;
    asc[n] = 0;
    k = snprintf(line, sizeof line, "phrase addr=%08x %s %s\n", addr, hex, asc);
    if (k > 0) {
        lines++;
        (void)write(logfd, line, (size_t)k);
    }
}

static int page_ok(const void *ptr, uint32_t nbytes) {
    uintptr_t addr = (uintptr_t)ptr;
    uintptr_t last;
    unsigned char vec;
    uint32_t page;
    if (!ptr || nbytes == 0 || addr < pagesz) {
        return 0;
    }
    last = addr + nbytes - 1;
    page = (uint32_t)(addr & ~(uintptr_t)(pagesz - 1u));
    if (mincore((void *)(uintptr_t)page, pagesz, &vec) != 0) {
        return 0;
    }
    if ((last & ~(uintptr_t)(pagesz - 1u)) != page) {
        page = (uint32_t)(last & ~(uintptr_t)(pagesz - 1u));
        if (mincore((void *)(uintptr_t)page, pagesz, &vec) != 0) {
            return 0;
        }
    }
    return 1;
}

static void ring_open(void) {
    int fd;
    void *mem;
    uint32_t now;
    if (ring) {
        return;
    }
    now = mono_ms();
    if (now < ring_next_try) {
        return;
    }
    ring_next_try = now + 1000;
    fd = open("/data/opt/yandex-station/live.ring", O_RDWR);
    if (fd < 0) {
        return;
    }
    mem = mmap(NULL, sizeof(struct LiveRing), PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    close(fd);
    if (mem == MAP_FAILED) {
        return;
    }
    if (((struct LiveRing *)mem)->magic != LIVE_MAGIC) {
        munmap(mem, sizeof(struct LiveRing));
        return;
    }
    ring = mem;
    last_hit = atomic_load_explicit(&ring->hit_seq, memory_order_acquire);
}

static int bridge_fresh(void) {
    uint32_t reply;
    uint32_t now;
    if (!ring) {
        return 0;
    }
    reply = atomic_load_explicit(&ring->reply_ms, memory_order_acquire);
    now = mono_ms();
    return reply != 0 && (now - reply) < 3000u;
}

static int custom_live(void) {
    if (!ring) {
        return 0;
    }
    if (atomic_load_explicit(&ring->mode, memory_order_acquire) != 1) {
        return 0;
    }
    return bridge_fresh();
}

static void copy_phrase(void) {
    int tries;
    for (tries = 0; tries < 4; tries++) {
        uint32_t s1 = atomic_load_explicit(&ring->cfg_seq, memory_order_acquire);
        uint32_t s2;
        if (s1 & 1u) {
            continue;
        }
        memcpy(word, ring->phrase, LIVE_PHRASE);
        word[LIVE_PHRASE - 1] = 0;
        s2 = atomic_load_explicit(&ring->cfg_seq, memory_order_acquire);
        if (s1 == s2 && word[0]) {
            return;
        }
    }
}

static int feed_ring(const void *ptr, uint32_t nbytes) {
    const int16_t *samples;
    uint32_t count;
    uint32_t pos;
    uint32_t i;
    ring_open();
    if (!ring || nbytes < 2 || (nbytes & 1u) || nbytes > 8192) {
        return 0;
    }
    if (((uintptr_t)ptr & 1u) != 0 || !page_ok(ptr, nbytes)) {
        return 0;
    }
    samples = ptr;
    count = nbytes / 2;
    pos = atomic_load_explicit(&ring->write_samples, memory_order_relaxed);
    for (i = 0; i < count; i++) {
        ring->pcm[(pos + i) % LIVE_SAMPLES] = samples[i];
    }
    atomic_store_explicit(&ring->write_samples, pos + count, memory_order_release);
    return 1;
}

static void on_event(uint32_t *rets) {
    uint32_t hit;
    ring_open();
    if (!custom_live()) {
        inject_left = 0;
        last_was_inject = 0;
        if (ring) {
            last_hit = atomic_load_explicit(&ring->hit_seq, memory_order_acquire);
        }
        return;
    }
    hit = atomic_load_explicit(&ring->hit_seq, memory_order_acquire);
    if (hit != last_hit) {
        last_hit = hit;
        copy_phrase();
        if (inject_left == 0 && word[0]) {
            inject_left = 2;
        }
    }
    if (inject_left == 2) {
        rets[0] = 2;
        cur_phrase = speech_started;
        cur_id = 28;
        inject_left = 1;
        last_was_inject = 1;
        return;
    }
    if (inject_left == 1) {
        rets[0] = 2;
        cur_phrase = word;
        cur_id = 1;
        inject_left = 0;
        last_was_inject = 1;
        tap_log("inject", cur_id, 0, 0, 0, word, 0);
        return;
    }
    last_was_inject = 0;
}

static void *resolve(int id) {
    if (!tried[id]) {
        realp[id] = dlsym(RTLD_NEXT, fns[id].name);
        tried[id] = 1;
    }
    return realp[id];
}

__attribute__((constructor)) static void tap_init(void) {
    char buf[120];
    long page;
    int k;
    logfd = open("/data/opt/spotter-tap.log", O_WRONLY | O_CREAT | O_APPEND, 0644);
    find_ids();
    page = sysconf(_SC_PAGESIZE);
    if (page > 0) {
        pagesz = (unsigned)page;
    }
    k = snprintf(buf, sizeof buf, "tap init pid=%d phrase=%d event=%d submit=%d page=%u\n",
                 (int)getpid(), id_phrase, id_event, id_submit, pagesz);
    if (k > 0) {
        log_line(buf);
    }
}

uint32_t tap_before(int id, uint32_t *frame) {
    void *fn;
    unsigned n;
    int slot;
    if (id < 0 || (unsigned)id >= NFN || !frame) {
        return 0;
    }
    fn = resolve(id);
    if (!fn) {
        return 0;
    }
    slot = sptr;
    if (slot >= DEPTH) {
        slot = DEPTH - 1;
    } else {
        sptr = slot + 1;
    }
    stack_r4[slot] = frame[F_R4];
    stack_lr[slot] = frame[F_LR];
    stack_id[slot] = id;
    n = ++calls[id];
    if (id == id_submit) {
        pending_pcm = (const void *)(uintptr_t)frame[1];
        pending_len = frame[2];
    }
    if (fns[id].flags & FN_HOT) {
        if (n <= 8 || n % 5000 == 0) {
            tap_log("enter", frame[0], frame[1], frame[2], frame[3], fns[id].name, n);
        }
    } else if (n <= 12) {
        tap_log("enter", frame[0], frame[1], frame[2], frame[3], fns[id].name, n);
    }
    if (n <= 12) {
        peek_create_args(fns[id].name, frame[0], frame[2], frame[3]);
        if (strcmp(fns[id].name, "spotter_create_from_multi_conf") == 0) {
            peek_stack_args(frame[12], frame[13]);
        }
    }
    return (uint32_t)fn;
}

static void peek_create_args(const char *name, uint32_t r0, uint32_t r2, uint32_t r3) {
    char buf[220];
    unsigned char raw[48];
    unsigned i;
    unsigned n;
    int k;
    if (!name || lines >= LINE_CAP) {
        return;
    }
    if (strcmp(name, "spotter_multi_conf_create") == 0) {
        if (!page_ok((const void *)(uintptr_t)r0, 48)) {
            return;
        }
        memcpy(raw, (const void *)(uintptr_t)r0, 48);
        for (i = 0; i < 47; i++) {
            if (raw[i] < 32 || raw[i] > 126) {
                raw[i] = '.';
            }
        }
        raw[47] = 0;
        k = snprintf(buf, sizeof buf, "mcreate %s\n", raw);
        if (k > 0) {
            lines++;
            log_line(buf);
        }
        return;
    }
    if (strcmp(name, "spotter_create_from_multi_conf") != 0) {
        return;
    }
    n = r2;
    if (n == 0 || n > 8) {
        return;
    }
    if (!page_ok((const void *)(uintptr_t)r3, n * 4)) {
        return;
    }
    memcpy(raw, (const void *)(uintptr_t)r3, n * 4);
    k = snprintf(buf, sizeof buf, "ctypes");
    for (i = 0; i < n && k > 0 && (unsigned)k < sizeof buf; i++) {
        uint32_t v;
        memcpy(&v, raw + i * 4, 4);
        k += snprintf(buf + k, sizeof buf - (unsigned)k, " %u", v);
    }
    if (k > 0 && (unsigned)k < sizeof buf - 1) {
        buf[k++] = '\n';
        buf[k] = 0;
        lines++;
        log_line(buf);
    }
}

static void peek_one_stack(uint32_t addr) {
    char buf[160];
    unsigned char raw[16];
    unsigned i;
    int k;
    uint32_t v;
    if (!page_ok((const void *)(uintptr_t)addr, 16)) {
        return;
    }
    memcpy(raw, (const void *)(uintptr_t)addr, 16);
    k = snprintf(buf, sizeof buf, "stackmem");
    for (i = 0; i < 4 && k > 0 && (unsigned)k < sizeof buf; i++) {
        memcpy(&v, raw + i * 4, 4);
        k += snprintf(buf + k, sizeof buf - (unsigned)k, " %u", v);
    }
    if (k > 0 && (unsigned)k < sizeof buf - 1) {
        buf[k++] = '\n';
        buf[k] = 0;
        lines++;
        log_line(buf);
    }
}

static void peek_stack_args(uint32_t a, uint32_t b) {
    char buf[80];
    int k;
    if (lines >= LINE_CAP) {
        return;
    }
    k = snprintf(buf, sizeof buf, "stack %u %u\n", a, b);
    if (k > 0) {
        lines++;
        log_line(buf);
    }
    if (a > 0x1000) {
        peek_one_stack(a);
    }
    if (b > 0x1000) {
        peek_one_stack(b);
    }
}

void tap_after(uint32_t *rets, Restore *out) {
    int slot;
    int id;
    unsigned n;
    unsigned flags;
    if (sptr <= 0) {
        slot = 0;
    } else {
        sptr--;
        slot = sptr;
    }
    id = stack_id[slot];
    out->r4 = stack_r4[slot];
    out->lr = stack_lr[slot];
    if (id < 0 || (unsigned)id >= NFN || !rets) {
        return;
    }
    n = ++leaves[id];
    flags = fns[id].flags;
    if (id == id_submit && pending_pcm) {
        uint32_t consumed = rets[0];
        if (consumed > pending_len) {
            consumed = pending_len;
        }
        /* Библиотека забирает consumed байт и сдвигает буфер. В кольцо кладём только их. */
        if (consumed >= 2 && (consumed & 1u) == 0) {
            feed_ring(pending_pcm, consumed);
        }
        pending_pcm = NULL;
    }
    if (id == id_event) {
        on_event(rets);
    } else if (last_was_inject && custom_live()) {
        if (id == id_phrase && cur_phrase) {
            rets[0] = (uint32_t)(uintptr_t)cur_phrase;
        } else if (id == id_phrase_id) {
            rets[0] = cur_id;
        }
    }
    if (flags & FN_EVENT) {
        if (event_logs[id] < 60 && (n <= 4 || rets[0] != prev_r0[id])) {
            event_logs[id]++;
            tap_log("leave", rets[0], rets[1], rets[2], rets[3], fns[id].name, n);
        }
        prev_r0[id] = rets[0];
    } else if (!(flags & FN_HOT) && n <= 12) {
        tap_log("leave", rets[0], rets[1], rets[2], rets[3], fns[id].name, n);
    } else if ((flags & FN_HOT) && n <= 2) {
        tap_log("leave", rets[0], rets[1], rets[2], rets[3], fns[id].name, n);
    }
    if (id == id_phrase) {
        peek_phrase(rets[0]);
    } else if (id == id_errmsg) {
        peek_errmsg(rets[0]);
    }
}
