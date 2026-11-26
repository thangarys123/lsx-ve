# LSX-VE interactive installer front-end.
#
# Runs inside Anaconda as the kickstart %pre script (python3, root) on tty6,
# before any disk is touched. It detects hardware, asks for the installation
# disk (with an explicit destructive-operation confirmation), the management
# network, hostname, time zone and the administrator account, then writes
# /tmp/lsxve-ks.cfg (%include'd by the main kickstart) and the hashed
# credentials for the first boot. The only disk write is the fixed LSX-VE
# partition layout on the chosen disk, after the administrator has typed the
# device name to confirm the erase:
# (GPT, ext4):
#     1  boot    1 GiB    /boot
#     2  swap    1 GiB
#     3  isos   32 GiB    /lsxve/iso (ISO library)
#     4  oslog 250 MiB    /var/log/lsxve (LSX-VE logs)
#     5  firmware boot    EFI system partition 600 MiB (/boot/efi) on UEFI,
#                         BIOS boot partition 2 MiB (GRUB) on legacy BIOS
#     6  root    rest     / (holds datastore1) - last, so it can simply be grown
# Anaconda then only formats these partitions (part --onpart).
# The graphical installer (MODE "gui") runs only the disk step here; Anaconda's
# GUI asks for everything else.
import base64
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import secrets
import subprocess
import sys
import termios
import time

VERSION = "@VERSION@"
MEDIA_LABEL = "@LABEL@"
MODE = "@MODE@"          # "text": full dialog; "gui": disk step only (graphical Anaconda asks the rest)
TTY = "/dev/tty6"
GIB = 1024 ** 3
MIB = 1024 ** 2
BOOT_GIB, SWAP_GIB, ISO_GIB, ROOT_MIN_GIB = 1, 1, 32, 16
LOG_MIB, ESP_MIB, BIOSBOOT_MIB = 250, 600, 2
MIN_DISK = (BOOT_GIB + SWAP_GIB + ISO_GIB + ROOT_MIN_GIB + 1) * GIB
FSTYPE = "ext4"

B, DIM, RED, GRN, YEL, CYA, RST = "\033[1m", "\033[2m", "\033[31;1m", "\033[32;1m", "\033[33;1m", "\033[36;1m", "\033[0m"

tty_in = open(TTY, "r")
tty_out = open(TTY, "w")


def chvt(n):
    try:
        fd = os.open("/dev/tty0", os.O_RDWR)
        fcntl.ioctl(fd, 0x5606, n)  # VT_ACTIVATE
        fcntl.ioctl(fd, 0x5607, n)  # VT_WAITACTIVE
        os.close(fd)
    except OSError:
        subprocess.call(["chvt", str(n)])


def out(s=""):
    tty_out.write(s + "\n")
    tty_out.flush()


def clear():
    tty_out.write("\033[2J\033[H")
    tty_out.flush()


def header(step):
    clear()
    out(f"{CYA}{B}  LSX-VE {VERSION} Installer{RST}{DIM}   Linux Server eXtended - Virtualization Environment{RST}")
    out(f"{DIM}  {'-' * 76}{RST}")
    out(f"  {B}{step}{RST}")
    out()


def ask(prompt, default=None, secret=False):
    suffix = f" [{default}]" if default not in (None, "") else ""
    tty_out.write(f"  {prompt}{suffix}: ")
    tty_out.flush()
    fd = tty_in.fileno()
    old = termios.tcgetattr(fd)
    try:
        if secret:
            new = termios.tcgetattr(fd)
            new[3] &= ~termios.ECHO
            termios.tcsetattr(fd, termios.TCSADRAIN, new)
        line = tty_in.readline()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        if secret:
            out()
    line = line.rstrip("\n").strip()
    return line if line else (default if default is not None else "")


def pause(msg="Press Enter to continue"):
    ask(f"{DIM}{msg}{RST}")


def yesno(prompt, default=True):
    while True:
        a = ask(prompt + " (y/n)", "y" if default else "n").lower()
        if a in ("y", "yes"):
            return True
        if a in ("n", "no"):
            return False


