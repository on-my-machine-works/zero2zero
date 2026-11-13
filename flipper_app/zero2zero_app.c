/*
 * zero2zero_app.c — Flipper Zero Bi-Directional BLE Companion App for CardputerZero
 *
 * Supports two zero-config transport modes over Flipper's native BLE stack:
 *   1. RPC Duplex Mode (`args = "RPC <hex_ptr>"`): exchanges live JSON frames
 *      with `z2zd` via `rpc_system_app_exchange_data` / `RpcAppEventTypeDataExchange`
 *      without disrupting Flipper's BLE serial or storage services.
 *   2. Pocket Mailbox Mode (manual launch on Flipper): writes requests to
 *      `/ext/apps_data/zero2zero/request.json` and polls `response.json` while
 *      CardputerZero runs `z2z watch on` in a pocket or backpack.
 *
 * Also includes a live CC1101 Sub-GHz OOK pulse + RSSI capture view that streams
 * received RF bursts over BLE into Kali Linux's preinstalled `rtl_433` engine.
 */

#include <furi.h>
#include <furi_hal_rtc.h>
#include <furi_hal_subghz.h>
#include <lib/subghz/devices/cc1101_configs.h>
#include <datetime/datetime.h>
#include <gui/canvas.h>
#include <gui/gui.h>
#include <gui/modules/submenu.h>
#include <gui/modules/text_box.h>
#include <gui/view.h>
#include <gui/view_dispatcher.h>
#include <infrared.h>
#include <infrared_transmit.h>
#include <infrared_worker.h>
#include <input/input.h>
#include <notification/notification_messages.h>
#include <rpc/rpc_app.h>
#include <storage/storage.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define Z2Z_FAP_VERSION     "0.1.0"
#define Z2Z_SUITE_VERSION   "0.1.0"
#define Z2Z_DATA_DIR        EXT_PATH("apps_data/zero2zero")
#define Z2Z_REQ_PATH        EXT_PATH("apps_data/zero2zero/request.json")
#define Z2Z_RESP_PATH       EXT_PATH("apps_data/zero2zero/response.json")
#define Z2Z_CFG_PATH        EXT_PATH("apps_data/zero2zero/settings.cfg")
#define Z2Z_LIVE_SUB_PATH   EXT_PATH("subghz/z2z_live.sub")
#define Z2Z_LEARNED_IR_PATH EXT_PATH("infrared/z2z_learned.ir")
#define Z2Z_TEXT_MAX        2048
#define Z2Z_MAX_JOBS        16
#define Z2Z_IR_RAW_MAX      256U

#define Z2Z_SUB_RING_CAP  512U
#define Z2Z_SUB_RING_MASK 511U
#define Z2Z_SUB_BURST_MAX 140U
#define Z2Z_SUB_FREQ_CNT  4U

static const uint32_t z2z_sub_freqs[Z2Z_SUB_FREQ_CNT] = {
    433920000UL,
    315000000UL,
    868350000UL,
    915000000UL,
};

typedef enum {
    Z2ZViewMenu = 0,
    Z2ZViewOutput = 1,
    Z2ZViewJobsMenu = 2,
    Z2ZViewSubLive = 3,
    Z2ZViewIrLive = 4,
} Z2ZViewId;

typedef enum {
    Z2ZMenuPing = 0,
    Z2ZMenuNet = 1,
    Z2ZMenuSubLive = 2,
    Z2ZMenuCapLatest = 3,
    Z2ZMenuSubLatest = 4,
    Z2ZMenuVault = 5,
    Z2ZMenuJobs = 6,
    Z2ZMenuClockSync = 7,
    Z2ZMenuAlertToggle = 8,
    Z2ZMenuLastOutput = 9,
    Z2ZMenuIrLive = 10,
    Z2ZMenuAbout = 11,
} Z2ZMenuIndex;

typedef enum {
    Z2ZRpcNone = 0,
    Z2ZRpcActive = 1,
    Z2ZRpcSending = 2,
    Z2ZRpcClosingAfterSend = 3,
    Z2ZRpcExiting = 4,
    Z2ZRpcClosed = 5,
} Z2ZRpcState;

typedef enum {
    Z2ZAlertVibro = 0,
    Z2ZAlertSoftBeep = 1,
    Z2ZAlertLedOnly = 2,
    Z2ZAlertSilent = 3,
    Z2ZAlertLoud = 4,
    Z2ZAlertCount = 5,
} Z2ZAlertMode;

typedef enum {
    Z2ZEventRxFrame = 100,
    Z2ZEventRpcExit = 101,
    Z2ZEventRpcClosed = 102,
    Z2ZEventIrRx = 103,
} Z2ZCustomEvent;

typedef struct {
    uint32_t freq_hz;
    int16_t rssi_dbm;
    uint32_t burst_count;
    bool paused;
    bool duplex;
    char line1[28];
    char line2[28];
    char line3[28];
} Z2ZSubLiveModel;

typedef struct {
    uint32_t rx_count;
    uint32_t tx_count;
    bool rx_listening;
    bool has_signal;
    bool duplex;
    char line1[36];
    char line2[36];
    char line3[36];
} Z2ZIrLiveModel;

typedef struct {
    Gui* gui;
    Storage* storage;
    NotificationApp* notifications;
    ViewDispatcher* view_dispatcher;
    Submenu* submenu;
    Submenu* jobs_submenu;
    TextBox* text_box;
    View* sub_live_view;
    View* ir_live_view;
    FuriString* text_buf;
    FuriMutex* rx_mutex;
    FuriMutex* rpc_mutex;
    char pending_rx[Z2Z_TEXT_MAX];
    char work_buf[Z2Z_TEXT_MAX];

    Z2ZAlertMode alert_mode;
    char alert_label[32];

    char job_ids[Z2Z_MAX_JOBS][28];
    char job_labels[Z2Z_MAX_JOBS][36];
    uint8_t job_count;

    RpcAppSystem* rpc_ctx;
    volatile uint8_t rpc_state;
    volatile bool app_exiting;
    bool started_by_rpc;
    uint32_t seq;
    bool waiting_mailbox;
    uint32_t mailbox_poll_ticks;
    uint32_t mailbox_subtick;
    char pending_tx_buf[1408];
    size_t pending_tx_len;
    uint32_t pending_tx_due_tick;

    /* Live CC1101 OOK RX -> Kali `rtl_433` state (lock-free SPSC ring for TIM2 ISR) */
    volatile bool sub_rx_active;
    volatile bool sub_rx_paused;
    uint8_t sub_freq_idx;
    uint32_t sub_actual_freq;
    volatile uint16_t sub_rx_head;
    volatile uint16_t sub_rx_tail;
    int32_t sub_rx_ring[Z2Z_SUB_RING_CAP];
    int32_t sub_burst[Z2Z_SUB_BURST_MAX];
    uint16_t sub_burst_len;
    int32_t sub_last_saved[Z2Z_SUB_BURST_MAX];
    uint16_t sub_last_saved_len;
    int16_t sub_peak_rssi;
    uint32_t sub_burst_total;
    uint32_t sub_last_tx_tick;

    /* High-Power IR Blaster & Receiver (`InfraredWorker` + `infrared_send*`) */
    InfraredWorker* ir_worker;
    volatile bool ir_rx_running;
    bool ir_want_rx;
    bool ir_view_open;
    bool ir_has_last;
    bool ir_last_decoded;
    InfraredMessage ir_last_msg;
    uint32_t ir_last_raw[Z2Z_IR_RAW_MAX];
    uint16_t ir_last_raw_len;
    uint32_t ir_last_freq;
    uint32_t ir_last_duty_pct;
    uint32_t ir_tx_raw[Z2Z_IR_RAW_MAX];
    uint32_t ir_rx_count;
    uint32_t ir_tx_count;
    uint32_t ir_last_tx_tick;
} Zero2ZeroApp;

/* Gentle 12% volume 880Hz chirp (25ms) instead of Flipper's loud alarm siren */
static const NotificationMessage z2z_msg_soft_tone = {
    .type = NotificationMessageTypeSoundOn,
    .data.sound = {
        .frequency = 880.0f,
        .volume = 0.12f,
    },
};

static const NotificationSequence z2z_seq_vibro_led = {
    &message_green_255,
    &message_vibro_on,
    &message_delay_50,
    &message_vibro_off,
    &message_delay_50,
    &message_green_0,
    NULL,
};

static const NotificationSequence z2z_seq_soft_beep = {
    &message_green_255,
    &z2z_msg_soft_tone,
    &message_delay_25,
    &message_sound_off,
    &message_delay_50,
    &message_green_0,
    NULL,
};

static const NotificationSequence z2z_seq_led_only = {
    &message_green_255,
    &message_delay_100,
    &message_green_0,
    NULL,
};

static const char* z2z_alert_key(Z2ZAlertMode m) {
    switch(m) {
    case Z2ZAlertSoftBeep:
        return "soft";
    case Z2ZAlertLedOnly:
        return "led";
    case Z2ZAlertSilent:
        return "off";
    case Z2ZAlertLoud:
        return "loud";
    case Z2ZAlertVibro:
    default:
        return "vibro";
    }
}

static const char* z2z_alert_title(Z2ZAlertMode m) {
    switch(m) {
    case Z2ZAlertSoftBeep:
        return "Soft Beep";
    case Z2ZAlertLedOnly:
        return "LED Only";
    case Z2ZAlertSilent:
        return "Silent";
    case Z2ZAlertLoud:
        return "Loud";
    case Z2ZAlertVibro:
    default:
        return "Vibro+LED";
    }
}

static void z2z_play_alert(Zero2ZeroApp* app, bool quiet) {
    if(!app || __atomic_load_n(&app->app_exiting, __ATOMIC_ACQUIRE)) return;
    switch(app->alert_mode) {
    case Z2ZAlertVibro:
        if(quiet) {
            notification_message(app->notifications, &z2z_seq_led_only);
        } else {
            notification_message(app->notifications, &z2z_seq_vibro_led);
        }
        break;
    case Z2ZAlertSoftBeep:
        notification_message(app->notifications, &z2z_seq_soft_beep);
        break;
    case Z2ZAlertLedOnly:
        notification_message(app->notifications, &z2z_seq_led_only);
        break;
    case Z2ZAlertLoud:
        notification_message(app->notifications, &sequence_success);
        break;
    case Z2ZAlertSilent:
    default:
        break;
    }
}

