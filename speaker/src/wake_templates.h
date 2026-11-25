#pragma once
#define WAKE_NTEMP 3
#define WAKE_NNEG 8
#define WAKE_MAXF 96
static const float wake_pos_max = 1.35000f;
/* Живое слово с записи: 0.19, второй образец 0.35.
   Ложные срабатывания в комнате начинались с 0.39. */
static const float wake_fire = 0.34000f;
static const float wake_second = 0.50000f;
static const float wake_peak_frac = 0.08000f;
/* Гул комнаты ниже 0.10. Живое слово от 0.18. */
static const float wake_zcr_min = 0.15000f;
static const int wake_tlen[WAKE_NTEMP] = {37, 42, 35};
static const int wake_nlen[WAKE_NNEG] = {63, 50, 67, 65, 76, 51, 56, 58};