def choose(prompt, n, default=1):
    while True:
        a = ask(prompt, str(default))
        if a.isdigit() and 1 <= int(a) <= n:
            return int(a) - 1
        out(f"  {RED}Enter a number between 1 and {n}.{RST}")


def run(argv):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def human(n):
    for u in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if n < 1024 or u == "PiB":
            return f"{n:.1f} {u}" if u != "B" else f"{n} B"
        n /= 1024.0


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


# ------------------------------------------------------------------ hardware
def hardware():
    cpu = read("/proc/cpuinfo")
    model = next((l.split(":", 1)[1].strip() for l in cpu.splitlines() if l.startswith("model name")), "Unknown CPU")
    flags = next((l.split(":", 1)[1].split() for l in cpu.splitlines() if l.startswith("flags")), [])
    threads = sum(1 for l in cpu.splitlines() if l.startswith("processor"))
    mem = next((int(l.split()[1]) * 1024 for l in read("/proc/meminfo").splitlines() if l.startswith("MemTotal")), 0)
    virt = "Intel VT-x" if "vmx" in flags else ("AMD-V" if "svm" in flags else None)
    return {"model": model, "threads": threads, "memory": mem, "virt": virt, "nested": "hypervisor" in flags,
            "uefi": os.path.isdir("/sys/firmware/efi"), "iommu": bool(os.listdir("/sys/kernel/iommu_groups"))
            if os.path.isdir("/sys/kernel/iommu_groups") else False}


def media_devices():
    """Block devices backing the installation media (never offered as targets)."""
    devs = set()
    src = run(["blkid", "-L", MEDIA_LABEL]).strip()
    for path in [src] + [l.split()[0] for l in read("/proc/mounts").splitlines()
                         if " /run/install/" in l and l.startswith("/dev/")]:
        if not path:
            continue
        name = os.path.basename(os.path.realpath(path))
        devs.add(name)
        parent = run(["lsblk", "-no", "PKNAME", "/dev/" + name]).strip()
        if parent:
            devs.add(parent)
    return devs


def disks():
    try:
        data = json.loads(run(["lsblk", "-J", "-b", "-o", "NAME,SIZE,TYPE,MODEL,SERIAL,TRAN,RO,RM,FSTYPE,LABEL,MOUNTPOINT"]))
    except ValueError:
        return []
    skip = media_devices()
    res = []
    for d in data.get("blockdevices", []):
        if d.get("type") != "disk" or d["name"] in skip or str(d.get("ro")) in ("1", "True", "true"):
            continue
        if d["name"].startswith(("zram", "loop", "sr", "fd")):
            continue
        parts = []
        for c in d.get("children") or []:
            parts.append(f"{c['name']} {human(int(c['size']))} {c.get('fstype') or ''} {c.get('label') or ''}".strip())
        res.append({"name": d["name"], "size": int(d["size"]), "model": (d.get("model") or "").strip() or "-",
                    "serial": (d.get("serial") or "").strip(), "tran": d.get("tran") or "", "parts": parts,
                    "rm": str(d.get("rm")) in ("1", "True", "true")})
    return res


def nics():
    res = []
    for n in sorted(os.listdir("/sys/class/net")):
        p = f"/sys/class/net/{n}"
        if n == "lo" or not os.path.exists(p + "/device"):
            continue
        speed = read(p + "/speed", "")
        res.append({"name": n, "mac": read(p + "/address"), "carrier": read(p + "/carrier", "0") == "1",
                    "speed": int(speed) if speed.lstrip("-").isdigit() and int(speed) > 0 else None})
    return res


# ------------------------------------------------------------------ validation
RE_HOST = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
RE_USER = re.compile(r"^[a-z_][a-z0-9_.-]{1,31}$")


def password_problem(pw):
    if len(pw) < 8:
        return "must be at least 8 characters"
    if len(pw) > 256:
        return "is too long"
    classes = sum(bool(re.search(r, pw)) for r in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]"))
    if classes < 3:
        return "must contain 3 of: lowercase, uppercase, digit, symbol"
    if ":" in pw or "\n" in pw:
        return "must not contain ':'"
    return None


