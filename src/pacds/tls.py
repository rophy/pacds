"""Trust for outbound TLS: the system CAs plus an optional corporate CA, for every client PACDS runs."""

from __future__ import annotations

import ssl
from pathlib import Path


def ca_bundle(ca_file: Path | None, work_dir: Path) -> Path | None:
    """Write the system CA bundle followed by ca_file to work_dir; None when no extra CA is configured.

    One file serves Python clients (as an SSL context) and git (GIT_SSL_CAINFO, which replaces git's own list),
    so internal hosts signed by the corporate CA and public ones both verify.
    """
    if ca_file is None:
        return None
    extra = Path(ca_file).read_text()
    if "BEGIN CERTIFICATE" not in extra:
        raise ValueError(f"tls.ca_file {ca_file} holds no PEM certificate")
    system = ssl.get_default_verify_paths().cafile
    base = Path(system).read_text() if system and Path(system).is_file() else ""
    work_dir.mkdir(parents=True, exist_ok=True)
    bundle = work_dir / "ca-bundle.pem"
    bundle.write_text(base.rstrip("\n") + "\n" + extra)
    return bundle


def context(bundle: Path | None) -> ssl.SSLContext | bool:
    """An SSL context trusting the bundle, or True (the client's default trust) without one."""
    return ssl.create_default_context(cafile=str(bundle)) if bundle else True
