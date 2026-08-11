#define _GNU_SOURCE

#include <errno.h>
#include <dlfcn.h>
#include <pthread.h>
#include <netdb.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <sys/socket.h>

/*
 * Minimal Bionic-to-glibc compatibility for UBox's arm64 P4P transport.
 *
 * The vendor library was built for Android and references a few Bionic-only
 * fortified helpers.  Keep this shim deliberately small: all networking and
 * threading continue to use glibc directly.
 */

int *__errno(void) {
    return &errno;
}

size_t __strlen_chk(const char *value, size_t limit) {
    size_t length = strlen(value);
    if (length >= limit) {
        abort();
    }
    return length;
}

void __FD_SET_chk(int fd, fd_set *set, size_t set_size) {
    if (fd < 0 || (size_t)fd >= set_size * 8U) {
        abort();
    }
    FD_SET(fd, set);
}

int __FD_ISSET_chk(int fd, const fd_set *set, size_t set_size) {
    if (fd < 0 || (size_t)fd >= set_size * 8U) {
        abort();
    }
    return FD_ISSET(fd, set);
}

void __assert2(const char *file, int line, const char *function,
               const char *expression) {
    fprintf(stderr, "%s:%d: %s: assertion `%s' failed\n", file, line,
            function, expression);
    abort();
}

int __android_log_print(int priority, const char *tag, const char *format, ...) {
    (void)priority;
    va_list args;
    va_start(args, format);
    if (tag != NULL) {
        fprintf(stderr, "[%s] ", tag);
    }
    int result = vfprintf(stderr, format, args);
    fputc('\n', stderr);
    va_end(args);
    return result;
}

/*
 * Bionic's opaque pthread objects are smaller than glibc's aarch64 objects.
 * Store the real glibc object out of line and key it by the address supplied
 * by the Android library.  This prevents glibc from writing past the vendor's
 * stack slots and heap allocations.
 */
enum object_kind {
    OBJECT_THREAD_ATTR = 1,
    OBJECT_MUTEX_ATTR = 2,
    OBJECT_MUTEX = 3,
    OBJECT_ADDRINFO = 4,
};

struct object_entry {
    void *key;
    void *object;
    enum object_kind kind;
};

static struct object_entry object_entries[128];
static atomic_flag object_entries_lock = ATOMIC_FLAG_INIT;

static void entries_lock(void) {
    while (atomic_flag_test_and_set_explicit(&object_entries_lock,
                                              memory_order_acquire)) {
    }
}

static void entries_unlock(void) {
    atomic_flag_clear_explicit(&object_entries_lock, memory_order_release);
}

static void *entry_find(void *key, enum object_kind kind) {
    void *result = NULL;
    entries_lock();
    for (size_t index = 0; index < 128; ++index) {
        if (object_entries[index].key == key &&
            object_entries[index].kind == kind) {
            result = object_entries[index].object;
            break;
        }
    }
    entries_unlock();
    return result;
}

static int entry_add(void *key, enum object_kind kind, void *object) {
    int result = -1;
    entries_lock();
    for (size_t index = 0; index < 128; ++index) {
        if (object_entries[index].key == NULL) {
            object_entries[index] =
                (struct object_entry){.key = key, .object = object, .kind = kind};
            result = 0;
            break;
        }
    }
    entries_unlock();
    return result;
}

static void *entry_take(void *key, enum object_kind kind) {
    void *result = NULL;
    entries_lock();
    for (size_t index = 0; index < 128; ++index) {
        if (object_entries[index].key == key &&
            object_entries[index].kind == kind) {
            result = object_entries[index].object;
            object_entries[index] = (struct object_entry){0};
            break;
        }
    }
    entries_unlock();
    return result;
}

#define NEXT_FUNCTION(type, name) ((type)dlsym(RTLD_NEXT, (name)))

int pthread_attr_init(pthread_attr_t *attr) {
    typedef int (*function_type)(pthread_attr_t *);
    function_type real_function =
        NEXT_FUNCTION(function_type, "pthread_attr_init");
    pthread_attr_t *real_attr = malloc(sizeof(*real_attr));
    if (real_attr == NULL) {
        return ENOMEM;
    }
    int result = real_function(real_attr);
    if (result != 0 || entry_add(attr, OBJECT_THREAD_ATTR, real_attr) != 0) {
        free(real_attr);
        return result != 0 ? result : ENOMEM;
    }
    return 0;
}

