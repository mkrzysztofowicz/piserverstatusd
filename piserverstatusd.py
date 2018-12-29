#!/usr/bin/env python3

"""
PiServerStatus Daemon

This program displays raspberry pi statistics on the Scroll pHAT which needs to be attached to the pi.
"""

import argparse
import configparser
from datetime import datetime
import fcntl
import logging
import logging.handlers
import os
import socket
import struct
import time

import psutil
import pyowm
import scrollphat

from pydaemon import Daemon

wxcodes = {
    200: 'TS -RA',
    201: 'TSRA',
    202: 'TS +RA',
    210: '-TS',
    211: 'TS',
    212: '+TS',
    221: 'TS',
    230: 'TS -DZ',
    231: 'TSDZ',
    232: 'TS +DZ',

    300: '-DZ',
    301: 'DZ',
    302: '+DZ',
    310: '-DZRA',
    311: 'DZRA',
    312: '+DZRA',
    313: 'SHRADZ',
    314: '+SHRADZ',
    321: 'SHDZ',

    500: '-RA',
    501: 'RA',
    502: '+RA',
    503: '+RA',
    504: '+RA',
    511: 'FZRA',
    520: '-SHRA',
    521: 'SHRA',
    522: '+SHRA',
    531: 'SHRA',

    600: '-SN',
    601: 'SN',
    602: '+SN',
    611: 'RASN',
    612: 'SHRASN',
    615: '-RASN',
    616: 'RASN',
    620: '-SHSN',
    621: 'SHSN',
    622: '+SHSN',

    701: 'BR',
    711: 'FU',
    721: 'HZ',
    731: 'PO',  # sand or dust swirls
    741: 'FG',
    751: 'SA',  # sand
    761: 'DU',  # widespread dust
    762: 'VA',  # volcanic ash
    771: 'SQ',  # squalls
    781: 'FC',  # funnel cloud - tornado

    800: 'SKC',
    801: 'FEW',
    802: 'SCT',
    803: 'BKN',
    804: 'OVC'
}


