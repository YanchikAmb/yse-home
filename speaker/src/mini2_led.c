#define _GNU_SOURCE
#include "mini2_led.h"

#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int yse_playback_value(const char *line) {
    const char *mark;
    int value;
    mark = strstr(line, "Playback ");
    if (!mark || !strchr(line, '[')) {
        return -1;
    }
    value = atoi(mark + 9);
    if (value < 0 || value > 255) {
        return -1;
    }
    return value;
}

int mini2_present(void) {
    return access("/sys/class/leds/mini2:red:ring1/brightness", W_OK) == 0;
}

static int ring_fd[18];
static int ring_ready;

static void ring_open(void) {
    static const char *color[] = {"red", "green", "blue"};
    int i;
    int color_i;
    char path[80];
    if (ring_ready) {
        return;
    }
    for (i = 0; i < 18; i++) {
        ring_fd[i] = -1;
    }
    for (color_i = 0; color_i < 3; color_i++) {
        for (i = 1; i <= 6; i++) {
            int slot = color_i * 6 + (i - 1);
            snprintf(path, sizeof path, "/sys/class/leds/mini2:%s:ring%d/brightness", color[color_i], i);
            ring_fd[slot] = open(path, O_WRONLY | O_CLOEXEC);
        }
    }
    ring_ready = 1;
}

static void ring_write(int slot, int value) {
    char buf[8];
    int n;
    if (slot < 0 || slot >= 18 || ring_fd[slot] < 0) {
        return;
    }
    if (value < 0) {
        value = 0;
    }
    if (value > 255) {
        value = 255;
    }
    n = snprintf(buf, sizeof buf, "%d\n", value);
    if (lseek(ring_fd[slot], 0, SEEK_SET) < 0) {
        return;
    }
    if (write(ring_fd[slot], buf, (size_t)n) != n) {
        return;
    }
}

void mini2_show(int red, int green, int blue) {
    int i;
    int color[3];
    if (red < 0) {
        red = 0;
    }
    if (green < 0) {
        green = 0;
    }
    if (blue < 0) {
        blue = 0;
    }
    if (red > 255) {
        red = 255;
    }
    if (green > 255) {
        green = 255;
    }
    if (blue > 255) {
        blue = 255;
    }
    color[0] = red;
    color[1] = green;
    color[2] = blue;
    ring_open();
    for (i = 0; i < 18; i++) {
        ring_write(i, color[i / 6]);
    }
}

static int clock_fd[34];
static int clock_ready;

static void clock_open(void) {
    static const char *seg[] = {"A", "B", "C", "D", "E", "F", "G"};
    static const char *extra[] = {"PLUS1", "PLUS2", "PLUS3", "PLUS4", "MINUS", "DEGREE"};
    int digit;
    int bit;
    int i;
    char path[80];
    if (clock_ready) {
        return;
    }
    for (i = 0; i < 34; i++) {
        clock_fd[i] = -1;
    }
    for (digit = 1; digit <= 4; digit++) {
        for (bit = 0; bit < 7; bit++) {
            snprintf(path, sizeof path, "/sys/class/leds/mini2:white:clock%s%d/brightness", seg[bit], digit);
            clock_fd[(digit - 1) * 7 + bit] = open(path, O_WRONLY | O_CLOEXEC);
        }
    }
    for (i = 0; i < 6; i++) {
        snprintf(path, sizeof path, "/sys/class/leds/mini2:white:clock%s/brightness", extra[i]);
        clock_fd[28 + i] = open(path, O_WRONLY | O_CLOEXEC);
    }
    clock_ready = 1;
}

static void clock_write(int slot, int value) {
    char buf[8];
    int n;
    if (slot < 0 || slot >= 34 || clock_fd[slot] < 0) {
        return;
    }
    if (value < 0) {
        value = 0;
    }
    if (value > 170) {
        value = 170;
    }
    n = snprintf(buf, sizeof buf, "%d\n", value);
    if (lseek(clock_fd[slot], 0, SEEK_SET) < 0) {
        return;
    }
    if (write(clock_fd[slot], buf, (size_t)n) != n) {
        return;
    }
}