def scrypt_hash(pw):
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 15, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)
    return f"scrypt${2 ** 15}$8$1${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def sha512_crypt(pw):
    import crypt
    return crypt.crypt(pw, crypt.mksalt(crypt.METHOD_SHA512))


# ------------------------------------------------------------------ steps
def step_welcome(hw):
    header("Welcome")
    out("  This installer sets up this machine as an LSX-VE virtualization host.")
    out()
    out(f"  CPU        {hw['model']} ({hw['threads']} threads)")
    out(f"  Memory     {human(hw['memory'])}")
    out(f"  Firmware   {'UEFI' if hw['uefi'] else 'Legacy BIOS'}")
    if hw["virt"]:
        out(f"  Hardware virtualization: {GRN}{hw['virt']} supported{RST}")
    else:
        out(f"  Hardware virtualization: {RED}NOT AVAILABLE{RST}")
        out(f"  {YEL}The CPU does not expose VT-x/AMD-V (or it is disabled in firmware). LSX-VE will{RST}")
        out(f"  {YEL}install, but virtual machines will run in slow software emulation.{RST}")
    if hw["nested"]:
        out(f"  {DIM}Running inside a virtual machine (nested virtualization).{RST}")
    out(f"  IOMMU      {'enabled' if hw['iommu'] else 'not enabled (PCI passthrough unavailable)'}")
    if hw["memory"] < 4 * 1024 ** 3:
        out(f"  {YEL}Warning: less than 4 GiB of memory - only very small VMs will fit.{RST}")
    out()
    pause()


def step_disk(steps):
    while True:
        header(f"Step 1 of {steps} - Installation disk")
        ds = disks()
        if not ds:
            out(f"  {RED}No suitable disk was found.{RST} Attach a disk and press Enter to rescan.")
            pause("Press Enter to rescan")
            continue
        out(f"  {'#':>2}  {'Device':<10} {'Size':>11}  {'Bus':<6} Model / serial")
        for i, d in enumerate(ds, 1):
            small = d["size"] < MIN_DISK
            col = DIM if small else ""
            out(f"  {col}{i:>2}  {d['name']:<10} {human(d['size']):>11}  {d['tran'] or '-':<6} {d['model']} "
                f"{d['serial']}{' (too small)' if small else ''}{' (removable)' if d['rm'] else ''}{RST}")
            for p in d["parts"][:4]:
                out(f"  {DIM}      `- {p}{RST}")
            if len(d["parts"]) > 4:
                out(f"  {DIM}      `- ... {len(d['parts']) - 4} more partitions{RST}")
        out()
        out(f"  {DIM}Fixed layout (GPT, {FSTYPE}): 1 boot {BOOT_GIB} GiB, 2 swap {SWAP_GIB} GiB, 3 ISO library {ISO_GIB} GiB,"
            f" 4 logs {LOG_MIB} MiB, 5 {'EFI system' if hardware_uefi() else 'BIOS boot'}, 6 root (rest).{RST}")
        out(f"  {DIM}Other disks are left untouched and can be configured later as datastores.{RST}")
        i = choose("Install LSX-VE on disk number", len(ds))
        d = ds[i]
        if d["size"] < MIN_DISK:
            out(f"  {RED}{d['name']} is smaller than {human(MIN_DISK)}.{RST}")
            pause()
            continue
        header(f"Step 1 of {steps} - Confirm destructive operation")
        out(f"  {RED}{B}WARNING: ALL DATA ON THIS DISK WILL BE PERMANENTLY ERASED{RST}")
        out()
        out(f"    Device  /dev/{d['name']}")
        out(f"    Size    {human(d['size'])}")
        out(f"    Model   {d['model']} {d['serial']}")
        if d["parts"]:
            out("    Current contents:")
            for p in d["parts"]:
                out(f"      {p}")
        else:
            out("    Current contents: no partitions detected")
        out()
        out("  The partition table will be replaced with the LSX-VE layout:")
        for line in layout_lines(d["name"], d["size"], hardware_uefi()):
            out(f"    {line}")
        out()
        out("  Existing operating systems, files and virtual machines on this disk cannot be")
        out("  recovered.")
        out()
        a = ask(f"Type the device name {B}{d['name']}{RST} to confirm, or press Enter to choose again")
        if a == d["name"]:
            return d


