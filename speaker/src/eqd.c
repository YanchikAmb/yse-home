#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <sys/select.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#include "mini2_led.h"

#define LEDS 89
#define BANDS 16
#define PORT 18767
#define LIGHT_PATH "/sys/devices/platform/ffd13000.spicc_a/spi_master/spi0/spi0.0/backlight/fb0/brightness"

static volatile sig_atomic_t run = 1;

static void on_stop(int signo) {
    (void)signo;
    run = 0;
}

static int lamp_on(void) {
    FILE *file = fopen("/data/opt/yandex-station/settings/lamp.txt", "r");
    int on = 0;
    if (!file) {
        return 0;
    }
    on = fgetc(file) == '1';
    fclose(file);
    return on;
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
    fd = open(LIGHT_PATH, O_WRONLY | O_CLOEXEC);
    if (fd < 0) {
        return;
    }
    n = snprintf(buf, sizeof buf, "%d", value);
    if (n > 0 && write(fd, buf, (size_t)n) != n) {
        /* keep the previous brightness */
    }
    close(fd);
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

static void read_face(int *red, int *green, int *blue, int *bright) {
    FILE *file;
    char text[16];
    int value;
    *red = 58;
    *green = 160;
    *blue = 255;
    *bright = 160;
    file = fopen("/data/opt/yandex-station/settings/ring.color", "r");
    if (file) {
        if (fscanf(file, "%6s", text) == 1 && strlen(text) == 6) {
            value = hex2(text);
            if (value >= 0) {
                *red = value;
            }
            value = hex2(text + 2);
            if (value >= 0) {
                *green = value;
            }
            value = hex2(text + 4);
            if (value >= 0) {
                *blue = value;
            }
        }
        fclose(file);
    }
    file = fopen("/data/opt/yandex-station/settings/ring.bright", "r");
    if (file) {
        if (fscanf(file, "%d", &value) == 1 && value >= 0 && value <= 255) {
            *bright = value;
        }
        fclose(file);
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
        /* the next packet tries again */
    }
}

static void draw(int fd, const unsigned char *levels, int red, int green, int blue) {
    unsigned char frame[LEDS * 4];
    int led;
    for (led = 0; led < LEDS; led++) {
        int band = led * BANDS / LEDS;
        int slot = led % (LEDS / BANDS);
        int level = levels[band];
        if (slot == 0) {
            level = level / 6;
        }
        frame[led * 4 + 0] = (unsigned char)(red * level / 255);
        frame[led * 4 + 1] = (unsigned char)(green * level / 255);
        frame[led * 4 + 2] = (unsigned char)(blue * level / 255);
        frame[led * 4 + 3] = 0;
    }
    paint(fd, frame);
}

static void clear_frame(int fd) {
    unsigned char frame[LEDS * 4];
    memset(frame, 0, sizeof frame);
    paint(fd, frame);
}

int main(void) {
    int fd;
    int sock;
    struct sockaddr_in addr;
    struct sigaction sa;
    unsigned char hold[BANDS];
    unsigned char packet[BANDS];
    int red;
    int green;
    int blue;
    int bright;
    int lit = 0;
    int ticks = 0;
    fd = open("/dev/fb0", O_RDWR | O_CLOEXEC);
    if (fd < 0 && mini2_present()) {
        fd = -1;
    } else if (fd < 0) {
        fprintf(stderr, "fb0: %s\n", strerror(errno));
        return 1;
    }
    sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) {
        if (fd >= 0) {
            close(fd);
        }
        return 1;
    }
    memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET;
    addr.sin_port = htons(PORT);
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    if (bind(sock, (struct sockaddr *)&addr, sizeof addr) != 0) {
        fprintf(stderr, "bind: %s\n", strerror(errno));
        close(sock);
        if (fd >= 0) {
            close(fd);
        }
        return 1;
    }
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_stop;
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);
    memset(hold, 0, sizeof hold);
    read_face(&red, &green, &blue, &bright);
    face_gain = bright;
    fprintf(stderr, "eq listening\n");
    while (run) {
        struct timeval wait;
        fd_set set;
        int ready;
        ssize_t got;
        int i;
        int energy = 0;
        FD_ZERO(&set);
        FD_SET(sock, &set);
        wait.tv_sec = 0;
        wait.tv_usec = 200000;
        ready = select(sock + 1, &set, NULL, NULL, &wait);
        if (ready < 0) {
            if (errno == EINTR) {
                continue;
            }
            break;
        }
        if (lamp_on()) {
            if (ready > 0) {
                recv(sock, packet, sizeof packet, 0);
            }
            lit = 0;
            continue;
        }
        if (ready == 0) {
            if (lit) {
                memset(hold, 0, sizeof hold);
                clear_frame(fd);
                set_light(0);
                lit = 0;
            }
            continue;
        }
        got = recv(sock, packet, sizeof packet, 0);
        if (got != BANDS) {
            continue;
        }
        if ((ticks++ % 25) == 0) {
            read_face(&red, &green, &blue, &bright);
            face_gain = bright;
        }
        for (i = 0; i < BANDS; i++) {
            if (packet[i] > hold[i]) {
                hold[i] = packet[i];
            } else {
                hold[i] = (unsigned char)(hold[i] * 3 / 4);
            }
            energy += hold[i];
        }
        if (energy < 40) {
            if (lit) {
                clear_frame(fd);
                set_light(0);
                lit = 0;
            }
            continue;
        }
        if (!lit) {
            set_light(bright);
            lit = 1;
        }
        draw(fd, hold, red, green, blue);
    }
    clear_frame(fd);
    set_light(0);
    close(sock);
    if (fd >= 0) {
        close(fd);
    }
    return 0;
}
