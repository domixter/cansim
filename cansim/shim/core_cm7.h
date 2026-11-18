/* cansim: minimalny zamiennik core_cm7.h (CMSIS-Core) do kompilacji na PC.
 * Prawdziwy plik zawiera asembler ARM, ktorego gcc na x86 nie skompiluje.
 * Tu: tylko typy, makra i funkcje, ktorych oficjalne naglowki urzadzen i typowy kod uzywaja.
 * Brakuje czegos? Dopisz tutaj (to zwykly plik C). */
#ifndef CANSIM_CORE_CM_H
#define CANSIM_CORE_CM_H
#include <stdint.h>
#include <stddef.h>

#define __I     volatile const
#define __O     volatile
#define __IO    volatile
#define __IM    volatile const
#define __OM    volatile
#define __IOM   volatile
#define __STATIC_INLINE        static inline
#define __STATIC_FORCEINLINE   static inline
#define __NO_RETURN            __attribute__((noreturn))
#define __WEAK                 __attribute__((weak))
#define __USED                 __attribute__((used))
#define __ALIGNED(x)           __attribute__((aligned(x)))
#define __PACKED               __attribute__((packed))
#define __RAM_FUNC
extern uint32_t cansim_spin_cnt; void cansim_advance_us(uint32_t us);
#define __NOP()  do { if (++cansim_spin_cnt >= 2000u) { cansim_spin_cnt = 0; cansim_advance_us(20u); } } while (0)
#define __DSB()  ((void)0)
#define __ISB()  ((void)0)
#define __DMB()  ((void)0)
#define __WFI()  cansim_wfi()
#define __WFE()  cansim_wfi()
#define __BKPT(x) ((void)0)
#define __CLZ(x)  ((uint32_t)__builtin_clz(x))
#define __REV(x)  __builtin_bswap32(x)

#ifdef __cplusplus
extern "C" {
#endif

/* hooki z cansim_rt.c */
void cansim_nvic(int op, int irq, uint32_t val);
uint32_t cansim_nvic_get(int op, int irq);
void cansim_wfi(void);
void cansim_set_primask(uint32_t v);
uint32_t cansim_get_primask(void);
uint32_t cansim_systick_config(uint32_t ticks);

static inline void __enable_irq(void)  { cansim_set_primask(0); }
static inline void __disable_irq(void) { cansim_set_primask(1); }
static inline uint32_t __get_PRIMASK(void) { return cansim_get_primask(); }
static inline void __set_PRIMASK(uint32_t v) { cansim_set_primask(v); }

/* NVIC: op 0 enable, 1 disable, 2 set priority, 3 set pending, 4 clear pending */
static inline void NVIC_EnableIRQ(IRQn_Type n)  { cansim_nvic(0, (int)n, 0); }
static inline void NVIC_DisableIRQ(IRQn_Type n) { cansim_nvic(1, (int)n, 0); }
static inline void NVIC_SetPriority(IRQn_Type n, uint32_t p) { cansim_nvic(2, (int)n, p); }
static inline uint32_t NVIC_GetPriority(IRQn_Type n) { return cansim_nvic_get(2, (int)n); }
static inline void NVIC_SetPendingIRQ(IRQn_Type n)   { cansim_nvic(3, (int)n, 0); }
static inline void NVIC_ClearPendingIRQ(IRQn_Type n) { cansim_nvic(4, (int)n, 0); }
static inline uint32_t NVIC_GetPendingIRQ(IRQn_Type n) { return cansim_nvic_get(3, (int)n); }
static inline uint32_t NVIC_GetEnableIRQ(IRQn_Type n)  { return cansim_nvic_get(0, (int)n); }
static inline void NVIC_SystemReset(void) { cansim_nvic(9, 0, 0); }
static inline void NVIC_SetPriorityGrouping(uint32_t g) { (void)g; }
static inline uint32_t NVIC_EncodePriority(uint32_t g, uint32_t p, uint32_t s) { (void)g; (void)s; return p; }

/* rejestry rdzenia: zwykla pamiec (bez modelu) */
typedef struct { volatile uint32_t CTRL, LOAD, VAL; volatile const uint32_t CALIB; } SysTick_Type;
typedef struct {
  volatile const uint32_t CPUID; volatile uint32_t ICSR, VTOR, AIRCR, SCR, CCR;
  volatile uint8_t SHP[12]; volatile uint32_t SHCSR, CFSR, HFSR, DFSR, MMFAR, BFAR, AFSR;
  volatile const uint32_t PFR[2], DFR, ADR, MMFR[4], ISAR[5], RESERVED0[5];
  volatile uint32_t CPACR;
} SCB_Type;
typedef struct { volatile uint32_t CTRL, CYCCNT, CPICNT, EXCCNT, SLEEPCNT, LSUCNT, FOLDCNT; } DWT_Type;
typedef struct { volatile uint32_t DHCSR, DCRSR, DCRDR, DEMCR; } CoreDebug_Type;
extern SysTick_Type cansim_core_systick; extern SCB_Type cansim_core_scb;
extern DWT_Type cansim_core_dwt; extern CoreDebug_Type cansim_core_coredebug;
#define SysTick   (&cansim_core_systick)
#define SCB       (&cansim_core_scb)
#define DWT       (&cansim_core_dwt)
#define CoreDebug (&cansim_core_coredebug)
#define SysTick_CTRL_ENABLE_Msk     (1UL << 0)
#define SysTick_CTRL_TICKINT_Msk    (1UL << 1)
#define SysTick_CTRL_CLKSOURCE_Msk  (1UL << 2)
#define SCB_CPACR_CP10_Pos 20U
#define SCB_CPACR_CP11_Pos 22U
static inline uint32_t SysTick_Config(uint32_t ticks) { return cansim_systick_config(ticks); }
static inline void SCB_EnableICache(void) {}
static inline void SCB_DisableICache(void) {}
static inline void SCB_EnableDCache(void) {}
static inline void SCB_DisableDCache(void) {}
static inline void SCB_CleanDCache(void) {}
static inline void SCB_CleanDCache_by_Addr(uint32_t *a, int32_t s) { (void)a; (void)s; }
static inline void SCB_InvalidateDCache_by_Addr(uint32_t *a, int32_t s) { (void)a; (void)s; }
static inline void SCB_CleanInvalidateDCache_by_Addr(uint32_t *a, int32_t s) { (void)a; (void)s; }

#ifdef __cplusplus
}
#endif
#endif
