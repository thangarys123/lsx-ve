# Building LSX-VE

## Requirements

* Python 3.8+ (Linux or Windows), with `pip install pycdlib` (pure Python ISO library).
* Node.js 18+ and npm, to build the web console.
* About 8 GB of free disk space and internet access to `dl.rockylinux.org`, the first time only.
  RPMs are cached in `build/cache/`.
* The source ISO `Rocky-9.9-x86_64-minimal.iso` in the repository root, or pass `--source`.

## Build the ISO

The build also creates `lsx-ve-guest-tools.iso` (the ISO behind "Install LSX-VE Guest Tools"): it downloads
the upstream `virtio-win.iso` (~700 MB, URL in `build/packages.json` → `guest_tools.virtio_win_url`) and the
EL9 `qemu-guest-agent` RPM into `build/cache`, adds the LSX-VE setup wrapper, Linux installer and README and
re-masters it with Joliet + Rock Ridge names. The result is cached; `--skip-guest-tools` leaves it out (the
menu item then reports that the tools are missing).

```
python -m venv build/.venv
build/.venv/bin/python -m pip install pycdlib        # Windows: build\.venv\Scripts\python
cd web && npm ci && cd ..
build/.venv/bin/python build/build.py                 # add --skip-web to reuse web/dist
build/.venv/bin/python tests/verify_iso.py dist/LSX-VE-1.0.0-x86_64.iso
```

Output: `dist/LSX-VE-<version>-x86_64.iso` and a `.sha256` file. Re-running is incremental:
the extraction is cached by source size and mtime, and RPMs are cached by checksum.

Files and settings you will most often change:

| What | Where |
|---|---|
| Version | `src/lsxve/__init__.py` (`__version__`). The volume label is derived from it (`LSX-VE-1-0-x86_64`). |
| Extra packages / exclusions | `build/packages.json`. An `upstream` entry can be a URL or `{"url": …, "only": [regex…]}`; with `only`, just the matching package names are taken from that repository (used for EPEL and Rocky Extras). |
| Installer dialog | `installer/lsxve-setup.py` (runs as kickstart `%pre`) |
| Kickstart | `installer/ks.cfg.in` (graphical), `installer/ks-text.cfg.in` (text mode) |
| Units, policies | `packaging/` |

`build/work/resolve.log` lists every added package and why it was needed. The build fails if
any dependency is unresolved.

### Write to USB

The image is hybrid. On Linux: `dd if=LSX-VE-1.0.0-x86_64.iso of=/dev/sdX bs=4M conv=fsync`.
On Windows, use a raw-image writer such as Rufus in DD mode.

## Manual install on an existing Rocky Linux 9 host

```
dnf install qemu-kvm-core libvirt-daemon-driver-qemu libvirt-daemon-driver-network \
  libvirt-daemon-driver-storage-core libvirt-daemon-driver-storage-logical python3-libvirt \
  python3-dbus edk2-ovmf swtpm-tools smartmontools nfs-utils iscsi-initiator-utils
mkdir -p payload/python && cp -r src/lsxve payload/python/ && cp -r web/dist payload/web \
  && cp -r packaging payload/ && cp packaging/install-lsxve.sh payload/ && echo 1.0.0 > payload/VERSION
sudo bash payload/install-lsxve.sh "$PWD/payload"
sudo systemctl start lsxve-init lsxve-agent lsxve-api lsxve-dcui
sudo cat /etc/lsxve/initial-admin-password     # user "admin", must be changed at first login
```

## Development mode

There is no hypervisor or root in development mode. It works on Windows and macOS.

```
cd src
LSXVE_DEV=1 LSXVE_DEV_ADMIN_PASSWORD='Dev-Passw0rd!' python -m lsxve.api.app   # http://127.0.0.1:8443
cd web && npm run dev                                                           # http://localhost:5173 (proxies /api)
python tests/smoke_api.py http://127.0.0.1:8443 admin 'Dev-Passw0rd!'
python -m unittest tests/test_units.py
```

In development mode, state is kept in `./var` (override with `LSXVE_DEV_ROOT`). Features that
need libvirt or the agent report "unavailable" instead of pretending to work.
