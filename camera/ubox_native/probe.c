#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>

typedef int (*crypto_init_fn)(int);
typedef void *(*crypto_decode_fn)(void *, unsigned short);
typedef int (*crypto_exit_fn)(void);

int main(int argc, char **argv) {
    if (argc != 2) {
        fprintf(stderr, "usage: %s PATH_TO_libUBICAPIs.so\n", argv[0]);
        return 2;
    }

    void *library = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    if (library == NULL) {
        fprintf(stderr, "dlopen: %s\n", dlerror());
        return 1;
    }

    crypto_init_fn crypto_init = (crypto_init_fn)dlsym(library, "p4p_crypto_init");
    crypto_decode_fn crypto_decode =
        (crypto_decode_fn)dlsym(library, "p4p_crypto_decode");
    crypto_exit_fn crypto_exit = (crypto_exit_fn)dlsym(library, "p4p_crypto_exit");
    if (crypto_init == NULL || crypto_decode == NULL || crypto_exit == NULL) {
        fprintf(stderr, "missing P4P crypto symbols: %s\n", dlerror());
        dlclose(library);
        return 1;
    }

    int result = crypto_init(1416);
    printf("loaded UBox P4P transport; crypto init=%d\n", result);
    if (result == 0) {
        crypto_exit();
    }
    dlclose(library);
    return result == 0 ? 0 : 1;
}
