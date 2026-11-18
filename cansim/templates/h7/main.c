/* cansim demo (STM32H743, FDCAN1, 250 kbit/s, ODrive CANSimple). Prosty, poprawny wzorzec do porownan.
 * Szablon cansim: punkt startowy do wlasnego kodu. Rejestry wprost (bare metal), bez HAL. */
#include "stm32h7xx.h"
#include <stdint.h>

#define MSGRAM_BASE 0x4000AC00UL       /* cansim przekierowuje te makro na tablice w pamieci PC */
#define N_AXES      4
#define FLT_OFF     0u                 /* 8 filtrow x 4 B */
#define TX_OFF      32u                /* 4 bufory Tx x 16 B */
#define RX_OFF      96u                /* 8 buforow Rx x 16 B (filtry zajmuja 0..31, Tx 32..95) */

volatile uint32_t ms_tick;
volatile uint32_t rx_frames;
volatile uint32_t hb_count[N_AXES + 1];
volatile uint32_t axis_state[N_AXES + 1];
volatile uint32_t tx_busy_drops;
volatile uint32_t battery_mv;          /* z ramki 0x100 plytki zasilania (jesli jest na magistrali) */
volatile uint32_t power_frames;

void SysTick_Handler(void) { ms_tick++; }

static void delay_ms(uint32_t d) {
  uint32_t s = ms_tick;
  while ((ms_tick - s) < d) { __NOP(); }
}

static volatile uint32_t *ram(uint32_t off) { return (volatile uint32_t *)(MSGRAM_BASE + off); }

static void can_init(void) {
  RCC->APB1HENR |= RCC_APB1HENR_FDCANEN;
  GPIOD->MODER = (GPIOD->MODER & ~(0xFu)) | 0xAu;          /* PD0, PD1: funkcja alternatywna */
  GPIOD->AFR[0] = (GPIOD->AFR[0] & ~0xFFu) | 0x99u;        /* AF9 = FDCAN1 */

  FDCAN1->CCCR |= FDCAN_CCCR_INIT;
  while ((FDCAN1->CCCR & FDCAN_CCCR_INIT) == 0u) { }       /* czekaj, az INIT sie utrwali */
  FDCAN1->CCCR |= FDCAN_CCCR_CCE;

  FDCAN1->NBTP = (3u << FDCAN_NBTP_NSJW_Pos) | (7u << FDCAN_NBTP_NBRP_Pos) |
                 (24u << FDCAN_NBTP_NTSEG1_Pos) | (5u << FDCAN_NBTP_NTSEG2_Pos);  /* 250 kbit/s przy 64 MHz */

  for (uint32_t i = 0; i < N_AXES; i++) {                  /* heartbeat node i+1 -> bufor Rx i */
    uint32_t id = ((i + 1u) << 5) | 0x01u;
    *ram(FLT_OFF + 4u * i) = (7u << 27) | (id << 16) | i;
  }
  *ram(FLT_OFF + 4u * N_AXES) = (7u << 27) | (0x100u << 16) | N_AXES;   /* 0x100 (plytka zasilania) -> bufor Rx 4 */
  FDCAN1->GFC = (3u << FDCAN_GFC_ANFS_Pos) | (3u << FDCAN_GFC_ANFE_Pos) | FDCAN_GFC_RRFS | FDCAN_GFC_RRFE;
  FDCAN1->SIDFC = ((N_AXES + 1u) << FDCAN_SIDFC_LSS_Pos) | FLT_OFF;
  FDCAN1->TXBC = (N_AXES << FDCAN_TXBC_NDTB_Pos) | TX_OFF;
  FDCAN1->TXESC = 0;
  FDCAN1->RXESC = 0;
  FDCAN1->RXBC = RX_OFF;
  FDCAN1->RXF0C = 0;
  FDCAN1->IE = FDCAN_IE_DRXE;
  FDCAN1->ILS = 0;
  FDCAN1->ILE = FDCAN_ILE_EINT0;
  NVIC_SetPriority(FDCAN1_IT0_IRQn, 3);
  NVIC_EnableIRQ(FDCAN1_IT0_IRQn);
  FDCAN1->CCCR &= ~FDCAN_CCCR_INIT;                        /* start */
}

static int can_send(uint32_t id, uint32_t d0, uint32_t d1, uint32_t buf) {
  if (FDCAN1->TXBRP & (1u << buf)) { tx_busy_drops++; return 0; }
  volatile uint32_t *e = ram(TX_OFF + 16u * buf);
  e[0] = id << 18;
  e[1] = 8u << 16;
  e[2] = d0;
  e[3] = d1;
  FDCAN1->TXBAR = 1u << buf;
  return 1;
}

void FDCAN1_IT0_IRQHandler(void) {
  FDCAN1->IR = FDCAN_IR_DRX;
  if (FDCAN1->NDAT1 & (1u << N_AXES)) {
    FDCAN1->NDAT1 = 1u << N_AXES;
    battery_mv = ram(RX_OFF + 16u * N_AXES)[2] & 0xFFFFu;   /* bajty 0..1: napiecie w mV */
    power_frames++;
  }
  for (uint32_t i = 0; i < N_AXES; i++) {
    if (FDCAN1->NDAT1 & (1u << i)) {
      FDCAN1->NDAT1 = 1u << i;
      volatile uint32_t *e = ram(RX_OFF + 16u * i);
      uint32_t id = (e[0] >> 18) & 0x7FFu;
      uint32_t node = id >> 5;
      rx_frames++;
      if (node >= 1u && node <= N_AXES) {
        hb_count[node]++;
        axis_state[node] = e[3] & 0xFFu;                   /* bajt 4 heartbeatu */
      }
    }
  }
}

union f2u { float f; uint32_t u; };

int main(void) {
  SysTick_Config(SystemCoreClock / 1000u);
  can_init();
  for (uint32_t n = 1; n <= N_AXES; n++) {                 /* CLOSED_LOOP */
    can_send((n << 5) | 0x07u, 8u, 0u, n - 1u);
    delay_ms(2);
  }
  union f2u v;
  for (;;) {
    v.f = 2.0f;                                             /* 2 obr/s */
    for (uint32_t n = 1; n <= N_AXES; n++) can_send((n << 5) | 0x0Du, v.u, 0u, n - 1u);
    delay_ms(25);
  }
}
