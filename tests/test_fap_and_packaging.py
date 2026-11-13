"""Tests for Flipper `.fap` C source compilation/logic (via SDK stubs) and APPLaunch packaging."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]


FAP_STUB_HARNESS_C = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* --- Minimal Flipper SDK Stubs for Host Compile & Logic Verification --- */
#define UNUSED(x) (void)(x)
#define EXT_PATH(p) "/ext/" p
#define furi_assert(cond) assert(cond)
#define RECORD_GUI "gui"
#define RECORD_STORAGE "storage"
#define RECORD_NOTIFICATION "notification"
#define FuriWaitForever 0xFFFFFFFFU
#define VIEW_NONE 0xFFFFFFFFU
#define VIEW_IGNORE 0xFFFFFFFEU
#define FSAM_READ 1
#define FSAM_WRITE 2
#define FSOM_OPEN_EXISTING 1
#define FSOM_CREATE_ALWAYS 2
#define FSOM_OPEN_APPEND 4
#define INFRARED_COMMON_CARRIER_FREQUENCY 38000U

typedef struct Gui Gui;
typedef struct Storage Storage;
typedef struct NotificationApp NotificationApp;
typedef struct View View;
typedef struct ViewDispatcher ViewDispatcher;
typedef struct Submenu Submenu;
typedef struct TextBox TextBox;
typedef struct FuriMutex FuriMutex;
typedef struct File { int dummy; } File;

typedef enum {
    NotificationMessageTypeVibro = 0,
    NotificationMessageTypeSoundOn = 1,
    NotificationMessageTypeSoundOff = 2,
    NotificationMessageTypeLedGreen = 3,
    NotificationMessageTypeDelay = 4,
} NotificationMessageType;

typedef struct {
    NotificationMessageType type;
    union {
        struct { float frequency; float volume; } sound;
        struct { uint8_t value; } led;
        struct { bool on; } vibro;
        struct { uint32_t length; } delay;
    } data;
} NotificationMessage;

typedef const NotificationMessage* NotificationSequence[];

static const NotificationMessage message_green_255 = { .type = NotificationMessageTypeLedGreen };
static const NotificationMessage message_green_0 = { .type = NotificationMessageTypeLedGreen };
static const NotificationMessage message_vibro_on = { .type = NotificationMessageTypeVibro };
static const NotificationMessage message_vibro_off = { .type = NotificationMessageTypeVibro };
static const NotificationMessage message_delay_25 = { .type = NotificationMessageTypeDelay };
static const NotificationMessage message_delay_50 = { .type = NotificationMessageTypeDelay };
static const NotificationMessage message_delay_100 = { .type = NotificationMessageTypeDelay };
static const NotificationMessage message_sound_off = { .type = NotificationMessageTypeSoundOff };
static const NotificationSequence sequence_success = { &message_green_255, NULL };

typedef struct {
    uint8_t hour;
    uint8_t minute;
    uint8_t second;
    uint8_t day;
    uint8_t month;
    uint16_t year;
    uint8_t weekday;
} DateTime;

static DateTime g_rtc_last = {0};
static bool datetime_validate_datetime(DateTime* dt) {
    return dt && dt->year >= 2000 && dt->month >= 1 && dt->month <= 12 && dt->day >= 1 && dt->day <= 31;
}
static void furi_hal_rtc_set_datetime(DateTime* dt) {
    if (dt) g_rtc_last = *dt;
}

typedef enum { FuriMutexTypeNormal = 0 } FuriMutexType;
typedef enum { ViewDispatcherTypeFullscreen = 0 } ViewDispatcherType;
typedef enum { TextBoxFontText = 0 } TextBoxFont;
typedef enum { TextBoxFocusStart = 0 } TextBoxFocus;
typedef enum { ViewModelTypeLocking = 2 } ViewModelType;
typedef enum { FontPrimary = 0, FontSecondary = 1 } Font;
typedef enum { InputTypeShort = 2 } InputType;
typedef enum {
    InputKeyUp = 0,
    InputKeyDown = 1,
    InputKeyRight = 2,
    InputKeyLeft = 3,
    InputKeyOk = 4,
    InputKeyBack = 5,
} InputKey;

typedef struct {
    InputKey key;
    InputType type;
} InputEvent;

typedef struct Canvas Canvas;
typedef void (*ViewDrawCallback)(Canvas* canvas, void* model);
typedef bool (*ViewInputCallback)(InputEvent* event, void* context);
typedef void (*ViewCallback)(void* context);
typedef void (*FuriHalSubGhzCaptureCallback)(bool level, uint32_t duration, void* context);

static const uint8_t subghz_device_cc1101_preset_ook_650khz_async_regs[4] = {1, 2, 3, 0};
static FuriHalSubGhzCaptureCallback g_subghz_isr_cb = NULL;
static void* g_subghz_isr_ctx = NULL;
static bool g_subghz_running = false;
static bool g_subghz_slept = false;
static uint32_t g_subghz_freq = 0;
static float g_subghz_rssi = -52.0f;

static void furi_hal_subghz_sleep(void) { g_subghz_slept = true; }
static void furi_hal_subghz_load_custom_preset(const uint8_t* p) { (void)p; }
static bool furi_hal_subghz_is_frequency_valid(uint32_t f) { return f >= 300000000UL && f <= 928000000UL; }
static uint32_t furi_hal_subghz_set_frequency_and_path(uint32_t f) { g_subghz_freq = f; return f; }
static void furi_hal_subghz_flush_rx(void) {}
static float furi_hal_subghz_get_rssi(void) { return g_subghz_rssi; }
static void furi_hal_subghz_start_async_rx(FuriHalSubGhzCaptureCallback cb, void* ctx) {
    g_subghz_isr_cb = cb;
    g_subghz_isr_ctx = ctx;
    g_subghz_running = true;
}
static void furi_hal_subghz_stop_async_rx(void) { g_subghz_running = false; }

typedef enum {
    InfraredProtocolUnknown = -1,
    InfraredProtocolNEC = 0,
    InfraredProtocolNECext = 1,
} InfraredProtocol;

typedef struct {
    InfraredProtocol protocol;
    uint32_t address;
    uint32_t command;
    bool repeat;
} InfraredMessage;

typedef struct InfraredWorker { int dummy; } InfraredWorker;
typedef struct {
    bool decoded;
    InfraredMessage msg;
    const uint32_t* raw;
    size_t raw_cnt;
} InfraredWorkerSignal;
typedef void (*InfraredWorkerReceivedSignalCallback)(void* context, InfraredWorkerSignal* received_signal);

static InfraredWorker g_fake_ir_worker = {1};
static InfraredWorkerReceivedSignalCallback g_ir_rx_cb = NULL;
static void* g_ir_rx_ctx = NULL;
static bool g_ir_rx_started = false;
static int g_ir_worker_alloc_count = 0;
static int g_ir_worker_free_count = 0;
static int g_ir_send_count = 0;
static int g_ir_send_raw_count = 0;
static InfraredMessage g_ir_last_sent = {0};

static InfraredProtocol infrared_get_protocol_by_name(const char* name) {
    if (!name) return InfraredProtocolUnknown;
    if (strcmp(name, "NEC") == 0) return InfraredProtocolNEC;
    if (strcmp(name, "NECext") == 0) return InfraredProtocolNECext;
    return InfraredProtocolUnknown;
}
static bool infrared_is_protocol_valid(InfraredProtocol p) {
    return p == InfraredProtocolNEC || p == InfraredProtocolNECext;
}
static const char* infrared_get_protocol_name(InfraredProtocol p) {
    if (p == InfraredProtocolNEC) return "NEC";
    if (p == InfraredProtocolNECext) return "NECext";
    return NULL;
}
static void infrared_send(const InfraredMessage* msg, int times) {
    (void)times;
    if (msg) {
        g_ir_last_sent = *msg;
        g_ir_send_count++;
    }
}
static void infrared_send_raw_ext(
    const uint32_t timings[],
    uint32_t timings_cnt,
    bool start_from_mark,
    uint32_t frequency,
    float duty_cycle
) {
    (void)timings; (void)timings_cnt; (void)start_from_mark; (void)frequency; (void)duty_cycle;
    g_ir_send_raw_count++;
}
static InfraredWorker* infrared_worker_alloc(void) {
    g_ir_worker_alloc_count++;
    return &g_fake_ir_worker;
}
static void infrared_worker_free(InfraredWorker* w) {
    assert(w == &g_fake_ir_worker);
    g_ir_worker_free_count++;
}
static void infrared_worker_rx_enable_signal_decoding(InfraredWorker* w, bool en) { (void)w; (void)en; }
static void infrared_worker_rx_enable_blink_on_receiving(InfraredWorker* w, bool en) { (void)w; (void)en; }
static void infrared_worker_rx_set_received_signal_callback(
    InfraredWorker* w, InfraredWorkerReceivedSignalCallback cb, void* ctx
) {
    (void)w;
    g_ir_rx_cb = cb;
    g_ir_rx_ctx = ctx;
}
static void infrared_worker_rx_start(InfraredWorker* w) { (void)w; g_ir_rx_started = true; }
static void infrared_worker_rx_stop(InfraredWorker* w) { (void)w; g_ir_rx_started = false; }
static bool infrared_worker_signal_is_decoded(const InfraredWorkerSignal* sig) { return sig && sig->decoded; }
static const InfraredMessage* infrared_worker_get_decoded_signal(const InfraredWorkerSignal* sig) {
    return sig ? &sig->msg : NULL;
}
static void infrared_worker_get_raw_signal(
    const InfraredWorkerSignal* sig, const uint32_t** timings, size_t* timings_cnt
) {
    if (sig) {
        *timings = sig->raw;
        *timings_cnt = sig->raw_cnt;
    }
}

static void canvas_clear(Canvas* c) { (void)c; }
static void canvas_set_font(Canvas* c, Font f) { (void)c; (void)f; }
static void canvas_draw_str(Canvas* c, int32_t x, int32_t y, const char* s) { (void)c; (void)x; (void)y; (void)s; }
static void canvas_draw_frame(Canvas* c, int32_t x, int32_t y, size_t w, size_t h) { (void)c; (void)x; (void)y; (void)w; (void)h; }
static void canvas_draw_box(Canvas* c, int32_t x, int32_t y, size_t w, size_t h) { (void)c; (void)x; (void)y; (void)w; (void)h; }

typedef struct {
    char buf[2048];
} FuriString;

static FuriString* furi_string_alloc(void) {
    return calloc(1, sizeof(FuriString));
}
static FuriString* furi_string_alloc_set_str(const char* s) {
    FuriString* fs = calloc(1, sizeof(FuriString));
    snprintf(fs->buf, sizeof(fs->buf), "%s", s ? s : "");
    return fs;
}
static void furi_string_free(FuriString* fs) { free(fs); }
static void furi_string_reset(FuriString* fs) { fs->buf[0] = '\0'; }
static size_t furi_string_size(const FuriString* fs) { return strlen(fs->buf); }
static bool furi_string_empty(const FuriString* fs) { return fs->buf[0] == '\0'; }
static int furi_string_cmp_str(const FuriString* fs, const char* s) { return strcmp(fs->buf, s); }
static void furi_string_set_str(FuriString* fs, const char* s) {
    snprintf(fs->buf, sizeof(fs->buf), "%s", s ? s : "");
}
static void furi_string_push_back(FuriString* fs, char c) {
    size_t l = strlen(fs->buf);
    if (l + 1 < sizeof(fs->buf)) { fs->buf[l] = c; fs->buf[l + 1] = '\0'; }
}
#define furi_string_printf(fs, fmt, ...) snprintf((fs)->buf, sizeof((fs)->buf), fmt, __VA_ARGS__)
static const char* furi_string_get_cstr(const FuriString* fs) { return fs->buf; }

static uint32_t g_tick = 100;
static void* furi_record_open(const char* name) { (void)name; return (void*)0x1; }
static void furi_record_close(const char* name) { (void)name; }
static uint32_t furi_get_tick(void) { return g_tick; }
static uint32_t furi_ms_to_ticks(uint32_t ms) { return ms; }
static void furi_delay_tick(uint32_t ticks) { g_tick += ticks; }
static FuriMutex* furi_mutex_alloc(FuriMutexType t) { (void)t; return (FuriMutex*)0x2; }
static void furi_mutex_free(FuriMutex* m) { assert(m == (FuriMutex*)0x2); }
static void furi_mutex_acquire(FuriMutex* m, uint32_t to) { assert(m == (FuriMutex*)0x2); (void)to; }
static void furi_mutex_release(FuriMutex* m) { assert(m == (FuriMutex*)0x2); }
static int g_vibro_count = 0;
static int g_led_count = 0;
static int g_beep_count = 0;
static void notification_message(NotificationApp* app, const NotificationSequence* seq) {
    (void)app;
    if (!seq) return;
    bool has_vibro = false;
    bool has_sound = false;
    bool has_led = false;
    for (int i = 0; (*seq)[i] != NULL; i++) {
        if ((*seq)[i]->type == NotificationMessageTypeVibro) has_vibro = true;
        if ((*seq)[i]->type == NotificationMessageTypeSoundOn) has_sound = true;
        if ((*seq)[i]->type == NotificationMessageTypeLedGreen) has_led = true;
    }
    if (has_vibro) g_vibro_count++;
    else if (has_sound) g_beep_count++;
    else if (has_led) g_led_count++;
}

typedef uint32_t (*ViewNavCallback)(void* ctx);
typedef bool (*ViewCustomCallback)(void* ctx, uint32_t ev);
typedef bool (*ViewDispatcherNavigationEventCallback)(void* ctx);
typedef void (*ViewTickCallback)(void* ctx);
typedef void (*SubmenuItemCallback)(void* ctx, uint32_t idx);

static void* g_vd_ctx = NULL;
static ViewCustomCallback g_vd_custom_cb = NULL;
static ViewDispatcherNavigationEventCallback g_vd_nav_cb = NULL;
static ViewTickCallback g_vd_tick_cb = NULL;
static SubmenuItemCallback g_sm_cb = NULL;
static void* g_sm_ctx = NULL;
static SubmenuItemCallback g_jobs_sm_cb = NULL;
static void* g_jobs_sm_ctx = NULL;
static char g_last_alert_label[64];
static char g_last_textbox[2048];
static uint32_t g_last_view_id = 0;

typedef struct {
    uint32_t magic;
    void* model;
    void* ctx;
    ViewDrawCallback draw_cb;
    ViewInputCallback input_cb;
    ViewCallback enter_cb;
    ViewCallback exit_cb;
    ViewNavCallback prev_cb;
} FakeCustomView;

#define FAKE_VIEW_MAGIC 0x5A325A56U
static FakeCustomView* g_views[8] = {0};

static bool is_custom_view(View* v) {
    return v && (uintptr_t)v > 0x1000U && ((FakeCustomView*)v)->magic == FAKE_VIEW_MAGIC;
}

static View* view_alloc(void) {
    FakeCustomView* fv = calloc(1, sizeof(FakeCustomView));
    fv->magic = FAKE_VIEW_MAGIC;
    return (View*)fv;
}
static void view_free(View* v) {
    if (is_custom_view(v)) {
        FakeCustomView* fv = (FakeCustomView*)v;
        free(fv->model);
        fv->model = NULL;
        fv->magic = 0;
        free(fv);
    }
}
static void view_allocate_model(View* v, ViewModelType t, size_t sz) {
    (void)t;
    if (is_custom_view(v)) ((FakeCustomView*)v)->model = calloc(1, sz);
}
static void* view_get_model(View* v) {
    return is_custom_view(v) ? ((FakeCustomView*)v)->model : NULL;
}
static void view_commit_model(View* v, bool upd) {
    if (!is_custom_view(v)) return;
    FakeCustomView* fv = (FakeCustomView*)v;
    if (upd && fv->draw_cb && fv->model) {
        fv->draw_cb((Canvas*)0x1, fv->model);
    }
}
#define with_view_model(view, type, code, update) \
    {                                             \
        type = view_get_model(view);              \
        {code};                                   \
        view_commit_model(view, update);          \
    }
static void view_set_context(View* v, void* ctx) {
    if (is_custom_view(v)) ((FakeCustomView*)v)->ctx = ctx;
}
static void view_set_draw_callback(View* v, ViewDrawCallback cb) {
    if (is_custom_view(v)) ((FakeCustomView*)v)->draw_cb = cb;
}
static void view_set_input_callback(View* v, ViewInputCallback cb) {
    if (is_custom_view(v)) ((FakeCustomView*)v)->input_cb = cb;
}
static void view_set_enter_callback(View* v, ViewCallback cb) {
    if (is_custom_view(v)) ((FakeCustomView*)v)->enter_cb = cb;
}
static void view_set_exit_callback(View* v, ViewCallback cb) {
    if (is_custom_view(v)) ((FakeCustomView*)v)->exit_cb = cb;
}

static ViewDispatcher* view_dispatcher_alloc(void) { return (ViewDispatcher*)0x10; }
static void view_dispatcher_free(ViewDispatcher* vd) { (void)vd; }
static void view_dispatcher_set_event_callback_context(ViewDispatcher* vd, void* ctx) {
    (void)vd; g_vd_ctx = ctx;
}
static void view_dispatcher_set_custom_event_callback(ViewDispatcher* vd, ViewCustomCallback cb) {
    (void)vd; g_vd_custom_cb = cb;
}
static void view_dispatcher_set_navigation_event_callback(
    ViewDispatcher* vd, ViewDispatcherNavigationEventCallback cb
) {
    (void)vd; g_vd_nav_cb = cb;
}
static void view_dispatcher_set_tick_event_callback(ViewDispatcher* vd, ViewTickCallback cb, uint32_t p) {
    (void)vd; (void)p; g_vd_tick_cb = cb;
}
static void view_dispatcher_add_view(ViewDispatcher* vd, uint32_t id, View* v) {
    (void)vd;
    if (id < 8 && is_custom_view(v)) g_views[id] = (FakeCustomView*)v;
}
static void view_dispatcher_remove_view(ViewDispatcher* vd, uint32_t id) {
    (void)vd;
    if (id < 8) g_views[id] = NULL;
}
static void view_dispatcher_attach_to_gui(ViewDispatcher* vd, Gui* g, ViewDispatcherType t) {
    (void)vd; (void)g; (void)t;
}
static int g_vd_stopped = 0;
static void view_dispatcher_stop(ViewDispatcher* vd) { (void)vd; g_vd_stopped++; }
static void view_dispatcher_switch_to_view(ViewDispatcher* vd, uint32_t id) {
    if (g_last_view_id < 8 && g_last_view_id != id && g_views[g_last_view_id] && g_views[g_last_view_id]->exit_cb) {
        g_views[g_last_view_id]->exit_cb(g_views[g_last_view_id]->ctx);
    }
    g_last_view_id = id;
    if (id == VIEW_NONE) {
        view_dispatcher_stop(vd);
        return;
    }
    if (id < 8 && g_views[id] && g_views[id]->enter_cb) {
        g_views[id]->enter_cb(g_views[id]->ctx);
    }
}
static void view_dispatcher_send_custom_event(ViewDispatcher* vd, uint32_t ev) {
    (void)vd;
    if (g_vd_custom_cb) g_vd_custom_cb(g_vd_ctx, ev);
}
static void view_set_previous_callback(View* v, ViewNavCallback cb) {
    if (is_custom_view(v)) ((FakeCustomView*)v)->prev_cb = cb;
}

/*
 * Faithfully mirror Flipper firmware `submenu_alloc` (`0x0807d7a0`: 4-byte
 * `Submenu*` heap block with `view->ctx = submenu`) and `text_box_alloc` (`0x0807dd04`:
 * 8-byte `TextBox*` heap block with `view->ctx = text_box`). Under ASan, any illegal cast
 * of `context` to `Zero2ZeroApp*` inside `previous_callback` fails immediately!
 */
static int g_sm_alloc_seq = 0;
static Submenu* g_main_sm_ptr = NULL;
static View* g_sm_views[8] = {0};
static Submenu* g_sm_ptrs[8] = {0};
static View* g_tb_view = NULL;
static TextBox* g_tb_ptr = NULL;
static char g_main_header[64] = {0};
static char g_about_label[64] = {0};

static Submenu* submenu_alloc(void) {
    Submenu* sm = malloc(4);
    memset(sm, 0xA5, 4);
    View* v = view_alloc();
    view_set_context(v, sm);
    int slot = g_sm_alloc_seq % 8;
    g_sm_ptrs[slot] = sm;
    g_sm_views[slot] = v;
    if (g_sm_alloc_seq % 2 == 0) g_main_sm_ptr = sm;
    g_sm_alloc_seq++;
    return sm;
}
static void submenu_free(Submenu* sm) {
    for (int i = 0; i < 8; i++) {
        if (g_sm_ptrs[i] == sm) {
            view_free(g_sm_views[i]);
            g_sm_views[i] = NULL;
            g_sm_ptrs[i] = NULL;
            break;
        }
    }
    if (g_main_sm_ptr == sm) g_main_sm_ptr = NULL;
    free(sm);
}
static void submenu_reset(Submenu* sm) { (void)sm; }
static void submenu_set_header(Submenu* sm, const char* h) {
    if (sm == g_main_sm_ptr && h) {
        snprintf(g_main_header, sizeof(g_main_header), "%s", h);
    }
}
static void submenu_add_item(Submenu* sm, const char* l, uint32_t idx, SubmenuItemCallback cb, void* ctx) {
    if (sm == g_main_sm_ptr) {
        g_sm_cb = cb;
        g_sm_ctx = ctx;
        if (idx == 11 && l) {
            snprintf(g_about_label, sizeof(g_about_label), "%s", l);
        }
    } else {
        g_jobs_sm_cb = cb;
        g_jobs_sm_ctx = ctx;
    }
}
static void submenu_change_item_label(Submenu* sm, uint32_t idx, const char* l) {
    (void)sm; (void)idx;
    snprintf(g_last_alert_label, sizeof(g_last_alert_label), "%s", l ? l : "");
}
static View* submenu_get_view(Submenu* sm) {
    for (int i = 0; i < 8; i++) {
        if (g_sm_ptrs[i] == sm) return g_sm_views[i];
    }
    return NULL;
}

static TextBox* text_box_alloc(void) {
    TextBox* tb = malloc(8);
    memset(tb, 0x5A, 8);
    g_tb_ptr = tb;
    g_tb_view = view_alloc();
    view_set_context(g_tb_view, tb);
    return tb;
}
static void text_box_free(TextBox* tb) {
    assert(tb == g_tb_ptr);
    view_free(g_tb_view);
    g_tb_view = NULL;
    g_tb_ptr = NULL;
    free(tb);
}
static void text_box_set_font(TextBox* tb, TextBoxFont f) { (void)tb; (void)f; }
static void text_box_set_focus(TextBox* tb, TextBoxFocus f) { (void)tb; (void)f; }
static void text_box_set_text(TextBox* tb, const char* txt) {
    (void)tb;
    snprintf(g_last_textbox, sizeof(g_last_textbox), "%s", txt ? txt : "");
}
static View* text_box_get_view(TextBox* tb) {
    assert(tb == g_tb_ptr);
    return g_tb_view;
}

/* Exact host simulation of Flipper `view_dispatcher_handle_input` (`0x080827ca..0x08082806`) */
static void simulate_back_press(void) {
    assert(g_last_view_id < 8);
    FakeCustomView* fv = g_views[g_last_view_id];
    assert(fv != NULL);
    InputEvent back_ev = { .key = InputKeyBack, .type = InputTypeShort };
    if (fv->input_cb && fv->input_cb(&back_ev, fv->ctx)) {
        return;
    }
    uint32_t prev = fv->prev_cb ? fv->prev_cb(fv->ctx) : VIEW_IGNORE;
    if (prev != VIEW_IGNORE) {
        view_dispatcher_switch_to_view((ViewDispatcher*)0x10, prev);
        return;
    }
    if (g_vd_nav_cb) {
        if (!g_vd_nav_cb(g_vd_ctx)) {
            view_dispatcher_stop((ViewDispatcher*)0x10);
        }
    }
}

#define FSE_OK 0
#define FSE_NOT_EXIST 1
static int g_stat_count = 0;
static int g_open_write_count = 0;
static int g_open_read_count = 0;
static int g_remove_count = 0;
static bool g_mailbox_mode = false;
static bool storage_simply_mkdir(Storage* s, const char* p) { (void)s; (void)p; return true; }
static bool storage_simply_remove(Storage* s, const char* p) {
    (void)s; (void)p; g_remove_count++; return true;
}
static int storage_common_stat(Storage* s, const char* p, void* info) {
    (void)s; (void)p; (void)info; g_stat_count++; return FSE_NOT_EXIST;
}
static File* storage_file_alloc(Storage* s) { (void)s; return calloc(1, sizeof(File)); }
static void storage_file_free(File* f) { free(f); }
static bool storage_file_open(File* f, const char* p, int a, int o) {
    (void)f; (void)p; (void)o;
    if (a == FSAM_WRITE) { g_open_write_count++; return g_mailbox_mode; }
    g_open_read_count++;
    return false;
}
static size_t storage_file_write(File* f, const void* b, size_t l) { (void)f; (void)b; return l; }
static size_t storage_file_read(File* f, void* b, size_t l) { (void)f; (void)b; (void)l; return 0; }
static void storage_file_close(File* f) { (void)f; }

typedef enum {
    RpcAppSystemEventDataTypeNone = 0,
    RpcAppSystemEventDataTypeBytes = 3,
} RpcAppSystemEventDataType;

typedef enum {
    RpcAppEventTypeSessionClose = 1,
    RpcAppEventTypeAppExit = 2,
    RpcAppEventTypeDataExchange = 7,
} RpcAppSystemEventType;

typedef struct {
    RpcAppSystemEventDataType type;
    union {
        struct { const uint8_t* ptr; size_t size; } bytes;
    };
} RpcAppSystemEventData;

typedef struct {
    RpcAppSystemEventType type;
    RpcAppSystemEventData data;
} RpcAppSystemEvent;

typedef struct RpcAppSystem { int dummy; } RpcAppSystem;
typedef void (*RpcAppSystemCallback)(const RpcAppSystemEvent* ev, void* ctx);

static RpcAppSystem g_fake_rpc = {42};
static RpcAppSystemCallback g_rpc_cb = NULL;
static void* g_rpc_cb_ctx = NULL;
static char g_tx_exchange[1200];
static int g_started_sent = 0;
static int g_exited_sent = 0;
static int g_confirmed = 0;
static int g_rejected = 0;
static bool g_test_session_close = false;

static void rpc_system_app_set_callback(RpcAppSystem* r, RpcAppSystemCallback cb, void* ctx) {
    assert(r == &g_fake_rpc);
    g_rpc_cb = cb;
    g_rpc_cb_ctx = ctx;
}
/*
 * In Flipper firmware (`rpc_system_app_free`), `rpc_app->callback != NULL` is the
 * ownership lease keeping `RpcAppSystem*` alive. Calling any `rpc_system_app_*` API after
 * `rpc_system_app_set_callback(r, NULL, NULL)` is a Use-After-Free (`BusFault` on STM32WB55)!
 */
static void rpc_system_app_send_started(RpcAppSystem* r) {
    assert(r == &g_fake_rpc);
    assert(g_rpc_cb != NULL);
    g_started_sent = 1;
}
static void rpc_system_app_send_exited(RpcAppSystem* r) {
    assert(r == &g_fake_rpc);
    assert(g_rpc_cb != NULL);
    g_exited_sent = 1;
}
static void rpc_system_app_confirm(RpcAppSystem* r, bool ok) {
    assert(r == &g_fake_rpc);
    assert(g_rpc_cb != NULL);
    if (ok) g_confirmed++;
    else g_rejected++;
}
static void rpc_system_app_exchange_data(RpcAppSystem* r, const uint8_t* d, size_t sz) {
    assert(r == &g_fake_rpc);
    assert(g_rpc_cb != NULL);
    size_t c = sz < sizeof(g_tx_exchange) - 1 ? sz : sizeof(g_tx_exchange) - 1;
    memcpy(g_tx_exchange, d, c);
    g_tx_exchange[c] = '\0';
}

/* Simulate menu clicks + incoming Kali responses inside `view_dispatcher_run` */
static void view_dispatcher_run(ViewDispatcher* vd) {
    (void)vd;
    assert(g_sm_cb != NULL);

    if (g_mailbox_mode) {
        /* Mailbox mode: click Ping -> writes request.json, Back cancels immediately */
        g_open_write_count = 0;
        g_open_read_count = 0;
        g_stat_count = 0;
        g_sm_cb(g_sm_ctx, 0);
        assert(g_last_view_id == 1);
        assert(g_open_write_count == 1);
        for (int i = 0; i < 3; i++) {
            g_tick += 180;
            g_vd_tick_cb(g_vd_ctx);
        }
        assert(g_open_read_count == 0);
        /* Back on `TextBox` (`malloc(8)`) MUST return to menu without heap OOB! */
        simulate_back_press();
        assert(g_last_view_id == 0);
        g_stat_count = 0;
        for (int i = 0; i < 20; i++) {
            g_tick += 180;
            g_vd_tick_cb(g_vd_ctx);
        }
        assert(g_stat_count == 0);

        /* Mailbox mode timeout after ~15s stops SD polling and displays helpful tip */
        g_sm_cb(g_sm_ctx, 0);
        g_stat_count = 0;
        for (int i = 0; i < 120; i++) {
            g_tick += 180;
            g_vd_tick_cb(g_vd_ctx);
        }
        assert(g_stat_count == 17);
        assert(strstr(g_last_textbox, "No reply from Kali yet") != NULL);
        /* Back from TextBox -> Menu, then Back from Menu (`Submenu*` `malloc(4)`) -> exits app! */
        simulate_back_press();
        assert(g_last_view_id == 0);
        int st0 = g_vd_stopped;
        simulate_back_press();
        assert(g_last_view_id == VIEW_NONE);
        assert(g_vd_stopped == st0 + 1);
        return;
    }

    assert(g_started_sent == 1);
    assert(g_vd_tick_cb != NULL);
    assert(strcmp(g_main_header, "Zero2Zero v0.1.0 (Kali)") == 0);
    assert(strcmp(g_about_label, "About: v0.1.0 (Kali 0.1.0)") == 0);

    if (g_test_session_close) {
        /* RPC-launched `.fap` MUST exit (`VIEW_NONE`) on `SessionClose` too! */
        RpcAppSystemEvent ev_close;
        memset(&ev_close, 0, sizeof(ev_close));
        ev_close.type = RpcAppEventTypeSessionClose;
        assert(g_rpc_cb != NULL);
        g_rpc_cb(&ev_close, g_rpc_cb_ctx);
        assert(g_rpc_cb == NULL);
        assert(g_last_view_id == VIEW_NONE);
        return;
    }

    if (g_mailbox_mode == false && g_exited_sent == 99) {
        /*
         * Exact user scenario — `ir_tx` over BLE RPC switches to `Z2ZViewIrLive`,
         * then user presses `Back` on Flipper (`Z2ZViewMenu`) and `Back` again on Flipper
         * (`Submenu*` `malloc(4)`) to exit `zero2zero.fap` on Flipper ONLY!
         */
        g_exited_sent = 0;
        const char* ir_cmd = "{\"seq\":201,\"op\":\"ir_tx\",\"quiet\":true,\"name\":\"Pwr\",\"proto\":\"NEC\",\"addr\":4,\"cmd\":8,\"repeat\":2}";
        RpcAppSystemEvent ev_ir;
        memset(&ev_ir, 0, sizeof(ev_ir));
        ev_ir.type = RpcAppEventTypeDataExchange;
        ev_ir.data.type = RpcAppSystemEventDataTypeBytes;
        ev_ir.data.bytes.ptr = (const uint8_t*)ir_cmd;
        ev_ir.data.bytes.size = strlen(ir_cmd);
        g_rpc_cb(&ev_ir, g_rpc_cb_ctx);
        assert(g_last_view_id == 4);
        simulate_back_press();
        assert(g_last_view_id == 0);
        simulate_back_press();
        assert(g_last_view_id == VIEW_NONE);
        return;
    }

    /* 0. Click About (`Z2ZMenuAbout = 11`) -> displays FAP + Suite versions & Back returns to Menu */
    g_sm_cb(g_sm_ctx, 11);
    assert(g_last_view_id == 1);
    assert(strstr(g_last_textbox, "FAP Version: v0.1.0") != NULL);
    assert(strstr(g_last_textbox, "Kali Suite:  v0.1.0") != NULL);
    assert(strstr(g_last_textbox, "BLE RPC Duplex (Remote)") != NULL);
    simulate_back_press();
    assert(g_last_view_id == 0);

    /* 1. Toggle Alert option (Z2ZMenuAlertToggle = 8): default Vibro+LED -> Soft Beep */
    g_sm_cb(g_sm_ctx, 8);
    assert(strcmp(g_last_alert_label, "Alert: [Soft Beep]") == 0);

    /* 2. Trigger Z2ZMenuNet -> MUST defer RPC TX out of submenu callback (`180ms` quiet window) */
    g_tx_exchange[0] = '\0';
    g_sm_cb(g_sm_ctx, 1);
    assert(g_tx_exchange[0] == '\0');
    g_tick += 180;
    g_vd_tick_cb(g_vd_ctx);
    assert(strstr(g_tx_exchange, "\"op\":\"net\"") != NULL);

    const char* reply = "{\"id\":101,\"ok\":true,\"op\":\"net\",\"text\":\"CZ 41.2C\\nwlan0: 192.168.1.50\"}";
    RpcAppSystemEvent ev;
    memset(&ev, 0, sizeof(ev));
    ev.type = RpcAppEventTypeDataExchange;
    ev.data.type = RpcAppSystemEventDataTypeBytes;
    ev.data.bytes.ptr = (const uint8_t*)reply;
    ev.data.bytes.size = strlen(reply);
    g_rpc_cb(&ev, g_rpc_cb_ctx);
    assert(g_confirmed == 1);
    assert(strstr(g_last_textbox, "wlan0: 192.168.1.50") != NULL);

    /* 2b. Toggle back to Vibro+LED (8->LedOnly->Silent->Loud->Vibro+LED) and test quiet=true LED alert */
    for (int i = 0; i < 4; i++) g_sm_cb(g_sm_ctx, 8);
    assert(strcmp(g_last_alert_label, "Alert: [Vibro+LED]") == 0);
    int vb0 = g_vibro_count;
    int ld0 = g_led_count;
    const char* qreply = "{\"id\":101,\"ok\":true,\"op\":\"ping\",\"quiet\":true,\"text\":\"Kali Ready\"}";
    ev.data.bytes.ptr = (const uint8_t*)qreply;
    ev.data.bytes.size = strlen(qreply);
    g_rpc_cb(&ev, g_rpc_cb_ctx);
    assert(g_vibro_count == vb0);
    assert(g_led_count == ld0 + 1);

    /* 3. Receive clock_sync reply -> applies DateTime to furi_hal_rtc_set_datetime */
    const char* clk_reply = "{\"id\":102,\"ok\":true,\"op\":\"clock_sync\",\"rtc\":\"2026-11-13 01:20:45 5\",\"text\":\"RTC Synced\"}";
    ev.data.bytes.ptr = (const uint8_t*)clk_reply;
    ev.data.bytes.size = strlen(clk_reply);
    g_rpc_cb(&ev, g_rpc_cb_ctx);
    assert(g_rtc_last.year == 2026 && g_rtc_last.month == 11 && g_rtc_last.day == 13);
    assert(g_rtc_last.hour == 1 && g_rtc_last.minute == 20 && g_rtc_last.weekday == 5);

    /* 4. Dynamic Jobs Browser -> populates Z2ZViewJobsMenu (2); Back returns to Menu cleanly */
    const char* jobs_reply = "{\"id\":103,\"ok\":true,\"op\":\"jobs\",\"items\":\"sys-health|Kali Health\\nlan-neighbors|LAN Snapshot\",\"text\":\"2 jobs\"}";
    ev.data.bytes.ptr = (const uint8_t*)jobs_reply;
    ev.data.bytes.size = strlen(jobs_reply);
    g_rpc_cb(&ev, g_rpc_cb_ctx);
    assert(g_last_view_id == 2);
    simulate_back_press();
    assert(g_last_view_id == 0);
    g_rpc_cb(&ev, g_rpc_cb_ctx);
    assert(g_last_view_id == 2);
    assert(g_jobs_sm_cb != NULL);
    g_tx_exchange[0] = '\0';
    g_jobs_sm_cb(g_jobs_sm_ctx, 1);
    assert(g_tx_exchange[0] == '\0');
    g_tick += 180;
    g_vd_tick_cb(g_vd_ctx);
    assert(strstr(g_tx_exchange, "\"op\":\"run\",\"job\":\"lan-neighbors\"") != NULL);

    /* 5. Live CC1101 Sub-GHz -> Kali rtl_433 streaming view (Z2ZMenuSubLive = 2, view 3) */
    g_tx_exchange[0] = '\0';
    g_sm_cb(g_sm_ctx, 2);
    assert(g_last_view_id == 3);
    assert(g_subghz_running == true);
    assert(g_subghz_freq == 433920000UL);
    assert(g_subghz_isr_cb != NULL);

    /* Switch band Right -> 315.00 MHz, then Left -> back to 433.92 MHz */
    assert(g_views[3] != NULL);
    InputEvent ie_r = { .key = InputKeyRight, .type = InputTypeShort };
    assert(g_views[3]->input_cb(&ie_r, g_views[3]->ctx) == true);
    assert(g_subghz_freq == 315000000UL);
    InputEvent ie_l = { .key = InputKeyLeft, .type = InputTypeShort };
    assert(g_views[3]->input_cb(&ie_l, g_views[3]->ctx) == true);
    assert(g_subghz_freq == 433920000UL);

    /* Simulate TIM2 ISR capturing 12 mark/space pairs (24 pulses) + 10ms gap */
    g_subghz_rssi = -48.0f;
    for (int i = 0; i < 12; i++) {
        g_subghz_isr_cb(true, 480, g_subghz_isr_ctx);
        g_subghz_isr_cb(false, 960, g_subghz_isr_ctx);
    }
    g_subghz_isr_cb(false, 10000, g_subghz_isr_ctx);

    g_tick += 1000;
    g_vd_tick_cb(g_vd_ctx);
    assert(strstr(g_tx_exchange, "\"op\":\"sub_live\"") != NULL);
    assert(strstr(g_tx_exchange, "\"freq\":433920000") != NULL);
    assert(strstr(g_tx_exchange, "480 -960") != NULL);

    /* Kali `rtl_433` replies in-place without switching away from Z2ZViewSubLive (3) */
    int vb_before = g_vibro_count;
    const char* sub_reply = "{\"id\":104,\"ok\":true,\"op\":\"sub_live\",\"matched\":true,\"quiet\":false,\"line1\":\"Oregon-THGR122N\",\"line2\":\"id=142 22.4C RH=54%\",\"line3\":\"433.92M RSSI:-48dBm\"}";
    ev.data.bytes.ptr = (const uint8_t*)sub_reply;
    ev.data.bytes.size = strlen(sub_reply);
    g_rpc_cb(&ev, g_rpc_cb_ctx);
    assert(g_last_view_id == 3);
    assert(g_vibro_count == vb_before + 1);

    /* Leaving view via Back stops CC1101 async RX and puts transceiver to sleep */
    g_subghz_slept = false;
    simulate_back_press();
    assert(g_last_view_id == 0);
    assert(g_subghz_running == false);
    assert(g_subghz_slept == true);

    /* 6. Live BLE IR Blaster (`op="ir_tx"`) + `Z2ZMenuIrLive` (10 -> view 4) RX/Replay */
    /* TX-only `op="ir_tx"` MUST NOT allocate ~19.5 KB `InfraredWorker` RX! */
    assert(g_ir_worker_alloc_count == 0);
    assert(g_ir_worker_free_count == 0);
    assert(g_views[4] != NULL);
    const char* ir_tx_cmd = "{\"seq\":105,\"op\":\"ir_tx\",\"quiet\":true,\"name\":\"Menu\",\"proto\":\"NECext\",\"addr\":34798,\"cmd\":39007,\"repeat\":2}";
    ev.data.bytes.ptr = (const uint8_t*)ir_tx_cmd;
    ev.data.bytes.size = strlen(ir_tx_cmd);
    g_rpc_cb(&ev, g_rpc_cb_ctx);
    assert(g_ir_send_count == 1);
    assert(g_ir_last_sent.protocol == InfraredProtocolNECext);
    assert(g_ir_last_sent.address == 34798UL);
    assert(g_ir_last_sent.command == 39007UL);
    assert(g_last_view_id == 4);
    assert(g_ir_rx_started == false);
    assert(g_ir_worker_alloc_count == 0);
    assert(g_ir_worker_free_count == 0);

    /* Raw IR blast also keeps `InfraredWorker` unallocated in TX-only mode */
    const char* ir_raw_cmd = "{\"seq\":106,\"op\":\"ir_tx\",\"quiet\":true,\"name\":\"Pwr\",\"freq\":38000,\"duty\":33,\"raw\":\"9000 4500 560 560 560\"}";
    ev.data.bytes.ptr = (const uint8_t*)ir_raw_cmd;
    ev.data.bytes.size = strlen(ir_raw_cmd);
    g_rpc_cb(&ev, g_rpc_cb_ctx);
    assert(g_ir_send_raw_count == 1);
    assert(g_ir_rx_started == false);
    assert(g_ir_worker_alloc_count == 0);
    assert(g_ir_worker_free_count == 0);

    /* Pressing Down in Z2ZViewIrLive explicitly toggles RX Learn ON (allocating InfraredWorker) */
    InputEvent ie_dn = { .key = InputKeyDown, .type = InputTypeShort };
    assert(g_views[4]->input_cb(&ie_dn, g_views[4]->ctx) == true);
    assert(g_ir_rx_started == true);
    assert(g_ir_worker_alloc_count == 1);
    assert(g_ir_worker_free_count == 0);

    /* Simulate `InfraredWorker` decoding an NEC button -> forwards `op="ir_rx"` to Kali */
    g_tick += 500;
    g_tx_exchange[0] = '\0';
    InfraredWorkerSignal ir_sig = {
        .decoded = true,
        .msg = { .protocol = InfraredProtocolNEC, .address = 0x04, .command = 0x08, .repeat = false },
        .raw = NULL,
        .raw_cnt = 0,
    };
    assert(g_ir_rx_cb != NULL);
    g_ir_rx_cb(g_ir_rx_ctx, &ir_sig);
    assert(strstr(g_tx_exchange, "\"op\":\"ir_rx\"") != NULL);
    assert(strstr(g_tx_exchange, "\"proto\":\"NEC\"") != NULL);
    assert(strstr(g_tx_exchange, "\"addr\":4") != NULL);
    assert(strstr(g_tx_exchange, "\"cmd\":8") != NULL);

    /* Pressing OK in Z2ZViewIrLive replays last learned signal, pausing/resuming RX cleanly */
    InputEvent ie_ok = { .key = InputKeyOk, .type = InputTypeShort };
    assert(g_views[4]->input_cb(&ie_ok, g_views[4]->ctx) == true);
    assert(g_ir_send_count == 2);
    assert(g_ir_last_sent.address == 0x04UL && g_ir_last_sent.command == 0x08UL);
    assert(g_ir_rx_started == true);
    assert(g_ir_worker_alloc_count == 1);
    assert(g_ir_worker_free_count == 0);

    /* Leaving Z2ZViewIrLive via Back immediately frees all ~19.5 KB of `InfraredWorker` heap! */
    simulate_back_press();
    assert(g_last_view_id == 0);
    assert(g_ir_rx_started == false);
    assert(g_ir_worker_free_count == 1);

    /* Selecting `IR Blaster & Learn` from Flipper menu (`Z2ZMenuIrLive = 10`) starts RX */
    g_sm_cb(g_sm_ctx, 10);
    assert(g_last_view_id == 4);
    assert(g_ir_rx_started == true);
    assert(g_ir_worker_alloc_count == 2);
    simulate_back_press();
    assert(g_last_view_id == 0);
    assert(g_ir_rx_started == false);
    assert(g_ir_worker_free_count == 2);

    /* Remote `op="ir_rx_start"` switches Flipper directly into `Z2ZViewIrLive` RX mode */
    const char* ir_arm_cmd = "{\"seq\":107,\"op\":\"ir_rx_start\"}";
    ev.data.bytes.ptr = (const uint8_t*)ir_arm_cmd;
    ev.data.bytes.size = strlen(ir_arm_cmd);
    g_rpc_cb(&ev, g_rpc_cb_ctx);
    assert(g_last_view_id == 4);
    assert(g_ir_rx_started == true);
    assert(g_ir_worker_alloc_count == 3);

    /* `RpcAppEventTypeAppExit` transitions cleanly to `VIEW_NONE`, freeing `InfraredWorker` */
    memset(&ev, 0, sizeof(ev));
    ev.type = RpcAppEventTypeAppExit;
    g_rpc_cb(&ev, g_rpc_cb_ctx);
    assert(g_last_view_id == VIEW_NONE);
    assert(g_ir_rx_started == false);
    assert(g_ir_worker_free_count == 3);
    assert(g_vd_stopped == 1);

    /* DataExchange arriving while `app_exiting == true` MUST confirm `false`! */
    int rej_before = g_rejected;
    memset(&ev, 0, sizeof(ev));
    ev.type = RpcAppEventTypeDataExchange;
    ev.data.type = RpcAppSystemEventDataTypeBytes;
    ev.data.bytes.ptr = (const uint8_t*)ir_tx_cmd;
    ev.data.bytes.size = strlen(ir_tx_cmd);
    g_rpc_cb(&ev, g_rpc_cb_ctx);
    assert(g_rejected == rej_before + 1);
}
"""


