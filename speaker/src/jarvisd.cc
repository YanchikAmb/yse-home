/* Слово «джарвис» на колонке без сервера.

Та же модель, что у серверного слова (ru_jarvis_mww.tflite, microWakeWord), тот же
microfrontend и те же правила срабатывания, что в mww_wake_listen.py: среднее по 10
окнам, порог с гистерезисом 0.04, громкость за 0.4 с, пауза 2 с. Вход — wake.ring:
housed кладёт туда ровно те отсчёты, что шлёт серверу (preemph микрофона, AEC при звуке).
Считает TFLite Micro: штатный libtflite.so колонки не знает CALL_ONCE и половины операций.
Слушает только «джарвис локально». Пока оно выбрано, раз в 3 с шлёт YSEMODE, чтобы
сервер молчал, и YSEWAKE на срабатывание.

  jarvisd                  слушает wake.ring
  jarvisd --pcm F          прогоняет s16le 16 кГц из wake.ring-потока, печатает срабатывания
  jarvisd --pcm-mic F      то же для mic.ring: сначала preemph 0.97, как в housed
*/

#include <arpa/inet.h>
#include <fcntl.h>
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
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

#include <new>

#include "clean_ring.h"
#include "host_conf.h"
#include "tensorflow/lite/micro/micro_allocator.h"
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/micro/micro_resource_variable.h"
#include "tensorflow/lite/micro/schema/schema_generated.h"

extern "C" {
#include "tensorflow/lite/experimental/microfrontend/lib/frontend.h"
#include "tensorflow/lite/experimental/microfrontend/lib/frontend_util.h"
}

#define DIR "/data/opt/yandex-station"
#define MODEL_PATH DIR "/jarvis/ru_jarvis_mww.tflite"
#define CHUNK 160
#define STRIDE 3
#define WINDOW 10
#define HIST 6400
#define RMS_MIN 3.0f
#define RMS_MUSIC 1.5f
#define REFRACTORY 2.0f
#define FEATURE_SCALE 0.0390625f

static volatile sig_atomic_t run = 1;

static void on_stop(int) {
    run = 0;
}

static float cutoff = 0.85f;

static void load_cutoff(void) {
    FILE *fp = fopen(DIR "/settings/jarvis_cutoff.txt", "r");
    char line[32];
    if (!fp) {
        return;
    }
    if (fgets(line, sizeof line, fp)) {
        float v = strtof(line, nullptr);
        if (v >= 0.5f && v <= 0.99f) {
            cutoff = v;
        }
    }
    fclose(fp);
}

static uint8_t *model_data;

static int load_model(const char *path) {
    FILE *fp = fopen(path, "rb");
    long len;
    if (!fp) {
        return -1;
    }
    fseek(fp, 0, SEEK_END);
    len = ftell(fp);
    fseek(fp, 0, SEEK_SET);
    model_data = (uint8_t *)aligned_alloc(16, ((size_t)len + 15) & ~(size_t)15);
    if (!model_data || fread(model_data, 1, (size_t)len, fp) != (size_t)len) {
        fclose(fp);
        return -1;
    }
    fclose(fp);
    return 0;
}

alignas(16) static uint8_t arena[64 * 1024];
alignas(tflite::MicroInterpreter) static uint8_t interp_mem[sizeof(tflite::MicroInterpreter)];

struct Fire {
    int kind; /* 0 нет, 1 срабатывание, 2 тихо: оценка есть, громкости нет */
    float t;
    float avg;
    float rms;
};

class Ear {
public:
    bool music = false;
    float max_avg = 0.0f;

