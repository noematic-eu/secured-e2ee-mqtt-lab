#!/usr/bin/env python3
"""
e2ee.py — Helpers crypto partagés (ECDH P-256 + AES-256-GCM).

Utilisé par pub-test.py (--e2ee), sub-decrypt.py, et l'enrôlement.

Protocole v1 :
  - Échange de clés : ECDH SECP256R1 (P-256)
  - KDF : HKDF-SHA256(shared_secret, info=contexte identités+usage) → AES-256
  - Chiffrement : AES-256-GCM, nonce 96 bits aléatoire
  - AAD : lier topic + identité émetteur + seq (anti-redirection, anti-rejeu)
  - Fil réseau : seq(4) ‖ nonce(12) ‖ ciphertext+tag (tag GCM 16 o.)

Clés privées : DER PKCS8, stockées localement dans keys/{id}.priv
Clés publiques : DER SubjectPublicKeyInfo, enregistrées via l'agent d'identité
"""
from __future__ import annotations

import base64
import json
import os
import urllib.request
import urllib.error
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# --- Constantes ---
WIRE_VERSION = 1
SEQ_LEN = 4       # 4 octets (message ID aléatoire, anti-rejeu)
NONCE_LEN = 12    # 96 bits (GCM)
AAD_PREFIX = f"v{WIRE_VERSION}"  # préfixe de version dans l'AAD

# --- Chemins ---
KEYS_DIR = Path(os.environ.get("E2EE_KEYS_DIR", str(Path(__file__).parent.parent / "keys")))
DEFAULT_ENROLL_URL = os.environ.get("ENROLL_URL", "http://localhost:8443")


def _ensure_keys_dir() -> Path:
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    return KEYS_DIR


# ---------------------------------------------------------------------------
# Clés
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# KDF — HKDF-SHA256 avec contexte
# ---------------------------------------------------------------------------

def derive_aes_key(priv_der: bytes, peer_pub_der: bytes,
                   id_a: str, id_b: str) -> bytes:
    """Dérive la clé AES-256 via HKDF-SHA256.

    Le contexte (info) lie la clé au couple d'identités et à l'usage,
    ce qui empêche la réutilisation de la clé dans un autre contexte.
    Les IDs sont triés pour que les deux côtés dérivent la même clé.
    """
    priv_key = serialization.load_der_private_key(priv_der, password=None)
    peer_pub = serialization.load_der_public_key(peer_pub_der)
    shared = priv_key.exchange(ec.ECDH(), peer_pub)

    ids = tuple(sorted([id_a, id_b]))
    info = f"m2dfs-e2ee|{ids[0]}|{ids[1]}|aes-256-gcm|v{WIRE_VERSION}".encode()

    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=info,
    )
    return hkdf.derive(shared)


# ---------------------------------------------------------------------------
# Chiffrement / Déchiffrement (AES-256-GCM + AAD)
# ---------------------------------------------------------------------------

def build_aad(topic: str, sender_id: str, seq: bytes) -> bytes:
    """Construit l'AAD : lier le message au topic, à l'émetteur, et au seq.

    Prévents :
      - redirection vers un autre topic (AAD contient le topic)
      - rejeu cross-device (AAD contient l'identité de l'émetteur)
      - rejeu simple (AAD contient le seq, détectable côté receveur)
    """
    return f"{AAD_PREFIX}|{topic}|{sender_id}|{seq.hex()}".encode()


def encrypt(aes_key: bytes, plaintext: bytes, aad: bytes | None = None) -> bytes:
    """Chiffre avec AES-256-GCM. Retourne seq(4) ‖ nonce(12) ‖ ciphertext+tag(16).

    Le seq est un ID de message aléatoire (en clair, mais authentifié via l'AAD).
    """
    seq = os.urandom(SEQ_LEN)
    nonce = os.urandom(NONCE_LEN)
    ct = AESGCM(aes_key).encrypt(nonce, plaintext, associated_data=aad)
    return seq + nonce + ct


def decrypt(aes_key: bytes, wire: bytes, aad: bytes | None = None) -> bytes:
    """Déchiffre seq(4) ‖ nonce(12) ‖ ciphertext+tag. Retourne le plaintext.

    Lève InvalidTag si l'AAD ne correspond pas (rejeu / redirection détectée).
    """
    from cryptography.exceptions import InvalidTag

    seq = wire[:SEQ_LEN]
    nonce = wire[SEQ_LEN:SEQ_LEN + NONCE_LEN]
    ct = wire[SEQ_LEN + NONCE_LEN:]

    if aad is None:
        # Compatibilité : si l'AAD n'est pas fournie, on reconstruit avec le seq
        # (le caller doit fournir l'AAD complète via session_decrypt)
        raise ValueError("AAD requise pour le déchiffrement (v1)")

    try:
        return AESGCM(aes_key).decrypt(nonce, ct, associated_data=aad)
    except InvalidTag as e:
        raise ValueError(
            f"Déchiffrement échoué (AAD invalide ou rejeu détecté). "
            f"seq={seq.hex()}, topic/identité peuvent ne pas correspondre."
        ) from e


