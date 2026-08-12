#define _GNU_SOURCE

#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define MAX_EVENTS 240
#define CHUNK_SIZE 1200U
#define CHUNK_REQUEST_SIZE 1224U
#define MAX_FILE_SIZE (32U * 1024U * 1024U)

typedef struct {
    uint8_t device_type;
    uint8_t video_on;
    uint8_t listen_on;
    uint8_t speak_on;
    uint8_t stream_index;
    uint8_t zone_id;
    uint8_t channel;
    uint8_t play_record;
    uint8_t uid[20];
    uint8_t login_id[16];
    uint8_t login_password[20];
} p4p_client;

typedef struct {
    uint32_t start;
    uint16_t duration;
    uint8_t type;
    uint8_t status;
} event_record;

_Static_assert(sizeof(p4p_client) == 64, "unexpected P4P client layout");

typedef int (*mgmt_init_fn)(int, unsigned short, const char *, int, void *,
                            unsigned short);
typedef int (*mgmt_simple_fn)(void);
typedef int (*set_netmode_fn)(int);
typedef int (*random_id_fn)(const char *);
typedef int (*client_start_fn)(const p4p_client *, int);
typedef int (*client_stop_fn)(const char *, int);
typedef int (*send_ioctrl_fn)(int, int, int, const void *, int);

static volatile sig_atomic_t latest_status = -1;
static atomic_int event_response_seen = 0;
static atomic_int metadata_response_seen = 0;
static event_record events[MAX_EVENTS];
static atomic_size_t event_count = 0;
static uint8_t *download_bytes;
static atomic_uchar *received_segments;
static size_t download_size;
static size_t segment_count;
static atomic_size_t received_byte_count = 0;
static atomic_uint last_chunk_size = 0;
static char download_name[33];
static atomic_int current_file_type = 0;

static uint16_t load_u16_le(const uint8_t *source) {
    return (uint16_t)((uint16_t)source[0] | ((uint16_t)source[1] << 8U));
}

static uint32_t load_u32_le(const uint8_t *source) {
    return (uint32_t)source[0] | ((uint32_t)source[1] << 8U) |
           ((uint32_t)source[2] << 16U) | ((uint32_t)source[3] << 24U);
}

static void store_u16_le(uint8_t *destination, unsigned int value) {
    destination[0] = (uint8_t)value;
    destination[1] = (uint8_t)(value >> 8U);
}

static void store_u32_le(uint8_t *destination, uint32_t value) {
    destination[0] = (uint8_t)value;
    destination[1] = (uint8_t)(value >> 8U);
    destination[2] = (uint8_t)(value >> 16U);
    destination[3] = (uint8_t)(value >> 24U);
}

/* These names intentionally interpose the JNI callbacks in libUBICAPIs. */
void ubic_java_cb_clientstatus(const char *uid, int sid, int unused,
                               int status) {
    (void)unused;
    latest_status = status;
    fprintf(stderr, "UBox session %.20s sid=%d status=%d\n", uid, sid,
            status);
}

void ubic_java_cb_loginstatus(const char *uid, int sid, int result, int status,
                              const void *info, int info_length) {
    (void)info;
    fprintf(stderr,
            "UBox login %.20s sid=%d result=%d status=%d info_bytes=%d\n",
            uid, sid, result, status, info_length);
}

static void receive_event_list(const uint8_t *payload, int data_length) {
    if (data_length < 12) {
        return;
    }
    unsigned int count = payload[7];
    size_t available = (size_t)(data_length - 12) / 8U;
    if (count > available) {
        count = (unsigned int)available;
    }
    for (unsigned int index = 0; index < count; ++index) {
        size_t destination = atomic_load(&event_count);
        if (destination >= MAX_EVENTS) {
            break;
        }
        const uint8_t *record = payload + 12 + index * 8U;
        events[destination] = (event_record){
            .start = load_u32_le(record),
            .duration = load_u16_le(record + 4),
            .type = record[6],
            .status = record[7],
        };
        atomic_store(&event_count, destination + 1);
    }
    fprintf(stderr, "UBox event page=%u count=%u reported_total=%u\n",
            payload[5], count, load_u16_le(payload + 8));
    atomic_store(&event_response_seen, 1);
}

