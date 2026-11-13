/*
 * z2z-ui.c — 320x170 RGB565 Handheld BLE Companion UI for M5Stack CardputerZero
 *
 * Talks to `z2zd` over a non-blocking Unix domain socket (`/run/zero2zero/rpc.sock`),
 * keeping the UI responsive during BLE scans, 6-digit passkey entry, job execution,
 * Sub-GHz `.sub` analysis, and incremental capture vault sync.
 *
 * Keys:
 *   ',' / '.'  or Left / Right : Switch tabs (LINK | REMOTE | CAPS | STUDIO | JOBS | NET)
 *   Up / Down  or ';' / '/'    : Move list selection / scroll output
 *   '4' '5' '6' '7' '8'        : Softkeys aligned directly beneath LCD
 *   '`' (ESC) hold 1.2s / '8'  : Exit back to APPLaunch
 */

#include "cz_ui.h"

#include <sys/socket.h>
#include <sys/un.h>
#include <sys/wait.h>

#define MAX_DEVICES   8
#define MAX_JOBS     20
#define MAX_CAPS     24
#define MAX_IR_BTNS  48
#define MAX_LOG_LINES 24
#define LOG_LINE_LEN  54
#define STUDIO_MAX   512
#define FKEY_RING_CAP 8
#define QR_MAX_DIM   69

struct fkey_item {
    char key[12];
    char mode[8];
    int coalesce_repeat;
};

enum ui_tab {
    TAB_LINK   = 0,
    TAB_REMOTE = 1,
    TAB_CAP    = 2,
    TAB_STUDIO = 3,
    TAB_JOBS   = 4,
    TAB_NET    = 5,
    TAB_COUNT  = 6
};

struct ble_dev_row {
    char address[24];
    char name[28];
    char shell[16];
    int rssi;
    int paired;
    int connected;
};

struct job_row {
    char id[28];
    char title[36];
};

struct cap_row {
    char kind[12];
    char name[32];
    char path[80];
    int size;
};

struct ir_btn_row {
    int index;
    char name[24];
    char summary[28];
};

static volatile sig_atomic_t g_quit = 0;

static void on_sig(int sig)
{
    (void)sig;
    g_quit = 1;
}

/* --- Minimal Zero-Dep JSON Extractors --- */

static const char *find_json_key(const char *json, const char *key)
{
    char pat[64];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *p = json;
    size_t plen = strlen(pat);
    while ((p = strstr(p, pat)) != NULL) {
        const char *q = p + plen;
        while (*q == ' ' || *q == '\t') q++;
        if (*q == ':') {
            q++;
            while (*q == ' ' || *q == '\t') q++;
            return q;
        }
        p += plen;
    }
    return NULL;
}

static int json_get_str(const char *json, const char *key, char *out, size_t out_sz)
{
    if (!out || out_sz == 0) return -1;
    out[0] = '\0';
    const char *v = find_json_key(json, key);
    if (!v || *v != '"') return -1;
    v++;
    size_t i = 0;
    while (*v && *v != '"' && i + 1 < out_sz) {
        if (*v == '\\' && v[1]) {
            v++;
            if (*v == 'n') out[i++] = '\n';
            else if (*v == 't') out[i++] = ' ';
            else if (*v == 'r') { /* skip */ }
            else out[i++] = *v;
            v++;
        } else {
            out[i++] = *v++;
        }
    }
    out[i] = '\0';
    return 0;
}

static int json_get_int(const char *json, const char *key, int *out)
{
    const char *v = find_json_key(json, key);
    if (!v) return -1;
    if (*v == '-' || isdigit((unsigned char)*v)) {
        *out = (int)strtol(v, NULL, 10);
        return 0;
    }
    return -1;
}

static int json_get_bool(const char *json, const char *key, int *out)
{
    const char *v = find_json_key(json, key);
    if (!v) return -1;
    if (strncmp(v, "true", 4) == 0) { *out = 1; return 0; }
    if (strncmp(v, "false", 5) == 0) { *out = 0; return 0; }
    return -1;
}

/* Split multi-line text into compact 52-col display lines */
static int split_lines(const char *text, char lines[MAX_LOG_LINES][LOG_LINE_LEN])
{
    int count = 0;
    for (int i = 0; i < MAX_LOG_LINES; i++) lines[i][0] = '\0';
    if (!text || !*text) return 0;

    const char *p = text;
    while (*p && count < MAX_LOG_LINES) {
        int col = 0;
        while (*p && *p != '\n' && col < LOG_LINE_LEN - 1) {
            lines[count][col++] = *p++;
        }
        lines[count][col] = '\0';
        count++;
        if (*p == '\n') p++;
    }
    return count;
}

static int parse_array_objects(const char *json, const char *arr_key,
                               const char *starts[], const char *ends[], int max_items)
{
    const char *v = find_json_key(json, arr_key);
    if (!v || *v != '[') return 0;
    v++;
    int n = 0;
    int depth = 0;
    int in_str = 0;
    const char *obj_start = NULL;

    for (const char *p = v; *p && n < max_items; p++) {
        if (*p == '"' && (p == v || p[-1] != '\\')) {
            in_str = !in_str;
            continue;
        }
        if (in_str) continue;
        if (*p == '{') {
            if (depth == 0) obj_start = p;
            depth++;
        } else if (*p == '}') {
            depth--;
            if (depth == 0 && obj_start) {
                starts[n] = obj_start;
                ends[n] = p + 1;
                n++;
                obj_start = NULL;
            }
        } else if (*p == ']' && depth == 0) {
            break;
        }
    }
    return n;
}

/* --- UI State --- */

struct app_state {
    int tab;
    int watch_enabled;
    int connected;
    char link_name[28];
    char link_addr[24];
    char link_shell[16];
    int link_rssi;
    int flipper_bat;
    int flipper_free_mb;
    int kali_temp_c;
    int kali_mem_used;
    int kali_mem_total;

    struct ble_dev_row devs[MAX_DEVICES];
    int dev_count;
    int dev_sel;

    struct job_row jobs[MAX_JOBS];
    int job_count;
    int job_sel;

    struct cap_row caps[MAX_CAPS];
    int cap_count;
    int cap_sel;
    int cap_view_detail;

    /* Live 128x64 Flipper screen stream state */
    int mirror_active;
    int want_screen_stop;
    uint8_t screen_fb[1024];
    int has_screen_fb;
    int screen_orient;
    int screen_seq;
    double last_frame_poll;
    struct fkey_item fkey_ring[FKEY_RING_CAP];
    int fkey_head;
    int fkey_count;
    int input_queue_depth;
    double last_repeat_mono;

    /* 46-key QWERTY USB script studio state */
    char studio_buf[STUDIO_MAX];
    int studio_len;
    int studio_raw_mode;
    int studio_edit_active;
    int studio_tpl_idx;
    int shift_held;

    char alert_mode[12];

    char log_lines[MAX_LOG_LINES][LOG_LINE_LEN];
    int log_count;
    int log_scroll;

    char status_banner[160];
    uint16_t banner_col;

    /* Persistent async IPC & 6-digit PIN modal */
    int ipc_fd;
    int ipc_busy;
    char pending_op[24];
    char rx_buf[32768];
    size_t rx_len;

    int pin_modal;
    char pin_dev_name[28];
    char pin_dev_addr[24];
    char pin_digits[10];
    int pin_len;

    /* Kali `qrencode` modal + NDEF `.nfc` synthesizer state */
    int qr_modal;
    int qr_size;
    uint8_t qr_bits[QR_MAX_DIM * QR_MAX_DIM];
    char qr_payload[192];
    char last_cap_qr[192];

    /* Capture Diff mark + QWERTY High-Power IR Remote Deck modal */
    char diff_mark_path[80];
    char diff_mark_name[32];

    int ir_modal;
    char ir_src[80];
    char ir_remotes[24][80];
    int ir_remote_count;
    int ir_remote_idx;
    struct ir_btn_row ir_btns[MAX_IR_BTNS];
    int ir_btn_count;
    int ir_sel;
    char ir_filter[20];
    int ir_filter_len;
};

static int ci_substr(const char *hay, const char *needle)
{
    if (!needle || !*needle) return 1;
    if (!hay) return 0;
    size_t nlen = strlen(needle);
    for (const char *p = hay; *p; p++) {
        size_t i = 0;
        while (i < nlen && p[i] &&
               tolower((unsigned char)p[i]) == tolower((unsigned char)needle[i])) {
            i++;
        }
        if (i == nlen) return 1;
    }
    return 0;
}

static int ir_filtered_indices(const struct app_state *st, int out_idx[MAX_IR_BTNS])
{
    int cnt = 0;
    for (int i = 0; i < st->ir_btn_count && cnt < MAX_IR_BTNS; i++) {
        if (ci_substr(st->ir_btns[i].name, st->ir_filter) ||
            ci_substr(st->ir_btns[i].summary, st->ir_filter)) {
            out_idx[cnt++] = i;
        }
    }
    return cnt;
}

static void set_banner(struct app_state *st, const char *msg, uint16_t col)
{
    snprintf(st->status_banner, sizeof(st->status_banner), "%s", msg ? msg : "");
    st->banner_col = col;
}

static void resolve_sock_path(char *out, size_t sz)
{
    const char *env = getenv("Z2Z_SOCKET");
    if (env && *env) {
        snprintf(out, sz, "%s", env);
        return;
    }
    if (access("/run/zero2zero/rpc.sock", W_OK) == 0 || geteuid() == 0) {
        snprintf(out, sz, "/run/zero2zero/rpc.sock");
        return;
    }
    const char *xdg = getenv("XDG_RUNTIME_DIR");
    if (xdg && *xdg) {
        snprintf(out, sz, "%s/rpc.sock", xdg);
    } else {
        snprintf(out, sz, "/tmp/zero2zero-%u/rpc.sock", (unsigned)getuid());
    }
}

static void try_autospawn_daemon(const char *sock_path)
{
    pid_t pid = fork();
    if (pid != 0) return;
    setsid();
    int nullfd = open("/dev/null", O_RDWR);
    if (nullfd >= 0) {
        dup2(nullfd, STDIN_FILENO);
        dup2(nullfd, STDOUT_FILENO);
        dup2(nullfd, STDERR_FILENO);
        if (nullfd > 2) close(nullfd);
    }
    const char *env_bin = getenv("Z2Z_DAEMON_BIN");
    if (env_bin && access(env_bin, X_OK) == 0)
        execl(env_bin, env_bin, "--socket", sock_path, (char *)NULL);
    if (access("/usr/lib/zero2zero/z2zd", X_OK) == 0)
        execl("/usr/lib/zero2zero/z2zd", "z2zd", "--socket", sock_path, (char *)NULL);
    execlp("z2zd", "z2zd", "--socket", sock_path, (char *)NULL);
    _exit(127);
}

static void clear_fkey_ring(struct app_state *st)
{
    st->fkey_head = 0;
    st->fkey_count = 0;
    st->input_queue_depth = 0;
}

static void ipc_abort_conn(struct app_state *st)
{
    if (st->ipc_fd >= 0) {
        close(st->ipc_fd);
        st->ipc_fd = -1;
    }
    st->ipc_busy = 0;
    st->rx_len = 0;
    st->pin_modal = 0;
    st->pending_op[0] = '\0';
    clear_fkey_ring(st);
}

static int ipc_open_socket(struct app_state *st)
{
    char spath[108];
    resolve_sock_path(spath, sizeof(spath));

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", spath);

    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) return -1;

    if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        close(fd);
        fd = -1;
        try_autospawn_daemon(spath);
        for (int attempt = 0; attempt < 15; attempt++) {
            usleep(80000);
            fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
            if (fd >= 0 && connect(fd, (struct sockaddr *)&addr, sizeof(addr)) == 0)
                break;
            if (fd >= 0) { close(fd); fd = -1; }
        }
    }

    if (fd < 0) {
        set_banner(st, "z2zd daemon unavailable", COL_RED);
        return -1;
    }

    int flags = fcntl(fd, F_GETFL, 0);
    if (flags >= 0) fcntl(fd, F_SETFL, flags | O_NONBLOCK);
    st->ipc_fd = fd;
    st->rx_len = 0;
    return fd;
}

