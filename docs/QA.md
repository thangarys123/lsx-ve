# QA report — LSX-VE 1.0.0

Build environment: Windows 11 workstation, Python 3.14, Node 22. No hypervisor (QEMU, Hyper-V
or WSL) was available, so **nothing has been verified by booting the ISO or running VMs.** Every
result below says how it was checked.

## Automated checks (run and passed)

| Check | Command | Result |
|---|---|---|
| Unit tests: rpm version compare, rich-dependency parser, FAT12 editor, scrypt compatibility between installer and API, validators, domain XML (UEFI + Secure Boot + vTPM, VNC on 127.0.0.1), repository allowlist (EPEL cannot replace Rocky packages), LXC config rewrite / lxc-info / image index parsing, container spec validation, symlink-safe rootfs access, guest agent channel state, DCUI kernel-message redirect, OVF build/parse round trip (and DTD rejection), CPU shares/limit, memory lock, disk IOPS/bandwidth, video, PCI/USB hostdev and autostart metadata in domain XML, VLAN port-group network XML, vSwitch backing names, bond/MTU bridge arguments, alarm thresholds, memory hot add (maxMemory/NUMA cell/DIMM folding), file serial ports, never-suspend logind/sleep config, fixed ext4 installer disk layout (MBR/GPT, ESP as partition 5, NVMe names, BIOS boot on >2 TiB) | `python -m unittest tests/test_units.py` | 31/31 pass |
| Live API smoke test: public info, security headers, 401, bad password, cookie flags, forced password change, CSRF reject, foreign Origin reject, 422 validation, user creation, roles/events/tasks/audit/metrics, RBAC denials, WebSocket upgrade and hello, logout and session invalidation | `python tests/smoke_api.py http://127.0.0.1:8443 admin '…'` | 26/26 pass |
| Web console typecheck and production build | `npm run build` | clean |
| Web console in a browser (dev mode): login, forced password change, dashboard, VM list, wizard, storage, networking, host management, users/roles, monitor, idle logout | manual, built-in browser | renders. Hypervisor and agent panels correctly show "unavailable" off-host. |
| Batch 2 UI in a browser (dev API + mock VM): Alarms tab and header badge (the "hypervisor unavailable" alarm triggers off-host), alarm definitions, Autostart, Security → Lockdown / Account lockout, Port groups, Edit settings (resources, disk limits, video, PCI/USB, VM autostart) | manual, built-in browser | renders; API endpoints for maintenance, autostart, alarms, account lockout answer correctly in dev mode |
| ISO structure: label, El Torito BIOS + UEFI, isohybrid MBR + GPT + EFI partition, boot menus on the ISO and inside efiboot.img (graphical default + text mode), unchanged shim, both kickstarts balanced and with the same packages, no disk/root password preset in the GUI kickstart, text installer compiles, typed disk confirmation, `.treeinfo` variant, repository checksums for all 128 RPMs, LXC and admin tools present, vim excluded, every `%packages` name available, dependency closure, no Cockpit/Webmin, payload, LF endings, product.img (buildstamp, Anaconda drop-in, branding) | `python tests/verify_iso.py dist/LSX-VE-1.0.0-x86_64.iso` | 83/83 pass (adds: fixed disk layout, GUI disk step in %pre, datastore1 on root, never-suspend logind/sleep config, batch 2 payload modules, rsyslog/net-snmp, lsx-ve-guest-tools.iso with label LSXVE_TOOLS, Joliet names, setup wrapper, virtio-win installer, Linux installer) |
| All Python modules byte-compile | `python -m py_compile src/lsxve/*/*.py` | pass |

Bugs found and fixed by these checks during development:

* Found in the first real install: pycdlib adds no padding when the image is an exact multiple
  of 1 MiB, and then writes the backup GPT over the last 16.5 KiB of ISO data. That corrupted
  `hv/driver.py`, so `lsxve-api` crash-looped. `build.py` now always leaves room for the backup
  GPT, and `verify_iso.py` checks both the padding and that every payload file matches `src/`.
* An IPv4/IPv6 address without a prefix was silently treated as /32 (or /128), which would have
  cut the host off the network. A prefix is now required.
* The repository and payload directories collided on case-insensitive filesystems (`LSXVE/` vs
  `lsxve/`). The repository was renamed to `Hypervisor/`.
* Chart rendering produced NaN with no memory data, the WebSocket refreshed the session idle
  timer, and several agent and route wiring errors were also fixed.

## Requested scenarios: status

| Scenario | Status |
|---|---|
| ISO boots in UEFI mode | **Not run.** Structure verified: EFI El Torito entry plus an unmodified signed shim/GRUB, with grub.cfg patched consistently. |
| ISO boots in legacy BIOS mode | **Not run.** Structure verified: isolinux El Torito entry with boot-info table and isohybrid MBR. |
| Installer detects hardware / virtualization, disk selection, destructive warning | **Not run in Anaconda.** The code path compiles and the confirmation is verified statically. |
| Install completes, first boot initialises KVM/libvirt/storage/network | **Not run.** The packages resolve and the units and install script are written, but not executed. |
| Web UI reachable from another computer | Verified in dev mode on localhost only. On a host it listens on 0.0.0.0:443, and the firewalld service `lsxve` opens 443/80. |
| Create / start / console / snapshot / clone / delete a VM | **Not run against libvirt.** The XML generation is unit-tested and the API paths are exercised up to the driver boundary. |
| Network change with rollback | **Not run.** Implemented with NetworkManager checkpoints. |
| Bridged VM network (management NIC → `br0`, libvirt `default` bridged, VM gets an address from the LAN DHCP server) | **Not run on a host.** The nmcli arguments and the network XML are unit-tested. |
| Port groups with VLAN, OVA import/export, Register VM, guest tools, resource limits, disk extend, CPU hot add, PCI/USB passthrough, maintenance mode, ordered autostart, syslog/SNMP/tuned, lockdown, support bundle | **Not run on a host.** XML generation and parsers are unit-tested; the agent operations (nmcli, qemu-img, tar, rsyslog, snmpd, tuned-adm) need the installed system. Suggested check order on the VMware test host: port group VLAN 0 → import an OVA → export it again → guest tools in a Windows VM → maintenance mode → reboot to test autostart order. |
| LXC: create (image server / tarball), start, console, snapshot, clone, delete, autostart | **Not run on a host.** Container API checked in dev mode: validation (422), RBAC (`ct.*` permissions in the built-in roles) and 503 when the agent is absent. The agent's LXC calls and the console fd passing need Linux. |

## How to run the remaining scenarios

1. Install QEMU (or use any hypervisor with nested virtualization) and create a VM with 4 vCPU,
   8 GiB RAM, a 100 GiB disk and a NIC.
2. Boot `dist/LSX-VE-1.0.0-x86_64.iso` twice: once with OVMF (`-bios OVMF.fd` or UEFI firmware)
   and once with SeaBIOS.
3. Follow [INSTALLATION.md](INSTALLATION.md). Expect the graphical installer (or, in text mode, the dialog on tty6), then the
   Anaconda progress view, then a reboot into the LSX-VE console.
4. From another machine open `https://<ip>/`, log in, upload a small ISO, create a VM, open the
   console, and take, revert and delete a snapshot. Then clone and delete the VM.
5. In **Networking**, change the management IP and do *not* confirm. The old address should
   return after 90 s.
6. Run `virt-host-validate qemu` and **F2 → Diagnostics** on the console.
