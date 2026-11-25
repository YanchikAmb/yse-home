/* Будильники колонки: alarms.txt, по одному на строку.
     <next> <id> <days> <HH:MM> <on|off> <подпись>
   next — unix-время ближайшего звонка, 0 у выключенного. days — семь знаков 0/1 с понедельника
   или "-" у разового. Строка из одного числа (старый формат, её же пишет ear.c) — разовый будильник.
   Местное время — UTC плюс settings/clock.off часов (по умолчанию 3). */
#ifndef YSE_ALARMS_H
#define YSE_ALARMS_H

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#ifndef ALARMS_FILE
#define ALARMS_FILE "/data/opt/yandex-station/alarms.txt"
#endif
#define ALARMS_MAX 16
#define ALARM_LABEL 48

struct alarm_item {
    long long next;
    long long id;
    char days[8];
    int hour;
    int minute;
    int on;
    char label[ALARM_LABEL];
};

static inline int alarm_tz(void) {
    FILE *fp = fopen("/data/opt/yandex-station/settings/clock.off", "r");
    int off = 3;
    if (fp) {
        if (fscanf(fp, "%d", &off) != 1 || off < -12 || off > 14) {
            off = 3;
        }
        fclose(fp);
    }
    return off * 3600;
}

static inline int alarm_days_ok(const char *days) {
    int i;
    int any = 0;
    if (strcmp(days, "-") == 0) {
        return 1;
    }
    if (strlen(days) != 7) {
        return 0;
    }
    for (i = 0; i < 7; i++) {
        if (days[i] != '0' && days[i] != '1') {
            return 0;
        }
        any |= days[i] == '1';
    }
    return any;
}

/* Ближайшее время hour:minute позже now в разрешённый день. 0 — таких дней нет. */
static inline long long alarm_next_after(time_t now, int hour, int minute, const char *days, int tz) {
    long long local = (long long)now + tz;
    long long midnight = local - (local % 86400);
    int wday = (int)((local / 86400 + 3) % 7); /* 01.01.1970 — четверг, 0 — понедельник */
    int d;
    for (d = 0; d <= 7; d++) {
        long long at = midnight + (long long)d * 86400 + hour * 3600 + minute * 60;
        if (at <= local) {
            continue;
        }
        if (days[0] == '-' || days[(wday + d) % 7] == '1') {
            return at - tz;
        }
    }
    return 0;
}

static inline void alarm_clean_label(char *label) {
    size_t n;
    char *p;
    for (p = label; *p; p++) {
        if (*p == '\n' || *p == '\r' || *p == '\t' || *p == '"' || *p == '\\' || (unsigned char)*p < 0x20) {
            *p = ' ';
        }
    }
    n = strlen(label);
    if (n >= ALARM_LABEL) {
        n = ALARM_LABEL - 1;
        while (n > 0 && ((unsigned char)label[n] & 0xC0) == 0x80) {
            n--;
        }
        label[n] = 0;
    }
    while (n > 0 && label[n - 1] == ' ') {
        label[--n] = 0;
    }
}

static inline int alarm_parse(const char *line, struct alarm_item *it, int tz) {
    char days[16] = "";
    char hm[16] = "";
    char st[16] = "";
    int used = 0;
    int n;
    memset(it, 0, sizeof *it);
    n = sscanf(line, "%lld %lld %15s %15s %15s %n", &it->next, &it->id, days, hm, st, &used);
    if (n == 5) {
        if (!alarm_days_ok(days) || sscanf(hm, "%d:%d", &it->hour, &it->minute) != 2
            || it->hour < 0 || it->hour > 23 || it->minute < 0 || it->minute > 59) {
            return -1;
        }
        snprintf(it->days, sizeof it->days, "%s", days);
        it->on = strcmp(st, "on") == 0;
        if (used > 0) {
            snprintf(it->label, sizeof it->label, "%s", line + used);
        }
        alarm_clean_label(it->label);
        return 0;
    }
    if (n >= 1 && it->next > 0) {
        long long local = it->next + tz;
        it->id = it->next;
        snprintf(it->days, sizeof it->days, "-");
        it->hour = (int)((local % 86400) / 3600);
        it->minute = (int)((local % 3600) / 60);
        it->on = 1;
        it->label[0] = 0;
        return 0;
    }
    return -1;
}

static inline int alarms_load(struct alarm_item *items, int cap) {
    FILE *fp = fopen(ALARMS_FILE, "r");
    char line[160];
    int tz = alarm_tz();
    int n = 0;
    if (!fp) {
        return 0;
    }
    while (n < cap && fgets(line, sizeof line, fp)) {
        if (alarm_parse(line, &items[n], tz) == 0) {
            n++;
        }
    }
    fclose(fp);
    return n;
}

static inline int alarms_save(const struct alarm_item *items, int n) {
    char tmp[96];
    FILE *fp;
    int i;
    snprintf(tmp, sizeof tmp, "%s.%d", ALARMS_FILE, (int)getpid());
    fp = fopen(tmp, "w");
    if (!fp) {
        return -1;
    }
    for (i = 0; i < n; i++) {
        fprintf(fp, "%lld %lld %s %02d:%02d %s %s\n", items[i].on ? items[i].next : 0LL, items[i].id,
                items[i].days, items[i].hour, items[i].minute, items[i].on ? "on" : "off", items[i].label);
    }
    if (fclose(fp) != 0) {
        unlink(tmp);
        return -1;
    }
    return rename(tmp, ALARMS_FILE);
}

/* Включённые по времени звонка, за ними выключенные по часам. */
static inline void alarms_sort(struct alarm_item *items, int n) {
    int i;
    for (i = 1; i < n; i++) {
        struct alarm_item key = items[i];
        int j = i;
        while (j > 0) {
            const struct alarm_item *a = &items[j - 1];
            int a_off = !a->on || a->next <= 0;
            int k_off = !key.on || key.next <= 0;
            int later = a_off != k_off ? a_off > k_off
                      : !a_off ? a->next > key.next
                      : a->hour * 60 + a->minute > key.hour * 60 + key.minute;
            if (!later) {
                break;
            }
            items[j] = items[j - 1];
            j--;
        }
        items[j] = key;
    }
}

#endif
