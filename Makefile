CC      ?= gcc
CFLAGS  ?= -O2 -Wall -Wextra -Werror
LDFLAGS ?=

UI_BIN  := src/z2z-ui

.PHONY: all build fap check deb clean clean-fap

all: build

build: $(UI_BIN)

$(UI_BIN): src/z2z-ui.c src/cz_ui.h
	$(CC) $(CFLAGS) -Isrc $< -o $@ $(LDFLAGS)

fap:
	./packaging/build-fap.sh

check: build
	@set -e; \
	tmp=$$(mktemp); \
	trap 'rm -f "$$tmp"' EXIT; \
	CZ_DUMP_PPM="$$tmp" ./$(UI_BIN); \
	test "$$(wc -c < "$$tmp")" -eq 163215; \
	echo "[OK] z2z-ui rendered valid 320x170 RGB565 -> PPM frame (163215 bytes)"
	python3 -m unittest discover -s tests -v

deb: build
	./packaging/build-deb.sh

clean:
	rm -rf $(UI_BIN) dist build *.o

clean-fap:
	rm -rf flipper_app/dist flipper_app/.vscode
