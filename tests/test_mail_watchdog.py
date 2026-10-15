import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('watchdog', Path(__file__).resolve().parents[1] / 'ops/mail_watchdog.py')
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'state.json'
        self.current = {'id':'fixture', 'running':True, 'health':'unhealthy', 'checks':[]}
        self.inspect = patch.object(watchdog, 'inspect_container', side_effect=lambda: dict(self.current)).start()
        self.docker = patch.object(watchdog, 'docker', return_value='fixture').start()
        patch.object(watchdog, 'emit').start()
        self.addCleanup(patch.stopall)

    def tick(self, now):
        watchdog.check_once(self.path, now)

    def test_sustained_failure_and_cooldown(self):
        self.tick(1000)
        self.tick(1030)
        self.docker.assert_not_called()
        self.tick(1060)
        self.docker.assert_any_call('restart', '--time', '20', 'fixture')
        self.docker.reset_mock()
        self.tick(1090)
        self.tick(1200)
        self.docker.assert_not_called()
        self.tick(1660)
        self.docker.assert_any_call('restart', '--time', '20', 'fixture')

    def test_healthy_starting_stopped_never_restarted(self):
        for health, running in [('healthy',True), ('starting',True), ('unhealthy',False), ('none',True)]:
            self.current.update(health=health, running=running)
            self.tick(1000)
            self.tick(1200)
        self.docker.assert_not_called()

    def test_recovery_and_replacement_reset_confirmation(self):
        self.tick(1000)
        self.current['health'] = 'healthy'
        self.tick(1030)
        self.current['health'] = 'unhealthy'
        self.tick(1100)
        self.current['id'] = 'replacement'
        self.tick(1160)
        self.docker.assert_not_called()
        self.tick(1220)
        self.docker.assert_any_call('restart', '--time', '20', 'replacement')

    def test_stop_between_checks_prevents_restart(self):
        self.tick(1000)
        self.inspect.side_effect = [self.current, {**self.current, 'running':False}]
        self.tick(1060)
        self.docker.assert_not_called()

    def test_inspection_failure_does_not_restart(self):
        self.inspect.side_effect = subprocess.CalledProcessError(1, 'docker')
        with self.assertRaises(subprocess.CalledProcessError):
            self.tick(1000)
        self.docker.assert_not_called()

    def test_failed_restart_retains_cooldown(self):
        self.tick(1000)
        self.docker.side_effect = ['processes', subprocess.TimeoutExpired('docker', 50)]
        with self.assertRaises(subprocess.TimeoutExpired):
            self.tick(1060)
        self.assertEqual(json.loads(self.path.read_text())['last_attempt'], 1060)
        self.docker.reset_mock()
        self.tick(1200)
        self.docker.assert_not_called()


if __name__ == '__main__':
    unittest.main()
