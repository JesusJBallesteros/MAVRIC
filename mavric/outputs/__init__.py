"""Event-driven outputs: rules that map RFID (and later camera) events to devices.

config.py   device/rule schema, load/save config/outputs.yaml
devices.py  output devices (sim, UDP, TCP, Intan RHX, MATLAB, command)
arduino_io.py  TTL outputs through the MAVRIC_IO Arduino firmware
engine.py   ActionEngine: evaluates rules, runs actions, logs them
"""