def _ipv4_iface(s):
    try:
        i = ipaddress.ip_interface(s)
        if i.version != 4 or i.network.prefixlen in (0, 32) or "/" not in s:
            return None
        return i
    except ValueError:
        return None


def step_network(hostname_default):
    while True:
        header("Step 2 of 5 - Management network")
        ns = nics()
        if not ns:
            out(f"  {YEL}No network adapters detected. The host will be installed without network")
            out(f"  configuration; use the local console (F2) to configure it later.{RST}")
            pause()
            return None
        out(f"  {'#':>2}  {'Adapter':<14} {'MAC':<18} Link")
        for i, n in enumerate(ns, 1):
            link = f"{GRN}connected{RST}" + (f" {n['speed']} Mb/s" if n["speed"] else "") if n["carrier"] else \
                f"{YEL}disconnected{RST}"
            out(f"  {i:>2}  {n['name']:<14} {n['mac']:<18} {link}")
        out()
        default = next((i for i, n in enumerate(ns, 1) if n["carrier"]), 1)
        nic = ns[choose("Management network adapter", len(ns), default)]
        vlan = ask("VLAN ID (0 = untagged)", "0")
        if not vlan.isdigit() or not (0 <= int(vlan) <= 4094):
            out(f"  {RED}Invalid VLAN ID.{RST}")
            pause()
            continue
        net = {"device": nic["name"], "vlan": int(vlan)}
        out()
        out("   1  DHCP (automatic)")
        out("   2  Static IPv4 address")
        if choose("IPv4 configuration", 2, 1) == 0:
            net["method"] = "dhcp"
        else:
            while True:
                ip = _ipv4_iface(ask("IPv4 address/prefix (e.g. 192.168.1.50/24)"))
                if ip:
                    break
                out(f"  {RED}Enter an address with prefix length, e.g. 10.0.0.10/24.{RST}")
            while True:
                gw = ask("Default gateway (blank = none)", "")
                if not gw:
                    break
                try:
                    g = ipaddress.ip_address(gw)
                    if g.version == 4 and g in ip.network and g != ip.ip:
                        break
                    out(f"  {RED}The gateway must be another address inside {ip.network}.{RST}")
                except ValueError:
                    out(f"  {RED}Invalid IPv4 address.{RST}")
            while True:
                dns = [x.strip() for x in ask("DNS servers (comma separated)", gw or "").split(",") if x.strip()]
                try:
                    [ipaddress.ip_address(x) for x in dns]
                    break
                except ValueError:
                    out(f"  {RED}Invalid DNS server address.{RST}")
            net.update({"method": "static", "ip": str(ip.ip), "netmask": str(ip.netmask), "prefix": ip.network.prefixlen,
                        "gateway": gw, "dns": dns})
        net["ipv6"] = yesno("Enable IPv6 automatic configuration (SLAAC/DHCPv6)?", True)
        return net


def step_hostname(default):
    while True:
        header("Step 3 of 5 - Host name and time zone")
        h = ask("Host name (FQDN recommended)", default)
        if RE_HOST.match(h) and not h.replace(".", "").isdigit():
            break
        out(f"  {RED}Invalid host name. Use letters, digits and hyphens, separated by dots.{RST}")
        pause()
    while True:
        tz = ask("Time zone (e.g. UTC, Europe/Berlin, America/New_York)", "UTC")
        if re.match(r"^[A-Za-z0-9_+\-/]{1,64}$", tz) and ".." not in tz and \
                os.path.isfile(os.path.join("/usr/share/zoneinfo", tz)):
            break
        out(f"  {RED}Unknown time zone.{RST}")
    ntp = ask("NTP servers (comma separated, blank = distribution pool)", "")
    ntp_l = []
    for x in (x.strip() for x in ntp.split(",")):
        if x and RE_HOST.match(x):
            ntp_l.append(x)
        elif x:
            out(f"  {YEL}Ignoring invalid NTP server {x}{RST}")
    return h, tz, ntp_l


