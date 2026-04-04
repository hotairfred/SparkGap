// Shim for KiwiSDR dependencies — standalone UHSDR CW decoder
#ifndef UHSDR_SHIM_H
#define UHSDR_SHIM_H

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <math.h>

typedef float float32_t;
typedef int16_t s2_t;
typedef uint32_t u4_t;
typedef int16_t TYPEMONO16;

#define MAX_RX_CHANS 1
#define K_PI M_PI
#define CLAMP(x, lo, hi) ((x) < (lo) ? (lo) : ((x) > (hi) ? (hi) : (x)))
#define SPACE_FOR_NULL 1
#define ARRAY_LEN(x) (sizeof(x)/sizeof(x[0]))
#define FALSE 0
#define TRUE 1
#define FLIP16(x) (x)
#define DIR_SAMPLES "."

#define ring_idx_wrap_upper(value,size) (((value) >= (size)) ? (value) - (size) : (value))
#define ring_idx_wrap_zero(value,size) (((value) < (0)) ? (value) + (size) : (value))
#define ring_idx_change(value,change,size) \
    ((change) >= 0? ring_idx_wrap_upper((value)+(change), (size)) : \
                     ring_idx_wrap_zero((value)+(change), (size)))
#define ring_idx_increment(value,size) ((value+1) == (size)? 0:(value+1))
#define ring_idx_decrement(value,size) ((value) == 0? (size)-1:(value)-1)

// Stub out KiwiSDR functions
static inline void ext_send_msg(int rx_chan, bool debug, const char *fmt, ...) {}
static inline void ext_send_msg_encoded(int rx_chan, bool debug, const char *grp, const char *cmd, const char *fmt, ...) {}
static inline float ext_update_get_sample_rateHz(int rx_chan) { return 12000.0f; }

// Global sample rate (set by main)
extern float snd_rate;

// Character output callback
typedef void (*cw_output_fn)(char ch, int wpm);
extern cw_output_fn cw_output_callback;

#endif

// Additional stubs
static inline u4_t timer_sec() { return 0; }
static inline void NextTask(const char *s) {}
static inline void TaskSleepMsec(int ms) {}