static int ipc_send_request(struct app_state *st, const char *op_label, const char *json_line)
{
    int quiet_op = (strcmp(op_label, "screen_frame") == 0 ||
                    strcmp(op_label, "gui_input") == 0 ||
                    strcmp(op_label, "screen_stop") == 0);
    if (st->ipc_busy) {
        if (!quiet_op)
            set_banner(st, "Busy with active BLE request...", COL_YELLOW);
        return -1;
    }

    size_t len = strlen(json_line);
    if (st->ipc_fd >= 0) {
        if (write(st->ipc_fd, json_line, len) != (ssize_t)len) {
            ipc_abort_conn(st);
        }
    }

    if (st->ipc_fd < 0) {
        if (ipc_open_socket(st) < 0)
            return -1;
        if (write(st->ipc_fd, json_line, len) != (ssize_t)len) {
            ipc_abort_conn(st);
            set_banner(st, "Socket write failed", COL_RED);
            return -1;
        }
    }

    st->ipc_busy = 1;
    snprintf(st->pending_op, sizeof(st->pending_op), "%s", op_label);
    if (!quiet_op) {
        char msg[54];
        snprintf(msg, sizeof(msg), "Running %s...", op_label);
        set_banner(st, msg, COL_CYAN);
    }
    return 0;
}

static int hex_nibble(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return 10 + (c - 'a');
    if (c >= 'A' && c <= 'F') return 10 + (c - 'A');
    return -1;
}

static int decode_screen_hex(const char *hex, uint8_t out[1024])
{
    for (int i = 0; i < 1024; i++) {
        int hi = hex_nibble(hex[i * 2]);
        int lo = hex_nibble(hex[i * 2 + 1]);
        if (hi < 0 || lo < 0) return -1;
        out[i] = (uint8_t)((hi << 4) | lo);
    }
    return 0;
}

static void json_escape_str(const char *src, char *dst, size_t dst_sz)
{
    size_t j = 0;
    for (size_t i = 0; src[i] && j + 3 < dst_sz; i++) {
        unsigned char c = (unsigned char)src[i];
        if (c == '"' || c == '\\') {
            dst[j++] = '\\';
            dst[j++] = (char)c;
        } else if (c == '\n') {
            dst[j++] = '\\';
            dst[j++] = 'n';
        } else if (c >= 32 && c < 127) {
            dst[j++] = (char)c;
        }
    }
    dst[j] = '\0';
}

static void apply_studio_preset(struct app_state *st, int idx)
{
    static const char *presets[4] = {
        "Checked by {{HOSTNAME}} ({{HOST_IP}}) at {{LOCAL_TIME}}",
        "REM Linux interface summary\nDELAY 400\nSTRING ip -br addr && ip route\nENTER",
        "REM Ping CardputerZero\nDELAY 400\nSTRING ping -c 3 {{HOST_IP}}\nENTER",
        "REM Windows network summary\nDELAY 500\nGUI r\nDELAY 350\nSTRING cmd /k ipconfig\nENTER"
    };
    static const int raw_flags[4] = { 0, 1, 1, 1 };
    int k = idx & 3;
    st->studio_tpl_idx = k;
    st->studio_raw_mode = raw_flags[k];
    snprintf(st->studio_buf, sizeof(st->studio_buf), "%s", presets[k]);
    st->studio_len = (int)strlen(st->studio_buf);
}

static void update_screen_from_json(struct app_state *st, const char *line, int allow_activate)
{
    char hex[2100];
    if (json_get_str(line, "frame_hex", hex, sizeof(hex)) == 0 && strlen(hex) >= 2048) {
        if (decode_screen_hex(hex, st->screen_fb) == 0) {
            st->has_screen_fb = 1;
        }
    }
    json_get_int(line, "orientation", &st->screen_orient);
    json_get_int(line, "seq", &st->screen_seq);

    int conn = st->connected;
    if (json_get_bool(line, "connected", &conn) == 0) {
        st->connected = conn;
    }
    int streaming = st->mirror_active;
    if (json_get_bool(line, "streaming", &streaming) == 0) {
        if (allow_activate) {
            st->mirror_active = streaming;
        } else if (!streaming) {
            /* In-flight `screen_frame` / `gui_input` must NEVER re-enable
             * `mirror_active` after user pressed [4:STOP], and must auto-stop on link drop! */
            st->mirror_active = 0;
        }
    }
    if (!st->connected && st->mirror_active) {
        st->mirror_active = 0;
    }
}

static int populate_devices_from_json(struct app_state *st, const char *line)
{
    const char *starts[MAX_DEVICES], *ends[MAX_DEVICES];
    int n = parse_array_objects(line, "devices", starts, ends, MAX_DEVICES);
    st->dev_count = n;
    if (st->dev_sel >= n) st->dev_sel = 0;
    for (int i = 0; i < n; i++) {
        char sub[512];
        size_t sl = (size_t)(ends[i] - starts[i]);
        if (sl >= sizeof(sub)) sl = sizeof(sub) - 1;
        memcpy(sub, starts[i], sl);
        sub[sl] = '\0';
        memset(&st->devs[i], 0, sizeof(st->devs[i]));
        json_get_str(sub, "address", st->devs[i].address, sizeof(st->devs[i].address));
        json_get_str(sub, "name", st->devs[i].name, sizeof(st->devs[i].name));
        json_get_str(sub, "shell_color", st->devs[i].shell, sizeof(st->devs[i].shell));
        json_get_int(sub, "rssi", &st->devs[i].rssi);
        json_get_bool(sub, "paired", &st->devs[i].paired);
        json_get_bool(sub, "connected", &st->devs[i].connected);
    }
    return n;
}

