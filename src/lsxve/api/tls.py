"""TLS certificate management for the management interface."""
from __future__ import annotations

import os
import socket
import ssl
import tempfile
import time
from pathlib import Path
from typing import Optional

from ..common import config, proc

CERT = config.TLS_DIR / "lsxve.crt"
KEY = config.TLS_DIR / "lsxve.key"


def generate_self_signed(hostname: Optional[str] = None, ips: Optional[list] = None) -> None:
    hostname = hostname or socket.gethostname() or "lsxve"
    config.TLS_DIR.mkdir(parents=True, exist_ok=True)
    san = [f"DNS:{hostname}", "DNS:localhost"] + [f"IP:{ip}" for ip in (ips or [])] + ["IP:127.0.0.1"]
    tmpdir = tempfile.mkdtemp(dir=str(config.TLS_DIR))
    try:
        k, c = os.path.join(tmpdir, "k"), os.path.join(tmpdir, "c")
        proc.run(["/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:3072", "-sha256", "-nodes", "-days", "3650",
                  "-keyout", k, "-out", c, "-subj", f"/O=LSX-VE/OU=Self-signed/CN={hostname}",
                  "-addext", "subjectAltName=" + ",".join(san),
                  "-addext", "keyUsage=digitalSignature,keyEncipherment",
                  "-addext", "extendedKeyUsage=serverAuth"], timeout=120)
        os.chmod(k, 0o600)
        os.replace(k, KEY)
        os.replace(c, CERT)
    finally:
        for f in os.listdir(tmpdir):
            os.unlink(os.path.join(tmpdir, f))
        os.rmdir(tmpdir)


def make_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_ciphers("ECDHE+AESGCM:ECDHE+CHACHA20:DHE+AESGCM:!aNULL:!MD5:!DSS")
    ctx.options |= ssl.OP_NO_COMPRESSION | ssl.OP_CIPHER_SERVER_PREFERENCE
    ctx.load_cert_chain(str(CERT), str(KEY))
    return ctx


def info() -> dict:
    try:
        d = ssl._ssl._test_decode_cert(str(CERT))  # type: ignore[attr-defined]
    except Exception as e:
        return {"present": CERT.exists(), "error": str(e)}

    def name(seq):
        return ", ".join(f"{k}={v}" for rdn in seq for k, v in rdn)
    not_after = ssl.cert_time_to_seconds(d["notAfter"])
    fp = ""
    try:
        der = ssl.PEM_cert_to_DER_cert(CERT.read_text())
        import hashlib
        fp = hashlib.sha256(der).hexdigest().upper()
        fp = ":".join(fp[i:i + 2] for i in range(0, len(fp), 2))
    except Exception:
        pass
    subject, issuer = name(d.get("subject", ())), name(d.get("issuer", ()))
    return {"present": True, "subject": subject, "issuer": issuer, "self_signed": subject == issuer,
            "not_before": ssl.cert_time_to_seconds(d["notBefore"]), "not_after": not_after,
            "expired": not_after < time.time(), "serial": d.get("serialNumber"),
            "san": [f"{t}:{v}" for t, v in d.get("subjectAltName", ())], "sha256_fingerprint": fp}


def install(cert_pem: str, key_pem: str, ctx: ssl.SSLContext) -> dict:
    """Validate and install a PEM certificate (+chain) and private key; hot-reload ``ctx``."""
    if "-----BEGIN CERTIFICATE-----" not in cert_pem or "PRIVATE KEY-----" not in key_pem:
        raise ValueError("Certificate and key must be PEM encoded")
    if len(cert_pem) > 64 * 1024 or len(key_pem) > 32 * 1024:
        raise ValueError("Certificate or key too large")
    tmpdir = tempfile.mkdtemp(dir=str(config.TLS_DIR))
    c, k = os.path.join(tmpdir, "c"), os.path.join(tmpdir, "k")
    try:
        with open(c, "w") as fh:
            fh.write(cert_pem.strip() + "\n")
        fd = os.open(k, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(key_pem.strip() + "\n")
        test = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            test.load_cert_chain(c, k)
        except ssl.SSLError as e:
            raise ValueError(f"Certificate and key do not match or are invalid ({e.reason or e})")
        d = ssl._ssl._test_decode_cert(c)  # type: ignore[attr-defined]
        if ssl.cert_time_to_seconds(d["notAfter"]) < time.time():
            raise ValueError("Certificate has expired")
        os.replace(KEY, str(KEY) + ".bak") if KEY.exists() else None
        os.replace(CERT, str(CERT) + ".bak") if CERT.exists() else None
        os.replace(k, KEY)
        os.replace(c, CERT)
    finally:
        for f in os.listdir(tmpdir):
            os.unlink(os.path.join(tmpdir, f))
        os.rmdir(tmpdir)
    ctx.load_cert_chain(str(CERT), str(KEY))
    return info()


def ensure() -> None:
    if not CERT.exists() or not KEY.exists():
        generate_self_signed()