class StatusDaemon(Daemon):

    DEFAULT_BRIGHTNESS = 10

    #
    # Daemon Initialisation methods
    #

    def __init__(self, pidfile, config_file=None, stdin='/dev/null', stdout='/dev/null', stderr='/dev/null'):
        """
        Override the __init__() method of the Daemon superclass to add extra properties
        """

        self.scrollphat_brightness = self.DEFAULT_BRIGHTNESS
        self.owm = None
        self.wx = None
        self.wx_acquisition_ts = 0
        self.wx_refresh_interval = 300

        super().__init__(pidfile, config_file, stdin, stdout, stderr, daemon_name='piserverstatusd')

    def configure(self):
        self.configuration = configparser.ConfigParser(interpolation=configparser.ExtendedInterpolation())

        if not self.config_file:
            self.logger.info('No configuration file specified. Running with defaults.')
            return

        if not os.path.exists(self.config_file):
            self.logger.error('Configuration file {} does not exist. Exiting'.format(self.config_file))
            raise SystemExit(1)

        self.configuration.read(self.config_file)

        if self.configuration.has_section('main'):
            if not self.username:
                if 'username' in self.configuration['main']:
                    self.username = self.configuration.get('main', 'username')

            loglevel = self.configuration.get('main', 'loglevel', fallback='INFO')
            loglevel = getattr(logging, loglevel.upper())
            formatter = logging.Formatter('%(levelname)s %(module)s: %(funcName)s(): %(message)s')
            syslog = logging.handlers.SysLogHandler()
            syslog.setLevel(loglevel)
            syslog.setFormatter(formatter)
            self.logger.addHandler(syslog)
            self.logger.setLevel(loglevel)

        if self.configuration.has_section('scrollphat'):
            self.scrollphat_brightness = int(self.configuration.get('scrollphat',
                                                                    'brightness',
                                                                    fallback=self.DEFAULT_BRIGHTNESS))

        owm_api_key = self.configuration.get('weather', 'openweathermap_api_key', fallback=None)
        if owm_api_key:
            owm_api_key = owm_api_key.strip("'")
            self.owm = pyowm.OWM(API_key=owm_api_key)

    #
    # Daemon Termination methods
    #

    def stop(self, silent=False):
        scrollphat.clear()
        super().stop(silent)

    def sigterm_handler(self, signo, frame):
        """
        Override the Daemon.sigterm_handler() to turn off the scrollphat when the daemon process is terminated
        """
        scrollphat.clear()
        super().sigterm_handler(signo, frame)

    #
    # Helper methods for information display
    #

    @staticmethod
    def get_ip(ifname):
        ifname = ifname[:15].encode('utf-8')
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        return socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, struct.pack('256s', ifname))[20:24])

    @staticmethod
    def get_ipv6(ifname):
        pass

    @staticmethod
    def mps_to_kt(mps):
        return int(mps * 1852 / 3600)

    @staticmethod
    def cloud(percentage):
        if percentage == 0:
            return ''
        elif 0 < percentage <= 25:
            return 'FEW'
        elif 25 < percentage <= 50:
            return 'SCT'
        elif 50 < percentage <= 75:
            return 'BKN'
        else:
            return 'OVC'

    @staticmethod
    def dewpoint(temperature, humidity):
        # calculate dewpoint using Magnus Formula
        dp = ((humidity / 100) ** 0.125) * (112 / 0.9 * temperature) / (0.1 + temperature) - 112
        if dp < 0:
            dp = 'M{:02}'.format(round(dp))
        else:
            dp = '{:02}'.format(round(dp))
        return dp

    @staticmethod
    def get_time():
        now = datetime.now()
        return now.strftime('%H:%M:%S')

    def get_weather(self, latitude, longitude):
        now = datetime.now()
        if now.minute == 1 or now.minute == 31 or now.timestamp() - self.wx_acquisition_ts > self.wx_refresh_interval:
            self.logger.info('Getting new weather for: {}, {}'.format(latitude, longitude))
            observations = None

            try:
                observations = self.owm.weather_around_coords(latitude, longitude, limit=1)
            except Exception as e:
                self.logger.exception('Error getting weather: {}: {}'.format(type(e).__name__,
                                                                             str(e)))
            if len(observations):
                self.wx = observations[0]
                self.wx_acquisition_ts = now.timestamp()

    def scroll_netinfo(self, interfaces, scroll_interval=0.1, repeat=1):
        sysinfo = list()
        for interface in interfaces:
            sysinfo.append('{}:{}'.format(interface[0].upper(), self.get_ip(interface)))
        self.scroll_text(sysinfo, scroll_interval, repeat)

    def scroll_cpuload(self, scroll_interval=0.1, repeat=1):
        loadavg = os.getloadavg()
        loadavg = 'L:{}/{}/{}'.format(loadavg[0], loadavg[1], loadavg[2])
        self.scroll_text(loadavg, scroll_interval, repeat)

    def scroll_time(self, duration=15, scroll_interval=0.1):
        scrollphat.clear()
        for _ in range(int(duration / scroll_interval)):
            scrollphat.write_string(self.get_time(), 4)
            scrollphat.scroll()
            time.sleep(scroll_interval)

    def scroll_weather(self, scroll_interval=0.1, repeat=1):
        if not self.owm:
            return

        lat = self.configuration.getfloat('weather', 'latitude', fallback=None)
        lon = self.configuration.getfloat('weather', 'longitude', fallback=None)

        if lat is not None and lon is not None:
            self.get_weather(lat, lon)
            location = self.wx.get_location().get_name()

            obtime = self.wx.get_weather().get_reference_time()
            obtime = datetime.fromtimestamp(obtime).strftime('%d%H%M')

            wind = self.wx.get_weather().get_wind()
            wv = '{:03}{:02}'.format(int(wind['deg']), int(self.mps_to_kt(wind['speed'])))

            visibility = self.wx.get_weather().get_visibility_distance()

            wxcode = self.wx.get_weather().get_weather_code()
            if type(wxcode) == list:
                weather = list()
                for item in wxcode:
                    if item > 800:
                        weather.append(wxcodes[item])
            else:
                weather = None
                if wxcode < 800:
                    weather = wxcodes[wxcode]

            cloud = self.cloud(self.wx.get_weather().get_clouds())

            temps = self.wx.get_weather().get_temperature('celsius')
            temperature = '{:02}'.format(round(temps['temp']))
            if temps['temp'] < 0:
                temperature = 'M{}'.format(temperature)

            humidity = self.wx.get_weather().get_humidity()

            dewpoint = self.wx.get_weather().get_dewpoint() or self.dewpoint(temps['temp'], humidity)

            rh = 'RH{}'.format(humidity)
            t_dp = '{}/{}'.format(temperature, dewpoint)

            pressure = self.wx.get_weather().get_pressure()

            qnh = None
            qfe = None
            if pressure['press']:
                qfe = 'QFE{}'.format(pressure['press'])

            if pressure['sea_level']:
                qnh = 'Q{}'.format(pressure['sea_level'])

            wx = ['WX']
            for item in [location.upper(), obtime, wv, visibility, weather, cloud, t_dp, rh, qnh, qfe]:
                if item is not None:
                    wx.append(item)

            wx.append('=')

            if len(wx) > 3:
                wx = ' '.join(wx)
                self.logger.info(wx)
                self.scroll_text(wx, scroll_interval, repeat)

    @staticmethod
    def scroll_text(text, scroll_interval=0.1, repeat=1):

        if not len(text):
            return

        if type(text) == list:
            text = ' | '.join(text)

        while repeat > 0:
            repeat -= 1
            scrollphat.clear()
            scrollphat.write_string(text, 11)
            for i in range(0, scrollphat.buffer_len()):
                scrollphat.scroll()
                time.sleep(scroll_interval)

    @staticmethod
    def scroll_cpugraph(duration=15, scroll_interval=0.2):
        scrollphat.clear()
        cpu_graph_values = [0] * 11
        for _ in range(int(duration / scroll_interval)):
            cpu_graph_values.pop(0)
            cpu_graph_values.append(psutil.cpu_percent())
            scrollphat.graph(cpu_graph_values, 0, 25)
            time.sleep(scroll_interval)

    #
    # Run loop
    #

    def run(self):
        scrollphat.set_brightness(self.scrollphat_brightness)
        rotate = self.configuration.getboolean('scrollphat', 'flip', fallback=False)
        scrollphat.set_rotate(rotate)

        loop_runs = 0
        while True:
            try:
                if self.configuration.getboolean('scrollphat', 'display_time', fallback=False):
                    self.scroll_time(duration=10)

                if self.configuration.getboolean('scrollphat', 'display_network', fallback=False):
                    if divmod(loop_runs, 10)[1] == 0:
                        interfaces = ['eth0', 'wlan0']
                        self.scroll_netinfo(interfaces, scroll_interval=0.07)

                if self.configuration.getboolean('scrollphat', 'display_cpuload', fallback=False):
                    self.scroll_cpuload()

                if self.configuration.getboolean('scrollphat', 'display_cpugraph', fallback=False):
                    self.scroll_cpugraph()

                if self.configuration.getboolean('scrollphat', 'display_weather', fallback=False):
                    self.scroll_weather(scroll_interval=0.15, repeat=2)

                loop_runs += 1

            except KeyboardInterrupt:
                self.logger.info('Exiting')
                scrollphat.clear()
                raise SystemExit(0)

def main():
    """
    Main entry point into the application
    """
    parser = argparse.ArgumentParser()
    parser.add_argument('-c, --cfg-file', dest='config_file', help='path to the configuration file',
                        type=str, default=None)
    parser.add_argument('-f, --foreground', dest='foreground', help='run in foreground',
                        action='store_true', default=False)
    args = parser.parse_args()

    if not hasattr(args, 'config_file'):
        args.config_file = None

    # Instantiate the daemon
    status_daemon = StatusDaemon('/var/run/piserverstatusd.pid', config_file=args.config_file)

    if hasattr(args, 'foreground'):
        status_daemon.dont_daemonize = args.foreground

    status_daemon.configure()
    status_daemon.start()
    pid = status_daemon.get_pid()

    if not pid:
        print('Error starting piserverstatusd')
        raise SystemExit(1)

    raise SystemExit(0)


if __name__ == '__main__':
    main()
