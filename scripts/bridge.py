#!/usr/bin/env python3
"""
bridge.py — MQTT → InfluxDB (télémétrie + supervision)

S'abonne aux topics télémétrie ET supervision de l'équipe,
écrit dans InfluxDB (measurements: telemetry, events, supervision).

Usage :
    python3 bridge.py --prefix promo/equipe-a --device sensor-1
"""

import argparse
import json
import logging
import signal
import sys
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bridge")

# --- InfluxDB ---
INFLUX_URL = "http://localhost:18086"
INFLUX_TOKEN = "m2dfs-token"
INFLUX_ORG = "m2dfs"
INFLUX_BUCKET = "m2dfs"


def parse_args():
    p = argparse.ArgumentParser(description="MQTT → InfluxDB bridge (télémétrie + supervision)")
    p.add_argument("--prefix", default="promo/equipe-a", help="Préfixe d'équipe")
    p.add_argument("--device", default="sensor-1", help="Nom du device")
    p.add_argument("--broker", default="localhost", help="Adresse du broker MQTT")
    p.add_argument("--port", type=int, default=11883, help="Port MQTT")
    p.add_argument("--threshold", type=float, default=28.0, help="Seuil d'alerte (°C)")
    return p.parse_args()


def make_client(args) -> InfluxDBClient:
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    log.info("InfluxDB connecté : %s (bucket %s)", INFLUX_URL, INFLUX_BUCKET)
    return client


def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        topics = [
            f"{args.prefix}/devices/{args.device}/telemetry",
            f"{args.prefix}/devices/{args.device}/supervision",
        ]
        for t in topics:
            client.subscribe(t, qos=1)
            log.info("Abonné à : %s", t)
    else:
        log.error("Échec de connexion MQTT, rc=%d", rc)


def on_disconnect(client, userdata, disconnect_flags, reason_code=0, properties=None):
    if reason_code != 0:
        log.warning("Déconnexion MQTT inattendue (rc=%s) — reconnexion auto…", reason_code)


def write_telemetry(payload):
    """Écrire une mesure de télémétrie dans InfluxDB."""
    temp_c = payload.get("temp_c")
    if temp_c is None:
        log.warning("Pas de temp_c dans le payload : %s", payload)
        return

    ts = payload.get("ts") or datetime.now(timezone.utc).isoformat()

    pt = (
        Point("telemetry")
        .tag("device", args.device)
        .tag("prefix", args.prefix)
        .field("temp_c", float(temp_c))
        .time(ts)
    )
    write_api.write(bucket=INFLUX_BUCKET, record=pt)

    # Alert event
    if payload.get("alert", False) or float(temp_c) >= args.threshold:
        ev = (
            Point("events")
            .tag("device", args.device)
            .tag("type", "threshold")
            .field("temp_c", float(temp_c))
            .field("threshold", args.threshold)
            .time(ts)
        )
        write_api.write(bucket=INFLUX_BUCKET, record=ev)
        log.warning("ALERTE : %.1f °C >= seuil %.1f °C", temp_c, args.threshold)

    log.info("temp_c=%.1f °C → InfluxDB (device=%s)", temp_c, args.device)


def write_supervision(payload):
    """Écrire des métriques de supervision dans InfluxDB."""
    ts = payload.get("ts") or datetime.now(timezone.utc).isoformat()

    pt = (
        Point("supervision")
        .tag("device", args.device)
        .tag("prefix", args.prefix)
    )

    # Champs optionnels — on n'écrit que ce qui est présent
    fields = {
        "cpu_pct": "cpu_pct",
        "mem_mb": "mem_mb",
        "msg_count": "msg_count",
        "uptime_s": "uptime_s",
        "broker_up": "broker_up",
    }
    added = {}
    for influx_field, payload_key in fields.items():
        if payload_key in payload:
            val = payload[payload_key]
            # InfluxDB : booléen → entier (1/0) pour éviter les conflits de type
            if isinstance(val, bool):
                val = int(val)
            pt = pt.field(influx_field, val)
            added[influx_field] = val

    if added:
        pt = pt.time(ts)
        write_api.write(bucket=INFLUX_BUCKET, record=pt)
        log.info("supervision → InfluxDB : %s", added)
    else:
        log.warning("Payload supervision vide : %s", payload)


def on_message(client, userdata, msg):
    """Routage : telemetry → write_telemetry, supervision → write_supervision."""
    try:
        payload = json.loads(msg.payload.decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        log.warning("Payload non JSON ignoré : %s", e)
        return

    if "/supervision" in msg.topic:
        try:
            write_supervision(payload)
        except Exception as e:
            log.error("Échec écriture supervision : %s", e)
    else:
        try:
            write_telemetry(payload)
        except Exception as e:
            log.error("Échec écriture InfluxDB : %s", e)


def main():
    global args, write_api
    args = parse_args()

    influx = make_client(args)
    write_api = influx.write_api(write_options=SYNCHRONOUS)

    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqtt_client.on_connect = on_connect
    mqtt_client.on_disconnect = on_disconnect
    mqtt_client.on_message = on_message
    mqtt_client.reconnect_delay_set(min_delay=1, max_delay=30)
    mqtt_client.connect(args.broker, args.port, keepalive=60)
    mqtt_client.loop_start()

    log.info("Bridge actif. Ctrl+C pour arrêter.")

    def shutdown(sig, frame):
        log.info("Arrêt…")
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        influx.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    signal.pause()


if __name__ == "__main__":
    main()
