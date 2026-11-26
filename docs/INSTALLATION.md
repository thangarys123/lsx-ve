# Installing and operating an LSX-VE host

## Hardware

* An x86-64 CPU with Intel VT-x or AMD-V enabled in firmware. Without it the host still installs,
  but VMs run in software emulation, which the installer, console and web UI all flag.
* At least 4 GiB of RAM, with 16 GiB or more recommended.
* One disk of at least 50 GiB for the system (fixed layout, see below).
* UEFI (with or without Secure Boot) or legacy BIOS firmware.

## Disk layout (fixed)

Both installers write the same partition table to the chosen disk. Other disks are not touched.

| Partition | Size | Use |
|---|---|---|
| 1 (`sda1`) | 1 GiB | **boot**: `/boot` |
| 2 (`sda2`) | 1 GiB | **swap** |
| 3 (`sda3`) | 32 GiB | **isos**: `/lsxve/iso`, the ISO library |
| 4 (`sda4`) | 250 MiB | **oslog**: `/var/log/lsxve`, the LSX-VE logs: `lsxve.log` (all `lsxve-*` services) and `hypervisor.log` (libvirt, QEMU, LXC), copied from the journal by rsyslog and rotated at 10 MiB (6 compressed generations, checked hourly); **the whole partition is emptied every 7 days** (`lsxve-log-purge.timer`, weekly, caught up at boot) |
| 5 (`sda5`) | 600 MiB / 2 MiB | **firmware boot**: EFI system partition `/boot/efi` (FAT) on UEFI, or the 2 MiB "BIOS boot" partition for GRUB on legacy BIOS |
| 6 (`sda6`) | rest | **root**: `/`. The local datastore `datastore1` is the directory `/lsxve/datastores/datastore1` on it. |

All file systems are ext4 and the partition table is always GPT (BIOS and UEFI). Root is the
last partition, so after enlarging the disk (for example a virtual disk) it grows in place:

```
echo ", +" | sfdisk --no-reread -N 6 /dev/sda   # move the end of partition 6 to the end of the disk
partx -u /dev/sda
resize2fs /dev/sda6                               # ext4 grows online
```

NVMe disks are named `nvme0n1p1` … `nvme0n1p6`. The disk must be at least 51 GiB (1 + 1 + 32 GiB,
the small partitions and at least 16 GiB for root). There is no LVM and no custom partitioning.

## Graphical installer (default)

1. Boot the ISO and choose **Install LSX-VE** (the default entry). "Basic graphics mode" adds
   `nomodeset` for displays the installer cannot drive.
2. Before the graphical installer starts, a text screen asks for the **installation disk**. It lists
   the disks with size, model and current partitions and shows the layout that will be written.
   You confirm by typing the device name (for example `sda`). Then the disk is erased and
   partitioned (see *Disk layout*). Press Enter instead to choose again; nothing is written
   before the device name is typed.
3. The graphical installer opens with LSX-VE branding. The software, installation source and
   user pages are fixed by LSX-VE and hidden. **Installation Destination** is shown already
   configured ("Custom partitioning selected") with the layout from step 2; leave it as it is.
   Manual partitioning is not offered there. You set:
   * **Network & Host Name:** enable the management NIC, DHCP or static address, VLAN, host name.
   * **Time & Date:** time zone and NTP servers.
   * **Root Password.** The web console account is **`admin` with this root password**. It is
     converted to the web console's own hash at the first login; after that the two passwords are
     independent.
4. **Begin Installation.** The system reboots at the end; remove the media if your firmware does
   not eject it.

The graphical installer does not run the LSX-VE hardware check. Look at the console (tty1)
after the first boot, or use the text mode, to see whether hardware virtualization is available.

## Text-mode installer

Choose **Install LSX-VE (text mode)** for a text dialog, a low-memory machine or a
remote KVM that cannot show graphics.

1. The LSX-VE installer starts on its own screen (tty6) and shows the CPU, memory, firmware,
   virtualization and IOMMU status.
3. **Disk:** pick the target disk by number. The installation media is never listed. You must
   type the device name (for example `sda`) to confirm that the disk will be erased. All other
   disks are left untouched.
4. **Network:** choose the management NIC (link state is shown), an optional VLAN, DHCP or a static
   address/prefix/gateway/DNS, and IPv6 auto on or off.
5. **Host name, time zone, NTP.**
6. **Administrator:** the user name (default `admin`) and password are used for the web console.
   The same password is set for `root` on the local console and SSH.
7. **Review**, then **Begin installation** (or change settings, or cancel and reboot). Nothing is
   written before this step.

The system reboots when installation finishes. Remove the media if your firmware does not eject it.

## First boot