static void handle_ipc_json_line(struct app_state *st, const char *line)
{
    char ev[24];
    if (json_get_str(line, "event", ev, sizeof(ev)) == 0) {
        if (strcmp(ev, "pin_required") == 0) {
            st->pin_modal = 1;
            st->pin_len = 0;
            st->pin_digits[0] = '\0';
            json_get_str(line, "name", st->pin_dev_name, sizeof(st->pin_dev_name));
            json_get_str(line, "address", st->pin_dev_addr, sizeof(st->pin_dev_addr));
            set_banner(st, "Type 6-digit PIN from Flipper LCD", COL_YELLOW);
            return;
        }
        if (strcmp(ev, "pin_cancelled") == 0) {
            st->pin_modal = 0;
            set_banner(st, "BLE peer cancelled passkey prompt", COL_ORANGE);
            return;
        }
        if (strcmp(ev, "screen_frame") == 0) {
            int was_mirror = st->mirror_active;
            update_screen_from_json(st, line, 0);
            json_get_int(line, "queue_depth", &st->input_queue_depth);
            if (was_mirror && !st->mirror_active) {
                clear_fkey_ring(st);
                set_banner(st, st->connected ? "Screen stream ended"
                                             : "BLE disconnected - mirror stopped", COL_ORANGE);
            }
            return;
        }
        if (strcmp(ev, "gui_input_state") == 0) {
            int was_mirror = st->mirror_active;
            json_get_int(line, "queue_depth", &st->input_queue_depth);
            int conn = st->connected;
            if (json_get_bool(line, "connected", &conn) == 0) st->connected = conn;
            int streaming = st->mirror_active;
            if (json_get_bool(line, "streaming", &streaming) == 0 && !streaming)
                st->mirror_active = 0;
            if (!st->connected) st->mirror_active = 0;
            char aerr[140];
            if (json_get_str(line, "error", aerr, sizeof(aerr)) == 0 && aerr[0]) {
                set_banner(st, aerr, COL_ORANGE);
            } else if (was_mirror && !st->mirror_active) {
                clear_fkey_ring(st);
                set_banner(st, st->connected ? "Screen stream ended"
                                             : "BLE disconnected - mirror stopped", COL_ORANGE);
            }
            return;
        }
    }

    int ok = 0;
    json_get_bool(line, "ok", &ok);
    if (!ok) {
        char err[160];
        if (json_get_str(line, "error", err, sizeof(err)) != 0)
            snprintf(err, sizeof(err), "Operation failed");
        if (strcmp(st->pending_op, "screen_frame") == 0 ||
            strcmp(st->pending_op, "gui_input") == 0) {
            st->mirror_active = 0;
            clear_fkey_ring(st);
            if (strstr(err, "disconnect") || strstr(err, "Not connected"))
                st->connected = 0;
            set_banner(st, err, COL_ORANGE);
        } else {
            set_banner(st, err, COL_RED);
        }
        st->ipc_busy = 0;
        st->pin_modal = 0;
        return;
    }

    if (strcmp(st->pending_op, "status") == 0) {
        json_get_bool(line, "watch_enabled", &st->watch_enabled);
        const char *lk = find_json_key(line, "link");
        if (lk) {
            json_get_bool(lk, "connected", &st->connected);
            json_get_str(lk, "name", st->link_name, sizeof(st->link_name));
            json_get_str(lk, "address", st->link_addr, sizeof(st->link_addr));
            json_get_str(lk, "shell_color", st->link_shell, sizeof(st->link_shell));
            json_get_int(lk, "rssi", &st->link_rssi);
            int b = -1;
            if (json_get_int(lk, "ble_battery_pct", &b) == 0 && b >= 0) st->flipper_bat = b;
        }
        const char *flp = find_json_key(line, "flipper");
        if (flp) {
            int chg = -1, fkb = 0;
            if (json_get_int(flp, "charge_pct", &chg) == 0) st->flipper_bat = chg;
            if (json_get_int(flp, "ext_free_kb", &fkb) == 0) st->flipper_free_mb = fkb / 1024;
        }
        const char *soc = find_json_key(line, "kali_soc");
        if (soc) {
            json_get_int(soc, "temp_c", &st->kali_temp_c);
            json_get_int(soc, "mem_used_mb", &st->kali_mem_used);
            json_get_int(soc, "mem_total_mb", &st->kali_mem_total);
        }
        if (st->dev_count == 0) {
            populate_devices_from_json(st, line);
        }
        set_banner(st, st->connected ? "BLE RPC Link Active" : "Ready (BLE Idle)", COL_GREEN);
    } else if (strcmp(st->pending_op, "scan") == 0) {
        int n = populate_devices_from_json(st, line);
        char msg[48];
        snprintf(msg, sizeof(msg), "Scan found %d Flipper(s)", n);
        set_banner(st, msg, n > 0 ? COL_GREEN : COL_ORANGE);
    } else if (strcmp(st->pending_op, "jobs") == 0) {
        const char *starts[MAX_JOBS], *ends[MAX_JOBS];
        int n = parse_array_objects(line, "jobs", starts, ends, MAX_JOBS);
        st->job_count = n;
        if (st->job_sel >= n) st->job_sel = 0;
        for (int i = 0; i < n; i++) {
            char sub[512];
            size_t sl = (size_t)(ends[i] - starts[i]);
            if (sl >= sizeof(sub)) sl = sizeof(sub) - 1;
            memcpy(sub, starts[i], sl);
            sub[sl] = '\0';
            json_get_str(sub, "id", st->jobs[i].id, sizeof(st->jobs[i].id));
            json_get_str(sub, "title", st->jobs[i].title, sizeof(st->jobs[i].title));
        }
        set_banner(st, "Loaded jobs.d manifests", COL_GREEN);
    } else if (strcmp(st->pending_op, "run") == 0) {
        char out[1024];
        if (json_get_str(line, "output", out, sizeof(out)) == 0) {
            st->log_count = split_lines(out, st->log_lines);
            st->log_scroll = 0;
        }
        set_banner(st, "Offload job complete", COL_GREEN);
    } else if (strcmp(st->pending_op, "sub") == 0 || strcmp(st->pending_op, "net") == 0) {
        char compact[1024];
        if (json_get_str(line, "compact_text", compact, sizeof(compact)) == 0) {
            st->log_count = split_lines(compact, st->log_lines);
            st->log_scroll = 0;
        }
        set_banner(st, "Updated report", COL_GREEN);
    } else if (strcmp(st->pending_op, "vault") == 0) {
        int pulled = 0, skipped = 0;
        json_get_int(line, "pulled_count", &pulled);
        json_get_int(line, "skipped_count", &skipped);
        char msg[54];
        snprintf(msg, sizeof(msg), "Vault: +%d new, %d unchanged", pulled, skipped);
        set_banner(st, msg, COL_GREEN);
    } else if (strcmp(st->pending_op, "watch") == 0) {
        json_get_bool(line, "watch_enabled", &st->watch_enabled);
        set_banner(st, st->watch_enabled ? "Pocket Watch ON" : "Pocket Watch OFF", COL_CYAN);
    } else if (strcmp(st->pending_op, "pair") == 0) {
        const char *dv = find_json_key(line, "device");
        if (dv) {
            if (st->dev_count == 0) {
                st->dev_count = 1;
                st->dev_sel = 0;
                memset(&st->devs[0], 0, sizeof(st->devs[0]));
            }
            json_get_str(dv, "address", st->devs[st->dev_sel].address, sizeof(st->devs[0].address));
            json_get_str(dv, "name", st->devs[st->dev_sel].name, sizeof(st->devs[0].name));
            json_get_str(dv, "shell_color", st->devs[st->dev_sel].shell, sizeof(st->devs[0].shell));
            json_get_int(dv, "rssi", &st->devs[st->dev_sel].rssi);
            st->devs[st->dev_sel].paired = 1;
        }
        int auto_conn = 0;
        json_get_bool(line, "connected", &auto_conn);
        const char *lk = find_json_key(line, "link");
        if (lk) {
            json_get_bool(lk, "connected", &auto_conn);
            if (auto_conn) {
                st->connected = 1;
                json_get_str(lk, "name", st->link_name, sizeof(st->link_name));
                json_get_str(lk, "address", st->link_addr, sizeof(st->link_addr));
                json_get_str(lk, "shell_color", st->link_shell, sizeof(st->link_shell));
                json_get_int(lk, "rssi", &st->link_rssi);
                int b = -1;
                if (json_get_int(lk, "ble_battery_pct", &b) == 0 && b >= 0) st->flipper_bat = b;
            }
        }
        if (auto_conn) {
            st->connected = 1;
            set_banner(st, "Paired & Connected! BLE RPC Active", COL_GREEN);
        } else {
            set_banner(st, "Paired! Press [6:CONN] for BLE RPC", COL_GREEN);
        }
    } else if (strcmp(st->pending_op, "unpair") == 0) {
        st->connected = 0;
        if (st->dev_count > 0 && st->dev_sel < st->dev_count) {
            for (int i = st->dev_sel; i + 1 < st->dev_count; i++)
                st->devs[i] = st->devs[i + 1];
            st->dev_count--;
            if (st->dev_sel >= st->dev_count && st->dev_count > 0)
                st->dev_sel = st->dev_count - 1;
        }
        set_banner(st, "Removed BlueZ bond (press [4:SCAN] or [5:PAIR])", COL_CYAN);
    } else if (strcmp(st->pending_op, "connect") == 0) {
        const char *lk = find_json_key(line, "link");
        st->connected = 1;
        if (lk) {
            json_get_bool(lk, "connected", &st->connected);
            json_get_str(lk, "name", st->link_name, sizeof(st->link_name));
            json_get_str(lk, "address", st->link_addr, sizeof(st->link_addr));
            json_get_str(lk, "shell_color", st->link_shell, sizeof(st->link_shell));
            json_get_int(lk, "rssi", &st->link_rssi);
        }
        set_banner(st, "BLE RPC Connected!", COL_GREEN);
    } else if (strcmp(st->pending_op, "screen_start") == 0) {
        st->connected = 1;
        update_screen_from_json(st, line, 1);
        st->mirror_active = 1;
        set_banner(st, "Live Mirror ON (Arrows/Ent/BS, [4]=Stop)", COL_CYAN);
    } else if (strcmp(st->pending_op, "screen_stop") == 0) {
        st->mirror_active = 0;
        clear_fkey_ring(st);
        json_get_bool(line, "connected", &st->connected);
        set_banner(st, "Stopped screen stream", COL_CYAN);
    } else if (strcmp(st->pending_op, "screen_frame") == 0 || strcmp(st->pending_op, "gui_input") == 0) {
        int was_mirror = st->mirror_active;
        update_screen_from_json(st, line, 0);
        json_get_int(line, "queue_depth", &st->input_queue_depth);
        if (was_mirror && !st->mirror_active) {
            clear_fkey_ring(st);
            set_banner(st, st->connected ? "Screen stream ended"
                                         : "BLE disconnected - mirror stopped", COL_ORANGE);
        }
    } else if (strcmp(st->pending_op, "clock") == 0) {
        char dt[32];
        if (json_get_str(line, "datetime", dt, sizeof(dt)) == 0) {
            char msg[64];
            snprintf(msg, sizeof(msg), "Flipper RTC synced: %s", dt);
            set_banner(st, msg, COL_GREEN);
        } else {
            set_banner(st, "Synced Flipper RTC", COL_GREEN);
        }
    } else if (strcmp(st->pending_op, "alert") == 0) {
        if (json_get_str(line, "alert_mode", st->alert_mode, sizeof(st->alert_mode)) == 0) {
            char msg[56];
            snprintf(msg, sizeof(msg), "Flipper alert mode -> %s", st->alert_mode);
            set_banner(st, msg, COL_GREEN);
        }
    } else if (strcmp(st->pending_op, "caps") == 0) {
        const char *starts[MAX_CAPS], *ends[MAX_CAPS];
        int n = parse_array_objects(line, "captures", starts, ends, MAX_CAPS);
        st->cap_count = n;
        if (st->cap_sel >= n) st->cap_sel = 0;
        st->cap_view_detail = 0;
        for (int i = 0; i < n; i++) {
            char sub[512];
            size_t sl = (size_t)(ends[i] - starts[i]);
            if (sl >= sizeof(sub)) sl = sizeof(sub) - 1;
            memcpy(sub, starts[i], sl);
            sub[sl] = '\0';
            memset(&st->caps[i], 0, sizeof(st->caps[i]));
            json_get_str(sub, "kind", st->caps[i].kind, sizeof(st->caps[i].kind));
            json_get_str(sub, "name", st->caps[i].name, sizeof(st->caps[i].name));
            json_get_str(sub, "path", st->caps[i].path, sizeof(st->caps[i].path));
            json_get_int(sub, "size", &st->caps[i].size);
        }
        char msg[54];
        snprintf(msg, sizeof(msg), "Listed %d capture(s) on Flipper", n);
        set_banner(st, msg, n > 0 ? COL_GREEN : COL_ORANGE);
    } else if (strcmp(st->pending_op, "cap_inspect") == 0) {
        char compact[1536];
        st->last_cap_qr[0] = '\0';
        json_get_str(line, "qr_payload", st->last_cap_qr, sizeof(st->last_cap_qr));
        if (json_get_str(line, "compact_text", compact, sizeof(compact)) == 0) {
            st->log_count = split_lines(compact, st->log_lines);
            st->log_scroll = 0;
            st->cap_view_detail = 1;
        }
        set_banner(st, st->last_cap_qr[0]
                       ? "Decoded capture ([Q]=QR Code, [4]=List)"
                       : "Decoded capture ([4:LIST] to return)", COL_GREEN);
    } else if (strcmp(st->pending_op, "qr_encode") == 0) {
        int qsz = 0;
        char flat[QR_MAX_DIM * QR_MAX_DIM + 8];
        if (json_get_int(line, "size", &qsz) == 0 && qsz > 0 && qsz <= QR_MAX_DIM &&
            json_get_str(line, "flat_bits", flat, sizeof(flat)) == 0 &&
            (int)strlen(flat) >= qsz * qsz) {
            st->qr_size = qsz;
            for (int i = 0; i < qsz * qsz; i++) {
                st->qr_bits[i] = (flat[i] == '1') ? 1U : 0U;
            }
            json_get_str(line, "payload", st->qr_payload, sizeof(st->qr_payload));
            st->qr_modal = 1;
            set_banner(st, "Rendered via Kali qrencode ([6]=Emulate NFC)", COL_GREEN);
        } else {
            set_banner(st, "QR matrix too large for 320x170 display", COL_ORANGE);
        }
    } else if (strcmp(st->pending_op, "nfc_make") == 0) {
        char rem[80];
        if (json_get_str(line, "remote", rem, sizeof(rem)) == 0 && rem[0]) {
            char msg[96];
            snprintf(msg, sizeof(msg), "NFC NDEF ready on Flipper: %.56s", rem);
            set_banner(st, msg, COL_GREEN);
        } else {
            set_banner(st, "Created NDEF .nfc tag!", COL_GREEN);
        }
    } else if (strcmp(st->pending_op, "rfid_make") == 0) {
        char kt[16], dh[36];
        kt[0] = '\0'; dh[0] = '\0';
        json_get_str(line, "key_type", kt, sizeof(kt));
        json_get_str(line, "data_hex", dh, sizeof(dh));
        char msg[96];
        snprintf(msg, sizeof(msg), "125kHz %s (%s) opened on Flipper!", kt[0] ? kt : "RFID", dh);
        set_banner(st, msg, COL_GREEN);
    } else if (strcmp(st->pending_op, "ibtn_make") == 0) {
        char kt[16], dh[36];
        kt[0] = '\0'; dh[0] = '\0';
        json_get_str(line, "key_type", kt, sizeof(kt));
        json_get_str(line, "data_hex", dh, sizeof(dh));
        char msg[96];
        snprintf(msg, sizeof(msg), "1-Wire %s (%s) opened on Flipper!", kt[0] ? kt : "iButton", dh);
        set_banner(st, msg, COL_GREEN);
    } else if (strcmp(st->pending_op, "cam_scan") == 0) {
        char sym[24], pay[96], tgt[12];
        sym[0] = '\0'; pay[0] = '\0'; tgt[0] = '\0';
        json_get_str(line, "symbology", sym, sizeof(sym));
        json_get_str(line, "payload", pay, sizeof(pay));
        json_get_str(line, "target", tgt, sizeof(tgt));
        char msg[140];
        snprintf(msg, sizeof(msg), "IMX219 [%s] -> Flipper %s: %.64s",
                 sym[0] ? sym : "Code", tgt[0] ? tgt : "NFC", pay);
        set_banner(st, msg, COL_GREEN);
    } else if (strcmp(st->pending_op, "capture_diff") == 0) {
        char compact[1536];
        if (json_get_str(line, "compact_text", compact, sizeof(compact)) == 0) {
            st->log_count = split_lines(compact, st->log_lines);
            st->log_scroll = 0;
            st->cap_view_detail = 1;
        }
        set_banner(st, "Structural Capture Diff ([4:LIST] to return)", COL_GREEN);
    } else if (strcmp(st->pending_op, "ir_list") == 0) {
        const char *starts[MAX_IR_BTNS], *ends[MAX_IR_BTNS];
        int n = parse_array_objects(line, "buttons", starts, ends, MAX_IR_BTNS);
        json_get_str(line, "src", st->ir_src, sizeof(st->ir_src));
        char rem_csv[2048];
        if (json_get_str(line, "remotes_csv", rem_csv, sizeof(rem_csv)) == 0 && rem_csv[0]) {
            st->ir_remote_count = 0;
            st->ir_remote_idx = 0;
            char *p = rem_csv;
            while (*p && st->ir_remote_count < 24) {
                char *bar = strchr(p, '|');
                if (bar) *bar = '\0';
                if (*p) {
                    int idx = st->ir_remote_count++;
                    snprintf(st->ir_remotes[idx], sizeof(st->ir_remotes[idx]), "%.79s", p);
                    if (strcmp(st->ir_remotes[idx], st->ir_src) == 0) {
                        st->ir_remote_idx = idx;
                    }
                }
                if (!bar) break;
                p = bar + 1;
            }
        }
        st->ir_btn_count = n;
        st->ir_sel = 0;
        st->ir_filter[0] = '\0';
        st->ir_filter_len = 0;
        for (int i = 0; i < n; i++) {
            char sub[512];
            size_t sl = (size_t)(ends[i] - starts[i]);
            if (sl >= sizeof(sub)) sl = sizeof(sub) - 1;
            memcpy(sub, starts[i], sl);
            sub[sl] = '\0';
            memset(&st->ir_btns[i], 0, sizeof(st->ir_btns[i]));
            st->ir_btns[i].index = i;
            json_get_int(sub, "index", &st->ir_btns[i].index);
            json_get_str(sub, "name", st->ir_btns[i].name, sizeof(st->ir_btns[i].name));
            json_get_str(sub, "summary", st->ir_btns[i].summary, sizeof(st->ir_btns[i].summary));
        }
        st->ir_modal = 1;
        char msg[80];
        if (st->ir_remote_count > 1) {
            snprintf(msg, sizeof(msg), "Loaded %d btn(s) — [,/.] switch remote, ENT=Fire", n);
        } else {
            snprintf(msg, sizeof(msg), "Loaded %d IR button(s) — type to filter, ENT=Fire", n);
        }
        set_banner(st, msg, COL_GREEN);
    } else if (strcmp(st->pending_op, "ir_tx") == 0) {
        char bnm[32], bsum[40];
        bnm[0] = '\0'; bsum[0] = '\0';
        json_get_str(line, "button", bnm, sizeof(bnm));
        json_get_str(line, "summary", bsum, sizeof(bsum));
        char msg[96];
        snprintf(msg, sizeof(msg), "Blasted IR '%s' [%s]!", bnm, bsum);
        set_banner(st, msg, COL_GREEN);
    } else if (strcmp(st->pending_op, "ir_save") == 0) {
        int armed_rx = 0;
        json_get_bool(line, "armed_rx", &armed_rx);
        if (armed_rx) {
            set_banner(st, "Armed Flipper IR RX! Point remote, then [5:SAVE-RX]", COL_CYAN);
        } else {
            char bnm[32], dst[80];
            bnm[0] = '\0'; dst[0] = '\0';
            json_get_str(line, "name", bnm, sizeof(bnm));
            json_get_str(line, "dst", dst, sizeof(dst));
            const char *starts[MAX_IR_BTNS], *ends[MAX_IR_BTNS];
            int n = parse_array_objects(line, "buttons", starts, ends, MAX_IR_BTNS);
            if (n > 0) {
                st->ir_btn_count = n;
                for (int i = 0; i < n; i++) {
                    char sub[512];
                    size_t sl = (size_t)(ends[i] - starts[i]);
                    if (sl >= sizeof(sub)) sl = sizeof(sub) - 1;
                    memcpy(sub, starts[i], sl);
                    sub[sl] = '\0';
                    memset(&st->ir_btns[i], 0, sizeof(st->ir_btns[i]));
                    st->ir_btns[i].index = i;
                    json_get_int(sub, "index", &st->ir_btns[i].index);
                    json_get_str(sub, "name", st->ir_btns[i].name, sizeof(st->ir_btns[i].name));
                    json_get_str(sub, "summary", st->ir_btns[i].summary, sizeof(st->ir_btns[i].summary));
                }
                st->ir_sel = n - 1;
                st->ir_filter[0] = '\0';
                st->ir_filter_len = 0;
            }
            char msg[96];
            snprintf(msg, sizeof(msg), "Saved learned IR '%s' -> %.44s", bnm, dst);
            set_banner(st, msg, COL_GREEN);
        }
    } else if (strcmp(st->pending_op, "cap_open") == 0) {
        char app_nm[32];
        if (json_get_str(line, "app", app_nm, sizeof(app_nm)) == 0) {
            char msg[64];
            snprintf(msg, sizeof(msg), "Opened in Flipper '%s' app!", app_nm);
            set_banner(st, msg, COL_GREEN);
        } else {
            set_banner(st, "Opened file on Flipper!", COL_GREEN);
        }
    } else if (strcmp(st->pending_op, "studio_push") == 0) {
        char rem[80];
        if (json_get_str(line, "remote", rem, sizeof(rem)) == 0) {
            char msg[96];
            snprintf(msg, sizeof(msg), "Saved %.64s on Flipper!", rem);
            set_banner(st, msg, COL_GREEN);
        } else {
            set_banner(st, "Pushed script to Flipper!", COL_GREEN);
        }
    } else {
        set_banner(st, "Done", COL_GREEN);
    }

    st->pin_modal = 0;
    st->ipc_busy = 0;
}