static void receive_metadata(const uint8_t *payload, int data_length) {
    if (data_length < 60 || atomic_load(&metadata_response_seen)) {
        return;
    }
    uint32_t size = load_u32_le(payload + 8);
    unsigned int name_length = payload[6];
    if (name_length > 32) {
        name_length = 32;
    }
    char candidate_name[33];
    memcpy(candidate_name, payload + 28, name_length);
    candidate_name[name_length] = '\0';
    const char *extension = strrchr(candidate_name, '.');
    int expected_type = atomic_load(&current_file_type);
    if (extension == NULL ||
        (expected_type == 1 && strcasecmp(extension, ".mp4") != 0) ||
        (expected_type == 2 && strcasecmp(extension, ".jpg") != 0 &&
         strcasecmp(extension, ".jpeg") != 0)) {
        return;
    }
    if (size == 0 || size > MAX_FILE_SIZE || name_length == 0) {
        fprintf(stderr, "Ignoring invalid metadata size=%u name_bytes=%u\n",
                size, name_length);
        atomic_store(&metadata_response_seen, -1);
        return;
    }
    download_bytes = calloc(1, size);
    segment_count = (size + CHUNK_SIZE - 1U) / CHUNK_SIZE;
    received_segments = calloc(segment_count, sizeof(*received_segments));
    if (download_bytes == NULL || received_segments == NULL) {
        fprintf(stderr, "Unable to allocate %u-byte download buffer\n", size);
        atomic_store(&metadata_response_seen, -1);
        return;
    }
    memcpy(download_name, candidate_name, name_length + 1U);
    download_size = size;
    atomic_store(&received_byte_count, 0);
    atomic_store(&metadata_response_seen, 1);
}

static void receive_chunk(const uint8_t *payload, int data_length) {
    if (data_length < 24 || download_bytes == NULL ||
        received_segments == NULL) {
        return;
    }
    int expected_type = atomic_load(&current_file_type);
    int response_is_video = payload[10] == 1 || payload[10] == 3;
    if (
        (expected_type == 1 && !response_is_video) ||
        (expected_type == 2 && response_is_video)) {
        return;
    }
    uint32_t offset = load_u32_le(payload + 16);
    uint32_t size = load_u32_le(payload + 20);
    size_t segment = offset / CHUNK_SIZE;
    size_t expected = download_size - offset;
    if (expected > CHUNK_SIZE) {
        expected = CHUNK_SIZE;
    }
    if (offset % CHUNK_SIZE != 0 || segment >= segment_count ||
        size != expected || (uint64_t)offset + size > download_size ||
        (uint64_t)24 + size > (uint32_t)data_length) {
        fprintf(stderr, "Ignoring invalid chunk offset=%u size=%u\n", offset,
                size);
        return;
    }
    memcpy(download_bytes + offset, payload + 24, size);
    atomic_store(&last_chunk_size, size);
    if (atomic_exchange(&received_segments[segment], 1) == 0) {
        atomic_fetch_add(&received_byte_count, size);
    }
}

void ubic_java_cb_ioctrl(int sid, int channel, int command, const void *data,
                         int data_length) {
    (void)sid;
    (void)channel;
    if (data == NULL) {
        return;
    }
    const uint8_t *payload = data;
    if (command == 257) {
        receive_event_list(payload, data_length);
    } else if (command == 261) {
        receive_metadata(payload, data_length);
    } else if (command == 263) {
        receive_chunk(payload, data_length);
    }
}

static void *required_symbol(void *library, const char *name) {
    dlerror();
    void *symbol = dlsym(library, name);
    const char *error = dlerror();
    if (error != NULL) {
        fprintf(stderr, "dlsym %s: %s\n", name, error);
        exit(1);
    }
    return symbol;
}

static int copy_field(uint8_t *destination, size_t capacity, const char *source,
                      const char *label) {
    size_t length = strlen(source);
    if (length == 0 || length > capacity) {
        fprintf(stderr, "%s must contain 1 to %zu bytes\n", label, capacity);
        return -1;
    }
    memcpy(destination, source, length);
    return 0;
}

