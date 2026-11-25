#define _GNU_SOURCE
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "clean_ring.h"

static const char pid_path[] = "/data/opt/yandex-station/bt.pid";
static const char bt_path[] = "/data/opt/yandex-station/settings/bt.txt";
static const char ring_path[] = "/data/opt/yandex-station/clean.ring";

static volatile sig_atomic_t run = 1;
static pid_t up_pid = -1;
static pid_t down_rec = -1;
static pid_t down_play = -1;
static int up_fd = -1;
static struct CleanRing *ring;
static uint32_t read_pos;
static int link_rate;

static void on_stop(int signo) {
    (void)signo;
    run = 0;
}

static int headset_on(void) {
    char buf[8];
    int fd = open(bt_path, O_RDONLY);
    ssize_t n;
    if (fd < 0) {
        return 0;
    }
    n = read(fd, buf, sizeof buf - 1);
    close(fd);
    return n > 0 && buf[0] == '1';
}

static int run_quiet(char *const argv[]) {
    pid_t pid = fork();
    int status;
    if (pid < 0) {
        return -1;
    }
    if (pid == 0) {
        int fd = open("/dev/null", O_RDWR);
        if (fd >= 0) {
            dup2(fd, 0);
            dup2(fd, 1);
            dup2(fd, 2);
        }
        execvp(argv[0], argv);
        _exit(127);
    }
    if (waitpid(pid, &status, 0) < 0) {
        return -1;
    }
    return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}

static int bt_text(const char *text) {
    int pipes[2];
    pid_t pid;
    int status;
    if (pipe(pipes) != 0) {
        return -1;
    }
    pid = fork();
    if (pid < 0) {
        return -1;
    }
    if (pid == 0) {
        int fd = open("/dev/null", O_RDWR);
        dup2(pipes[0], 0);
        if (fd >= 0) {
            dup2(fd, 1);
            dup2(fd, 2);
        }
        close(pipes[1]);
        execlp("bluetoothctl", "bluetoothctl", (char *)NULL);
        _exit(127);
    }
    close(pipes[0]);
    if (write(pipes[1], text, strlen(text)) < 0) {
        /* bluetoothctl still exits on its own */
    }
    close(pipes[1]);
    if (waitpid(pid, &status, 0) < 0) {
        return -1;
    }
    return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}

static void stop_pidfile(void) {
    char buf[32];
    int fd = open(pid_path, O_RDONLY);
    ssize_t n;
    pid_t pid;
    if (fd < 0) {
        return;
    }
    n = read(fd, buf, sizeof buf - 1);
    close(fd);
    if (n <= 0) {
        return;
    }
    buf[n] = 0;
    pid = (pid_t)atoi(buf);
    if (pid <= 1 || pid == getpid()) {
        return;
    }
    if (kill(pid, 0) != 0) {
        unlink(pid_path);
        return;
    }
    kill(-pid, SIGTERM);
    kill(pid, SIGTERM);
    sleep(1);
    kill(-pid, SIGKILL);
    kill(pid, SIGKILL);
    unlink(pid_path);
}

static int restart_audio(void) {
    char *stop_play[] = {"/etc/init.d/S53bluealsa-aplay", "stop", NULL};
    char *stop_alsa[] = {"/etc/init.d/S52bluealsa", "stop", NULL};
    char *start_alsa[] = {"/etc/init.d/S52bluealsa", "start", NULL};
    char *start_play[] = {"/etc/init.d/S53bluealsa-aplay", "start", NULL};
    run_quiet(stop_play);
    if (run_quiet(stop_alsa) != 0) {
        return -1;
    }
    if (run_quiet(start_alsa) != 0) {
        return -1;
    }
    run_quiet(start_play);
    return 0;
}

static int hex_pair(const char *p) {
    return isxdigit((unsigned char)p[0]) && isxdigit((unsigned char)p[1]);
}

static int parse_dev(const char *at, char *addr) {
    int i;
    if (strncmp(at, "dev_", 4) != 0) {
        return -1;
    }
    at += 4;
    for (i = 0; i < 6; i++) {
        if (!hex_pair(at)) {
            return -1;
        }
        addr[i * 3] = at[0];
        addr[i * 3 + 1] = at[1];
        addr[i * 3 + 2] = (i == 5) ? 0 : ':';
        at += 2;
        if (i < 5) {
            if (*at != '_') {
                return -1;
            }
            at++;
        }
    }
    return 0;
}

