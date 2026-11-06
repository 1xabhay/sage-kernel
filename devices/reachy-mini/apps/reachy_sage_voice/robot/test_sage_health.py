"""The robot's health logger: one line every few seconds, so the last seconds before a drop are on record.

    cd apps/reachy_sage_voice/robot && python3 -m unittest test_sage_health
"""
import os
import tempfile
import unittest

import sage_health

LINK = """Connected to <bssid> (on wlan0)
	SSID: Home-2g
	freq: 2422.0
	signal: -33 dBm
	rx bitrate: 65.0 MBit/s
	tx bitrate: 72.2 MBit/s
"""
STATION = """Station <bssid> (on wlan0)
	inactive time:	40 ms
	tx retries:	1203
	tx failed:	7
	beacon loss:	2
	signal:  	-35 [-35, -38] dBm
"""


# /proc/944/stat on the robot, 2026-10-22: utime 351257, stime 85622 (fields 14 and 15).
STAT = ("944 (python) R 930 930 930 0 -1 4194304 549568 16690 896 104 351257 85622 159 45 20 0 62 0 1789 "
        "1299390464 71502 18446744073709551615")
DAEMON_CMDLINE = b"python\0-u\0-m\0reachy_mini.daemon.app.main\0--wireless-version\0--no-wake-up-on-start\0"
STATUS = ('{"state": "running", "backend_status": {"ready": true, "control_loop_stats": '
          '{"mean_control_loop_frequency": 49.23863833772247, "max_control_loop_interval": 0.028326988220214844, '
          '"nb_error": 0}}}')


def fake_proc(processes: dict) -> str:
    """A /proc with `{pid: (cmdline bytes, stat text)}`."""
    root = tempfile.mkdtemp()
    for pid, (cmdline, stat) in processes.items():
        os.mkdir(os.path.join(root, str(pid)))
        with open(os.path.join(root, str(pid), "cmdline"), "wb") as f:
            f.write(cmdline)
        with open(os.path.join(root, str(pid), "stat"), "w") as f:
            f.write(stat)
    os.mkdir(os.path.join(root, "sys"))  # not a pid
    return root