def step_admin():
    header("Step 4 of 5 - Administrator account")
    out("  This account signs in to the LSX-VE web console with full administrator rights.")
    out("  The same password is set for the local 'root' account (local console and SSH).")
    out()
    while True:
        u = ask("Administrator user name", "admin")
        if RE_USER.match(u) and u != "root":
            break
        out(f"  {RED}Use 2-32 lowercase letters, digits, '.', '_' or '-' (not 'root').{RST}")
    while True:
        p1 = ask("Password", secret=True)
        prob = password_problem(p1)
        if prob:
            out(f"  {RED}Password {prob}.{RST}")
            continue
        if ask("Confirm password", secret=True) != p1:
            out(f"  {RED}Passwords do not match.{RST}")
            continue
        return u, p1


def step_summary(cfg):
    header("Step 5 of 5 - Review")
    d = cfg["disk"]
    out(f"  Install disk     /dev/{d['name']}  {human(d['size'])}  {d['model']}   {RED}(will be erased){RST}")
    for i, line in enumerate(layout_lines(d["name"], d["size"], hardware_uefi())):
        out(f"  {'Partitions' if i == 0 else '':<16} {line}")
    out("  Local datastore  datastore1 (/lsxve/datastores/datastore1 on the root partition)")
    n = cfg["net"]
    if n:
        desc = "DHCP" if n["method"] == "dhcp" else f"{n['ip']}/{n['prefix']} gw {n['gateway'] or '-'} " \
                                                    f"dns {','.join(n['dns']) or '-'}"
        vl = f" VLAN {n['vlan']}" if n["vlan"] else ""
        out(f"  Management NIC   {n['device']}{vl}  {desc}  IPv6 {'auto' if n['ipv6'] else 'off'}")
    else:
        out("  Management NIC   none")
    out(f"  Host name        {cfg['hostname']}")
    out(f"  Time zone        {cfg['tz']}   NTP {','.join(cfg['ntp']) or 'default pool'}")
    out(f"  Administrator    {cfg['user']}  (web console) and root (console/SSH)")
    out()
    out("   1  Begin installation")
    out("   2  Change settings")
    out("   3  Cancel and reboot (nothing has been written)")
    return choose("Select", 3, 1)


def hardware_uefi():
    return os.path.isdir("/sys/firmware/efi")


def part_name(disk, n):
    """sda -> sda1, nvme0n1 -> nvme0n1p1, mmcblk0 -> mmcblk0p1"""
    return f"{disk}{'p' if disk[-1].isdigit() else ''}{n}"


def plan(disk, size, uefi):
    """The fixed LSX-VE layout (always GPT). Returns (label, partitions); each partition is
    (number, size_mib or None for the rest, sfdisk type GUID, kickstart part line)."""
    linux = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
    if uefi:
        fw = (5, ESP_MIB, "C12A7328-F81F-11D2-BA4B-00A0C93EC93B", "part /boot/efi --fstype=efi")
    else:
        fw = (5, BIOSBOOT_MIB, "21686148-6449-6E6F-744E-656564454649", "part biosboot --fstype=biosboot")
    return "gpt", [
        (1, BOOT_GIB * 1024, linux, f"part /boot --fstype={FSTYPE} --label=lsxve-boot"),
        (2, SWAP_GIB * 1024, "0657FD6D-A4AB-43C4-84E5-0933C84B4F4F", "part swap"),
        (3, ISO_GIB * 1024, linux, f"part /lsxve/iso --fstype={FSTYPE} --label=lsxve-iso"),
        (4, LOG_MIB, linux, f"part /var/log/lsxve --fstype={FSTYPE} --label=lsxve-log"),
        fw,
        (6, None, "4F68BCE3-E8CD-4DB1-96E7-FBCAF984B709", f"part / --fstype={FSTYPE} --label=lsxve-root"),
    ]


