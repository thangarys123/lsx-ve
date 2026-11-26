# Changelog

All notable changes to LSX-VE. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/).

Versions before 1.0.0 were development builds of the ISO. They were not committed to git
separately, so they have no tags; 1.0.0 is the first tagged release (`v1.0.0`). Every version lists
what was verified. Unless a line says otherwise, a feature has been checked by unit tests, the API
smoke test, the dev-mode web UI and the ISO structure check, **not** on a real installed host.

## [Unreleased]

### Fixed
- The `%pre` disk step now exits with an error when it fails, so `--erroronfail` stops the
  installation with the real message instead of a later `%include /tmp/lsxve-ks.cfg` error.
- `/var/log/lsxve` (its own file system) is labelled `var_log_t` by the install script and by
  `lsxve-init`. It was unlabelled, so SELinux would have kept rsyslog from writing the LSX-VE logs.
- Log rotation for `/var/log/lsxve`: 10 MiB, 6 compressed generations, checked hourly
  (`logrotate.timer` drop-in). With the stock daily run a busy day could fill the 250 MiB partition.
- **Graphical installer failed at "Configuring storage"** with
  `DBusError: 'NoneType' object has no attribute 'type'`. Installation Destination was hidden,
  but in the GUI only that spoke applies the kickstart partitioning. Storage stayed empty, so
  Anaconda found no boot device. The spoke is visible again and applies the fixed layout on its
  own; only manual partitioning (Custom, Blivet-GUI) is hidden.

## [1.0.0] - 2026-11-26

### Changed
- **Fixed installer disk layout.** Both installers write the same GPT partition table with `sfdisk`
  after the administrator types the device name. All file systems are ext4:

  | Partition | Size | Mount |
  |---|---|---|
  | 1 | 1 GiB | `/boot` |
  | 2 | 1 GiB | swap |
  | 3 | 32 GiB | `/lsxve/iso` (ISO library) |
  | 4 | 250 MiB | `/var/log/lsxve` (LSX-VE logs, "oslog") |
  | 5 | 600 MiB / 2 MiB | EFI system partition `/boot/efi` (UEFI) or BIOS boot (legacy BIOS) |
  | 6 | rest | `/` (holds `datastore1`) |

  Root is the last partition so it can be grown in place (`sfdisk -N 6` + `resize2fs`). Anaconda
  only formats the partitions (`part --onpart`), because its own allocator would order them
  differently. NVMe disks get `p1` … `p6` names. The minimum disk size is 51 GiB.
- The graphical installer asks for the disk on a text screen (with typed confirmation) before the
  GUI starts. *Installation Destination* is hidden (reverted in Unreleased, it broke storage setup).
- `datastore1` is a directory on root (`/lsxve/datastores/datastore1`, marker `.lsxve-datastore`),
  no longer an LVM logical volume. LVM is not used for the system any more.

### Added
- rsyslog rule `40-lsxve-local.conf`: the logs of all `lsxve-*` services go to
  `/var/log/lsxve/lsxve.log` and libvirt/QEMU/LXC logs to `/var/log/lsxve/hypervisor.log`.
- logrotate policy for `/var/log/lsxve` (20 MiB, 8 compressed generations), so the 250 MiB log
  partition cannot fill up. `logrotate` was added to the package set and `rsyslog` is enabled.
- `CHANGELOG.md` and a git repository with `.gitignore` (build outputs, caches, ISO images and
  `node_modules` are excluded).

### Verified
- Unit tests 31/31 (layout for BIOS, UEFI and NVMe, rsyslog and logrotate config), `verify_iso`
  84/84. Not installed on a host.

## [0.10.0] - 2026-11-26

### Added
- **Memory hot add** (Edit settings). Memory is added live in 128 MiB DIMMs (up to 16 slots, up to
  the host RAM) on a single guest NUMA node. Hot-added DIMMs are folded into the boot memory when
  the configuration is rewritten. The `mem_hotadd` option is also accepted when creating a VM.
- **Serial ports.** Up to three extra COM ports write their output to a file on a directory or
  NFS datastore. They are added and removed while the VM is powered off. COM1 stays the console.
