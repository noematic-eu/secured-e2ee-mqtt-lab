#!/usr/bin/env python3
"""
pub-test.py — Simule un device qui publie télémétrie + supervision.

Usage :
    python3 pub-test.py --prefix promo/equipe-a --device sensor-1
    python3 pub-test.py --prefix promo/equipe-a --device sensor-1 --once 25.3
    python3 pub-test.py --prefix promo/equipe-a --device sensor-1 --interval 1
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import signal
import sys
import time
import uuid
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e2ee


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Simule un device → MQTT (télémétrie + supervision)")
    p.add_argument("--prefix", default="promo/equipe-a", help="Préfixe d'équipe")
    p.add_argument("--device", default="sensor-1", help="Nom du device")
    p.add_argument("--broker", default="localhost", help="Adresse du broker MQTT")
    p.add_argument("--port", type=int, default=11883, help="Port MQTT")
    p.add_argument("--interval", type=float, default=2.0, help="Intervalle télémétrie (s)")
    p.add_argument("--start", type=float, default=21.5, help="Température initiale (°C)")
    p.add_argument("--once", type=float, metavar="TEMP_C", help="Publie une seule fois et quitte")
    # --- E2EE ---
    p.add_argument("--e2ee", action="store_true", help="Chiffrer avant publish (ECDH + AES-256-GCM)")
    p.add_argument("--app-id", default="app-1", help="ID du destinataire (pour E2EE)")
    p.add_argument("--enroll-url", default="http://localhost:8443", help="URL agent d'identité")
    return p.parse_args()


def make_client(broker: str, port: int, client_id: str) -> mqtt.Client:
    full_id = f"{client_id}-{uuid.uuid4().hex[:6]}"
    try:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=full_id)
    except AttributeError:
        c = mqtt.Client(client_id=full_id)
    c.connect(broker, port, keepalive=60)
    c.loop_start()
    return c


def get_metrics() -> dict:
    """Lire les métriques système réelles (CPU, mémoire) — fallback si psutil absent."""
    metrics = {
        "cpu_pct": round(random.uniform(10, 45), 1),
        "mem_mb": round(random.uniform(40, 80), 1),
        "broker_up": 1,
    }
    # Essaier de lire les vrais chiffres
    try:
        with open("/proc/stat") as f:
            first = f.readline().split()
        time.sleep(0.1)
        with open("/proc/stat") as f:
            second = f.readline().split()
        idle1 = int(first[4])
        idle2 = int(second[4])
        total1 = sum(int(x) for x in first[1:8])
        total2 = sum(int(x) for x in second[1:8])
        cpu = 100.0 * (1 - (idle2 - idle1) / max(1, total2 - total1))
        metrics["cpu_pct"] = round(cpu, 1)
    except Exception:
        pass

    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    metrics["mem_mb"] = round(int(line.split()[1]) / 1024, 1)
                    break
    except Exception:
        pass

    return metrics


def main() -> None:
    args = parse_args()
    topic_tel = f"{args.prefix}/devices/{args.device}/telemetry"
    topic_sup = f"{args.prefix}/devices/{args.device}/supervision"
    client = make_client(args.broker, args.port, f"simu-{args.device}")

    msg_count = 0
    t0 = time.time()

    # --- E2EE setup ---
    if args.e2ee:
        priv_der, pub_der = e2ee.generate_keypair(args.device)
        e2ee.register_pubkey(args.enroll_url, args.device, pub_der)
        print(f"[e2ee] {args.device} enrôlé → {args.enroll_url}")
        try:
            app_pub = e2ee.fetch_pubkey(args.enroll_url, args.app_id)
            print(f"[e2ee] pubkey de {args.app_id} récupérée ({len(app_pub)} o.)")
        except RuntimeError as e:
            print(f"[e2ee] ⚠ {e}")
            print(f"[e2ee] Lancer sub-decrypt.py d'abord pour enrôler {args.app_id}")
            sys.exit(1)

    def _publish(topic: str, payload_dict: dict, retain: bool = False) -> None:
        """Publie (chiffré si --e2ee, sinon JSON clair)."""
        if args.e2ee:
            wire = e2ee.session_encrypt(args.device, args.app_id,
                                        json.dumps(payload_dict).encode(), args.enroll_url)
            client.publish(topic, wire, qos=1, retain=retain)
        else:
            client.publish(topic, json.dumps(payload_dict), qos=1, retain=retain)

    # --- Mode one-shot ---
    if args.once is not None:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "device_id": args.device,
            "temp_c": args.once,
            "alert": args.once >= 28.0,
        }
        _publish(topic_tel, payload, retain=True)

        sup = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "device_id": args.device,
            "msg_count": 1,
            "uptime_s": 0,
            **get_metrics(),
        }
        _publish(topic_sup, sup)

        client.loop_stop()
        client.disconnect()
        if args.e2ee:
            print(f"PUB (chiffré) {topic_tel}  temp_c={args.once}")
            print(f"PUB (chiffré) {topic_sup}")
        else:
            print(f"PUB {topic_tel}  {json.dumps(payload)}")
            print(f"PUB {topic_sup}  {json.dumps(sup)}")
        return

    # --- Mode continu ---
    temp = args.start
    amp = 1.5
    running = True

    def shutdown(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print(f"Simu {args.device}")
    print(f"  télémétrie : {topic_tel}")
    print(f"  supervision: {topic_sup}")
    print(f"  intervalle : {args.interval}s  (Ctrl+C pour arrêter)")

    while running:
        elapsed = time.time() - t0
        msg_count += 1
        temp = (args.start + 3.0 * math.sin(elapsed / 30.0)
                + amp * math.sin(elapsed / 5.0)
                + random.uniform(-0.3, 0.3))

        # --- Télémétrie ---
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "device_id": args.device,
            "temp_c": round(temp, 1),
            "alert": temp >= 28.0,
        }
        _publish(topic_tel, payload)

        # --- Supervision (tous les 5 messages) ---
        if msg_count % 5 == 1:
            sup = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "device_id": args.device,
                "msg_count": msg_count,
                "uptime_s": round(elapsed, 1),
                **get_metrics(),
            }
            _publish(topic_sup, sup)

        print(f"  [{msg_count:>3}] {payload['temp_c']:>5.1f} °C  alert={payload['alert']}")
        time.sleep(args.interval)

    client.loop_stop()
    client.disconnect()
    print(f"Arrêt. {msg_count} messages publiés.")


if __name__ == "__main__":
    main()
