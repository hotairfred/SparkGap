CC	:= g++
INCDIRS	:=
LIBDIRS	:=
LIBS	:= -lcsdr++ -lfftw3f
CFLAGS	:= -O3 $(INCDIRS)
OBJECTS	:= cw-skimmer.o rtty-skimmer.o bufmodule.o

# make native ARCH=native for -march=native.
ARCHFLAG := $(if $(ARCH),-march=$(ARCH))

# libbmorse can't be rebuilt here (missing morse-wip headers).
NATIVE_LIBS := libhpsdr_fast.so libitila.so libitila_dsp.so libitila_scanner.so \
               librtty.so libuhsdr_cw.so libcw_engine.so libpfb_scanner.so libitila2.so \
               libitila2_scanner.so

all: native libcw_dispatcher.so

native: $(NATIVE_LIBS)

# Needs libcsdr++.
csdr: csdr-cwskimmer csdr-rttyskimmer

libhpsdr_fast.so: hpsdr_fast.c
	gcc -shared -O2 -fPIC -pthread $(ARCHFLAG) -o $@ hpsdr_fast.c

libitila.so: itila_core.c fb_core.c itila.h
	gcc -O3 $(ARCHFLAG) -ffast-math -shared -fPIC \
	    -o $@ itila_core.c fb_core.c -lm

libitila2.so: itila2_core.c fb_core.c itila.h
	gcc -O3 $(ARCHFLAG) -ffast-math -shared -fPIC \
	    -o $@ itila2_core.c fb_core.c -lm

libitila_dsp.so: itila_dsp.c itila_dsp.h
	gcc -O3 $(ARCHFLAG) -ffast-math -shared -fPIC \
	    -o $@ itila_dsp.c -lm

libitila_scanner.so: itila_scanner.c itila_scanner.h itila_fir_coeffs.h
	gcc -O3 $(ARCHFLAG) -ffast-math -shared -fPIC -pthread \
	    -o $@ itila_scanner.c -lm

libitila2_scanner.so: itila2_scanner.c itila_scanner.h itila_fir_coeffs.h
	gcc -O3 $(ARCHFLAG) -ffast-math -shared -fPIC -pthread \
	    -o $@ itila2_scanner.c -lm

librtty.so: rtty_core.c
	gcc -shared -O2 -fPIC $(ARCHFLAG) -o $@ rtty_core.c -lm

# No recorded recipe for these two; -O2 is a guess.
libuhsdr_cw.so: uhsdr_cw_lib.cpp uhsdr_cw_lib.h uhsdr_cw.h uhsdr_shim.h
	$(CC) -O2 -shared -fPIC $(ARCHFLAG) -o $@ uhsdr_cw_lib.cpp -I. -lm

libcw_engine.so: cw_engine.cpp cw_engine.h uhsdr_cw_lib.h libbmorse.h libuhsdr_cw.so
	$(CC) -O2 -shared -fPIC $(ARCHFLAG) -o $@ cw_engine.cpp -I. \
	    -L. -Wl,-rpath,'$$ORIGIN' -luhsdr_cw -lm

csdr-cwskimmer: cw-skimmer.o bufmodule.o
	$(CC) $(CFLAGS) -o $@ $^ $(LIBDIRS) $(LIBS)

csdr-rttyskimmer: rtty-skimmer.o bufmodule.o
	$(CC) $(CFLAGS) -o $@ $^ $(LIBDIRS) $(LIBS)

# Parallel CW decoder dispatcher (uhsdr + bmorse fan-out via OpenMP, PFB inside).
libcw_dispatcher.so: cw_dispatcher.cpp cw_dispatcher.h cw_pfb.cpp cw_pfb.h uhsdr_cw_lib.cpp uhsdr_cw_lib.h uhsdr_shim.h libbmorse.h libbmorse.so
	$(CC) -O3 -fPIC -fopenmp -shared -o $@ cw_dispatcher.cpp cw_pfb.cpp uhsdr_cw_lib.cpp -I. -L. -Wl,-rpath,'$$ORIGIN' -lbmorse -lfftw3f -lm

# PFB-backed band scanner — drop-in replacement for libitila_scanner.so when
# use_pfb_scanner: true is set.  Replaces per-bin NCO+FIR with a shared PFB.
libpfb_scanner.so: pfb_scanner.c pfb_scanner.h cw_pfb.cpp cw_pfb.h
	$(CC) -O3 $(ARCHFLAG) -ffast-math -shared -fPIC \
	    -o $@ pfb_scanner.c cw_pfb.cpp -lfftw3f -lm

cw_dispatcher_test: cw_dispatcher_test.cpp cw_dispatcher.h libcw_dispatcher.so
	$(CC) -O2 -Wall -o $@ cw_dispatcher_test.cpp -L. -lcw_dispatcher -fopenmp

cw_dispatcher_pfb_test: cw_dispatcher_pfb_test.cpp cw_dispatcher.h libcw_dispatcher.so
	$(CC) -O2 -Wall -o $@ cw_dispatcher_pfb_test.cpp -L. -lcw_dispatcher -fopenmp

dispatcher-test: cw_dispatcher_test cw_dispatcher_pfb_test
	LD_LIBRARY_PATH=. ./cw_dispatcher_test
	@echo
	LD_LIBRARY_PATH=. ./cw_dispatcher_pfb_test

clean:
	rm -f $(OBJECTS) csdr-cwskimmer csdr-rttyskimmer \
	      libcw_dispatcher.so \
	      cw_dispatcher_test cw_dispatcher_pfb_test

# Committed .so files can look newer than their sources; always rebuild.
.PHONY: all native csdr clean dispatcher-test $(NATIVE_LIBS)