def layout_lines(disk, size, uefi):
    label, parts = plan(disk, size, uefi)
    names = {1: f"boot (/boot, {FSTYPE})", 2: "swap", 3: f"isos (/lsxve/iso, {FSTYPE})",
             4: f"oslog (/var/log/lsxve, {FSTYPE})",
             5: "EFI system partition (/boot/efi)" if uefi else "BIOS boot (GRUB)", 6: f"root (/, {FSTYPE})"}
    rest = size - sum(p[1] for p in parts if p[1]) * MIB - 2 * MIB   # alignment + backup GPT
    res = ["partition table: GPT"]
    for n, mib, _t, _ks in parts:
        res.append(f"/dev/{part_name(disk, n):<12} {human(mib * MIB if mib else rest):>10}  {names[n]}")
    return res


def sfdisk_script(disk, size, uefi):
    label, parts = plan(disk, size, uefi)
    lines = [f"label: {label}", ""]
    for n, mib, t, _ks in parts:
        f = [f"size={mib}MiB"] if mib else []
        f.append(f"type={t}")
        lines.append(f"/dev/{part_name(disk, n)} : " + ", ".join(f))
    return "\n".join(lines) + "\n"


def partitioning(disk, size, uefi):
    """Kickstart lines that format the partitions written by make_partitions()."""
    _label, parts = plan(disk, size, uefi)
    lines = [f"ignoredisk --only-use={disk}"]
    for n, _mib, _t, ks in parts:
        lines.append(f"{ks} --onpart={part_name(disk, n)}")
    lines.append(f"bootloader --timeout=5 --boot-drive={disk}")
    return lines


