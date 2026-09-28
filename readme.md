There are two branches for this github repository:
1. Main
2. constantControl

## Hardware Needed
1. Programmable power supply
2. Arduino uno
3. Pt100 sensor (for feedback)

## Software requirements
Python >=3.11
1. pyserial
2. pyvisa
3. openpyxl
4. pandas
5. matplotlib

## to use
1. Clone the repository using the following command: `git clone https://github.com/PID_temperature_controller.git`
2. Make necessary changes like arduino device name, power supply name, target temperature etc.
3. run main file using `python main.py
4. for using other branches`
5. Change into the repository directory: `cd Control`
6. fetch the other branch using : git fetch origin constantControl                               
7. checkout the constantControl branch for continuous temperature drop very slowly around dew point using: `git checkout constantControl`  
8. checkout to the constantControl branch using: `git checkout constantControl
9. run main file using `python main.py `

