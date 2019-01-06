#!/usr/bin/env python3

"""
PiServerStatus Daemon

Display raspberry pi statistics (IP addresses, CPU load), weather, time, on the Scroll pHAT attached to the pi.
"""

import argparse
import configparser
from datetime import datetime
import fcntl
import logging
import logging.handlers
import math
import os
import socket
import struct
import time

import psutil
import pyowm
import scrollphat

from pydaemon import Daemon

#
# List of possible weather phenomena codes returned by OpenWeatherMap
# https://openweathermap.org/weather-conditions
#
wxcodes = {
    200: 'TS -RA',  # thunderstorm, light rain
    201: 'TSRA',    # thunderstorm, rain
    202: 'TS +RA',  # thunderstorm, heavy rain
    210: '-TS',     # light thunderstorm
    211: 'TS',      # thunderstorm
    212: '+TS',     # heavy thunderstorm
    221: 'TS',      # ragged thunderstorm
    230: 'TS -DZ',  # thunderstorm, light drizzle
    231: 'TSDZ',    # thunderstorm, drizzle
    232: 'TS +DZ',  # thunderstorm, heavy drizzle

    300: '-DZ',     # light drizzle
    301: 'DZ',      # drizzle
    302: '+DZ',     # heavy drizzle
    310: '-DZRA',   # light drizzle, rain
    311: 'DZRA',    # drizzle, rain
    312: '+DZRA',   # heavy drizzle, rain
    313: 'SHRADZ',  # rain showers, drizzle
    314: '+SHRADZ', # heavy rain showers, drizzle
    321: 'SHDZ',    # drizzle showers

    500: '-RA',     # light rain
    501: 'RA',      # rain
    502: '+RA',     # heavy rain
    503: '+RA',     # very heavy rain
    504: '+RA',     # extreme rain
    511: 'FZRA',    # freezing rain
    520: '-SHRA',   # light rain showers
    521: 'SHRA',    # rain showers
    522: '+SHRA',   # heavy rain showers
    531: 'SHRA',    # ragged rain showers

    600: '-SN',     # light snow
    601: 'SN',      # snow
    602: '+SN',     # heavy snow
    611: 'RASN',    # rain and snow (sleet)
    612: 'SHRASN',  # rain and snow showers (sleet showers)
    615: '-RASN',   # light rain and snow
    616: 'RASN',    # rain and snow
    620: '-SHSN',   # light snow showers
    621: 'SHSN',    # snow showers
    622: '+SHSN',   # heavy snow showers

    701: 'BR',      # mist
    711: 'FU',      # smoke (fumes)
    721: 'HZ',      # haze
    731: 'PO',      # sand or dust swirls
    741: 'FG',      # fog
    751: 'SA',      # sand
    761: 'DU',      # widespread dust
    762: 'VA',      # volcanic ash
    771: 'SQ',      # squalls
    781: 'FC',      # funnel cloud - tornado

    800: 'SKC',     # sky clear
    801: 'FEW',     # 1-2 oktas of cloud
    802: 'SCT',     # 3-4 oktas of cloud
    803: 'BKN',     # 5-6 oktas of cloud
    804: 'OVC'      # 7-8 oktas of cloud
}


