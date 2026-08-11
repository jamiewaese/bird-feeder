CC ?= cc
CFLAGS ?= -O2 -Wall -Wextra -Werror

.PHONY: all clean install

all: liblog.so ubox-probe ubox-connect

liblog.so: android_compat.c
	$(CC) $(CFLAGS) -shared -fPIC -o $@ $<

ubox-probe: probe.c
	$(CC) $(CFLAGS) -o $@ $< -ldl

ubox-connect: ubox_connect.c
	$(CC) $(CFLAGS) -rdynamic -o $@ $< -ldl

clean:
	rm -f liblog.so ubox-probe ubox-connect

install: liblog.so ubox-connect
	install -m 0755 ubox-connect "$(DESTDIR)/ubox-connect"
	install -m 0755 liblog.so "$(DESTDIR)/liblog.so"
