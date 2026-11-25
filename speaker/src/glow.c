#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "mini2_led.h"

#define LEDS 89
#define PID_PATH "/data/opt/yandex-station/glow.pid"
#define LIGHT_PATH "/sys/devices/platform/ffd13000.spicc_a/spi_master/spi0/spi0.0/backlight/fb0/brightness"

static volatile sig_atomic_t run = 1;

static void on_stop(int signo) {
    (void)signo;
    run = 0;
}

static int hex2(const char *text) {
    int value = 0;
    int i;
    for (i = 0; i < 2; i++) {
        char c = text[i];
        int digit;
        if (c >= '0' && c <= '9') {
            digit = c - '0';
        } else if (c >= 'a' && c <= 'f') {
            digit = c - 'a' + 10;
        } else if (c >= 'A' && c <= 'F') {
            digit = c - 'A' + 10;
        } else {
            return -1;
        }
        value = (value << 4) | digit;
    }
    return value;
}

static void set_light(int value) {
    char buf[16];
    int fd;
    int n;
    if (value < 0) {
        value = 0;
    }
    if (value > 255) {
        value = 255;
    }
    fd = open(LIGHT_PATH, O_WRONLY);
    if (fd < 0) {
        return;
    }
    n = snprintf(buf, sizeof buf, "%d", value);
    if (n > 0 && write(fd, buf, (size_t)n) != n) {
        /* the panel keeps the previous brightness */
    }
    close(fd);
}

static int effect_code(const char *name) {
    if (name && strcmp(name, "breathe") == 0) {
        return 1;
    }
    if (name && strcmp(name, "spin") == 0) {
        return 2;
    }
    if (name && strcmp(name, "fill") == 0) {
        return 3;
    }
    if (name && strcmp(name, "spark") == 0) {
        return 4;
    }
    if (name && strcmp(name, "shift") == 0) {
        return 5;
    }
    if (name && strcmp(name, "comet") == 0) {
        return 6;
    }
    if (name && strcmp(name, "aurora") == 0) {
        return 7;
    }
    if (name && strcmp(name, "twinkle") == 0) {
        return 8;
    }
    return 0;
}

static void hue_rgb(int hue, int *red, int *green, int *blue) {
    int region;
    int rise;
    int fall;
    hue %= 360;
    if (hue < 0) {
        hue += 360;
    }
    region = hue / 60;
    rise = (hue % 60) * 255 / 60;
    fall = 255 - rise;
    if (region == 0) {
        *red = 255;
        *green = rise;
        *blue = 0;
    } else if (region == 1) {
        *red = fall;
        *green = 255;
        *blue = 0;
    } else if (region == 2) {
        *red = 0;
        *green = 255;
        *blue = rise;
    } else if (region == 3) {
        *red = 0;
        *green = fall;
        *blue = 255;
    } else if (region == 4) {
        *red = rise;
        *green = 0;
        *blue = 255;
    } else {
        *red = 255;
        *green = 0;
        *blue = fall;
    }
}

static int face_gain = 255;

static void paint(int fd, const unsigned char *frame) {
    int red = 0;
    int green = 0;
    int blue = 0;
    int led;
    if (fd < 0) {
        for (led = 0; led < LEDS; led++) {
            red += frame[led * 4 + 0];
            green += frame[led * 4 + 1];
            blue += frame[led * 4 + 2];
        }
        mini2_show(red / LEDS * face_gain / 255, green / LEDS * face_gain / 255, blue / LEDS * face_gain / 255);
        return;
    }
    if (lseek(fd, 0, SEEK_SET) < 0) {
        return;
    }
    if (write(fd, frame, LEDS * 4) < 0) {
        /* the next frame tries again */
    }
}

static int lamp_hold(int *red, int *green, int *blue, int *bright) {
    FILE *file;
    char on[8];
    char color[16];
    char text[16];
    int value = 180;
    file = fopen("/data/opt/yandex-station/settings/lamp.txt", "r");
    if (!file) {
        return 0;
    }
    if (!fgets(on, sizeof on, file) || on[0] != '1') {
        fclose(file);
        return 0;
    }
    fclose(file);
    file = fopen("/data/opt/yandex-station/settings/lamp.color", "r");
    if (!file || !fgets(color, sizeof color, file) || strlen(color) < 6) {
        if (file) {
            fclose(file);
        }
        return 0;
    }
    fclose(file);
    *red = hex2(color);
    *green = hex2(color + 2);
    *blue = hex2(color + 4);
    file = fopen("/data/opt/yandex-station/settings/lamp.bright", "r");
    if (file) {
        if (fgets(text, sizeof text, file)) {
            value = atoi(text);
        }
        fclose(file);
    }
    if (*red < 0 || *green < 0 || *blue < 0 || value <= 0 || value > 255) {
        return 0;
    }
    *bright = value;
    return 1;
}

static void paint_solid(int fd, int red, int green, int blue) {
    unsigned char buf[LEDS * 4];
    int led;
    for (led = 0; led < LEDS; led++) {
        buf[led * 4 + 0] = (unsigned char)red;
        buf[led * 4 + 1] = (unsigned char)green;
        buf[led * 4 + 2] = (unsigned char)blue;
        buf[led * 4 + 3] = 0;
    }
    paint(fd, buf);
}

static void clear_pid(void) {
    FILE *file;
    int pid = 0;
    file = fopen(PID_PATH, "r");
    if (!file) {
        return;
    }
    if (fscanf(file, "%d", &pid) == 1 && pid == getpid()) {
        fclose(file);
        unlink(PID_PATH);
        return;
    }
    fclose(file);
}

