# Known limitations (1.0.0)

## Not yet validated on hardware

The ISO has not been booted and the installer has not been run end to end (see [QA.md](QA.md)).
Expect first-boot issues to show up in Anaconda's `%pre`/`%post` steps or in SELinux denials
(`ausearch -m avc`) until a real installation test has been done.

## Installer

* The graphical installer is Anaconda with an LSX-VE configuration drop-in and branding in
  `product.img`. It has no LSX-VE hardware/virtualization check page (the text mode has one),
  and the web account is always `admin` with the root password.
* The text-mode dialog runs on tty6 inside kickstart `%pre`; the graphical installer runs the disk
  step there too, before the GUI starts. Both write a fixed GPT + ext4 layout with `sfdisk`: 1 `/boot` 1 GiB,
  2 swap 1 GiB, 3 `/lsxve/iso` 32 GiB, 4 `/var/log/lsxve` 250 MiB, 5 EFI system partition 600 MiB
  (UEFI) or BIOS boot 2 MiB (BIOS), 6 root = rest. Some very old BIOSes refuse to boot from GPT
  disks. Only LSX-VE's own logs go to the 250 MiB log partition; the journal, `/var/log/messages`
  and libvirt's per-VM QEMU logs stay on root. There is no
  LVM, no custom partitioning, no separate datastore partition, and no software RAID, multipath or
  iSCSI/FC boot. 1 GiB of swap is deliberately small: memory overcommit relies on RAM, not swap.
  Serial console installs are not supported.
* In text mode, a single management NIC. Bonds and bridges for management are configured after
  installation.
* In both modes the first web administrator password is the root password.
* There is no "verify media" boot entry, because no MD5 is implanted. Use the `.sha256` file
  instead.
* The Hypervisor repository snapshots Rocky 9.9 updates as of the build date, and it upgrades
  systemd to the matching 9.9 update. Rebuild the ISO to pick up newer updates.

## Hypervisor / VMs

* No live migration, clustering, HA, shared-nothing vMotion equivalent, or vCenter-like management.
* PCI passthrough needs an active IOMMU (`intel_iommu=on` / `amd_iommu=on`) and the VM powered off when the
  device is added. Devices the host needs (management NIC, controller of a used disk, primary GPU, bridges)
  are refused. Whole IOMMU groups are not checked: if a device shares its group with others, the VM
  fails to start with a libvirt error. USB devices are matched by vendor:product ID.
* Resource allocation: CPU shares / limit and disk IOPS / bandwidth limits use cgroups and QEMU throttling.
  There is no CPU or memory *reservation* (except "reserve all memory", which locks the guest RAM) and no
  resource pools.
* CPU hot add works for Linux guests and Windows Server editions that support it; vCPUs cannot be hot
  removed.
* Memory hot add plugs DIMMs (multiples of 128 MiB, up to 16, up to the host RAM) into a single guest NUMA
  node. The guest must support memory hotplug (Linux with memory auto-online, Windows Server Standard /
  Datacenter; desktop Windows ignores the new memory). Memory cannot be hot removed; reductions apply at the
  next power on, when hot-added DIMMs are folded into the boot memory. Enabling or disabling it needs a
  power cycle.
* Serial ports: besides the VM console (COM1), up to three serial ports can write their output to a file on
  a directory or NFS datastore. Physical serial ports, named pipes and network (telnet) serial ports are not
  implemented, and serial ports are added or removed only while the VM is powered off.
* The USB controller is fixed (USB 3.1 xHCI, present in every VM); there is no USB 2.0 / no-controller choice.
* Laptops as hosts: the host never suspends or hibernates (lid switch, sleep keys and idle are ignored by
  systemd-logind, sleep targets are masked). The DCUI switches the display off after 10 idle minutes
  (`console_blank_minutes` in `/etc/lsxve/lsxve.conf`; -1 = 10 on laptops / never elsewhere, 0 = never)
  only while the status screen is shown, not while an F2 menu is open. Backlight control needs a kernel
  backlight driver (`/sys/class/backlight`); with the basic EFI framebuffer only the text is blanked.
  Battery level is not monitored.
* Disks can be extended, not shrunk, and not while the VM has snapshots. The guest's partition and file
  system must be grown inside the guest.
* Import: OVF hardware is mapped to vCPUs, memory, firmware, disks and NIC count; VMware-specific
  settings (VMware Tools, vmxnet3, PVSCSI, extra config) are not carried over. Windows guests imported from
  VMware boot on SATA/e1000e; switch to VirtIO after installing LSX-VE Guest Tools.
* Export to OVA needs the VM powered off and writes to a datastore (download it from the datastore
  browser). Snapshots, NVRAM/TPM state and passthrough devices are not exported.
