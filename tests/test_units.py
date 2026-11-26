"""Unit tests for pure-Python parts (run anywhere): python -m unittest tests/test_units.py"""
import importlib.util
import os
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "build"))
sys.path.insert(0, str(ROOT / "src"))

import fatimg  # noqa: E402
import repotool  # noqa: E402
from lsxve.common import security, validate as v  # noqa: E402
from lsxve.ct import lxc  # noqa: E402
from lsxve.hv import domxml  # noqa: E402


class RpmVersion(unittest.TestCase):
    def test_vercmp(self):
        c = repotool._rpmvercmp
        self.assertEqual(c("1.0", "1.0"), 0)
        self.assertEqual(c("1.10", "1.9"), 1)
        self.assertEqual(c("1.0~rc1", "1.0"), -1)
        self.assertEqual(c("1.0^git1", "1.0"), 1)
        self.assertEqual(c("2.el9", "2.el9_9.1"), -1)
        self.assertEqual(c("a", "1"), -1)

    def test_evr(self):
        self.assertEqual(repotool.evr_cmp(("1", "1", "1"), ("0", "9", "9")), 1)
        self.assertEqual(repotool.evr_cmp(("0", "1.2", None), ("0", "1.2", "5")), 0)

    def test_rich(self):
        t, _ = repotool.parse_rich(repotool.tokenize("(a >= 1.2 if b)"))
        self.assertEqual(t[0], "if")
        self.assertEqual(t[1][0][1].name, "a")
        self.assertEqual(t[1][0][1].flags, "GE")


