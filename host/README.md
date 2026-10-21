# Host side: discovery

The apps find real OpenHamShack boxes by Bonjour (`_openhamshack._tcp`). The fakebox's
container can't announce itself, so `announce.py` runs on shack as a user
service and announces it only while its device port answers. Off in Dock360
means gone from every device's "On this network" list within seconds.

Needs, on the host (done on shack 09/10/2026):

- `avahi-daemon` and `avahi-utils`, with `allow-interfaces=eno1` in
  `/etc/avahi/avahi-daemon.conf` so nothing is advertised on tailscale or the
  podman networks (`publish-workstation` and `publish-hinfo` are off by default), and `use-ipv6=no`: shack has only ULA IPv6 and the firewall rules for test devices are IPv4, so a device that picked the v6 address would fail to connect.
- ufw: `allow in on eno1 from 198.51.100.0/24 to any port 5353 proto udp`,
  because shack's firewall otherwise drops the devices' mDNS queries.
- `cp openhamshack-fakebox-announce.service ~/.config/systemd/user/ &&
  systemctl --user daemon-reload && systemctl --user enable --now openhamshack-fakebox-announce`

The announcer itself is always running; it is the box being on that decides
whether anything is announced.
