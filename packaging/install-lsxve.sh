#!/bin/bash
# Install the LSX-VE payload onto a Rocky Linux 9 system.
# Used by the LSX-VE installer (%post, chroot) and for manual installs:
#     ./install-lsxve.sh [payload-dir]
# payload-dir must contain python/lsxve, web/, packaging/ (default: script dir).
set -euo pipefail
P="${1:-$(cd "$(dirname "$0")" && pwd)}"
PK="$P/packaging"
VERSION="$(cat "$P/VERSION" 2>/dev/null || echo 1.0.0)"
log() { echo "[lsxve-install] $*"; }

[ -d "$P/python/lsxve" ] || { echo "payload not found in $P" >&2; exit 1; }

log "installing LSX-VE $VERSION from $P"
install -d -m 0755 /usr/lib/lsxve/python /usr/libexec/lsxve /usr/share/lsxve
rm -rf /usr/lib/lsxve/python/lsxve /usr/share/lsxve/web
cp -r "$P/python/lsxve" /usr/lib/lsxve/python/
find /usr/lib/lsxve/python -name '__pycache__' -prune -exec rm -rf {} +
cp -r "$P/web" /usr/share/lsxve/web
# LSX-VE Guest Tools (VirtIO drivers + QEMU guest agent), mounted into VMs by "Install guest tools"
if [ -f "$P/guest-tools/lsx-ve-guest-tools.iso" ]; then
    install -d -m 0755 /usr/share/lsxve/guest-tools
    install -m 0644 "$P/guest-tools/lsx-ve-guest-tools.iso" /usr/share/lsxve/guest-tools/lsx-ve-guest-tools.iso
    if [ -f "$P/guest-tools/VERSION" ]; then
        install -m 0644 "$P/guest-tools/VERSION" /usr/share/lsxve/guest-tools/VERSION
    fi
fi
chmod -R u=rwX,go=rX /usr/lib/lsxve /usr/share/lsxve
python3 -m compileall -q /usr/lib/lsxve/python >/dev/null || true
echo "$VERSION" > /usr/share/lsxve/VERSION

for b in api agent init dcui vm-autostart; do install -m 0755 "$PK/bin/lsxve-$b" /usr/libexec/lsxve/lsxve-$b; done
install -m 0755 "$PK/bin/lsxve-admin" /usr/sbin/lsxve-admin