def make_fat12(size=1440 * 1024):
    bps, spc, res, nfats, roots, fatsz = 512, 1, 1, 2, 224, 9
    d = bytearray(size)
    struct.pack_into("<3s8sHBHBHHBH", d, 0, b"\xeb\x3c\x90", b"MSDOS5.0", bps, spc, res, nfats, roots,
                     size // bps, 0xF0, fatsz)
    for f in range(nfats):
        base = (res + f * fatsz) * bps
        d[base:base + 3] = b"\xf0\xff\xff"
    root = (res + nfats * fatsz) * bps
    data = root + roots * 32
    # directory EFI at cluster 2, file GRUB.CFG at cluster 3
    d[root:root + 11] = b"EFI        "
    d[root + 11] = 0x10
    struct.pack_into("<H", d, root + 26, 2)
    e = data
    d[e:e + 11] = b"GRUB    CFG"
    struct.pack_into("<HI", d, e + 26, 3, 5)
    d[data + 512:data + 517] = b"hello"
    img = fatimg.FatImage(bytes(d))
    img._fat_set(2, 0xFFF)
    img._fat_set(3, 0xFFF)
    return img


def _primary(*pkgs):
    """Minimal primary.xml: pkgs = (name, version, [requires])."""
    body = ""
    for name, ver, req in pkgs:
        reqs = "".join(f'<rpm:entry name="{r}"/>' for r in req)
        body += (f'<package type="rpm"><name>{name}</name><arch>x86_64</arch><version epoch="0" ver="{ver}" rel="1.el9"/>'
                 f'<checksum type="sha256">00</checksum><location href="Packages/{name}.rpm"/><size package="1"/>'
                 f'<format><rpm:sourcerpm>{name}.src.rpm</rpm:sourcerpm>'
                 f'<rpm:provides><rpm:entry name="{name}"/></rpm:provides><rpm:requires>{reqs}</rpm:requires>'
                 f'</format></package>')
    return (f'<metadata xmlns="http://linux.duke.edu/metadata/common" xmlns:rpm="http://linux.duke.edu/metadata/rpm">'
            f'{body}</metadata>').encode()


class RepoAllowlist(unittest.TestCase):
    def test_only_allowlisted_packages_come_from_third_party_repo(self):
        remote = {
            "base": repotool.parse_primary(_primary(("openssl", "3.0", []), ("wget", "1.21", [])), "base"),
            # EPEL also carries a newer openssl and wget: they must never be picked
            "epel": repotool.parse_primary(_primary(("lxc", "4.0", ["wget", "openssl"]), ("openssl", "9.9", []),
                                                    ("wget", "9.9", [])), "epel"),
        }
        orig = repotool.load_remote_repo
        repotool.load_remote_repo = lambda url, name, cache: remote[name]
        try:
            res, pkgs = repotool.resolve(_primary(), {"base": "http://b/", "epel": {"url": "http://e/", "only": ["lxc"]}},
                                         ["lxc"], [], Path("."))
        finally:
            repotool.load_remote_repo = orig
        got = {p.name: (p.repo, p.evr[1]) for p in pkgs}
        self.assertEqual(got, {"lxc": ("epel", "4.0"), "wget": ("base", "1.21"), "openssl": ("base", "3.0")})


class Fat(unittest.TestCase):
    def test_read_write(self):
        img = make_fat12()
        self.assertEqual(img.read("/EFI/grub.cfg"), b"hello")
        big = b"x" * 5000
        img.write("/efi/GRUB.CFG", big)
        img2 = fatimg.FatImage(img.bytes())
        self.assertEqual(img2.read("/EFI/GRUB.CFG"), big)
        img2.write("/EFI/GRUB.CFG", b"small")
        self.assertEqual(fatimg.FatImage(img2.bytes()).read("/EFI/GRUB.CFG"), b"small")


class Security(unittest.TestCase):
    def test_password_hash(self):
        h = security.hash_password("Corr3ct-horse")
        self.assertTrue(security.verify_password("Corr3ct-horse", h))
        self.assertFalse(security.verify_password("wrong", h))

    def test_installer_hash_compatible(self):
        spec = importlib.util.spec_from_file_location("x", ROOT / "installer" / "lsxve-setup.py")
        src = (ROOT / "installer" / "lsxve-setup.py").read_text()
        ns = {}
        start = src.index("def scrypt_hash")
        exec("import base64, hashlib, secrets\n" + src[start:src.index("def sha512_crypt")], ns)
        h = ns["scrypt_hash"]("Install3r-pass")
        self.assertTrue(security.verify_password("Install3r-pass", h))


class DiskLayout(unittest.TestCase):
    """Fixed installer layout: 1 boot 1G, 2 swap 1G, 3 isos 32G, 4 root rest (ext4); 5 ESP / BIOS boot."""

    @classmethod
    def setUpClass(cls):
        src = (ROOT / "installer" / "lsxve-setup.py").read_text(encoding="utf-8")
        ns = {}
        consts = src[src.index("GIB = 1024 ** 3"):src.index("B, DIM, RED")]
        code = (src[src.index("def human"):src.index("def read(")] +
                src[src.index("def hardware_uefi"):src.index("def make_partitions")])
        exec("import os\n" + consts + code, ns)
        cls.ns = ns

    def test_bios_layout(self):
        ns = self.ns
        label, parts = ns["plan"]("sda", 100 * 1024 ** 3, False)
        self.assertEqual(label, "gpt")
        self.assertEqual([(n, mib) for n, mib, *_ in parts],
                         [(1, 1024), (2, 1024), (3, 32768), (4, 250), (5, 2), (6, None)])
        ks = ns["partitioning"]("sda", 100 * 1024 ** 3, False)
        self.assertEqual(ks[1:7], ["part /boot --fstype=ext4 --label=lsxve-boot --onpart=sda1",
                                   "part swap --onpart=sda2",
                                   "part /lsxve/iso --fstype=ext4 --label=lsxve-iso --onpart=sda3",
                                   "part /var/log/lsxve --fstype=ext4 --label=lsxve-log --onpart=sda4",
                                   "part biosboot --fstype=biosboot --onpart=sda5",
                                   "part / --fstype=ext4 --label=lsxve-root --onpart=sda6"])
        self.assertIn("ignoredisk --only-use=sda", ks)
        self.assertIn("bootloader --timeout=5 --boot-drive=sda", ks)
        sf = ns["sfdisk_script"]("sda", 100 * 1024 ** 3, False)
        self.assertIn("label: gpt", sf)
        self.assertIn("/dev/sda4 : size=250MiB, type=0FC63DAF-8483-4772-8E79-3D69D8477DE4", sf)
        self.assertIn("/dev/sda5 : size=2MiB, type=21686148-6449-6E6F-744E-656564454649", sf)
        # root is the last partition and takes the rest, so it can be grown in place
        self.assertTrue(sf.rstrip().splitlines()[-1].startswith("/dev/sda6 : type=4F68BCE3"))
        self.assertNotIn("xfs", " ".join(ks))

    def test_uefi_layout_nvme(self):
        ns = self.ns
        size = 256 * 1024 ** 3
        ks = ns["partitioning"]("nvme0n1", size, True)
        self.assertEqual(ks[5], "part /boot/efi --fstype=efi --onpart=nvme0n1p5")
        self.assertEqual(ks[6], "part / --fstype=ext4 --label=lsxve-root --onpart=nvme0n1p6")
        sf = ns["sfdisk_script"]("nvme0n1", size, True)
        self.assertIn("/dev/nvme0n1p5 : size=600MiB, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B", sf)
        self.assertTrue(sf.rstrip().splitlines()[-1].startswith("/dev/nvme0n1p6 : type="))
        lines = ns["layout_lines"]("nvme0n1", size, True)
        self.assertIn("oslog", lines[4])
        self.assertIn("root", lines[6])

    def test_log_partition_config(self):
        pk = ROOT / "packaging"
        rs = (pk / "etc" / "lsxve-rsyslog.conf").read_text(encoding="utf-8")
        self.assertIn('/var/log/lsxve/lsxve.log', rs)
        self.assertIn('$programname startswith "lsxve"', rs)
        lr = (pk / "etc" / "lsxve-logrotate").read_text(encoding="utf-8")
        self.assertIn("size 10M", lr)
        self.assertNotIn("delaycompress", lr)
        inst = (pk / "install-lsxve.sh").read_text(encoding="utf-8")
        self.assertIn("/etc/rsyslog.d/40-lsxve-local.conf", inst)
        self.assertIn("/etc/logrotate.d/lsxve", inst)
        self.assertIn("logrotate.timer.d/90-lsxve.conf", inst)
        self.assertIn("restorecon -R /var/log/lsxve", inst)
        self.assertIn("OnCalendar=hourly", (pk / "etc" / "lsxve-logrotate-timer.conf").read_text(encoding="utf-8"))


class Validation(unittest.TestCase):
    def test_names(self):
        self.assertEqual(v.hostname("esx01.lab.local"), "esx01.lab.local")
        for bad in ("-bad", "a" * 300, "x;rm -rf /", "a..b"):
            with self.assertRaises(v.ValidationError):
                v.hostname(bad)
        with self.assertRaises(v.ValidationError):
            v.password("short")
        with self.assertRaises(v.ValidationError):
            v.ipv4_interface("10.0.0.1", "f")
        self.assertEqual(v.ipv4_interface("10.0.0.1/24", "f"), "10.0.0.1/24")


class DomainXml(unittest.TestCase):
    def test_build_domain(self):
        from lsxve.api.routes_vm import validate_spec
        spec = validate_spec({"name": "win01", "os_type": "windows", "vcpus": 1, "memory_mib": 2048,
                              "firmware": "uefi", "secure_boot": True, "tpm": True})
        root = domxml.build_domain(spec, "kvm", [{"path": "/lsxve/datastores/d1/win01.qcow2", "block": False,
                                                  "format": "qcow2", "bus": "sata"}],
                                   "/lsxve/iso/win.iso", True, "admin")
        xml = domxml.tostring(root)
        self.assertIn("<name>win01</name>", xml)
        self.assertIn("listen=\"127.0.0.1\"", xml)       # VNC never exposed on the network
        self.assertIn("secure-boot", xml)
        self.assertIn("<smm", xml)
        self.assertIn("tpm", xml)
        with self.assertRaises(v.ValidationError):
            validate_spec({"name": "x", "vcpus": 1, "memory_mib": 512, "firmware": "bios", "secure_boot": True})
        with self.assertRaises(v.ValidationError):
            validate_spec({"name": "../etc", "vcpus": 1, "memory_mib": 512})

    def test_guest_agent_channel_state(self):
        from lsxve.api.routes_vm import validate_spec
        spec = validate_spec({"name": "lin01", "vcpus": 1, "memory_mib": 1024})
        xml = domxml.tostring(domxml.build_domain(spec, "kvm", [], None, False, "admin"))
        info = domxml.parse_domain(xml)
        self.assertEqual(info["guest_agent"], "configured")      # inactive XML: channel without state
        self.assertEqual(info["created_by"], "admin")
        live = xml.replace('name="org.qemu.guest_agent.0"', 'name="org.qemu.guest_agent.0" state="connected"')
        self.assertEqual(domxml.parse_domain(live)["guest_agent"], "connected")


class Dcui(unittest.TestCase):
    def test_kernel_messages_go_to_tty12(self):
        src = (ROOT / "src/lsxve/dcui/main.py").read_text()   # curses is not importable on every build host
        for needle in ("TIOCLINUX = 0x541C", "TIOCL_SETKMSGREDIRECT = 11", "KMSG_VT = 12", "redirect_kernel_messages()"):
            self.assertIn(needle, src)
        unit = (ROOT / "packaging/systemd/lsxve-syslog-console.service").read_text()
        self.assertIn("TTYPath=/dev/tty11", unit)
        self.assertIn("lsxve-syslog-console.service", (ROOT / "packaging/install-lsxve.sh").read_text())


class BridgedNetwork(unittest.TestCase):
    def test_bridge_copies_ip_configuration(self):
        from lsxve.host import mgmtbridge
        dhcp = {"type": "ethernet", "ipv4": {"method": "auto", "addresses": [], "gateway": None, "dns": []},
                "ipv6": {"method": "auto"}}
        self.assertEqual(mgmtbridge.ip_args(dhcp, ""), ["ipv4.method", "auto", "ipv6.method", "auto"])
        static = {"type": "ethernet", "ipv4": {"method": "manual", "addresses": ["10.6.0.8/8"], "gateway": "10.0.0.138",
                                                "dns": ["10.0.0.138", "1.1.1.1"]}, "ipv6": {"method": "ignore"}}
        a = mgmtbridge.ip_args(static, "lan")
        self.assertEqual(a, ["ipv4.method", "manual", "ipv4.addresses", "10.6.0.8/8", "ipv4.gateway", "10.0.0.138",
                             "ipv4.dns", "10.0.0.138 1.1.1.1", "ipv4.dns-search", "lan", "ipv6.method", "ignore"])
        p = mgmtbridge.port_args("br0", "ens33", static)
        self.assertEqual(p[:6], ["connection", "add", "type", "ethernet", "ifname", "ens33"])
        self.assertIn("master", p)
        self.assertEqual(p[p.index("master") + 1], "br0")
        vl = mgmtbridge.port_args("br0", "ens33.10", {"type": "vlan", "vlan": {"parent": "ens33", "id": "10"}})
        self.assertEqual(vl[:10], ["connection", "add", "type", "vlan", "ifname", "ens33.10", "dev", "ens33", "id", "10"])

    def test_default_network_is_bridged(self):
        xml = domxml.build_network({"name": "default", "mode": "bridge", "bridge": "br0"})
        self.assertIn('<forward mode="bridge"', xml)
        self.assertNotIn("<ip", xml)
        info = domxml.parse_network(xml)
        self.assertEqual((info["mode"], info["bridge"]), ("bridge", "br0"))


class Lxc(unittest.TestCase):
    CFG = """# Template used to create this container: /usr/share/lxc/templates/lxc-download
lxc.include = /usr/share/lxc/config/common.conf
lxc.idmap = u 0 100000 65536
lxc.idmap = g 0 100000 65536
lxc.rootfs.path = dir:/var/lib/lxc/web1/rootfs
lxc.uts.name = web1
lxc.net.0.type = veth
lxc.net.0.link = virbr0
lxc.cgroup2.memory.max = 268435456
"""

    def test_config_rewrite_only_touches_managed_keys(self):
        out = lxc.rewrite_config(self.CFG, {lxc.K_MEM: str(512 * 1048576), lxc.K_AUTO: "1", lxc.K_IP4: None})
        items = lxc.parse_config(out)
        self.assertEqual(lxc.cfg_get(items, lxc.K_MEM), "536870912")
        self.assertEqual(lxc.cfg_get(items, lxc.K_AUTO), "1")
        self.assertIsNone(lxc.cfg_get(items, lxc.K_IP4))
        self.assertEqual([k for k, _ in items].count(lxc.K_MEM), 1)
        self.assertIn("# Template used", out)
        self.assertEqual(lxc._rootfs(items), "/var/lib/lxc/web1/rootfs")
        self.assertEqual(lxc._cpus(lxc.parse_config("lxc.cgroup2.cpu.max = 200000 100000")), 2.0)
        self.assertEqual(lxc._cpus(items), 0.0)
        # idempotent: rewriting again does not duplicate the marker or keys
        again = lxc.rewrite_config(out, {lxc.K_AUTO: "1"})
        self.assertEqual(again.count("# managed by LSX-VE"), 1)

    def test_parse_info(self):
        i = lxc.parse_info("Name:           web1\nState:          RUNNING\nPID:            4242\n"
                           "IP:             192.168.122.50\nIP:             fd00::5\nCPU use:        123456789\n"
                           "Memory use:     52428800\nLink:           vethX\n TX bytes:      100\n")
        self.assertEqual(i["state"], "running")
        self.assertEqual(i["pid"], 4242)
        self.assertEqual(i["ips"], ["192.168.122.50", "fd00::5"])
        self.assertEqual(i["memory_bytes"], 52428800)
        self.assertEqual(i["tx_bytes"], 100)
        self.assertEqual(lxc.parse_info("State: STOPPED")["state"], "stopped")

    def test_parse_index(self):
        idx = lxc.parse_index("debian;bookworm;amd64;default;20250101_05:24;/images/debian/bookworm/amd64/default/x\n"
                              "debian;bookworm;arm64;default;20250101_05:24;/images/x\n"
                              "bad;rel$;amd64;default;1;/x\n"
                              "alpine;3.20;amd64;cloud;20250101;/images/alpine\n")
        self.assertEqual([(i["distro"], i["release"], i["variant"]) for i in idx],
                         [("alpine", "3.20", "cloud"), ("debian", "bookworm", "default")])

    def test_validate_spec(self):
        good = {"name": "web1", "source": {"type": "download", "distro": "debian", "release": "bookworm"},
                "bridge": "virbr0", "memory_mib": 512, "cpus": 2}
        s = lxc.validate_spec(good)
        self.assertTrue(s["unprivileged"])
        self.assertEqual(s["ipv4"], "dhcp")
        self.assertEqual(s["source"]["variant"], "default")
        for bad in ({"name": "web_1"}, {"name": "status"}, {"name": "-x"},
                    {"source": {"type": "download", "distro": "debian;rm", "release": "x"}},
                    {"source": {"type": "tarball", "file": "../etc/shadow.tar"}},
                    {"source": {"type": "tarball", "file": "rootfs.zip"}},
                    {"bridge": "virbr0 x"}, {"ipv4": "10.0.0.5"}, {"root_password": "short"},
                    {"source": {"type": "oci"}}):
            with self.assertRaises(v.ValidationError, msg=str(bad)):
                lxc.validate_spec({**good, **bad})

    def test_os_release_ignores_symlinks(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "etc"))
            with open(os.path.join(d, "etc", "os-release"), "w") as f:
                f.write('NAME="Debian GNU/Linux"\nPRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\n')
            self.assertEqual(lxc.os_release(d), "Debian GNU/Linux 12 (bookworm)")
            self.assertIsNone(lxc._safe_file(d, "etc/missing"))
            if hasattr(os, "symlink"):
                try:
                    os.remove(os.path.join(d, "etc", "os-release"))
                    os.symlink("/etc/hostname", os.path.join(d, "etc", "os-release"))
                except OSError:
                    return  # no symlink privilege (Windows)
                self.assertIsNone(lxc._safe_file(d, "etc/os-release"))


