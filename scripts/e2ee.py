#!/usr/bin/env python3
"""
e2ee.py — Helpers crypto partagés (ECDH P-256 + AES-256-GCM).

Utilisé par pub-test.py (--e2ee), sub-decrypt.py, et l'enrôlement.

Protocole :
  - Échange de clés : ECDH SECP256R1 (P-256)
  - Clé symétrique : SHA-256(shared_secret) → AES-256
  - Chiffrement : AES-256-GCM, nonce 96 bits aléatoire
  - Fil réseau : nonce(12) ‖ ciphertext+tag (ciphertext inclut le tag GCM de 16 o.)

Clés privées : DER PKCS8, stockées localement dans keys/{id}.priv
Clés publiques : DER SubjectPublicKeyInfo, enregistrées via l'agent d'identité
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.request
import urllib.error
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# --- Chemins ---
KEYS_DIR = Path(os.environ.get("E2EE_KEYS_DIR", str(Path(__file__).parent.parent / "keys")))
DEFAULT_ENROLL_URL = os.environ.get("ENROLL_URL", "http://localhost:8443")


def _ensure_keys_dir() -> Path:
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    return KEYS_DIR


def generate_keypair(device_id: str) -> tuple[bytes, bytes]:
    """Génère (ou recharge) une paire de clés ECDH P-256 pour device_id.

    Retourne (private_key_der, public_key_der).
    La clé privée est persistée dans keys/{device_id}.priv
    """
    priv_path = _ensure_keys_dir() / f"{device_id}.priv"

    if priv_path.exists():
        priv_der = priv_path.read_bytes()
        priv_key = serialization.load_der_private_key(priv_der, password=None)
    else:
        priv_key = ec.generate_private_key(ec.SECP256R1())
        priv_der = priv_key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        priv_path.write_bytes(priv_der)
        priv_path.chmod(0o600)

    pub_der = priv_key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv_der, pub_der


def load_privkey(device_id: str) -> bytes:
    """Charger la clé privée DER PKCS8 d'un device (doit exister)."""
    priv_path = KEYS_DIR / f"{device_id}.priv"
    if not priv_path.exists():
        raise FileNotFoundError(f"Clé privée introuvable : {priv_path} (lancer generate_keypair d'abord)")
    return priv_path.read_bytes()


def derive_aes_key(priv_der: bytes, peer_pub_der: bytes) -> bytes:
    """Dérive la clé AES-256 = SHA-256(shared_secret ECDH)."""
    priv_key = serialization.load_der_private_key(priv_der, password=None)
    peer_pub = serialization.load_der_public_key(peer_pub_der)
    shared = priv_key.exchange(ec.ECDH(), peer_pub)
    return hashlib.sha256(shared).digest()


def encrypt(aes_key: bytes, plaintext: bytes) -> bytes:
    """Chiffre avec AES-256-GCM. Retourne nonce(12) ‖ ciphertext+tag(16)."""
    nonce = os.urandom(12)
    ct = AESGCM(aes_key).encrypt(nonce, plaintext, associated_data=None)
    return nonce + ct  # ct inclut déjà le tag GCM (16 octets)


def decrypt(aes_key: bytes, wire: bytes) -> bytes:
    """Déchiffre nonce(12) ‖ ciphertext+tag. Retourne le plaintext."""
    nonce = wire[:12]
    ct = wire[12:]
    return AESGCM(aes_key).decrypt(nonce, ct, associated_data=None)


# --- Enrôlement (HTTP) ---

def register_pubkey(enroll_url: str, device_id: str, pub_der: bytes) -> None:
    """Enregistre la clé publique d'un device auprès de l'agent d'identité."""
    url = f"{enroll_url}/devices/{device_id}/pubkey"
    body = json.dumps({"pubkey": base64.b64encode(pub_der).decode()}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Enrôlement échoué : POST {url} → {e.code} {e.reason}") from e


def fetch_pubkey(enroll_url: str, device_id: str) -> bytes:
    """Récupère la clé publique d'un device depuis l'agent d'identité."""
    url = f"{enroll_url}/devices/{device_id}/pubkey"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
            return base64.b64decode(data["pubkey"])
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Fetch pubkey échoué : GET {url} → {e.code} (device enrôlé ?)") from e


def session_encrypt(device_id: str, peer_id: str, plaintext: bytes,
                    enroll_url: str = DEFAULT_ENROLL_URL) -> bytes:
    """Convenience : génère/charge la clé device, fetch pub peer, chiffre.

    Retourne le fil réseau (nonce ‖ ct+tag).
    """
    priv_der, _ = generate_keypair(device_id)
    peer_pub = fetch_pubkey(enroll_url, peer_id)
    key = derive_aes_key(priv_der, peer_pub)
    return encrypt(key, plaintext)


def session_decrypt(device_id: str, peer_id: str, wire: bytes,
                    enroll_url: str = DEFAULT_ENROLL_URL) -> bytes:
    """Convenience : génère/charge la clé device, fetch pub peer, déchiffre.

    Retourne le plaintext.
    """
    priv_der, _ = generate_keypair(device_id)
    peer_pub = fetch_pubkey(enroll_url, peer_id)
    key = derive_aes_key(priv_der, peer_pub)
    return decrypt(key, wire)
