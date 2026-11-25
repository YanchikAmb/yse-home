#define _GNU_SOURCE
#include <dlfcn.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

typedef int (*create_fn)(const char *name, void **out);
typedef int (*get_fn)(void *cfg);
typedef int (*eng_fn)(void *cfg, const char *path, int n, void *pairs, void **out);
typedef int (*proc_fn)(void *eng, void *mic, int mic_n, void *spk, int spk_n);
typedef int (*out_fn)(void *eng, int type, int channel, void **data, int *count);

static int span_count(uint32_t begin, uint32_t end) {
    uint32_t bytes;
    if (begin == 0 || end < begin) {
        return -1;
    }
    bytes = end - begin;
    if (bytes % 12u != 0 || bytes / 12u > 32u) {
        return -1;
    }
    return (int)(bytes / 12u);
}

static void try_path(void *cfg, eng_fn make, proc_fn process, out_fn output, int chunk, const char *path) {
    pid_t pid;
    int status;
    pid = fork();
    if (pid < 0) {
        perror("fork");
        return;
    }
    if (pid == 0) {
        void *eng = 0;
        uint32_t *words;
        int mics;
        int speakers;
        int rc;
        alarm(15);
        rc = make(cfg, path, 0, 0, &eng);
        printf("create path=%s rc=%d eng=%p\n", path[0] ? path : "(empty)", rc, eng);
        fflush(stdout);
        if (rc != 0 || !eng) {
            _exit(0);
        }
        words = eng;
        mics = span_count(words[0x9cc / 4], words[0x9d0 / 4]);
        speakers = span_count(words[0x70 / 4], words[0x74 / 4]);
        printf("records mics=%d speakers=%d chunk=%d\n", mics, speakers, chunk);
        fflush(stdout);
        if (process && mics > 0 && speakers >= 0 && chunk > 0 && chunk <= 8192 && mics <= 8 && speakers <= 8) {
            float *mic = calloc((size_t)mics * (size_t)chunk, sizeof(float));
            float *spk = calloc((size_t)(speakers ? speakers : 1) * (size_t)chunk, sizeof(float));
            int frame;
            if (!mic || !spk) {
                _exit(0);
            }
            for (frame = 0; frame < 40; frame++) {
                int s;
                int prc;
                for (s = 0; s < chunk; s++) {
                    float v = ((s / 48) & 1) ? 0.25f : -0.25f;
                    int c;
                    for (c = 0; c < mics; c++) {
                        mic[(size_t)s * (size_t)mics + (size_t)c] = v;
                    }
                }
                prc = process(eng, mic, mics * chunk, speakers ? spk : 0, speakers * chunk);
                if (prc != 0) {
                    printf("process frame=%d rc=%d\n", frame, prc);
                    break;
                }
                if (output && (frame == 0 || frame == 9 || frame == 19 || frame == 39)) {
                    void *data = 0;
                    int count = -1;
                    int orc = output(eng, 6, 0, &data, &count);
                    double acc = 0;
                    if (orc == 0 && data && count > 0 && count <= 8192) {
                        float *f = data;
                        int n = count > 512 ? 512 : count;
                        int k;
                        for (k = 0; k < n; k++) {
                            acc += (double)f[k] * (double)f[k];
                        }
                        printf("frame=%d count=%d out0=%g energy=%g\n", frame, count, f[0], acc);
                    } else {
                        printf("frame=%d out rc=%d count=%d\n", frame, orc, count);
                    }
                    fflush(stdout);
                }
            }
        }
        fflush(stdout);
        _exit(0);
    }
    if (waitpid(pid, &status, 0) < 0) {
        perror("wait");
        return;
    }
    if (WIFSIGNALED(status)) {
        printf("create path=%s signal=%d\n", path[0] ? path : "(empty)", WTERMSIG(status));
    }
}

int main(void) {
    void *lib;
    create_fn create;
    get_fn mics;
    get_fn in_rate;
    get_fn out_rate;
    get_fn chunk;
    get_fn frame;
    get_fn speakers;
    eng_fn make;
    proc_fn process;
    out_fn output;
    void *cfg = 0;
    int chunk_n = 0;
    const char *names[] = {
        "station_2_rev5", "station_2_rev4", "station_2_rev3", "station_2_rev2",
        "station_2_rev1", "station_2_16kHz", "yandexmidi", 0
    };
    const char *paths[] = {
        "",
        "/system/vendor/quasar/vqe_models",
        "/system/vendor/quasar/vqe_models/",
        0
    };
    int i;
    lib = dlopen("/system/vendor/quasar/libs/libyandex_evil.so", RTLD_NOW);
    if (!lib) {
        fprintf(stderr, "open %s\n", dlerror());
        return 1;
    }
    create = dlsym(lib, "YandexVqeC_Config_CreateFromPreset");
    mics = dlsym(lib, "YandexVqeC_Config_GetMicsCount");
    in_rate = dlsym(lib, "YandexVqeC_Config_GetInputSamplingRate");
    out_rate = dlsym(lib, "YandexVqeC_Config_GetOutputSamplingRate");
    chunk = dlsym(lib, "YandexVqeC_Config_GetInputChunkSize");
    frame = dlsym(lib, "YandexVqeC_Config_GetProcessingFrame");
    speakers = dlsym(lib, "YandexVqeC_Config_GetSpeakersCount");
    make = dlsym(lib, "YandexVqeC_Create");
    process = dlsym(lib, "YandexVqeC_Process");
    output = dlsym(lib, "YandexVqeC_GetOutputChannelData");
    if (!create || !mics || !in_rate || !out_rate || !chunk || !frame || !speakers || !make) {
        fprintf(stderr, "symbol %s\n", dlerror());
        return 1;
    }
    for (i = 0; names[i]; i++) {
        void *one = 0;
        int rc = create(names[i], &one);
        printf("%s rc=%d cfg=%p", names[i], rc, one);
        if (rc == 0 && one) {
            printf(" mics=%d speakers=%d in=%d out=%d chunk=%d frame=%d",
                   mics(one), speakers(one), in_rate(one), out_rate(one), chunk(one), frame(one));
            if (strcmp(names[i], "yandexmidi") == 0) {
                cfg = one;
                chunk_n = chunk(one);
            }
        }
        printf("\n");
        fflush(stdout);
    }
    if (!cfg) {
        return 1;
    }
    for (i = 0; paths[i]; i++) {
        try_path(cfg, make, process, output, chunk_n, paths[i]);
        fflush(stdout);
    }
    return 0;
}
