/*
 * cz_ui.h — Shared 320x170 RGB565 Framebuffer, Font, Chrome & Evdev Input Kit
 *           for Kali Linux on M5Stack CardputerZero
 *
 * Hardware geometry (matched to M5Stack's official bottom_key_bar.cpp):
 *   - Display: 1.9" ST7789v3 SPI LCD, 320x170 RGB565 (/dev/fb_lcd)
 *   - Bottom 5 physical number keys directly below the LCD: '4', '5', '6', '7', '8'
 *   - Exact softkey horizontal centers on 320px width:
 *       Slot 0 ('4'): x =  48  (center - 112)
 *       Slot 1 ('5'): x = 104  (center -  56)
 *       Slot 2 ('6'): x = 160  (center +   0)
 *       Slot 3 ('7'): x = 216  (center +  56)
 *       Slot 4 ('8'): x = 272  (center + 112)
 */

#ifndef CZ_UI_H
#define CZ_UI_H

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/fb.h>
#include <linux/input.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define LCD_W 320
#define LCD_H 170
#define CZ_MAX_INPUT_FDS 12

static inline uint16_t rgb565(uint8_t r, uint8_t g, uint8_t b)
{
    return (uint16_t)(((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3));
}

/* Kali Dark Palette */
#define COL_BG         rgb565(8, 12, 22)
#define COL_PANEL      rgb565(15, 22, 38)
#define COL_PANEL_ALT  rgb565(20, 30, 50)
#define COL_BORDER     rgb565(34, 56, 92)
#define COL_HDR_BG     rgb565(12, 28, 58)
#define COL_SEL_BG     rgb565(28, 66, 128)
#define COL_TAB_ACT    rgb565(44, 132, 245)
#define COL_TAB_IDLE   rgb565(20, 34, 58)
#define COL_TEXT       rgb565(235, 243, 255)
#define COL_DIM        rgb565(128, 152, 185)
#define COL_CYAN       rgb565(56, 188, 255)
#define COL_GREEN      rgb565(38, 218, 132)
#define COL_YELLOW     rgb565(250, 196, 48)
#define COL_ORANGE     rgb565(255, 140, 42)
#define COL_RED        rgb565(248, 78, 92)
#define COL_PURPLE     rgb565(178, 102, 255)
#define COL_BAR_BG     rgb565(24, 34, 54)
#define COL_WHITE      rgb565(255, 255, 255)
#define COL_BLACK      rgb565(0, 0, 0)

/* Physical softkey centers aligned to '4', '5', '6', '7', '8' under the LCD */
static const int CZ_SOFTKEY_X[5] = { 48, 104, 160, 216, 272 };

/* Compact 5x7 ASCII font (0x20..0x7E), 5 columns per glyph, LSB = top row */
static const uint8_t CZ_FONT5X7[95][5] = {
    {0x00,0x00,0x00,0x00,0x00}, {0x00,0x00,0x5F,0x00,0x00}, {0x00,0x07,0x00,0x07,0x00},
    {0x14,0x7F,0x14,0x7F,0x14}, {0x24,0x2A,0x7F,0x2A,0x12}, {0x23,0x13,0x08,0x64,0x62},
    {0x36,0x49,0x55,0x22,0x50}, {0x00,0x05,0x03,0x00,0x00}, {0x00,0x1C,0x22,0x41,0x00},
    {0x00,0x41,0x22,0x1C,0x00}, {0x14,0x08,0x3E,0x08,0x14}, {0x08,0x08,0x3E,0x08,0x08},
    {0x00,0x50,0x30,0x00,0x00}, {0x08,0x08,0x08,0x08,0x08}, {0x00,0x60,0x60,0x00,0x00},
    {0x20,0x10,0x08,0x04,0x02}, {0x3E,0x51,0x49,0x45,0x3E}, {0x00,0x42,0x7F,0x40,0x00},
    {0x42,0x61,0x51,0x49,0x46}, {0x21,0x41,0x45,0x4B,0x31}, {0x18,0x14,0x12,0x7F,0x10},
    {0x27,0x45,0x45,0x45,0x39}, {0x3C,0x4A,0x49,0x49,0x30}, {0x01,0x71,0x09,0x05,0x03},
    {0x36,0x49,0x49,0x49,0x36}, {0x06,0x49,0x49,0x29,0x1E}, {0x00,0x36,0x36,0x00,0x00},
    {0x00,0x56,0x36,0x00,0x00}, {0x08,0x14,0x22,0x41,0x00}, {0x14,0x14,0x14,0x14,0x14},
    {0x00,0x41,0x22,0x14,0x08}, {0x02,0x01,0x51,0x09,0x06}, {0x32,0x49,0x79,0x41,0x3E},
    {0x7E,0x11,0x11,0x11,0x7E}, {0x7F,0x49,0x49,0x49,0x36}, {0x3E,0x41,0x41,0x41,0x22},
    {0x7F,0x41,0x41,0x22,0x1C}, {0x7F,0x49,0x49,0x49,0x41}, {0x7F,0x09,0x09,0x09,0x01},
    {0x3E,0x41,0x49,0x49,0x7A}, {0x7F,0x08,0x08,0x08,0x7F}, {0x00,0x41,0x7F,0x41,0x00},
    {0x20,0x40,0x41,0x3F,0x01}, {0x7F,0x08,0x14,0x22,0x41}, {0x7F,0x40,0x40,0x40,0x40},
    {0x7F,0x02,0x0C,0x02,0x7F}, {0x7F,0x04,0x08,0x10,0x7F}, {0x3E,0x41,0x41,0x41,0x3E},
    {0x7F,0x09,0x09,0x09,0x06}, {0x3E,0x41,0x51,0x21,0x5E}, {0x7F,0x09,0x19,0x29,0x46},
    {0x46,0x49,0x49,0x49,0x31}, {0x01,0x01,0x7F,0x01,0x01}, {0x3F,0x40,0x40,0x40,0x3F},
    {0x1F,0x20,0x40,0x20,0x1F}, {0x3F,0x40,0x38,0x40,0x3F}, {0x63,0x14,0x08,0x14,0x63},
    {0x07,0x08,0x70,0x08,0x07}, {0x61,0x51,0x49,0x45,0x43}, {0x00,0x7F,0x41,0x41,0x00},
    {0x02,0x04,0x08,0x10,0x20}, {0x00,0x41,0x41,0x7F,0x00}, {0x04,0x02,0x01,0x02,0x04},
    {0x40,0x40,0x40,0x40,0x40}, {0x00,0x01,0x02,0x04,0x00}, {0x20,0x54,0x54,0x54,0x78},
    {0x7F,0x48,0x44,0x44,0x38}, {0x38,0x44,0x44,0x44,0x20}, {0x38,0x44,0x44,0x48,0x7F},
    {0x38,0x54,0x54,0x54,0x18}, {0x08,0x7E,0x09,0x01,0x02}, {0x0C,0x52,0x52,0x52,0x3E},
    {0x7F,0x08,0x04,0x04,0x78}, {0x00,0x44,0x7D,0x40,0x00}, {0x20,0x40,0x44,0x3D,0x00},
    {0x7F,0x10,0x28,0x44,0x00}, {0x00,0x41,0x7F,0x40,0x00}, {0x7C,0x04,0x18,0x04,0x78},
    {0x7C,0x08,0x04,0x04,0x78}, {0x38,0x44,0x44,0x44,0x38}, {0x7C,0x14,0x14,0x14,0x08},
    {0x08,0x14,0x14,0x18,0x7C}, {0x7C,0x08,0x04,0x04,0x08}, {0x48,0x54,0x54,0x54,0x20},
    {0x04,0x3F,0x44,0x40,0x20}, {0x3C,0x40,0x40,0x20,0x7C}, {0x1C,0x20,0x40,0x20,0x1C},
    {0x3C,0x40,0x30,0x40,0x3C}, {0x44,0x28,0x10,0x28,0x44}, {0x0C,0x50,0x50,0x50,0x3C},
    {0x44,0x64,0x54,0x4C,0x44}, {0x00,0x08,0x36,0x41,0x00}, {0x00,0x00,0x7F,0x00,0x00},
    {0x00,0x41,0x36,0x08,0x00}, {0x08,0x04,0x08,0x10,0x08},
};

static uint16_t g_buf[LCD_H * LCD_W];

static inline void fill_rect(int x, int y, int w, int h, uint16_t col)
{
    if (x < 0) { w += x; x = 0; }
    if (y < 0) { h += y; y = 0; }
    if (x + w > LCD_W) w = LCD_W - x;
    if (y + h > LCD_H) h = LCD_H - y;
    if (w <= 0 || h <= 0) return;
    for (int r = y; r < y + h; r++) {
        uint16_t *row = &g_buf[r * LCD_W + x];
        for (int c = 0; c < w; c++) row[c] = col;
    }
}

static inline void draw_rect(int x, int y, int w, int h, uint16_t col)
{
    fill_rect(x, y, w, 1, col);
    fill_rect(x, y + h - 1, w, 1, col);
    fill_rect(x, y, 1, h, col);
    fill_rect(x + w - 1, y, 1, h, col);
}

static inline void draw_str(int x, int y, const char *s, uint16_t col)
{
    if (!s) return;
    for (; *s; s++, x += 6) {
        if (x > LCD_W - 5) break;
        unsigned char uc = (unsigned char)*s;
        if (uc < 32 || uc > 126) uc = '?';
        const uint8_t *g = CZ_FONT5X7[uc - 32];
        for (int cx = 0; cx < 5; cx++) {
            uint8_t bits = g[cx];
            int px = x + cx;
            if (px < 0 || px >= LCD_W) continue;
            for (int cy = 0; cy < 7; cy++) {
                if ((bits >> cy) & 1) {
                    int py = y + cy;
                    if (py >= 0 && py < LCD_H)
                        g_buf[py * LCD_W + px] = col;
                }
            }
        }
    }
}

static inline void draw_str_clip(int x, int y, const char *s, int max_chars, uint16_t col)
{
    if (!s || max_chars <= 0) return;
    for (int i = 0; *s && i < max_chars; s++, i++, x += 6) {
        if (x > LCD_W - 5) break;
        unsigned char uc = (unsigned char)*s;
        if (uc < 32 || uc > 126) uc = '?';
        const uint8_t *g = CZ_FONT5X7[uc - 32];
        for (int cx = 0; cx < 5; cx++) {
            uint8_t bits = g[cx];
            int px = x + cx;
            if (px < 0 || px >= LCD_W) continue;
            for (int cy = 0; cy < 7; cy++) {
                if ((bits >> cy) & 1) {
                    int py = y + cy;
                    if (py >= 0 && py < LCD_H)
                        g_buf[py * LCD_W + px] = col;
                }
            }
        }
    }
}

static inline void draw_bar(int x, int y, int w, int h, int pct, uint16_t fg)
{
    if (pct < 0) pct = 0;
    if (pct > 100) pct = 100;
    fill_rect(x, y, w, h, COL_BAR_BG);
    int fw = (w * pct) / 100;
    if (fw > 0) fill_rect(x, y, fw, h, fg);
    draw_rect(x, y, w, h, COL_BORDER);
}

static inline int cz_read_str_file(const char *path, char *buf, size_t sz)
{
    FILE *fp = fopen(path, "r");
    if (!fp) return -1;
    if (!fgets(buf, (int)sz, fp)) { fclose(fp); return -1; }
    fclose(fp);
    size_t l = strlen(buf);
    while (l > 0 && (buf[l - 1] == '\n' || buf[l - 1] == '\r')) buf[--l] = '\0';
    return 0;
}

static inline int cz_read_int_file(const char *path, long *out)
{
    FILE *fp = fopen(path, "r");
    if (!fp) return -1;
    int ok = (fscanf(fp, "%ld", out) == 1) ? 0 : -1;
    fclose(fp);
    return ok;
}

static inline int cz_battery_info(int *charging_out)
{
    int pct = -1;
    if (charging_out) *charging_out = 0;
    DIR *d = opendir("/sys/class/power_supply");
    if (!d) return -1;
    struct dirent *de;
    while ((de = readdir(d))) {
        if (de->d_name[0] == '.') continue;
        char p[320], buf[64];
        long v = 0;
        snprintf(p, sizeof(p), "/sys/class/power_supply/%.64s/capacity", de->d_name);
        if (cz_read_int_file(p, &v) == 0 && v >= 0 && v <= 100)
            pct = (int)v;
        snprintf(p, sizeof(p), "/sys/class/power_supply/%.64s/status", de->d_name);
        if (cz_read_str_file(p, buf, sizeof(buf)) == 0) {
            if (strcmp(buf, "Charging") == 0 && charging_out) *charging_out = 1;
        }
    }
    closedir(d);
    return pct;
}

/* Top Kali header bar (y = 0..14) */
static inline void cz_draw_topbar(const char *title, const char *subtitle)
{
    fill_rect(0, 0, LCD_W, 15, COL_HDR_BG);
    fill_rect(0, 14, LCD_W, 1, COL_BORDER);

    fill_rect(3, 2, 28, 10, COL_TAB_ACT);
    draw_str(5, 4, "KALI", COL_WHITE);
    draw_str(35, 4, title ? title : "", COL_TEXT);

    if (subtitle && *subtitle) {
        int tx = 35 + (int)strlen(title ? title : "") * 6 + 6;
        draw_str_clip(tx, 4, subtitle, 22, COL_CYAN);
    }

    int chg = 0;
    int bat = cz_battery_info(&chg);
    time_t now = time(NULL);
    struct tm tmv;
    localtime_r(&now, &tmv);

    char right[32];
    if (bat >= 0) {
        snprintf(right, sizeof(right), "%s%d%% %02d:%02d",
                 chg ? "+" : "", bat, tmv.tm_hour, tmv.tm_min);
    } else {
        snprintf(right, sizeof(right), "%02d:%02d", tmv.tm_hour, tmv.tm_min);
    }
    int rx = LCD_W - (int)strlen(right) * 6 - 3;
    draw_str(rx, 4, right, (bat >= 0 && bat <= 15 && !chg) ? COL_RED : COL_DIM);
}

/*
 * Bottom softkey bar (y = 152..169) aligned to physical keys '4', '5', '6', '7', '8'
 * at X centers 48, 104, 160, 216, 272, with downward indicator ticks matching M5's UI.
 */
static inline void cz_draw_softkeys(const char *k4, const char *k5, const char *k6,
                                    const char *k7, const char *k8, int active_slot)
{
    const char *labels[5] = { k4, k5, k6, k7, k8 };
    fill_rect(0, 152, LCD_W, 18, COL_HDR_BG);
    fill_rect(0, 152, LCD_W, 1, COL_BORDER);

    for (int i = 0; i < 5; i++) {
        if (!labels[i] || !*labels[i]) continue;
        int cx = CZ_SOFTKEY_X[i];
        int bw = 52;
        int bx = cx - bw / 2;
        int is_act = (i == active_slot);
        int is_exit = (i == 4 && (strcmp(labels[i], "EXIT") == 0 || strcmp(labels[i], "BACK") == 0));

        uint16_t bg = is_act ? COL_TAB_ACT : (is_exit ? rgb565(64, 24, 36) : COL_TAB_IDLE);
        uint16_t brd = is_act ? COL_CYAN : (is_exit ? COL_RED : COL_BORDER);
        uint16_t fg = is_act ? COL_WHITE : (is_exit ? rgb565(255, 180, 188) : COL_TEXT);

        fill_rect(bx, 154, bw, 12, bg);
        draw_rect(bx, 154, bw, 12, brd);

        int len = (int)strlen(labels[i]);
        if (len > 8) len = 8;
        int tx = cx - (len * 6 - 1) / 2;
        draw_str_clip(tx, 157, labels[i], 8, fg);

        /* Physical key alignment tick at bottom edge (y = 167..169) */
        fill_rect(cx - 1, 167, 2, 3, is_act ? COL_CYAN : COL_DIM);
    }
}

struct cz_fb {
    int fd;
    uint8_t *map;
    size_t map_len;
    struct fb_fix_screeninfo fix;
    struct fb_var_screeninfo var;
};

static inline int cz_fb_open(struct cz_fb *fb)
{
    memset(fb, 0, sizeof(*fb));
    fb->fd = -1;

    char discovered[128] = "";
    DIR *d = opendir("/sys/class/graphics");
    if (d) {
        struct dirent *de;
        while ((de = readdir(d))) {
            if (strncmp(de->d_name, "fb", 2) != 0 || !isdigit((unsigned char)de->d_name[2]))
                continue;
            char np[320], nm[64];
            snprintf(np, sizeof(np), "/sys/class/graphics/%.64s/name", de->d_name);
            if (cz_read_str_file(np, nm, sizeof(nm)) == 0 &&
                (strstr(nm, "st7789") || strstr(nm, "panel-mipi-dbi"))) {
                snprintf(discovered, sizeof(discovered), "/dev/%.64s", de->d_name);
                break;
            }
        }
        closedir(d);
    }

    const char *cands[4] = {
        getenv("LV_LINUX_FBDEV_DEVICE"),
        "/dev/fb_lcd",
        discovered[0] ? discovered : NULL,
        "/dev/fb0",
    };

    for (int i = 0; i < 4; i++) {
        if (!cands[i] || !*cands[i]) continue;
        int fd = open(cands[i], O_RDWR | O_CLOEXEC);
        if (fd < 0) continue;
        if (ioctl(fd, FBIOGET_FSCREENINFO, &fb->fix) == 0 &&
            ioctl(fd, FBIOGET_VSCREENINFO, &fb->var) == 0 &&
            (fb->var.bits_per_pixel == 16 || fb->var.bits_per_pixel == 32)) {
            fb->map_len = fb->fix.smem_len ? fb->fix.smem_len
                                           : (size_t)fb->fix.line_length * fb->var.yres_virtual;
            void *m = mmap(NULL, fb->map_len, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
            if (m != MAP_FAILED) {
                fb->fd = fd;
                fb->map = (uint8_t *)m;
                return 0;
            }
        }
        close(fd);
    }
    return -1;
}

static inline void cz_fb_blit(struct cz_fb *fb)
{
    if (!fb || fb->fd < 0 || !fb->map) return;
    int bpp = (int)(fb->var.bits_per_pixel / 8);
    int xoff = ((int)fb->var.xres > LCD_W) ? ((int)fb->var.xres - LCD_W) / 2 : 0;
    int yoff = ((int)fb->var.yres > LCD_H) ? ((int)fb->var.yres - LCD_H) / 2 : 0;
    int cw = ((int)fb->var.xres < LCD_W) ? (int)fb->var.xres : LCD_W;
    int ch = ((int)fb->var.yres < LCD_H) ? (int)fb->var.yres : LCD_H;

    for (int y = 0; y < ch; y++) {
        uint8_t *dst = fb->map + (size_t)(y + yoff + (int)fb->var.yoffset) * fb->fix.line_length
                               + (size_t)(xoff + (int)fb->var.xoffset) * (size_t)bpp;
        const uint16_t *src = &g_buf[y * LCD_W];
        if (bpp == 2) {
            memcpy(dst, src, (size_t)cw * 2);
        } else {
            uint32_t *d32 = (uint32_t *)dst;
            for (int x = 0; x < cw; x++) {
                uint16_t p = src[x];
                uint32_t r = ((p >> 11) & 0x1F) * 255 / 31;
                uint32_t g = ((p >> 5) & 0x3F) * 255 / 63;
                uint32_t b = (p & 0x1F) * 255 / 31;
                d32[x] = (0xFFU << 24) | (r << 16) | (g << 8) | b;
            }
        }
    }
}

static inline void cz_fb_close(struct cz_fb *fb)
{
    if (!fb) return;
    if (fb->map && fb->map != MAP_FAILED) munmap(fb->map, fb->map_len);
    if (fb->fd >= 0) close(fb->fd);
    fb->fd = -1;
    fb->map = NULL;
}

static inline int cz_dump_ppm(const char *path)
{
    FILE *fp = fopen(path, "wb");
    if (!fp) return -1;
    fprintf(fp, "P6\n%d %d\n255\n", LCD_W, LCD_H);
    for (int i = 0; i < LCD_W * LCD_H; i++) {
        uint16_t p = g_buf[i];
        uint8_t rgb[3] = {
            (uint8_t)(((p >> 11) & 0x1F) * 255 / 31),
            (uint8_t)(((p >> 5) & 0x3F) * 255 / 63),
            (uint8_t)((p & 0x1F) * 255 / 31),
        };
        fwrite(rgb, 1, 3, fp);
    }
    fclose(fp);
    return 0;
}

/* Open keyboard evdev nodes, deduplicating symlinks via st_rdev */
static inline int cz_inputs_open(int *fds, int max_fds)
{
    dev_t seen[CZ_MAX_INPUT_FDS];
    int n = 0;
    const char *primary = "/dev/input/by-path/platform-3f804000.i2c-event";
    struct stat st;
    int pfd = open(primary, O_RDONLY | O_NONBLOCK | O_CLOEXEC);
    if (pfd >= 0) {
        if (fstat(pfd, &st) == 0 && n < max_fds) {
            seen[n] = st.st_rdev;
            fds[n++] = pfd;
        } else {
            close(pfd);
        }
    }

    DIR *d = opendir("/dev/input");
    if (d) {
        struct dirent *de;
        while (n < max_fds && (de = readdir(d))) {
            if (strncmp(de->d_name, "event", 5) != 0) continue;
            char p[128];
            snprintf(p, sizeof(p), "/dev/input/%.64s", de->d_name);
            int fd = open(p, O_RDONLY | O_NONBLOCK | O_CLOEXEC);
            if (fd < 0) continue;
            if (fstat(fd, &st) != 0) { close(fd); continue; }
            int dup = 0;
            for (int i = 0; i < n; i++) {
                if (seen[i] == st.st_rdev) { dup = 1; break; }
            }
            unsigned long evbit = 0;
            if (!dup &&
                ioctl(fd, EVIOCGBIT(0, sizeof(evbit)), &evbit) >= 0 &&
                (evbit & (1UL << EV_KEY))) {
                seen[n] = st.st_rdev;
                fds[n++] = fd;
            } else {
                close(fd);
            }
        }
        closedir(d);
    }
    return n;
}

static inline void cz_inputs_close(int *fds, int n)
{
    for (int i = 0; i < n; i++) {
        if (fds[i] >= 0) close(fds[i]);
    }
}

/*
 * CardputerZero TCA8418 46-key I2C matrix keyboard `Sym` layer (`linux,keymap1`)
 * and evdev -> ASCII helpers:
 *   - When `Sym` (`KEY_CD = 0x17f`) is active (held, one-shot tapped, or double-tap
 *     locked in `tca8418_fsm`), `tca8418_keypad_m5stack.ko` emits 32 synthetic evdev
 *     keycodes mapped via `/usr/share/keymaps/tca8418_keypad_m5stack_keymap.map`.
 *   - `cz_is_tca8418_fd()` distinguishes the internal TCA8418 keypad from external
 *     USB/Bluetooth keyboards so standard US QWERTY punctuation stays intact on
 *     external keyboards while all 32 `Sym` legends work natively on the device.
 */
#define CZ_TCA8418_KEYMAP_PATH "/usr/share/keymaps/tca8418_keypad_m5stack_keymap.map"
#define CZ_TCA8418_SYM_MAX 256

static inline void cz_tca8418_sym_defaults(char sym_map[CZ_TCA8418_SYM_MAX])
{
    memset(sym_map, 0, CZ_TCA8418_SYM_MAX);
    /* Row 4 & Row 0: Sym + 1..0 */
    sym_map[26] = '!';  /* KEY_LEFTBRACE        */
    sym_map[27] = '@';  /* KEY_RIGHTBRACE       */
    sym_map[39] = '#';  /* KEY_SEMICOLON        */
    sym_map[40] = '$';  /* KEY_APOSTROPHE       */
    sym_map[41] = '%';  /* KEY_GRAVE            */
    sym_map[43] = '^';  /* KEY_BACKSLASH        */
    sym_map[51] = '&';  /* KEY_COMMA            */
    sym_map[52] = '*';  /* KEY_DOT              */
    sym_map[53] = '(';  /* KEY_SLASH            */
    sym_map[94] = ')';  /* KEY_MUHENKAN         */
    /* Row 1: Sym + Q..P */
    sym_map[55] = '~';  /* KEY_KPASTERISK       */
    sym_map[69] = '`';  /* KEY_NUMLOCK          */
    sym_map[70] = '_';  /* KEY_SCROLLLOCK       */
    sym_map[71] = '-';  /* KEY_KP7              */
    sym_map[72] = '+';  /* KEY_KP8              */
    sym_map[73] = '=';  /* KEY_KP9              */
    sym_map[74] = '[';  /* KEY_KPMINUS          */
    sym_map[75] = ']';  /* KEY_KP4              */
    sym_map[76] = '{';  /* KEY_KP5              */
    sym_map[77] = '}';  /* KEY_KP6              */
    /* Row 2: Sym + A,S,D,G,H,J,K,L (F has no Sym legend) */
    sym_map[79] = ';';  /* KEY_KP1              */
    sym_map[80] = ':';  /* KEY_KP2              */
    sym_map[81] = '\''; /* KEY_KP3              */
    sym_map[82] = '"';  /* KEY_KP0              */
    sym_map[83] = '<';  /* KEY_KPDOT            */
    sym_map[85] = '>';  /* KEY_ZENKAKUHANKAKU   */
    sym_map[86] = '\\'; /* KEY_102ND            */
    sym_map[89] = '|';  /* KEY_RO               */
    /* Row 3: Sym + V,B,N,M (Z,X,C have no Sym legend) */
    sym_map[90] = ',';  /* KEY_KATAKANA         */
    sym_map[91] = '.';  /* KEY_HIRAGANA         */
    sym_map[92] = '/';  /* KEY_HENKAN           */
    sym_map[93] = '?';  /* KEY_KATAKANAHIRAGANA */
}

static inline char cz_keysym_to_ascii(const char *name)
{
    static const struct {
        const char *name;
        char ch;
    } tbl[] = {
        { "space", ' ' },        { "exclam", '!' },       { "quotedbl", '"' },
        { "numbersign", '#' },   { "dollar", '$' },       { "percent", '%' },
        { "ampersand", '&' },    { "apostrophe", '\'' },  { "quoteright", '\'' },
        { "parenleft", '(' },    { "parenright", ')' },   { "asterisk", '*' },
        { "plus", '+' },         { "comma", ',' },        { "minus", '-' },
        { "period", '.' },       { "slash", '/' },        { "colon", ':' },
        { "semicolon", ';' },    { "less", '<' },         { "equal", '=' },
        { "greater", '>' },      { "question", '?' },     { "at", '@' },
        { "bracketleft", '[' },  { "backslash", '\\' },   { "bracketright", ']' },
        { "asciicircum", '^' },  { "underscore", '_' },   { "grave", '`' },
        { "quoteleft", '`' },    { "braceleft", '{' },    { "bar", '|' },
        { "braceright", '}' },   { "asciitilde", '~' },
    };
    if (!name || !*name) return 0;
    if (name[1] == '\0' && (unsigned char)name[0] >= 0x20 && (unsigned char)name[0] <= 0x7E)
        return name[0];
    for (size_t i = 0; i < sizeof(tbl) / sizeof(tbl[0]); i++) {
        if (strcmp(name, tbl[i].name) == 0)
            return tbl[i].ch;
    }
    if ((name[0] == 'U' || name[0] == 'u') && isxdigit((unsigned char)name[1])) {
        char *end = NULL;
        unsigned long cp = strtoul(name + 1, &end, 16);
        if (end && *end == '\0' && cp >= 0x20 && cp <= 0x7E)
            return (char)cp;
    }
    return 0;
}

static inline void cz_tca8418_keymap_load(const char *path, char sym_map[CZ_TCA8418_SYM_MAX])
{
    cz_tca8418_sym_defaults(sym_map);
    if (!path || !*path) return;
    FILE *fp = fopen(path, "r");
    if (!fp) return;

    char line[256];
    while (fgets(line, sizeof(line), fp)) {
        char *hash = strchr(line, '#');
        if (hash) *hash = '\0';
        char *p = line;
        while (*p && isspace((unsigned char)*p)) p++;
        if (!*p) continue;
        unsigned int kc = 0;
        char sym[64] = "";
        if (sscanf(p, "keycode %u = %63s", &kc, sym) == 2 && kc < CZ_TCA8418_SYM_MAX) {
            char ch = cz_keysym_to_ascii(sym);
            if (ch != 0)
                sym_map[kc] = ch;
        }
    }
    fclose(fp);
}

static inline int cz_is_tca8418_fd(int fd)
{
    struct stat st, ref;
    if (fstat(fd, &st) == 0) {
        if (stat("/dev/input/by-path/platform-3f804000.i2c-event", &ref) == 0 &&
            st.st_rdev == ref.st_rdev)
            return 1;
        if (stat("/dev/input/cardputer-zero-internal", &ref) == 0 &&
            st.st_rdev == ref.st_rdev)
            return 1;
    }
    char name[64] = "";
    if (ioctl(fd, EVIOCGNAME(sizeof(name)), name) >= 0) {
        for (const char *p = name; *p; p++) {
            if (tolower((unsigned char)p[0]) == 't' &&
                tolower((unsigned char)p[1]) == 'c' &&
                tolower((unsigned char)p[2]) == 'a' &&
                p[3] == '8' && p[4] == '4' && p[5] == '1' && p[6] == '8')
                return 1;
        }
    }
    return 0;
}

static inline int cz_test_evdev_bit(const unsigned long *bits, unsigned int bit)
{
    unsigned int bpl = (unsigned int)(sizeof(unsigned long) * 8);
    return (int)((bits[bit / bpl] >> (bit % bpl)) & 1UL);
}

static inline void cz_inputs_sync_modifiers(const int *fds, int n,
                                            int *shift_out, int *ctrl_out, int *alt_out)
{
    int sh = 0, ct = 0, al = 0;
    for (int i = 0; i < n; i++) {
        unsigned long kbits[(KEY_MAX + 1) / (sizeof(unsigned long) * 8) + 1];
        memset(kbits, 0, sizeof(kbits));
        if (fds[i] >= 0 && ioctl(fds[i], EVIOCGKEY(sizeof(kbits)), kbits) >= 0) {
            if (cz_test_evdev_bit(kbits, KEY_LEFTSHIFT) || cz_test_evdev_bit(kbits, KEY_RIGHTSHIFT))
                sh = 1;
            if (cz_test_evdev_bit(kbits, KEY_LEFTCTRL) || cz_test_evdev_bit(kbits, KEY_RIGHTCTRL))
                ct = 1;
            if (cz_test_evdev_bit(kbits, KEY_LEFTALT) || cz_test_evdev_bit(kbits, KEY_RIGHTALT))
                al = 1;
        }
    }
    if (shift_out) *shift_out = sh;
    if (ctrl_out)  *ctrl_out = ct;
    if (alt_out)   *alt_out = al;
}

/* Standard US QWERTY evdev keycode -> ASCII (0 if unmapped) */
static inline int cz_us_qwerty_to_ascii(int code, int shift)
{
    switch (code) {
        case KEY_A: return shift ? 'A' : 'a';
        case KEY_B: return shift ? 'B' : 'b';
        case KEY_C: return shift ? 'C' : 'c';
        case KEY_D: return shift ? 'D' : 'd';
        case KEY_E: return shift ? 'E' : 'e';
        case KEY_F: return shift ? 'F' : 'f';
        case KEY_G: return shift ? 'G' : 'g';
        case KEY_H: return shift ? 'H' : 'h';
        case KEY_I: return shift ? 'I' : 'i';
        case KEY_J: return shift ? 'J' : 'j';
        case KEY_K: return shift ? 'K' : 'k';
        case KEY_L: return shift ? 'L' : 'l';
        case KEY_M: return shift ? 'M' : 'm';
        case KEY_N: return shift ? 'N' : 'n';
        case KEY_O: return shift ? 'O' : 'o';
        case KEY_P: return shift ? 'P' : 'p';
        case KEY_Q: return shift ? 'Q' : 'q';
        case KEY_R: return shift ? 'R' : 'r';
        case KEY_S: return shift ? 'S' : 's';
        case KEY_T: return shift ? 'T' : 't';
        case KEY_U: return shift ? 'U' : 'u';
        case KEY_V: return shift ? 'V' : 'v';
        case KEY_W: return shift ? 'W' : 'w';
        case KEY_X: return shift ? 'X' : 'x';
        case KEY_Y: return shift ? 'Y' : 'y';
        case KEY_Z: return shift ? 'Z' : 'z';
        case KEY_1: return shift ? '!' : '1';
        case KEY_2: return shift ? '@' : '2';
        case KEY_3: return shift ? '#' : '3';
        case KEY_4: return shift ? '$' : '4';
        case KEY_5: return shift ? '%' : '5';
        case KEY_6: return shift ? '^' : '6';
        case KEY_7: return shift ? '&' : '7';
        case KEY_8: return shift ? '*' : '8';
        case KEY_9: return shift ? '(' : '9';
        case KEY_0: return shift ? ')' : '0';
        case KEY_MINUS:      return shift ? '_' : '-';
        case KEY_EQUAL:      return shift ? '+' : '=';
        case KEY_LEFTBRACE:  return shift ? '{' : '[';
        case KEY_RIGHTBRACE: return shift ? '}' : ']';
        case KEY_BACKSLASH:  return shift ? '|' : '\\';
        case KEY_SEMICOLON:  return shift ? ':' : ';';
        case KEY_APOSTROPHE: return shift ? '"' : '\'';
        case KEY_GRAVE:      return shift ? '~' : '`';
        case KEY_COMMA:      return shift ? '<' : ',';
        case KEY_DOT:        return shift ? '>' : '.';
        case KEY_SLASH:      return shift ? '?' : '/';
        case KEY_SPACE:      return ' ';
        default: return 0;
    }
}

static inline int cz_evdev_to_ascii(const char sym_map[CZ_TCA8418_SYM_MAX],
                                    int is_tca8418, int code, int shift)
{
    if (is_tca8418 && sym_map && code >= 0 && code < CZ_TCA8418_SYM_MAX && sym_map[code] != 0)
        return (unsigned char)sym_map[code];
    return cz_us_qwerty_to_ascii(code, shift);
}

static inline double cz_mono_sec(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

#endif /* CZ_UI_H */