static void pump_ipc_socket(struct app_state *st)
{
    if (st->ipc_fd < 0) return;
    while (st->rx_len + 1 < sizeof(st->rx_buf)) {
        ssize_t r = read(st->ipc_fd, st->rx_buf + st->rx_len,
                         sizeof(st->rx_buf) - 1 - st->rx_len);
        if (r > 0) {
            st->rx_len += (size_t)r;
            st->rx_buf[st->rx_len] = '\0';
            char *nl;
            while ((nl = strchr(st->rx_buf, '\n')) != NULL) {
                *nl = '\0';
                size_t line_len = (size_t)(nl - st->rx_buf);
                char line_copy[sizeof(st->rx_buf)];
                memcpy(line_copy, st->rx_buf, line_len + 1);
                size_t consumed = line_len + 1;
                memmove(st->rx_buf, st->rx_buf + consumed, st->rx_len - consumed + 1);
                st->rx_len -= consumed;
                handle_ipc_json_line(st, line_copy);
                if (st->ipc_fd < 0) return;
            }
        } else if (r == 0) {
            if (st->ipc_busy &&
                (strcmp(st->pending_op, "screen_frame") == 0 ||
                 strcmp(st->pending_op, "gui_input") == 0)) {
                st->mirror_active = 0;
            }
            ipc_abort_conn(st);
            return;
        } else {
            if (errno == EAGAIN || errno == EWOULDBLOCK) return;
            ipc_abort_conn(st);
            set_banner(st, "IPC read error", COL_RED);
            return;
        }
    }
}

/* --- Framebuffer Rendering (`320x170` RGB565) --- */

static void draw_tabs(int active_tab)
{
    static const char *names[TAB_COUNT] = { "LINK", "REMOTE", "CAPS", "STUDIO", "JOBS", "KALI" };
    int tw = 49;
    for (int i = 0; i < TAB_COUNT; i++) {
        int x = 4 + i * (tw + 3);
        int act = (i == active_tab);
        fill_rect(x, 17, tw, 12, act ? COL_TAB_ACT : COL_TAB_IDLE);
        draw_rect(x, 17, tw, 12, act ? COL_CYAN : COL_BORDER);
        int tx = x + (tw - (int)strlen(names[i]) * 6) / 2;
        draw_str(tx, 20, names[i], act ? COL_WHITE : COL_DIM);
    }
}

static void draw_flipper_mirror(const struct app_state *st)
{
    uint16_t fl_bg  = rgb565(255, 130, 0);
    uint16_t fl_ink = rgb565(20, 10, 0);
    int ox = 32;
    int oy = 20;

    fill_rect(ox - 2, oy - 2, 260, 132, fl_ink);
    draw_rect(ox - 2, oy - 2, 260, 132, COL_ORANGE);

    if (!st->has_screen_fb) {
        fill_rect(ox, oy, 256, 128, fl_bg);
        draw_str(76, 78, "Waiting for BLE ScreenFrame...", fl_ink);
        return;
    }

    for (int fy = 0; fy < 64; fy++) {
        int sy = (st->screen_orient == 1) ? (63 - fy) : fy;
        const uint8_t *page_row = &st->screen_fb[(sy >> 3) * 128];
        uint8_t bit_mask = (uint8_t)(1U << (sy & 7));
        uint16_t *dst0 = &g_buf[(oy + fy * 2) * LCD_W + ox];
        uint16_t *dst1 = &g_buf[(oy + fy * 2 + 1) * LCD_W + ox];
        for (int fx = 0; fx < 128; fx++) {
            int sx = (st->screen_orient == 1) ? (127 - fx) : fx;
            uint16_t c = (page_row[sx] & bit_mask) ? fl_ink : fl_bg;
            dst0[fx * 2]     = c;
            dst0[fx * 2 + 1] = c;
            dst1[fx * 2]     = c;
            dst1[fx * 2 + 1] = c;
        }
    }
}

static void draw_qr_modal(const struct app_state *st)
{
    fill_rect(6, 17, 308, 134, rgb565(14, 22, 38));
    draw_rect(6, 17, 308, 134, COL_CYAN);

    int qsz = st->qr_size;
    int scale = (qsz > 0) ? (126 / qsz) : 2;
    if (scale < 1) scale = 1;
    if (scale > 4) scale = 4;
    int box_px = qsz * scale;
    if (box_px > 128) box_px = 128;
    int qx = 14 + (128 - box_px) / 2;
    int qy = 20 + (128 - box_px) / 2;

    fill_rect(qx - 3, qy - 3, box_px + 6, box_px + 6, COL_WHITE);
    for (int ry = 0; ry < qsz && (ry * scale) < 128; ry++) {
        for (int rx = 0; rx < qsz && (rx * scale) < 128; rx++) {
            if (st->qr_bits[ry * qsz + rx]) {
                fill_rect(qx + rx * scale, qy + ry * scale, scale, scale, rgb565(0, 0, 0));
            }
        }
    }

    draw_str(152, 23, "KALI QRENCODE", COL_CYAN);
    char dim[32];
    snprintf(dim, sizeof(dim), "Matrix: %dx%d", qsz, qsz);
    draw_str(152, 35, dim, COL_DIM);

    /* Wrap QR payload across up to 5 lines (25 cols each) on right panel */
    const char *p = st->qr_payload;
    for (int row = 0; row < 5 && *p; row++) {
        char chunk[26];
        int c = 0;
        while (*p && c < 25) {
            chunk[c++] = (*p == '\n') ? ' ' : *p;
            p++;
        }
        chunk[c] = '\0';
        draw_str(152, 51 + row * 11, chunk, COL_WHITE);
    }

    draw_str(152, 118, "[6] Push & Open NFC Tag", COL_YELLOW);
    draw_str(152, 130, "[4 / ESC] Close Modal", COL_DIM);
    cz_draw_softkeys("CLOSE", "", "PUSH-NFC", "", "EXIT", 0);
}