int main(int argc, char **argv) {
    int red;
    int green;
    int blue;
    int ms;
    int fd;
    int frames;
    int frame;
    int effect = 0;
    int bright = -1;
    int hold = 0;
    int old = 0;
    FILE *file;
    struct sigaction sa;
    unsigned char blank[LEDS * 4];
    if (argc == 4 && strcmp(argv[2], "hold") == 0 && strlen(argv[1]) == 6) {
        hold = 1;
        bright = atoi(argv[3]);
        ms = 0;
    } else if ((argc != 3 && argc != 5) || strlen(argv[1]) != 6) {
        fprintf(stderr, "usage: glow RRGGBB milliseconds [effect bright] | glow RRGGBB hold bright\n");
        return 2;
    }
    red = hex2(argv[1]);
    green = hex2(argv[1] + 2);
    blue = hex2(argv[1] + 4);
    if (!hold) {
        ms = atoi(argv[2]);
        effect = argc == 5 ? effect_code(argv[3]) : 0;
        bright = argc == 5 ? atoi(argv[4]) : -1;
    }
    if (red < 0 || green < 0 || blue < 0) {
        return 2;
    }
    if (hold && (bright < 1 || bright > 255)) {
        return 2;
    }
    if (!hold && (ms < 200 || ms > 20000)) {
        return 2;
    }
    if (!hold && argc == 5 && (bright < 0 || bright > 255)) {
        return 2;
    }
    file = fopen(PID_PATH, "r");
    if (file) {
        if (fscanf(file, "%d", &old) == 1 && old > 1 && old != getpid()) {
            kill(old, SIGTERM);
        }
        fclose(file);
    }
    file = fopen(PID_PATH, "w");
    if (file) {
        fprintf(file, "%d\n", getpid());
        fclose(file);
    }
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_stop;
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);
    fd = open("/dev/fb0", O_RDWR | O_CLOEXEC);
    if (fd < 0 && mini2_present()) {
        fd = -1;
        if (bright >= 0) {
            face_gain = bright;
        }
    } else if (fd < 0) {
        fprintf(stderr, "fb0: %s\n", strerror(errno));
        clear_pid();
        return 1;
    }
    if (fd >= 0 && bright >= 0) {
        set_light(bright);
    }
    if (hold) {
        while (run) {
            paint_solid(fd, red, green, blue);
            usleep(200000);
        }
    }
    frames = ms / 20;
    for (frame = 0; run && !hold && frame < frames; frame++) {
        unsigned char buf[LEDS * 4];
        int phase = frame % 80;
        int led;
        for (led = 0; led < LEDS; led++) {
            int level = 0;
            int use_red = red;
            int use_green = green;
            int use_blue = blue;
            if (effect == 5 || effect == 7) {
                int hue = effect == 5 ? (led * 360 / LEDS + frame * 6) % 360 : (frame * 3 + led * 4) % 360;
                int hr;
                int hg;
                int hb;
                hue_rgb(hue, &hr, &hg, &hb);
                if (effect == 7) {
                    use_red = (hr + red) / 2;
                    use_green = (hg + green) / 2;
                    use_blue = (hb + blue) / 2;
                } else {
                    use_red = hr;
                    use_green = hg;
                    use_blue = hb;
                }
                level = 150 + ((led * 5 + frame * 3) % 106);
            } else if (effect == 8) {
                int n = (led * 1103515245 + (frame / 4) * 12345) & 0x7fffffff;
                level = (n % 19) == 0 ? 255 : 18;
            } else if (effect == 1) {
                level = phase < 40 ? 50 + phase * 5 : 50 + (80 - phase) * 5;
            } else if (effect == 3) {
                level = phase < 40 ? 170 + phase : 170 + (80 - phase);
            } else if (effect == 4) {
                int spark = (frame * 17 + led * 31) % 89;
                level = spark < 14 ? 255 : 24;
            } else {
                int speed = effect == 6 ? 70 : (effect == 2 ? 40 : 22);
                int tail = effect == 6 ? 4 * 16 : (effect == 2 ? 6 * 16 : 18 * 16);
                int floor = (effect == 2 || effect == 6) ? 0 : (phase < 40 ? 18 + phase / 3 : 18 + (80 - phase) / 3);
                int head = (frame * speed) % (LEDS * 16);
                int pos = led * 16 + 8;
                int dist = pos - head;
                if (dist < 0) {
                    dist = -dist;
                }
                if (dist > LEDS * 8) {
                    dist = LEDS * 16 - dist;
                }
                if (dist >= tail) {
                    level = floor;
                } else {
                    int linear = 255 - dist * 255 / tail;
                    level = linear * linear / 255;
                    if (level < floor) {
                        level = floor;
                    }
                }
            }
            if (level > 255) {
                level = 255;
            }
            buf[led * 4 + 0] = (unsigned char)(use_red * level / 255);
            buf[led * 4 + 1] = (unsigned char)(use_green * level / 255);
            buf[led * 4 + 2] = (unsigned char)(use_blue * level / 255);
            buf[led * 4 + 3] = 0;
        }
        paint(fd, buf);
        usleep(20000);
    }
    if (run && !hold) {
        int stay_red = 0;
        int stay_green = 0;
        int stay_blue = 0;
        int stay_bright = 0;
        if (lamp_hold(&stay_red, &stay_green, &stay_blue, &stay_bright)) {
            set_light(stay_bright);
            while (run) {
                paint_solid(fd, stay_red, stay_green, stay_blue);
                usleep(200000);
            }
        }
    }
    memset(blank, 0, sizeof blank);
    paint(fd, blank);
    if (bright >= 0) {
        set_light(0);
    }
    if (fd >= 0) {
        close(fd);
    }
    clear_pid();
    return 0;
}
