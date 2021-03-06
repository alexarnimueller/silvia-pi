#! /usr/bin/env python
# -*- coding: utf-8 -*-

import sys
import time
from datetime import datetime
from multiprocessing import Process, Manager
from subprocess import call
from time import sleep, time
from urllib.request import urlopen

import board
import schedule
from adafruit_max31855 import MAX31855
from busio import SPI
from digitalio import DigitalInOut
from flask import Flask, jsonify, request, render_template, abort
from gpiozero import LED, CPUTemperature, Button
import RPi.GPIO as GPIO
from simple_pid import PID

import config

GPIO.setmode(GPIO.BCM)
GPIO.setup(config.pin_mainswitch, GPIO.IN, pull_up_down=GPIO.PUD_UP)
pwr_led = LED(config.pin_powerled, initial_value=config.initial_on)
heater = LED(config.pin_heat, initial_value=False)


def wakeup(state):
    state['is_awake'] = True
    pwr_led.on()


def gotosleep(state):
    state['is_awake'] = False
    state['heating'] = False
    heater.off()
    pwr_led.off()


def power_loop(state):
    while True:
        tick = 0
        while GPIO.input(config.pin_mainswitch) == GPIO.LOW:
            tick += 1
            sleep(0.1)
        if tick >= 2:
            if state['is_awake']:
                gotosleep(state)
            else:
                wakeup(state)


def heating_loop(state):
    while True:
        if state['is_awake']:
            avgpid = state['avgpid']
            if 0 < avgpid:  # heat if pid positive
                state['heating'] = True
                heater.on()
                sleep(avgpid / config.boundary)
                heater.off()
                sleep(1. - avgpid / config.boundary)
            else:  # turn off if negative output
                state['heating'] = False
                heater.off()
                sleep(-avgpid / config.boundary)
        else:
            heater.off()
            sleep(1)


def pid_loop(state):
    i = 0
    pidhist = config.pid_hist_len * [0.]
    temphist = config.temp_hist_len * [0.]
    temperr = config.temp_hist_len * [0]
    temp = 25.
    lastsettemp = state['brewtemp']
    lasttime = time()

    sensor = MAX31855(SPI(board.SCK, MOSI=board.MOSI, MISO=board.MISO), DigitalInOut(board.D5))
    pid = PID(Kp=config.pidc_kp, Ki=config.pidc_ki, Kd=config.pidc_kd, setpoint=state['brewtemp'],
              sample_time=config.time_sample, proportional_on_measurement=False,
              output_limits=(-config.boundary, config.boundary))

    while True:
        try:
            temp = sensor.temperature
            del temperr[0]
            temperr.append(0)
            del temphist[0]
            temphist.append(temp)
        except RuntimeError:
            del temperr[0]
            temperr.append(1)
        if sum(temperr) >= 5 * config.temp_hist_len:
            print("Temperature sensor error!")
            call(["killall", "python3"])

        avgtemp = sum(temphist) / config.temp_hist_len

        if avgtemp <= 0.9 * state['brewtemp']:
            pid.tunings = (config.pidc_kp, config.pidc_ki, config.pidc_kd)
        else:
            pid.tunings = (config.pidw_kp, config.pidw_ki, config.pidw_kd)

        if state['brewtemp'] != lastsettemp:
            pid.setpoint = state['brewtemp']
            lastsettemp = state['brewtemp']

        pidout = pid(avgtemp)
        pidhist.append(pidout)
        del pidhist[0]
        avgpid = sum(pidhist) / config.pid_hist_len

        state['i'] = i
        state['temp'] = temp
        state['pterm'], state['iterm'], state['dterm'] = pid.components
        state['avgtemp'] = round(avgtemp, 2)
        state['pidval'] = round(pidout, 2)
        state['avgpid'] = round(avgpid, 2)

        sleeptime = lasttime + config.time_sample - time()
        sleep(max(sleeptime, 0.))
        i += 1
        lasttime = time()


def scheduler(state):
    last_wake = 0
    last_sleep = 0
    last_sched_switch = False

    while True:
        if last_wake != state['wake_time'] or last_sleep != state['sleep_time'] or \
                last_sched_switch != state['sched_enabled']:
            schedule.clear()

            if state['sched_enabled']:
                schedule.every().day.at(state['sleep_time']).do(gotosleep, state)
                schedule.every().day.at(state['wake_time']).do(wakeup, state)

                nowtm = datetime.now().hour + datetime.now().minute / 60.
                sleeptm = state['sleep_time'].split(":")
                sleeptm = float(sleeptm[0]) + float(sleeptm[1]) / 60.
                waketm = state['wake_time'].split(":")
                waketm = float(waketm[0]) + float(waketm[1]) / 60.

                if waketm <= nowtm < sleeptm:
                    wakeup(state)
                else:
                    gotosleep(state)
        
        last_wake = state['wake_time']
        last_sleep = state['sleep_time']
        last_sched_switch = state['sched_enabled']

        schedule.run_pending()
        sleep(5)