static void reset_download(void) {
    atomic_store(&current_file_type, 0);
    free(received_segments);
    free(download_bytes);
    received_segments = NULL;
    download_bytes = NULL;
    download_size = 0;
    segment_count = 0;
    download_name[0] = '\0';
    atomic_store(&received_byte_count, 0);
    atomic_store(&last_chunk_size, 0);
    atomic_store(&metadata_response_seen, 0);
}

static int valid_component(const char *value) {
    if (*value == '\0') {
        return 0;
    }
    for (const char *cursor = value; *cursor != '\0'; ++cursor) {
        if (!(('0' <= *cursor && *cursor <= '9') ||
              ('A' <= *cursor && *cursor <= 'Z') ||
              ('a' <= *cursor && *cursor <= 'z') || *cursor == '_' ||
              *cursor == '-' || *cursor == '.')) {
            return 0;
        }
    }
    return 1;
}

static int canonical_path(char *destination, size_t capacity, int file_type) {
    const char *name = download_name;
    char date_code[7] = {0};
    if (strlen(name) < 10 || name[8] != '_') {
        return -1;
    }
    for (size_t index = 0; index < 8; ++index) {
        if (name[index] < '0' || name[index] > '9') {
            return -1;
        }
    }
    memcpy(date_code, name + 2, 6);
    name += 9;
    if (!valid_component(name)) {
        return -1;
    }
    const char *extension = strrchr(name, '.');
    if (extension == NULL ||
        (file_type == 1 && strcasecmp(extension, ".mp4") != 0) ||
        (file_type == 2 && strcasecmp(extension, ".jpg") != 0 &&
         strcasecmp(extension, ".jpeg") != 0)) {
        return -1;
    }
    const char *top = file_type == 1 ? "video" : "snaps";
    if (snprintf(destination, capacity, "%s/%s/%s", top, date_code, name) >=
        (int)capacity) {
        return -1;
    }
    return 0;
}

static int mkdir_if_needed(const char *path) {
    if (mkdir(path, 0770) == 0 || errno == EEXIST) {
        struct stat status;
        return stat(path, &status) == 0 && S_ISDIR(status.st_mode) ? 0 : -1;
    }
    return -1;
}

static int prepare_parent(const char *root, const char *relative) {
    char top[1024];
    char date[1024];
    const char *first = strchr(relative, '/');
    const char *second = first == NULL ? NULL : strchr(first + 1, '/');
    if (first == NULL || second == NULL ||
        snprintf(top, sizeof(top), "%s/%.*s", root, (int)(first - relative),
                 relative) >= (int)sizeof(top) ||
        snprintf(date, sizeof(date), "%s/%.*s", root,
                 (int)(second - relative), relative) >= (int)sizeof(date)) {
        return -1;
    }
    return mkdir_if_needed(root) == 0 && mkdir_if_needed(top) == 0 &&
                   mkdir_if_needed(date) == 0
               ? 0
               : -1;
}

static int existing_matches(const char *root, const char *relative) {
    char path[2048];
    struct stat status;
    if (snprintf(path, sizeof(path), "%s/%s", root, relative) >=
        (int)sizeof(path)) {
        return 0;
    }
    return stat(path, &status) == 0 && S_ISREG(status.st_mode) &&
           (uint64_t)status.st_size == download_size;
}

static int write_atomic(const char *root, const char *relative) {
    if (prepare_parent(root, relative) != 0) {
        fprintf(stderr, "Unable to create staging directories for %s\n",
                relative);
        return -1;
    }
    char final_path[2048];
    char temporary_path[2048];
    if (snprintf(final_path, sizeof(final_path), "%s/%s", root, relative) >=
            (int)sizeof(final_path) ||
        snprintf(temporary_path, sizeof(temporary_path), "%s/.%s.%ld.part",
                 root, strrchr(relative, '/') + 1, (long)getpid()) >=
            (int)sizeof(temporary_path)) {
        return -1;
    }
    int descriptor = open(temporary_path, O_WRONLY | O_CREAT | O_EXCL, 0660);
    if (descriptor < 0) {
        fprintf(stderr, "Unable to create %s: %s\n", temporary_path,
                strerror(errno));
        return -1;
    }
    size_t written = 0;
    int result = 0;
    while (written < download_size) {
        ssize_t count = write(descriptor, download_bytes + written,
                              download_size - written);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            result = -1;
            break;
        }
        written += (size_t)count;
    }
    if (result == 0 && fsync(descriptor) != 0) {
        result = -1;
    }
    if (close(descriptor) != 0) {
        result = -1;
    }
    if (result == 0 && rename(temporary_path, final_path) != 0) {
        result = -1;
    }
    if (result != 0) {
        unlink(temporary_path);
    }
    return result;
}

