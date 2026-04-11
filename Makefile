CC	:= g++
INCDIRS	:=
LIBDIRS	:=
LIBS	:= -lcsdr++ -lfftw3f
CFLAGS	:= -O3 $(INCDIRS)
OBJECTS	:= cw-skimmer.o rtty-skimmer.o bufmodule.o

all: csdr-cwskimmer csdr-rttyskimmer libcw_dispatcher.so

csdr-cwskimmer: cw-skimmer.o bufmodule.o
	$(CC) $(CFLAGS) -o $@ $^ $(LIBDIRS) $(LIBS)

csdr-rttyskimmer: rtty-skimmer.o bufmodule.o
	$(CC) $(CFLAGS) -o $@ $^ $(LIBDIRS) $(LIBS)

# Parallel CW decoder dispatcher. Extended with bmorse support (bmorse
# channels run PFB + shift + decimate + FIR in C++; bmorse_feed itself
# runs serially because libbmorse is not thread-safe).
libcw_dispatcher.so: cw_dispatcher.cpp cw_dispatcher.h cw_pfb.cpp cw_pfb.h uhsdr_cw_lib.cpp uhsdr_cw_lib.h uhsdr_shim.h libbmorse.h libbmorse.so
	$(CC) -O3 -fPIC -fopenmp -shared -o $@ cw_dispatcher.cpp cw_pfb.cpp uhsdr_cw_lib.cpp -I. -L. -Wl,-rpath,'$$ORIGIN' -lbmorse -lfftw3f -lm

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
	      libcw_dispatcher.so cw_dispatcher_test cw_dispatcher_pfb_test

.PHONY: all clean dispatcher-test