`lsxve-init` prepares the host. `lsxve-api` then imports the administrator account (from the
text-mode dialog, or `admin` with the root password from the graphical installer) and creates:

* the libvirt storage pools `datastore1` (or `default` in `/var/lib/libvirt/images`) and `iso`
  (`/lsxve/iso`),
* the virtual network `default`, bridged to `br0` (see below).

### Bridged VM network

On first boot (or the first start of `lsxve-agent` after an update) the agent moves the
management interface onto a Linux bridge `br0`, the way a vSwitch carries the management port
on ESXi:

* `br0` gets the NIC's IPv4/IPv6 settings, DNS, firewall zone and **MAC address**, so a DHCP
  server gives the host the same address. The NIC becomes a port of `br0`. The old connection is
  kept with autoconnect off.
* The switch-over runs inside a NetworkManager checkpoint. If `br0` gets no address, or the
  gateway stops answering, everything is rolled back automatically and the console shows why.
  Retry with **F2 → Bridged VM Network** or **Networking → Retry** in the web console.
* The libvirt network `default` is `<forward mode="bridge"/>` on `br0`. VMs and containers on it
  are ordinary machines on your LAN and get their addresses from its DHCP server. A NAT `default`
  left over from an earlier version is replaced as soon as no running VM uses it.
* Ethernet and VLAN management interfaces are converted. A bond, or an interface not managed by
  NetworkManager, is left alone: create a bridge in **Networking** yourself.
* Disable it with `"management_bridge": ""` in `/etc/lsxve/lsxve.conf`.

**When LSX-VE itself runs as a VM** (VMware, Hyper-V, VirtualBox, Proxmox), the outer hypervisor
must let the LSX-VE VM send frames from other MAC addresses, otherwise nested VMs get no DHCP
answer:

* VMware ESXi: on the port group or vSwitch set *Promiscuous mode*, *MAC address changes* and
  *Forged transmits* to **Accept**.
* VMware Workstation / Player: use *Bridged* networking on a **wired** host adapter. Wi-Fi
  adapters cannot carry several MAC addresses.
* Hyper-V: enable *MAC address spoofing* on the network adapter.
* VirtualBox: *Bridged Adapter*, *Promiscuous Mode: Allow All*.

The console (tty1) shows the URL, for example `https://192.168.1.50/`. The certificate is
self-signed, so the browser will warn on first use. Until the first web login the console also
says which account to use. You can import your own certificate in
**Host → Manage → Certificate**.

## Containers (LXC)

Open **Containers** in the navigator and click **Create container**:

* **Image server:** pick a distribution, release and variant from images.linuxcontainers.org.
  The host needs Internet access. **Rootfs archive:** upload a `.tar.*` root filesystem to the ISO
  library (Storage → ISO library → Upload) and pick it.
* Set CPU and memory limits (0 = no limit), the bridge (`br0` is the physical network, so the container
  gets an address from your DHCP server; other host bridges from Networking also appear), DHCP or a static address, and optionally a root
  password, so you can log in on the console.
* **Unprivileged** is the default and is recommended.

Then **Start** and **Console** (a terminal in a new window; leave with Ctrl+a q). Snapshots, restore
and clone need the container stopped. On the host the containers are ordinary LXC containers:
`lxc-ls -f`, `systemctl status lsxve-ct@NAME`.

## Local console (DCUI)

| Key | Action |
|---|---|
| F2 | Customize System (root password required): management network, test network, host name, restart management agents, SSH on/off, reset web administrator password, logs, diagnostics, shut down/restart |
| F12 | Shut down / restart (root password required) |
| Alt+F2 | Normal login shell |
| Alt+F11 | Live system log (`journalctl -f`, read-only) |
| Alt+F12 | Kernel messages. They are redirected here so they never overwrite the status screen, like the ESXi vmkernel log screen |
| Alt+F1 | Back to the Direct Console |

On a laptop the host keeps running with the lid closed (no suspend or hibernation, sleep keys ignored), and
the console display and backlight switch off after 10 minutes without a key press; any key turns them back
on. Set `"console_blank_minutes"` in `/etc/lsxve/lsxve.conf` (0 = never) and restart `lsxve-dcui` to change it.

When you change the management network from the DCUI, it reverts after 60 s unless you press
**F11** to keep it.

## Recovering access

* Forgotten web password: on the console press F2 and choose **Reset Web Administrator Password**, or run
  `lsxve-admin reset-password admin` as root. The new password must be changed at the next login.
* List accounts: `lsxve-admin list-users`. Re-enable an account: `lsxve-admin unlock <user>`.
* Logs: `journalctl -u lsxve-api -u lsxve-agent -u lsxve-init`. The installer log is in `/root/lsxve-install.log`.