class StatusDaemon(Daemon):

    DEFAULT_BRIGHTNESS = 10
    DEFAULT_WX_INTERVAL = 300
    DEFAULT_DISPLAY_COUNT = 1
    DEFAULT_INTERVAL = 0.2

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
        self.wx_refresh_interval = self.DEFAULT_WX_INTERVAL

        super().__init__(pidfile, config_file, stdin, stdout, stderr, daemon_name='piserverstatusd')

    def configure(self):
        """
        Configures the daemon based on the content of the configuration file
        """
        self.configuration = configparser.ConfigParser(interpolation=configparser.ExtendedInterpolation())

        if not self.config_file:
            self.logger.info('No configuration file specified. Running with defaults.')
            return

        if not os.path.exists(self.config_file):
            self.logger.error('Configuration file {} does not exist. Exiting'.format(self.config_file))
            raise SystemExit(1)

        self.configuration.read(self.config_file)

        self.reconfigure_logging()

        self.username = self.configuration.get('main', 'username', fallback=None)
        self.scrollphat_brightness = self.configuration.getint('scrollphat',
                                                               'brightness',
                                                               fallback=self.DEFAULT_BRIGHTNESS)

        owm_api_key = self.configuration.get('weather', 'openweathermap_api_key', fallback=None)
        if owm_api_key:
            owm_api_key = owm_api_key.strip("'")
            self.owm = pyowm.OWM(API_key=owm_api_key)

    def reconfigure_logging(self):
        """
        The Daemon class initially configures its own logger, however often this configuration can be improved upon
        using the details from the configuration file - this is used to reconfigure the logging completely
        """

        loglevel = self.configuration.get('main', 'loglevel', fallback='INFO')
        loglevel = getattr(logging, loglevel.upper())

        for handler in self.logger.handlers:
            if isinstance(handler, logging.StreamHandler):
                formatter = logging.Formatter(
                    '%(asctime)s %(name)s[%(process)s]: [%(levelname)s] %(lineno)s:%(funcName)s(): %(message)s'
                )
                handler.setLevel(loglevel)
                handler.setFormatter(formatter)

            elif isinstance(handler, logging.handlers.SysLogHandler):

                formatter = logging.Formatter('%(levelname)s %(module)s: %(lineno)s:%(funcName)s(): %(message)s')
                handler.facility = 'daemon'
                handler.setLevel(loglevel)
                handler.setFormatter(formatter)

        self.logger.setLevel(loglevel)

    #
    # Daemon Termination methods
    #

    def stop(self, silent=False):
        """
        Executed on daemon shutdown - will turn off the Scroll pHAT
        """
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

    def get_ip(self, ifname):
        """
        Get the IPv4 address configured on an interface
        :param str ifname: interface name
        :return str: IP address configured on an interface
        """

        ifname = ifname[:15].encode('utf-8')
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ipaddr = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, struct.pack('256s', ifname))[20:24])
        self.logger.debug('IP address: {}:{}'.format(str(ifname), ipaddr))
        return ipaddr

    @staticmethod
    def get_ipv6(ifname):
        raise NotImplemented

    @staticmethod
    def get_time():
        """
        Return current time in the hh:mm:ss format
        :return str: current time as hh:mm:ss
        """
        now = datetime.now()
        return now.strftime('%H:%M:%S')

    @staticmethod
    def mps_to_kt(mps):
        """
        Converts speed in m/s to knots
        :param float mps: speed in m/s
        :return int: knots
        """
        return mps * 1852 / 3600

    @staticmethod
    def cloud(percentage):
        """
        Converts cloud cover percentage to METAR codes: FEW, SCT, BKN, OVC
        :param int percentage: cloud cover as percentage
        :return str: METAR code for the cloud cover
        """

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
        """
        Calculate dewpoint temperature using methodology from Vaisala document:
        https://www.vaisala.com/sites/default/files/documents/Humidity_Conversion_Formulas_B210973EN-F.pdf

        This uses the simpler formula (6), as well as constants suitable for temperature range -20°C...+50°C

        :param float temperature: outside air temperature in degrees Celsius
        :param float humidity: relative humidity as percentage
        :return float: dewpoint temperature in degrees Celsius
        """

        A = 6.116441
        m = 7.591386
        Tn = 240.7263
        Pws = A * pow(10, (m * temperature / (temperature + Tn)))
        Pw = Pws * humidity / 100.0

        Td = Tn / (m / math.log10(Pw / A) - 1)
        return Td

    def metar_dewpoint(self, temperature, humidity):
        """
        Return METAR encoded dewpoint temperature, calculated based on outside air temperature and relative humidity
        :param float temperature: OAT
        :param float humidity: RH
        :return str: metar encoded dewpoint temperature
        """
        dp = self.dewpoint(temperature, humidity)
        dp = self.metar_temperature(dp)
        self.logger.debug('Dewpoint: {}'.format(dp))
        return dp

    def metar_temperature(self, temperature):
        """
        Convert temperature to a METAR format: an integer number; if negative - prepend with 'M'
        :param int or float temperature: temperature
        :return str: temperature in METAR format
        """

        sign = 'M' if temperature < 0 else ''
        temperature = int(abs(round(temperature)))
        temperature = '{}{:02}'.format(sign, temperature)
        self.logger.debug('Temperature: {}'.format(temperature))
        return temperature

    def metar_wind(self, wind):
        """
        Extract the wind details from the wind dict as returned by pyowm get_wind()
        Convert the values into a METAR string
        :param dict wind: wind data as returned by pyowm get_wind()
        :return str: METAR code for wind
        """

        wind_dir = wind.get('deg')
        wind_speed = wind.get('speed')
        wind_gust = wind.get('gust')

        if not wind_dir or self.mps_to_kt(wind_speed) < 2:
            wind_dir = '000'
        else:
            wind_dir = '{:03}'.format(wind_dir)

        wind_speed = '{:02}'.format(int(self.mps_to_kt(wind_speed))) if wind_speed is not None else '00'
        wind_gust = 'G{:02}'.format(int(self.mps_to_kt(wind_gust))) if wind_gust else ''

        wv = '{}{}{}KT'.format(wind_dir, wind_speed, wind_gust)
        self.logger.debug('Wind dir: {}'.format(wv))
        return wv

    def metar_pressure(self, pressure):
        """
        Extract sea-level and/or local pressure from pressure dict as returned by pyowm get_pressure()
        Convert the data into a METAR code
        :param dict pressure: pressure data as returned by pyowm get_pressure()
        :return str: METAR code for pressure (QNH, QFE)
        """

        result = ['', '']

        if pressure['sea_level']:
            result[0] = 'Q{:04}'.format(pressure['sea_level'])

        if pressure['press']:
            result[1] = 'QFE{:04}'.format(pressure['press'])

        pressure = ' '.join(result).strip()
        self.logger.debug('Pressure: {}'.format(pressure))
        return pressure

    def metar_weather(self, wxcode):
        """
        Extract OpenWeatherMap weather codes and convert into METAR weather
        :param int or list[int] wxcode: weather codes
        :return str: METAR codes for weather phenomena
        """

        if type(wxcode) == list:
            weather = list()
            for item in wxcode:
                if item != 800:
                    weather.append(wxcodes[item])
            weather = ' '.join(weather).strip()

        else:
            weather = ''
            if wxcode < 800:
                weather = wxcodes[wxcode]

        self.logger.debug('Weather phenomena: {}'.format(weather))
        return weather

    def get_weather(self, latitude, longitude):
        """
        Get current weather for given coordinates from OpenWeatherMap
        The weather observation will be saved in self.wx property for access by other methods

        :param float latitude: GPS latitude
        :param float longitude: GPS longitude
        """
        now = datetime.now()
        if now.timestamp() - self.wx_acquisition_ts > self.wx_refresh_interval:
            self.logger.info('Getting new weather for: {}, {}'.format(latitude, longitude))
            observations = None

            try:
                observations = self.owm.weather_around_coords(latitude, longitude, limit=1)
            except Exception as e:
                self.logger.exception('Error getting weather: {}: {}'.format(type(e).__name__,
                                                                             str(e)))
            else:
                if len(observations):
                    self.wx = observations[0]
                    self.wx_acquisition_ts = now.timestamp()
                    self.logger.debug('Weather: {}'.format(self.wx.get_weather().to_JSON()))

    def generate_metar(self):
        """
        Generate METAR code from the latest weather observation in self.wx

        :return str: METAR code
        """

        if not self.owm:
            self.logger.warn('Error establishing connection to OpenWeatherMap')
            return

        lat = self.configuration.getfloat('weather', 'latitude', fallback=None)
        lon = self.configuration.getfloat('weather', 'longitude', fallback=None)

        if lat is not None and lon is not None:
            self.get_weather(lat, lon)
            if not self.wx:
                self.logger.warn('Failed to download weather')
                return ''

            location = self.wx.get_location().get_name()

            obtime = self.wx.get_weather().get_reference_time()
            obtime = datetime.fromtimestamp(obtime).strftime('%d%H%M')

            wind = self.wx.get_weather().get_wind()
            wv = self.metar_wind(wind)

            visibility = self.wx.get_weather().get_visibility_distance()

            wxcode = self.wx.get_weather().get_weather_code()
            weather = self.metar_weather(wxcode)

            cloud = self.cloud(self.wx.get_weather().get_clouds())

            temps = self.wx.get_weather().get_temperature('celsius')
            temperature = self.metar_temperature(temps['temp'])

            humidity = self.wx.get_weather().get_humidity()
            dewpoint = self.wx.get_weather().get_dewpoint() or self.metar_dewpoint(temps['temp'], humidity)

            rh = 'RH{}'.format(humidity)
            t_dp = '{}/{}'.format(temperature, dewpoint)

            pressure = self.wx.get_weather().get_pressure()
            pressure = self.metar_pressure(pressure)

            wx = ['PsMETAR']
            for item in [location.upper(), obtime, wv, visibility, weather, cloud, t_dp, rh, pressure]:
                if item:
                    wx.append(item)

            wx = ' '.join(wx) + '='
            self.logger.info(wx)

            return wx

    #
    # Methods for displaying the information on the Scroll pHAT
    #

    def scroll_netinfo(self, interfaces, scroll_interval=0.1, display_count=1):
        """
        Display Network Information on the Scroll pHAT
        :param list interfaces: a list of interfaces for which to display their IPv4 configuration
        :param float scroll_interval: time in seconds to shift the scroll phat display by one pixel to the left
        :param int display_count: how many times to repeat a given information in the display cycle
        """

        sysinfo = list()
        for interface in interfaces:
            sysinfo.append('{}:{}'.format(interface[0].upper(), self.get_ip(interface)))
        self.scroll_text(sysinfo, scroll_interval, display_count)

    def scroll_cpuload(self, scroll_interval=0.1, display_count=1):
        """
        Display CPU load on the Scroll pHAT
        :param float scroll_interval: time in seconds to shift the scroll phat display by one pixel to the left
        :param int display_count: how many times to repeat a given information in the display cycle
        """

        loadavg = os.getloadavg()
        loadavg = 'L:{}/{}/{}'.format(loadavg[0], loadavg[1], loadavg[2])
        self.scroll_text(loadavg, scroll_interval, display_count)

    def scroll_time(self, scroll_interval=0.1, display_count=4):
        """
        Display current time on the Scroll pHAT
        :param float scroll_interval: time in seconds to shift the scroll phat display by one pixel to the left
        :param int display_count: how many times to repeat a given information in the display cycle
        """

        scrollphat.clear()
        while display_count > 0:
            current_time = self.get_time()
            scrollphat.write_string(current_time, 11)
            display_count -= 1
            for _ in range(scrollphat.buffer_len()):
                current_time = self.get_time()
                scrollphat.write_string(current_time, 11)
                scrollphat.scroll()
                time.sleep(scroll_interval)

    def scroll_weather(self, scroll_interval=0.1, display_count=1):
        """
        Display current weather on the Scroll pHAT
        :param float scroll_interval: time in seconds to shift the scroll phat display by one pixel to the left
        :param int display_count: how many times to repeat a given information in the display cycle
        """

        wx = self.generate_metar()
        self.scroll_text(wx, scroll_interval, display_count)

    def scroll_text(self, text, scroll_interval=0.1, display_count=1):
        """
        Display arbitrary text on the Scroll pHAT
        :param str or list[str] text: text message to display
        :param float scroll_interval: time in seconds to shift the scroll phat display by one pixel to the left
        :param int display_count: how many times to repeat a given information in the display cycle
        """

        if not len(text):
            return

        if type(text) == list:
            text = ' | '.join(text)

        self.logger.debug('Scrolling: {}'.format(text))
        while display_count > 0:
            display_count -= 1
            scrollphat.clear()
            scrollphat.write_string(text, 11)
            for i in range(0, scrollphat.buffer_len()):
                scrollphat.scroll()
                time.sleep(scroll_interval)

    @staticmethod
    def scroll_cpugraph(duration=15, scroll_interval=0.2):
        """
        Plot the CPU load on the Scroll pHAT (graphically)
        :param int duration: how long to keep scrolling the CPU load graph for
        :param float scroll_interval: time in seconds to shift the scroll phat display by one pixel to the left
        """

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
        """
        Main program run loop
        """

        scrollphat.set_brightness(self.scrollphat_brightness)
        rotate = self.configuration.getboolean('scrollphat', 'flip', fallback=False)
        scrollphat.set_rotate(rotate)

        loop_runs = 0
        while True:
            try:
                if self.configuration.getboolean('scrollphat', 'time_display', fallback=False):
                    interval = self.configuration.getfloat('scrollphat', 'time_interval',
                                                           fallback=self.DEFAULT_INTERVAL)
                    display_count = self.configuration.getint('scrollphat', 'time_display_count',
                                                              fallback=self.DEFAULT_DISPLAY_COUNT)
                    self.scroll_time(display_count=display_count, scroll_interval=interval)

                if self.configuration.getboolean('scrollphat', 'network_display', fallback=False):
                    if divmod(loop_runs, 10)[1] == 0:
                        interfaces = ['eth0', 'wlan0']
                        interval = self.configuration.getfloat('scrollphat', 'network_interval',
                                                               fallback=self.DEFAULT_INTERVAL)
                        display_count = self.configuration.getint('scrollphat', 'network_display_count',
                                                                  fallback=self.DEFAULT_DISPLAY_COUNT)
                        self.scroll_netinfo(interfaces, scroll_interval=interval, display_count=display_count)

                if self.configuration.getboolean('scrollphat', 'cpuload_display', fallback=False):
                    display_count = self.configuration.getint('scrollphat', 'cpuload_display_count',
                                                              fallback=self.DEFAULT_DISPLAY_COUNT)
                    interval = self.configuration.getint('scrollphat', 'cpuload_interval',
                                                         fallback=self.DEFAULT_INTERVAL)
                    self.scroll_cpuload(display_count=display_count, scroll_interval=interval)

                if self.configuration.getboolean('scrollphat', 'cpugraph_display', fallback=False):
                    duration = self.configuration.getint('scrollphat', 'cpugraph_duration', fallback=15)
                    interval = self.configuration.getfloat('scrollphat', 'cpugraph_interval',
                                                           fallback=self.DEFAULT_INTERVAL)
                    self.scroll_cpugraph(duration=duration, scroll_interval=interval)

                if self.configuration.getboolean('scrollphat', 'weather_display', fallback=False):
                    display_count = self.configuration.getint('scrollphat', 'weather_display_count',
                                                              fallback=self.DEFAULT_DISPLAY_COUNT)
                    interval = self.configuration.getfloat('scrollphat', 'weather_interval',
                                                           fallback=self.DEFAULT_INTERVAL)
                    self.scroll_weather(scroll_interval=interval, display_count=display_count)

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
