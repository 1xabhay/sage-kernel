"""The guard's rules, read as text: there is no nft on the Mac, and nothing here touches the robot.
`make -C robot guard` checks the syntax on the robot (nft -c) before it loads anything.

    cd apps/reachy_sage_voice/robot && python3 -m unittest test_sage_guard
"""
import pathlib
import re
import unittest

import hosts

HERE = pathlib.Path(__file__).parent
RULES = [line.split("#")[0].strip() for line in (HERE / "sage-guard.nft").read_text().splitlines()]
RULES = [line for line in RULES if line]


def index(pattern: str) -> int:
    """The first rule matching `pattern`; fails the test if there is none."""
    for i, rule in enumerate(RULES):
        if re.search(pattern, rule):
            return i
    raise AssertionError(f"no rule matches {pattern!r}")


class Guard(unittest.TestCase):
    def test_udp_answers_the_mac_by_its_ip_or_its_hardware_address(self):
        index(r"^meta l4proto udp ip saddr \$MAC_IP accept$")
        index(r"^meta l4proto udp ether saddr \$MAC_HW accept$")

    def test_mdns_answers_the_lan_only(self):
        index(r"^udp dport 5353 ip saddr \$LAN accept$")
        index(r"^udp dport 5353 ip6 saddr fe80::/10 accept$")
        self.assertNotRegex("\n".join(RULES), r"udp dport 5353 accept")

    def test_the_robots_own_dhcp_answers_get_in(self):
        index(r"^udp sport 67 udp dport 68 accept$")

    def test_every_other_udp_packet_is_counted_and_dropped_last(self):
        drop = index(r"^meta l4proto udp counter drop$")
        for accept in (r"udp ip saddr \$MAC_IP", r"udp ether saddr \$MAC_HW", r"5353 ip saddr", r"5353 ip6",
                       r"sport 67", r"established,related"):
            self.assertLess(index(accept), drop, accept)

    def test_tcp_is_still_guarded_and_the_policy_still_accepts(self):
        index(r"^tcp dport \{ 22, 8000, 8443 \} counter drop$")
        index(r"policy accept;")  # outbound and loopback are never touched

    def test_the_addresses_come_only_from_the_robots_own_hosts_file(self):
        include = index(r'^include "/etc/sage-guard/hosts\.nft"$')
        self.assertLess(include, index(r"^table inet sage_guard$"))
        self.assertEqual([r for r in RULES if r.startswith("define")], [])

    def test_braces_balance(self):
        text = "\n".join(RULES)
        self.assertEqual(text.count("{"), text.count("}"))


# A documentation address (RFC 5737) and a locally administered hardware address, built here so
# no address literal sits in a tracked file.
HW = ":".join(["02", "00", "5e", "00", "53", "01"])


class Hosts(unittest.TestCase):
    def test_this_macs_addresses_become_the_three_defines(self):
        self.assertEqual(hosts.render("192.0.2.7", HW.upper(), "255.255.255.0"),
                         f"define MAC_IP = 192.0.2.7\ndefine MAC_HW = {HW}\ndefine LAN = 192.0.2.0/24\n")

    def test_the_lan_is_the_netmask_as_a_cidr(self):
        self.assertIn("define LAN = 192.0.0.0/22\n", hosts.render("192.0.2.7", HW, "255.255.252.0"))

    def test_anything_but_an_address_is_refused(self):
        for ip, hw, mask in [("", HW, "255.255.255.0"), ("192.0.2.7; flush ruleset", HW, "255.255.255.0"),
                             ("192.0.2.7", "", "255.255.255.0"), ("192.0.2.7", HW + "\ndefine X = 1", "255.255.255.0"),
                             ("192.0.2.7", HW, "255.0.255.0"), ("8.8.8.8", HW, "255.255.255.0")]:
            with self.assertRaises(ValueError, msg=(ip, hw, mask)):
                hosts.render(ip, hw, mask)

    def test_the_makefile_writes_it_and_takes_the_robot_from_a_local_env(self):
        make = (HERE / "Makefile").read_text()
        self.assertRegex(make, r"(?m)^-include \.env$")
        self.assertRegex(make, r"(?m)^ROBOT \?= pollen@reachy-mini\.local$")
        self.assertRegex(make, r"(?m)^allow-this-mac:")
        self.assertIn(".env", (HERE / ".gitignore").read_text().split())


class Relay(unittest.TestCase):
    def test_the_relay_drop_in_points_the_central_url_at_a_closed_local_port(self):
        # reachy_mini 1.10.0 media/central_signaling_relay.py reads REACHY_CENTRAL_URL once, at import.
        text = (HERE / "50-sage-no-relay.conf").read_text()
        self.assertIn("[Service]\n", text)
        self.assertRegex(text, r"(?m)^Environment=REACHY_CENTRAL_URL=http://127\.0\.0\.1:9$")


if __name__ == "__main__":
    unittest.main()