class Batch2(unittest.TestCase):
    VM = {"name": "web01", "vcpus": 2, "memory_mib": 2048, "os_type": "linux", "os_variant": "Rocky Linux 9",
          "description": "test", "hardware": {"firmware": "uefi", "nics": [{"source": "default", "model": "virtio",
                                                                           "mac": "52:54:00:00:00:01"}],
                                              "disks": [{"target": "vda", "capacity": 10 * 2 ** 30}], "cdroms": []}}

    def test_ovf_roundtrip(self):
        from lsxve.hv import ovf
        text = ovf.build(self.VM, [{"file": "web01-disk1.vmdk", "size": 123456, "capacity": 10 * 2 ** 30}])
        info = ovf.parse(text)
        self.assertEqual(info["name"], "web01")
        self.assertEqual(info["vcpus"], 2)
        self.assertEqual(info["memory_mib"], 2048)
        self.assertEqual(info["firmware"], "uefi")
        self.assertEqual([d["file"] for d in info["disks"]], ["web01-disk1.vmdk"])
        self.assertEqual(info["disks"][0]["capacity"], 10 * 2 ** 30)
        self.assertEqual(len(info["nics"]), 1)
        with self.assertRaises(Exception):   # no DTD / entity expansion
            ovf.parse('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>')
        self.assertEqual(ovf.nic_model("VmxNet3", "windows"), "e1000e")
        self.assertEqual(ovf.nic_model("VmxNet3", "linux"), "virtio")

    def test_tuning_hostdev_autostart(self):
        import xml.etree.ElementTree as ET
        from lsxve.api.routes_vm import validate_spec
        spec = {**validate_spec({"name": "t1", "vcpus": 1, "memory_mib": 1024, "cpu_hotadd": True}), "vcpus_max": 4}
        root = domxml.build_domain(spec, "kvm", [{"path": "/lsxve/datastores/d1/t1.qcow2", "block": False,
                                                  "format": "qcow2", "bus": "virtio"}], None, False, "admin")
        domxml.set_tuning(root, cpu_shares=2000, cpu_quota_us=50000, mem_lock=True)
        root.find("devices").append(domxml.build_hostdev({"type": "pci", "address": "0000:03:00.1"}))
        root.find("devices").append(domxml.build_hostdev({"type": "usb", "vendor": "046d", "product": "c52b"}))
        domxml.set_disk_iotune(root.find("devices/disk"), 500, 50 * 2 ** 20)
        domxml.set_video(root, "vga", 32)
        md = root.find("metadata")
        if md is None:
            md = ET.SubElement(root, "metadata")
        md.append(ET.fromstring(domxml.build_autostart({"enabled": True, "order": 2, "start_delay": 10,
                                                        "stop_action": "poweroff"})))
        info = domxml.parse_domain(domxml.tostring(root))
        self.assertEqual(info["cpu_shares"], 2000)
        self.assertEqual(info["cpu_quota_us"], 50000)
        self.assertTrue(info["mem_locked"])
        self.assertEqual(info["video"]["model"], "vga")
        self.assertEqual(info["video"]["vram_mib"], 32)
        self.assertEqual({h["id"] for h in info["hostdevs"]}, {"pci:0000:03:00.1", "usb:046d:c52b"})
        self.assertEqual(info["disks"][0]["iops_limit"], 500)
        self.assertEqual(info["disks"][0]["bps_limit"], 50 * 2 ** 20)
        a = info["autostart_cfg"]
        self.assertTrue(a["enabled"])
        self.assertEqual((a["order"], a["start_delay"], a["stop_action"], a["stop_delay"]), (2, 10, "poweroff", -1))
        self.assertTrue(info["cpu_hotadd"])
        self.assertEqual((info["vcpus"], info["vcpus_max"]), (1, 4))

    def test_portgroup_network(self):
        xml = domxml.build_network({"name": "VLAN20", "mode": "bridge", "bridge": "br0v20", "vswitch": "br0", "vlan": 20})
        n = domxml.parse_network(xml)
        self.assertEqual((n["vswitch"], n["vlan"], n["bridge"]), ("br0", 20, "br0v20"))

    def test_vswitch_and_bridge_helpers(self):
        from lsxve.host import mgmtbridge, vswitch
        self.assertEqual(vswitch.backing_name("br0", 20), "br0v20")
        self.assertLessEqual(len(vswitch.backing_name("averyverylongswitch", 4094)), 15)   # IFNAMSIZ
        self.assertEqual(mgmtbridge.mtu_of("9000"), 9000)
        self.assertIsNone(mgmtbridge.mtu_of("auto"))
        self.assertIsNone(mgmtbridge.mtu_of("1500"))
        args = mgmtbridge.bond_port_args("br0")
        self.assertIn("connection.master", args)
        self.assertEqual(args[args.index("ipv4.method") + 1], "disabled")

    def test_cpu_quota(self):
        from lsxve.api import routes_vm
        self.assertEqual(routes_vm.cpu_quota_from_mhz(0), 0)
        self.assertGreater(routes_vm.cpu_quota_from_mhz(1000), 0)

    def test_alarm_levels(self):
        from lsxve.api.alarms import AlarmManager
        cfg = {"warning": 80, "critical": 90}
        self.assertIsNone(AlarmManager._level(50, cfg))
        self.assertEqual(AlarmManager._level(85, cfg), "warning")
        self.assertEqual(AlarmManager._level(95, cfg), "critical")



