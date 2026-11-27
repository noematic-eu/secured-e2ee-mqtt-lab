#!/usr/bin/env python3
"""
sub-decrypt.py — Côté app : s'abonne, déchiffre, affiche le clair.

Usage :
    python3 sub-decrypt.py --app-id app-1 --device sensor-1
    python3 sub-decrypt.py --app-id app-1 --device sensor-1 --count 3
    python3 sub-decrypt.py --app-id app-1 --device sensor-1 --once  # attend 1 msg puis quitte

Flux :
  1. Génère/charge la clé privée de l'app (keys/{app-id}.priv)
  2. Enregistre sa pubkey auprès de l'agent d'identité
  3. S'abonne au topic télémétrie du device
  4. À chaque message reçu : déchiffre (ECDH avec pubkey du device) → affiche JSON clair
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid

import paho.mqtt.client as mqtt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e2ee


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Subscribe + déchiffrement E2EE")
    p.add_argument("--app-id", default="app-1", help="ID de l'app (destinataire)")
    p.add_argument("--device", default="sensor-1", help="Device source")
    p.add_argument("--prefix", default="promo/equipe-a", help="Préfixe d'équipe")
    p.add_argument("--broker", default="localhost", help="Adresse du broker MQTT")
    p.add_argument("--port", type=int, default=11883, help="Port MQTT")
    p.add_argument("--enroll-url", default="http://localhost:8443", help="URL agent d'identité")
    p.add_argument("--count", type=int, default=0, help="Arrêter après N messages (0 = infini)")
    p.add_argument("--timeout", type=float, default=30.0, help="Timeout attente 1er message (s)")
    return p.parse_args()


def on_message(client, userdata, msg):
    """Reçoit un message MQTT, le déchiffre, affiche le clair."""
    count = userdata.get("count", 0)
    max_count = userdata.get("max_count", 0)
    device_id = userdata["device_id"]
    app_id = userdata["app_id"]
    enroll_url = userdata["enroll_url"]

    try:
        plaintext = e2ee.session_decrypt(app_id, device_id, msg.payload, enroll_url)
        data = json.loads(plaintext)
        count += 1
        print(f"  [{count}] {msg.topic}")
        print(f"       {json.dumps(data, indent=2)}")
        userdata["count"] = count

        if max_count and count >= max_count:
            userdata["done"] = True
    except Exception as e:
        print(f"  [ERREUR] Déchiffrement échoué : {e}", file=sys.stderr)
        print(f"           payload raw ({len(msg.payload)} o.) : {msg.payload[:40].hex()}…", file=sys.stderr)


def main() -> None:
    args = parse_args()
    topic = f"{args.prefix}/devices/{args.device}/telemetry"

    # 1. Générer/charger la clé de l'app
    priv_der, pub_der = e2ee.generate_keypair(args.app_id)
    print(f"[e2ee] Clé {args.app_id} prête ({e2ee.KEYS_DIR / (args.app_id + '.priv')})")

    # 2. Enregistrer la pubkey
    e2ee.register_pubkey(args.enroll_url, args.app_id, pub_der)
    print(f"[e2ee] {args.app_id} enrôlé → {args.enroll_url}")

    # 3. Vérifier que la pubkey du device est dispo (sinon, attendre)
    deadline = time.time() + 5
    while True:
        try:
            dev_pub = e2ee.fetch_pubkey(args.enroll_url, args.device)
            print(f"[e2ee] pubkey de {args.device} récupérée ({len(dev_pub)} o.)")
            break
        except RuntimeError:
            if time.time() > deadline:
                print(f"[e2ee] ⚠ Device {args.device} pas encore enrôlé.", file=sys.stderr)
                print(f"[e2ee] Lancer pub-test.py --e2ee d'abord (ou en parallèle).", file=sys.stderr)
                sys.exit(1)
            time.sleep(0.5)

    # 4. S'abonner
    client_id = f"sub-{args.app_id}-{uuid.uuid4().hex[:6]}"
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except AttributeError:
        client = mqtt.Client(client_id=client_id)

    userdata = {
        "device_id": args.device,
        "app_id": args.app_id,
        "enroll_url": args.enroll_url,
        "count": 0,
        "max_count": args.count,
        "done": False,
    }
    client.user_data_set(userdata)
    client.on_message = on_message
    client.connect(args.broker, args.port, keepalive=60)
    client.subscribe(topic, qos=1)
    client.loop_start()
    print(f"[sub] Abonné à {topic}")
    print(f"[sub] En attente de messages chiffrés… (Ctrl+C pour arrêter)")
    if args.count:
        print(f"[sub] Arrêt auto après {args.count} message(s)")

    try:
        while not userdata["done"]:
            if time.time() > deadline + args.timeout and userdata["count"] == 0:
                print("\n[timeout] Aucun message reçu — est-ce que pub-test.py --e2ee tourne ?", file=sys.stderr)
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
        client.disconnect()
        print(f"\n[fin] {userdata['count']} message(s) déchiffré(s)")


if __name__ == "__main__":
    main()