int pthread_attr_setdetachstate(pthread_attr_t *attr, int state) {
    typedef int (*function_type)(pthread_attr_t *, int);
    function_type real_function =
        NEXT_FUNCTION(function_type, "pthread_attr_setdetachstate");
    pthread_attr_t *real_attr = entry_find(attr, OBJECT_THREAD_ATTR);
    return real_function(real_attr != NULL ? real_attr : attr, state);
}

int pthread_attr_destroy(pthread_attr_t *attr) {
    typedef int (*function_type)(pthread_attr_t *);
    function_type real_function =
        NEXT_FUNCTION(function_type, "pthread_attr_destroy");
    pthread_attr_t *real_attr = entry_take(attr, OBJECT_THREAD_ATTR);
    if (real_attr == NULL) {
        return real_function(attr);
    }
    int result = real_function(real_attr);
    free(real_attr);
    return result;
}

int pthread_create(pthread_t *thread, const pthread_attr_t *attr,
                   void *(*start_routine)(void *), void *argument) {
    typedef int (*function_type)(pthread_t *, const pthread_attr_t *,
                                 void *(*)(void *), void *);
    function_type real_function = NEXT_FUNCTION(function_type, "pthread_create");
    pthread_attr_t *real_attr =
        attr == NULL ? NULL : entry_find((void *)attr, OBJECT_THREAD_ATTR);
    return real_function(thread, real_attr != NULL ? real_attr : attr,
                         start_routine, argument);
}

int pthread_mutexattr_init(pthread_mutexattr_t *attr) {
    typedef int (*function_type)(pthread_mutexattr_t *);
    function_type real_function =
        NEXT_FUNCTION(function_type, "pthread_mutexattr_init");
    pthread_mutexattr_t *real_attr = malloc(sizeof(*real_attr));
    if (real_attr == NULL) {
        return ENOMEM;
    }
    int result = real_function(real_attr);
    if (result != 0 || entry_add(attr, OBJECT_MUTEX_ATTR, real_attr) != 0) {
        free(real_attr);
        return result != 0 ? result : ENOMEM;
    }
    return 0;
}

int pthread_mutexattr_settype(pthread_mutexattr_t *attr, int type) {
    typedef int (*function_type)(pthread_mutexattr_t *, int);
    function_type real_function =
        NEXT_FUNCTION(function_type, "pthread_mutexattr_settype");
    pthread_mutexattr_t *real_attr = entry_find(attr, OBJECT_MUTEX_ATTR);
    return real_function(real_attr != NULL ? real_attr : attr, type);
}

int pthread_mutexattr_destroy(pthread_mutexattr_t *attr) {
    typedef int (*function_type)(pthread_mutexattr_t *);
    function_type real_function =
        NEXT_FUNCTION(function_type, "pthread_mutexattr_destroy");
    pthread_mutexattr_t *real_attr = entry_take(attr, OBJECT_MUTEX_ATTR);
    if (real_attr == NULL) {
        return real_function(attr);
    }
    int result = real_function(real_attr);
    free(real_attr);
    return result;
}

int pthread_mutex_init(pthread_mutex_t *mutex,
                       const pthread_mutexattr_t *attr) {
    typedef int (*function_type)(pthread_mutex_t *, const pthread_mutexattr_t *);
    function_type real_function =
        NEXT_FUNCTION(function_type, "pthread_mutex_init");
    pthread_mutex_t *real_mutex = malloc(sizeof(*real_mutex));
    if (real_mutex == NULL) {
        return ENOMEM;
    }
    pthread_mutexattr_t *real_attr =
        attr == NULL ? NULL : entry_find((void *)attr, OBJECT_MUTEX_ATTR);
    int result = real_function(real_mutex, real_attr != NULL ? real_attr : attr);
    if (result != 0 || entry_add(mutex, OBJECT_MUTEX, real_mutex) != 0) {
        free(real_mutex);
        return result != 0 ? result : ENOMEM;
    }
    return 0;
}

int pthread_mutex_lock(pthread_mutex_t *mutex) {
    typedef int (*function_type)(pthread_mutex_t *);
    function_type real_function =
        NEXT_FUNCTION(function_type, "pthread_mutex_lock");
    pthread_mutex_t *real_mutex = entry_find(mutex, OBJECT_MUTEX);
    return real_function(real_mutex != NULL ? real_mutex : mutex);
}