void mini2_digits(int left, int right, int colon, int bright) {
    static const unsigned char mask[10] = {
        0x3f, 0x06, 0x5b, 0x4f, 0x66, 0x6d, 0x7d, 0x07, 0x7f, 0x6f
    };
    int digits[4];
    int i;
    int bit;
    if (bright < 0) {
        bright = 0;
    }
    if (bright > 170) {
        bright = 170;
    }
    if (left < 0 || left > 99 || right < 0 || right > 99) {
        digits[0] = digits[1] = digits[2] = digits[3] = -1;
    } else {
        digits[0] = left >= 10 ? left / 10 : -1;
        digits[1] = left % 10;
        digits[2] = right / 10;
        digits[3] = right % 10;
    }
    clock_open();
    for (i = 0; i < 4; i++) {
        unsigned char bits = digits[i] < 0 ? 0 : mask[digits[i]];
        for (bit = 0; bit < 7; bit++) {
            clock_write(i * 7 + bit, (bits & (1u << bit)) ? bright : 0);
        }
    }
    clock_write(28, colon ? bright : 0);
    clock_write(29, colon ? bright : 0);
    clock_write(30, 0);
    clock_write(31, 0);
    clock_write(32, 0);
    clock_write(33, 0);
}

void mini2_clock(int hour, int minute, int colon, int bright) {
    static const unsigned char mask[10] = {
        0x3f, 0x06, 0x5b, 0x4f, 0x66, 0x6d, 0x7d, 0x07, 0x7f, 0x6f
    };
    int digits[4];
    int i;
    int bit;
    if (bright < 0) {
        bright = 0;
    }
    if (bright > 170) {
        bright = 170;
    }
    if (hour < 0 || hour > 23 || minute < 0 || minute > 59) {
        digits[0] = digits[1] = digits[2] = digits[3] = -1;
    } else {
        digits[0] = hour / 10;
        digits[1] = hour % 10;
        digits[2] = minute / 10;
        digits[3] = minute % 10;
    }
    clock_open();
    for (i = 0; i < 4; i++) {
        unsigned char bits = digits[i] < 0 ? 0 : mask[digits[i]];
        for (bit = 0; bit < 7; bit++) {
            clock_write(i * 7 + bit, (bits & (1u << bit)) ? bright : 0);
        }
    }
    clock_write(28, colon ? bright : 0);
    clock_write(29, colon ? bright : 0);
    clock_write(30, 0);
    clock_write(31, 0);
    clock_write(32, 0);
    clock_write(33, 0);
}

/* Температура: « 23°», «-5°», «-12°». Градус — верхний квадрат четвёртой цифры. */
void mini2_temp(int value, int bright) {
    static const unsigned char mask[10] = {
        0x3f, 0x06, 0x5b, 0x4f, 0x66, 0x6d, 0x7d, 0x07, 0x7f, 0x6f
    };
    unsigned char cells[4] = {0, 0, 0, 0x63};
    int mag = value < 0 ? -value : value;
    int i;
    int bit;
    if (bright < 0) {
        bright = 0;
    }
    if (bright > 170) {
        bright = 170;
    }
    if (mag > 99) {
        mag = 99;
    }
    cells[2] = mask[mag % 10];
    if (mag >= 10) {
        cells[1] = mask[mag / 10];
        if (value < 0) {
            cells[0] = 0x40;
        }
    } else if (value < 0) {
        cells[1] = 0x40;
    }
    clock_open();
    for (i = 0; i < 4; i++) {
        for (bit = 0; bit < 7; bit++) {
            clock_write(i * 7 + bit, (cells[i] & (1u << bit)) ? bright : 0);
        }
    }
    for (i = 28; i < 34; i++) {
        clock_write(i, 0);
    }
}