static int find_sco(char *addr, int *rate) {
    FILE *fp;
    char *buf;
    char *found;
    size_t got;
    int ok = -1;
    buf = malloc(131072);
    if (!buf) {
        return -1;
    }
    fp = popen(
        "dbus-send --system --dest=org.bluealsa --print-reply /org/bluealsa "
        "org.freedesktop.DBus.ObjectManager.GetManagedObjects",
        "r");
    if (!fp) {
        free(buf);
        return -1;
    }
    got = fread(buf, 1, 131071, fp);
    pclose(fp);
    buf[got] = 0;
    found = strstr(buf, "/sco/");
    if (found) {
        char *dev = found;
        int back = 0;
        while (dev > buf && back < 80 && strncmp(dev, "dev_", 4) != 0) {
            dev--;
            back++;
        }
        if (parse_dev(dev, addr) == 0) {
            *rate = strstr(found, "8000") != NULL ? 8000 : 16000;
            ok = 0;
        }
    }
    free(buf);
    return ok;
}

static void stop_link(void) {
    if (up_fd >= 0) {
        close(up_fd);
        up_fd = -1;
    }
    if (up_pid > 0) {
        kill(up_pid, SIGTERM);
    }
    if (down_rec > 0) {
        kill(down_rec, SIGTERM);
    }
    if (down_play > 0) {
        kill(down_play, SIGTERM);
    }
    while (waitpid(-1, NULL, WNOHANG) > 0) {
    }
    up_pid = -1;
    down_rec = -1;
    down_play = -1;
}

static const char *speaker_dev(void) {
    FILE *fp = fopen("/etc/asound.conf", "r");
    char buf[4096];
    size_t n;
    if (!fp) {
        return "plughw:0,0";
    }
    n = fread(buf, 1, sizeof buf - 1, fp);
    fclose(fp);
    buf[n] = 0;
    if (strstr(buf, "softvol")) {
        return "default";
    }
    return "plughw:0,0";
}

static int child_play(const char *dev, const char *rate, int stdin_pipe) {
    int fd = open("/dev/null", O_RDWR);
    if (stdin_pipe >= 0) {
        dup2(stdin_pipe, 0);
    }
    if (fd >= 0) {
        dup2(fd, 1);
        dup2(fd, 2);
    }
    execlp("aplay", "aplay", "-D", dev, "-f", "S16_LE", "-r", rate, "-c", "1", "-t", "raw", "-", (char *)NULL);
    _exit(1);
}

static int start_link(const char *addr, int rate) {
    char pcm[80];
    char rate_text[16];
    int pipes[2];
    int down[2];
    int n;
    n = snprintf(pcm, sizeof pcm, "bluealsa:DEV=%s,PROFILE=sco", addr);
    snprintf(rate_text, sizeof rate_text, "%d", rate);
    if (n <= 0 || n >= (int)sizeof pcm) {
        return -1;
    }
    if (pipe(pipes) != 0) {
        return -1;
    }
    up_pid = fork();
    if (up_pid < 0) {
        close(pipes[0]);
        close(pipes[1]);
        return -1;
    }
    if (up_pid == 0) {
        close(pipes[1]);
        child_play(pcm, rate_text, pipes[0]);
    }
    close(pipes[0]);
    up_fd = pipes[1];
    if (pipe(down) != 0) {
        stop_link();
        return -1;
    }
    down_rec = fork();
    if (down_rec < 0) {
        close(down[0]);
        close(down[1]);
        stop_link();
        return -1;
    }
    if (down_rec == 0) {
        int fd = open("/dev/null", O_RDWR);
        close(up_fd);
        dup2(down[1], 1);
        if (fd >= 0) {
            dup2(fd, 2);
        }
        close(down[0]);
        close(down[1]);
        execlp("arecord", "arecord", "-D", pcm, "-f", "S16_LE", "-r", rate_text, "-c", "1", "-t", "raw", "-", (char *)NULL);
        _exit(1);
    }
    down_play = fork();
    if (down_play < 0) {
        close(down[0]);
        close(down[1]);
        stop_link();
        return -1;
    }
    if (down_play == 0) {
        close(up_fd);
        close(down[1]);
        child_play(speaker_dev(), rate_text, down[0]);
    }
    close(down[0]);
    close(down[1]);
    link_rate = rate;
    if (ring) {
        __sync_synchronize();
        read_pos = ring->write_pos;
    }
    fprintf(stderr, "sco up %d\n", rate);
    return 0;
}

