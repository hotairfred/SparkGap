/* Minimal C capture — no threads, no ring buffer */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <stdint.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <time.h>

int main(int argc, char **argv) {
    if (argc != 3) { fprintf(stderr, "Usage: %s <duration_sec> <out_file>\n", argv[0]); return 1; }
    int duration = atoi(argv[1]);

    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    int rcvbuf = 32 * 1024 * 1024;
    setsockopt(sock, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof(rcvbuf));
    int reuse = 1;
    setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));

    struct sockaddr_in bind_addr = {0};
    bind_addr.sin_family = AF_INET;
    bind_addr.sin_addr.s_addr = INADDR_ANY;
    bind_addr.sin_port = htons(1024);
    bind(sock, (struct sockaddr *)&bind_addr, sizeof(bind_addr));

    struct sockaddr_in pitaya = {0};
    pitaya.sin_family = AF_INET;
    pitaya.sin_addr.s_addr = inet_addr("192.168.1.54");
    pitaya.sin_port = htons(1024);

    /* STOP, START, then config + freq for rx2=14090 */
    uint8_t stop[64] = {0xEF, 0xFE, 0x04, 0x00};
    sendto(sock, stop, 64, 0, (struct sockaddr *)&pitaya, sizeof(pitaya));
    usleep(200000);
    uint8_t start[64] = {0xEF, 0xFE, 0x04, 0x01};
    sendto(sock, start, 64, 0, (struct sockaddr *)&pitaya, sizeof(pitaya));
    usleep(100000);

    uint8_t pkt[1032] = {0};
    pkt[0] = 0xEF; pkt[1] = 0xFE; pkt[2] = 0x01; pkt[3] = 0x02;
    pkt[8] = 0x7F; pkt[9] = 0x7F; pkt[10] = 0x7F;
    pkt[11] = 0x00; pkt[12] = 0x02; pkt[13] = 0x00; pkt[14] = 0x00; pkt[15] = (1<<2)|(7<<3);
    pkt[520] = 0x7F; pkt[521] = 0x7F; pkt[522] = 0x7F;
    pkt[523] = 0x14; pkt[524] = 0; pkt[525] = 0; pkt[526] = 0; pkt[527] = 40;
    sendto(sock, pkt, 1032, 0, (struct sockaddr *)&pitaya, sizeof(pitaya));
    usleep(50000);

    uint32_t freq = (uint32_t)((double)14090000 * 0.9999961);
    pkt[523] = 0x08;  /* rx2 = (2+2)*2 = 8 */
    pkt[524] = (freq >> 24) & 0xFF;
    pkt[525] = (freq >> 16) & 0xFF;
    pkt[526] = (freq >> 8) & 0xFF;
    pkt[527] = freq & 0xFF;
    pkt[4]=0; pkt[5]=0; pkt[6]=0; pkt[7]=1;
    sendto(sock, pkt, 1032, 0, (struct sockaddr *)&pitaya, sizeof(pitaya));
    usleep(500000);

    /* Open output as binary float32 IQ pairs */
    FILE *out = fopen(argv[2], "wb");
    if (!out) { perror("fopen"); return 1; }

    /* Receive until duration elapsed */
    uint8_t buf[2048];
    struct timespec t_start, t_now;
    clock_gettime(CLOCK_MONOTONIC, &t_start);
    int n_pkts = 0;
    int n_samples = 0;

    while (1) {
        int len = recv(sock, buf, sizeof(buf), 0);
        if (len < 1032) continue;
        if (buf[0] != 0xEF || buf[1] != 0xFE || buf[2] != 0x01 || buf[3] != 0x06) continue;
        n_pkts++;

        /* Parse rx2 from both frames */
        for (int frame_off = 16; frame_off <= 528; frame_off += 512) {
            uint8_t *iq_data = buf + frame_off;
            for (int g = 0; g < 10; g++) {
                uint8_t *s = iq_data + g * 50 + 2 * 6;  /* rx2 at offset 12 */
                int32_t iv = (s[0]<<16)|(s[1]<<8)|s[2];
                if (iv >= 0x800000) iv -= 0x1000000;
                int32_t qv = (s[3]<<16)|(s[4]<<8)|s[5];
                if (qv >= 0x800000) qv -= 0x1000000;
                float i_f = (float)iv;
                float q_f = -(float)qv;  /* negate Q like our pipeline */
                fwrite(&i_f, sizeof(float), 1, out);
                fwrite(&q_f, sizeof(float), 1, out);
                n_samples++;
            }
        }

        clock_gettime(CLOCK_MONOTONIC, &t_now);
        double elapsed = (t_now.tv_sec - t_start.tv_sec) + (t_now.tv_nsec - t_start.tv_nsec)/1e9;
        if (elapsed >= duration) break;
    }

    fclose(out);
    sendto(sock, stop, 64, 0, (struct sockaddr *)&pitaya, sizeof(pitaya));
    close(sock);
    fprintf(stderr, "Captured %d packets, %d samples in %d seconds\n", n_pkts, n_samples, duration);
    return 0;
}
