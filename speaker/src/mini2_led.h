#ifndef YSE_MINI2_LED_H
#define YSE_MINI2_LED_H

/* Мини 2: кольцо логотипа mini2:{red,green,blue}:ring1..6, шкала 0..255.
   Часы — mini2:white:clock*, шкала 0..170, как заводской led_clock_on.
   Станции 2 этого каталога нет. */

int yse_playback_value(const char *line);
int mini2_present(void);
void mini2_show(int red, int green, int blue);
void mini2_clock(int hour, int minute, int colon, int bright);
void mini2_digits(int left, int right, int colon, int bright);
void mini2_temp(int value, int bright);

#endif
