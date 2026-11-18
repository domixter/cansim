/* cansim: czesc wspolna kompilowana razem z firmware. Laczy skompilowany kod z modelem w Pythonie. */
#include <stdint.h>
#include <string.h>
#include "cansim_rt.h"
#include <stm32_device.h>   /* wrapper wygenerowany przez cansim, wciaga oficjalny naglowek ST */

typedef uint32_t (*rd_t)(int, uint32_t);
typedef void (*wr_t)(int, uint32_t, uint32_t);
typedef void (*tr_t)(const char *);
typedef void (*nv_t)(int, int, uint32_t);
typedef uint32_t (*nvg_t)(int, int);
typedef void (*wt_t)(uint32_t);
static rd_t g_rd; static wr_t g_wr; static tr_t g_tr; static nv_t g_nv; static nvg_t g_nvg; static wt_t g_wt;

extern uint32_t *const cansim_reg_base_tab[];

uint8_t cansim_msgram[16384] __attribute__((aligned(4)));
const uint32_t cansim_msgram_size = sizeof(cansim_msgram);
volatile uint32_t cansim_primask;
uint32_t cansim_spin_cnt;
const char *volatile cansim_loc_file = "";
volatile int cansim_loc_line;

SysTick_Type cansim_core_systick; SCB_Type cansim_core_scb;
DWT_Type cansim_core_dwt; CoreDebug_Type cansim_core_coredebug;

/* SystemCoreClock definiujemy tylko, gdy firmware nie ma wlasnej definicji (wykrywa build.py). Zmienna "weak" nie
   jest eksportowana z DLL na Windows, dlatego nie uzywamy tu __attribute__((weak)). */
#ifndef CANSIM_FW_HAS_SYSCLK
uint32_t SystemCoreClock = 64000000u;
#endif
#ifndef CANSIM_FW_HAS_SYSINIT
void SystemInit(void) {}
#endif
#ifndef CANSIM_FW_HAS_SYSCLKUPD
void SystemCoreClockUpdate(void) {}
#endif

void cansim_set_callbacks(rd_t a, wr_t b, tr_t c, nv_t d, nvg_t e, wt_t f) {
  g_rd = a; g_wr = b; g_tr = c; g_nv = d; g_nvg = e; g_wt = f;
}
uint32_t cansim_rd(int id, const volatile void *p) {
  uint32_t off = (uint32_t)((const char *)p - (const char *)cansim_reg_base_tab[id]);
  return g_rd ? g_rd(id, off) : *(const volatile uint32_t *)p;
}
void cansim_wr(int id, volatile void *p, uint32_t v) {
  uint32_t off = (uint32_t)((const char *)p - (const char *)cansim_reg_base_tab[id]);
  if (g_wr) g_wr(id, off, v); else *(volatile uint32_t *)p = v;
}
void cansim_trace(const char *fn) { if (g_tr) g_tr(fn); }
void cansim_advance_us(uint32_t us) { if (g_wt) g_wt(us); }
void cansim_wfi(void) { if (g_wt) g_wt(0xFFFFFFFFu); }
void cansim_nvic(int op, int irq, uint32_t val) { if (g_nv) g_nv(op, irq, val); }
uint32_t cansim_nvic_get(int op, int irq) { return g_nvg ? g_nvg(op, irq) : 0; }
void cansim_set_primask(uint32_t v) { cansim_primask = v; }
uint32_t cansim_get_primask(void) { return cansim_primask; }
uint32_t cansim_systick_config(uint32_t ticks) {
  if (ticks - 1u > 0xFFFFFFu) return 1u;
  cansim_core_systick.LOAD = ticks - 1u;
  cansim_core_systick.VAL = 0;
  cansim_core_systick.CTRL = 7u;
  return 0u;
}