    int open() {
        ops_.AddCallOnce();
        ops_.AddVarHandle();
        ops_.AddReadVariable();
        ops_.AddAssignVariable();
        ops_.AddReshape();
        ops_.AddConcatenation();
        ops_.AddStridedSlice();
        ops_.AddConv2D();
        ops_.AddDepthwiseConv2D();
        ops_.AddAdd();
        ops_.AddSplitV();
        ops_.AddAveragePool2D();
        ops_.AddFullyConnected();
        ops_.AddLogistic();
        ops_.AddQuantize();
        FrontendConfig c;
        c.window.size_ms = 30;
        c.window.step_size_ms = 10;
        c.filterbank.num_channels = 40;
        c.filterbank.lower_band_limit = 125.0f;
        c.filterbank.upper_band_limit = 7500.0f;
        c.noise_reduction.smoothing_bits = 10;
        c.noise_reduction.even_smoothing = 0.025f;
        c.noise_reduction.odd_smoothing = 0.06f;
        c.noise_reduction.min_signal_remaining = 0.05f;
        c.pcan_gain_control.enable_pcan = 1;
        c.pcan_gain_control.strength = 0.95f;
        c.pcan_gain_control.offset = 80.0f;
        c.pcan_gain_control.gain_bits = 21;
        c.log_scale.enable_log = 1;
        c.log_scale.scale_shift = 6;
        if (!FrontendPopulateState(&c, &fe_, 16000)) {
            return -1;
        }
        return reset();
    }

    /* Переменные состояния начинаются с -128 (ноль int8) только через CALL_ONCE,
       а MicroInterpreter::Reset() обнуляет их байтами — поэтому интерпретатор заново. */
    int reset() {
        if (it_) {
            it_->~MicroInterpreter();
            it_ = nullptr;
        }
        tflite::MicroAllocator *ma = tflite::MicroAllocator::Create(arena, sizeof arena);
        tflite::MicroResourceVariables *mrv = tflite::MicroResourceVariables::Create(ma, 20);
        it_ = new (interp_mem) tflite::MicroInterpreter(tflite::GetModel(model_data), ops_, ma, mrv);
        if (it_->AllocateTensors() != kTfLiteOk) {
            return -1;
        }
        in_ = it_->input(0);
        out_ = it_->output(0);
        FrontendReset(&fe_);
        memset(hist_, 0, sizeof hist_);
        hist_pos_ = hist_len_ = 0;
        energy_ = 0;
        probs_n_ = 0;
        buf_n_ = 0;
        frame_ = 0;
        hot_ = false;
        last_fire_ = last_note_ = -10.0f;
        return 0;
    }

    /* Ровно CHUNK отсчётов. */
    Fire feed(const int16_t *s) {
        Fire f = {0, 0, 0, 0};
        for (int i = 0; i < CHUNK; i++) {
            int64_t v = s[i];
            if (hist_len_ == HIST) {
                int64_t old = hist_[hist_pos_];
                energy_ -= old * old;
            } else {
                hist_len_++;
            }
            hist_[hist_pos_] = (int16_t)v;
            hist_pos_ = (hist_pos_ + 1) % HIST;
            energy_ += v * v;
        }
        size_t read = 0;
        FrontendOutput fo = FrontendProcessSamples(&fe_, s, CHUNK, &read);
        if (fo.size == 0) {
            return f;
        }
        int8_t *dst = in_->data.int8 + buf_n_ * 40;
        for (size_t k = 0; k < fo.size && k < 40; k++) {
            float x = (float)fo.values[k] * FEATURE_SCALE;
            long q = lrintf(x / in_->params.scale + (float)in_->params.zero_point);
            dst[k] = (int8_t)(q > 127 ? 127 : q < -128 ? -128 : q);
        }
        frame_++;
        if (++buf_n_ < STRIDE) {
            return f;
        }
        buf_n_ = 0;
        if (it_->Invoke() != kTfLiteOk) {
            return f;
        }
        float p = ((float)out_->data.uint8[0] - (float)out_->params.zero_point) * out_->params.scale;
        p = p < 0.0f ? 0.0f : p > 1.0f ? 1.0f : p;
        probs_[probs_n_ % WINDOW] = p;
        probs_n_++;
        int cnt = probs_n_ < WINDOW ? probs_n_ : WINDOW;
        float avg = 0.0f;
        for (int k = 0; k < cnt; k++) {
            avg += probs_[k];
        }
        avg /= (float)cnt;
        float rms = hist_len_ ? sqrtf((float)energy_ / (float)hist_len_) : 0.0f;
        float t = (float)frame_ * 0.01f;
        if (avg > max_avg) {
            max_avg = avg;
        }
        float floor = music ? RMS_MUSIC : RMS_MIN;
        bool loud = rms >= floor || (avg >= 0.95f && rms >= 2.5f) || (avg >= 0.97f && rms >= 1.5f);
        bool was_hot = hot_;
        if (avg < cutoff - 0.04f) {
            hot_ = false;
        } else if (avg >= cutoff) {
            hot_ = true;
        }
        bool crossed = hot_ && !was_hot;
        if (crossed && loud && t >= 1.0f && t - last_fire_ >= REFRACTORY) {
            last_fire_ = t;
            f = {1, t, avg, rms};
        } else if (avg >= cutoff && !loud && t - last_note_ >= REFRACTORY) {
            last_note_ = t;
            f = {2, t, avg, rms};
        }
        return f;
    }

private:
    tflite::MicroMutableOpResolver<16> ops_;
    tflite::MicroInterpreter *it_ = nullptr;
    TfLiteTensor *in_ = nullptr;
    TfLiteTensor *out_ = nullptr;
    FrontendState fe_;
    int16_t hist_[HIST];
    int hist_pos_ = 0;
    int hist_len_ = 0;
    int64_t energy_ = 0;
    float probs_[WINDOW];
    int probs_n_ = 0;
    int buf_n_ = 0;
    int frame_ = 0;
    bool hot_ = false;
    float last_fire_ = -10.0f;
    float last_note_ = -10.0f;
};

