/*******************************************************************************
  Main Source File - Citanje sirovih podataka sa MAX30100 + Binarni Protokol
  sa dinami?kom promenom struje dioda
 *******************************************************************************/

#include <stddef.h>
#include <stdbool.h>
#include <stdlib.h>
#include <stdio.h>
#include "definitions.h"

#include "FreeRTOS.h"
#include "task.h"
#include "semphr.h"

#include "max30100.h"

// Globalne promenljive za komunikaciju izmedju prekida i zadatka
MAX30100_sens_t senzor;
SemaphoreHandle_t xDataReadySemaphore;
static uint8_t packet_seq = 0; // Brojac paketa (0 - 255)

// --- Funkcija za racunanje CRC16 CCITT algoritma ---
uint16_t Calculate_CRC16(const uint8_t *data, size_t length) {
    uint16_t crc = 0xFFFF;
    for (size_t i = 0; i < length; i++) {
        crc ^= (uint16_t)data[i] << 8;
        for (uint8_t bit = 0; bit < 8; bit++) {
            if (crc & 0x8000) {
                crc = (crc << 1) ^ 0x1021;
            } else {
                crc = (crc << 1);
            }
        }
    }
    return crc;
}

// --- Funkcija za pakovanje i slanje bajtova na UART ---
void UART_Send_Sample(uint16_t ir, uint16_t red) {
    uint8_t frame[10];

    // 1. Header (2 bajta)
    frame[0] = 0xAA;
    frame[1] = 0x55;

    // 2. Brojac paketa (1 bajt)
    frame[2] = packet_seq++;

    // 3. Payload (IR i RED kao Big-Endian 16-bitni brojevi)
    frame[3] = (uint8_t)(ir >> 8);
    frame[4] = (uint8_t)(ir & 0xFF);
    frame[5] = (uint8_t)(red >> 8);
    frame[6] = (uint8_t)(red & 0xFF);

    // 4. CRC16 racunat nad Sequence + IR + RED (polja [2..6])
    uint16_t crc = Calculate_CRC16(&frame[2], 5);
    frame[7] = (uint8_t)(crc >> 8);
    frame[8] = (uint8_t)(crc & 0xFF);

    // 5. Tail (1 bajt)
    frame[9] = 0x0A; // '\n'

    // Slanje celog paketa od 10 bajtova preko UART-a
    UART1_Write(frame, sizeof(frame));
}

// Prekidna rutina na pin-u kada MAX30100 spusti INT liniju (podaci spremni)
void ulPinIntHandler(GPIO_PIN pin, uintptr_t context) {
    if (GPIO_PinRead(pin) == 0) {
        BaseType_t xHigherPriorityTaskWoken = pdFALSE;
        xSemaphoreGiveFromISR(xDataReadySemaphore, &xHigherPriorityTaskWoken);
        portEND_SWITCHING_ISR(xHigherPriorityTaskWoken);
    }
}

// Zadatak za prijem i obradu komandi promena struje sa PC-a
// Format paketa sa PC-a: [HEADER(0xBB), RED_CURRENT, IR_CURRENT, TAIL(0x0A)]
// NAPOMENA: UART1_Read je blokirajuca funkcija (polling), pa je task-safe
// da se zove direktno - task ce se "uspavati" dok bajt ne stigne, a
// scheduler ce u medjuvremenu pustiti druge taskove (npr. vCitanjeSenzora).
void vRxTask(void *pvParameters) {
    uint8_t rx_byte;
    uint8_t cmd_buf[4];
    uint8_t state = 0;

    for (;;) {
        if (UART1_Read(&rx_byte, 1)) {
            switch (state) {
                case 0: // Header
                    if (rx_byte == 0xBB) {
                        cmd_buf[0] = rx_byte;
                        state = 1;
                    }
                    break;
                case 1: // RED LED struja (0x00 - 0x0F za 0mA do 50mA)
                    cmd_buf[1] = rx_byte;
                    state = 2;
                    break;
                case 2: // IR LED struja (0x00 - 0x0F za 0mA do 50mA)
                    cmd_buf[2] = rx_byte;
                    state = 3;
                    break;
                case 3: // Tail (0x0A)
                    if (rx_byte == 0x0A) {
                     // Direktno postavljanje odabranih struja dioda
                        MAX30100_set_led_current((MAX30100_LED_CURRENT_t)cmd_buf[1],
                                                (MAX30100_LED_CURRENT_t)cmd_buf[2]);

                    // Potvrda ka Python-u da je konfiguracija primenjena
                        uint8_t ack[2] = {0xCC, 0x0A};
                        UART1_Write(ack, sizeof(ack));
                    }
                    state = 0;
                    break;
                default:
                    state = 0;
                    break;
            }
        }
    }
}

// Zadatak za ocitavanje i slanje sirovih vrednosti
void vCitanjeSenzora(void *pvParameters) {
    // Inicijalizacija MAX30100 senzora (podrazumevano 50 mA)
    MAX30100_init(MAX30100_MODE_SPO2_HR,
                  MAX30100_LED_CURRENT_50mA,
                  MAX30100_LED_CURRENT_50mA,
                  MAX30100_SAMPLING_RATE_100Hz,
                  MAX30100_PULSE_WIDTH_1600uS_ADC_16);

    MAX30100_FIFO_init();
    MAX30100_read_int_status(); // Ocisti inicijalni interrupt status

    for (;;) {
        // Cekamo signal od prekida da su podaci spremni
        if (xSemaphoreTake(xDataReadySemaphore, portMAX_DELAY) == pdTRUE) {

            // Citanje sirovih podataka iz FIFO registra
            MAX30100_read_led_data(&senzor);

            // Saljemo binarni zasticeni paket ka Python-u na 100 Hz
            UART_Send_Sample(senzor.ir_led, senzor.red_led);
        }
    }
}

int main(void) {
    /* Inicijalizacija perifernih modula sistema */
    SYS_Initialize(NULL);

    // Kreiranje binarnog semafora
    xDataReadySemaphore = xSemaphoreCreateBinary();

    // Registracija i omogucavanje prekida za eksterni INT pin sa senzora (RE8)
    GPIO_PinInterruptCallbackRegister(GPIO_PIN_RE8, ulPinIntHandler, 0);
    GPIO_PinIntEnable(GPIO_PIN_RE8, GPIO_INTERRUPT_ON_MISMATCH);

    if (xDataReadySemaphore != NULL) {
        // Kreiranje RTOS zadataka
        xTaskCreate(vCitanjeSenzora, "Citanje Senzora", 2000, NULL, 2, NULL);
        xTaskCreate(vRxTask, "Prijem Sa PC", 1000, NULL, 1, NULL);

        // Pokretanje FreeRTOS rasporedivaca
        vTaskStartScheduler();
    } else {
        printf("Semafor nije uspesno kreiran!\n");
    }

    for (;;);

    return (EXIT_FAILURE);
}