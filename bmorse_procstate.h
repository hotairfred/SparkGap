/**
 * bmorse_procstate.h — Per-instance state for bmorse free-function statics
 *
 * Phases 1+2 of arc-bmorse-reentrant: lifts function-local statics from
 * filter(), apply_window(), rx_FFTprocess(), and process_data() out of
 * global storage and into a per-handle struct.
 *
 * Each bmorse_handle_t carries one ProcessState. bmorse_feed() threads
 * ProcessState* through the call chain. The morse* decoder object lives
 * here too, so each handle gets independent Bayesian decoder state.
 *
 * Note: morse::noise_() and morse::trelis_() statics have been moved to
 * class member variables (see bmorse.h changes). The morse object itself
 * already encapsulated most state — this finishes the job.
 */

#ifndef BMORSE_PROCSTATE_H
#define BMORSE_PROCSTATE_H

#include <cstdlib>
#include <cstddef>
#include "bmorse.h"

struct ProcessState {
    // --- filter() state ---
    double flt_in[2000];
    double flt_out;
    int    flt_i;
    int    flt_pint;
    int    flt_empty;       // 1 = not yet initialized

    // --- apply_window() state ---
    int    win_len;         // cached window length (0 = not yet cached)

    // --- rx_FFTprocess() state ---
    int    smpl_ctr;        // sample counter for decimation
    double FFTvalue;        // envelope magnitude after filter
    double FFTphase;        // phase accumulator for baseband mixing

    // --- process_data() state ---
    int        pd_sample_counter;
    float      pd_rn;       // noise estimate (init: 0.1)
    int        pd_retstat;
    int        pd_n1, pd_n2;
    long int   pd_imax, pd_xhat, pd_elmhat;
    float      pd_pmax, pd_zout, pd_spdhat, pd_px;
    int        pd_init;     // 1 = morse object not yet created
    int        pd_pinit;    // 1 = header not yet printed
    double     pd_agc_peak;
    morse     *pd_mp;       // Bayesian Viterbi decoder — owned by this state

    // --- libbmorse output (written by process_data, read by bmorse_feed) ---
    char       outbuf[4096];
    int        outlen;
    float      spdhat;
};

/**
 * Allocate and initialize a ProcessState with correct starting values.
 * Returns NULL on allocation failure.
 */
inline ProcessState* process_state_create()
{
    ProcessState* st = (ProcessState*)calloc(1, sizeof(ProcessState));
    if (!st) return NULL;
    // Override fields whose zero-init isn't the correct initial value
    st->flt_empty = 1;
    st->pd_rn     = 0.1f;
    st->pd_init   = 1;   // triggers morse object creation on first process_data call
    st->pd_pinit  = 1;
    return st;
}

/**
 * Destroy a ProcessState, including the owned morse object.
 */
inline void process_state_destroy(ProcessState* st)
{
    if (!st) return;
    if (st->pd_mp) {
        delete st->pd_mp;
        st->pd_mp = NULL;
    }
    free(st);
}

#endif /* BMORSE_PROCSTATE_H */