static void z2z_load_settings(Zero2ZeroApp* app) {
    app->alert_mode = Z2ZAlertVibro;
    File* f = storage_file_alloc(app->storage);
    if(storage_file_open(f, Z2Z_CFG_PATH, FSAM_READ, FSOM_OPEN_EXISTING)) {
        char buf[128];
        size_t n = storage_file_read(f, buf, sizeof(buf) - 1);
        buf[n] = '\0';
        storage_file_close(f);
        if(strstr(buf, "alert_mode=soft"))
            app->alert_mode = Z2ZAlertSoftBeep;
        else if(strstr(buf, "alert_mode=led"))
            app->alert_mode = Z2ZAlertLedOnly;
        else if(strstr(buf, "alert_mode=off"))
            app->alert_mode = Z2ZAlertSilent;
        else if(strstr(buf, "alert_mode=loud"))
            app->alert_mode = Z2ZAlertLoud;
        else
            app->alert_mode = Z2ZAlertVibro;
    }
    storage_file_free(f);
    snprintf(
        app->alert_label, sizeof(app->alert_label), "Alert: [%s]", z2z_alert_title(app->alert_mode));
}

static void z2z_save_settings(Zero2ZeroApp* app) {
    storage_simply_mkdir(app->storage, Z2Z_DATA_DIR);
    File* f = storage_file_alloc(app->storage);
    if(storage_file_open(f, Z2Z_CFG_PATH, FSAM_WRITE, FSOM_CREATE_ALWAYS)) {
        char line[48];
        int len = snprintf(line, sizeof(line), "alert_mode=%s\n", z2z_alert_key(app->alert_mode));
        if(len > 0) storage_file_write(f, line, (size_t)len);
        storage_file_close(f);
    }
    storage_file_free(f);
}

/* Extract a JSON string field (`"<key>":"..."`) into `FuriString`, unescaping `\n` and `\t` */
static bool z2z_extract_json_field(const char* json, const char* key, FuriString* out) {
    char pat[32];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char* p = strstr(json, pat);
    if(!p) return false;
    p += strlen(pat);
    while(*p == ' ' || *p == ':' || *p == '\t') p++;
    if(*p != '"') return false;
    p++;
    furi_string_reset(out);
    while(*p && *p != '"') {
        if(*p == '\\' && p[1]) {
            p++;
            if(*p == 'n')
                furi_string_push_back(out, '\n');
            else if(*p == 't')
                furi_string_push_back(out, ' ');
            else if(*p != 'r')
                furi_string_push_back(out, *p);
            p++;
        } else {
            furi_string_push_back(out, *p++);
        }
    }
    return true;
}

static bool z2z_extract_json_bool(const char* json, const char* key, bool* out) {
    char pat[32];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char* p = strstr(json, pat);
    if(!p) return false;
    p += strlen(pat);
    while(*p == ' ' || *p == ':' || *p == '\t') p++;
    if(strncmp(p, "true", 4) == 0) {
        *out = true;
        return true;
    }
    if(strncmp(p, "false", 5) == 0) {
        *out = false;
        return true;
    }
    return false;
}

static bool z2z_extract_json_u32(const char* json, const char* key, uint32_t* out) {
    char pat[32];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char* p = strstr(json, pat);
    if(!p) return false;
    p += strlen(pat);
    while(*p == ' ' || *p == ':' || *p == '\t') p++;
    if(*p == '"') p++;
    char* end = NULL;
    unsigned long v = strtoul(p, &end, 0);
    if(end == p) return false;
    *out = (uint32_t)v;
    return true;
}

static bool z2z_json_matches_seq(const char* json, uint32_t expected_seq) {
    char needle_seq[32];
    char needle_id[32];
    snprintf(needle_seq, sizeof(needle_seq), "\"seq\": %lu", (unsigned long)expected_seq);
    snprintf(needle_id, sizeof(needle_id), "\"seq\":%lu", (unsigned long)expected_seq);
    return (strstr(json, needle_seq) != NULL) || (strstr(json, needle_id) != NULL);
}

static bool z2z_rpc_is_active(Zero2ZeroApp* app) {
    if(!app) return false;
    uint8_t st = __atomic_load_n(&app->rpc_state, __ATOMIC_ACQUIRE);
    return (st == Z2ZRpcActive || st == Z2ZRpcSending);
}

static void z2z_post_event_if_alive(Zero2ZeroApp* app, uint32_t ev) {
    furi_mutex_acquire(app->rpc_mutex, FuriWaitForever);
    if(!__atomic_load_n(&app->app_exiting, __ATOMIC_ACQUIRE) && app->view_dispatcher) {
        view_dispatcher_send_custom_event(app->view_dispatcher, ev);
    }
    furi_mutex_release(app->rpc_mutex);
}

