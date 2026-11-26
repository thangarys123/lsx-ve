# LSX-VE — Linux Server eXtended – Virtualization Environment

LSX-VE is a bare-metal hypervisor appliance built on Rocky Linux 9 with KVM/QEMU and
libvirt. You install it from its own ISO, it boots to a local text console that shows the
management address, and you run it from a browser at `https://<management-ip>/`. The
workflow is modelled on VMware ESXi. The management stack is written from scratch for this
project. It does not use Cockpit, Webmin or any other generic server panel.

```
┌────────────── Browser (React + TypeScript web console, noVNC) ──────────────┐
│ HTTPS :443  REST /api/v1/*  ·  WebSocket /api/v1/ws (live)  ·  /api/v1/console│
└───────────────────────────────┬──────────────────────────────────────────────┘
                     lsxve-api  │  (user "lsxve", no root, CAP_NET_BIND_SERVICE)
          ┌─────────────────────┼─────────────────────────┐
          │ libvirt (local unix │ socket, polkit)          │ lsxve-agent (root)
          ▼                     ▼                          ▼ fixed op allow-list over
   virtqemud / virtnetworkd / virtstoraged        /run/lsxve/agent.sock (SO_PEERCRED)
          │                                       NetworkManager · firewalld · chrony
        QEMU/KVM guests (VNC on 127.0.0.1 only)   sshd · disks/LVM/XFS · SMART · power
```

## What you get

| Area | Implemented |
|---|---|
| Installer | Its own boot menus (UEFI + legacy BIOS, hybrid USB/DVD image). Default: the graphical installer, rebranded as LSX-VE, with a fixed GPT + ext4 disk layout (1 `/boot` 1 GiB, 2 swap 1 GiB, 3 ISO library 32 GiB, 4 LSX-VE logs 250 MiB, 5 EFI system / BIOS boot, 6 root = rest and last so it can be grown, holding `datastore1`; the disk is chosen and confirmed by typing its name before the GUI starts); the web account is `admin` with the root password. Alternative "text mode": an interactive installer checks the hardware and virtualization support. It asks for the disk and erases it only after you type the device name. It also asks for the management NIC (DHCP/static, VLAN, IPv6), host name, time zone/NTP and the administrator account. Same fixed disk layout. |
| First boot | `lsxve-init` loads KVM, prepares directories and SELinux labels, and generates the TLS certificate. It starts libvirt and writes the console banner. `lsxve-agent` moves the management NIC onto bridge `br0` (IP settings and MAC kept, automatic rollback). The API imports the administrator account and creates the default datastore, the ISO library and the virtual network `default`, **bridged to `br0`**: VMs and containers get their addresses from your physical network. There is no NAT network. |
| Local console (tty1) | A status screen with version, host name, URL, IPv4/IPv6, NIC link state, CPU/RAM, hypervisor and service health. **F2** asks for the root password and then offers: management network (with automatic rollback), network test, host name, restart management agents, SSH, web admin password reset, lockdown mode, View Support Information (serial number, UUID, TLS certificate thumbprint), logs, diagnostics, reboot/shutdown. The status screen shows maintenance and lockdown mode. Kernel messages go to Alt+F12 and the live system log to Alt+F11, so the status screen stays clean. |
| Web console | Host dashboard, live charts, VM inventory and a 6-step VM wizard (OS profile, ISO, CPU/RAM, disks and bus, NICs, BIOS/UEFI, Secure Boot, vTPM, boot order). Also: VM power, edit, clone, template, snapshots, delete, and a noVNC console. Edit settings covers CPU and memory hot add, serial ports (output to a datastore file), resource allocation (CPU shares Low/Normal/High/custom, CPU limit in MHz, reserve all memory, per-disk IOPS and MB/s limits), disk extend, video card model and memory, PCI and USB passthrough, and per-VM autostart options. **Install LSX-VE Guest Tools** mounts an ISO with the VirtIO drivers and the QEMU guest agent; **Export to OVA** writes an OVF 1.0 package with stream-optimized VMDK disks. The VM page follows the vSphere layout: an icon toolbar with Actions, the tabs Summary / Monitor / Configure / Datastores / Networks / Snapshots, and Summary tiles (console preview, VM details with QEMU guest agent data such as DNS name, guest OS and IPs, usage in MHz/GB, VM hardware, related objects, notes, snapshots). |
| LXC containers | System containers with LXC 4.0 (from EPEL 9): create from an image of images.linuxcontainers.org (downloaded and GPG-verified on the host) or from a rootfs tarball in the ISO library. Unprivileged by default, CPU and memory limits, bridge networking (DHCP or static IPv4), root password, autostart, start/shut down/restart/freeze/stop, clone, snapshots, delete and a browser terminal console (xterm.js). |
| Storage | Datastores (directory, NFS, LVM, iSCSI), a datastore browser with upload/download, **Import VM** (OVA, OVF, VMDK, VHD(X), VDI, qcow2, raw - converted to qcow2) and **Register VM** (use an existing qcow2/raw disk in place), an ISO library with streaming upload, physical disks with SMART, and guarded disk formatting (XFS/ext4/LVM) that requires the device name to be typed. Filesystem and LVM views. |
| Networking | Physical adapters and host connections (edit IPv4/IPv6/MTU), plus bridges, VLANs and bonds. Every change runs inside a NetworkManager checkpoint and is rolled back automatically unless you confirm it. **Virtual switches and port groups with VLAN IDs** (802.1Q tagging on the switch uplink); bonds and custom MTUs are carried over to the management bridge. Virtual networks: NAT, routed, isolated, bridge. |
| Admin tools | `htop`, `mc`, `nano`, `net-tools`, `nload` and `epel-release` are installed; `vim` is not. |
| Host | Host name, DNS, NTP/time zone, SSH, root password, firewall, allow-listed services, certificate import/regeneration, users and roles (RBAC), reboot/shutdown. **Maintenance mode**, ordered VM **autostart/autostop** (order, delays, stop action), **remote syslog**, **SNMP v2c** agent, **power policy** (tuned), **lockdown mode** (normal/strict), account lockout policy, **support bundle** download. |
| Monitoring | Performance history (host and per-VM, kept on the server), per-VM utilisation, sensors, PCI devices, **alarms** (host CPU/memory, datastore usage, uplink redundancy, VM CPU, VM crashed, hypervisor, certificate expiry - with thresholds and acknowledge), events, tasks with progress, journal viewer, audit log. |

