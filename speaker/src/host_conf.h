#ifndef YSE_HOST_CONF_H
#define YSE_HOST_CONF_H

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* settings/host.conf: строки host=IP и base=PORT. От base идут порты сервера:
   base слово, base+1 музыка, base+2 сбор записей для обучения слова, base+3 ответы,
   base+5 поток фразы после слова. Без файла — адреса спальни. */
#define YSE_HOST_CONF "/data/opt/yandex-station/settings/host.conf"
#define YSE_DEFAULT_HOST "192.168.209.50"
#define YSE_DEFAULT_BASE 18765

struct yse_host {
    char ip[64];
    int base;
};

__attribute__((unused)) static void yse_host_load(struct yse_host *conf) {
    FILE *file;
    char line[128];
    snprintf(conf->ip, sizeof conf->ip, "%s", YSE_DEFAULT_HOST);
    conf->base = YSE_DEFAULT_BASE;
    file = fopen(YSE_HOST_CONF, "r");
    if (!file) {
        return;
    }
    while (fgets(line, sizeof line, file)) {
        size_t n = strcspn(line, "\r\n");
        line[n] = 0;
        if (strncmp(line, "host=", 5) == 0 && line[5] && strspn(line + 5, "0123456789.") == strlen(line + 5)) {
            snprintf(conf->ip, sizeof conf->ip, "%s", line + 5);
        } else if (strncmp(line, "base=", 5) == 0) {
            int base = atoi(line + 5);
            if (base >= 1024 && base <= 65000) {
                conf->base = base;
            }
        }
    }
    fclose(file);
}

__attribute__((unused)) static const char *yse_url(char *out, size_t cap, int offset, const char *path) {
    struct yse_host conf;
    yse_host_load(&conf);
    snprintf(out, cap, "http://%s:%d%s", conf.ip, conf.base + offset, path ? path : "");
    return out;
}

#define YSE_MUSIC 1
#define YSE_TRAIN 2
#define YSE_TALK 3
#define YSE_LISTEN 5

#endif