* VM console uses VNC through noVNC. There is no SPICE, audio or USB redirection.
* Snapshots are internal qcow2 snapshots, so raw and block disks cannot be snapshotted (the UI
  says so). Memory-state snapshots need the VM to support them.
* Templates are VMs marked as templates. "Deploy from template" is a clone.
* Guest details (DNS name, guest OS, exact IPs) need `qemu-guest-agent` running in the guest. Without it,
  VMs on the bridged network show only the addresses the host has seen in its ARP/neighbour table.
  The Summary has no Tags, Custom Attributes or Encryption tiles, because those features do not exist in
  LSX-VE. LSX-VE Guest Tools are the upstream virtio-win drivers and installer (unmodified, signed by
  Red Hat) plus the QEMU guest agent; there is no tools auto-upgrade.
* Software emulation (TCG) works but is slow. The UI warns when KVM is unavailable.

## LXC containers

* Not tested on a real host yet (see QA). The design follows LXC 4.0.12 from EPEL 9.
* Directory backend only: a snapshot or clone is a full copy of the root filesystem, and the
  container must be stopped for snapshots, restores and clones. No ZFS/Btrfs/LVM-thin backends.
* A container's disk size is set at creation (ext4 disk image, LXC "loop" backend; 0 = directory
  without a limit). It cannot be changed later from the web console. The image is thin (sparse),
  so the datastore can be overcommitted; creation only checks that the size is free right now.
  Root password and network files inside an image are written only while the container is stopped.
* One network interface (eth0, veth on a host bridge). A static IPv4 is set by LXC and also
  written into the container's own network configuration before every start (systemd-networkd,
  netplan, ifupdown, network-scripts or a NetworkManager keyfile, whichever the image has), with
  the host's DNS servers. Images that configure the network some other way may still run DHCP.
* Images from images.linuxcontainers.org need Internet access from the host (HTTPS). The
  LXC 4.0.12 download template (EPEL 9) checks only the HTTPS certificate of the image server; it
  does not GPG-verify the image. Offline: upload a rootfs tarball to the ISO library.
* All unprivileged containers share the same uid/gid range (100000–165535), as on Proxmox VE.
  Isolation between containers relies on namespaces and cgroups, not on separate ranges.
* No per-container CPU/memory charts, no bind mounts or device passthrough in the UI, no
  OCI/Docker images (Podman is not included), no live migration.
* Container processes run in the host's `unconfined_service_t` SELinux domain; LXC has no
  SELinux policy in Rocky Linux 9. Use unprivileged containers.

## Storage

* Formatting supports XFS, ext4 and LVM VG on a whole disk. There is no partition editor.
* NFS and iSCSI datastores are libvirt pools. Multipath and CHAP authentication for iSCSI are
  not exposed.
* LVM thin pools are shown, but there is no UI to create them.

## Networking

* VMs share the management bridge `br0` with the host. There is no NAT network; create one under
  Networking → Virtual networks if you need an isolated or NAT segment.
* Automatic bridging handles Ethernet, VLAN and bond management interfaces, not Wi-Fi. A custom MTU on
  the old connection is copied to `br0` and its port.
* Nested (LSX-VE inside another hypervisor): bridged VMs need promiscuous mode / forged
  transmits / MAC spoofing allowed by the outer hypervisor (see INSTALLATION.md).

* Management changes rely on NetworkManager checkpoints. Interfaces not managed by
  NetworkManager are shown but cannot be edited.
* Standard switches only: no distributed switch, no per-port-group security or traffic shaping policies,
  no NIC teaming policy per port group (use a bond as the uplink), no SR-IOV configuration. A VLAN port group
  needs a virtual switch with a physical or bond uplink; VLAN 4095 (guest tagging) is not supported.

## Platform

* x86-64 only.
* `vim` is not installed (use `nano` or `mc`); `dnf install vim-enhanced` adds it back.
* Updates: `dnf update` works for the OS. LSX-VE itself is updated by re-running
  `install-lsxve.sh` from a newer payload. There is no in-UI update manager.
* Remote syslog forwards everything (no per-facility filter); no TLS syslog. SNMP is v2c read-only with a
  community and source networks; there is no SNMPv3 and no trap configuration. Alarms are shown in the UI,
  the event log and syslog; there are no e-mail or script alarm actions.
* Active Directory / LDAP authentication is not implemented. Users are local LSX-VE accounts.
* Lockdown mode has no exception-user list. In strict mode the host can be recovered only from the web
  console (or by booting a rescue system).
* Maintenance mode shuts guests down (no live migration on a standalone host) and blocks power-on of VMs
  and containers; running containers are not stopped automatically.
* Built-in TLS certificate: RSA self-signed. There is no ACME (Let's Encrypt) client, so import
  your own certificate instead.