class Parsing(unittest.TestCase):
    def test_throttled_flags_are_named(self):
        self.assertEqual(sage_health.throttled("throttled=0x50005"),
                         {"throttled": "0x50005", "undervolt_now": 1, "capped_now": 0, "throttled_now": 1,
                          "undervolt_since_boot": 1, "throttled_since_boot": 1})
        self.assertEqual(sage_health.throttled("throttled=0x0")["undervolt_now"], 0)
        self.assertEqual(sage_health.throttled(""), {})

    def test_temperature(self):
        self.assertEqual(sage_health.temperature("temp=43.3'C"), {"temp_c": 43.3})
        self.assertEqual(sage_health.temperature("nope"), {})

    def test_wifi_link_and_station_counters(self):
        self.assertEqual(sage_health.wifi(LINK, STATION),
                         {"wifi": "up", "signal_dbm": -33, "rx_mbit": 65.0, "tx_mbit": 72.2,
                          "tx_retries": 1203, "tx_failed": 7, "beacon_loss": 2})
        self.assertEqual(sage_health.wifi("Not connected.", ""), {"wifi": "down"})

    def test_wifi_power_save(self):
        self.assertEqual(sage_health.power_save("Power save: off\n"), {"power_save": "off"})
        self.assertEqual(sage_health.power_save("Power save: on\n"), {"power_save": "on"})
        self.assertEqual(sage_health.power_save(""), {})

    def test_the_daemons_pid_is_found_by_its_command_line_without_systemd(self):
        proc = fake_proc({1: (b"/sbin/init\0", STAT), 930: (b"/bin/bash\0launcher.sh\0", STAT),
                          944: (DAEMON_CMDLINE, STAT)})
        self.assertEqual(sage_health.daemon_pid(proc), 944)
        self.assertIsNone(sage_health.daemon_pid(fake_proc({1: (b"/sbin/init\0", STAT)})))

    def test_cpu_ticks_are_user_plus_system_time(self):
        self.assertEqual(sage_health.cpu_ticks(STAT), 351257 + 85622)
        # A process name with spaces and parentheses does not shift the fields.
        self.assertEqual(sage_health.cpu_ticks(STAT.replace("(python)", "(a (b) c)")), 351257 + 85622)

    def test_daemon_cpu_is_the_tick_delta_over_the_wall_delta(self):
        busy = STAT.replace(" 351257 85622 ", " 351357 85672 ")  # +150 ticks
        last = {}
        first = sage_health.daemon_cpu(fake_proc({944: (DAEMON_CMDLINE, STAT)}), last, now=100.0, hz=100)
        self.assertEqual(first, {})  # nothing to compare with yet
        self.assertEqual(sage_health.daemon_cpu(fake_proc({944: (DAEMON_CMDLINE, busy)}), last, now=110.0, hz=100),
                         {"daemon_cpu": 15.0})  # 1.5 s of CPU in 10 s; over 100 means more than one core

    def test_a_restarted_daemon_starts_its_cpu_count_again(self):
        last = {}
        sage_health.daemon_cpu(fake_proc({944: (DAEMON_CMDLINE, STAT)}), last, now=100.0, hz=100)
        self.assertEqual(sage_health.daemon_cpu(fake_proc({1200: (DAEMON_CMDLINE, STAT)}), last, now=110.0, hz=100), {})
        self.assertEqual(sage_health.daemon_cpu(fake_proc({}), last, now=120.0, hz=100), {})

    def test_the_control_loop_rate_its_worst_gap_and_errors_come_from_the_daemon(self):
        self.assertEqual(sage_health.daemon(lambda: STATUS),
                         {"daemon": "running", "backend_ready": 1, "loop_hz": 49.2, "loop_max_ms": 28,
                          "loop_errors": 0})

    def test_one_line_of_key_values_in_a_fixed_order(self):
        line = sage_health.line({"temp_c": 43.3, "wifi": "up", "throttled": "0x0"})
        self.assertEqual(line, "sage-health throttled=0x0 temp_c=43.3 wifi=up")

    def test_the_daemon_is_asked_itself_so_a_hung_one_shows(self):
        self.assertEqual(sage_health.daemon(lambda: '{"state": "running", "backend_status": {"ready": true}}'),
                         {"daemon": "running", "backend_ready": 1})
        self.assertEqual(sage_health.daemon(lambda: '{"state": "error", "backend_status": {"ready": false}}'),
                         {"daemon": "error", "backend_ready": 0})

        def hung():
            raise TimeoutError("timed out")
        self.assertEqual(sage_health.daemon(hung), {"daemon": "unreachable"})

    def test_a_sample_survives_a_command_that_fails(self):
        def run(argv):
            if argv[0] == "vcgencmd":
                raise OSError("no vcgencmd")
            if argv[:4] == ["iw", "dev", "wlan0", "get"]:
                return "Power save: off\n"
            return {"iw": LINK}.get(argv[0], "")
        sample = sage_health.sample(run, loadavg="0.64 0.16 0.05 1/200 900", meminfo="MemAvailable:  3395000 kB\n",
                                    status=lambda: STATUS, proc=fake_proc({944: (DAEMON_CMDLINE, STAT)}), last={})
        self.assertEqual(sample["load1"], 0.64)
        self.assertEqual(sample["mem_avail_mb"], 3315)
        self.assertEqual(sample["daemon"], "running")
        self.assertEqual(sample["signal_dbm"], -33)
        self.assertNotIn("temp_c", sample)
        self.assertEqual(sample["power_save"], "off")
        self.assertEqual(sample["loop_max_ms"], 28)
        self.assertNotIn("daemon_cpu", sample)  # the first sample has nothing to compare with


if __name__ == "__main__":
    unittest.main()
