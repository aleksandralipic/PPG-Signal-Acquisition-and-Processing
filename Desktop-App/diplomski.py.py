import sys
import math
import struct
import collections
import numpy as np
import serial
import serial.tools.list_ports

from PySide6.QtCore import QThread, Signal, Slot, Qt
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QComboBox, QLabel, QGroupBox, QFrame, QSpinBox, 
    QDoubleSpinBox, QDialog, QMessageBox
)
import pyqtgraph as pg


# =============================================================================
# REČNIK STRUJA ZA MAX30100 (0x00 - 0x0F)
# =============================================================================
LED_CURRENTS = {
    "0.0 mA": 0x00,
    "4.4 mA": 0x01,
    "7.6 mA": 0x02,
    "11.0 mA": 0x03,
    "14.2 mA": 0x04,
    "17.4 mA": 0x05,
    "20.8 mA": 0x06,
    "24.0 mA": 0x07,
    "27.1 mA": 0x08,
    "30.6 mA": 0x09,
    "33.8 mA": 0x0A,
    "37.0 mA": 0x0B,
    "40.2 mA": 0x0C,
    "43.6 mA": 0x0D,
    "46.8 mA": 0x0E,
    "50.0 mA": 0x0F
}


# =============================================================================
# 0. CRC16 PROVERA INTEGRITETA
# =============================================================================
def calculate_crc16_ccitt(data: bytes) -> int:
    """CRC16 CCITT algoritam (Init: 0xFFFF, Polinom: 0x1021)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= (byte << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


# =============================================================================
# 1. APSTRAKCIJA KOMUNIKACIJE
# =============================================================================
class BaseCommunication:
    """Apstraktni komunikacioni interfejs."""
    def open(self): pass
    def close(self): pass
    def read_bytes(self, num_bytes: int) -> bytes: pass
    def write_bytes(self, data: bytes): pass
    def bytes_available(self) -> int: pass


class SerialCommunication(BaseCommunication):
    """Konkretna UART/USB Serial implementacija."""
    def __init__(self, port="COM7", baudrate=115200):
        self.port = port
        self.baudrate = baudrate
        self.ser = None

    def open(self):
        self.ser = serial.Serial(self.port, self.baudrate, timeout=1)

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()

    def read_bytes(self, num_bytes: int) -> bytes:
        return self.ser.read(num_bytes) if self.ser else b""

    def write_bytes(self, data: bytes):
        if self.ser and self.ser.is_open:
            self.ser.write(data)

    def bytes_available(self) -> int:
        return self.ser.in_waiting if self.ser else 0


# =============================================================================
# 2. DIGITALNI FILTAR
# =============================================================================
class HRFilter:
    """FIR filtar 26. reda sa kružnim baferom."""
    def __init__(self):
        self.TAPS = np.array([
            -0.003957, -0.005166, -0.007626, -0.011036, -0.014565, -0.017124,
            -0.014166, -0.006240,  0.007137,  0.026771,  0.051939,  0.080766,
             0.110757,  0.139263,  0.163486,  0.180820,  0.188981,  0.188981,
             0.180820,  0.163486,  0.139263,  0.110757,  0.080766,  0.051939,
             0.026771,  0.007137
        ], dtype=float)
        
        self.num_taps = len(self.TAPS)
        self.buffer = np.zeros(self.num_taps, dtype=float)
        self.buf_idx = 0

    def filter_sample(self, input_val: float) -> float:
        self.buffer[self.buf_idx] = input_val
        out_val = 0.0
        idx = self.buf_idx
        for i in range(self.num_taps):
            out_val += self.buffer[idx] * self.TAPS[i]
            idx -= 1
            if idx < 0:
                idx = self.num_taps - 1
        self.buf_idx = (self.buf_idx + 1) % self.num_taps
        return out_val


# =============================================================================
# 3. POZADINSKI TRED ZA OBRADU I ANALITIKU
# =============================================================================
class SerialThread(QThread):
    data_received = Signal(float, float)  # ir_filt, threshold
    vitals_updated = Signal(str, str, str, str, str, str, str)  # bpm, spo2, hrv, zone, kcal, status, style
    hrv_data_updated = Signal(list)  # Lista RR intervala (ms) za tahogram
    config_ack_received = Signal()  # Potvrda sa PIC32 da je struja dioda primenjena

    def __init__(self, port="COM7", baudrate=115200, age=23, weight=70.0, red_current=0x0F, ir_current=0x0F):
        super().__init__()
        self.comm = SerialCommunication(port=port, baudrate=baudrate)
        self.is_running = True

        self.age = age
        self.weight = weight
        self.red_current = red_current
        self.ir_current = ir_current

        self.filter_ir = HRFilter()
        self.filter_red = HRFilter()

        self.ir_window = collections.deque(maxlen=10)
        self.red_window = collections.deque(maxlen=10)

        self.ir_peak_buffer = collections.deque(maxlen=250)
        self.threshold = 50.0

        # AUTOMAT STANJA
        self.user_state = "WAITING_FOR_FINGER"
        self.stabilize_counter = 0
        self.STABILIZE_SAMPLES = 500  # 5 sekundi pauza

        # BPM & HRV automat
        self.fsm_state = "INIT"
        self.pulse_counter = 0
        self.rr_intervals = []
        self.bpm_niz = []

        self.rr_history_hrv = collections.deque(maxlen=50)

        self.last_bpm = "--"
        self.last_spo2 = "-- %"
        self.last_hrv = "-- ms"
        self.last_zone = "--"
        self.last_kcal = "-- kcal/min"

        # SpO2 baferi
        self.num_elements_spo2 = 400
        self.ir_spo2_buf = []
        self.red_spo2_buf = []

    def set_user_params(self, age, weight):
        self.age = age
        self.weight = weight

    def run(self):
        try:
            self.comm.open()
            # Slanje komande za podešavanje struje na PIC32 [0xBB, RED_VAL, IR_VAL, 0x0A]
            config_pkt = bytes([0xBB, self.red_current, self.ir_current, 0x0A])
            self.comm.write_bytes(config_pkt)

            self.vitals_updated.emit("--", "-- %", "-- ms", "--", "--", "Inicijalizacija sistema...", "color: #ffb703;")
        except Exception as e:
            self.vitals_updated.emit("--", "-- %", "-- ms", "--", "--", f"Greška konekcije: {e}", "color: #ff5252;")
            return

        buffer = bytearray()

        while self.is_running:
            if self.comm.bytes_available() > 0:
                chunk = self.comm.read_bytes(self.comm.bytes_available())
                buffer.extend(chunk)

            while True:
                # 1. Provera ACK paketa konfiguracije struje: [0xCC, 0x0A] (2 bajta)
                if len(buffer) >= 2 and buffer[0] == 0xCC and buffer[1] == 0x0A:
                    del buffer[:2]
                    self.config_ack_received.emit()
                    continue

                # 2. Provera standardnog 10-bajtnog paketa sa merenjima
                if len(buffer) >= 10 and buffer[0] == 0xAA and buffer[1] == 0x55:
                    if buffer[9] == 0x0A:
                        packet = buffer[:10]
                        received_crc = (packet[7] << 8) | packet[8]
                        calculated_crc = calculate_crc16_ccitt(packet[2:7])

                        if received_crc == calculated_crc:
                            ir_val, red_val = struct.unpack(">HH", packet[3:7])
                            self.process_sample(ir_val, red_val)
                            del buffer[:10]
                        else:
                            del buffer[0]
                    else:
                        del buffer[0]
                    continue

                # 3. Nema dovoljno bajtova da se prepozna nijedan tip paketa,
                #    ili prvi bajt ne odgovara nijednom poznatom headeru -> odbaci ga
                if len(buffer) > 0 and buffer[0] not in (0xAA, 0xCC):
                    del buffer[0]
                    continue

                # Nedovoljno podataka za odluku - sačekaj sledeći chunk
                break

            self.msleep(1)

        self.comm.close()

    def process_sample(self, ir_val, red_val):
        finger_present = (2000 < ir_val < 60000)

        if not finger_present:
            self.user_state = "WAITING_FOR_FINGER"
            self.stabilize_counter = 0
            self.ir_peak_buffer.clear()
            self.ir_spo2_buf.clear()
            self.red_spo2_buf.clear()
            self.bpm_niz.clear()
            self.rr_intervals.clear()
            self.rr_history_hrv.clear()

            self.last_bpm = "--"
            self.last_spo2 = "-- %"
            self.last_hrv = "-- ms"
            self.last_zone = "--"
            self.last_kcal = "--"
            self.fsm_state = "INIT"

            self.vitals_updated.emit("--", "-- %", "-- ms", "--", "--", "SENZOR PRAZAN - Postavite prst", "color: #ff5252;")
            self.data_received.emit(0.0, 0.0)
            self.hrv_data_updated.emit([])

        else:
            self.ir_window.append(ir_val)
            self.red_window.append(red_val)

            ir_dc = sum(self.ir_window) / len(self.ir_window)
            red_dc = sum(self.red_window) / len(self.red_window)

            ir_ac = ir_val - ir_dc
            red_ac = red_val - red_dc

            ir_filtered = -self.filter_ir.filter_sample(ir_ac)
            red_filtered = -self.filter_red.filter_sample(red_ac)

            if self.user_state == "WAITING_FOR_FINGER":
                self.user_state = "STABILIZING"
                self.stabilize_counter = 0

            if self.user_state == "STABILIZING":
                self.stabilize_counter += 1
                preostalo_sec = (self.STABILIZE_SAMPLES - self.stabilize_counter) / 100.0
                status_msg = f"STABILIZACIJA SIGNALA... ({preostalo_sec:.1f}s)"
                self.vitals_updated.emit("--", "-- %", "-- ms", "--", "--", status_msg, "color: #ffb703;")
                self.data_received.emit(0.0, 0.0)

                if self.stabilize_counter >= self.STABILIZE_SAMPLES:
                    self.user_state = "MEASURING"

            elif self.user_state == "MEASURING":
                self.ir_peak_buffer.append(ir_filtered)
                max_in_buf = max(self.ir_peak_buffer) if len(self.ir_peak_buffer) > 0 else 0.0
                self.threshold = max(30.0, 0.5 * max_in_buf)

                self.process_fsm(ir_filtered)

                # SpO2 proračun
                self.ir_spo2_buf.append(ir_filtered)
                self.red_spo2_buf.append(red_filtered)

                if len(self.ir_spo2_buf) >= self.num_elements_spo2:
                    rms_ir = math.sqrt(sum(x**2 for x in self.ir_spo2_buf) / self.num_elements_spo2)
                    rms_red = math.sqrt(sum(x**2 for x in self.red_spo2_buf) / self.num_elements_spo2)

                    if ir_dc > 0 and red_dc > 0 and rms_ir > 0:
                        R = (rms_red / red_dc) / (rms_ir / ir_dc)
                        spo2_val = 1.5958422 * R * R - 34.6596622 * R + 112.6898759
                        spo2_val = max(0.0, min(100.0, spo2_val))
                        self.last_spo2 = f"{spo2_val:.1f} %"
                    else:
                        self.last_spo2 = "-- %"

                    self.ir_spo2_buf.clear()
                    self.red_spo2_buf.clear()

                self.vitals_updated.emit(
                    str(self.last_bpm), str(self.last_spo2), str(self.last_hrv),
                    str(self.last_zone), str(self.last_kcal), "MONITORING AKTIVAN", "color: #00e676;"
                )
                self.data_received.emit(ir_filtered, self.threshold)

    def process_fsm(self, ir_nf):
        if self.fsm_state == "INIT":
            self.fsm_state = "CEKAJ_PIK_1" if ir_nf >= self.threshold else "CEKAJ_TH_1"
        elif self.fsm_state == "CEKAJ_TH_1":
            if ir_nf >= self.threshold:
                self.fsm_state = "CEKAJ_PIK_1"
        elif self.fsm_state == "CEKAJ_PIK_1":
            if ir_nf < self.threshold:
                self.pulse_counter = 0
                self.fsm_state = "CEKAJ_TH_2"
        elif self.fsm_state == "CEKAJ_TH_2":
            self.pulse_counter += 1
            if ir_nf > self.threshold:
                self.fsm_state = "CEKAJ_PIK_2"
        elif self.fsm_state == "CEKAJ_PIK_2":
            self.pulse_counter += 1
            if ir_nf < self.threshold and len(self.bpm_niz) < 5:
                if self.pulse_counter > 0:
                    rr_ms = self.pulse_counter * 10.0
                    bpm = 60000.0 / rr_ms
                    self.bpm_niz.append(bpm)
                    self.rr_intervals.append(rr_ms)
                    
                    self.rr_history_hrv.append(rr_ms)
                    self.hrv_data_updated.emit(list(self.rr_history_hrv))

                self.fsm_state = "IZRACUNAJ_BPM" if len(self.bpm_niz) == 5 else "INIT"

        if self.fsm_state == "IZRACUNAJ_BPM":
            self.bpm_niz.sort()
            calculated_bpm = int(round(self.bpm_niz[2]))
            self.last_bpm = str(calculated_bpm)

            if len(self.rr_intervals) >= 2:
                diffs = [self.rr_intervals[i+1] - self.rr_intervals[i] for i in range(len(self.rr_intervals)-1)]
                squared_diffs = [d**2 for d in diffs]
                rmssd = math.sqrt(sum(squared_diffs) / len(squared_diffs))
                self.last_hrv = f"{int(round(rmssd))} ms"

            hr_max = 220 - self.age
            pct = (calculated_bpm / hr_max) * 100.0

            if pct < 50:
                self.last_zone = "Odmor (<50%)"
            elif 50 <= pct < 60:
                self.last_zone = "Zagrevanje (50-60%)"
            elif 60 <= pct < 70:
                self.last_zone = "Aerobna (60-70%)"
            elif 70 <= pct < 80:
                self.last_zone = "Kardio (70-80%)"
            else:
                self.last_zone = "Anaerobna (>80%)"

            kcal_per_min = (-55.0969 + (0.6309 * calculated_bpm) + (0.1988 * self.weight) + (0.2017 * self.age)) / 4.184
            kcal_per_min = max(0.0, kcal_per_min)
            self.last_kcal = f"{kcal_per_min:.1f} kcal/min"

            self.bpm_niz.clear()
            self.fsm_state = "INIT"

    def stop(self):
        self.is_running = False
        self.wait()


# =============================================================================
# 4. DIJALOG POSTAVKI PRE POČETKA MERENJA
# =============================================================================
class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Postavke merenja - MAX30100")
        self.resize(340, 250)
        self.setStyleSheet(MODERN_STYLE)

        self.selected_port = ""
        self.red_current_val = 0x0F
        self.ir_current_val = 0x0F

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # 1. Izbor Serijskog Porta
        layout.addWidget(QLabel("<b style='color:#38bdf8;'>Izaberite COM Port:</b>"))
        self.cb_ports = QComboBox()
        layout.addWidget(self.cb_ports)
        self.refresh_ports()

        # 2. Izbor Struje za RED LED
        layout.addWidget(QLabel("<b style='color:#38bdf8;'>Struja RED diode:</b>"))
        self.cb_red = QComboBox()
        for name in LED_CURRENTS.keys():
            self.cb_red.addItem(name)
        self.cb_red.setCurrentText("50.0 mA")  # Default vrednost u projektu
        layout.addWidget(self.cb_red)

        # 3. Izbor Struje za IR LED
        layout.addWidget(QLabel("<b style='color:#38bdf8;'>Struja IR diode:</b>"))
        self.cb_ir = QComboBox()
        for name in LED_CURRENTS.keys():
            self.cb_ir.addItem(name)
        self.cb_ir.setCurrentText("50.0 mA")  # Default vrednost u projektu
        layout.addWidget(self.cb_ir)

        layout.addSpacing(10)

        # 4. Potvrda i Pokretanje
        self.btn_start = QPushButton("Započni merenje")
        self.btn_start.setStyleSheet("background-color: #0284c7; color: white; font-size: 11pt; padding: 8px;")
        self.btn_start.clicked.connect(self.on_start)
        layout.addWidget(self.btn_start)

    def refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.cb_ports.clear()
        for p in ports:
            self.cb_ports.addItem(p)

    def on_start(self):
        if not self.cb_ports.currentText():
            QMessageBox.warning(self, "Greška", "Nije izabran COM port!")
            return

        self.selected_port = self.cb_ports.currentText()
        self.red_current_val = LED_CURRENTS[self.cb_red.currentText()]
        self.ir_current_val = LED_CURRENTS[self.cb_ir.currentText()]
        self.accept()


# =============================================================================
# 5. GUI INTERFEJS
# =============================================================================
MODERN_STYLE = """
QMainWindow, QDialog {
    background-color: #0f141c;
}
QWidget {
    color: #e1e7ed;
    font-family: 'Segoe UI', Arial, sans-serif;
}
QGroupBox {
    background-color: #171e28;
    border: 1px solid #232d3d;
    border-radius: 8px;
    margin-top: 12px;
    font-size: 11px;
    font-weight: bold;
    color: #7a8b9e;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 4px;
}
QLabel {
    background: transparent;
}
QPushButton {
    background-color: #1e293b;
    border: 1px solid #334155;
    border-radius: 6px;
    color: #38bdf8;
    font-weight: bold;
    padding: 6px 14px;
}
QPushButton:hover {
    background-color: #334155;
    border-color: #38bdf8;
}
QPushButton:disabled {
    background-color: #121721;
    border-color: #1e293b;
    color: #475569;
}
QComboBox, QSpinBox, QDoubleSpinBox {
    background-color: #171e28;
    border: 1px solid #232d3d;
    border-radius: 6px;
    color: #f8fafc;
    padding: 4px 8px;
    font-weight: 500;
}
QComboBox:disabled {
    background-color: #121721;
    color: #475569;
}
"""

class PulseOximeterGUI(QMainWindow):
    def __init__(self, init_port, red_current, ir_current):
        super().__init__()
        self.setWindowTitle("Medical Pulse Oximetry & HRV Bio-Analytics Platform")
        self.resize(1180, 880)
        self.setStyleSheet(MODERN_STYLE)

        self.init_port = init_port
        self.red_current = red_current
        self.ir_current = ir_current

        self.serial_thread = None
        self.buffer_size = 500
        self.ir_data = [0.0] * self.buffer_size
        self.th_data = [0.0] * self.buffer_size

        self.init_ui()
        self.auto_start_connection()

    def init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(14, 14, 14, 14)

        # ---------------- TOP PANEL ----------------
        top_frame = QFrame()
        top_frame.setStyleSheet("background-color: #171e28; border: 1px solid #232d3d; border-radius: 8px;")
        top_layout = QHBoxLayout(top_frame)
        top_layout.setContentsMargins(12, 8, 12, 8)

        top_layout.addWidget(QLabel("<b style='color:#38bdf8;'>KONEKCIJA:</b>"))
        top_layout.addWidget(QLabel("Port:"))
        self.combo_ports = QComboBox()
        self.combo_ports.setMinimumWidth(100)
        top_layout.addWidget(self.combo_ports)

        self.btn_refresh = QPushButton("Osveži")
        self.btn_refresh.clicked.connect(self.refresh_ports)
        top_layout.addWidget(self.btn_refresh)

        self.btn_connect = QPushButton("Poveži se")
        self.btn_connect.clicked.connect(self.toggle_connection)
        top_layout.addWidget(self.btn_connect)

        top_layout.addSpacing(30)
        top_layout.addWidget(QLabel("<b style='color:#38bdf8;'>PROFIL:</b>"))
        top_layout.addWidget(QLabel("Godine:"))
        self.spin_age = QSpinBox()
        self.spin_age.setRange(10, 100)
        self.spin_age.setValue(23)
        top_layout.addWidget(self.spin_age)

        top_layout.addWidget(QLabel("Težina (kg):"))
        self.spin_weight = QDoubleSpinBox()
        self.spin_weight.setRange(30.0, 200.0)
        self.spin_weight.setValue(70.0)
        top_layout.addWidget(self.spin_weight)

        # Indikator potvrde konfiguracije struje (ACK sa PIC32)
        top_layout.addSpacing(30)
        self.lbl_config_ack = QLabel("Konfiguracija: čeka se...")
        self.lbl_config_ack.setStyleSheet("font-size: 9pt; font-weight: 600; color: #94a3b8;")
        top_layout.addWidget(self.lbl_config_ack)

        top_layout.addStretch()
        main_layout.addWidget(top_frame)

        # ---------------- MIDDLE PANEL ----------------
        middle_layout = QHBoxLayout()
        middle_layout.setSpacing(10)

        pg.setConfigOptions(antialias=True)
        self.graph_widget = pg.PlotWidget()
        self.graph_widget.setBackground('#11161f')
        self.graph_widget.setTitle("<span style='color:#94a3b8; font-size:11pt; font-weight:600;'>DIGITALNO FILTRIRANI PPG SIGNAL (IR KANAL)</span>")
        self.graph_widget.setLabel("left", "Amplituda", colors='#64748b')
        self.graph_widget.setLabel("bottom", "Uzorci", colors='#64748b')
        self.graph_widget.showGrid(x=True, y=True, alpha=0.15)
        self.graph_widget.addLegend(offset=(-10, 10))

        self.curve_ir = self.graph_widget.plot(pen=pg.mkPen(color='#ff2a5f', width=2), name="PPG IR Waveform")
        self.curve_th = self.graph_widget.plot(pen=pg.mkPen(color='#f59e0b', width=1.5, style=Qt.DashLine), name="Detection Threshold")

        middle_layout.addWidget(self.graph_widget, stretch=3)

        side_panel = QVBoxLayout()
        side_panel.setSpacing(8)

        box_bpm = QGroupBox("PULS (BPM)")
        layout_bpm = QVBoxLayout(box_bpm)
        self.lbl_bpm = QLabel("--")
        self.lbl_bpm.setAlignment(Qt.AlignCenter)
        self.lbl_bpm.setStyleSheet("font-size: 36pt; font-weight: 800; color: #ff2a5f;")
        layout_bpm.addWidget(self.lbl_bpm)

        box_spo2 = QGroupBox("SATURACIJA (SpO2)")
        layout_spo2 = QVBoxLayout(box_spo2)
        self.lbl_spo2 = QLabel("-- %")
        self.lbl_spo2.setAlignment(Qt.AlignCenter)
        self.lbl_spo2.setStyleSheet("font-size: 36pt; font-weight: 800; color: #00e676;")
        layout_spo2.addWidget(self.lbl_spo2)

        box_hrv = QGroupBox("HRV (RMSSD)")
        layout_hrv = QVBoxLayout(box_hrv)
        self.lbl_hrv = QLabel("-- ms")
        self.lbl_hrv.setAlignment(Qt.AlignCenter)
        self.lbl_hrv.setStyleSheet("font-size: 24pt; font-weight: 700; color: #a855f7;")
        layout_hrv.addWidget(self.lbl_hrv)

        box_fitness = QGroupBox("FITNES ANALITIKA")
        layout_fitness = QVBoxLayout(box_fitness)
        self.lbl_zone = QLabel("Zona: --")
        self.lbl_zone.setStyleSheet("font-size: 10pt; font-weight: 600; color: #ffb703;")
        self.lbl_kcal = QLabel("Potrošnja: --")
        self.lbl_kcal.setStyleSheet("font-size: 10pt; font-weight: 600; color: #fb923c;")
        layout_fitness.addWidget(self.lbl_zone)
        layout_fitness.addWidget(self.lbl_kcal)

        side_panel.addWidget(box_bpm)
        side_panel.addWidget(box_spo2)
        side_panel.addWidget(box_hrv)
        side_panel.addWidget(box_fitness)

        middle_layout.addLayout(side_panel, stretch=1)
        main_layout.addLayout(middle_layout, stretch=3)

        # ---------------- BOTTOM PANEL (TAHOGRAM) ----------------
        self.hrv_graph_widget = pg.PlotWidget()
        self.hrv_graph_widget.setBackground('#11161f')
        self.hrv_graph_widget.setTitle("<span style='color:#94a3b8; font-size:10pt; font-weight:600;'>HRV TAHOGRAM (Varijabilnost RR intervala)</span>")
        self.hrv_graph_widget.setLabel("left", "RR (ms)", colors='#64748b')
        self.hrv_graph_widget.setLabel("bottom", "Otkucaj", colors='#64748b')
        self.hrv_graph_widget.showGrid(x=True, y=True, alpha=0.15)
        self.hrv_graph_widget.setMaximumHeight(170)

        self.curve_rr = self.hrv_graph_widget.plot(
            pen=pg.mkPen(color='#a855f7', width=2),
            symbol='o', symbolSize=5, symbolBrush='#c084fc', symbolPen='#a855f7'
        )

        main_layout.addWidget(self.hrv_graph_widget, stretch=1)

        # ---------------- STATUS BAR ----------------
        self.lbl_status = QLabel("STATUS: Nije povezano")
        self.lbl_status.setStyleSheet("background-color: #171e28; border: 1px solid #232d3d; border-radius: 6px; font-size: 10pt; font-weight: bold; padding: 6px 12px; color: #94a3b8;")
        main_layout.addWidget(self.lbl_status)

        self.refresh_ports()

    def refresh_ports(self):
        current_port = self.combo_ports.currentText()
        self.combo_ports.clear()

        ports = [p.device for p in serial.tools.list_ports.comports()]
        for p in ports:
            self.combo_ports.addItem(p)

        if current_port in ports:
            self.combo_ports.setCurrentText(current_port)

    def auto_start_connection(self):
        """Automatski pokreće konekciju sa izabranim portom i strujama iz postavki."""
        if self.init_port:
            self.combo_ports.setCurrentText(self.init_port)
            self.toggle_connection()

    def toggle_connection(self):
        if self.serial_thread is None or not self.serial_thread.isRunning():
            port = self.combo_ports.currentText()
            if not port:
                self.lbl_status.setText("STATUS: Izaberite COM port!")
                self.lbl_status.setStyleSheet("background-color: #171e28; border: 1px solid #232d3d; border-radius: 6px; font-size: 10pt; font-weight: bold; padding: 6px 12px; color: #ff5252;")
                return

            age = self.spin_age.value()
            weight = self.spin_weight.value()

            self.lbl_status.setText("STATUS: Povezivanje u toku...")
            self.lbl_status.setStyleSheet("background-color: #171e28; border: 1px solid #232d3d; border-radius: 6px; font-size: 10pt; font-weight: bold; padding: 6px 12px; color: #ffb703;")

            self.lbl_config_ack.setText("Konfiguracija: čeka se...")
            self.lbl_config_ack.setStyleSheet("font-size: 9pt; font-weight: 600; color: #ffb703;")

            self.serial_thread = SerialThread(
                port=port, 
                age=age, 
                weight=weight,
                red_current=self.red_current,
                ir_current=self.ir_current
            )
            self.serial_thread.data_received.connect(self.update_graph)
            self.serial_thread.vitals_updated.connect(self.update_vitals)
            self.serial_thread.hrv_data_updated.connect(self.update_hrv_plot)
            self.serial_thread.config_ack_received.connect(self.on_config_ack)
            self.serial_thread.start()

            self.btn_connect.setText("Prekini")
            self.btn_connect.setStyleSheet("color: #ff5252; border-color: #ef4444;")

            self.combo_ports.setEnabled(False)
            self.btn_refresh.setEnabled(False)
        else:
            self.serial_thread.stop()
            self.serial_thread = None

            self.reset_ui_display()

            self.btn_connect.setText("Poveži se")
            self.btn_connect.setStyleSheet("color: #38bdf8; border-color: #334155;")

            self.combo_ports.setEnabled(True)
            self.btn_refresh.setEnabled(True)

    def reset_ui_display(self):
        self.lbl_bpm.setText("--")
        self.lbl_spo2.setText("-- %")
        self.lbl_hrv.setText("-- ms")
        self.lbl_zone.setText("Zona: --")
        self.lbl_kcal.setText("Potrošnja: --")
        
        self.lbl_status.setText("STATUS: Nije povezano")
        self.lbl_status.setStyleSheet("background-color: #171e28; border: 1px solid #232d3d; border-radius: 6px; font-size: 10pt; font-weight: bold; padding: 6px 12px; color: #94a3b8;")

        self.lbl_config_ack.setText("Konfiguracija: čeka se...")
        self.lbl_config_ack.setStyleSheet("font-size: 9pt; font-weight: 600; color: #94a3b8;")

        self.curve_rr.clear()
        self.ir_data = [0.0] * self.buffer_size
        self.th_data = [0.0] * self.buffer_size
        self.curve_ir.setData(self.ir_data)
        self.curve_th.setData(self.th_data)

    @Slot(float, float)
    def update_graph(self, ir_val, th_val):
        self.ir_data.pop(0)
        self.ir_data.append(ir_val)

        self.th_data.pop(0)
        self.th_data.append(th_val)

        self.curve_ir.setData(self.ir_data)
        self.curve_th.setData(self.th_data)

    @Slot(str, str, str, str, str, str, str)
    def update_vitals(self, bpm_str, spo2_str, hrv_str, zone_str, kcal_str, status_txt, color_style):
        self.lbl_bpm.setText(bpm_str)
        self.lbl_spo2.setText(spo2_str)
        self.lbl_hrv.setText(hrv_str)
        self.lbl_zone.setText(f"Zona: {zone_str}")
        self.lbl_kcal.setText(f"Potrošnja: {kcal_str}")
        self.lbl_status.setText(f"STATUS: {status_txt}")
        self.lbl_status.setStyleSheet(f"background-color: #171e28; border: 1px solid #232d3d; border-radius: 6px; font-size: 10pt; font-weight: bold; padding: 6px 12px; {color_style}")

    @Slot(list)
    def update_hrv_plot(self, rr_list):
        if not rr_list:
            self.curve_rr.clear()
            return
        self.curve_rr.setData(rr_list)

    @Slot()
    def on_config_ack(self):
        """Poziva se kada PIC32 potvrdi prijem i primenu komande za struju dioda."""
        self.lbl_config_ack.setText("Konfiguracija: primenjena ✓")
        self.lbl_config_ack.setStyleSheet("font-size: 9pt; font-weight: 600; color: #00e676;")

    def closeEvent(self, event):
        if self.serial_thread and self.serial_thread.isRunning():
            self.serial_thread.stop()
        event.accept()


# =============================================================================
# MAIN POKRETANJE
# =============================================================================
if __name__ == "__main__":
    app = QApplication(sys.argv)

    # 1. Otvara se prozor postavki
    dialog = SettingsDialog()
    if dialog.exec() == QDialog.Accepted:
        # 2. Ako je korisnik potvrdio, otvara se glavni prozor i započinje merenje
        window = PulseOximeterGUI(
            init_port=dialog.selected_port,
            red_current=dialog.red_current_val,
            ir_current=dialog.ir_current_val
        )
        window.show()
        sys.exit(app.exec())
    else:
        sys.exit(0)