static void draw_ir_modal(const struct app_state *st)
{
    fill_rect(6, 17, 308, 121, rgb565(16, 22, 40));
    draw_rect(6, 17, 308, 121, COL_ORANGE);

    const char *base = strrchr(st->ir_src, '/');
    base = base ? (base + 1) : st->ir_src;
    char hdr[96];
    if (st->ir_remote_count > 1) {
        snprintf(hdr, sizeof(hdr), "IR[%d/%d] %-16.16s <,/.> Flt:[%s_]",
                 st->ir_remote_idx + 1, st->ir_remote_count,
                 base[0] ? base : "z2z_learned.ir", st->ir_filter);
    } else {
        snprintf(hdr, sizeof(hdr), "IR DECK: %-18.18s Filter:[%s_]",
                 base[0] ? base : "z2z_learned.ir", st->ir_filter);
    }
    draw_str_clip(12, 21, hdr, 49, COL_YELLOW);
    fill_rect(10, 31, 300, 1, COL_BORDER);

    int fidx[MAX_IR_BTNS];
    int fcnt = ir_filtered_indices(st, fidx);
    if (fcnt == 0) {
        draw_str(14, 48, "No matching buttons in .ir file.", COL_DIM);
        draw_str(14, 62, "Point remote at Flipper, type name,", COL_TEXT);
        draw_str(14, 74, "then press [5:SAVE-RX] to record!", COL_CYAN);
    } else {
        int cur = st->ir_sel;
        if (cur >= fcnt) cur = fcnt - 1;
        if (cur < 0) cur = 0;
        int start = (cur >= 7) ? (cur - 6) : 0;
        for (int row = 0; row < 7 && (start + row) < fcnt; row++) {
            int bi = fidx[start + row];
            int y = 35 + row * 12;
            int sel = ((start + row) == cur);
            if (sel) fill_rect(10, y - 1, 300, 10, COL_SEL_BG);
            char rline[64];
            snprintf(rline, sizeof(rline), "%-18.18s  %-27.27s",
                     st->ir_btns[bi].name, st->ir_btns[bi].summary);
            draw_str_clip(12, y, rline, 48, sel ? COL_WHITE : COL_TEXT);
        }
    }

    fill_rect(4, 140, 312, 10, COL_PANEL_ALT);
    draw_str_clip(8, 141, st->status_banner, 50, st->banner_col);
    cz_draw_softkeys("CLOSE", "SAVE-RX", "FIRE-IR", "CLR-FLT", "EXIT", 2);
}

static void render_ui(const struct app_state *st)
{
    fill_rect(0, 0, LCD_W, LCD_H, COL_BG);
    char top_badge[20];
    if (st->mirror_active) {
        int qtot = st->input_queue_depth + st->fkey_count;
        if (qtot > 9) qtot = 9;
        if (qtot > 0) snprintf(top_badge, sizeof(top_badge), "MIRROR Q:%d", qtot);
        else snprintf(top_badge, sizeof(top_badge), "MIRROR");
    } else {
        snprintf(top_badge, sizeof(top_badge), "%s",
                 st->connected ? "BLE-OK" : (st->watch_enabled ? "WATCH" : "IDLE"));
    }
    cz_draw_topbar("Zero2Zero", top_badge);

    /* Full-height 2x (256x128) Flipper live display when mirror is active on REMOTE tab */
    if (st->tab == TAB_REMOTE && st->mirror_active && !st->pin_modal) {
        draw_flipper_mirror(st);
        cz_draw_softkeys("STOP", "OK", "BACK", "LONG-OK", "EXIT", 0);
        return;
    }

    if (st->qr_modal && !st->pin_modal) {
        draw_qr_modal(st);
        return;
    }

    if (st->ir_modal && !st->pin_modal) {
        draw_ir_modal(st);
        return;
    }

    draw_tabs(st->tab);

    /* Main content panel (y = 31..138) */
    fill_rect(4, 31, 312, 107, COL_PANEL);
    draw_rect(4, 31, 312, 107, COL_BORDER);

    if (st->tab == TAB_LINK) {
        char ln[64];
        snprintf(ln, sizeof(ln), "State: %s   Watch: %s",
                 st->connected ? "CONNECTED" : "DISCONNECTED",
                 st->watch_enabled ? "ON (Pocket)" : "OFF");
        draw_str(10, 36, ln, st->connected ? COL_GREEN : COL_TEXT);

        if (st->link_addr[0]) {
            snprintf(ln, sizeof(ln), "Peer : %.14s (%s) %ddBm",
                     st->link_name[0] ? st->link_name : st->link_addr,
                     st->link_shell[0] ? st->link_shell : "BLE",
                     st->link_rssi);
            draw_str_clip(10, 46, ln, 50, COL_CYAN);
        }
        if (st->flipper_bat >= 0 || st->flipper_free_mb > 0) {
            snprintf(ln, sizeof(ln), "Flip : Bat %d%%   /ext Free %d MiB",
                     st->flipper_bat >= 0 ? st->flipper_bat : 0, st->flipper_free_mb);
            draw_str(10, 56, ln, COL_DIM);
        }

        fill_rect(8, 67, 304, 1, COL_BORDER);
        draw_str(10, 71, "Flippers (Up/Dn select, BS=Unpair):", COL_DIM);
        if (st->dev_count == 0) {
            draw_str(14, 86, "Press [4:SCAN] to find Flipper BLE", COL_YELLOW);
        } else {
            for (int i = 0; i < st->dev_count && i < 4; i++) {
                int y = 82 + i * 13;
                int sel = (i == st->dev_sel);
                if (sel) fill_rect(8, y - 2, 304, 11, COL_SEL_BG);
                snprintf(ln, sizeof(ln), "%s %-13.13s %4d %s",
                         st->devs[i].paired ? "*" : " ",
                         st->devs[i].name,
                         st->devs[i].rssi,
                         st->devs[i].address);
                draw_str_clip(10, y, ln, 49, sel ? COL_WHITE : COL_TEXT);
            }
        }
    } else if (st->tab == TAB_REMOTE) {
        draw_str(10, 36, "Flipper Live Screen & Remote Control:", COL_CYAN);
        fill_rect(8, 46, 304, 1, COL_BORDER);
        draw_str(10, 52, "[4:MIRROR] Stream 128x64 @ 2x (256x128)", COL_TEXT);
        draw_str(10, 64, "  Arrows / ; / : D-Pad    Enter : OK", COL_DIM);
        draw_str(10, 75, "  Backspace    : Back     Space : Long OK", COL_DIM);
        draw_str(10, 89, "[5:CLOCK]  Sync Kali NTP -> Flipper RTC", COL_TEXT);
        char aln[56];
        snprintf(aln, sizeof(aln), "[6:ALERT]  Flipper Buzzer Mode: %s",
                 st->alert_mode[0] ? st->alert_mode : "vibro");
        draw_str_clip(10, 101, aln, 49, COL_YELLOW);
        draw_str(10, 113, "[7:FAP]    Launch zero2zero.fap on Flipper", COL_TEXT);
    } else if (st->tab == TAB_CAP) {
        if (!st->cap_view_detail) {
            draw_str_clip(10, 35, "Captures [D:Diff I:IR Q:QR C:Cam]:", 50, COL_CYAN);
            fill_rect(8, 45, 304, 1, COL_BORDER);
            if (st->cap_count == 0) {
                draw_str(12, 58, "Press [4:LIST] to scan Flipper /ext", COL_YELLOW);
                draw_str(12, 70, "[D]=Diff 2 caps  [I]=IR Deck  [C]=IMX219", COL_DIM);
            } else {
                int start = (st->cap_sel >= 7) ? (st->cap_sel - 6) : 0;
                for (int row = 0; row < 7 && (start + row) < st->cap_count; row++) {
                    int idx = start + row;
                    int y = 49 + row * 12;
                    int sel = (idx == st->cap_sel);
                    int marked = (st->diff_mark_path[0] &&
                                  strcmp(st->diff_mark_path, st->caps[idx].path) == 0);
                    if (sel) fill_rect(8, y - 1, 304, 10, COL_SEL_BG);
                    char line_buf[64];
                    snprintf(line_buf, sizeof(line_buf), "%c[%-6.6s] %-25.25s %5dB",
                             marked ? '*' : ' ',
                             st->caps[idx].kind, st->caps[idx].name, st->caps[idx].size);
                    draw_str_clip(10, y, line_buf, 49, marked ? COL_YELLOW : (sel ? COL_WHITE : COL_TEXT));
                }
            }
        } else {
            draw_str(10, 35, "Capture Inspector (Up/Dn, Q=QR):", COL_CYAN);
            fill_rect(8, 45, 304, 1, COL_BORDER);
            for (int i = 0; i < 8; i++) {
                int idx = st->log_scroll + i;
                if (idx < st->log_count)
                    draw_str_clip(10, 49 + i * 10, st->log_lines[idx], 49, COL_TEXT);
            }
        }
    } else if (st->tab == TAB_STUDIO) {
        char hdr[64];
        snprintf(hdr, sizeof(hdr), "Studio [%s] %s",
                 st->studio_raw_mode ? "RAW" : "LINE",
                 st->studio_edit_active ? "(TYPING)" : "Q:QR N:NFC R:RFID B:iBtn C:Cam");
        draw_str_clip(10, 35, hdr, 50, st->studio_edit_active ? COL_YELLOW : COL_CYAN);
        fill_rect(8, 45, 304, 1, COL_BORDER);

        char preview[STUDIO_MAX + 4];
        snprintf(preview, sizeof(preview), "%s%s",
                 st->studio_buf, st->studio_edit_active ? "_" : "");
        char ed_lines[MAX_LOG_LINES][LOG_LINE_LEN];
        int n_ed = split_lines(preview, ed_lines);
        int start = (n_ed > 6) ? (n_ed - 6) : 0;
        if (n_ed == 0) {
            draw_str(12, 56, "Press [4:EDIT] to type on QWERTY,", COL_DIM);
            draw_str(12, 68, "or [5:PRESET] for {{HOST_IP}} templates.", COL_DIM);
        } else {
            for (int i = 0; i < 6 && (start + i) < n_ed; i++) {
                draw_str_clip(10, 49 + i * 11, ed_lines[start + i], 49, COL_TEXT);
            }
        }
        draw_str(10, 118, "Vars: {{HOST_IP}} {{GATEWAY}} {{HOSTNAME}}", COL_DIM);
    } else if (st->tab == TAB_JOBS) {
        draw_str(10, 35, "Offload Hooks (/etc/zero2zero/jobs.d):", COL_CYAN);
        if (st->job_count == 0) {
            draw_str(12, 48, "Press [5:RELOAD] to list jobs", COL_DIM);
        } else {
            int start = (st->job_sel >= 3) ? (st->job_sel - 2) : 0;
            for (int row = 0; row < 3 && (start + row) < st->job_count; row++) {
                int i = start + row;
                int y = 46 + row * 11;
                int sel = (i == st->job_sel);
                if (sel) fill_rect(8, y - 1, 304, 10, COL_SEL_BG);
                char row_txt[60];
                snprintf(row_txt, sizeof(row_txt), "%-14.14s %.32s", st->jobs[i].id, st->jobs[i].title);
                draw_str_clip(10, y, row_txt, 49, sel ? COL_WHITE : COL_TEXT);
            }
        }
        fill_rect(8, 81, 304, 1, COL_BORDER);
        for (int i = 0; i < 5; i++) {
            int idx = st->log_scroll + i;
            if (idx < st->log_count)
                draw_str_clip(10, 85 + i * 10, st->log_lines[idx], 49, COL_GREEN);
        }
    } else if (st->tab == TAB_NET) {
        draw_str(10, 35, "Kali CM0 Host & Wireless Summary:", COL_CYAN);
        fill_rect(8, 45, 304, 1, COL_BORDER);
        if (st->log_count == 0) {
            draw_str(12, 60, "Press [4:REFRESH] for Kali net summary", COL_DIM);
        } else {
            for (int i = 0; i < 8; i++) {
                int idx = st->log_scroll + i;
                if (idx < st->log_count)
                    draw_str_clip(10, 49 + i * 10, st->log_lines[idx], 49, COL_TEXT);
            }
        }
    }

    /* Status banner strip (expands to 2 lines when message > 50 chars) */
    if (strlen(st->status_banner) > 50) {
        fill_rect(4, 129, 312, 21, COL_PANEL_ALT);
        draw_rect(4, 129, 312, 21, st->banner_col);
        draw_str_clip(8, 131, st->status_banner, 50, st->banner_col);
        draw_str_clip(8, 141, st->status_banner + 50, 50, st->banner_col);
    } else {
        fill_rect(4, 140, 312, 10, COL_PANEL_ALT);
        draw_str_clip(8, 141, st->status_banner, 50, st->banner_col);
    }

    /* Dedicated 6-digit Passkey Modal overrides softkeys 4..8 */
    if (st->pin_modal) {
        fill_rect(28, 42, 264, 84, rgb565(18, 28, 52));
        draw_rect(28, 42, 264, 84, COL_YELLOW);
        draw_str(38, 49, "BLE PASSKEY PAIRING", COL_YELLOW);
        char sub[48];
        snprintf(sub, sizeof(sub), "Flipper: %.24s",
                 st->pin_dev_name[0] ? st->pin_dev_name : st->pin_dev_addr);
        draw_str_clip(38, 61, sub, 38, COL_TEXT);
        draw_str(38, 73, "Type 6 digits shown on Flipper:", COL_DIM);

        fill_rect(82, 86, 156, 18, COL_BG);
        draw_rect(82, 86, 156, 18, COL_CYAN);
        char pbox[24];
        snprintf(pbox, sizeof(pbox), "[ %-6s ]", st->pin_digits);
        draw_str(126, 91, pbox, COL_WHITE);
        draw_str(44, 111, "0-9:Digit  BS:Del  ENT:Pair  ESC:Abort", COL_CYAN);

        cz_draw_softkeys("0-9", "PIN", "BS", "ENTER", "ABORT", 3);
        return;
    }

    if (st->tab == TAB_STUDIO && st->studio_edit_active) {
        cz_draw_softkeys("A-Z/0-9", "SPACE", "BS:DEL", "ENT:NL", "ESC:DONE", 4);
        return;
    }

    if (st->tab == TAB_LINK) {
        int sel_paired = (st->dev_count > 0 && st->dev_sel < st->dev_count && st->devs[st->dev_sel].paired);
        cz_draw_softkeys("SCAN", sel_paired ? "REPAIR" : "PAIR",
                         st->connected ? "DISC" : "CONN", "WATCH", "EXIT", -1);
    } else if (st->tab == TAB_REMOTE) {
        cz_draw_softkeys("MIRROR", "CLOCK", "ALERT", "FAP", "EXIT", -1);
    } else if (st->tab == TAB_CAP) {
        cz_draw_softkeys("LIST", "INSPECT", "OPEN", "VAULT", "EXIT", -1);
    } else if (st->tab == TAB_STUDIO) {
        cz_draw_softkeys("EDIT", "PRESET", st->studio_raw_mode ? "AUTO" : "RAW", "PUSH+GO", "EXIT", -1);
    } else if (st->tab == TAB_JOBS) {
        cz_draw_softkeys("RUN", "RELOAD", "FAP", "VAULT", "EXIT", -1);
    } else {
        cz_draw_softkeys("REFRESH", "NOTE", "STATUS", "WATCH", "EXIT", -1);
    }
}

