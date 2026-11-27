#!/usr/bin/env python3
"""
enrollment_service/app.py — Agent d'identité (enrôlement de clés publiques).

Port 8443. Persistance dans state.json (à côté du script).

Endpoints :
  POST /devices/<id>/pubkey  — enregistrer la clé publique d'un device
  GET  /devices/<id>/pubkey  — récupérer la clé publique d'un device
  GET  /health               — health check

Le service stocke uniquement les clés PUBLIQUES (DER SubjectPublicKeyInfo,
base64). Les clés privées restent locales sur chaque machine.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from flask import Flask, jsonify, request

app = Flask(__name__)

STATE_FILE = Path(os.environ.get("ENROLL_STATE", str(Path(__file__).parent / "state.json")))


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


@app.route("/devices/<device_id>/pubkey", methods=["POST"])
def register_pubkey(device_id: str):
    """Enregistrer la clé publique d'un device.

    Refuse l'écrasement d'une clé déjà enrôlée (409) sauf ?force=true.
    Cela empêche un client anonyme de substituer sa propre clé sous un id existant.
    """
    body = request.get_json(silent=True)
    if not body or "pubkey" not in body:
        return jsonify({"error": "body must contain 'pubkey' (base64 DER)"}), 400

    pubkey_b64 = body["pubkey"]
    # Validation minimale : doit être du base64 décodable
    try:
        raw = base64.b64decode(pubkey_b64)
    except Exception:
        return jsonify({"error": "pubkey is not valid base64"}), 400
    if len(raw) < 20:
        return jsonify({"error": "pubkey too short (expected DER SubjectPublicKeyInfo)"}), 400

    force = request.args.get("force", "false").lower() == "true"
    state = _load_state()

    if device_id in state and not force:
        return jsonify({
            "error": f"device '{device_id}' déjà enrôlé",
            "hint": "Pour réenrôler : effacer state.json + keys/{device_id}.priv, ou passer ?force=true",
        }), 409

    state[device_id] = {"pubkey": pubkey_b64}
    _save_state(state)
    action = "ré-enrôlé (force)" if force else "enrôlé"
    print(f"  [enroll] {device_id} → pubkey {action} ({len(raw)} o.)")
    return jsonify({"status": "ok", "device": device_id}), 201


@app.route("/devices/<device_id>/pubkey", methods=["GET"])
def get_pubkey(device_id: str):
    """Récupérer la clé publique d'un device."""
    state = _load_state()
    if device_id not in state:
        return jsonify({"error": f"device '{device_id}' not enrolled"}), 404
    return jsonify({"device": device_id, "pubkey": state[device_id]["pubkey"]})


@app.route("/devices", methods=["GET"])
def list_devices():
    """Liste des devices enrôlés."""
    state = _load_state()
    return jsonify({"devices": list(state.keys())})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("ENROLL_PORT", "8443"))
    print(f"Agent d'identité (enrôlement) — port {port}")
    print(f"  Persistance : {STATE_FILE}")
    print(f"  POST /devices/<id>/pubkey")
    print(f"  GET  /devices/<id>/pubkey")
    app.run(host="0.0.0.0", port=port, debug=False)