- The VM hardware tile shows the USB controller (USB 3.1 xHCI), serial ports and memory hot add.
- **Laptop hosts:**
  - The host never suspends or hibernates. systemd-logind ignores the lid switch (on battery,
    external power and docked), the sleep keys and idle time, and the sleep targets are masked.
  - The DCUI blanks the console and switches the backlight off after 10 idle minutes; any key wakes
    it. This only applies when SMBIOS chassis type, lid switch or battery shows a laptop. Set
    `console_blank_minutes` in `lsxve.conf` to change it (0 = never).

### Verified
- Unit tests 28/28, smoke 26/26, `verify_iso` 80/80. Edit settings panels checked in the browser
  against mock data.

## [0.9.0] - 2026-11-25 - Host operations (batch C)

### Added
- **Maintenance mode.** It can shut down the VMs first. While it is on, VMs and containers cannot
  be powered on. There is a banner and chip in the UI, and a status line on the DCUI.
- **Autostart configuration** like ESXi: per-VM order, start/stop delay, stop action, and "continue
  when the guest agent runs". The new `lsxve-vm-autostart.service` runs it (libvirt's unordered
  autostart is switched off).
- **Alarms**, evaluated every 30 s with thresholds and acknowledge, plus a badge in the top bar:
  - host CPU and memory, datastore usage, inactive datastore, uplink redundancy,
  - VM CPU, VM crashed,
  - hypervisor, certificate expiry, maintenance mode.
- **Remote syslog** (rsyslog forwarding), **SNMP v2c** read-only agent (community and source
  networks, firewalld service), **power policy** through tuned profiles.
- **Lockdown mode** (normal / strict) and the account lockout policy. In strict mode the DCUI only
  offers support information and logs, F12 is blocked and SSH cannot be enabled.
- **Support bundle** (like `vm-support`) created by the agent without secrets and downloadable in
  the UI. The DCUI gets *View Support Information*: serial number, UUID and TLS certificate
  thumbprint.
- **VM performance history** is kept on the server, so the Monitor charts are filled as soon as
  the page opens.

### Fixed
- A custom MTU of the management connection is copied to `br0` and its port.
- Bonds are bridged automatically like Ethernet and VLAN connections.

### Not implemented
- Active Directory / LDAP authentication. The UI and `LIMITATIONS.md` say so.

## [0.8.0] - 2026-11-25 - Guest tools and VM settings (batch B)

### Added
- **LSX-VE Guest Tools** (`lsx-ve-guest-tools.iso`, volume label `LSXVE_TOOLS`). The ISO holds the
  upstream virtio-win drivers (unmodified, signed by Red Hat), the EL9 `qemu-guest-agent` RPM,
  `LSX-VE-Guest-Tools-Setup.cmd` for Windows, `linux/install.sh` and a README. Built and cached by
  `build/guesttools.py` (Joliet + Rock Ridge, deduplicated with hard links, about 837 MiB);
  `--skip-guest-tools` leaves it out. In the UI: *Actions → Install / Unmount LSX-VE Guest Tools*.
- **Resource allocation:**
  - CPU shares (Low / Normal / High / custom) and a CPU limit in MHz,
  - "reserve all guest memory" (locked pages),
  - per-disk IOPS and MB/s limits.
  Changes are applied live where libvirt allows it.
- **Disk extend.** Disks only grow, and not while the VM has snapshots.
- **CPU hot add** (one core per socket, vCPUs added live), **video card** model and video memory.
- **PCI and USB passthrough.** The UI picks the devices. The IOMMU status is shown. Devices the host
  needs are refused: the management NIC, the controller of a used disk, the primary GPU and bridges.

## [0.7.0] - 2026-11-25 - Networking and migration (batch A)

### Added
- **Virtual switches and port groups with VLAN IDs.** `br0` is vSwitch0. VLAN *N* uses a backing
  bridge with the 802.1Q sub-interface of the uplink. Changes run inside a NetworkManager checkpoint
  with automatic rollback.
