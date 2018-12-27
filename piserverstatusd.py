#!/usr/bin/env python3

"""
PiServerStatus Daemon

This program displays raspberry pi statistics on the Scroll pHAT which needs to be attached to the pi.
"""

import argparse
import configparser
import datetime
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

DEFAULT_BRIGHTNESS = 10

class StatusDaemon(Daemon):

    def __init__(self, pidfile, config_file=None, stdin='/dev/null', stdout='/dev/null', stderr='/dev/null'):
        """
        Override the __init__() method of the Daemon superclass to add extra properties
        """
        self.scrollphat_brightness = DEFAULT_BRIGHTNESS
        self.owm = pyowm.OWM(API_key='40901ca687a4cfd6ad115b22c8686bc1')
        self.weather = None

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
            self.scrollphat_brightness = int(self.configuration.get('scrollphat', 'brightness', fallback=DEFAULT_BRIGHTNESS))

    @staticmethod
    def get_ip(ifname):
        ifname = ifname[:15].encode('utf-8')
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        return socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, struct.pack('256s', ifname))[20:24])

    @staticmethod
    def get_ipv6(ifname):
        pass

    def get_time(self):
        now = datetime.datetime.now()
        return now.strftime('%H:%M:%S')

    def get_weather(self):
        observation = self.owm.weather_at_place('Bray,IE')
        self.weather = observation.get_weather()

    def run(self):
        scrollphat.set_brightness(self.scrollphat_brightness)

        displayed_text = list()
        for interface in ['eth0', 'wlan0']:
            displayed_text.append('{}:{}'.format(interface, self.get_ip(interface)))

        cpu_values = [0] * 11

        while True:
            try:
                scrollphat.clear()
                for _ in range(150):
                    scrollphat.write_string(self.get_time(), 4)
                    scrollphat.scroll()
                    time.sleep(0.1)

                scrollphat.clear()
                scrollphat.write_string(' | '.join(displayed_text), 11)
                for i in range(0, scrollphat.buffer_len()):
                    scrollphat.scroll()
                    time.sleep(0.1)

                scrollphat.clear()
                for _ in range(75):
                    cpu_values.pop(0)
                    cpu_values.append(psutil.cpu_percent())
                    scrollphat.graph(cpu_values, 0, 25)
                    time.sleep(0.2)

            except KeyboardInterrupt:
                self.logger.info('Exiting')
                scrollphat.clear()
                raise SystemExit(0)

    def stop(self, silent=False):
        scrollphat.clear()
        super().stop(silent)

    def sigterm_handler(self, signo, frame):
        """
        Override the Daemon.sigterm_handler() to turn off the scrollphat when the daemon process is terminated
        """
        scrollphat.clear()
        super().sigterm_handler(signo, frame)

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