static void open_ring(void) {
    int fd;
    struct stat st;
    void *map;
    if (ring) {
        return;
    }
    fd = open(ring_path, O_RDONLY);
    if (fd < 0) {
        return;
    }
    if (fstat(fd, &st) != 0 || st.st_size < (off_t)sizeof(struct CleanRing)) {
        close(fd);
        return;
    }
    map = mmap(NULL, sizeof(struct CleanRing), PROT_READ, MAP_SHARED, fd, 0);
    close(fd);
    if (map == MAP_FAILED) {
        return;
    }
    ring = map;
    if (ring->magic != CLEAN_MAGIC || ring->capacity != CLEAN_CAP) {
        munmap(map, sizeof(struct CleanRing));
        ring = NULL;
    }
}

static void pump(void) {
    uint32_t w;
    uint32_t avail;
    uint32_t need;
    uint32_t i;
    int16_t chunk[320];
    ssize_t wr;
    if (up_fd < 0) {
        return;
    }
    open_ring();
    need = link_rate == 8000 ? 640u : 320u;
    if (!ring) {
        usleep(20000);
        memset(chunk, 0, 640);
        wr = write(up_fd, chunk, 640);
        if (wr < 0) {
            stop_link();
        }
        return;
    }
    w = ring->write_pos;
    __sync_synchronize();
    avail = w - read_pos;
    if (avail > CLEAN_CAP) {
        read_pos = w;
        return;
    }
    if (avail < need) {
        usleep(10000);
        return;
    }
    if (link_rate == 8000) {
        for (i = 0; i < 320u; i++) {
            int a = ring->pcm[(read_pos + i * 2u) & (CLEAN_CAP - 1u)];
            int b = ring->pcm[(read_pos + i * 2u + 1u) & (CLEAN_CAP - 1u)];
            chunk[i] = (int16_t)((a + b) / 2);
        }
        read_pos += 640u;
        wr = write(up_fd, chunk, 640);
    } else {
        for (i = 0; i < 320u; i++) {
            chunk[i] = ring->pcm[(read_pos + i) & (CLEAN_CAP - 1u)];
        }
        read_pos += 320u;
        wr = write(up_fd, chunk, 640);
    }
    if (wr < 0) {
        stop_link();
        fprintf(stderr, "sco down\n");
    }
}

static void run_loop(void) {
    char addr[18];
    char current[18];
    int rate = 16000;
    time_t next = 0;
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_stop;
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);
    signal(SIGPIPE, SIG_IGN);
    current[0] = 0;
    fprintf(stderr, "bt loop\n");
    while (run) {
        time_t now = time(NULL);
        int status;
        int died = 0;
        if (up_pid > 0 && waitpid(up_pid, &status, WNOHANG) == up_pid) {
            up_pid = -1;
            died = 1;
        }
        if (down_rec > 0 && waitpid(down_rec, &status, WNOHANG) == down_rec) {
            down_rec = -1;
            died = 1;
        }
        if (down_play > 0 && waitpid(down_play, &status, WNOHANG) == down_play) {
            down_play = -1;
            died = 1;
        }
        if (died) {
            stop_link();
            current[0] = 0;
        }
        if (now >= next) {
            next = now + 1;
            if (find_sco(addr, &rate) != 0) {
                if (current[0]) {
                    stop_link();
                    current[0] = 0;
                    fprintf(stderr, "sco down\n");
                }
            } else if (strcmp(addr, current) != 0 || rate != link_rate || up_fd < 0) {
                stop_link();
                if (start_link(addr, rate) == 0) {
                    snprintf(current, sizeof current, "%s", addr);
                } else {
                    current[0] = 0;
                }
            }
        }
        if (up_fd >= 0) {
            pump();
        } else {
            sleep(1);
        }
    }
    stop_link();
    unlink(pid_path);
}

static void daemonize(void) {
    pid_t pid = fork();
    int fd;
    char buf[32];
    int n;
    if (pid < 0) {
        exit(1);
    }
    if (pid > 0) {
        _exit(0);
    }
    setsid();
    fd = open(pid_path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd >= 0) {
        n = snprintf(buf, sizeof buf, "%d\n", getpid());
        if (n > 0) {
            if (write(fd, buf, (size_t)n) < 0) {
                /* pid file is best-effort */
            }
        }
        close(fd);
    }
}

int main(int argc, char **argv) {
    int on;
    if (argc < 2 || strcmp(argv[1], "apply") != 0) {
        fprintf(stderr, "usage: bthead apply\n");
        return 1;
    }
    on = headset_on();
    stop_pidfile();
    if (restart_audio() != 0) {
        fprintf(stderr, "bluealsa failed\n");
        return 1;
    }
    if (on) {
        bt_text("power on\npairable on\ndiscoverable on\nquit\n");
        fprintf(stderr, "bt on\n");
        daemonize();
        run_loop();
        return 0;
    }
    bt_text("discoverable off\npairable off\nquit\n");
    fprintf(stderr, "bt off\n");
    return 0;
}
