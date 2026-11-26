# Security model

| Requirement | Implementation |
|---|---|
| Password hashing | scrypt (N=2^15, r=8, p=1, 16-byte salt), constant-time compare, dummy verification for unknown users. Root password uses SHA-512 crypt. |
| Sessions | 256-bit random token in an `HttpOnly; Secure; SameSite=Strict` cookie. Only the sha256 is stored. Idle timeout 30 min and absolute timeout 12 h (configurable). Sessions are revoked on password change, user disable/delete and logout. |
| RBAC | Permissions: host.view/config/power, vm.view/power/console/snapshot/manage, ct.view/power/console/snapshot/manage, storage.view/manage/format, network.view/manage, security.manage, logs.view. Built-in roles Administrator, Operator and ReadOnly cannot be modified, and custom roles can be added. The last enabled administrator cannot be removed. |
| CSRF | `X-CSRF-Token` header bound to the session on every state-changing request, plus an `Origin` check (also on WebSockets). |
| XSS | React escaping. Strict CSP: `default-src 'self'`, no inline script, `frame-ancestors 'none'`. `X-Content-Type-Options`, `Referrer-Policy: no-referrer`, HSTS. |
| Input validation | Every field is validated server-side by type, range and regex (names, hostnames, IPs, interfaces, IQNs, paths restricted to datastore prefixes). Violations return 422 with the field name. |
| No command execution | No endpoint runs a user-supplied command. `proc.run` executes only allow-listed absolute paths with argv lists (no shell), a fixed environment and timeouts. |
| Least privilege | The API runs as `lsxve` with `CAP_NET_BIND_SERVICE` only and a systemd sandbox. Root actions go through `lsxve-agent`, which has a fixed operation table, peer-credential checks and a protected firewall rule for the management service. |
| Web root access | The web UI exposes no shell and no root terminal. Setting the root password is a dedicated validated operation (`security.manage`). |
| libvirt / QEMU | libvirt sockets are local only. VNC listens on 127.0.0.1 with a random per-VM password. The browser console uses a one-time ticket (30 s, bound to user and session) over the authenticated WSS bridge. |
| LXC containers | Unprivileged by default: uid/gid 0–65535 in the container are mapped to 100000–165535 on the host, so root in the container is not root on the host. A privileged container must be chosen explicitly, and the UI warns. Image names are checked against the image server's index, and the LXC download template GPG-verifies the image. Rootfs tarballs come only from the ISO library and are unpacked with GNU tar (no absolute paths, no `..`, `security.selinux` xattrs dropped). Files inside the untrusted rootfs (`/etc/os-release`, `/etc/shadow`) are opened only if no path component is a symlink. The container console is the container's own login prompt (`/dev/console`), not a host shell. |
| Third-party packages | EPEL 9 and Rocky Extras are used at build time with a package **allowlist** (`build/packages.json`, `"only"`): only `lxc`, `lxc-libs`, `lxc-templates`, `lxcfs`, `htop`, `nload` (EPEL) and `epel-release` (Extras) can come from them, so they can never replace a Rocky package. The installed `epel-release` enables EPEL for later `dnf` use on the host. |
| TLS | TLS 1.2+ only. The self-signed certificate (RSA-3072, SAN = hostname + IPs) is generated on first boot. You can import your own certificate and key (validated as a matching pair, hot-reloaded). HTTP on :80 only redirects. |
| Brute force | Login limited to 20 attempts per 5 min per IP. Accounts lock for 5 min after 5 failures. API rate limit 1200 requests per minute per IP. |
| Audit | Every mutating request records user, IP, action, target and result in the `audit` table (Monitor → Audit log). Agent operations are journaled. |
| Cockpit / Webmin | Excluded at install (`-cockpit*`). `install-lsxve.sh` disables the cockpit socket and removes the service from firewalld. |