- **Import VM** from OVA, OVF, VMDK, VHD(X), VDI, qcow2 or raw. Disks are converted to qcow2
  (`qemu-img`, no backing files) and the source is left unchanged. OVF hardware is mapped to vCPUs,
  memory, firmware, disks and NICs. Descriptors with DTDs or entities are rejected.
- **Export to OVA**: an OVF 1.0 package with stream-optimized VMDK disks, written to a datastore.
- **Register VM**: create a VM that uses an existing qcow2/raw disk in place.
- Datastore browser **upload and download**.

### Verified
- For batches A-C together: unit tests 24/24, smoke 26/26, `verify_iso` 79/79, and a browser check
  of all new pages against the dev API and mock data.

## [0.6.0] - 2026-11-24 - Console and vSphere-style VM page

### Added
- The DCUI redirects kernel messages to **tty12 (Alt+F12)**, like the ESXi vmkernel log. The new
  `lsxve-syslog-console.service` shows the live system log on **tty11 (Alt+F11)**. tty1 is fully
  repainted every 5 s, so nothing else stays on the status screen.
- VM page modelled on vSphere:
  - a header with power, console, snapshot and edit icons, and an **Actions** menu,
  - tabs Summary / Monitor / Configure / Datastores / Networks / Snapshots,
  - Summary tiles: Guest OS (screen preview), VM details, Usage, VM Hardware, Related objects,
    Notes, Snapshots and General.
- Guest information from the QEMU guest agent: hostname, OS, kernel and time zone. The page
  refreshes when the agent connects.
- Guest IP addresses are found from the host ARP table on bridged networks even without the agent.
- Notes are applied to a running VM immediately.

### Verified
- Unit tests 18/18, smoke 26/26, `verify_iso` 66/66.

## [0.5.0] - 2026-11-24 - Bridged VM network

### Changed
- There is no NAT network any more. On first boot the NIC with the default route moves under the
  new bridge **`br0`**, which keeps its IP settings, DNS, firewall zone and MAC address. This runs
  in a NetworkManager checkpoint with automatic rollback if `br0` gets no address or the gateway
  stops answering. Retry is in *F2 → Bridged VM Network* and on the Networking page.
- The libvirt network `default` is bridged to `br0`, so VMs and containers get their addresses
  from the physical network. An old NAT `default` network is replaced once no running VM uses it.
- The management bridge can be disabled with `"management_bridge": ""` in `lsxve.conf`.

### Verified
- Unit tests 16/16 (nmcli arguments, network XML), `verify_iso` 65/65.

## [0.4.0] - 2026-11-24 - LXC containers and admin tools

### Added
- **LXC system containers** (LXC 4.0.12 and lxcfs from EPEL 9):
  - **Create** from images.linuxcontainers.org (downloaded and GPG-verified on the host) or from a
    rootfs tarball in the ISO library.
  - **Unprivileged** by default.
  - **Settings:** CPU and memory limits, bridge networking with DHCP or static IPv4, root password,
    autostart.
  - **Operations:** start / shut down / restart / freeze / kill, clone, snapshots, delete.
  - **Console:** a browser terminal (xterm.js).
  - Each running container is a `lsxve-ct@NAME.service`.
  - New permissions `ct.view`, `ct.power`, `ct.console`, `ct.snapshot` and `ct.manage`.
- Packages `epel-release`, `htop`, `mc`, `nano`, `net-tools` and `nload`. Only allow-listed packages
  may come from EPEL / Extras, and a test checks that no Rocky package is replaced.

### Removed
- `vim` (excluded in both kickstarts).

### Verified
- Unit tests 14/14, smoke 26/26, `verify_iso` 64/64. The hypervisor repository has 120 RPMs.

## [0.3.0] - 2026-11-24 - Graphical installer

### Added
- The default boot entry starts the **graphical Anaconda installer** with LSX-VE branding
  (`product.img`: buildstamp, configuration drop-in, CSS, sidebar and top bar images). The software,
  source and user pages are hidden.
- The web account `admin` uses the root password chosen in the GUI. It is converted to the web
  console's own hash at the first login.
- The original console installer remains as **Install LSX-VE (text mode)**.

### Verified
- `verify_iso` 58/58.