static Ear ear;

static int pcm_file(const char *path, bool mic) {
    FILE *fp = fopen(path, "rb");
    int16_t buf[CHUNK];
    float prev = 0.0f;
    bool first = true;
    int hits = 0;
    if (!fp) {
        fprintf(stderr, "pcm\n");
        return 2;
    }
    while (fread(buf, sizeof(int16_t), CHUNK, fp) == CHUNK) {
        if (mic) {
            for (int i = 0; i < CHUNK; i++) {
                float x = (float)buf[i];
                float y = first ? 0.0f : x - 0.97f * prev;
                first = false;
                prev = x;
                long r = lrintf(y);
                buf[i] = (int16_t)(r > 32767 ? 32767 : r < -32768 ? -32768 : r);
            }
        }
        Fire f = ear.feed(buf);
        if (f.kind == 1) {
            hits++;
            printf("hit t=%.2f avg=%.3f rms=%.0f\n", f.t, f.avg, f.rms);
        } else if (f.kind == 2) {
            printf("quiet t=%.2f avg=%.3f rms=%.0f\n", f.t, f.avg, f.rms);
        }
    }
    fclose(fp);
    printf("hits %d max_avg %.3f cutoff %.2f\n", hits, ear.max_avg, cutoff);
    return hits ? 0 : 1;
}

static int wanted(void) {
    FILE *fp = fopen(DIR "/settings/wake.txt", "r");
    char line[64];
    if (!fp) {
        return 0;
    }
    if (!fgets(line, sizeof line, fp)) {
        line[0] = 0;
    }
    fclose(fp);
    line[strcspn(line, "\r\n")] = 0;
    return strcmp(line, "джарвис-локально") == 0 || strcmp(line, "джарвис локально") == 0;
}

/* housed пишет wake=aec, пока колонка звучит: тогда в кольце выход AEC, порог громкости ниже. */
static bool playing(void) {
    FILE *fp = fopen(DIR "/house.status", "r");
    char line[64];
    bool on = false;
    if (!fp) {
        return false;
    }
    while (fgets(line, sizeof line, fp)) {
        if (strncmp(line, "wake=aec", 8) == 0) {
            on = true;
            break;
        }
    }
    fclose(fp);
    return on;
}

static void tell(int sock, const struct sockaddr_in *to, const char *text) {
    sendto(sock, text, strlen(text), 0, (const struct sockaddr *)to, sizeof *to);
}