# ---------------------------------------------------------------------------
# Enrôlement (HTTP)
# ---------------------------------------------------------------------------

def register_pubkey(enroll_url: str, device_id: str, pub_der: bytes,
                    force: bool = False) -> None:
    """Enregistre la clé publique d'un device auprès de l'agent d'identité.

    Si le device est déjà enrôlé avec la même clé, continue silencieusement.
    Si c'est une clé différente, l'agent renvoie 409 et on lève une erreur.
    """
    url = f"{enroll_url}/devices/{device_id}/pubkey"
    if force:
        url += "?force=true"
    body = json.dumps({"pubkey": base64.b64encode(pub_der).decode()}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 409:
            # Déjà enrôlé : vérifier si c'est la même clé (récupérer et comparer)
            existing = fetch_pubkey(enroll_url, device_id)
            if existing == pub_der:
                return  # même clé, pas de problème
            raise RuntimeError(
                f"Device '{device_id}' déjà enrôlé avec une clé DIFFÉRENTE. "
                f"⚠ Substitution de clé détectée !\n"
                f"  Pour réenrôler avec VOTRE clé : "
                f"rm enrollment_service/state.json (entrée {device_id}) + keys/{device_id}.priv\n"
                f"  ou POST ?force=true"
            ) from e
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


# ---------------------------------------------------------------------------
# Session (convenience)
# ---------------------------------------------------------------------------

def session_encrypt(device_id: str, peer_id: str, plaintext: bytes,
                    topic: str, enroll_url: str = DEFAULT_ENROLL_URL) -> bytes:
    """Chiffre un message pour peer_id, lié au topic et à l'identité de device_id.

    Args:
        device_id: identité de l'émetteur (ex: "sensor-1")
        peer_id: identité du destinataire (ex: "app-1")
        plaintext: données à chiffrer (JSON encodé)
        topic: topic MQTT de destination (lié via AAD)
        enroll_url: URL de l'agent d'identité

    Retourne le fil réseau : seq(4) ‖ nonce(12) ‖ ciphertext+tag
    """
    priv_der, _ = generate_keypair(device_id)
    peer_pub = fetch_pubkey(enroll_url, peer_id)
    key = derive_aes_key(priv_der, peer_pub, device_id, peer_id)
    return _encrypt_with_aad(key, plaintext, topic, device_id)


def session_decrypt(device_id: str, peer_id: str, wire: bytes,
                    topic: str, enroll_url: str = DEFAULT_ENROLL_URL) -> bytes:
    """Déchiffre un message reçu de device_id, vérifie l'AAD (topic + identité).

    Args:
        device_id: identité de l'émetteur attendu (ex: "sensor-1")
        peer_id: identité du receveur (ex: "app-1")
        wire: fil réseau reçu (seq ‖ nonce ‖ ct+tag)
        topic: topic MQTT d'où le message a été reçu (vérifié via AAD)
        enroll_url: URL de l'agent d'identité

    Retourne le plaintext. Lève ValueError si AAD invalide (rejeu/redirection).
    """
    priv_der, _ = generate_keypair(peer_id)
    peer_pub = fetch_pubkey(enroll_url, device_id)
    key = derive_aes_key(priv_der, peer_pub, peer_id, device_id)
    return _decrypt_with_aad(key, wire, topic, device_id)


def _encrypt_with_aad(aes_key: bytes, plaintext: bytes,
                      topic: str, sender_id: str) -> bytes:
    """Chiffre avec AAD construite depuis topic + sender + seq."""
    seq = os.urandom(SEQ_LEN)
    aad = build_aad(topic, sender_id, seq)
    nonce = os.urandom(NONCE_LEN)
    ct = AESGCM(aes_key).encrypt(nonce, plaintext, associated_data=aad)
    return seq + nonce + ct


def _decrypt_with_aad(aes_key: bytes, wire: bytes,
                      topic: str, sender_id: str) -> bytes:
    """Déchiffre en reconstruisant l'AAD depuis topic + sender + seq (du wire)."""
    from cryptography.exceptions import InvalidTag

    seq = wire[:SEQ_LEN]
    nonce = wire[SEQ_LEN:SEQ_LEN + NONCE_LEN]
    ct = wire[SEQ_LEN + NONCE_LEN:]

    aad = build_aad(topic, sender_id, seq)

    try:
        return AESGCM(aes_key).decrypt(nonce, ct, associated_data=aad)
    except InvalidTag as e:
        raise ValueError(
            f"Déchiffrement échoué : AAD invalide.\n"
            f"  topic attendu : {topic}\n"
            f"  émetteur attendu : {sender_id}\n"
            f"  seq : {seq.hex()}\n"
            f"  → Possible rejeu, redirection de topic, ou mauvaise clé."
        ) from e
