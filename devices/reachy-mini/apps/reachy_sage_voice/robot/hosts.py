"""The guard's hosts file, /etc/sage-guard/hosts.nft on the robot: this Mac's IPv4 address, its
Wi-Fi hardware address and its LAN as a CIDR. Only the robot keeps it; sage-guard.nft includes it.
`make -C robot allow-this-mac` reads en0 and writes it there.

    python3 -I hosts.py IP HWADDR NETMASK
"""
import ipaddress
import re
import sys


def render(ip: str, hw: str, netmask: str) -> str:
    """The three defines; ValueError on anything that is not a private address, a hardware address
    and a netmask, since the text goes into the robot's firewall as root."""
    address = ipaddress.IPv4Address(ip)
    lan = ipaddress.IPv4Network(f"{address}/{netmask}", strict=False)
    if not address.is_private:
        raise ValueError(f"not a LAN address: {ip}")
    if not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", hw.lower()):
        raise ValueError(f"not a hardware address: {hw!r}")
    return f"define MAC_IP = {address}\ndefine MAC_HW = {hw.lower()}\ndefine LAN = {lan}\n"


if __name__ == "__main__":
    try:
        sys.stdout.write(render(*sys.argv[1:4]))
    except (TypeError, ValueError) as error:
        sys.exit(f"hosts.py: {error} (usage: hosts.py IP HWADDR NETMASK)")