def server(state):
    app = Flask(__name__)

    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)

    @app.route('/')
    @app.route('/home')
    @app.route('/index')
    def index():
        return render_template("index.html")

    @app.route('/brewtemp', methods=['POST'])
    def brewtemp():
        try:
            settemp = int(request.form.get('settemp'))
            if 80 <= settemp <= 120:
                state['brewtemp'] = settemp
                return str(settemp)
            else:
                abort(400, 'Temperature out of accepted range: 80 - 120 °C!')
        except TypeError:
            abort(400, 'Invalid number for set temp.')

    @app.route('/is_awake', methods=['GET'])
    def get_is_awake():
        return jsonify({"awake": state['is_awake']})

    @app.route('/allstats', methods=['GET'])
    def allstats():
        return jsonify(dict(state))

    @app.route('/setwake', methods=['POST'])
    def set_wake():
        wake = request.form.get('wake')
        try:
            datetime.strptime(wake, '%H:%M')
        except ValueError:
            abort(400, 'Invalid time format.')
        state['wake_time'] = wake
        return str(wake)

    @app.route('/setsleep', methods=['POST'])
    def set_sleep():
        slp = request.form.get('sleep')
        try:
            datetime.strptime(slp, '%H:%M')
        except ValueError:
            abort(400, 'Invalid time format.')
        state['sleep_time'] = slp
        return str(slp)

    @app.route('/scheduler', methods=['POST'])
    def set_sched():
        sched = request.form.get('scheduler')
        if sched == "True":
            state['sched_enabled'] = True
            sleep(0.25)
        else:
            state['sched_enabled'] = False
            sleep(0.25)
        return str(sched)

    @app.route('/turnon', methods=['GET'])
    def turnon():
        wakeup(state)
        return str("On")

    @app.route('/turnoff', methods=['GET'])
    def turnoff():
        gotosleep(state)
        return str("Off")
    
    @app.route('/restart')
    def restart():
        call(["reboot"])
        return 'Rebooting...'

    @app.route('/shutdown')
    def shutdown():
        call(["shutdown", "-h", "now"])
        return 'Shutting down...'

    @app.route('/healthcheck', methods=['GET'])
    def healthcheck():
        return 'OK'

    app.run(host='0.0.0.0', port=config.port)


if __name__ == "__main__":
    manager = Manager()
    pidstate = manager.dict()
    pidstate['is_awake'] = config.initial_on
    pidstate['heating'] = False
    pidstate['sched_enabled'] = config.schedule
    pidstate['sleep_time'] = config.time_sleep
    pidstate['wake_time'] = config.time_wake
    pidstate['i'] = 0
    pidstate['brewtemp'] = config.brew_temp
    pidstate['avgpid'] = 0.
    cpu = CPUTemperature()

    print("Starting scheduler thread...")
    s = Process(target=scheduler, args=(pidstate,))
    s.daemon = True
    s.start()
    
    print("Starting power button thread...")
    b = Process(target=power_loop, args=(pidstate,))
    b.daemon = True
    b.start()
    
    print("Starting PID thread...")
    p = Process(target=pid_loop, args=(pidstate,))
    p.daemon = True
    p.start()

    print("Starting heat control thread...")
    h = Process(target=heating_loop, args=(pidstate,))
    h.daemon = True
    h.start()

    print("Starting server thread...")
    r = Process(target=server, args=(pidstate,))
    r.daemon = True
    r.start()

    # Start Watchdog loop
    print("Starting Watchdog...")
    piderr = 0
    weberr = 0
    cpuhot = 0
    urlhc = 'http://localhost:' + str(config.port) + '/healthcheck'

    lasti = pidstate['i']
    sleep(1)

    while b.is_alive() and p.is_alive() and h.is_alive() and r.is_alive() and s.is_alive():
        curi = pidstate['i']
        if curi == lasti:
            piderr += 1
        else:
            piderr = 0
        lasti = curi

        if piderr > 9:
            print('ERROR IN PID THREAD')
            p.terminate()

        try:
            hc = urlopen(urlhc, timeout=2)
            if hc.getcode() != 200:
                weberr += 1
        except:
            weberr += 1

        if weberr > 9:
            print('ERROR IN WEB SERVER THREAD')
            r.terminate()

        if cpu.temperature > 70:
            cpuhot += 1
            if cpuhot > 9:
                print("CPU TOO HOT! SHUTTING DOWN")
                call(["shutdown", "-h", "now"])

        sleep(1)

    call(["killall", "python3"])
    gotosleep(pidstate)
