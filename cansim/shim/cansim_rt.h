/* cansim: kod pomocniczy wstrzykiwany do skompilowanego firmware. */
#ifndef CANSIM_RT_H
#define CANSIM_RT_H
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
uint32_t cansim_rd(int id, const volatile void *p);
void     cansim_wr(int id, volatile void *p, uint32_t v);
void     cansim_trace(const char *fn);
void     cansim_advance_us(uint32_t us);
extern uint8_t cansim_msgram[];
extern uint32_t cansim_spin_cnt;
extern const char *volatile cansim_loc_file;   /* plik i linia ostatniego dostepu do rejestru (do komunikatow) */
extern volatile int cansim_loc_line;
#define CANSIM_LOC()          (cansim_loc_file = __FILE__, cansim_loc_line = __LINE__)
#define CANSIM_SPIN() do { if (++cansim_spin_cnt >= 2000u) { cansim_spin_cnt = 0; cansim_advance_us(20u); } } while (0)
#define CANSIM_RD(id, lv)     (CANSIM_LOC(), (__typeof__(lv))cansim_rd((id), (const volatile void *)&(lv)))
#define CANSIM_WR(id, lv, v)  (CANSIM_LOC(), cansim_wr((id), (volatile void *)&(lv), (uint32_t)(v)))
#define CANSIM_FN(name)       cansim_trace(name)
#ifdef __cplusplus
}
#endif
#endif
