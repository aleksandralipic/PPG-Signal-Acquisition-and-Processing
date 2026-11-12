#include "max30100.h"

uint8_t MAX30100_get_device_ID(void) {
    static uint8_t wr[1];
    static uint8_t rd[1];
    wr[0] = MAX30100_PART_ID;
    I2C2_WriteRead(MAX30100_SLAVE_ADDR, wr, 1, rd, 1);
    while (I2C2_IsBusy());
    return rd[0];
}

uint8_t MAX30100_read_int_status(void) {
    uint8_t citaj_vr[1], citaj_reg[1];
    citaj_reg[0] = MAX30100_INT_STATUS;
    I2C2_WriteRead(MAX30100_SLAVE_ADDR, citaj_reg, 1, citaj_vr, 1);
    while (I2C2_IsBusy());
    return citaj_vr[0];
}

// Samostalna funkcija za promenu struje dioda u toku rada
void MAX30100_set_led_current(MAX30100_LED_CURRENT_t red_led_current, MAX30100_LED_CURRENT_t ir_led_current) {
    static uint8_t wr[2];
    wr[0] = MAX30100_LED_CFG;
    wr[1] = (red_led_current << 4) | ir_led_current;
    I2C2_Write(MAX30100_SLAVE_ADDR, wr, 2);
    while (I2C2_IsBusy());
}

void MAX30100_init(MAX30100_MODE_t mode, MAX30100_LED_CURRENT_t red_led_current, MAX30100_LED_CURRENT_t ir_led_current, MAX30100_SAMPLE_RATE_t sample_rate, MAX30100_LED_PULSE_WIDTH_t pulse_width) {
    static uint8_t wr[2];

    // Podesavanje moda rada
    wr[0] = MAX30100_MODE_CFG;
    wr[1] = mode;
    I2C2_Write(MAX30100_SLAVE_ADDR, wr, 2);
    while (I2C2_IsBusy());

    // Podesavanje struje za RED i IR LED
    MAX30100_set_led_current(red_led_current, ir_led_current);

    // Podesavanje SpO2 - sample rate i pulse width
    wr[0] = MAX30100_SPO2_CFG;
    wr[1] = (sample_rate << 2) | pulse_width;
    I2C2_Write(MAX30100_SLAVE_ADDR, wr, 2);
    while (I2C2_IsBusy());

    // Podesavanje interrupt-a (A_FULL / HR_RDY)
    wr[0] = MAX30100_INT_ENABLE;
    wr[1] = 0x10;
    I2C2_Write(MAX30100_SLAVE_ADDR, wr, 2);
    while (I2C2_IsBusy());
}

void MAX30100_FIFO_init(void) {
    static uint8_t wr[2];
    wr[0] = MAX30100_FIFO_WR_PTR;
    wr[1] = 0x00;
    I2C2_Write(MAX30100_SLAVE_ADDR, wr, 2);
    while (I2C2_IsBusy());

    wr[0] = MAX30100_FIFO_OVERFLOW_CNT;
    I2C2_Write(MAX30100_SLAVE_ADDR, wr, 2);
    while (I2C2_IsBusy());

    wr[0] = MAX30100_FIFO_RD_PTR;
    I2C2_Write(MAX30100_SLAVE_ADDR, wr, 2);
    while (I2C2_IsBusy());
}

void MAX30100_read_led_data(MAX30100_sens_t* sens) {
    static uint8_t wr_data;
    static uint8_t tmp_reg[4];

    wr_data = MAX30100_FIFO_DATA;
    I2C2_WriteRead(MAX30100_SLAVE_ADDR, &wr_data, 1, tmp_reg, 4);
    while (I2C2_IsBusy());

    sens->ir_led = (tmp_reg[0] << 8) | tmp_reg[1];
    sens->red_led = (tmp_reg[2] << 8) | tmp_reg[3];
}

void MAX30100_reset(void) {
    static uint8_t wr[2];
    wr[0] = MAX30100_MODE_CFG;
    wr[1] = MAX30100_RESET;
    I2C2_Write(MAX30100_SLAVE_ADDR, wr, 2);
    while (I2C2_IsBusy());
}

void MAX30100_enable_temp_read(void) {
    static uint8_t wr[2];
    static uint8_t mode_cfg_reg;

    wr[0] = MAX30100_MODE_CFG;
    I2C2_WriteRead(MAX30100_SLAVE_ADDR, wr, 1, &mode_cfg_reg, 1);
    while (I2C2_IsBusy());

    wr[1] = mode_cfg_reg | 0x08;
    I2C2_Write(MAX30100_SLAVE_ADDR, wr, 2);
    while (I2C2_IsBusy());
}

void MAX30100_read_temp_data(MAX30100_sens_t* sens) {
    static uint8_t wr;
    static uint8_t temp_int1;
    static int8_t temp_int2;
    static uint8_t temp_frac;

    wr = MAX30100_TEMP_INTEGER;
    I2C2_WriteRead(MAX30100_SLAVE_ADDR, &wr, 1, &temp_int1, 1);
    while (I2C2_IsBusy());
    temp_int2 = (int8_t)temp_int1;

    wr = MAX30100_TEMP_FRACTION;
    I2C2_WriteRead(MAX30100_SLAVE_ADDR, &wr, 1, &temp_frac, 1);
    while (I2C2_IsBusy());

    sens->temperature = (float)(temp_int2 + temp_frac * 0.0625);
}

uint32_t srednja_vrednost(uint16_t *niz, uint8_t N) {
    uint32_t suma = 0;
    for (uint8_t i = 0; i < N; i++) {
        suma += niz[i];
    }
    return (uint32_t)(suma / N);
}

int16_t pik_detekcija_min(int16_t *ring_buf, int16_t *pik_vrednost) {
    *pik_vrednost = ring_buf[0];
    for (uint8_t i = 1; i < 100; i++) {
        if (ring_buf[i] < *pik_vrednost) {
            *pik_vrednost = ring_buf[i];
        }
    }
    return *pik_vrednost;
}