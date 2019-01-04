"""
Unit testing of the piserverstatusd.py module

To run:
nosetests -s test_piserverstatusd.py
"""

import unittest

import piserverstatusd

class PiServerStatusdTestCase(unittest.TestCase):

    def setUp(self):
        self.daemon = piserverstatusd.StatusDaemon('/tmp/test_piserverstatusd.pid')

    def test_mps_to_kt(self):
        speed = 100
        self.assertAlmostEqual(self.daemon.mps_to_kt(speed), 51.44, 2)

    def test_cloud(self):
        self.assertEqual('', self.daemon.cloud(0))
        self.assertEqual('FEW', self.daemon.cloud(12))
        self.assertEqual('SCT', self.daemon.cloud(30))
        self.assertEqual('BKN', self.daemon.cloud(70))
        self.assertEqual('OVC', self.daemon.cloud(99))

    def test_dewpoint(self):
        self.assertAlmostEqual(self.daemon.dewpoint(40, 50), 27.59, 2)

    def test_metar_dewpoint(self):
        test_values = [
            {'t': 40, 'rh': 50, 'dp': '28'},
            {'t': -10, 'rh': 66, 'dp': 'M15'},
            {'t': 25, 'rh': 100, 'dp': '25'},
            {'t': 10, 'rh': 50, 'dp': '00'}
        ]

        for vals in test_values:
            self.assertEqual(vals['dp'], self.daemon.metar_dewpoint(vals['t'], vals['rh']))

    def test_metar_temperature(self):
        test_values = [
            (0, '00'), (-1, 'M01'), (-0.51, 'M01'), (-0.5, 'M00'), (15, '15')
        ]

        for vals in test_values:
            self.assertEqual(vals[1], self.daemon.metar_temperature(vals[0]))

    def test_metar_wind(self):
        test_values = [
            {'speed': 20, 'deg': 320, 'result': '32010KT'},
            {'speed': 2, 'deg': 320, 'result': '00001KT'},
            {'speed': 50, 'deg': 90, 'gust': 85, 'result': '09025G43KT'}
        ]

        for vals in test_values:
            self.assertEqual(vals['result'], self.daemon.metar_wind(vals))

    def test_metar_pressure(self):
        test_values = [
            {'press': 1043, 'sea_level': 1046, 'result': 'Q1046 QFE1043'},
            {'press': 1043, 'sea_level': None, 'result': 'QFE1043'},
            {'press': None, 'sea_level': 1046, 'result': 'Q1046'},
            {'press': 997, 'sea_level': 999, 'result': 'Q0999 QFE0997'},
        ]

        for vals in test_values:
            self.assertEqual(vals['result'], self.daemon.metar_pressure(vals))

    def test_metar_weather(self):
        self.assertEqual('SQ TSRA', self.daemon.metar_weather([771, 201]))
        self.assertEqual('', self.daemon.metar_weather(800))
        self.assertEqual('TSRA', self.daemon.metar_weather([201, 800]))

