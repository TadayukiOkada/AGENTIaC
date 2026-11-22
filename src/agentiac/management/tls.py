"""Validate operator-provided TLS files before binding a loopback listener."""
import os
from pathlib import Path
import ssl


def validate_tls(cert, key):
    if bool(cert) != bool(key):
        raise ValueError('provide both --tls-cert and --tls-key')
    if not cert:
        return {}
    cert, key = Path(cert), Path(key)
    for file in (cert, key):
        if file.is_symlink() or not file.is_file():
            raise ValueError('TLS files must be regular files, not symlinks')
    if key.stat().st_uid != os.getuid() or key.stat().st_mode & 0o077:
        raise ValueError('TLS private key must be owner-only and owned by the operator')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(cert), str(key))
    return {'ssl_certfile': str(cert.resolve()), 'ssl_keyfile': str(key.resolve())}