int pthread_mutex_unlock(pthread_mutex_t *mutex) {
    typedef int (*function_type)(pthread_mutex_t *);
    function_type real_function =
        NEXT_FUNCTION(function_type, "pthread_mutex_unlock");
    pthread_mutex_t *real_mutex = entry_find(mutex, OBJECT_MUTEX);
    return real_function(real_mutex != NULL ? real_mutex : mutex);
}

int pthread_mutex_destroy(pthread_mutex_t *mutex) {
    typedef int (*function_type)(pthread_mutex_t *);
    function_type real_function =
        NEXT_FUNCTION(function_type, "pthread_mutex_destroy");
    pthread_mutex_t *real_mutex = entry_take(mutex, OBJECT_MUTEX);
    if (real_mutex == NULL) {
        return real_function(mutex);
    }
    int result = real_function(real_mutex);
    free(real_mutex);
    return result;
}

struct bionic_addrinfo {
    int ai_flags;
    int ai_family;
    int ai_socktype;
    int ai_protocol;
    socklen_t ai_addrlen;
    char *ai_canonname;
    struct sockaddr *ai_addr;
    struct bionic_addrinfo *ai_next;
};

_Static_assert(sizeof(struct bionic_addrinfo) == 48,
               "unexpected Bionic addrinfo layout");

int getaddrinfo(const char *node, const char *service,
                const struct addrinfo *hints, struct addrinfo **result) {
    typedef int (*function_type)(const char *, const char *,
                                 const struct addrinfo *, struct addrinfo **);
    function_type real_function = NEXT_FUNCTION(function_type, "getaddrinfo");

    struct addrinfo real_hints = {0};
    const struct addrinfo *real_hints_pointer = NULL;
    if (hints != NULL) {
        const struct bionic_addrinfo *bionic_hints =
            (const struct bionic_addrinfo *)hints;
        real_hints.ai_flags = bionic_hints->ai_flags;
        real_hints.ai_family = bionic_hints->ai_family;
        real_hints.ai_socktype = bionic_hints->ai_socktype;
        real_hints.ai_protocol = bionic_hints->ai_protocol;
        real_hints_pointer = &real_hints;
    }

    struct addrinfo *real_result = NULL;
    int status = real_function(node, service, real_hints_pointer, &real_result);
    if (status != 0) {
        *result = NULL;
        return status;
    }

    struct bionic_addrinfo *head = NULL;
    struct bionic_addrinfo **next = &head;
    for (struct addrinfo *item = real_result; item != NULL;
         item = item->ai_next) {
        struct bionic_addrinfo *converted = calloc(1, sizeof(*converted));
        if (converted == NULL) {
            while (head != NULL) {
                struct bionic_addrinfo *following = head->ai_next;
                free(head);
                head = following;
            }
            typedef void (*free_function_type)(struct addrinfo *);
            free_function_type real_free =
                NEXT_FUNCTION(free_function_type, "freeaddrinfo");
            real_free(real_result);
            *result = NULL;
            return EAI_MEMORY;
        }
        converted->ai_flags = item->ai_flags;
        converted->ai_family = item->ai_family;
        converted->ai_socktype = item->ai_socktype;
        converted->ai_protocol = item->ai_protocol;
        converted->ai_addrlen = item->ai_addrlen;
        converted->ai_canonname = item->ai_canonname;
        converted->ai_addr = item->ai_addr;
        *next = converted;
        next = &converted->ai_next;
    }

    if (entry_add(head, OBJECT_ADDRINFO, real_result) != 0) {
        while (head != NULL) {
            struct bionic_addrinfo *following = head->ai_next;
            free(head);
            head = following;
        }
        typedef void (*free_function_type)(struct addrinfo *);
        free_function_type real_free =
            NEXT_FUNCTION(free_function_type, "freeaddrinfo");
        real_free(real_result);
        *result = NULL;
        return EAI_MEMORY;
    }
    *result = (struct addrinfo *)head;
    return 0;
}

void freeaddrinfo(struct addrinfo *result) {
    typedef void (*function_type)(struct addrinfo *);
    function_type real_function = NEXT_FUNCTION(function_type, "freeaddrinfo");
    struct addrinfo *real_result = entry_take(result, OBJECT_ADDRINFO);
    if (real_result == NULL) {
        real_function(result);
        return;
    }
    struct bionic_addrinfo *item = (struct bionic_addrinfo *)result;
    while (item != NULL) {
        struct bionic_addrinfo *following = item->ai_next;
        free(item);
        item = following;
    }
    real_function(real_result);
}