static int keycode_to_digit(int code)
{
    if (code >= KEY_1 && code <= KEY_9) return '1' + (code - KEY_1);
    if (code == KEY_0) return '0';
    switch (code) {
        case KEY_KP0: return '0';
        case KEY_KP1: return '1';
        case KEY_KP2: return '2';
        case KEY_KP3: return '3';
        case KEY_KP4: return '4';
        case KEY_KP5: return '5';
        case KEY_KP6: return '6';
        case KEY_KP7: return '7';
        case KEY_KP8: return '8';
        case KEY_KP9: return '9';
        default: return -1;
    }
}

static int evdev_to_ascii(int code, int shift)
{
    int dg = keycode_to_digit(code);
    if (dg >= 0) {
        if (!shift) return dg;
        static const char sh_num[10] = { ')', '!', '@', '#', '$', '%', '^', '&', '*', '(' };
        return sh_num[dg - '0'];
    }
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
        case KEY_SPACE:      return ' ';
        case KEY_MINUS:      return shift ? '_' : '-';
        case KEY_EQUAL:      return shift ? '+' : '=';
        case KEY_LEFTBRACE:  return shift ? '{' : '[';
        case KEY_RIGHTBRACE: return shift ? '}' : ']';
        case KEY_BACKSLASH:  return shift ? '|' : '\\';
        case KEY_SEMICOLON:  return shift ? ':' : ';';
        case KEY_APOSTROPHE: return shift ? '"' : '\'';
        case KEY_COMMA:      return shift ? '<' : ',';
        case KEY_DOT:        return shift ? '>' : '.';
        case KEY_SLASH:      return shift ? '?' : '/';
        default: return -1;
    }
}

static void cycle_alert_mode(struct app_state *st)
{
    static const char *modes[5] = { "vibro", "soft", "led", "off", "loud" };
    int next_idx = 0;
    for (int i = 0; i < 5; i++) {
        if (strcmp(st->alert_mode, modes[i]) == 0) {
            next_idx = (i + 1) % 5;
            break;
        }
    }
    char cmd[96];
    snprintf(cmd, sizeof(cmd), "{\"op\":\"alert_mode\",\"mode\":\"%s\"}\n", modes[next_idx]);
    ipc_send_request(st, "alert", cmd);
}

static void stop_mirror_mode(struct app_state *st, const char *banner_msg, uint16_t col)
{
    st->mirror_active = 0;
    clear_fkey_ring(st);
    if (st->ipc_busy &&
        (strcmp(st->pending_op, "screen_frame") == 0 ||
         strcmp(st->pending_op, "gui_input") == 0 ||
         strcmp(st->pending_op, "screen_start") == 0)) {
        /* Drop in-flight mirror request socket so [4:STOP] never waits on BLE timeouts */
        ipc_abort_conn(st);
    }
    if (!st->ipc_busy) {
        st->want_screen_stop = 0;
        ipc_send_request(st, "screen_stop", "{\"op\":\"screen_stop\"}\n");
    } else {
        st->want_screen_stop = 1;
    }
    if (banner_msg) {
        set_banner(st, banner_msg, col);
    }
}

static void send_flipper_key_ex(struct app_state *st, const char *fkey, const char *mode, int coalesce_repeat)
{
    if (st->ipc_busy) {
        if (coalesce_repeat && (st->fkey_count > 0 || st->input_queue_depth > 0)) {
            return;
        }
        if (st->fkey_count < FKEY_RING_CAP) {
            int tail = (st->fkey_head + st->fkey_count) % FKEY_RING_CAP;
            snprintf(st->fkey_ring[tail].key, sizeof(st->fkey_ring[tail].key), "%s", fkey);
            snprintf(st->fkey_ring[tail].mode, sizeof(st->fkey_ring[tail].mode), "%s", mode);
            st->fkey_ring[tail].coalesce_repeat = coalesce_repeat;
            st->fkey_count++;
        }
        return;
    }
    char cmd[192];
    snprintf(cmd, sizeof(cmd),
             "{\"op\":\"gui_input\",\"key\":\"%s\",\"mode\":\"%s\",\"async\":true,\"coalesce_repeat\":%s}\n",
             fkey, mode, coalesce_repeat ? "true" : "false");
    ipc_send_request(st, "gui_input", cmd);
}

static void send_flipper_key(struct app_state *st, const char *fkey, const char *mode)
{
    send_flipper_key_ex(st, fkey, mode, 0);
}

static void handle_key_repeat(struct app_state *st, int code)
{
    if (st->tab != TAB_REMOTE || !st->mirror_active || st->pin_modal) return;
    const char *dir = NULL;
    if (code == KEY_UP || code == KEY_SEMICOLON || code == KEY_W) dir = "up";
    else if (code == KEY_DOWN || code == KEY_SLASH || code == KEY_S) dir = "down";
    else if (code == KEY_LEFT || code == KEY_A) dir = "left";
    else if (code == KEY_RIGHT || code == KEY_D) dir = "right";
    if (!dir) return;

    double now_mono = cz_mono_sec();
    if ((now_mono - st->last_repeat_mono) < 0.14) return;
    if (st->input_queue_depth > 0 || st->fkey_count > 0) return;
    st->last_repeat_mono = now_mono;
    send_flipper_key_ex(st, dir, "short", 1);
}

static void submit_pin_reply(struct app_state *st, const char *pin_str)
{
    if (st->ipc_fd < 0) {
        st->pin_modal = 0;
        return;
    }
    char msg[64];
    snprintf(msg, sizeof(msg), "{\"pin\":\"%s\"}\n", pin_str);
    ssize_t wr = write(st->ipc_fd, msg, strlen(msg));
    (void)wr;
    st->pin_modal = 0;
    set_banner(st, "Verifying BLE passkey...", COL_CYAN);
}

