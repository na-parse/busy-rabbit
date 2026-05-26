'''Self-signed TLS certificate helper.

In-process equivalent of:
    openssl req -x509 -newkey rsa:4096 -nodes \
        -out busy-rabbit-cert.pem -keyout busy-rabbit-key.pem -days 2920

Used only when ``[server].use_https`` is enabled. The cert/key pair lives
alongside the database file (see :meth:`Config.cert_path` / ``key_path``); the
location is deliberately not configurable. CA-signed certificates are out of
scope -- for trusted public HTTPS, run busy-rabbit behind a reverse proxy.
'''

from __future__ import annotations

import datetime
from pathlib import Path
from ssl import PROTOCOL_TLS_SERVER, SSLContext

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

# =============================================================================
# Certificate parameters
# =============================================================================

CERT_KEY_SIZE = 4096
CERT_VALID_DAYS = 2920  # ~8 years; long-lived by design for a local service.
CERT_ORG_NAME = 'busy-rabbit'
CERT_ORG_UNIT = 'SelfSignForFlask'


# =============================================================================
# Public API
# =============================================================================

def create_self_signed_cert(
    cert_path: Path,
    key_path: Path,
    hostname: str = 'localhost',
) -> None:
    '''Write a self-signed cert/key pair to the given paths.'''
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    _write_ssl_files(cert_path, key_path, hostname)
    key_path.chmod(0o600)
    cert_path.chmod(0o644)


def validate_ssl_files(cert_path: Path, key_path: Path) -> bool:
    '''Whether the cert/key files load as a usable TLS server pair.'''
    try:
        context = SSLContext(PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        return True
    except Exception:
        return False


def is_ssl_configured(cert_path: Path, key_path: Path) -> bool:
    '''Whether both files exist and form a valid TLS pair.'''
    if cert_path.is_file() and key_path.is_file():
        return validate_ssl_files(cert_path, key_path)
    return False


# =============================================================================
# Internal helpers
# =============================================================================

def _write_ssl_files(cert_path: Path, key_path: Path, hostname: str) -> None:
    '''Generate the RSA key and self-signed certificate and write them out.'''
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=CERT_KEY_SIZE,
    )

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, CERT_ORG_NAME),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, CERT_ORG_UNIT),
    ])

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=CERT_VALID_DAYS))
        .sign(private_key, hashes.SHA256())
    )

    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