static int wait_for(atomic_int *flag, int seconds) {
    for (int elapsed = 0; elapsed < seconds; ++elapsed) {
        int value = atomic_load(flag);
        if (value != 0) {
            return value;
        }
        sleep(1);
    }
    return 0;
}

static int send_chunk_request(send_ioctrl_fn send_ioctrl, int sid,
                              const event_record *event, int file_type,
                              int missing_only) {
    uint8_t request[CHUNK_REQUEST_SIZE] = {0};
    store_u32_le(request + 4, event->start);
    request[9] = (uint8_t)file_type;
    if (!missing_only) {
        store_u16_le(request + 1212, (unsigned int)segment_count);
        store_u32_le(request + 1220, (uint32_t)download_size);
    } else {
        unsigned int missing_total = 0;
        unsigned int missing = 0;
        size_t last_missing = 0;
        for (size_t index = 0; index < segment_count; ++index) {
            if (!atomic_load(&received_segments[index])) {
                if (missing < 600) {
                    store_u16_le(request + 12 + missing * 2U,
                                 (unsigned int)index);
                    ++missing;
                }
                ++missing_total;
                last_missing = index;
            }
        }
        if (missing_total == 0) {
            return 0;
        }
        if (atomic_load(&last_chunk_size) == CHUNK_SIZE) {
            store_u16_le(request + 1212, missing_total);
            store_u32_le(request + 1216,
                         (uint32_t)(last_missing * CHUNK_SIZE));
            store_u32_le(request + 1220, missing_total * CHUNK_SIZE);
            fprintf(stderr,
                    "Retrying %u missing chunks backward from offset %zu for "
                    "%s\n",
                    missing_total, last_missing * CHUNK_SIZE, download_name);
        } else {
            store_u16_le(request + 10, missing);
            store_u16_le(request + 1212, missing);
            store_u32_le(request + 1220, missing * CHUNK_SIZE);
            fprintf(stderr,
                    "Retrying %u of %u indexed missing chunks for %s\n",
                    missing, missing_total, download_name);
        }
    }
    return send_ioctrl(sid, 0, 262, request, sizeof(request));
}