static void handle_key_press(struct app_state *st, int code)
{
    /* When PIN modal is active, number keys 0-9 enter PIN digits! */
    if (st->pin_modal) {
        int dg = keycode_to_digit(code);
        if (dg >= 0 && st->pin_len < 6) {
            st->pin_digits[st->pin_len++] = (char)dg;
            st->pin_digits[st->pin_len] = '\0';
            return;
        }
        if ((code == KEY_BACKSPACE || code == KEY_DELETE) && st->pin_len > 0) {
            st->pin_digits[--st->pin_len] = '\0';
            return;
        }
        if (code == KEY_ENTER || code == KEY_KPENTER) {
            if (st->pin_len < 6) {
                set_banner(st, "Enter all 6 digits from Flipper LCD", COL_YELLOW);
                return;
            }
            submit_pin_reply(st, st->pin_digits);
            return;
        }
        if (code == KEY_ESC || code == KEY_GRAVE) {
            submit_pin_reply(st, "");
            return;
        }
        return;
    }

    /* Kali `qrencode` modal key handling */
    if (st->qr_modal) {
        if (code == KEY_8) {
            g_quit = 1;
            return;
        }
        if (code == KEY_4 || code == KEY_ESC || code == KEY_GRAVE || code == KEY_Q ||
            code == KEY_BACKSPACE) {
            st->qr_modal = 0;
            return;
        }
        if (code == KEY_6 && st->qr_payload[0]) {
            char escaped[400];
            char qcmd[640];
            json_escape_str(st->qr_payload, escaped, sizeof(escaped));
            snprintf(qcmd, sizeof(qcmd),
                     "{\"op\":\"nfc_make\",\"payload\":\"%s\",\"remote\":\"/ext/nfc/z2z_ndef.nfc\","
                     "\"open_on_flipper\":true}\n",
                     escaped);
            st->qr_modal = 0;
            ipc_send_request(st, "nfc_make", qcmd);
        }
        return;
    }

    /* QWERTY High-Power IR Remote Deck modal key handling */
    if (st->ir_modal) {
        int fidx[MAX_IR_BTNS];
        int fcnt = ir_filtered_indices(st, fidx);
        if (st->ir_sel >= fcnt) st->ir_sel = (fcnt > 0) ? (fcnt - 1) : 0;

        if (code == KEY_8) {
            g_quit = 1;
            return;
        }
        if (code == KEY_4 || code == KEY_ESC || code == KEY_GRAVE) {
            st->ir_modal = 0;
            return;
        }
        if (code == KEY_7) {
            st->ir_filter[0] = '\0';
            st->ir_filter_len = 0;
            st->ir_sel = 0;
            return;
        }
        if (code == KEY_UP || code == KEY_SEMICOLON) {
            if (st->ir_sel > 0) st->ir_sel--;
            return;
        }
        if (code == KEY_DOWN || code == KEY_SLASH) {
            if (st->ir_sel + 1 < fcnt) st->ir_sel++;
            return;
        }
        if ((code == KEY_LEFT || code == KEY_COMMA) && st->ir_remote_count > 1) {
            st->ir_remote_idx = (st->ir_remote_idx + st->ir_remote_count - 1) % st->ir_remote_count;
            char lcmd[256];
            snprintf(lcmd, sizeof(lcmd), "{\"op\":\"ir_list\",\"src\":\"%s\"}\n",
                     st->ir_remotes[st->ir_remote_idx]);
            ipc_send_request(st, "ir_list", lcmd);
            return;
        }
        if ((code == KEY_RIGHT || code == KEY_DOT || code == KEY_TAB) && st->ir_remote_count > 1) {
            st->ir_remote_idx = (st->ir_remote_idx + 1) % st->ir_remote_count;
            char rcmd[256];
            snprintf(rcmd, sizeof(rcmd), "{\"op\":\"ir_list\",\"src\":\"%s\"}\n",
                     st->ir_remotes[st->ir_remote_idx]);
            ipc_send_request(st, "ir_list", rcmd);
            return;
        }
        if (code == KEY_6 || code == KEY_ENTER || code == KEY_KPENTER) {
            if (fcnt > 0) {
                int bi = fidx[st->ir_sel];
                char icmd[320];
                snprintf(icmd, sizeof(icmd),
                         "{\"op\":\"ir_tx\",\"src\":\"%s\",\"button\":\"%d\"}\n",
                         st->ir_src[0] ? st->ir_src : "/ext/infrared/z2z_learned.ir",
                         st->ir_btns[bi].index);
                ipc_send_request(st, "ir_tx", icmd);
            } else {
                set_banner(st, "No button selected — [,/.] switch remote or [5] Learn", COL_YELLOW);
            }
            return;
        }
        if (code == KEY_5) {
            const char *save_nm = st->ir_filter[0] ? st->ir_filter : "Learned";
            const char *dst_ir = st->ir_src[0] ? st->ir_src : "/ext/infrared/z2z_learned.ir";
            char esc_nm[64];
            char scmd[360];
            json_escape_str(save_nm, esc_nm, sizeof(esc_nm));
            snprintf(scmd, sizeof(scmd),
                     "{\"op\":\"ir_save\",\"name\":\"%s\",\"dst\":\"%s\",\"arm_if_empty\":true}\n",
                     esc_nm, dst_ir);
            ipc_send_request(st, "ir_save", scmd);
            return;
        }
        if (code == KEY_BACKSPACE || code == KEY_DELETE) {
            if (st->ir_filter_len > 0) {
                st->ir_filter[--st->ir_filter_len] = '\0';
                st->ir_sel = 0;
            } else {
                st->ir_modal = 0;
            }
            return;
        }
        int ch = evdev_to_ascii(code, st->shift_held);
        if (ch >= 32 && st->ir_filter_len + 1 < (int)sizeof(st->ir_filter)) {
            st->ir_filter[st->ir_filter_len++] = (char)ch;
            st->ir_filter[st->ir_filter_len] = '\0';
            st->ir_sel = 0;
        }
        return;
    }

    /* Full 46-key QWERTY typing mode inside STUDIO tab */
    if (st->tab == TAB_STUDIO && st->studio_edit_active) {
        if (code == KEY_ESC || code == KEY_GRAVE) {
            st->studio_edit_active = 0;
            set_banner(st, "Exited typing mode ([7:PUSH+GO] to upload)", COL_CYAN);
            return;
        }
        if (code == KEY_BACKSPACE || code == KEY_DELETE) {
            if (st->studio_len > 0) st->studio_buf[--st->studio_len] = '\0';
            return;
        }
        if ((code == KEY_ENTER || code == KEY_KPENTER) && st->studio_len + 1 < STUDIO_MAX) {
            st->studio_buf[st->studio_len++] = '\n';
            st->studio_buf[st->studio_len] = '\0';
            return;
        }
        int ch = evdev_to_ascii(code, st->shift_held);
        if (ch >= 32 && st->studio_len + 1 < STUDIO_MAX) {
            st->studio_buf[st->studio_len++] = (char)ch;
            st->studio_buf[st->studio_len] = '\0';
        }
        return;
    }

    /* Live Flipper Screen Mirror & D-Pad Remote Mode */
    if (st->tab == TAB_REMOTE && st->mirror_active) {
        if (code == KEY_8) {
            stop_mirror_mode(st, NULL, COL_CYAN);
            g_quit = 1;
            return;
        }
        if (code == KEY_4 || code == KEY_ESC || code == KEY_GRAVE) {
            stop_mirror_mode(st, "Stopped screen stream", COL_CYAN);
            return;
        }
        if (code == KEY_TAB || code == KEY_DOT) {
            stop_mirror_mode(st, "Stopped screen stream", COL_CYAN);
            st->tab = (st->tab + 1) % TAB_COUNT;
            return;
        }
        if (code == KEY_COMMA) {
            stop_mirror_mode(st, "Stopped screen stream", COL_CYAN);
            st->tab = (st->tab + TAB_COUNT - 1) % TAB_COUNT;
            return;
        }
        if (code == KEY_UP || code == KEY_SEMICOLON || code == KEY_W) {
            send_flipper_key(st, "up", "short");
        } else if (code == KEY_DOWN || code == KEY_SLASH || code == KEY_S) {
            send_flipper_key(st, "down", "short");
        } else if (code == KEY_LEFT || code == KEY_A) {
            send_flipper_key(st, "left", "short");
        } else if (code == KEY_RIGHT || code == KEY_D) {
            send_flipper_key(st, "right", "short");
        } else if (code == KEY_ENTER || code == KEY_KPENTER || code == KEY_5) {
            send_flipper_key(st, "ok", "short");
        } else if (code == KEY_SPACE || code == KEY_7) {
            send_flipper_key(st, "ok", "long");
        } else if (code == KEY_BACKSPACE || code == KEY_DELETE || code == KEY_6) {
            send_flipper_key(st, "back", "short");
        }
        return;
    }

    if (code == KEY_COMMA || code == KEY_LEFT) {
        st->tab = (st->tab + TAB_COUNT - 1) % TAB_COUNT;
        return;
    }
    if (code == KEY_DOT || code == KEY_RIGHT || code == KEY_TAB) {
        st->tab = (st->tab + 1) % TAB_COUNT;
        return;
    }

    if (code == KEY_UP || code == KEY_SEMICOLON) {
        if (st->tab == TAB_LINK && st->dev_sel > 0) st->dev_sel--;
        else if (st->tab == TAB_CAP && !st->cap_view_detail && st->cap_sel > 0) st->cap_sel--;
        else if (st->tab == TAB_JOBS && st->job_sel > 0) st->job_sel--;
        else if (st->log_scroll > 0) st->log_scroll--;
        return;
    }
    if (code == KEY_DOWN || code == KEY_SLASH) {
        if (st->tab == TAB_LINK && st->dev_sel + 1 < st->dev_count) st->dev_sel++;
        else if (st->tab == TAB_CAP && !st->cap_view_detail && st->cap_sel + 1 < st->cap_count) st->cap_sel++;
        else if (st->tab == TAB_JOBS && st->job_sel + 1 < st->job_count) st->job_sel++;
        else if (st->log_scroll + 1 < st->log_count) st->log_scroll++;
        return;
    }

    if (code == KEY_8) {
        g_quit = 1;
        return;
    }

    char cmd[1536];
    const char *sel_target = (st->dev_count > 0) ? st->devs[st->dev_sel].address : "";

    if (st->tab == TAB_LINK) {
        int sel_paired = (st->dev_count > 0 && st->dev_sel < st->dev_count && st->devs[st->dev_sel].paired);
        if (code == KEY_4) {
            ipc_send_request(st, "scan", "{\"op\":\"scan\",\"timeout\":3.5}\n");
        } else if (code == KEY_5) {
            snprintf(cmd, sizeof(cmd), "{\"op\":\"pair\",\"target\":\"%s\",\"force\":%s}\n",
                     sel_target, sel_paired ? "true" : "false");
            ipc_send_request(st, "pair", cmd);
        } else if ((code == KEY_BACKSPACE || code == KEY_DELETE) && sel_target[0]) {
            snprintf(cmd, sizeof(cmd), "{\"op\":\"unpair\",\"target\":\"%s\"}\n", sel_target);
            ipc_send_request(st, "unpair", cmd);
        } else if (code == KEY_6) {
            if (st->connected) {
                st->mirror_active = 0;
                ipc_send_request(st, "disc", "{\"op\":\"disconnect\"}\n");
                st->connected = 0;
            } else {
                snprintf(cmd, sizeof(cmd), "{\"op\":\"connect\",\"target\":\"%s\"}\n", sel_target);
                ipc_send_request(st, "connect", cmd);
            }
        } else if (code == KEY_7) {
            snprintf(cmd, sizeof(cmd), "{\"op\":\"watch_set\",\"enabled\":%s}\n",
                     st->watch_enabled ? "false" : "true");
            ipc_send_request(st, "watch", cmd);
        }
    } else if (st->tab == TAB_REMOTE) {
        if (code == KEY_4) {
            ipc_send_request(st, "screen_start",
                             "{\"op\":\"screen_start\",\"wait\":0.8,\"subscribe\":true}\n");
        } else if (code == KEY_5) {
            ipc_send_request(st, "clock", "{\"op\":\"clock_sync\"}\n");
        } else if (code == KEY_6) {
            cycle_alert_mode(st);
        } else if (code == KEY_7) {
            ipc_send_request(st, "fap", "{\"op\":\"fap_launch\"}\n");
        } else if (code == KEY_I) {
            ipc_send_request(st, "ir_list", "{\"op\":\"ir_list\",\"src\":\"\",\"auto_select\":true}\n");
        }
    } else if (st->tab == TAB_CAP) {
        if (code == KEY_4) {
            if (st->cap_view_detail && st->cap_count > 0) {
                st->cap_view_detail = 0;
                set_banner(st, "Capture list (Up/Dn select, [5]=Inspect, [6]=Open)", COL_CYAN);
            } else {
                ipc_send_request(st, "caps", "{\"op\":\"captures_list\",\"limit\":20}\n");
            }
        } else if (code == KEY_5 || code == KEY_ENTER || code == KEY_KPENTER) {
            if (st->cap_count > 0 && st->cap_sel < st->cap_count) {
                snprintf(cmd, sizeof(cmd), "{\"op\":\"capture_inspect\",\"remote\":\"%s\"}\n",
                         st->caps[st->cap_sel].path);
                ipc_send_request(st, "cap_inspect", cmd);
            } else {
                ipc_send_request(st, "cap_inspect", "{\"op\":\"capture_inspect\"}\n");
            }
        } else if (code == KEY_6) {
            if (st->cap_count > 0 && st->cap_sel < st->cap_count) {
                snprintf(cmd, sizeof(cmd), "{\"op\":\"capture_open\",\"remote\":\"%s\"}\n",
                         st->caps[st->cap_sel].path);
                ipc_send_request(st, "cap_open", cmd);
            } else {
                set_banner(st, "Press [4:LIST] first to pick a capture", COL_YELLOW);
            }
        } else if (code == KEY_7) {
            ipc_send_request(st, "vault", "{\"op\":\"vault_sync\"}\n");
        } else if (code == KEY_Q) {
            if (st->cap_view_detail && st->last_cap_qr[0]) {
                char escaped[400];
                json_escape_str(st->last_cap_qr, escaped, sizeof(escaped));
                snprintf(cmd, sizeof(cmd), "{\"op\":\"qr_encode\",\"text\":\"%s\"}\n", escaped);
                ipc_send_request(st, "qr_encode", cmd);
            } else if (st->cap_count > 0 && st->cap_sel < st->cap_count) {
                snprintf(cmd, sizeof(cmd), "{\"op\":\"qr_encode\",\"remote\":\"%s\"}\n",
                         st->caps[st->cap_sel].path);
                ipc_send_request(st, "qr_encode", cmd);
            } else {
                set_banner(st, "Select or inspect an .nfc capture first", COL_YELLOW);
            }
        } else if (code == KEY_D) {
            if (st->cap_count > 0 && st->cap_sel < st->cap_count) {
                const char *cur_p = st->caps[st->cap_sel].path;
                if (!st->diff_mark_path[0]) {
                    snprintf(st->diff_mark_path, sizeof(st->diff_mark_path), "%s", cur_p);
                    snprintf(st->diff_mark_name, sizeof(st->diff_mark_name), "%s",
                             st->caps[st->cap_sel].name);
                    char msg[96];
                    snprintf(msg, sizeof(msg), "Marked %.24s — pick 2nd & press [D]",
                             st->diff_mark_name);
                    set_banner(st, msg, COL_YELLOW);
                } else if (strcmp(st->diff_mark_path, cur_p) == 0) {
                    st->diff_mark_path[0] = '\0';
                    set_banner(st, "Cleared diff mark", COL_CYAN);
                } else {
                    snprintf(cmd, sizeof(cmd),
                             "{\"op\":\"capture_diff\",\"src_a\":\"%s\",\"src_b\":\"%s\"}\n",
                             st->diff_mark_path, cur_p);
                    st->diff_mark_path[0] = '\0';
                    ipc_send_request(st, "capture_diff", cmd);
                }
            } else {
                set_banner(st, "Press [4:LIST] first, then [D] on 2 captures", COL_YELLOW);
            }
        } else if (code == KEY_I) {
            if (st->cap_count > 0 && st->cap_sel < st->cap_count &&
                strcmp(st->caps[st->cap_sel].kind, "infrared") == 0) {
                snprintf(cmd, sizeof(cmd), "{\"op\":\"ir_list\",\"src\":\"%s\"}\n",
                         st->caps[st->cap_sel].path);
                ipc_send_request(st, "ir_list", cmd);
            } else {
                ipc_send_request(st, "ir_list",
                                 "{\"op\":\"ir_list\",\"src\":\"\",\"auto_select\":true}\n");
            }
        } else if (code == KEY_C) {
            ipc_send_request(st, "cam_scan",
                             "{\"op\":\"cam_scan\",\"target\":\"auto\",\"open_on_flipper\":true}\n");
        }
    } else if (st->tab == TAB_STUDIO) {
        if (code == KEY_4 || code == KEY_ENTER || code == KEY_KPENTER) {
            st->studio_edit_active = 1;
            set_banner(st, "QWERTY Typing Active (press ESC when done)", COL_YELLOW);
        } else if (code == KEY_5) {
            apply_studio_preset(st, st->studio_tpl_idx + 1);
            set_banner(st, "Loaded starter template ([4:EDIT] to customize)", COL_CYAN);
        } else if (code == KEY_6) {
            st->studio_raw_mode = !st->studio_raw_mode;
            set_banner(st, st->studio_raw_mode ? "Mode: RAW script lines" : "Mode: Auto-type lines", COL_CYAN);
        } else if (code == KEY_7) {
            if (st->studio_len == 0) {
                set_banner(st, "Buffer empty — press [4:EDIT] or [5:PRESET]", COL_YELLOW);
            } else {
                char escaped[STUDIO_MAX * 2];
                json_escape_str(st->studio_buf, escaped, sizeof(escaped));
                snprintf(cmd, sizeof(cmd),
                         "{\"op\":\"badusb_push\",\"name\":\"z2z_studio\",\"content\":\"%s\","
                         "\"raw_ducky\":%s,\"template\":true,\"open_on_flipper\":true}\n",
                         escaped, st->studio_raw_mode ? "true" : "false");
                ipc_send_request(st, "studio_push", cmd);
            }
        } else if (code == KEY_BACKSPACE || code == KEY_DELETE) {
            st->studio_buf[0] = '\0';
            st->studio_len = 0;
            set_banner(st, "Cleared script buffer", COL_CYAN);
        } else if (code == KEY_Q && st->studio_len > 0) {
            char escaped[STUDIO_MAX * 2];
            json_escape_str(st->studio_buf, escaped, sizeof(escaped));
            snprintf(cmd, sizeof(cmd), "{\"op\":\"qr_encode\",\"text\":\"%s\"}\n", escaped);
            ipc_send_request(st, "qr_encode", cmd);
        } else if (code == KEY_N && st->studio_len > 0) {
            char escaped[STUDIO_MAX * 2];
            json_escape_str(st->studio_buf, escaped, sizeof(escaped));
            snprintf(cmd, sizeof(cmd),
                     "{\"op\":\"nfc_make\",\"payload\":\"%s\",\"remote\":\"/ext/nfc/z2z_studio.nfc\","
                     "\"open_on_flipper\":true}\n",
                     escaped);
            ipc_send_request(st, "nfc_make", cmd);
        } else if (code == KEY_R && st->studio_len > 0) {
            char escaped[STUDIO_MAX * 2];
            json_escape_str(st->studio_buf, escaped, sizeof(escaped));
            snprintf(cmd, sizeof(cmd),
                     "{\"op\":\"rfid_make\",\"spec\":\"%s\",\"remote\":\"/ext/lfrfid/z2z_studio.rfid\","
                     "\"open_on_flipper\":true}\n",
                     escaped);
            ipc_send_request(st, "rfid_make", cmd);
        } else if (code == KEY_B && st->studio_len > 0) {
            char escaped[STUDIO_MAX * 2];
            json_escape_str(st->studio_buf, escaped, sizeof(escaped));
            snprintf(cmd, sizeof(cmd),
                     "{\"op\":\"ibtn_make\",\"spec\":\"%s\",\"remote\":\"/ext/ibutton/z2z_studio.ibtn\","
                     "\"open_on_flipper\":true}\n",
                     escaped);
            ipc_send_request(st, "ibtn_make", cmd);
        } else if (code == KEY_C) {
            ipc_send_request(st, "cam_scan",
                             "{\"op\":\"cam_scan\",\"target\":\"auto\",\"open_on_flipper\":true}\n");
        }
    } else if (st->tab == TAB_JOBS) {
        if (code == KEY_4 && st->job_count > 0) {
            snprintf(cmd, sizeof(cmd), "{\"op\":\"job_run\",\"job_id\":\"%s\"}\n",
                     st->jobs[st->job_sel].id);
            ipc_send_request(st, "run", cmd);
        } else if (code == KEY_5) {
            ipc_send_request(st, "jobs", "{\"op\":\"jobs_list\"}\n");
        } else if (code == KEY_6) {
            ipc_send_request(st, "fap", "{\"op\":\"fap_launch\"}\n");
        } else if (code == KEY_7) {
            ipc_send_request(st, "vault", "{\"op\":\"vault_sync\"}\n");
        }
    } else if (st->tab == TAB_NET) {
        if (code == KEY_4) {
            ipc_send_request(st, "net", "{\"op\":\"net_summary\"}\n");
        } else if (code == KEY_5) {
            ipc_send_request(st, "run", "{\"op\":\"job_run\",\"job_id\":\"note-sync\"}\n");
        } else if (code == KEY_6) {
            ipc_send_request(st, "status", "{\"op\":\"status\",\"query_rpc\":true}\n");
        } else if (code == KEY_7) {
            snprintf(cmd, sizeof(cmd), "{\"op\":\"watch_set\",\"enabled\":%s}\n",
                     st->watch_enabled ? "false" : "true");
            ipc_send_request(st, "watch", cmd);
        }
    }
}

