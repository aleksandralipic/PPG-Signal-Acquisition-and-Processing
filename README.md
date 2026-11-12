\# PPG Signal Acquisition, Processing and Visualization System



\## Project Overview



This project was developed as part of my Bachelor's thesis, titled \*\*"System for Acquisition, Processing and Visualization of Photoplethysmographic (PPG) Signals."\*\*



The system enables the acquisition of RED and IR photoplethysmographic signals using the MAX30100 sensor and a PIC32MZ2048EFH144 microcontroller. The acquired data is transmitted to a desktop application developed in Python, where digital signal processing, physiological parameter estimation and signal visualization are performed.



\## System Architecture



The project consists of two main components:



\### 1. Embedded Firmware

\- \*\*Microcontroller:\*\* PIC32MZ2048EFH144

\- \*\*Sensor:\*\* MAX30100

\- \*\*Programming language:\*\* C

\- \*\*Development environment:\*\* MPLAB X IDE

\- Acquisition of RED and IR PPG signals

\- I2C communication with the MAX30100 sensor

\- Serial data transmission to the desktop application via UART



\### 2. Desktop Application

\- \*\*Programming language:\*\* Python

\- \*\*GUI framework:\*\* PySide6

\- Reception and processing of acquired PPG data

\- Digital signal processing and signal visualization

\- Estimation of heart rate (HR) and blood oxygen saturation (SpO₂)

\- Calculation of RMSSD (Root Mean Square of Successive Differences)

\- Interactive graphical user interface



\## Key Features



\- Acquisition of RED and IR photoplethysmographic signals

\- Embedded communication using I2C and UART interfaces

\- Digital processing and visualization of PPG signals

\- Estimation of physiological parameters

\- Configurable sensor LED current

\- Evaluation under different measurement conditions, including finger positioning and movement



\## Technologies and Tools



| Technology | Purpose |

|---|---|

| C | Embedded firmware development |

| Python | Signal processing and application development |

| MPLAB X IDE | Microcontroller development |

| PIC32MZ2048EFH144 | Embedded platform |

| MAX30100 | Optical PPG sensor |

| PySide6 | Desktop application GUI |

| I2C | Sensor communication |

| UART | Serial data transmission |



\## Project Structure



```text

PPG-Signal-Acquisition-and-Processing/

│

├── Firmware/

│   └── MPLAB X project

│

├── Desktop-App/

│   ├── diplomski.py

│   └── requirements.txt

│

└── README.md

```



\## Project Purpose



The main objective of this project was to design and implement an embedded system for PPG signal acquisition and develop a desktop application for signal processing, visualization and extraction of relevant physiological parameters.



The project combines embedded systems programming, hardware interfacing, serial communication, digital signal processing and graphical application development.