static struct CleanRing *open_wake_ring(void) {
    struct stat st;
    int fd = open(DIR "/wake.ring", O_RDONLY);
    if (fd < 0) {
        return nullptr;
    }
    if (fstat(fd, &st) != 0 || st.st_size < (off_t)sizeof(struct CleanRing)) {
        close(fd);
        return nullptr;
    }
    void *map = mmap(nullptr, sizeof(struct CleanRing), PROT_READ, MAP_SHARED, fd, 0);
    close(fd);
    if (map == MAP_FAILED) {
        return nullptr;
    }
    struct CleanRing *ring = (struct CleanRing *)map;
    if (ring->magic != CLEAN_MAGIC || ring->capacity != CLEAN_CAP) {
        munmap(map, sizeof(struct CleanRing));
        return nullptr;
    }
    return ring;
}

static int listen_ring(void) {
    struct CleanRing *ring = nullptr;
    struct yse_host conf;
    struct sockaddr_in to;
    uint32_t read_pos = 0;
    int armed = 0;
    time_t told = 0;
    time_t peeked = 0;
    while (run && !(ring = open_wake_ring())) {
        usleep(200000);
    }
    if (!ring) {
        return 0;
    }
    yse_host_load(&conf);
    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    memset(&to, 0, sizeof to);
    to.sin_family = AF_INET;
    to.sin_port = htons((unsigned short)conf.base);
    inet_pton(AF_INET, conf.ip, &to.sin_addr);
    fprintf(stderr, "jarvis listening cutoff %.2f\n", cutoff);
    while (run) {
        time_t now = time(nullptr);
        if (!wanted()) {
            if (armed) {
                armed = 0;
                fprintf(stderr, "jarvis idle\n");
            }
            usleep(200000);
            continue;
        }
        if (!armed) {
            load_cutoff();
            ear.reset();
            __sync_synchronize();
            read_pos = ring->write_pos;
            armed = 1;
            told = 0;
            fprintf(stderr, "jarvis on cutoff %.2f\n", cutoff);
        }
        if (now - told >= 3) {
            tell(sock, &to, "YSEMODE jarvis");
            told = now;
        }
        uint32_t w = ring->write_pos;
        __sync_synchronize();
        uint32_t avail = w - read_pos;
        if (avail > CLEAN_CAP) {
            read_pos = w;
            ear.reset();
            usleep(20000);
            continue;
        }
        if (avail < CHUNK) {
            usleep(5000);
            continue;
        }
        int16_t buf[CHUNK];
        for (uint32_t i = 0; i < CHUNK; i++) {
            buf[i] = ring->pcm[(read_pos + i) & (CLEAN_CAP - 1u)];
        }
        read_pos += CHUNK;
        Fire f = ear.feed(buf);
        if (now != peeked) {
            ear.music = playing();
            if (ear.max_avg >= 0.5f) {
                fprintf(stderr, "peak %.3f%s\n", ear.max_avg, ear.music ? " music" : "");
            }
            ear.max_avg = 0.0f;
            peeked = now;
        }
        if (f.kind == 1) {
            struct timeval tv;
            char text[64];
            gettimeofday(&tv, nullptr);
            long long ms = (long long)tv.tv_sec * 1000 + tv.tv_usec / 1000;
            snprintf(text, sizeof text, "YSEWAKE %lld jarvis", ms);
            tell(sock, &to, text);
            fprintf(stderr, "hit avg %.3f rms %.0f\n", f.avg, f.rms);
        } else if (f.kind == 2) {
            fprintf(stderr, "quiet avg %.3f rms %.0f\n", f.avg, f.rms);
        }
    }
    munmap(ring, sizeof *ring);
    close(sock);
    return 0;
}

int main(int argc, char **argv) {
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_stop;
    sigaction(SIGTERM, &sa, nullptr);
    sigaction(SIGINT, &sa, nullptr);
    const char *model = getenv("JARVIS_MODEL") ? getenv("JARVIS_MODEL") : MODEL_PATH;
    if (load_model(model) != 0) {
        fprintf(stderr, "model %s\n", model);
        return 2;
    }
    load_cutoff();
    if (ear.open() != 0) {
        fprintf(stderr, "tflm init\n");
        return 2;
    }
    if (argc == 3 && strcmp(argv[1], "--pcm") == 0) {
        return pcm_file(argv[2], false);
    }
    if (argc == 3 && strcmp(argv[1], "--pcm-mic") == 0) {
        return pcm_file(argv[2], true);
    }
    return listen_ring();
}
