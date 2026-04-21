/*
 * fb_core.c — Fast 2-state HMM forward-backward for ITILA CW decoder
 *
 * Compile: gcc -O3 -march=native -ffast-math -shared -fPIC -o fb_core.so fb_core.c -lm
 *
 * Interface: fb_core(log_B, log_T, T, log_alpha, log_beta, log_Z_out)
 *   log_B:     T*2 float64 array (row-major), log observation likelihoods
 *   log_T:     4 float64 (row-major 2x2), log transition matrix
 *              log_T[0]=T[0,0], log_T[1]=T[0,1], log_T[2]=T[1,0], log_T[3]=T[1,1]
 *   T:         number of timesteps
 *   log_alpha: T*2 float64 output, forward log-probs
 *   log_beta:  T*2 float64 output, backward log-probs
 *   log_Z_out: scalar float64 output, log normalizer
 */

#include <math.h>

static inline double logaddexp_pair(double a, double b) {
    if (a > b) {
        double diff = b - a;
        return a + (diff < -40.0 ? 0.0 : log1p(exp(diff)));
    } else {
        double diff = a - b;
        return b + (diff < -40.0 ? 0.0 : log1p(exp(diff)));
    }
}

void fb_core(const double* log_B, const double* log_T, int T,
             double* log_alpha, double* log_beta, double* log_Z_out) {
    /* log_T layout (row-major 2x2):
     *   log_T[0] = log P(j=0 | i=0) = log(1-p01)  SPACE->SPACE
     *   log_T[1] = log P(j=1 | i=0) = log(p01)    SPACE->MARK
     *   log_T[2] = log P(j=0 | i=1) = log(p10)    MARK->SPACE
     *   log_T[3] = log P(j=1 | i=1) = log(1-p10)  MARK->MARK
     */
    const double log05 = -0.6931471805599453;  /* log(0.5) */
    const double lT00 = log_T[0], lT01 = log_T[1];
    const double lT10 = log_T[2], lT11 = log_T[3];

    /* Forward pass */
    log_alpha[0] = log05 + log_B[0];
    log_alpha[1] = log05 + log_B[1];

    for (int t = 1; t < T; t++) {
        double a0 = log_alpha[(t-1)*2];
        double a1 = log_alpha[(t-1)*2 + 1];
        log_alpha[t*2]     = log_B[t*2]     + logaddexp_pair(lT00 + a0, lT10 + a1);
        log_alpha[t*2 + 1] = log_B[t*2 + 1] + logaddexp_pair(lT01 + a0, lT11 + a1);
    }

    *log_Z_out = logaddexp_pair(log_alpha[(T-1)*2], log_alpha[(T-1)*2 + 1]);

    /* Backward pass */
    log_beta[(T-1)*2]     = 0.0;
    log_beta[(T-1)*2 + 1] = 0.0;

    for (int t = T-2; t >= 0; t--) {
        double lb0 = log_B[(t+1)*2]     + log_beta[(t+1)*2];
        double lb1 = log_B[(t+1)*2 + 1] + log_beta[(t+1)*2 + 1];
        log_beta[t*2]     = logaddexp_pair(lT00 + lb0, lT01 + lb1);
        log_beta[t*2 + 1] = logaddexp_pair(lT10 + lb0, lT11 + lb1);
    }
}
