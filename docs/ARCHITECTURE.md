# LSX-VE architecture

## Components and processes

| Unit | Runs as | Purpose |
|---|---|---|
| `lsxve-init.service` | root, oneshot at boot | **Core**: loads `kvm_intel`/`kvm_amd`, creates and labels directories, generates the self-signed TLS certificate on first boot, starts the modular libvirt daemons, writes `/etc/issue.d/50-lsxve.issue` (management URL), records `/run/lsxve/host.json` (hardware/virtualization snapshot). |
| `lsxve-agent.service` | root | The **only** privileged LSX-VE process. Listens on `/run/lsxve/agent.sock` (0660 root:lsxve) and accepts only root or the `lsxve` uid (SO_PEERCRED). It exposes a fixed table of operations (`host.*`, `firewall.*`, `network.*`, `storage.*`, `ct.*`) and has no generic command execution. Mutating operations are serialised and logged, with secrets redacted. |
| `lsxve-api.service` | `lsxve` (groups `libvirt`, `systemd-journal`), `CAP_NET_BIND_SERVICE` only | **Management API + web console**: an asyncio HTTPS/1.1 server with REST `/api/v1/*`, a WebSocket event stream, a WebSocket VNC bridge, and static SPA files. It talks to libvirt over the local socket (polkit rule `50-lsxve.rules`) and to the agent for host changes. Sandboxed with `ProtectSystem=strict`, `MemoryDenyWriteExecute`, `SystemCallFilter=@system-service`, `NoNewPrivileges`. |
| `lsxve-dcui.service` | root on tty1 | **Local console** (curses). Replaces `getty@tty1`. The status screen needs no login. The F2 menu requires the root password and calls the same agent operations as the web UI. At start it redirects kernel console output to tty12 (`TIOCL_SETKMSGREDIRECT`) and repaints the full screen every refresh. |
| `lsxve-syslog-console.service` | root on tty11 | Read-only `journalctl -f` view (Alt+F11). |
| `lsxve-ct@NAME.service` | root (LXC monitor), container processes in their user namespace | One instance per running LXC container: `lxc-start -F`, `Delegate=yes`, clean `lxc-stop -t 90` when stopped or at host shutdown. Started by the agent, so restarting the agent or the API never affects containers. The agent starts containers marked for autostart once per boot. |
| `virtqemud`, `virtnetworkd`, `virtstoraged`, `virtnodedevd`, … | libvirt | Hypervisor layer: modular libvirt daemons, QEMU/KVM, OVMF (UEFI / Secure Boot), swtpm (vTPM). |

## Request flow

1. The browser sends HTTPS to `lsxve-api`. The middleware applies, in order: per-IP rate limit, then an
   `Origin` check on state-changing requests and WebSockets, then the session cookie (`HttpOnly`,
   `Secure`, `SameSite=Strict`, only its sha256 stored), then `X-CSRF-Token`, then the
   forced-password-change gate, then the route permission (RBAC).
2. The route handler validates every field (`common/validate.py`) and builds argv lists or XML
   from the validated values. User input is never interpolated into a shell. `proc.run` refuses
   any executable that is not on an absolute-path allow-list.
3. Long operations (VM create/clone/delete, power, uploads, formatting) become **tasks**
   (`api/events.py`). They carry progress and a per-target exclusive lock, and their state is
   streamed over `/api/v1/ws`.
4. libvirt domain lifecycle events and host metrics (every 3 s) are pushed to the browser over
   the same WebSocket. Each client only receives what its permissions allow.

## Bridged VM network

`host/mgmtbridge.py` runs once in `lsxve-agent` at start-up. It finds the interface with the default
route and creates bridge `br0` with that connection's IP settings, DNS, zone and MAC, plus a
port connection for the NIC. It switches over inside a NetworkManager checkpoint (300 s) and
verifies that `br0` has an address and that the gateway answers, if it answered before. Then it
commits or rolls back. The outcome is stored in `/var/lib/lsxve/management-bridge.json` (never
retried in a loop; `network.bridge_retry` retries on request). The API then makes the libvirt
network `default` a `forward mode="bridge"` network on `br0`. LXC containers attach to `br0` directly.

## LXC containers

* `ct/lxc.py` runs inside the agent. It calls only `lxc-create`, `lxc-destroy`, `lxc-info`,
  `lxc-stop`, `lxc-freeze`, `lxc-unfreeze`, `lxc-copy`, `lxc-snapshot`, `tar` and `systemctl`
  through `proc.run` (allow-listed argv, no shell), plus `setsid --ctty --wait lxc-console` for
  the console.
* LSX-VE manages a few keys in each container's `config` (memory, swap, CPU quota, autostart,
  bridge, static IPv4) and leaves everything else as written by LXC or by you. Notes, creator and
  source are kept in `lsxve.json` next to the config.
* Containers are stored in `lxc.lxcpath` from `/etc/lxc/lxc.conf`. On first boot `lsxve-init` sets it
  to `/lsxve/datastores/datastore1/containers` when datastore1 exists, otherwise `/var/lib/lxc`. A
  value set by you is kept. The Storage page shows that directory as "in use by LXC containers".
