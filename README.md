# lte-pro-access

Keeps SSH to a UniFi LTE Backup Pro (the "device") enabled and keeps socat relays to its modem up, either as a standalone service on a Raspberry Pi (the "Pi", any Linux host works) or embedded as a library in another asyncio program:

- QMI on `qmi_port` (always)
- AT on `at_relay.port` (optional: set `at_relay` to `null` to skip it)

The device relays accept connections only from `pi_address`.

> **Warning:** this software enables SSH on, uploads a socat binary to and runs socat on a device you own. Use it only on devices you own or are authorized to administer, and at your own risk.
>
> **The relays are unauthenticated, unencrypted TCP straight into the modem.** The only protection is socat's source-IP `range=` filter, which accepts the address `pi_address`. Anything on the LAN that can use that source address (a host that takes it over or spoofs it) can send AT commands (the AT relay gives full modem control: SMS, SIM, IMEI, radio settings) and QMI commands to the modem, with no password and no encryption. Run this only on a LAN you fully trust.

## Ports and footprint

- Device `qmi_port` (18222): TCP relay to the QMI proxy, listens on the LTE device, `pi_address` only.
- Device `at_relay.port` (18223), only when `at_relay` is set: TCP relay to `at_relay.device` (`/dev/ttyUSB2`), one client at a time, idle timeout `at_relay.idle_seconds`, `pi_address` only.
- Pi: an abstract Unix socket `qmi-proxy` (no TCP port), fed by the Pi-side relay to `lte_host:qmi_port`. The relay's TCP leg uses keepalive (`keepidle=30,keepintvl=10,keepcnt=3`), so a leg left half-open by an LTE reboot errors within about a minute and the client reconnects.
- SSH to the device is enabled on demand through the UniFi controller. The service does not own or change its port.

Only `/tmp/socat` may be placed on the device. Nothing else is installed there.

## Configuration notes