int main(void)
{
    struct app_state st;
    memset(&st, 0, sizeof(st));
    st.ipc_fd = -1;
    st.flipper_bat = -1;
    st.link_rssi = -100;
    snprintf(st.alert_mode, sizeof(st.alert_mode), "vibro");
    apply_studio_preset(&st, 0);
    set_banner(&st, "Zero2Zero BLE Offload Companion", COL_CYAN);

    /* Headless PPM self-test hook (identical to kali-on-cardputerzero `make check`) */
    const char *dump_ppm = getenv("CZ_DUMP_PPM");
    if (dump_ppm && *dump_ppm) {
        snprintf(st.devs[0].name, sizeof(st.devs[0].name), "Flipper Zero");
        snprintf(st.devs[0].address, sizeof(st.devs[0].address), "80:E1:26:12:34:56");
        st.devs[0].rssi = -52;
        st.devs[0].paired = 1;
        st.dev_count = 1;
        render_ui(&st);
        return cz_dump_ppm(dump_ppm) == 0 ? 0 : 1;
    }

    signal(SIGINT, on_sig);
    signal(SIGTERM, on_sig);
    signal(SIGCHLD, SIG_IGN);
    signal(SIGPIPE, SIG_IGN);

    struct cz_fb fb;
    if (cz_fb_open(&fb) < 0) {
        fprintf(stderr, "z2z-ui: unable to open /dev/fb_lcd framebuffer\n");
        return 1;
    }

    int in_fds[CZ_MAX_INPUT_FDS];
    int n_in = cz_inputs_open(in_fds, CZ_MAX_INPUT_FDS);

    ipc_send_request(&st, "status", "{\"op\":\"status\",\"query_rpc\":false}\n");

    double esc_down_at = 0.0;
    int jobs_loaded_once = 0;

    while (!g_quit) {
        if (!st.ipc_busy && st.want_screen_stop) {
            st.want_screen_stop = 0;
            ipc_send_request(&st, "screen_stop", "{\"op\":\"screen_stop\"}\n");
        }

        if (!st.ipc_busy && !jobs_loaded_once) {
            jobs_loaded_once = 1;
            ipc_send_request(&st, "jobs", "{\"op\":\"jobs_list\"}\n");
        }

        if (st.tab == TAB_REMOTE && st.mirror_active && !st.ipc_busy && st.fkey_count > 0) {
            struct fkey_item item = st.fkey_ring[st.fkey_head];
            st.fkey_head = (st.fkey_head + 1) % FKEY_RING_CAP;
            st.fkey_count--;
            send_flipper_key_ex(&st, item.key, item.mode, item.coalesce_repeat);
        }

        double now_mono = cz_mono_sec();
        if (st.tab == TAB_REMOTE && st.mirror_active && !st.ipc_busy &&
            (now_mono - st.last_frame_poll) >= 0.60) {
            char fcmd[128];
            int sseq = st.has_screen_fb ? st.screen_seq : -1;
            st.last_frame_poll = now_mono;
            snprintf(fcmd, sizeof(fcmd),
                     "{\"op\":\"screen_frame\",\"auto_start\":false,\"since_seq\":%d}\n", sseq);
            ipc_send_request(&st, "screen_frame", fcmd);
        }

        render_ui(&st);
        cz_fb_blit(&fb);

        struct pollfd pfds[CZ_MAX_INPUT_FDS + 1];
        int nfds = 0;
        for (int i = 0; i < n_in; i++) {
            pfds[nfds].fd = in_fds[i];
            pfds[nfds].events = POLLIN;
            nfds++;
        }
        int ipc_slot = -1;
        if (st.ipc_fd >= 0) {
            ipc_slot = nfds;
            pfds[nfds].fd = st.ipc_fd;
            pfds[nfds].events = POLLIN;
            nfds++;
        }

        int poll_ms = (st.tab == TAB_REMOTE && st.mirror_active) ? 16 : 150;
        int rc = poll(pfds, (nfds_t)nfds, poll_ms);
        if (rc > 0) {
            for (int i = 0; i < n_in; i++) {
                if (!(pfds[i].revents & POLLIN)) continue;
                struct input_event ev;
                while (read(in_fds[i], &ev, sizeof(ev)) == (ssize_t)sizeof(ev)) {
                    if (ev.type != EV_KEY) continue;
                    if (ev.code == KEY_LEFTSHIFT || ev.code == KEY_RIGHTSHIFT) {
                        st.shift_held = (ev.value != 0);
                        continue;
                    }
                    if (ev.code == KEY_ESC || ev.code == KEY_GRAVE) {
                        if (ev.value == 1) esc_down_at = cz_mono_sec();
                        else if (ev.value == 0) esc_down_at = 0.0;
                    }
                    if (ev.value == 1) {
                        handle_key_press(&st, ev.code);
                    } else if (ev.value == 2) {
                        handle_key_repeat(&st, ev.code);
                    }
                }
            }
            if (ipc_slot >= 0 && (pfds[ipc_slot].revents & (POLLIN | POLLHUP | POLLERR))) {
                pump_ipc_socket(&st);
            }
        }

        if (esc_down_at > 0.0 && (cz_mono_sec() - esc_down_at) >= 1.2) {
            if (st.mirror_active) {
                stop_mirror_mode(&st, NULL, COL_CYAN);
            }
            g_quit = 1;
        }
    }

    if (st.ipc_fd >= 0) close(st.ipc_fd);
    cz_inputs_close(in_fds, n_in);
    cz_fb_close(&fb);
    return 0;
}