class FapAndPackagingTests(unittest.TestCase):
    def test_flipper_fap_compiles_cleanly_and_handles_rpc_duplex(self):
        with tempfile.TemporaryDirectory() as tmp:
            inc = Path(tmp) / "inc"
            for sub in (
                "datetime",
                "gui/modules",
                "input",
                "lib/subghz/devices",
                "notification",
                "rpc",
                "storage",
            ):
                (inc / sub).mkdir(parents=True, exist_ok=True)
            # Create empty stub headers satisfied by FAP_STUB_HARNESS_C
            for hdr in (
                "furi.h",
                "furi_hal_rtc.h",
                "furi_hal_subghz.h",
                "lib/subghz/devices/cc1101_configs.h",
                "datetime/datetime.h",
                "gui/canvas.h",
                "gui/gui.h",
                "gui/view.h",
                "gui/view_dispatcher.h",
                "gui/modules/submenu.h",
                "gui/modules/text_box.h",
                "infrared.h",
                "infrared_transmit.h",
                "infrared_worker.h",
                "input/input.h",
                "notification/notification_messages.h",
                "rpc/rpc_app.h",
                "storage/storage.h",
            ):
                (inc / hdr).write_text("/* stub */\n", encoding="utf-8")

            fap_src = (REPO_ROOT / "flipper_app" / "zero2zero_app.c").read_text(encoding="utf-8")
            combined = (
                FAP_STUB_HARNESS_C
                + "\n"
                + fap_src
                + r"""
int main(void) {
    char arg[64];
    g_mailbox_mode = false;
    g_test_session_close = false;
    snprintf(arg, sizeof(arg), "RPC %lX", (unsigned long)(uintptr_t)&g_fake_rpc);
    int rc = zero2zero_app(arg);
    assert(rc == 0);
    assert(g_exited_sent == 1);
    assert(g_rpc_cb == NULL);

    /* Run 2: Verify `SessionClose` on RPC-launched `.fap` exits cleanly without `send_exited` UAF */
    g_started_sent = 0;
    g_exited_sent = 0;
    g_test_session_close = true;
    int rc_sc = zero2zero_app(arg);
    assert(rc_sc == 0);
    assert(g_exited_sent == 0);
    assert(g_rpc_cb == NULL);
    g_test_session_close = false;

    /* Run 3: `ir_tx` over BLE RPC followed by exiting `zero2zero.fap` on Flipper ONLY */
    g_started_sent = 0;
    g_exited_sent = 99;
    int rc_ir_exit = zero2zero_app(arg);
    assert(rc_ir_exit == 0);
    assert(g_exited_sent == 1);
    assert(g_rpc_cb == NULL);

    g_mailbox_mode = true;
    int rc2 = zero2zero_app(NULL);
    assert(rc2 == 0);
    return 0;
}
"""
            )
            harness_c = Path(tmp) / "harness.c"
            harness_bin = Path(tmp) / "harness"
            harness_c.write_text(combined, encoding="utf-8")

            subprocess.run(
                [
                    "gcc",
                    "-O2",
                    "-g",
                    "-fsanitize=address,undefined",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    f"-I{inc}",
                    str(harness_c),
                    "-o",
                    str(harness_bin),
                ],
                check=True,
            )
            subprocess.run([str(harness_bin)], check=True)

    def test_applaunch_registration_and_removal(self):
        spec = importlib.util.spec_from_file_location(
            "register_applaunch", REPO_ROOT / "packaging" / "register-applaunch.py"
        )
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "usr/share/APPLaunch").mkdir(parents=True)
            (root / "home/kali/.config").mkdir(parents=True)

            self.assertTrue(mod.register(root, remove=False))
            dfile = root / "usr/share/APPLaunch/applications/zero2zero.desktop"
            ifile = root / "usr/share/APPLaunch/share/images/zero2zero_100.png"
            tsv = root / "usr/share/APPLaunch/preinstalled-desktop-apps.tsv"
            cfg = root / "home/kali/.config/cardputerzero/config.json"

            self.assertTrue(dfile.is_file())
            self.assertTrue(ifile.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertIn("zero2zero.desktop\tZero2Zero\t", tsv.read_text(encoding="utf-8"))

            expected_key = mod.desktop_config_key("zero2zero.desktop")
            cfg_obj = json.loads(cfg.read_text(encoding="utf-8"))
            self.assertEqual(cfg_obj[expected_key], 1)

            # Verify clean removal
            self.assertTrue(mod.register(root, remove=True))
            self.assertFalse(dfile.exists())
            self.assertNotIn("zero2zero.desktop", tsv.read_text(encoding="utf-8"))
            cfg_after = json.loads(cfg.read_text(encoding="utf-8"))
            self.assertNotIn(expected_key, cfg_after)

    def test_compiled_fap_binary_and_auto_provision_on_launch(self):
        import struct
        import sys

        sys.path.insert(0, str(REPO_ROOT / "python"))
        sys.path.insert(0, str(REPO_ROOT / "tests"))
        from test_proto_and_rpc import SimulatedFlipperTransport
        from z2z.daemon import DEFAULT_FAP_PATH, Zero2ZeroDaemon, find_bundled_fap

        fap_bin = REPO_ROOT / "flipper_app" / "dist" / "zero2zero.fap"
        self.assertTrue(fap_bin.is_file(), f"Expected compiled .fap at {fap_bin}")
        raw = fap_bin.read_bytes()

        # ELF32 little-endian relocatable (ET_REL=1) for ARM (EM_ARM=40)
        self.assertEqual(raw[:6], b"\x7fELF\x01\x01")
        e_type, e_machine = struct.unpack_from("<HH", raw, 16)
        self.assertEqual(e_type, 1)
        self.assertEqual(e_machine, 40)
        self.assertIn(b".fapmeta", raw)
        self.assertIn(b".fast.rel.text", raw)
        self.assertIn(b"zero2zero", raw)
        self.assertEqual(find_bundled_fap(), fap_bin)

        # Verify `application.fam` sets >= 6 KB stack and `zero2zero_app.c` avoids
        # stack-allocated `Z2Z_TEXT_MAX` arrays and newlib `sscanf` (prevents Cortex-M4 MPU fault)
        fam_txt = (REPO_ROOT / "flipper_app" / "application.fam").read_text(encoding="utf-8")
        c_txt = (REPO_ROOT / "flipper_app" / "zero2zero_app.c").read_text(encoding="utf-8")
        icon_png = REPO_ROOT / "flipper_app" / "zero2zero_10px.png"
        self.assertTrue(icon_png.is_file())
        png_hdr = icon_png.read_bytes()
        self.assertEqual(png_hdr[:8], b"\x89PNG\r\n\x1a\n")
        icon_w, icon_h = struct.unpack_from(">II", png_hdr, 16)
        self.assertEqual((icon_w, icon_h), (10, 10))
        self.assertIn('fap_icon="zero2zero_10px.png"', fam_txt)
        self.assertIn('fap_version="0.1.0"', fam_txt)
        self.assertIn("stack_size=6 * 1024", fam_txt)
        self.assertNotIn("sscanf(", c_txt)
        self.assertNotIn("char local_copy[Z2Z_TEXT_MAX]", c_txt)
        self.assertNotIn("char buf[Z2Z_TEXT_MAX]", c_txt)

        # First `fap_launch` on a Flipper missing `/ext/apps/Bluetooth/zero2zero.fap`
        # must unlock any running app, stage `.tmp` + verify MD5, and launch `app_start(..., "RPC")`
        sim = SimulatedFlipperTransport()
        sim.app_locked = True
        self.assertNotIn(DEFAULT_FAP_PATH, sim.files)
        with tempfile.TemporaryDirectory() as tmp:
            daemon = Zero2ZeroDaemon(socket_path=os.path.join(tmp, "rpc.sock"), transport=sim)
            r1 = daemon.handle_ipc_request({"op": "fap_launch"})
            self.assertTrue(r1["ok"])
            self.assertTrue(r1["auto_installed"])
            self.assertEqual(sim.files[DEFAULT_FAP_PATH], raw)
            self.assertNotIn(DEFAULT_FAP_PATH + ".tmp", sim.files)

            # Subsequent launch skips re-uploading when both size AND MD5 match
            r2 = daemon.handle_ipc_request({"op": "fap_launch"})
            self.assertTrue(r2["ok"])
            self.assertFalse(r2["auto_installed"])

            # Same-size corrupted/old `.fap` on Flipper SD MUST be detected via MD5 and replaced!
            sim.files[DEFAULT_FAP_PATH] = b"\x00" * len(raw)
            r3 = daemon.handle_ipc_request({"op": "fap_launch"})
            self.assertTrue(r3["ok"])
            self.assertTrue(r3["auto_installed"])
            self.assertEqual(sim.files[DEFAULT_FAP_PATH], raw)


if __name__ == "__main__":
    unittest.main()