class Batch3(unittest.TestCase):
    """Memory hot add, file-backed serial ports, laptop power handling."""

    def _dom(self, **kw):
        from lsxve.api.routes_vm import validate_spec
        spec = {**validate_spec({"name": "m1", "vcpus": 2, "memory_mib": 2048, **kw}), "vcpus_max": 4,
                "mem_max_mib": 16384}
        return domxml.build_domain(spec, "kvm", [{"path": "/d/m1.qcow2", "block": False, "format": "qcow2",
                                                  "bus": "virtio"}], None, False, "admin")

    def test_memory_hotadd(self):
        root = self._dom(mem_hotadd=True, cpu_hotadd=True)
        info = domxml.parse_domain(domxml.tostring(root))
        self.assertTrue(info["mem_hotadd"])
        self.assertEqual((info["mem_max_mib"], info["memory_mib"], info["mem_dimms"]), (16384, 2048, 0))
        cell = root.find("cpu/numa/cell")
        self.assertEqual((cell.get("cpus"), cell.get("memory")), ("0-3", "2048"))
        self.assertEqual(root.find("maxMemory").get("slots"), str(domxml.MEM_SLOTS))
        # a hot-added DIMM, as libvirt stores it in the persistent config
        root.find("devices").append(domxml.build_dimm(1024))
        root.find("memory").text = str(3072 * 1024)
        root.find("memory").set("unit", "KiB")
        info = domxml.parse_domain(domxml.tostring(root))
        self.assertEqual((info["memory_mib"], info["mem_dimms"]), (3072, 1))
        # rewriting the configuration folds the DIMMs into the boot memory
        domxml.set_memory(root, 4096, True, 16384)
        info = domxml.parse_domain(domxml.tostring(root))
        self.assertEqual((info["memory_mib"], info["mem_dimms"]), (4096, 0))
        self.assertEqual(root.find("cpu/numa/cell").get("memory"), "4096")
        domxml.set_vcpus(root, 2, False, 2)
        self.assertEqual(root.find("cpu/numa/cell").get("cpus"), "0-1")
        domxml.set_memory(root, 4096, False, 0)
        info = domxml.parse_domain(domxml.tostring(root))
        self.assertFalse(info["mem_hotadd"])
        self.assertIsNone(root.find("cpu/numa"))
        self.assertIsNone(root.find("maxMemory"))

    def test_no_hotadd_by_default(self):
        root = self._dom()
        self.assertIsNone(root.find("maxMemory"))
        self.assertIsNone(root.find("cpu/numa"))

    def test_serial_ports(self):
        root = self._dom()
        root.find("devices").append(domxml.build_serial_file(1, "/lsxve/datastores/d1/m1-com2.log"))
        info = domxml.parse_domain(domxml.tostring(root))
        self.assertEqual([(x["port"], x["type"], x["console"]) for x in info["serials"]],
                         [(0, "pty", True), (1, "file", False)])
        self.assertEqual(info["serials"][1]["path"], "/lsxve/datastores/d1/m1-com2.log")
        self.assertEqual(info["usb_controllers"], ["qemu-xhci"])

    def test_laptop_power_config(self):
        pk = ROOT / "packaging"
        logind = (pk / "etc" / "lsxve-logind.conf").read_text(encoding="utf-8")
        for k in ("HandleLidSwitch", "HandleLidSwitchExternalPower", "HandleLidSwitchDocked",
                  "HandleSuspendKey", "HandleHibernateKey", "IdleAction"):
            self.assertIn(k + "=ignore", logind.splitlines())
        self.assertIn("AllowSuspend=no", (pk / "etc" / "lsxve-sleep.conf").read_text(encoding="utf-8"))
        inst = (pk / "install-lsxve.sh").read_text(encoding="utf-8")
        self.assertIn("logind.conf.d/90-lsxve.conf", inst)
        self.assertIn("systemctl mask sleep.target suspend.target hibernate.target", inst)
        from lsxve.host import hardware
        self.assertIsInstance(hardware.is_laptop(), bool)
        self.assertIn(8 + 2, hardware.LAPTOP_CHASSIS)  # "Notebook"


if __name__ == "__main__":
    unittest.main()