static int download_one(send_ioctrl_fn send_ioctrl, int sid,
                        const event_record *event, int file_type,
                        const char *staging_root, const char *existing_root) {
    reset_download();
    atomic_store(&current_file_type, file_type);
    uint8_t metadata_request[16] = {0};
    metadata_request[5] = (uint8_t)file_type;
    store_u32_le(metadata_request + 8, event->start);
    store_u16_le(metadata_request + 12, event->duration);
    metadata_request[14] = event->type;
    metadata_request[15] = event->status;
    int metadata_status = 0;
    for (int attempt = 0; attempt < 3 && metadata_status == 0; ++attempt) {
        if (send_ioctrl(sid, 0, 260, metadata_request,
                        sizeof(metadata_request)) < 0) {
            break;
        }
        metadata_status = wait_for(&metadata_response_seen, 15);
        if (metadata_status == 0) {
            fprintf(stderr,
                    "Retrying type-%d metadata for event %u (attempt %d)\n",
                    file_type, event->start, attempt + 2);
        }
    }
    if (metadata_status != 1) {
        fprintf(stderr, "No valid type-%d metadata for event %u\n", file_type,
                event->start);
        return -1;
    }

    char relative[256];
    if (canonical_path(relative, sizeof(relative), file_type) != 0) {
        fprintf(stderr, "Rejecting unexpected UBox filename: %s\n",
                download_name);
        return -1;
    }
    if (existing_matches(existing_root, relative) ||
        existing_matches(staging_root, relative)) {
        fprintf(stderr, "Already present: %s\n", relative);
        return 0;
    }

    if (send_chunk_request(send_ioctrl, sid, event, file_type, 0) < 0) {
        return -1;
    }
    for (int attempt = 0; attempt < 15; ++attempt) {
        size_t previous = atomic_load(&received_byte_count);
        int idle_seconds = 0;
        for (int elapsed = 0; elapsed < 20; ++elapsed) {
            if (atomic_load(&received_byte_count) == download_size) {
                break;
            }
            sleep(1);
            size_t current = atomic_load(&received_byte_count);
            if (current == previous) {
                ++idle_seconds;
            } else {
                previous = current;
                idle_seconds = 0;
            }
            if (idle_seconds >= 12) {
                break;
            }
        }
        if (atomic_load(&received_byte_count) == download_size) {
            break;
        }
        if (send_chunk_request(send_ioctrl, sid, event, file_type, 1) < 0) {
            break;
        }
    }
    if (atomic_load(&received_byte_count) != download_size) {
        fprintf(stderr, "Incomplete %s: %zu of %zu bytes\n", download_name,
                atomic_load(&received_byte_count), download_size);
        return -1;
    }
    if (write_atomic(staging_root, relative) != 0) {
        fprintf(stderr, "Unable to stage completed file %s\n", relative);
        return -1;
    }
    fprintf(stderr, "Downloaded %zu bytes: %s\n", download_size, relative);
    sleep(2);
    return 1;
}

static int compare_events(const void *left, const void *right) {
    const event_record *a = left;
    const event_record *b = right;
    /* Recover the newest visits first so one damaged older file cannot make a
       bounded nightly run miss the media users are most likely to expect. */
    return a->start > b->start ? -1 : a->start < b->start ? 1 : 0;
}

