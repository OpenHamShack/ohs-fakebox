# OpenHamShack Fakebox

The OpenHamShack Remote test fakebox (`openhamshack-remote/tools/fakebox.py`) as a
Dock360 app, so it is started, stopped and reconfigured from the Dock360
admin interface instead of over SSH. It serves the **real** OpenHamShack
(Classic) box API, imported from the appliance code (`~/openhamshack`), with the radio and tuner faked.
Nothing in it can transmit.

## Ports

| Where | Port | What |
| --- | --- | --- |
| LAN, published | the "Devices" extra port (see `dock360ctl status` / the module page) -> container 18081 | Box API as a browser elsewhere sees it: PIN rules apply. The only thing devices should reach. |
| Container loopback only | 18080 | Box API as the box's own screen: **always authorised**. Never published. |
| Dock360 web | container 8080, behind Dock360 sign-in | Status page, clip upload. |

The host port for "Devices" is allocated by Dock360 at install and stays the
same for the life of the install. The host firewall decides which devices get
in; on shack that is ufw, e.g.

    sudo ufw allow in on eno1 from <device ip> to any port <devices port> proto tcp comment "OpenHamShack fakebox - <device>"

To use the box-screen view (change the PIN as the box would, read secrets):

    cd /tmp && sudo -u dock360 XDG_RUNTIME_DIR=/run/user/$(id -u dock360) \
      podman exec dock360-openhamshack-fakebox python3 -c \
      "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:18080/api/status').read()[:300])"

## Settings

All in the Dock360 admin interface (module settings), or from a shell:

    sudo dock360ctl module set openhamshack-fakebox BOX_CODEC=h264 BOX_LOSS_PERCENT=0.5

| Setting | Values | fakebox switch |
| --- | --- | --- |
| `BOX_CLIP` | a file name in `clips/`; empty or `testcard` for the test card | `FAKEBOX_FILE` |
| `BOX_CODEC` | `h264`, `hevc` | `FAKEBOX_CODEC` |
| `BOX_AUDIO` | `mp2`, `aac` | `FAKEBOX_AUDIO` |
| `BOX_LOSS_PERCENT` | 0-100, decimals allowed | `FAKEBOX_CORRUPT` (as a fraction) |
| `BOX_CUT_SECONDS` | whole seconds, 0 = never | `FAKEBOX_CUT` |
| `BOX_NO_SIGNAL` | on/off | `FAKEBOX_NOSIGNAL` |
| `BOX_PIN` | 1-32 letters/digits | the box's `remotePin` |

A change restarts the container, which ends every PIN session. A value that
makes no sense falls back to the harmless default and is listed on the status
page rather than stopping the app. `app/settings.py` is the mapping.

## Clips

Clips live in the data volume, `/var/lib/dock360/modules/openhamshack-fakebox/data/clips/`
on the host. Upload more from the app's page (up to 2 GB each), or copy them in:

    sudo install -o dock360 -g dock360 -m 0644 film.mp4 /var/lib/dock360/modules/openhamshack-fakebox/data/clips/

then set `BOX_CLIP` to the file name. The volume is not backed up: the clips
are test films that exist elsewhere.

## SRT camera ingest

The daemon's SRT listener (udp 19710) runs inside the container but is not
published. The box tells a phone which port and address to send to
(`/api/stream/larix`), and inside a container those are the container's own,
not the host's; Dock360 would also allocate a different host port number. On
top of that, shack's ufw drops inbound UDP. Making it work means declaring a
udp extra port (which needs a reinstall, or a manual allocation - extra ports
are only allocated at install) and teaching `app/main.py` to advertise the
host's address and port. Until then, test camera ingest against the systemd
fakebox or a real box.

## The vendored code

`vendor/` holds `fakebox.py`, taken out of git at the commit named in
`vendor/PINNED`, and the Classic `appliance/` directory it drives, a frozen
copy at Classic's last commit - Classic was archived on 21/10/2026 and has no
live repo to take a newer copy from. To pick up newer `fakebox.py`:

    tools/refresh-vendor.sh                 # HEAD of ~/openhamshack-remote
    tools/refresh-vendor.sh <openhamshack-remote-rev>
    python3 -m unittest discover -s tests
    git add vendor && git commit            # name the commit in the message

then bump `version` in `module.yaml` and deploy (below). See
`tools/refresh-vendor.sh`'s own comment for the rare case of needing to
re-vendor Classic's frozen commit itself.

## Building and deploying

    sudo dock360ctl dev link ~/dock360-apps-openhamshack-fakebox/openhamshack-fakebox   # once
    sudo dock360ctl dev sync openhamshack-fakebox
    sudo dock360ctl update openhamshack-fakebox      # dev sync's own job never runs

Start and stop with `sudo dock360ctl module start|stop|restart openhamshack-fakebox`
or from the admin interface. Logs: `sudo dock360ctl module logs openhamshack-fakebox`.

## Tests

Plain `unittest`; `test_running.py` starts the real app on spare ports, so it
runs beside a fakebox already on the host. Needs ffmpeg for the stream test.

    python3 -m unittest discover -s tests