* Console: the API asks the agent for `ct.console`. The agent opens a pty, starts `lxc-console`
  on it and returns the pty master **file descriptor** over the Unix socket (`SCM_RIGHTS`). The
  API bridges it to a WebSocket (binary frames = terminal data, text frames = resize). Closing
  the browser closes the fd, and `lxc-console` exits on the hang-up. The console is gated by a
  30 s one-time ticket, like the VM console.

## Storage layout on an installed host

```
partition 1  1 GiB         /boot (ext4, label lsxve-boot)
partition 2  1 GiB         swap
partition 3  32 GiB        /lsxve/iso (ext4, label lsxve-iso)
partition 4  rest          / (ext4, label lsxve-root)
partition 5  600 MiB       /boot/efi (EFI system partition, UEFI only; 2 MiB BIOS boot on BIOS+GPT >2 TiB)
/lsxve/datastores/datastore1   directory on root (marker .lsxve-datastore; libvirt pool "datastore1")
/lsxve/datastores/datastore1/containers   LXC containers (lxc.lxcpath; /var/lib/lxc without datastore1)
/lsxve/iso                ISO library (libvirt pool "iso", SELinux virt_content_t)
/var/lib/lsxve/lsxve.db   SQLite (users, roles, sessions, tasks, events, audit)
/etc/lsxve/               lsxve.conf (JSON), tls/lsxve.{crt,key}
```

## Virtual switches and port groups

A virtual switch is a Linux bridge with physical (or bond) uplinks; `br0` is the management switch
(vSwitch0). A port group is a libvirt network in bridge mode with LSX-VE metadata (`vswitch`, `vlan`).
VLAN 0 attaches VMs directly to the switch bridge. VLAN *N* uses a backing bridge `<switch>v<N>` whose only
port is the 802.1Q sub-interface `<uplink>.<N>`, so guest frames leave the uplink tagged. Backing
bridges are NetworkManager connections created by `lsxve-agent` inside a checkpoint that rolls back if the
management address disappears, and removed with the last port group that uses them.

## Host administration services

* **Alarms** - evaluated every 30 s in `lsxve-api` (`api/alarms.py`) from the host sampler, VM statistics,
  datastores, vSwitch uplinks and the TLS certificate; state changes go to the event log, syslog and the
  WebSocket. Thresholds are stored in the database.
* **Maintenance mode** - `/var/lib/lsxve/maintenance.json`; checked by every power-on path (API, autostart).
* **Autostart** - per-VM settings in the domain metadata, host defaults in `/var/lib/lsxve/autostart.json`,
  executed by `lsxve-vm-autostart.service` (start in order at boot, stop in reverse order before
  libvirt-guests at shutdown). libvirt's own unordered autostart is switched off.
* **Syslog / SNMP / power policy / lockdown / support bundle** - allow-listed `lsxve-agent` operations that
  write `/etc/rsyslog.d/90-lsxve-remote.conf`, `/etc/snmp/snmpd.conf` (+ firewalld service), run
  `tuned-adm`, and stop sshd / local gettys. Support bundles are created by the agent in
  `/var/lib/lsxve/support` without secrets.
* **Import / export** - `lsxve-agent` (`host/vmio.py`) reads OVF descriptors (no DTDs/entities), unpacks
  OVAs, converts disks with `qemu-img` (no backing files allowed) and packs OVAs; it only touches paths
  under the datastore roots and only deletes its own `import-*` / `export-*` temporary files.

## Network change safety

Network changes made from the web UI or the DCUI follow these steps:

1. `network.apply` creates a NetworkManager **checkpoint** (D-Bus `CheckpointCreate`, flags
   destroy-all | delete-new-connections | disconnect-new-devices) with a rollback timeout of 90 s
   in the web UI and 60 s in the DCUI.
2. The validated change is applied with `nmcli` (argv only).
3. The UI shows a countdown. The administrator must confirm from the new address, or from the
   console, before the timeout.
4. If nobody confirms (for example because the management connection was lost), **NetworkManager
   itself** rolls back to the checkpoint, even if LSX-VE has crashed.

## Disk safety

* The installer offers only non-media, writable disks. Before it writes anything it shows the
  model, size and existing partitions, and requires the device name to be typed.
* Formatting a disk in the web console runs a usage check first (mounted, LVM PV, swap, RAID
  member all refuse). The UI and the agent both require the confirmation string `/dev/<name>`.
* Nothing in LSX-VE ever formats or wipes a disk on its own.

## Build pipeline (ISO)

`build/build.py` takes the Rocky Linux Minimal ISO and:

1. extracts it,
2. resolves the RPM closure of the hypervisor stack against the ISO repository plus the official
   Rocky BaseOS/AppStream (`repotool.py`) and writes `Hypervisor/` (sha256-verified RPMs and
   repodata),
3. adds the `lsxve/` payload,
4. generates `ks.cfg`, isolinux/grub menus (also patched inside `images/efiboot.img` with
   `fatimg.py`), `splash.png`, `images/product.img` (installer branding), and the `.treeinfo`
   variant `LSXVE`,
5. masters a new Rock Ridge ISO with El Torito BIOS + UEFI and isohybrid MBR/GPT using pycdlib.

The signed shim, GRUB and kernel are copied unchanged, so UEFI Secure Boot keeps working with
the Rocky Linux keys.
