"""Local TLS certificate lifecycle management.

Generates and maintains a private CA and a localhost leaf certificate using
the cryptography package - no external tools required.

Flow
----
1. Ensure the CA exists and is not near expiry. Generate (or regenerate) if needed.
2. Ensure the leaf cert exists and is not near expiry. Generate (or regenerate) if
   needed, signing against the current CA.

Returns the CA path for the SignalRGB cacert patching step.
"""

from __future__ import annotations

import ipaddress
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .config import CA_FILE, CA_KEY_FILE, CERT_FILE, CERTS_DIR, KEY_FILE

logger = logging.getLogger("huesignal")

_CA_VALIDITY_DAYS = 10 * 365  # ~10 years
_LEAF_VALIDITY_DAYS = 2 * 365  # ~2 years
_RENEW_THRESHOLD_DAYS = 30


class CertError(Exception):
    """Raised when local certificate management fails fatally."""


def ensure_local_certs() -> Path:
    """Ensure the local CA and leaf TLS certificates are present and not near expiry.

    Installs the CA into the Windows user trust store when (re)generated.

    Returns:
        Path to the CA certificate file (for SignalRGB cacert patching).

    Raises:
        CertError: if certificate generation fails.
    """
    _ensure_ca()
    _ensure_leaf_cert()
    return CA_FILE


# ------------------------------------------------------------------
# CA management
# ------------------------------------------------------------------


def _ensure_ca() -> None:
    """Ensure the CA cert and key exist and are not near expiry."""
    if CA_FILE.is_file() and CA_KEY_FILE.is_file():
        expiry = _read_cert_expiry(CA_FILE)
        if expiry is None:
            logger.warning(
                "[certs] Could not read CA expiry - regenerating to be safe."
            )
        else:
            days_remaining = (expiry - datetime.now(timezone.utc)).days
            if days_remaining > _RENEW_THRESHOLD_DAYS:
                logger.info("[certs] CA valid for %d more day(s).", days_remaining)
                return
            logger.info(
                "[certs] CA expires in %d day(s) - regenerating...", days_remaining
            )
    else:
        logger.info("[certs] CA not found - generating...")

    _generate_ca()


def _generate_ca() -> None:
    CERTS_DIR.mkdir(parents=True, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "HueSignal Local CA")])
    now = datetime.now(timezone.utc)

    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=_CA_VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .sign(key, hashes.SHA256())
    )

    CA_KEY_FILE.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    CA_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    logger.info("[certs] CA certificate generated.")


# ------------------------------------------------------------------
# Leaf certificate management
# ------------------------------------------------------------------


def _ensure_leaf_cert() -> None:
    """Ensure the leaf cert and key exist and are not near expiry."""
    if CERT_FILE.is_file() and KEY_FILE.is_file():
        expiry = _read_cert_expiry(CERT_FILE)
        if expiry is None:
            logger.warning(
                "[certs] Could not read leaf cert expiry - regenerating to be safe."
            )
        else:
            days_remaining = (expiry - datetime.now(timezone.utc)).days
            if days_remaining > _RENEW_THRESHOLD_DAYS:
                logger.info(
                    "[certs] Leaf cert valid for %d more day(s).", days_remaining
                )
                return
            logger.info(
                "[certs] Leaf cert expires in %d day(s) - renewing...", days_remaining
            )
    else:
        logger.info("[certs] Leaf cert not found - generating...")

    _generate_leaf_cert()


def _generate_leaf_cert() -> None:
    try:
        ca_key = serialization.load_pem_private_key(
            CA_KEY_FILE.read_bytes(), password=None
        )
        ca_cert = x509.load_pem_x509_certificate(CA_FILE.read_bytes())
    except Exception as exc:
        raise CertError(
            f"Failed to load CA files for leaf cert signing.\n\n{exc}"
        ) from exc

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)

    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=_LEAF_VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                    x509.DNSName("localhost"),
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_cert.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    KEY_FILE.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    logger.info("[certs] Leaf certificate generated.")


# ------------------------------------------------------------------
# Shared helper
# ------------------------------------------------------------------


def _read_cert_expiry(path: Path) -> datetime | None:
    """Return the certificate's not-after date in UTC, or None on any failure."""
    try:
        cert = x509.load_pem_x509_certificate(path.read_bytes())
        # not_valid_after_utc added in cryptography 42.0; fall back for older installs.
        try:
            return cert.not_valid_after_utc
        except AttributeError:
            return cert.not_valid_after.replace(tzinfo=timezone.utc)  # type: ignore[attr-defined]
    except Exception as exc:
        logger.debug("[certs] Failed to parse %s: %s", path.name, exc)
        return None