## [0.2.0] - 2026-11-24 - Guest OS catalog and first-boot fixes

### Added
- **Guest OS catalog** (35 entries: Windows 11/10/Server, the major Linux distributions, BSDs,
  other) in the VM wizard and in Edit settings. It sets the OS family and the recommended vCPU,
  RAM, disk size and UEFI / Secure Boot / TPM.

### Fixed
- **ISO corruption.** When the ISO size was an exact multiple of 1 MiB, pycdlib added no padding
  and the backup GPT overwrote the end of `hv/driver.py`. As a result `lsxve-api` crashed with
  "null bytes". The build now always reserves space for the backup GPT. `verify_iso` checks the
  GPT position and that every payload file is byte-identical to `src/` and compiles.
- The API sandbox allows netlink sockets, which are needed to read the host addresses.
- **Time settings:**
  - `chronyc` is queried only through its Unix socket.
  - The time zone falls back to `/etc/localtime`.
  - An unknown time zone shows "(unknown)" instead of `Africa/Abidjan`, and saving is disabled.
- `lsxve-agent` no longer runs with `NoNewPrivileges`, which stopped `chronyc` and other tools from
  entering their SELinux domains.
- "Host agent unavailable" is only shown when the agent really is down. A single failed read is a
  warning.
- Charts follow the panel width (ResizeObserver) instead of a fixed 600 px.
- The VM wizard preselects the first available datastore instead of `default`.

### Verified
- On a VMware test host: the API starts after the fix, the web UI loads, firewall and SELinux are
  fine. `verify_iso` 48/48.

## [0.1.0] - 2026-11-24 - Initial platform

### Added
- **ISO builder** (`build/`):
  - `build.py` turns the Rocky Linux 9 minimal ISO into the LSX-VE ISO, bootable through UEFI
    (Secure Boot chain unchanged) and legacy BIOS, as a hybrid USB/DVD image.
  - `repotool.py` resolves and downloads the hypervisor packages into the `Hypervisor` repository,
    with a dependency closure check and no Cockpit or Webmin.
  - `fatimg.py` edits the EFI boot image.
- **Text-mode installer** on tty6:
  - a hardware and virtualization check,
  - disk selection that excludes the install media and needs a typed confirmation,
  - management network (DHCP or static, VLAN, IPv6), host name, time zone / NTP, administrator
    account,
  - a review screen.
- **First boot** (`lsxve-init`): loads KVM, prepares directories and SELinux labels, creates the TLS
  certificate, starts libvirt and prints the management URL.
- **DCUI** on tty1:
  - a status screen with URL, addresses, links, CPU/RAM and service health,
  - *F2* (root password): management network with automatic rollback, network test, host name,
    restart agents, SSH, web admin password reset, logs, diagnostics, reboot / shutdown.
- **`lsxve-agent`**, the only privileged process. It serves a fixed table of validated operations
  over a Unix socket and runs no generic commands.
- **`lsxve-api`** and the **React web console**:
  - host dashboard, VM inventory and a 6-step wizard (BIOS/UEFI, Secure Boot, vTPM),
  - power operations, edit, clone, templates, snapshots and a noVNC console,
  - datastores (directory, NFS, LVM, iSCSI), ISO library, disks with SMART and guarded
    formatting,
  - networking with checkpoint rollback, host settings, firewall, certificates,
  - users and roles (RBAC), events, tasks, audit log.
  - The API runs as an unprivileged user with TLS, CSRF and Origin checks.
- `lsxve-admin` CLI (list users, reset or unlock accounts) and systemd units, polkit rule, firewalld
  service, sysusers and tmpfiles in `packaging/`.
- Documentation: README, ARCHITECTURE, BUILDING, INSTALLATION, SECURITY, QA, LIMITATIONS.

### Fixed
- An IPv4 address without a prefix was treated as `/32`, which could cut the host off the network.
  A prefix is now required.
- On case-insensitive file systems the repository folder `LSXVE/` collided with the payload `lsxve/`.
  It is now `Hypervisor/`.

### Verified
- `verify_iso` 44/44, unit tests 8/8, smoke 26/26.