int main(int argc, char **argv) {
    if (argc != 7) {
        fprintf(stderr,
                "usage: %s LIBUBICAPIS STAGING_ROOT EXISTING_MEDIA_ROOT "
                "LOOKBACK_HOURS EVENT_INDEX FILE_TYPE\n",
                argv[0]);
        return 2;
    }
    const char *uid = getenv("UBOX_UID");
    const char *password = getenv("UBOX_PASSWORD");
    if (uid == NULL || password == NULL) {
        fprintf(stderr, "UBOX_UID and UBOX_PASSWORD must be set\n");
        return 2;
    }
    char *end = NULL;
    long lookback_hours = strtol(argv[4], &end, 10);
    if (end == argv[4] || *end != '\0' || lookback_hours < 1 ||
        lookback_hours > 168) {
        fprintf(stderr, "LOOKBACK_HOURS must be from 1 through 168\n");
        return 2;
    }
    long event_index = strtol(argv[5], &end, 10);
    if (end == argv[5] || *end != '\0' || event_index < -1 ||
        (unsigned long)event_index > UINT32_MAX) {
        fprintf(stderr,
                "EVENT_INDEX must be -1 (list only), an index from 0 through %d, "
                "or an exact event start timestamp\n",
                MAX_EVENTS - 1);
        return 2;
    }
    long requested_file_type = strtol(argv[6], &end, 10);
    if (end == argv[6] || *end != '\0' ||
        (requested_file_type != 1 && requested_file_type != 2)) {
        fprintf(stderr, "FILE_TYPE must be 1 (MP4) or 2 (JPEG)\n");
        return 2;
    }

    void *library = dlopen(argv[1], RTLD_NOW | RTLD_GLOBAL);
    if (library == NULL) {
        fprintf(stderr, "dlopen: %s\n", dlerror());
        return 1;
    }
    mgmt_init_fn mgmt_init =
        (mgmt_init_fn)required_symbol(library, "p4p_mgmt_init");
    mgmt_simple_fn mgmt_exit =
        (mgmt_simple_fn)required_symbol(library, "p4p_mgmt_exit");
    set_netmode_fn set_netmode =
        (set_netmode_fn)required_symbol(library, "p4p_mgmt_setnetmode");
    random_id_fn random_id =
        (random_id_fn)required_symbol(library, "p4p_client_randomID");
    client_start_fn client_start =
        (client_start_fn)required_symbol(library, "p4p_client_start");
    client_stop_fn client_stop =
        (client_stop_fn)required_symbol(library, "p4p_client_stop");
    send_ioctrl_fn send_ioctrl =
        (send_ioctrl_fn)required_symbol(library, "p4p_client_send_ioctrl");

    int result = mgmt_init(16, 0, "NULL", 0, NULL, 0);
    if (result != 0) {
        fprintf(stderr, "p4p_mgmt_init failed: %d\n", result);
        dlclose(library);
        return 1;
    }
    set_netmode(0);

    p4p_client client = {0};
    client.device_type = 1;
    if (copy_field(client.uid, sizeof(client.uid), uid, "UBOX_UID") != 0 ||
        copy_field(client.login_id, sizeof(client.login_id), "admin",
                   "login ID") != 0 ||
        copy_field(client.login_password, sizeof(client.login_password),
                   password, "UBOX_PASSWORD") != 0) {
        mgmt_exit();
        dlclose(library);
        return 2;
    }
    int nonce = random_id(uid);
    if (nonce == 0) {
        nonce = random_id(uid);
    }
    int sid = client_start(&client, nonce);
    if (sid < 0) {
        fprintf(stderr, "p4p_client_start failed: %d\n", sid);
        mgmt_exit();
        dlclose(library);
        return 1;
    }
    for (int elapsed = 0; elapsed < 30 && latest_status != 5; ++elapsed) {
        sleep(1);
    }

    int failures = 0;
    int downloaded = 0;
    if (latest_status != 5) {
        fprintf(stderr, "Camera did not establish a direct LAN session\n");
        failures++;
    } else {
        uint8_t request[20] = {0};
        time_t now = time(NULL);
        request[5] = 0;
        request[6] = 60;
        store_u32_le(request + 8,
                     (uint32_t)(now - lookback_hours * 60L * 60L));
        store_u32_le(request + 12, (uint32_t)(now + 5 * 60));
        if (send_ioctrl(sid, 0, 256, request, sizeof(request)) < 0 ||
            wait_for(&event_response_seen, 15) != 1) {
            fprintf(stderr, "No SD event-list response\n");
            failures++;
        } else {
            size_t count = atomic_load(&event_count);
            qsort(events, count, sizeof(events[0]), compare_events);
            fprintf(stderr,
                    "Found %zu SD events; processing index %ld type %ld\n",
                    count, event_index, requested_file_type);
            if (event_index == -1) {
                for (size_t index = 0; index < count; ++index) {
                    fprintf(stderr,
                            "Event index=%zu start=%u duration=%u type=%u status=%u\n",
                            index, events[index].start, events[index].duration,
                            events[index].type, events[index].status);
                }
            } else {
                long selected_index = event_index;
                if (event_index >= MAX_EVENTS) {
                    selected_index = -1;
                    for (size_t index = 0; index < count; ++index) {
                        if (events[index].start == (uint32_t)event_index) {
                            selected_index = (long)index;
                            break;
                        }
                    }
                    fprintf(stderr,
                            "Resolved event start %ld to index %ld\n",
                            event_index, selected_index);
                }
                if (selected_index < 0 || (size_t)selected_index >= count) {
                    failures = -1;
                } else if (events[selected_index].duration != 0) {
                int status = download_one(
                    send_ioctrl, sid, &events[selected_index],
                    (int)requested_file_type, argv[2], argv[3]);
                if (status < 0) {
                    failures++;
                } else if (status > 0) {
                    downloaded++;
                }
                }
            }
        }
    }

    reset_download();
    client_stop(uid, sid);
    mgmt_exit();
    dlclose(library);
    fprintf(stderr, "UBox file session finished: downloaded=%d failures=%d\n",
            downloaded, failures);
    if (failures < 0) {
        return 11;
    }
    if (failures > 0) {
        return 1;
    }
    return downloaded > 0 ? 10 : 0;
}