static bool z2z_rpc_send_exchange(Zero2ZeroApp* app, const uint8_t* data, size_t len) {
    if(!app || !data || len == 0 || __atomic_load_n(&app->app_exiting, __ATOMIC_ACQUIRE)) {
        return false;
    }
    uint8_t expected = Z2ZRpcActive;
    if(!__atomic_compare_exchange_n(
           &app->rpc_state, &expected, Z2ZRpcSending, false, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
        return false;
    }
    RpcAppSystem* rpc = __atomic_load_n(&app->rpc_ctx, __ATOMIC_ACQUIRE);
    bool sent = false;
    if(rpc && !__atomic_load_n(&app->app_exiting, __ATOMIC_ACQUIRE)) {
        rpc_system_app_exchange_data(rpc, data, len);
        sent = true;
    }
    uint8_t send_st = Z2ZRpcSending;
    if(!__atomic_compare_exchange_n(
           &app->rpc_state, &send_st, Z2ZRpcActive, false, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
        __atomic_store_n(&app->rpc_ctx, NULL, __ATOMIC_RELEASE);
        if(rpc) {
            rpc_system_app_set_callback(rpc, NULL, NULL);
        }
        __atomic_store_n(&app->rpc_state, Z2ZRpcClosed, __ATOMIC_RELEASE);
        z2z_post_event_if_alive(
            app, app->started_by_rpc ? Z2ZEventRpcExit : Z2ZEventRpcClosed);
        return false;
    }
    return sent;
}

/* --- Live CC1101 OOK Pulse Capture & `rtl_433` Bridge --- */

/*
 * TIM2 input-capture ISR callback (`FURI_IS_IRQ_MODE()`):
 * Strictly lock-free SPSC ring write — NO mutexes, NO allocations, NO GUI/RPC calls.
 */
static void z2z_subghz_rx_isr_cb(bool level, uint32_t duration, void* context) {
    Zero2ZeroApp* app = context;
    if(!app || !app->sub_rx_active || app->sub_rx_paused) return;
    if(duration < 65U) return;
    if(duration > 100000U) duration = 100000U;

    uint16_t head = __atomic_load_n(&app->sub_rx_head, __ATOMIC_RELAXED);
    uint16_t next = (uint16_t)((head + 1U) & Z2Z_SUB_RING_MASK);
    uint16_t tail = __atomic_load_n(&app->sub_rx_tail, __ATOMIC_ACQUIRE);
    if(next == tail) return;

    app->sub_rx_ring[head] = level ? (int32_t)duration : -(int32_t)duration;
    __atomic_store_n(&app->sub_rx_head, next, __ATOMIC_RELEASE);
}

static void z2z_sub_stop_rx(Zero2ZeroApp* app) {
    if(app->sub_rx_active) {
        app->sub_rx_active = false;
        furi_hal_subghz_stop_async_rx();
        furi_hal_subghz_sleep();
    }
}

static void z2z_sub_start_rx(Zero2ZeroApp* app) {
    z2z_sub_stop_rx(app);

    uint32_t req_freq = z2z_sub_freqs[app->sub_freq_idx % Z2Z_SUB_FREQ_CNT];
    if(!furi_hal_subghz_is_frequency_valid(req_freq)) {
        req_freq = 433920000UL;
        app->sub_freq_idx = 0;
    }

    __atomic_store_n(&app->sub_rx_head, 0U, __ATOMIC_RELAXED);
    __atomic_store_n(&app->sub_rx_tail, 0U, __ATOMIC_RELAXED);
    app->sub_burst_len = 0;
    app->sub_peak_rssi = -110;

    furi_hal_subghz_sleep();
    furi_hal_subghz_load_custom_preset(subghz_device_cc1101_preset_ook_650khz_async_regs);
    app->sub_actual_freq = furi_hal_subghz_set_frequency_and_path(req_freq);
    furi_hal_subghz_flush_rx();
    app->sub_rx_active = true;
    furi_hal_subghz_start_async_rx(z2z_subghz_rx_isr_cb, app);

    bool duplex = z2z_rpc_is_active(app);
    with_view_model(
        app->sub_live_view,
        Z2ZSubLiveModel * m,
        {
            m->freq_hz = app->sub_actual_freq ? app->sub_actual_freq : req_freq;
            m->rssi_dbm = -95;
            m->paused = app->sub_rx_paused;
            m->duplex = duplex;
            if(m->burst_count == 0) {
                snprintf(m->line1, sizeof(m->line1), "CC1101 OOK 650kHz RX");
                snprintf(
                    m->line2,
                    sizeof(m->line2),
                    "%s",
                    duplex ? "BLE -> Kali rtl_433" : "Standalone ([Up]=Save)");
                snprintf(m->line3, sizeof(m->line3), "[<>]Band [OK]Pause");
            }
        },
        true);
}

static void z2z_sub_live_enter_cb(void* context) {
    Zero2ZeroApp* app = context;
    app->sub_rx_paused = false;
    z2z_sub_start_rx(app);
}

static void z2z_sub_live_exit_cb(void* context) {
    Zero2ZeroApp* app = context;
    z2z_sub_stop_rx(app);
}

static bool z2z_save_last_sub_burst(Zero2ZeroApp* app) {
    if(app->sub_last_saved_len < 8) return false;
    storage_simply_mkdir(app->storage, EXT_PATH("subghz"));
    File* f = storage_file_alloc(app->storage);
    bool ok = false;
    if(storage_file_open(f, Z2Z_LIVE_SUB_PATH, FSAM_WRITE, FSOM_CREATE_ALWAYS)) {
        char hdr[160];
        uint32_t f_hz = app->sub_actual_freq ? app->sub_actual_freq : 433920000UL;
        int hlen = snprintf(
            hdr,
            sizeof(hdr),
            "Filetype: Flipper SubGhz RAW File\n"
            "Version: 1\n"
            "Frequency: %lu\n"
            "Preset: FuriHalSubGhzPresetOok650Async\n"
            "Protocol: RAW\n"
            "RAW_Data:",
            (unsigned long)f_hz);
        if(hlen > 0) storage_file_write(f, hdr, (size_t)hlen);
        for(uint16_t i = 0; i < app->sub_last_saved_len; i++) {
            char tok[16];
            int tlen = snprintf(tok, sizeof(tok), " %ld", (long)app->sub_last_saved[i]);
            if(tlen > 0) storage_file_write(f, tok, (size_t)tlen);
        }
        storage_file_write(f, "\n", 1);
        storage_file_close(f);
        ok = true;
    }
    storage_file_free(f);
    return ok;
}

static void z2z_sub_live_draw_cb(Canvas* canvas, void* model) {
    Z2ZSubLiveModel* m = model;
    canvas_clear(canvas);

    canvas_set_font(canvas, FontPrimary);
    char hdr[48];
    uint32_t mhz_i = m->freq_hz / 1000000UL;
    uint32_t mhz_f = (m->freq_hz % 1000000UL) / 10000UL;
    snprintf(
        hdr,
        sizeof(hdr),
        "<%u.%02uM> %s",
        (unsigned)mhz_i,
        (unsigned)(mhz_f % 100UL),
        m->paused ? "PAUSED" : "rtl_433");
    canvas_draw_str(canvas, 2, 10, hdr);

    /* RSSI bar (-95..-30 dBm mapped to 0..82 px) */
    canvas_draw_frame(canvas, 2, 13, 84, 8);
    int32_t clamped = m->rssi_dbm;
    if(clamped < -95) clamped = -95;
    if(clamped > -30) clamped = -30;
    size_t fill_w = (size_t)(((clamped + 95) * 80) / 65);
    if(fill_w > 0) {
        canvas_draw_box(canvas, 4, 15, fill_w, 4);
    }

    canvas_set_font(canvas, FontSecondary);
    char rssi_txt[20];
    snprintf(rssi_txt, sizeof(rssi_txt), "%ddB", (int)m->rssi_dbm);
    canvas_draw_str(canvas, 89, 20, rssi_txt);

    canvas_draw_str(canvas, 2, 33, m->line1);
    canvas_draw_str(canvas, 2, 44, m->line2);
    canvas_draw_str(canvas, 2, 55, m->line3);

    char foot[48];
    snprintf(foot, sizeof(foot), "#%u [Up]Save [Bk]Menu", (unsigned)m->burst_count);
    canvas_draw_str(canvas, 2, 63, foot);
}

static bool z2z_sub_live_input_cb(InputEvent* event, void* context) {
    Zero2ZeroApp* app = context;
    if(event->type != InputTypeShort) return false;

    if(event->key == InputKeyLeft) {
        app->sub_freq_idx = (uint8_t)((app->sub_freq_idx + Z2Z_SUB_FREQ_CNT - 1U) % Z2Z_SUB_FREQ_CNT);
        z2z_sub_start_rx(app);
        return true;
    }
    if(event->key == InputKeyRight) {
        app->sub_freq_idx = (uint8_t)((app->sub_freq_idx + 1U) % Z2Z_SUB_FREQ_CNT);
        z2z_sub_start_rx(app);
        return true;
    }
    if(event->key == InputKeyOk) {
        app->sub_rx_paused = !app->sub_rx_paused;
        with_view_model(
            app->sub_live_view,
            Z2ZSubLiveModel * m,
            { m->paused = app->sub_rx_paused; },
            true);
        return true;
    }
    if(event->key == InputKeyUp) {
        bool saved = z2z_save_last_sub_burst(app);
        with_view_model(
            app->sub_live_view,
            Z2ZSubLiveModel * m,
            {
                snprintf(
                    m->line3,
                    sizeof(m->line3),
                    "%s",
                    saved ? "Saved z2z_live.sub!" : "No burst to save yet");
            },
            true);
        if(saved) z2z_play_alert(app, true);
        return true;
    }
    return false;
}

static void z2z_pump_sub_live(Zero2ZeroApp* app) {
    if(!app->sub_rx_active) return;

    int16_t rssi = (int16_t)furi_hal_subghz_get_rssi();
    bool dispatched_burst = false;

    uint16_t tail = __atomic_load_n(&app->sub_rx_tail, __ATOMIC_RELAXED);
    uint16_t head = __atomic_load_n(&app->sub_rx_head, __ATOMIC_ACQUIRE);

    while(tail != head) {
        int32_t sample = app->sub_rx_ring[tail];
        tail = (uint16_t)((tail + 1U) & Z2Z_SUB_RING_MASK);

        int32_t abs_s = sample < 0 ? -sample : sample;
        bool is_gap = (sample < 0) && (abs_s >= 4000);

        if(!is_gap && abs_s >= 90 && abs_s <= 8000) {
            if(app->sub_burst_len == 0) {
                app->sub_peak_rssi = rssi;
            } else if(rssi > app->sub_peak_rssi) {
                app->sub_peak_rssi = rssi;
            }
            if(app->sub_burst_len < Z2Z_SUB_BURST_MAX) {
                app->sub_burst[app->sub_burst_len++] = sample;
            }
        }

        if(is_gap || app->sub_burst_len >= Z2Z_SUB_BURST_MAX) {
            if(app->sub_burst_len >= 20 && app->sub_peak_rssi >= -84) {
                memcpy(
                    app->sub_last_saved,
                    app->sub_burst,
                    app->sub_burst_len * sizeof(int32_t));
                app->sub_last_saved_len = app->sub_burst_len;
                app->sub_burst_total++;

                uint32_t now = furi_get_tick();
                if(!dispatched_burst && z2z_rpc_is_active(app) && app->pending_tx_due_tick == 0 &&
                   (now - app->sub_last_tx_tick) >= furi_ms_to_ticks(850)) {
                    app->seq++;
                    uint32_t f_hz = app->sub_actual_freq ? app->sub_actual_freq : 433920000UL;
                    int pos = snprintf(
                        app->pending_tx_buf,
                        sizeof(app->pending_tx_buf),
                        "{\"id\":%lu,\"seq\":%lu,\"op\":\"sub_live\",\"freq\":%lu,\"rssi\":%d,\"pulses\":\"",
                        (unsigned long)app->seq,
                        (unsigned long)app->seq,
                        (unsigned long)f_hz,
                        (int)app->sub_peak_rssi);
                    if(pos > 0 && (size_t)pos < sizeof(app->pending_tx_buf) - 16) {
                        for(uint16_t i = 0; i < app->sub_burst_len; i++) {
                            int n = snprintf(
                                app->pending_tx_buf + pos,
                                sizeof(app->pending_tx_buf) - (size_t)pos - 4,
                                "%s%ld",
                                (i == 0) ? "" : " ",
                                (long)app->sub_burst[i]);
                            if(n <= 0 ||
                               (size_t)(pos + n) >= sizeof(app->pending_tx_buf) - 6) {
                                break;
                            }
                            pos += n;
                        }
                        app->pending_tx_buf[pos++] = '"';
                        app->pending_tx_buf[pos++] = '}';
                        app->pending_tx_buf[pos] = '\0';
                        app->pending_tx_len = (size_t)pos;
                        if(z2z_rpc_send_exchange(
                               app, (const uint8_t*)app->pending_tx_buf, app->pending_tx_len)) {
                            app->sub_last_tx_tick = now;
                            dispatched_burst = true;
                        }
                    }
                }
            }
            app->sub_burst_len = 0;
            app->sub_peak_rssi = -110;
        }
    }

    __atomic_store_n(&app->sub_rx_tail, tail, __ATOMIC_RELEASE);

    bool duplex = z2z_rpc_is_active(app);
    with_view_model(
        app->sub_live_view,
        Z2ZSubLiveModel * m,
        {
            m->rssi_dbm = rssi;
            m->burst_count = app->sub_burst_total;
            m->duplex = duplex;
        },
        true);
}

/* --- High-Power IR Blaster & Receiver (`InfraredWorker` + `infrared_send*`) --- */

static void z2z_ir_pause_rx(Zero2ZeroApp* app) {
    if(__atomic_load_n(&app->ir_rx_running, __ATOMIC_ACQUIRE) && app->ir_worker) {
        __atomic_store_n(&app->ir_rx_running, false, __ATOMIC_SEQ_CST);
        infrared_worker_rx_stop(app->ir_worker);
        infrared_worker_rx_set_received_signal_callback(app->ir_worker, NULL, NULL);
    }
}

static void z2z_ir_stop_rx(Zero2ZeroApp* app) {
    z2z_ir_pause_rx(app);
    if(app->ir_worker) {
        infrared_worker_free(app->ir_worker);
        app->ir_worker = NULL;
    }
}

static void z2z_ir_rx_signal_cb(void* context, InfraredWorkerSignal* received_signal) {
    Zero2ZeroApp* app = context;
    if(!app || !received_signal || !__atomic_load_n(&app->ir_rx_running, __ATOMIC_ACQUIRE) ||
       __atomic_load_n(&app->app_exiting, __ATOMIC_ACQUIRE)) {
        return;
    }

    furi_mutex_acquire(app->rx_mutex, FuriWaitForever);
    if(infrared_worker_signal_is_decoded(received_signal)) {
        const InfraredMessage* msg = infrared_worker_get_decoded_signal(received_signal);
        if(msg) {
            if(msg->repeat && app->ir_has_last && app->ir_last_decoded &&
               app->ir_last_msg.protocol == msg->protocol &&
               app->ir_last_msg.address == msg->address &&
               app->ir_last_msg.command == msg->command) {
                furi_mutex_release(app->rx_mutex);
                return;
            }
            app->ir_last_msg = *msg;
            app->ir_last_msg.repeat = false;
            app->ir_last_decoded = true;
            app->ir_has_last = true;
            app->ir_rx_count++;
            furi_mutex_release(app->rx_mutex);
            if(!__atomic_load_n(&app->app_exiting, __ATOMIC_ACQUIRE)) {
                view_dispatcher_send_custom_event(app->view_dispatcher, Z2ZEventIrRx);
            }
            return;
        }
    } else {
        const uint32_t* timings = NULL;
        size_t timings_cnt = 0;
        infrared_worker_get_raw_signal(received_signal, &timings, &timings_cnt);
        if(timings && timings_cnt >= 6U) {
            if(timings_cnt > Z2Z_IR_RAW_MAX) timings_cnt = Z2Z_IR_RAW_MAX;
            if((timings_cnt & 1U) == 0U && timings_cnt > 1U) timings_cnt--;
            memcpy(app->ir_last_raw, timings, timings_cnt * sizeof(uint32_t));
            app->ir_last_raw_len = (uint16_t)timings_cnt;
            app->ir_last_freq = INFRARED_COMMON_CARRIER_FREQUENCY;
            app->ir_last_duty_pct = 33U;
            app->ir_last_decoded = false;
            app->ir_has_last = true;
            app->ir_rx_count++;
            furi_mutex_release(app->rx_mutex);
            if(!__atomic_load_n(&app->app_exiting, __ATOMIC_ACQUIRE)) {
                view_dispatcher_send_custom_event(app->view_dispatcher, Z2ZEventIrRx);
            }
            return;
        }
    }
    furi_mutex_release(app->rx_mutex);
}

static void z2z_ir_start_rx(Zero2ZeroApp* app) {
    if(__atomic_load_n(&app->app_exiting, __ATOMIC_ACQUIRE)) return;
    z2z_sub_stop_rx(app);
    z2z_ir_pause_rx(app);
    /*
     * Allocate `InfraredWorker` (~19.5 KB: 8,200B stream buffer + 4,176B worker +
     * 2,048B thread stack + codecs) lazily ONLY when explicit RX Learn is active, and free it
     * immediately on view exit so TX-only `ir_tx` + BLE RPC transitions never starve heap!
     */
    if(!app->ir_worker) {
        app->ir_worker = infrared_worker_alloc();
    }
    if(!app->ir_worker) return;
    infrared_worker_rx_enable_signal_decoding(app->ir_worker, true);
    infrared_worker_rx_enable_blink_on_receiving(app->ir_worker, true);
    infrared_worker_rx_set_received_signal_callback(app->ir_worker, z2z_ir_rx_signal_cb, app);
    infrared_worker_rx_start(app->ir_worker);
    __atomic_store_n(&app->ir_rx_running, true, __ATOMIC_RELEASE);
}

static void z2z_ir_live_enter_cb(void* context) {
    Zero2ZeroApp* app = context;
    app->ir_view_open = true;
    if(app->ir_want_rx) {
        z2z_ir_start_rx(app);
    }
    bool listening = __atomic_load_n(&app->ir_rx_running, __ATOMIC_ACQUIRE);
    bool duplex = z2z_rpc_is_active(app);
    with_view_model(
        app->ir_live_view,
        Z2ZIrLiveModel * m,
        {
            m->rx_listening = listening;
            m->has_signal = app->ir_has_last;
            m->rx_count = app->ir_rx_count;
            m->tx_count = app->ir_tx_count;
            m->duplex = duplex;
            if(m->rx_count == 0 && m->tx_count == 0) {
                if(listening) {
                    snprintf(m->line1, sizeof(m->line1), "Point remote at Flipper");
                    snprintf(
                        m->line2,
                        sizeof(m->line2),
                        "%s",
                        duplex ? "or fire from Kali Deck" : "IR 3-LED TX + RX Ready");
                    snprintf(m->line3, sizeof(m->line3), "[OK]Replay [Up]Save .ir");
                } else {
                    snprintf(m->line1, sizeof(m->line1), "IR 3-LED TX Ready");
                    snprintf(m->line2, sizeof(m->line2), "RX idle (saves RAM)");
                    snprintf(m->line3, sizeof(m->line3), "[OK]Replay [Dn]RX Learn");
                }
            } else if(listening) {
                snprintf(m->line3, sizeof(m->line3), "[OK]Replay [Up]Save .ir");
            }
        },
        true);
}

static void z2z_ir_live_exit_cb(void* context) {
    Zero2ZeroApp* app = context;
    app->ir_view_open = false;
    app->ir_want_rx = false;
    z2z_ir_stop_rx(app);
}

static bool z2z_replay_last_ir(Zero2ZeroApp* app) {
    furi_mutex_acquire(app->rx_mutex, FuriWaitForever);
    bool has = app->ir_has_last;
    bool decoded = app->ir_last_decoded;
    InfraredMessage msg = app->ir_last_msg;
    uint16_t raw_len = app->ir_last_raw_len;
    uint32_t freq = app->ir_last_freq ? app->ir_last_freq : INFRARED_COMMON_CARRIER_FREQUENCY;
    uint32_t duty_pct = app->ir_last_duty_pct ? app->ir_last_duty_pct : 33U;
    if(has && !decoded && raw_len > 0 && raw_len <= Z2Z_IR_RAW_MAX) {
        memcpy(app->ir_tx_raw, app->ir_last_raw, raw_len * sizeof(uint32_t));
    }
    furi_mutex_release(app->rx_mutex);

    if(!has) return false;

    bool was_rx = __atomic_load_n(&app->ir_rx_running, __ATOMIC_ACQUIRE);
    if(was_rx) z2z_ir_pause_rx(app);

    if(decoded) {
        infrared_send(&msg, 2);
    } else if(raw_len > 0) {
        infrared_send_raw_ext(
            app->ir_tx_raw, (uint32_t)raw_len, true, freq, (float)duty_pct * 0.01f);
    }
    app->ir_tx_count++;

    if(was_rx && app->ir_view_open) {
        z2z_ir_start_rx(app);
    }
    return true;
}

static bool z2z_save_last_ir(Zero2ZeroApp* app) {
    furi_mutex_acquire(app->rx_mutex, FuriWaitForever);
    bool has = app->ir_has_last;
    bool decoded = app->ir_last_decoded;
    InfraredMessage msg = app->ir_last_msg;
    uint16_t raw_len = app->ir_last_raw_len;
    uint32_t freq = app->ir_last_freq ? app->ir_last_freq : INFRARED_COMMON_CARRIER_FREQUENCY;
    uint32_t duty_pct = app->ir_last_duty_pct ? app->ir_last_duty_pct : 33U;
    uint32_t btn_idx = app->ir_rx_count + app->ir_tx_count;
    if(has && !decoded && raw_len > 0 && raw_len <= Z2Z_IR_RAW_MAX) {
        memcpy(app->ir_tx_raw, app->ir_last_raw, raw_len * sizeof(uint32_t));
    }
    furi_mutex_release(app->rx_mutex);

    if(!has) return false;
    storage_simply_mkdir(app->storage, EXT_PATH("infrared"));
    bool exists = (storage_common_stat(app->storage, Z2Z_LEARNED_IR_PATH, NULL) == FSE_OK);
    File* f = storage_file_alloc(app->storage);
    bool ok = false;
    if(storage_file_open(
           f,
           Z2Z_LEARNED_IR_PATH,
           FSAM_WRITE,
           exists ? FSOM_OPEN_APPEND : FSOM_CREATE_ALWAYS)) {
        if(!exists) {
            const char* fh = "Filetype: IR signals file\nVersion: 1\n";
            storage_file_write(f, fh, strlen(fh));
        }
        char block[256];
        if(decoded) {
            const char* pname = infrared_get_protocol_name(msg.protocol);
            if(!pname) pname = "NEC";
            uint32_t a = msg.address;
            uint32_t c = msg.command;
            int blen = snprintf(
                block,
                sizeof(block),
                "#\nname: Btn_%u\ntype: parsed\nprotocol: %s\n"
                "address: %02X %02X %02X %02X\n"
                "command: %02X %02X %02X %02X\n",
                (unsigned)btn_idx,
                pname,
                (unsigned)(a & 0xFFU),
                (unsigned)((a >> 8) & 0xFFU),
                (unsigned)((a >> 16) & 0xFFU),
                (unsigned)((a >> 24) & 0xFFU),
                (unsigned)(c & 0xFFU),
                (unsigned)((c >> 8) & 0xFFU),
                (unsigned)((c >> 16) & 0xFFU),
                (unsigned)((c >> 24) & 0xFFU));
            if(blen > 0) storage_file_write(f, block, (size_t)blen);
            ok = true;
        } else if(raw_len > 0) {
            int blen = snprintf(
                block,
                sizeof(block),
                "#\nname: Raw_%u\ntype: raw\nfrequency: %lu\nduty_cycle: 0.%02u0000\ndata:",
                (unsigned)btn_idx,
                (unsigned long)freq,
                (unsigned)(duty_pct % 100U));
            if(blen > 0) storage_file_write(f, block, (size_t)blen);
            for(uint16_t i = 0; i < raw_len; i++) {
                char tok[16];
                int tlen = snprintf(tok, sizeof(tok), " %lu", (unsigned long)app->ir_tx_raw[i]);
                if(tlen > 0) storage_file_write(f, tok, (size_t)tlen);
            }
            storage_file_write(f, "\n", 1);
            ok = true;
        }
        storage_file_close(f);
    }
    storage_file_free(f);
    return ok;
}

static void z2z_ir_live_draw_cb(Canvas* canvas, void* model) {
    Z2ZIrLiveModel* m = model;
    canvas_clear(canvas);

    canvas_set_font(canvas, FontPrimary);
    char hdr[48];
    const char* badge = m->rx_listening ? (m->duplex ? "[BLE RX+TX]" : "[RX+TX]") :
                                          (m->duplex ? "[BLE TX]" : "[TX Ready]");
    snprintf(hdr, sizeof(hdr), "IR Deck %s", badge);
    canvas_draw_str(canvas, 2, 11, hdr);

    canvas_set_font(canvas, FontSecondary);
    canvas_draw_str(canvas, 2, 25, m->line1);
    canvas_draw_str(canvas, 2, 37, m->line2);
    canvas_draw_str(canvas, 2, 49, m->line3);

    char foot[48];
    snprintf(
        foot,
        sizeof(foot),
        "RX:%u TX:%u [OK]Send [Up]Sav",
        (unsigned)m->rx_count,
        (unsigned)m->tx_count);
    canvas_draw_str(canvas, 2, 62, foot);
}

static bool z2z_ir_live_input_cb(InputEvent* event, void* context) {
    Zero2ZeroApp* app = context;
    if(event->type != InputTypeShort) return false;

    if(event->key == InputKeyOk) {
        bool sent = z2z_replay_last_ir(app);
        bool listening = __atomic_load_n(&app->ir_rx_running, __ATOMIC_ACQUIRE);
        with_view_model(
            app->ir_live_view,
            Z2ZIrLiveModel * m,
            {
                m->tx_count = app->ir_tx_count;
                m->rx_listening = listening;
                snprintf(
                    m->line3,
                    sizeof(m->line3),
                    "%s",
                    sent ? "Blasted IR! [Up]Save" : "No IR learned yet");
            },
            true);
        if(sent) z2z_play_alert(app, true);
        return true;
    }
    if(event->key == InputKeyUp) {
        bool saved = z2z_save_last_ir(app);
        with_view_model(
            app->ir_live_view,
            Z2ZIrLiveModel * m,
            {
                snprintf(
                    m->line3,
                    sizeof(m->line3),
                    "%s",
                    saved ? "Saved z2z_learned.ir!" : "Nothing to save yet");
            },
            true);
        if(saved) z2z_play_alert(app, true);
        return true;
    }
    if(event->key == InputKeyDown) {
        if(__atomic_load_n(&app->ir_rx_running, __ATOMIC_ACQUIRE)) {
            app->ir_want_rx = false;
            z2z_ir_stop_rx(app);
        } else {
            app->ir_want_rx = true;
            z2z_ir_start_rx(app);
        }
        bool listening = __atomic_load_n(&app->ir_rx_running, __ATOMIC_ACQUIRE);
        with_view_model(
            app->ir_live_view,
            Z2ZIrLiveModel * m,
            {
                m->rx_listening = listening;
                snprintf(
                    m->line3,
                    sizeof(m->line3),
                    "%s",
                    listening ? "RX Learn ON ([Dn]Off)" : "RX Learn OFF ([Dn]On)");
            },
            true);
        return true;
    }
    return false;
}

static void z2z_on_ir_rx_event(Zero2ZeroApp* app) {
    if(!app->ir_view_open || __atomic_load_n(&app->app_exiting, __ATOMIC_ACQUIRE)) {
        return;
    }

    furi_mutex_acquire(app->rx_mutex, FuriWaitForever);
    bool has = app->ir_has_last;
    bool decoded = app->ir_last_decoded;
    InfraredMessage msg = app->ir_last_msg;
    uint16_t raw_len = app->ir_last_raw_len;
    uint32_t freq = app->ir_last_freq ? app->ir_last_freq : INFRARED_COMMON_CARRIER_FREQUENCY;
    uint32_t rx_cnt = app->ir_rx_count;
    if(has && !decoded && raw_len > 0 && raw_len <= Z2Z_IR_RAW_MAX) {
        memcpy(app->ir_tx_raw, app->ir_last_raw, raw_len * sizeof(uint32_t));
    }
    furi_mutex_release(app->rx_mutex);

    if(!has) return;

    const char* pname = decoded ? infrared_get_protocol_name(msg.protocol) : NULL;
    if(!pname) pname = "NEC";

    bool duplex = z2z_rpc_is_active(app);
    with_view_model(
        app->ir_live_view,
        Z2ZIrLiveModel * m,
        {
            m->rx_count = rx_cnt;
            m->has_signal = true;
            m->duplex = duplex;
            if(decoded) {
                snprintf(m->line1, sizeof(m->line1), "RX: %.22s", pname);
                snprintf(
                    m->line2,
                    sizeof(m->line2),
                    "A:0x%lX C:0x%lX",
                    (unsigned long)msg.address,
                    (unsigned long)msg.command);
            } else {
                snprintf(
                    m->line1,
                    sizeof(m->line1),
                    "RX: RAW %up @%ukHz",
                    (unsigned)raw_len,
                    (unsigned)(freq / 1000UL));
                uint32_t t0 = raw_len > 0 ? app->ir_tx_raw[0] : 0U;
                uint32_t t1 = raw_len > 1 ? app->ir_tx_raw[1] : 0U;
                if(t0 > 999999U) t0 = 999999U;
                if(t1 > 999999U) t1 = 999999U;
                snprintf(
                    m->line2,
                    sizeof(m->line2),
                    "T0: %u/%u us",
                    (unsigned)t0,
                    (unsigned)t1);
            }
            snprintf(m->line3, sizeof(m->line3), "[OK]Replay [Up]Save .ir");
        },
        true);

    z2z_play_alert(app, true);

    /* Forward learned IR frame over BLE RPC Duplex to Kali (`z2z ir-save`) */
    uint32_t now = furi_get_tick();
    if(duplex && app->pending_tx_due_tick == 0 &&
       (now - app->ir_last_tx_tick) >= furi_ms_to_ticks(350)) {
        app->seq++;
        int pos = 0;
        if(decoded) {
            pos = snprintf(
                app->pending_tx_buf,
                sizeof(app->pending_tx_buf),
                "{\"id\":%lu,\"seq\":%lu,\"op\":\"ir_rx\",\"type\":\"parsed\",\"proto\":\"%s\",\"addr\":%lu,\"cmd\":%lu}",
                (unsigned long)app->seq,
                (unsigned long)app->seq,
                pname,
                (unsigned long)msg.address,
                (unsigned long)msg.command);
        } else if(raw_len > 0) {
            pos = snprintf(
                app->pending_tx_buf,
                sizeof(app->pending_tx_buf),
                "{\"id\":%lu,\"seq\":%lu,\"op\":\"ir_rx\",\"type\":\"raw\",\"freq\":%lu,\"duty\":33,\"pulses\":\"",
                (unsigned long)app->seq,
                (unsigned long)app->seq,
                (unsigned long)freq);
            if(pos > 0 && (size_t)pos < sizeof(app->pending_tx_buf) - 16) {
                for(uint16_t i = 0; i < raw_len; i++) {
                    int n = snprintf(
                        app->pending_tx_buf + pos,
                        sizeof(app->pending_tx_buf) - (size_t)pos - 4,
                        "%s%lu",
                        (i == 0) ? "" : " ",
                        (unsigned long)app->ir_tx_raw[i]);
                    if(n <= 0 || (size_t)(pos + n) >= sizeof(app->pending_tx_buf) - 6) {
                        break;
                    }
                    pos += n;
                }
                app->pending_tx_buf[pos++] = '"';
                app->pending_tx_buf[pos++] = '}';
                app->pending_tx_buf[pos] = '\0';
            }
        }
        if(pos > 0 && (size_t)pos < sizeof(app->pending_tx_buf)) {
            app->pending_tx_len = (size_t)pos;
            if(z2z_rpc_send_exchange(
                   app, (const uint8_t*)app->pending_tx_buf, app->pending_tx_len)) {
                app->ir_last_tx_tick = now;
            }
        }
    }
}

static void z2z_execute_ir_tx_json(Zero2ZeroApp* app, const char* json) {
    if(!app || __atomic_load_n(&app->app_exiting, __ATOMIC_ACQUIRE)) return;
    z2z_sub_stop_rx(app);
    bool was_rx = __atomic_load_n(&app->ir_rx_running, __ATOMIC_ACQUIRE);
    if(was_rx) z2z_ir_pause_rx(app);

    FuriString* name_str = furi_string_alloc();
    FuriString* proto_str = furi_string_alloc();
    FuriString* raw_str = furi_string_alloc();

    if(!z2z_extract_json_field(json, "name", name_str) || furi_string_empty(name_str)) {
        furi_string_set_str(name_str, "Remote");
    }
    bool has_proto = z2z_extract_json_field(json, "proto", proto_str) &&
                     !furi_string_empty(proto_str);
    bool has_raw = z2z_extract_json_field(json, "raw", raw_str) && !furi_string_empty(raw_str);

    bool quiet = true;
    z2z_extract_json_bool(json, "quiet", &quiet);

    bool ok_tx = false;
    char detail_line[28];
    snprintf(detail_line, sizeof(detail_line), "Invalid IR frame");

    if(has_proto) {
        InfraredProtocol proto = infrared_get_protocol_by_name(furi_string_get_cstr(proto_str));
        if(infrared_is_protocol_valid(proto)) {
            uint32_t addr = 0;
            uint32_t cmd = 0;
            uint32_t repeat = 2;
            z2z_extract_json_u32(json, "addr", &addr);
            z2z_extract_json_u32(json, "cmd", &cmd);
            z2z_extract_json_u32(json, "repeat", &repeat);
            if(repeat < 1U) repeat = 1U;
            if(repeat > 8U) repeat = 8U;

            InfraredMessage msg = {
                .protocol = proto,
                .address = addr,
                .command = cmd,
                .repeat = false,
            };
            infrared_send(&msg, (int)repeat);
            ok_tx = true;
            app->ir_tx_count++;

            furi_mutex_acquire(app->rx_mutex, FuriWaitForever);
            app->ir_last_msg = msg;
            app->ir_last_decoded = true;
            app->ir_has_last = true;
            furi_mutex_release(app->rx_mutex);

            const char* pname = infrared_get_protocol_name(proto);
            snprintf(
                detail_line,
                sizeof(detail_line),
                "%.10s %lX/%lX",
                pname ? pname : "IR",
                (unsigned long)addr,
                (unsigned long)cmd);
        }
    } else if(has_raw) {
        uint32_t freq = INFRARED_COMMON_CARRIER_FREQUENCY;
        uint32_t duty = 33U;
        z2z_extract_json_u32(json, "freq", &freq);
        z2z_extract_json_u32(json, "duty", &duty);
        if(freq < 10000U || freq > 60000U) freq = INFRARED_COMMON_CARRIER_FREQUENCY;
        if(duty < 5U || duty > 90U) duty = 33U;

        uint16_t cnt = 0;
        const char* p = furi_string_get_cstr(raw_str);
        while(*p && cnt < Z2Z_IR_RAW_MAX) {
            while(*p == ' ' || *p == ',' || *p == '\t') p++;
            if(!*p) break;
            char* end = NULL;
            unsigned long v = strtoul(p, &end, 10);
            if(end == p) {
                p++;
                continue;
            }
            if(v > 0UL) {
                app->ir_tx_raw[cnt++] = (uint32_t)v;
            }
            p = end;
        }
        if(cnt > 0U) {
            if((cnt & 1U) == 0U && cnt > 1U) cnt--;
            infrared_send_raw_ext(
                app->ir_tx_raw, (uint32_t)cnt, true, freq, (float)duty * 0.01f);
            ok_tx = true;
            app->ir_tx_count++;

            furi_mutex_acquire(app->rx_mutex, FuriWaitForever);
            memcpy(app->ir_last_raw, app->ir_tx_raw, cnt * sizeof(uint32_t));
            app->ir_last_raw_len = cnt;
            app->ir_last_freq = freq;
            app->ir_last_duty_pct = duty;
            app->ir_last_decoded = false;
            app->ir_has_last = true;
            furi_mutex_release(app->rx_mutex);

            snprintf(
                detail_line,
                sizeof(detail_line),
                "RAW %up @%ukHz",
                (unsigned)cnt,
                (unsigned)(freq / 1000UL));
        }
    }

    /*
     * Remote TX-only `op="ir_tx"` uses synchronous `infrared_send*` HAL calls
     * and does NOT need the ~19.5 KB `InfraredWorker` + `TIM2` async RX thread! Keep RX off
     * unless RX Learn was already open (`was_rx`), preventing both BLE RPC heap pressure and
     * reflected-IR `Z2ZEventIrRx` -> `DataExchange` churn when the user exits `.fap`.
     */
    if(app->ir_view_open && was_rx) {
        z2z_ir_start_rx(app);
    }
    bool listening = __atomic_load_n(&app->ir_rx_running, __ATOMIC_ACQUIRE);
    bool duplex = z2z_rpc_is_active(app);
    with_view_model(
        app->ir_live_view,
        Z2ZIrLiveModel * m,
        {
            m->tx_count = app->ir_tx_count;
            m->rx_listening = listening;
            m->has_signal = app->ir_has_last;
            m->duplex = duplex;
            snprintf(
                m->line1,
                sizeof(m->line1),
                "%s: %.21s",
                ok_tx ? "TX" : "ERR",
                furi_string_get_cstr(name_str));
            snprintf(m->line2, sizeof(m->line2), "%.27s", detail_line);
            snprintf(
                m->line3,
                sizeof(m->line3),
                "%s",
                listening ? "[OK]Replay [Bk]Menu" : "[OK]Replay [Dn]Learn");
        },
        true);

    if(!app->ir_view_open) {
        app->ir_want_rx = false;
        view_dispatcher_switch_to_view(app->view_dispatcher, Z2ZViewIrLive);
    }

    if(ok_tx) {
        z2z_play_alert(app, quiet);
    }

    furi_string_free(raw_str);
    furi_string_free(proto_str);
    furi_string_free(name_str);
}

static void z2z_send_command(Zero2ZeroApp* app, const char* op, const char* job_id);

static void z2z_job_item_cb(void* context, uint32_t index) {
    Zero2ZeroApp* app = context;
    if(index < app->job_count) {
        z2z_send_command(app, "run", app->job_ids[index]);
    }
}

/* Parse `"items":"id1|Title 1\nid2|Title 2"` into `jobs_submenu` */
static bool z2z_populate_jobs_submenu(Zero2ZeroApp* app, const char* json) {
    FuriString* items = furi_string_alloc();
    bool ok = z2z_extract_json_field(json, "items", items);
    if(!ok || furi_string_size(items) == 0) {
        furi_string_free(items);
        return false;
    }

    submenu_reset(app->jobs_submenu);
    submenu_set_header(app->jobs_submenu, "Run Kali Offload Job");
    app->job_count = 0;

    const char* p = furi_string_get_cstr(items);
    while(*p && app->job_count < Z2Z_MAX_JOBS) {
        const char* nl = strchr(p, '\n');
        size_t line_len = nl ? (size_t)(nl - p) : strlen(p);
        if(line_len > 0) {
            char line[80];
            if(line_len >= sizeof(line)) line_len = sizeof(line) - 1;
            memcpy(line, p, line_len);
            line[line_len] = '\0';

            char* bar = strchr(line, '|');
            if(bar) {
                *bar = '\0';
                const char* jid = line;
                const char* jtitle = bar + 1;
                if(*jid) {
                    uint8_t idx = app->job_count++;
                    snprintf(app->job_ids[idx], sizeof(app->job_ids[idx]), "%.27s", jid);
                    snprintf(
                        app->job_labels[idx],
                        sizeof(app->job_labels[idx]),
                        "%.35s",
                        *jtitle ? jtitle : jid);
                    submenu_add_item(
                        app->jobs_submenu, app->job_labels[idx], idx, z2z_job_item_cb, app);
                }
            }
        }
        if(!nl) break;
        p = nl + 1;
    }

    furi_string_free(items);
    return app->job_count > 0;
}

/* Parse `"YYYY-MM-DD HH:MM:SS[ W]"` without pulling in newlib `sscanf` stack frames */
static bool z2z_parse_rtc_cstr(const char* s, DateTime* dt) {
    if(!s || !dt) return false;
    char* end = NULL;
    unsigned long yr = strtoul(s, &end, 10);
    if(end == s || *end != '-') return false;
    s = end + 1;
    unsigned long mo = strtoul(s, &end, 10);
    if(end == s || *end != '-') return false;
    s = end + 1;
    unsigned long dy = strtoul(s, &end, 10);
    if(end == s || *end != ' ') return false;
    s = end + 1;
    unsigned long hr = strtoul(s, &end, 10);
    if(end == s || *end != ':') return false;
    s = end + 1;
    unsigned long mn = strtoul(s, &end, 10);
    if(end == s || *end != ':') return false;
    s = end + 1;
    unsigned long sc = strtoul(s, &end, 10);
    if(end == s) return false;
    unsigned long wd = 1;
    if(*end == ' ') {
        unsigned long parsed_wd = strtoul(end + 1, &end, 10);
        if(parsed_wd >= 1 && parsed_wd <= 7) wd = parsed_wd;
    }
    dt->year = (uint16_t)yr;
    dt->month = (uint8_t)mo;
    dt->day = (uint8_t)dy;
    dt->hour = (uint8_t)hr;
    dt->minute = (uint8_t)mn;
    dt->second = (uint8_t)sc;
    dt->weekday = (uint8_t)wd;
    return true;
}

/* Apply `"rtc":"YYYY-MM-DD HH:MM:SS W"` directly to Flipper's hardware RTC */
static void z2z_try_apply_rtc(const char* json) {
    FuriString* rtc_str = furi_string_alloc();
    if(z2z_extract_json_field(json, "rtc", rtc_str)) {
        DateTime dt = {0};
        if(z2z_parse_rtc_cstr(furi_string_get_cstr(rtc_str), &dt) &&
           datetime_validate_datetime(&dt)) {
            furi_hal_rtc_set_datetime(&dt);
        }
    }
    furi_string_free(rtc_str);
}

static void z2z_handle_json_response(Zero2ZeroApp* app, const char* json) {
    z2z_try_apply_rtc(json);

    FuriString* op_str = furi_string_alloc();
    bool has_op = z2z_extract_json_field(json, "op", op_str);

    if(has_op && furi_string_cmp_str(op_str, "sub_live") == 0) {
        FuriString* l1 = furi_string_alloc();
        FuriString* l2 = furi_string_alloc();
        FuriString* l3 = furi_string_alloc();
        bool g1 = z2z_extract_json_field(json, "line1", l1);
        bool g2 = z2z_extract_json_field(json, "line2", l2);
        bool g3 = z2z_extract_json_field(json, "line3", l3);
        bool matched = false;
        bool quiet = true;
        z2z_extract_json_bool(json, "matched", &matched);
        z2z_extract_json_bool(json, "quiet", &quiet);

        with_view_model(
            app->sub_live_view,
            Z2ZSubLiveModel * m,
            {
                if(g1) snprintf(m->line1, sizeof(m->line1), "%.27s", furi_string_get_cstr(l1));
                if(g2) snprintf(m->line2, sizeof(m->line2), "%.27s", furi_string_get_cstr(l2));
                if(g3) snprintf(m->line3, sizeof(m->line3), "%.27s", furi_string_get_cstr(l3));
            },
            true);

        if(matched) {
            z2z_play_alert(app, quiet);
        }
        furi_string_free(l1);
        furi_string_free(l2);
        furi_string_free(l3);
        furi_string_free(op_str);
        return;
    }

    if(has_op && furi_string_cmp_str(op_str, "ir_rx_start") == 0) {
        furi_string_free(op_str);
        app->ir_want_rx = true;
        if(!app->ir_view_open) {
            view_dispatcher_switch_to_view(app->view_dispatcher, Z2ZViewIrLive);
        } else if(!__atomic_load_n(&app->ir_rx_running, __ATOMIC_ACQUIRE)) {
            z2z_ir_start_rx(app);
            bool listening = __atomic_load_n(&app->ir_rx_running, __ATOMIC_ACQUIRE);
            with_view_model(
                app->ir_live_view,
                Z2ZIrLiveModel * m,
                {
                    m->rx_listening = listening;
                    snprintf(m->line3, sizeof(m->line3), "[OK]Replay [Up]Save .ir");
                },
                true);
        }
        return;
    }

    if(has_op && furi_string_cmp_str(op_str, "ir_tx") == 0) {
        furi_string_free(op_str);
        z2z_execute_ir_tx_json(app, json);
        return;
    }

    if(has_op && furi_string_cmp_str(op_str, "ir_rx_ack") == 0) {
        FuriString* l3 = furi_string_alloc();
        if(app->ir_view_open && z2z_extract_json_field(json, "line3", l3) &&
           !furi_string_empty(l3)) {
            with_view_model(
                app->ir_live_view,
                Z2ZIrLiveModel * m,
                { snprintf(m->line3, sizeof(m->line3), "%.27s", furi_string_get_cstr(l3)); },
                true);
        }
        furi_string_free(l3);
        furi_string_free(op_str);
        return;
    }

    if(!z2z_extract_json_field(json, "text", app->text_buf)) {
        furi_string_set_str(app->text_buf, json);
    }
    text_box_set_text(app->text_box, furi_string_get_cstr(app->text_buf));
    bool quiet = false;
    z2z_extract_json_bool(json, "quiet", &quiet);
    z2z_play_alert(app, quiet);

    bool is_jobs = has_op && (furi_string_cmp_str(op_str, "jobs") == 0);
    furi_string_free(op_str);

    if(is_jobs && z2z_populate_jobs_submenu(app, json)) {
        view_dispatcher_switch_to_view(app->view_dispatcher, Z2ZViewJobsMenu);
    } else {
        view_dispatcher_switch_to_view(app->view_dispatcher, Z2ZViewOutput);
    }
}

static void z2z_rpc_callback(const RpcAppSystemEvent* event, void* context) {
    furi_assert(context);
    Zero2ZeroApp* app = context;
    RpcAppSystem* rpc = __atomic_load_n(&app->rpc_ctx, __ATOMIC_ACQUIRE);
    if(!rpc) return;

    if(event->type == RpcAppEventTypeSessionClose) {
        /*
         * `rpc_system_app_free` runs on FreeRTOS `Tmr Svc` (`PendingSrv`) and
         * spins `while(rpc_app->callback != NULL) furi_delay_tick(1);`. Coordinate ownership
         * of the `callback != NULL` lease lock-free via `app->rpc_state` (never blocking
         * `Tmr Svc` on `rpc_mutex`), and exit RPC-launched `.fap` instances on `SessionClose`
         * so no orphan `.fap` stays locked across BLE reconnects.
         */
        uint8_t st = __atomic_load_n(&app->rpc_state, __ATOMIC_ACQUIRE);
        for(;;) {
            if(st == Z2ZRpcExiting || st == Z2ZRpcClosed || st == Z2ZRpcNone ||
               st == Z2ZRpcClosingAfterSend) {
                return;
            }
            if(st == Z2ZRpcSending) {
                if(__atomic_compare_exchange_n(
                       &app->rpc_state,
                       &st,
                       Z2ZRpcClosingAfterSend,
                       false,
                       __ATOMIC_ACQ_REL,
                       __ATOMIC_ACQUIRE)) {
                    return;
                }
                continue;
            }
            if(__atomic_compare_exchange_n(
                   &app->rpc_state,
                   &st,
                   Z2ZRpcClosed,
                   false,
                   __ATOMIC_ACQ_REL,
                   __ATOMIC_ACQUIRE)) {
                __atomic_store_n(&app->rpc_ctx, NULL, __ATOMIC_RELEASE);
                rpc_system_app_set_callback(rpc, NULL, NULL);
                z2z_post_event_if_alive(
                    app, app->started_by_rpc ? Z2ZEventRpcExit : Z2ZEventRpcClosed);
                return;
            }
        }
    }

    if(__atomic_load_n(&app->rpc_state, __ATOMIC_ACQUIRE) >= Z2ZRpcClosingAfterSend) {
        rpc_system_app_confirm(rpc, event->type == RpcAppEventTypeAppExit);
        return;
    }
    bool exiting = __atomic_load_n(&app->app_exiting, __ATOMIC_ACQUIRE);

    if(event->type == RpcAppEventTypeAppExit) {
        rpc_system_app_confirm(rpc, true);
        if(!exiting) {
            z2z_post_event_if_alive(app, Z2ZEventRpcExit);
        }
        return;
    }

    if(event->type == RpcAppEventTypeDataExchange) {
        bool queued = false;
        if(!exiting && event->data.type == RpcAppSystemEventDataTypeBytes &&
           event->data.bytes.ptr && event->data.bytes.size > 0) {
            furi_mutex_acquire(app->rx_mutex, FuriWaitForever);
            size_t copy_len = event->data.bytes.size;
            if(copy_len >= sizeof(app->pending_rx)) copy_len = sizeof(app->pending_rx) - 1;
            memcpy(app->pending_rx, event->data.bytes.ptr, copy_len);
            app->pending_rx[copy_len] = '\0';
            furi_mutex_release(app->rx_mutex);
            queued = true;
        }
        /* Reject with `ERROR_APP_CMD_ERROR` (`false`) if exiting so Kali relaunches `.fap`! */
        rpc_system_app_confirm(rpc, queued);
        if(queued) {
            z2z_post_event_if_alive(app, Z2ZEventRxFrame);
        }
        return;
    }

    rpc_system_app_confirm(rpc, false);
}

static void z2z_send_command(Zero2ZeroApp* app, const char* op, const char* job_id) {
    app->waiting_mailbox = false;
    app->pending_tx_due_tick = 0;
    app->seq++;
    char payload[192];
    if(job_id && *job_id) {
        snprintf(
            payload,
            sizeof(payload),
            "{\"id\":%lu,\"seq\":%lu,\"op\":\"%s\",\"job\":\"%s\"}",
            (unsigned long)app->seq,
            (unsigned long)app->seq,
            op,
            job_id);
    } else {
        snprintf(
            payload,
            sizeof(payload),
            "{\"id\":%lu,\"seq\":%lu,\"op\":\"%s\"}",
            (unsigned long)app->seq,
            (unsigned long)app->seq,
            op);
    }

    if(z2z_rpc_is_active(app)) {
        size_t plen = strlen(payload);
        if(plen >= sizeof(app->pending_tx_buf)) plen = sizeof(app->pending_tx_buf) - 1;
        memcpy(app->pending_tx_buf, payload, plen);
        app->pending_tx_buf[plen] = '\0';
        app->pending_tx_len = plen;

        furi_string_printf(
            app->text_buf,
            "[BLE Duplex #%lu]\nSent op=%s...\nWaiting for Kali...",
            (unsigned long)app->seq,
            op);
        text_box_set_text(app->text_box, furi_string_get_cstr(app->text_buf));
        view_dispatcher_switch_to_view(app->view_dispatcher, Z2ZViewOutput);

        /*
         * Defer `rpc_system_app_exchange_data()` by 180ms out of `InputTypeShort` submenu
         * dispatch so `RpcSrv`'s `SendInputEventRequest(RELEASE)` response and `GuiRpcWorker`'s
         * `ScreenFrame` indications flush cleanly on STM32WB55 CPU2 without colliding.
         */
        uint32_t due = furi_get_tick() + furi_ms_to_ticks(180);
        app->pending_tx_due_tick = (due == 0U) ? 1U : due;
        return;
    }

    /* Pocket Mailbox fallback (`/ext/apps_data/zero2zero/request.json`) */
    storage_simply_mkdir(app->storage, Z2Z_DATA_DIR);
    storage_simply_remove(app->storage, Z2Z_RESP_PATH);
    File* f = storage_file_alloc(app->storage);
    bool wrote = false;
    if(storage_file_open(f, Z2Z_REQ_PATH, FSAM_WRITE, FSOM_CREATE_ALWAYS)) {
        size_t len = strlen(payload);
        wrote = (storage_file_write(f, payload, len) == len);
        storage_file_close(f);
    }
    storage_file_free(f);

    if(wrote) {
        app->waiting_mailbox = true;
        app->mailbox_poll_ticks = 0;
        app->mailbox_subtick = 0;
        furi_string_printf(
            app->text_buf,
            "[Pocket Mailbox #%lu]\nQueued op=%s\nWaiting for Kali BLE watch...",
            (unsigned long)app->seq,
            op);
    } else {
        furi_string_set_str(app->text_buf, "SD write failed:\n" Z2Z_REQ_PATH);
    }
    text_box_set_text(app->text_box, furi_string_get_cstr(app->text_buf));
    view_dispatcher_switch_to_view(app->view_dispatcher, Z2ZViewOutput);
}

static void z2z_submenu_cb(void* context, uint32_t index) {
    Zero2ZeroApp* app = context;
    switch(index) {
    case Z2ZMenuPing:
        z2z_send_command(app, "ping", NULL);
        break;
    case Z2ZMenuNet:
        z2z_send_command(app, "net", NULL);
        break;
    case Z2ZMenuSubLive:
        view_dispatcher_switch_to_view(app->view_dispatcher, Z2ZViewSubLive);
        break;
    case Z2ZMenuIrLive:
        app->ir_want_rx = true;
        view_dispatcher_switch_to_view(app->view_dispatcher, Z2ZViewIrLive);
        break;
    case Z2ZMenuCapLatest:
        z2z_send_command(app, "cap_latest", NULL);
        break;
    case Z2ZMenuSubLatest:
        z2z_send_command(app, "sub_latest", NULL);
        break;
    case Z2ZMenuVault:
        z2z_send_command(app, "vault", NULL);
        break;
    case Z2ZMenuJobs:
        z2z_send_command(app, "jobs", NULL);
        break;
    case Z2ZMenuClockSync:
        z2z_send_command(app, "clock_sync", NULL);
        break;
    case Z2ZMenuAlertToggle:
        app->alert_mode = (Z2ZAlertMode)((app->alert_mode + 1) % Z2ZAlertCount);
        snprintf(
            app->alert_label,
            sizeof(app->alert_label),
            "Alert: [%s]",
            z2z_alert_title(app->alert_mode));
        submenu_change_item_label(app->submenu, Z2ZMenuAlertToggle, app->alert_label);
        z2z_save_settings(app);
        z2z_play_alert(app, false);
        break;
    case Z2ZMenuLastOutput:
        text_box_set_text(app->text_box, furi_string_get_cstr(app->text_buf));
        view_dispatcher_switch_to_view(app->view_dispatcher, Z2ZViewOutput);
        break;
    case Z2ZMenuAbout: {
        const char* mode = z2z_rpc_is_active(app) ?
                               (app->started_by_rpc ? "BLE RPC Duplex (Remote)" :
                                                      "BLE RPC Duplex") :
                               "Pocket Mailbox (/ext)";
        furi_string_printf(
            app->text_buf,
            "Zero2Zero Kali Companion\n"
            "FAP Version: v" Z2Z_FAP_VERSION "\n"
            "Kali Suite:  v" Z2Z_SUITE_VERSION "\n"
            "Mode: %s\n"
            "Alert: %s\n\n"
            "CardputerZero x Flipper BLE\n"
            "3-LED IR Deck, CC1101->rtl433,\n"
            "IMX219 Cam->NFC/RFID, Jobs.",
            mode,
            z2z_alert_title(app->alert_mode));
        text_box_set_text(app->text_box, furi_string_get_cstr(app->text_buf));
        view_dispatcher_switch_to_view(app->view_dispatcher, Z2ZViewOutput);
        break;
    }
    default:
        break;
    }
}

static uint32_t z2z_nav_exit_cb(void* context) {
    /*
     * `submenu_alloc()` (`0x0807d7a0`) and `text_box_alloc()` (`0x0807dd04`)
     * set `view->context` to `Submenu*` (4 bytes) / `TextBox*` (8 bytes), NOT `Zero2ZeroApp*`!
     * NEVER cast `context` to `Zero2ZeroApp*` inside a `ViewNavigationCallback` registered on
     * a `Submenu` or `TextBox` view. Returning `VIEW_NONE` stops `ViewDispatcher` so
     * `zero2zero_app()` cleans up safely with the real `Zero2ZeroApp*`.
     */
    UNUSED(context);
    return VIEW_NONE;
}

static bool z2z_back_event_cb(void* context) {
    Zero2ZeroApp* app = context;
    if(app) {
        app->waiting_mailbox = false;
        app->pending_tx_due_tick = 0;
        view_dispatcher_switch_to_view(app->view_dispatcher, Z2ZViewMenu);
    }
    return true;
}

static bool z2z_custom_event_cb(void* context, uint32_t event) {
    Zero2ZeroApp* app = context;
    if(event == Z2ZEventRxFrame) {
        if(__atomic_load_n(&app->app_exiting, __ATOMIC_ACQUIRE)) return true;
        furi_mutex_acquire(app->rx_mutex, FuriWaitForever);
        memcpy(app->work_buf, app->pending_rx, sizeof(app->work_buf));
        furi_mutex_release(app->rx_mutex);

        z2z_handle_json_response(app, app->work_buf);
        return true;
    }
    if(event == Z2ZEventIrRx) {
        z2z_on_ir_rx_event(app);
        return true;
    }
    if(event == Z2ZEventRpcClosed) {
        app->pending_tx_due_tick = 0;
        app->waiting_mailbox = false;
        return true;
    }
    if(event == Z2ZEventRpcExit) {
        furi_mutex_acquire(app->rpc_mutex, FuriWaitForever);
        __atomic_store_n(&app->app_exiting, true, __ATOMIC_SEQ_CST);
        furi_mutex_release(app->rpc_mutex);
        app->pending_tx_due_tick = 0;
        app->waiting_mailbox = false;
        z2z_sub_stop_rx(app);
        z2z_ir_stop_rx(app);
        view_dispatcher_switch_to_view(app->view_dispatcher, VIEW_NONE);
        return true;
    }
    return true;
}

static void z2z_tick_cb(void* context) {
    Zero2ZeroApp* app = context;
    if(__atomic_load_n(&app->app_exiting, __ATOMIC_ACQUIRE)) return;

    if(app->pending_tx_due_tick != 0 &&
       (int32_t)(furi_get_tick() - app->pending_tx_due_tick) >= 0) {
        app->pending_tx_due_tick = 0;
        if(app->pending_tx_len > 0) {
            z2z_rpc_send_exchange(
                app, (const uint8_t*)app->pending_tx_buf, app->pending_tx_len);
        }
    }

    if(app->sub_rx_active) {
        z2z_pump_sub_live(app);
        return;
    }

    if(z2z_rpc_is_active(app) || !app->waiting_mailbox) return;
    if((++app->mailbox_subtick % 5U) != 0U) return;

    if(storage_common_stat(app->storage, Z2Z_RESP_PATH, NULL) == FSE_OK) {
        File* f = storage_file_alloc(app->storage);
        if(storage_file_open(f, Z2Z_RESP_PATH, FSAM_READ, FSOM_OPEN_EXISTING)) {
            size_t n = storage_file_read(f, app->work_buf, sizeof(app->work_buf) - 1);
            app->work_buf[n] = '\0';
            storage_file_close(f);

            if(n > 0 && z2z_json_matches_seq(app->work_buf, app->seq)) {
                app->waiting_mailbox = false;
                storage_file_free(f);
                storage_simply_remove(app->storage, Z2Z_RESP_PATH);
                z2z_handle_json_response(app, app->work_buf);
                return;
            }
        }
        storage_file_free(f);
    }

    if(++app->mailbox_poll_ticks > 16U) {
        app->waiting_mailbox = false;
        furi_string_set_str(
            app->text_buf,
            "No reply from Kali yet.\n\n"
            "Tip: Launch via REMOTE -> [7:FAP]\n"
            "for live BLE RPC Duplex, or\n"
            "enable LINK -> [7:WATCH].");
        text_box_set_text(app->text_box, furi_string_get_cstr(app->text_buf));
    }
}

static Zero2ZeroApp* zero2zero_alloc(void) {
    Zero2ZeroApp* app = malloc(sizeof(Zero2ZeroApp));
    memset(app, 0, sizeof(Zero2ZeroApp));

    app->gui = furi_record_open(RECORD_GUI);
    app->storage = furi_record_open(RECORD_STORAGE);
    app->notifications = furi_record_open(RECORD_NOTIFICATION);
    app->text_buf = furi_string_alloc_set_str(
        "Zero2Zero v" Z2Z_FAP_VERSION " (Kali " Z2Z_SUITE_VERSION ")\nSelect an action from menu.");
    app->rx_mutex = furi_mutex_alloc(FuriMutexTypeNormal);
    app->rpc_mutex = furi_mutex_alloc(FuriMutexTypeNormal);
    app->seq = (uint32_t)(furi_get_tick() & 0xFFFF);

    z2z_load_settings(app);

    app->view_dispatcher = view_dispatcher_alloc();
    view_dispatcher_set_event_callback_context(app->view_dispatcher, app);
    view_dispatcher_set_custom_event_callback(app->view_dispatcher, z2z_custom_event_cb);
    view_dispatcher_set_navigation_event_callback(app->view_dispatcher, z2z_back_event_cb);
    view_dispatcher_set_tick_event_callback(app->view_dispatcher, z2z_tick_cb, 180);

    app->submenu = submenu_alloc();
    submenu_set_header(app->submenu, "Zero2Zero v" Z2Z_FAP_VERSION " (Kali)");
    submenu_add_item(app->submenu, "Kali Health & Ping", Z2ZMenuPing, z2z_submenu_cb, app);
    submenu_add_item(app->submenu, "Wireless & LAN Recon", Z2ZMenuNet, z2z_submenu_cb, app);
    submenu_add_item(
        app->submenu, "Live Sub-GHz -> rtl_433", Z2ZMenuSubLive, z2z_submenu_cb, app);
    submenu_add_item(
        app->submenu, "IR Blaster & Learn", Z2ZMenuIrLive, z2z_submenu_cb, app);
    submenu_add_item(
        app->submenu, "Browse & Run Jobs...", Z2ZMenuJobs, z2z_submenu_cb, app);
    submenu_add_item(
        app->submenu, "Inspect Latest Capture", Z2ZMenuCapLatest, z2z_submenu_cb, app);
    submenu_add_item(
        app->submenu, "Decode Latest .sub", Z2ZMenuSubLatest, z2z_submenu_cb, app);
    submenu_add_item(app->submenu, "Vault Sync Captures", Z2ZMenuVault, z2z_submenu_cb, app);
    submenu_add_item(
        app->submenu, "Sync Clock from Kali", Z2ZMenuClockSync, z2z_submenu_cb, app);
    submenu_add_item(
        app->submenu, app->alert_label, Z2ZMenuAlertToggle, z2z_submenu_cb, app);
    submenu_add_item(app->submenu, "Last Output", Z2ZMenuLastOutput, z2z_submenu_cb, app);
    submenu_add_item(
        app->submenu,
        "About: v" Z2Z_FAP_VERSION " (Kali " Z2Z_SUITE_VERSION ")",
        Z2ZMenuAbout,
        z2z_submenu_cb,
        app);
    view_set_previous_callback(submenu_get_view(app->submenu), z2z_nav_exit_cb);
    view_dispatcher_add_view(
        app->view_dispatcher, Z2ZViewMenu, submenu_get_view(app->submenu));

    app->jobs_submenu = submenu_alloc();
    submenu_set_header(app->jobs_submenu, "Run Kali Offload Job");
    view_dispatcher_add_view(
        app->view_dispatcher, Z2ZViewJobsMenu, submenu_get_view(app->jobs_submenu));

    app->text_box = text_box_alloc();
    text_box_set_font(app->text_box, TextBoxFontText);
    text_box_set_focus(app->text_box, TextBoxFocusStart);
    text_box_set_text(app->text_box, furi_string_get_cstr(app->text_buf));
    view_dispatcher_add_view(
        app->view_dispatcher, Z2ZViewOutput, text_box_get_view(app->text_box));

    app->sub_live_view = view_alloc();
    view_allocate_model(app->sub_live_view, ViewModelTypeLocking, sizeof(Z2ZSubLiveModel));
    view_set_context(app->sub_live_view, app);
    view_set_draw_callback(app->sub_live_view, z2z_sub_live_draw_cb);
    view_set_input_callback(app->sub_live_view, z2z_sub_live_input_cb);
    view_set_enter_callback(app->sub_live_view, z2z_sub_live_enter_cb);
    view_set_exit_callback(app->sub_live_view, z2z_sub_live_exit_cb);
    view_dispatcher_add_view(app->view_dispatcher, Z2ZViewSubLive, app->sub_live_view);

    app->ir_live_view = view_alloc();
    view_allocate_model(app->ir_live_view, ViewModelTypeLocking, sizeof(Z2ZIrLiveModel));
    view_set_context(app->ir_live_view, app);
    view_set_draw_callback(app->ir_live_view, z2z_ir_live_draw_cb);
    view_set_input_callback(app->ir_live_view, z2z_ir_live_input_cb);
    view_set_enter_callback(app->ir_live_view, z2z_ir_live_enter_cb);
    view_set_exit_callback(app->ir_live_view, z2z_ir_live_exit_cb);
    view_dispatcher_add_view(app->view_dispatcher, Z2ZViewIrLive, app->ir_live_view);

    return app;
}

static void zero2zero_free(Zero2ZeroApp* app) {
    furi_mutex_acquire(app->rpc_mutex, FuriWaitForever);
    __atomic_store_n(&app->app_exiting, true, __ATOMIC_SEQ_CST);
    furi_mutex_release(app->rpc_mutex);
    z2z_sub_stop_rx(app);
    z2z_ir_stop_rx(app);

    /*
     * In Flipper firmware (`rpc_system_app_free` on `Tmr Svc`),
     * `rpc_app->callback != NULL` is the ownership lease preventing `vPortFree(rpc_app)`
     * (`BusFault` if `rpc_system_app_send_exited` dereferences `rpc_app->session` after
     * clearing `callback`), while holding `rpc_mutex` across `rpc_send` can deadlock
     * `Tmr Svc` if `SessionClose` fires concurrently. Transition `Z2ZRpcActive ->
     * Z2ZRpcExiting` lock-free, send `APP_CLOSED` while holding the `callback != NULL`
     * lease, and clear `callback` LAST.
     */
    uint8_t st = Z2ZRpcActive;
    if(__atomic_compare_exchange_n(
           &app->rpc_state, &st, Z2ZRpcExiting, false, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
        RpcAppSystem* rpc = __atomic_load_n(&app->rpc_ctx, __ATOMIC_ACQUIRE);
        if(rpc) {
            rpc_system_app_send_exited(rpc);
            rpc_system_app_set_callback(rpc, NULL, NULL);
        }
        __atomic_store_n(&app->rpc_ctx, NULL, __ATOMIC_RELEASE);
        __atomic_store_n(&app->rpc_state, Z2ZRpcClosed, __ATOMIC_RELEASE);
        furi_delay_tick(2);
    }

    view_dispatcher_remove_view(app->view_dispatcher, Z2ZViewMenu);
    view_dispatcher_remove_view(app->view_dispatcher, Z2ZViewJobsMenu);
    view_dispatcher_remove_view(app->view_dispatcher, Z2ZViewOutput);
    view_dispatcher_remove_view(app->view_dispatcher, Z2ZViewSubLive);
    view_dispatcher_remove_view(app->view_dispatcher, Z2ZViewIrLive);
    submenu_free(app->submenu);
    submenu_free(app->jobs_submenu);
    text_box_free(app->text_box);
    view_free(app->sub_live_view);
    view_free(app->ir_live_view);
    furi_mutex_acquire(app->rpc_mutex, FuriWaitForever);
    ViewDispatcher* vd = app->view_dispatcher;
    app->view_dispatcher = NULL;
    furi_mutex_release(app->rpc_mutex);
    view_dispatcher_free(vd);
    if(app->ir_worker) {
        infrared_worker_free(app->ir_worker);
        app->ir_worker = NULL;
    }

    furi_mutex_free(app->rpc_mutex);
    furi_mutex_free(app->rx_mutex);
    furi_string_free(app->text_buf);
    furi_record_close(RECORD_NOTIFICATION);
    furi_record_close(RECORD_STORAGE);
    furi_record_close(RECORD_GUI);
    free(app);
}

int32_t zero2zero_app(void* p) {
    Zero2ZeroApp* app = zero2zero_alloc();

    /*
     * Flipper passes `"RPC <hex_ptr>"` string in `p` when started via
     * `PB_App.StartRequest(args="RPC")`, NOT a raw `RpcAppSystem*` struct pointer!
     */
    if(p && strncmp((const char*)p, "RPC ", 4) == 0) {
        char* endp = NULL;
        unsigned long rpc_ptr = strtoul((const char*)p + 4, &endp, 16);
        if(endp != ((const char*)p + 4) && rpc_ptr != 0) {
            RpcAppSystem* rpc = (RpcAppSystem*)(uintptr_t)rpc_ptr;
            app->started_by_rpc = true;
            __atomic_store_n(&app->rpc_ctx, rpc, __ATOMIC_RELEASE);
            __atomic_store_n(&app->rpc_state, Z2ZRpcActive, __ATOMIC_SEQ_CST);
            rpc_system_app_set_callback(rpc, z2z_rpc_callback, app);
            rpc_system_app_send_started(rpc);
        }
    }

    view_dispatcher_attach_to_gui(
        app->view_dispatcher, app->gui, ViewDispatcherTypeFullscreen);
    view_dispatcher_switch_to_view(app->view_dispatcher, Z2ZViewMenu);
    view_dispatcher_run(app->view_dispatcher);
    furi_mutex_acquire(app->rpc_mutex, FuriWaitForever);
    __atomic_store_n(&app->app_exiting, true, __ATOMIC_SEQ_CST);
    furi_mutex_release(app->rpc_mutex);

    zero2zero_free(app);
    return 0;
}
