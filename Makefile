GCC := gcc

all: libitila.so libitila_scanner.so libitila_dsp.so

libitila.so: itila_core.c fb_core.c itila.h
	$(GCC) -O3 -ffast-math -shared -fPIC -o $@ itila_core.c fb_core.c -lm

libitila_scanner.so: itila_scanner.c itila_scanner.h
	$(GCC) -O3 -ffast-math -shared -fPIC -o $@ itila_scanner.c -lm

libitila_dsp.so: itila_dsp.c itila_dsp.h
	$(GCC) -O3 -ffast-math -shared -fPIC -o $@ itila_dsp.c -lm

clean:
	rm -f *.o *.so

.PHONY: all clean