## Repository layout

```
src/lsxve/          Python backend (stdlib + libvirt-python + dbus)
  api/              HTTPS/WebSocket server, auth/RBAC, REST routes, admin CLI
  agent/            privileged host agent (allow-listed operations)
  hv/               libvirt driver + domain/pool/network XML
  host/             hardware detection, metrics, root operations
  core/             boot-time host initialisation (lsxve-init)
  dcui/             local text console (lsxve-dcui, curses)
  common/           config, logging, validation, safe subprocess, DB
web/                React + TypeScript web console (Vite)
packaging/          systemd units, sysusers/tmpfiles, polkit, firewalld, install script
installer/          kickstarts (graphical ks.cfg.in, text ks-text.cfg.in), text installer (%pre), Anaconda drop-in + CSS
build/              ISO build: build.py, repotool.py (RPM closure), fatimg.py
tests/              smoke_api.py (live API), test_units.py, verify_iso.py
docs/               architecture, building, installation, security, QA, limitations
```

## Quick start

* **Build the ISO:** see [docs/BUILDING.md](docs/BUILDING.md).
  ```
  python -m pip install pycdlib
  cd web && npm ci && cd ..
  python build/build.py
  python tests/verify_iso.py dist/LSX-VE-1.0.0-x86_64.iso
  ```
* **Install a host:** see [docs/INSTALLATION.md](docs/INSTALLATION.md).
* **Develop without a hypervisor:** `LSXVE_DEV=1 LSXVE_DEV_ADMIN_PASSWORD='Dev-Passw0rd!' python -m lsxve.api.app`
  from `src/`, then `npm run dev` in `web/` (see [docs/BUILDING.md](docs/BUILDING.md#development-mode)).

Further reading: [Architecture](docs/ARCHITECTURE.md) · [Security](docs/SECURITY.md) ·
[QA report](docs/QA.md) · [Known limitations](docs/LIMITATIONS.md) · [Changelog](CHANGELOG.md)

## Licensing

The LSX-VE source in this repository is project code. The ISO also carries unmodified
Rocky Linux packages and boot loaders under their own licenses (see `LICENSE`, `EULA` and
`COMMUNITY-CHARTER` on the media). The web console bundles React (MIT) and noVNC (MPL-2.0).