install -m 0644 "$PK"/systemd/*.service /usr/lib/systemd/system/
install -m 0644 "$PK/etc/lsxve.sysusers" /usr/lib/sysusers.d/lsxve.conf
install -m 0644 "$PK/etc/lsxve.tmpfiles" /usr/lib/tmpfiles.d/lsxve.conf
install -d -m 0755 /etc/polkit-1/rules.d
install -m 0644 "$PK/etc/50-lsxve.rules" /etc/polkit-1/rules.d/50-lsxve.rules
install -m 0644 "$PK/etc/lsxve-firewalld.xml" /usr/lib/firewalld/services/lsxve.xml
install -m 0644 "$PK/etc/90-lsxve.sysctl" /usr/lib/sysctl.d/90-lsxve.conf
install -m 0644 "$PK/etc/libvirt-guests.sysconfig" /etc/sysconfig/libvirt-guests
# never suspend (laptop lid, sleep keys, idle): the host keeps running its VMs
install -d -m 0755 /etc/systemd/logind.conf.d /etc/systemd/sleep.conf.d
install -m 0644 "$PK/etc/lsxve-logind.conf" /etc/systemd/logind.conf.d/90-lsxve.conf
install -m 0644 "$PK/etc/lsxve-sleep.conf" /etc/systemd/sleep.conf.d/90-lsxve.conf
# LSX-VE logs: /var/log/lsxve (own "oslog" partition on installed hosts), rotated by size
install -d -m 0755 /etc/rsyslog.d /etc/logrotate.d
install -m 0644 "$PK/etc/lsxve-rsyslog.conf" /etc/rsyslog.d/40-lsxve-local.conf
install -m 0644 "$PK/etc/lsxve-logrotate" /etc/logrotate.d/lsxve
# check sizes every hour (the stock timer is daily): a busy day must not fill the 250 MiB partition
install -d -m 0755 /etc/systemd/system/logrotate.timer.d
install -m 0644 "$PK/etc/lsxve-logrotate-timer.conf" /etc/systemd/system/logrotate.timer.d/90-lsxve.conf

# service account and directories
getent group libvirt >/dev/null || groupadd -r libvirt
systemd-sysusers /usr/lib/sysusers.d/lsxve.conf
usermod -a -G libvirt,systemd-journal lsxve
systemd-tmpfiles --create /usr/lib/tmpfiles.d/lsxve.conf
[ -f /etc/lsxve/lsxve.conf ] || install -m 0640 -o lsxve -g lsxve "$PK/etc/lsxve.conf" /etc/lsxve/lsxve.conf

# SELinux labels: datastores hold VM disks, the ISO library holds install media
if command -v semanage >/dev/null 2>&1; then
    semanage fcontext -a -t virt_image_t '/lsxve/datastores(/.*)?' 2>/dev/null || \
        semanage fcontext -m -t virt_image_t '/lsxve/datastores(/.*)?'
    semanage fcontext -a -t virt_content_t '/lsxve/iso(/.*)?' 2>/dev/null || \
        semanage fcontext -m -t virt_content_t '/lsxve/iso(/.*)?'
    semanage fcontext -a -t virt_content_t '/usr/share/lsxve/guest-tools(/.*)?' 2>/dev/null || \
        semanage fcontext -m -t virt_content_t '/usr/share/lsxve/guest-tools(/.*)?'
    restorecon -R /lsxve /usr/lib/lsxve /usr/libexec/lsxve /usr/share/lsxve /etc/lsxve 2>/dev/null || true
    # the log partition's root is not owned by any package; label it var_log_t so rsyslog may write
    install -d -m 0750 /var/log/lsxve
    restorecon -R /var/log/lsxve 2>/dev/null || true
fi

# product identity
sed -e "s/@VERSION@/$VERSION/" "$PK/etc/os-release.lsxve" > /etc/lsxve-release
cat > /etc/motd <<MOTD

  LSX-VE $VERSION - Linux Server eXtended Virtualization Environment
  Manage this host with a web browser: https://<management-ip>/
  Local administration: lsxve-admin, journalctl -u lsxve-api

MOTD

# no generic management panels on an LSX-VE host
for u in cockpit.socket cockpit.service; do systemctl disable --now "$u" 2>/dev/null || true; done
rm -f /etc/issue.d/cockpit.issue /etc/motd.d/cockpit 2>/dev/null || true

# hypervisor: modular libvirt daemons
systemctl disable libvirtd.service libvirtd.socket libvirtd-ro.socket libvirtd-admin.socket 2>/dev/null || true
for d in qemu network storage nodedev secret interface; do
    systemctl enable "virt${d}d.socket" "virt${d}d-ro.socket" "virt${d}d-admin.socket" 2>/dev/null || true
done
systemctl enable virtqemud.service virtlogd.socket virtlockd.socket 2>/dev/null || true
systemctl enable libvirt-guests.service 2>/dev/null || true

systemctl enable lsxve-init.service lsxve-agent.service lsxve-api.service lsxve-dcui.service lsxve-syslog-console.service lsxve-vm-autostart.service

# LXC containers: started per container as lsxve-ct@NAME.service by lsxve-agent (which also does
# autostart), so the stock lxc.service / lxc-net (lxcbr0) stay off; lxcfs gives containers their
# own /proc/meminfo, /proc/cpuinfo and uptime
if [ -x /usr/bin/lxc-start ]; then
    systemctl disable lxc.service lxc-net.service lxc-monitord.service 2>/dev/null || true
    systemctl enable lxcfs.service 2>/dev/null || true
fi
systemctl mask getty@tty1.service
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target suspend-then-hibernate.target
systemctl set-default multi-user.target

if systemctl is-enabled firewalld >/dev/null 2>&1 || [ -n "${LSXVE_OFFLINE:-}" ]; then
    firewall-offline-cmd --add-service=lsxve >/dev/null
    firewall-offline-cmd --remove-service=cockpit >/dev/null 2>&1 || true
fi
command -v tuned-adm >/dev/null 2>&1 && systemctl enable tuned 2>/dev/null && \
    { mkdir -p /etc/tuned; echo virtual-host > /etc/tuned/active_profile; echo manual > /etc/tuned/profile_mode; } || true

log "LSX-VE installed"