- Config lives at `/home/pi/.config/lte-pro-access/config.json`. Keep it mode 600: it holds the UniFi credentials.
- `state_dir` (required) is a writable directory. The supervisor keeps the pinned device SSH host key there (`lte_host_key.pub`, mode 600).
- `ssh_key_path` is the private key the Pi uses to log in to the device. `socat_device_binary` is the MIPS socat binary that is hashed and uploaded to the device as `/tmp/socat` (see [Getting the MIPS socat](#getting-the-mips-socat); any socat 1.7.x build for mips_24kc musl works). `socat_pi_binary` is the socat the Pi itself runs for its relay (`/usr/bin/socat`). They are different architectures, so the two paths must differ; the config is rejected when they are equal.
- The device host key is pinned per boot cycle (trust on first use). A different key is accepted only on the first connection after the supervisor re-enabled SSH following a refused connection (a reboot), and only when the UniFi controller confirms dropbear was started. Any other key change is refused, logged at WARNING, and the tick fails without running commands.
- Resetting the host key pin: if the key changed without this service restarting dropbear, every tick fails with a host key error until the pin is cleared. Run `rm <state_dir>/lte_host_key.pub`, then restart the service (`sudo systemctl restart lte-pro-access`). Never start dropbear by hand while this service runs: the service owns it, and a hand-started dropbear changes the host key without the service seeing a refusal.
- `at_relay` is required: an object with `port`, `device` and `idle_seconds`, or `null` when only the QMI relay is needed.
- `unifi` is passed to pyunifiapi: `host`, `username`, `password` for a local controller, or `device_id` (plus the same credentials) for the cloud.
- `config.example.json` is the template and is checked by the test suite, so it cannot drift from the loader.

## Dependencies

`pyunifiapi[cloud]>=0.2.0` is required (the `cloud` extra brings the WebRTC library that SSH enabling uses), and the service exits at startup with an error if it is missing. It is not yet on PyPI, so install it from the GitHub tag until it is published:

```
uv pip install "pyunifiapi[cloud] @ git+https://github.com/seidnerj-oss/pyunifiapi@v0.2.0"
```

Run this after `uv sync --inexact`. A plain `uv sync` removes packages that are not in the lock file.

## Getting the MIPS socat

`fetch-socat` downloads the MIPS socat that the supervisor uploads to the device, so you do not need to build one:

```
uv run python -m lte_pro_access fetch-socat --destination /home/pi/.local/share/lte-pro-access/socat-mips
```

Then set `socat_device_binary` in the config to that path. The command prints the path on success, creates missing parent directories, writes the file atomically with mode 755 and leaves a destination that already holds the pinned binary untouched. Embedders call `lte_pro_access.fetch_socat(destination: Path) -> Path` instead; it raises `SocatFetchError` on a hash mismatch or an unexpected package layout, and `OSError` (including `urllib.error.URLError`) on network or filesystem failures.

The binary is socat 1.7.3.1 (GPL-2.0, with the OpenSSL exception) from the OpenWrt 17.01.6 `mips_24kc` package feed (package `socat_1.7.3.1-1_mips_24kc.ipk`, build recipe in `feeds/packages/net/socat` of the [OpenWrt packages feed](https://github.com/openwrt/packages), source at [dest-unreach.org/socat](http://www.dest-unreach.org/socat/)). It is downloaded from `downloads.openwrt.org` when you run the command, not bundled with this project, which stays MIT. The URL and two SHA-256 hashes are pinned in `src/lte_pro_access/socat_fetch.py`, and both are verified before anything is written:

- the `.ipk`: `57f96162c901467bde1aa1f57c695fffe591e4f924c3760831b83d21dd718e7b`
- the extracted `usr/bin/socat`: `ec8bb6f7b197eb086de29c48457d803347b4252ba2e9eee0e0197b7d871e39a5`

## Install on the Pi

```
git clone <repo-url> /home/pi/projects/lte-pro-access
cd /home/pi/projects/lte-pro-access
uv sync --inexact
uv pip install "pyunifiapi[cloud] @ git+https://github.com/seidnerj-oss/pyunifiapi@v0.2.0"

mkdir -p /home/pi/.config/lte-pro-access /home/pi/.local/state/lte-pro-access
install -m 600 config.example.json /home/pi/.config/lte-pro-access/config.json
$EDITOR /home/pi/.config/lte-pro-access/config.json   # set the UniFi credentials and check the paths

# the unit assumes user "pi" and /home/pi/projects/lte-pro-access: edit User, WorkingDirectory and ExecStart first if yours differ
sudo install -m 644 systemd/lte-pro-access.service /etc/systemd/system/lte-pro-access.service
sudo systemctl daemon-reload
sudo systemctl enable --now lte-pro-access
journalctl -u lte-pro-access -f
```

## Rollback

```
sudo systemctl disable --now lte-pro-access
pgrep -a socat   # should list nothing on the Pi: stopping the unit stops its relay
```

## Dependent services

A service that needs the device reachable adds a systemd drop-in so it starts after this one and is pulled in with it. For a unit named `<unit>.service`, create `/etc/systemd/system/<unit>.service.d/lte-pro-access.conf`:

```
[Unit]
Wants=lte-pro-access.service
After=lte-pro-access.service
```

Then run `sudo systemctl daemon-reload`.

## Using as a library

Another asyncio program can run the supervisor in-process instead of the systemd service. The package is typed (`py.typed`) and exports everything an embedder needs from `lte_pro_access`:

```python
import asyncio
from pathlib import Path

from lte_pro_access import TICK_TIMEOUT_SECONDS, Config, create_supervisor, run_forever

cfg = Config(
    lte_host="192.168.1.20",
    lte_mac="aa:bb:cc:dd:ee:ff",
    pi_address="192.168.1.10",
    ssh_user="root",
    ssh_key_path=Path("/home/pi/.ssh/id_ed25519_lte"),
    state_dir=Path("/home/pi/.local/state/my-app"),
    socat_device_binary=Path("/home/pi/.local/share/my-app/socat-mips"),
    socat_pi_binary=Path("/usr/bin/socat"),
    qmi_port=18222,
    at_relay=None,
    check_interval_seconds=30,
    unifi={"host": "192.168.1.1", "username": "...", "password": "..."},
)

supervisor = create_supervisor(cfg)
task = asyncio.create_task(run_forever(supervisor, cfg.check_interval_seconds, TICK_TIMEOUT_SECONDS, asyncio.sleep))
...
healthy = supervisor.relays_up
...
task.cancel()
await asyncio.gather(task, return_exceptions=True)
supervisor.close()
```

Await the cancelled task before `close()`: a cancel that lands while the Pi relay is being started could otherwise leave that relay running after `close()`.

- `Config(...)` takes every field explicitly (path fields accept `str` or `Path` and are stored as `Path`); `at_relay` is an `AtRelayConfig(port=..., device=..., idle_seconds=...)` or `None`. `load_config(path)` (in `lte_pro_access.config`) builds one from the JSON file described above.
- `create_supervisor(cfg) -> Supervisor` wires the real dependencies (SSH via asyncssh, SSH enabling via the UniFi controller, local subprocesses, `asyncio.sleep`), the same way the service does. It raises `ModuleNotFoundError` when pyunifiapi is not installed.
- `run_forever(supervisor, interval_seconds, tick_timeout_seconds, sleep)` ticks forever, logging and surviving tick failures and bounding each tick by the timeout. Cancel its task to stop it.
- `await supervisor.tick() -> TickResult` runs one tick yourself instead; `TickResult.actions` lists what it did and `TickResult.relays_up` whether it left every relay up. It is safe to call while `run_forever` runs: ticks are serialized, so an overlapping call waits for the running tick and then runs its own.
- `supervisor.relays_up -> bool` is the health signal: true when the last tick completed with the Pi relay, the device QMI relay and (when configured) the device AT relay all up, and the Pi relay is still running. It is false before the first tick and after a tick that raised or timed out; a tick in progress keeps the previous answer. The device side is only as fresh as the last tick (up to `check_interval_seconds` old; only the Pi relay's process state is read live). When you need a current answer, tick first: `await supervisor.tick()` and then read `supervisor.relays_up`.
- `await supervisor.run_on_device(command) -> str` runs a shell command on the device and returns its stdout, over the same pinned-host-key SSH connection the supervisor uses; if the device refuses the connection it enables SSH through the UniFi controller and retries once, exactly as a tick does. The exit status is not checked, and failures (a host key change, SSH still refused, a timeout) raise. Calls are serialized with ticks, so one can wait for a running tick. Use it for device-side housekeeping the supervisor does not do itself, such as killing stale `socat` processes before forcing a fresh start. Keep the command to what you need: it runs as `ssh_user` on the device, and nothing but `/tmp/socat` may be installed there.
- `supervisor.close()` stops the Pi-side relay. Call it when you stop ticking.
- Logs go to the `lte_pro_access` logger.

## Development

- Tests: `uv run pytest`
- Lint: `uv run ruff check` (format: `uv run ruff format`)

## Disclaimer and Legal Notice

Ubiquiti, UniFi and UniFi OS are trademarks of Ubiquiti Inc. This project is not affiliated with, endorsed by, or sponsored by Ubiquiti Inc.

The socat relays are unauthenticated, unencrypted TCP into the modem, protected only by socat's source-IP `range=` filter. Anything on the LAN that can use the allowed source address (`pi_address`) can send AT and QMI commands to the modem, including SMS, SIM and radio control. Use this software only on a LAN you fully trust; you are responsible for that exposure.

This software enables SSH on the device through the UniFi controller's debug terminal (via pyunifiapi), uploads a socat binary to the device's `/tmp` and runs socat relays on it. These are unofficial uses of the device that Ubiquiti does not support, and firmware updates can change or break them. **You assume all risk of using this software, including the risk of misconfiguring or losing access to the device.**

Use it only on devices you own or are authorized to administer.

## License

MIT © 2026 seidnerj

This software is provided "as is" without warranty of any kind. See [LICENSE](LICENSE) for full terms.
