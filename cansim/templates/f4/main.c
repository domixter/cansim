/* Szablon cansim (STM32F407, bxCAN CAN1, 250 kbit/s, ODrive CANSimple). Punkt startowy do wlasnego kodu.
 * Rejestry wprost (bare metal), bez HAL. APB1 = 42 MHz. */
#include "stm32f4xx.h"
#include <stdint.h>

#define N_AXES 4

volatile uint32_t ms_tick;
volatile uint32_t rx_frames;
volatile uint32_t hb_count[N_AXES + 1];
volatile uint32_t axis_state[N_AXES + 1];
volatile uint32_t tx_busy_drops;

void SysTick_Handler(void) { ms_tick++; }

static void delay_ms(uint32_t d) {
  uint32_t s = ms_tick;
  while ((ms_tick - s) < d) { __NOP(); }
}

static void can_init(void) {
  RCC->AHB1ENR |= RCC_AHB1ENR_GPIODEN;
  RCC->APB1ENR |= RCC_APB1ENR_CAN1EN;
  GPIOD->MODER = (GPIOD->MODER & ~0xFu) | 0xAu;             /* PD0 (RX), PD1 (TX): funkcja alternatywna */
  GPIOD->AFR[0] = (GPIOD->AFR[0] & ~0xFFu) | 0x99u;         /* AF9 = CAN1 */

  CAN1->MCR &= ~CAN_MCR_SLEEP;                              /* wyjscie z uspienia */
  while (CAN1->MSR & CAN_MSR_SLAK) { }
  CAN1->MCR |= CAN_MCR_INRQ;                                /* tryb inicjalizacji */
  while ((CAN1->MSR & CAN_MSR_INAK) == 0u) { }

  CAN1->MCR |= CAN_MCR_ABOM;                                /* automatyczne wyjscie z Bus-Off */
  CAN1->BTR = (1u << CAN_BTR_SJW_Pos) | (10u << CAN_BTR_TS1_Pos) | (1u << CAN_BTR_TS2_Pos) | 11u;   /* 42 MHz / 12 / 14 tq = 250 kbit/s */

  CAN1->FMR |= CAN_FMR_FINIT;                               /* konfiguracja filtrow: FINIT=1 */
  CAN1->FA1R &= ~1u;
  CAN1->FS1R |= 1u;                                         /* bank 0: 32 bity */
  CAN1->FM1R &= ~1u;                                        /* tryb maski */
  CAN1->FFA1R &= ~1u;                                       /* do FIFO0 */
  CAN1->sFilterRegister[0].FR1 = (0x01u << 21);             /* cmd = 1 (heartbeat) */
  CAN1->sFilterRegister[0].FR2 = (0x1Fu << 21) | (1u << 2); /* porownaj 5 mlodszych bitow ID i bit IDE */
  CAN1->FA1R |= 1u;                                         /* aktywuj bank */
  CAN1->FMR &= ~CAN_FMR_FINIT;                              /* koniec konfiguracji filtrow */

  CAN1->IER |= CAN_IER_FMPIE0;                              /* przerwanie przy odebranej ramce w FIFO0 */
  NVIC_SetPriority(CAN1_RX0_IRQn, 3);
  NVIC_EnableIRQ(CAN1_RX0_IRQn);

  CAN1->MCR &= ~CAN_MCR_INRQ;                               /* start */
  while (CAN1->MSR & CAN_MSR_INAK) { }
}

static int can_send(uint32_t id, uint32_t d0, uint32_t d1, uint32_t mbox) {
  if ((CAN1->TSR & (CAN_TSR_TME0 << mbox)) == 0u) { tx_busy_drops++; return 0; }
  CAN1->sTxMailBox[mbox].TIR = id << 21;
  CAN1->sTxMailBox[mbox].TDTR = 8u;
  CAN1->sTxMailBox[mbox].TDLR = d0;
  CAN1->sTxMailBox[mbox].TDHR = d1;
  CAN1->sTxMailBox[mbox].TIR |= CAN_TI0R_TXRQ;
  return 1;
}

void CAN1_RX0_IRQHandler(void) {
  while (CAN1->RF0R & CAN_RF0R_FMP0) {
    uint32_t id = (CAN1->sFIFOMailBox[0].RIR >> 21) & 0x7FFu;
    uint32_t hi = CAN1->sFIFOMailBox[0].RDHR;
    uint32_t node = id >> 5;
    rx_frames++;
    if (node >= 1u && node <= N_AXES) {
      hb_count[node]++;
      axis_state[node] = hi & 0xFFu;
    }
    CAN1->RF0R |= CAN_RF0R_RFOM0;                           /* zwolnij ramke */
  }
}

union f2u { float f; uint32_t u; };

int main(void) {
  SysTick_Config(SystemCoreClock / 1000u);
  can_init();
  for (uint32_t n = 1; n <= N_AXES; n++) {
    can_send((n << 5) | 0x07u, 8u, 0u, 0u);
    delay_ms(2);
  }
  union f2u v;
  for (;;) {
    v.f = 2.0f;
    for (uint32_t n = 1; n <= N_AXES; n++) {
      can_send((n << 5) | 0x0Du, v.u, 0u, 0u);
      delay_ms(2);
    }
    delay_ms(17);
  }
}