def make_partitions(disk, size, uefi):
    """Erase the confirmed disk and write the fixed LSX-VE partition table."""
    dev = f"/dev/{disk}"
    # release old LVM volume groups / RAID arrays on this disk, then wipe all signatures
    try:
        pvs = run(["lvm", "pvs", "--noheadings", "-o", "pv_name,vg_name"])
    except Exception:
        pvs = ""
    for line in pvs.splitlines():
        f = line.split()
        if len(f) == 2 and f[0].startswith(dev):
            subprocess.call(["lvm", "vgchange", "-an", f[1]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.call(["mdadm", "--stop", "--scan"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for name in sorted(os.listdir("/dev")):
        if name.startswith(disk) and name != disk:
            subprocess.call(["wipefs", "-a", "-q", f"/dev/{name}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.check_call(["wipefs", "-a", "-q", dev])
    p = subprocess.run(["sfdisk", "-q", "--wipe", "always", "--wipe-partitions", "always", dev],
                       input=sfdisk_script(disk, size, uefi), universal_newlines=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise RuntimeError(f"sfdisk failed on {dev}: {p.stderr.strip() or p.stdout.strip()}")
    subprocess.call(["udevadm", "settle", "--timeout=30"])
    _label, parts = plan(disk, size, uefi)
    missing = [part_name(disk, n) for n, *_ in parts if not os.path.exists(f"/dev/{part_name(disk, n)}")]
    if missing:
        raise RuntimeError(f"partitions not visible after partitioning: {', '.join(missing)}")


def network_lines(net, hostname):
    if not net:
        return [f"network --hostname={hostname}"]
    a = [f"network --device={net['device']}", "--onboot=yes", "--activate", f"--hostname={hostname}"]
    if net["vlan"]:
        a.append(f"--vlanid={net['vlan']}")
    if net["method"] == "dhcp":
        a.append("--bootproto=dhcp")
    else:
        a += ["--bootproto=static", f"--ip={net['ip']}", f"--netmask={net['netmask']}"]
        if net["gateway"]:
            a.append(f"--gateway={net['gateway']}")
        if net["dns"]:
            a.append(f"--nameserver={','.join(net['dns'])}")
    a.append("--ipv6=auto" if net["ipv6"] else "--noipv6")
    return [" ".join(a)]


def find_media():
    for base in ["/run/install/repo"] + sorted(os.path.join("/run/install/sources", d)
                                                for d in (os.listdir("/run/install/sources")
                                                          if os.path.isdir("/run/install/sources") else [])):
        if os.path.isdir(os.path.join(base, "lsxve")):
            return base
    for line in read("/proc/mounts").splitlines():
        mp = line.split()[1]
        if os.path.isdir(os.path.join(mp, "lsxve", "python")):
            return mp
    return ""


def main_gui():
    """Graphical install: only the disk is chosen here, because LSX-VE uses a fixed partition layout.
    The GUI's Installation Destination applies the generated kickstart partitioning (manual
    partitioning is hidden); Anaconda's GUI asks for the rest."""
    chvt(6)
    d = step_disk(1)
    header("Preparing the installation disk")
    out(f"  Writing the LSX-VE partition table to /dev/{d['name']} ...")
    make_partitions(d["name"], d["size"], hardware_uefi())
    with open("/tmp/lsxve-ks.cfg", "w") as f:
        f.write("\n".join(["# generated by lsxve-setup (graphical mode)"] +
                          partitioning(d["name"], d["size"], hardware_uefi())) + "\n")
    out(f"  {GRN}Done. The graphical installer starts now.{RST}")
    time.sleep(2)
    chvt(1)


def main():
    if MODE == "gui":
        return main_gui()
    chvt(6)
    hw = hardware()
    step_welcome(hw)
    cfg = {}
    while True:
        cfg["disk"] = step_disk(5)
        cfg["net"] = step_network("lsxve")
        cfg["hostname"], cfg["tz"], cfg["ntp"] = step_hostname(cfg.get("hostname", "lsxve.localdomain"))
        cfg["user"], pw = step_admin()
        c = step_summary(cfg)
        if c == 0:
            break
        if c == 2:
            header("Installation cancelled")
            out("  No changes were made. The system will reboot.")
            time.sleep(3)
            subprocess.call(["systemctl", "reboot"])
            time.sleep(60)
    header("Preparing installation")
    out("  Hashing credentials ...")
    admin = {"username": cfg["user"], "password_hash": scrypt_hash(pw), "full_name": "Administrator"}
    with open("/tmp/lsxve-admin.json", "w") as f:
        json.dump(admin, f)
    os.chmod("/tmp/lsxve-admin.json", 0o600)
    ks = ["# generated by lsxve-setup"]
    out(f"  Writing the LSX-VE partition table to /dev/{cfg['disk']['name']} ...")
    make_partitions(cfg["disk"]["name"], cfg["disk"]["size"], hardware_uefi())
    ks += partitioning(cfg["disk"]["name"], cfg["disk"]["size"], hardware_uefi())
    ks += network_lines(cfg["net"], cfg["hostname"])
    ks.append(f"rootpw --iscrypted {sha512_crypt(pw)}")
    tz = f"timezone {cfg['tz']} --utc"
    if cfg["ntp"]:
        tz += " --ntpservers=" + ",".join(cfg["ntp"])
    ks.append(tz)
    with open("/tmp/lsxve-ks.cfg", "w") as f:
        f.write("\n".join(ks) + "\n")
    os.chmod("/tmp/lsxve-ks.cfg", 0o600)
    media = find_media()
    with open("/tmp/lsxve-media", "w") as f:
        f.write(media)
    with open("/tmp/lsxve-hw.json", "w") as f:
        json.dump({"hardware": hw, "settings": {k: v for k, v in cfg.items() if k != "disk"},
                   "disk": cfg["disk"]["name"]}, f)
    out(f"  {GRN}Settings saved. Installing LSX-VE - this takes a few minutes.{RST}")
    if not media:
        out(f"  {YEL}Warning: LSX-VE payload not found on the installation media.{RST}")
    time.sleep(2)
    chvt(1)


try:
    main()
except Exception as e:  # show the error instead of a silent anaconda failure
    out(f"\n  {RED}Installer error: {e}{RST}")
    import traceback
    tty_out.write(traceback.format_exc())
    pause("Press Enter to abort the installation")
    sys.exit(1)
