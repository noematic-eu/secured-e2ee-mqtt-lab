#!/usr/bin/env python3
"""
test-all.py — Vérification complète du stack lab-synthese, en un passage.

Pipeline testé :
    1. Services up      (mosquitto, influxdb, loki, objectstore)
    2. Pub → InfluxDB   (pub-test.py → bridge.py → InfluxDB)
    3. Logs dans Loki   (promtail → Loki, requête LogQL)
    4. Export store     (InfluxDB → CSV → SeaweedFS)

Un résumé OK/KO est affiché à la fin. Le script est idempotent et ne
laisse pas de processus orphelin (le bridge est arrêté en fin de test).

Usage :
    python3 test-all.py
    python3 test-all.py --device sensor-1 --prefix promo/equipe-a --temp 25.3
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone

import requests

# --- Stack (ports lab-synthese) ---
BROKER = os.environ.get("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "11883"))
INFLUX_URL = os.environ.get("INFLUX_URL", "http://localhost:18086")
INFLUX_TOKEN = os.environ.get("INFLUX_TOKEN", "m2dfs-token")
INFLUX_ORG = os.environ.get("INFLUX_ORG", "m2dfs")
INFLUX_BUCKET = os.environ.get("INFLUX_BUCKET", "m2dfs")
LOKI_URL = os.environ.get("LOKI_URL", "http://localhost:13100")
STORE_URL = os.environ.get("STORE_URL", "http://localhost:19000")
STORE_BUCKET = os.environ.get("STORE_BUCKET", "mesures")

HERE = os.path.dirname(os.path.abspath(__file__))
PUB_TEST = os.path.join(HERE, "pub-test.py")
BRIDGE = os.path.join(HERE, "bridge.py")

# Résultats du test
RESULTS: dict[str, tuple[bool, str]] = {}


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS[name] = (bool(ok), detail)
    mark = "OK " if ok else "KO "
    line = f"  [{mark}] {name}"
    if detail:
        line += f"  —  {detail}"
    print(line)
    return bool(ok)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Vérification complète du stack lab-synthese")
    p.add_argument("--prefix", default="promo/equipe-a")
    p.add_argument("--device", default="sensor-1")
    p.add_argument("--temp", type=float, default=25.3, help="Valeur publiée (°C)")
    p.add_argument("--timeout", type=float, default=20.0, help="Délai max attente InfluxDB (s)")
    return p.parse_args()


# ---------------------------------------------------------------------------
# 1. Services up
# ---------------------------------------------------------------------------
def tcp_up(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def step_services() -> None:
    print("\n1. Services up ?")
    check("mosquitto (MQTT)", tcp_up(BROKER, MQTT_PORT), f"{BROKER}:{MQTT_PORT}")

    try:
        r = requests.get(f"{INFLUX_URL}/ping", timeout=3)
        ok = r.status_code in (200, 204)
    except requests.RequestException as e:
        ok, detail = False, str(e)
    else:
        detail = f"HTTP {r.status_code}"
    check("influxdb", ok, detail)

    # /ready peut renvoyer 503 pendant les opérations d'index ; l'endpoint
    # labels est plus fiable pour vérifier que Loki sert des requêtes.
    try:
        r = requests.get(f"{LOKI_URL}/loki/api/v1/labels", timeout=5)
        ok = r.status_code == 200
        detail = f"labels={r.json().get('data', [])[:4]}"
    except requests.RequestException as e:
        ok, detail = False, str(e)
    check("loki", ok, detail)

    try:
        r = requests.get(f"{STORE_URL}/", timeout=3)
        ok = r.status_code == 200
        detail = f"health={r.status_code}"
    except requests.RequestException:
        ok, detail = False, "hors ligne"
    check("minio", ok, detail)


# ---------------------------------------------------------------------------
# 2. Pub → InfluxDB
# ---------------------------------------------------------------------------
def query_influx_flux(flux: str) -> str:
    r = requests.post(
        f"{INFLUX_URL}/api/v2/query?org={INFLUX_ORG}",
        headers={
            "Authorization": f"Token {INFLUX_TOKEN}",
            "Content-Type": "application/vnd.flux",
            "Accept": "application/csv",
        },
        data=flux,
        timeout=10,
    )
    r.raise_for_status()
    return r.text


def influx_has_measurement(measurement: str, device: str, since_s: float) -> bool:
    """Vrai si au moins un point de `measurement` (device) existe sur la fenêtre."""
    flux = (
        f'from(bucket:"{INFLUX_BUCKET}") '
        f'|> range(start:-{int(since_s)}s) '
        f'|> filter(fn:(r)=>r._measurement=="{measurement}") '
        f'|> filter(fn:(r)=>r.device=="{device}")'
    )
    try:
        text = query_influx_flux(flux)
    except requests.RequestException:
        return False
    # CSV InfluxDB : 1 ligne d'en-tête + ≥1 ligne de données ⇒ existe.
    lines = [l for l in text.strip().splitlines() if l.strip()]
    return len(lines) >= 2


def step_pub_influx(args) -> None:
    print("\n2. Pub → InfluxDB (via bridge) ?")
    topic = f"{args.prefix}/devices/{args.device}/telemetry"

    # On démarre le bridge pour la durée du test.
    bridge = subprocess.Popen(
        [sys.executable, BRIDGE, "--prefix", args.prefix, "--device", args.device],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        time.sleep(3)  # laisse le bridge s'abonner
        if bridge.poll() is not None:
            out = bridge.stdout.read() if bridge.stdout else ""
            check("bridge démarré", False, (out or "exit").strip()[-160:])
            return

        # Publie une mesure.
        pub = subprocess.run(
            [sys.executable, PUB_TEST, "--prefix", args.prefix,
             "--device", args.device, "--once", str(args.temp)],
            capture_output=True, text=True, timeout=20,
        )
        if pub.returncode != 0:
            check("pub-test.py", False, (pub.stderr or pub.stdout).strip()[-160:])
            return
        check("pub-test.py", True, f"publié {args.temp} °C → {topic}")

        # Attends l'arrivée dans InfluxDB (le bridge écrit de façon synchrone).
        deadline = time.time() + args.timeout
        seen_tel = seen_sup = False
        last = ""
        while time.time() < deadline:
            seen_tel = influx_has_measurement("telemetry", args.device, args.timeout + 30)
            seen_sup = influx_has_measurement("supervision", args.device, args.timeout + 30)
            if seen_tel and seen_sup:
                break
            time.sleep(1)

        check("InfluxDB : telemetry", seen_tel,
              f"temp_c={args.temp} (device={args.device})" if seen_tel else "pas trouvé")
        check("InfluxDB : supervision", seen_sup,
              "cpu/mem/msg_count…" if seen_sup else "pas trouvé")
    finally:
        bridge.terminate()
        try:
            bridge.wait(timeout=5)
        except subprocess.TimeoutExpired:
            bridge.kill()


# ---------------------------------------------------------------------------
# 3. Logs dans Loki
# ---------------------------------------------------------------------------
def step_loki() -> None:
    print("\n3. Logs dans Loki ?")
    # Loki doit au moins exposer des labels (job/stream) si promtail a poussé.
    try:
        r = requests.get(f"{LOKI_URL}/loki/api/v1/labels", timeout=5)
        labels = r.json().get("data", [])
    except (requests.RequestException, ValueError) as e:
        check("Loki : labels", False, str(e))
        return
    has_job = "job" in labels
    check("Loki : labels", has_job, f"labels={labels}" if labels else "aucun label")

    # Requête des logs récentes (job=docker).
    now = int(time.time())
    start = now - 600
    # NB : on passe la LogQL brute à `params` ; requests fait l'encodage.
    try:
        r = requests.get(
            f"{LOKI_URL}/loki/api/v1/query_range",
            params={"query": '{job="docker"}', "start": start * 10**9, "end": now * 10**9, "limit": 5},
            timeout=8,
        )
        res = r.json().get("data", {}).get("result", [])
        n_lines = sum(len(s.get("values", [])) for s in res)
        check("Loki : requête logs", n_lines > 0,
              f"{n_lines} lignes / {len(res)} séries" if n_lines else "aucune ligne")
    except (requests.RequestException, ValueError) as e:
        check("Loki : requête logs", False, str(e))


# ---------------------------------------------------------------------------
# 4. Export object store
# ---------------------------------------------------------------------------
def step_store(args) -> None:
    print("\n4. Export object store ?")
    try:
        health = requests.get(f"{STORE_URL}/", timeout=3)
    except requests.RequestException:
        check("store : upload", False, "object store hors ligne — étape ignorée")
        return
    if health.status_code != 200:
        check("store : upload", False, f"health={health.status_code}")
        return

    # Récupère les mesures (si aucune, on exporte un CSV minimal pour tester le flux).
    try:
        flux = (
            f'from(bucket:"{INFLUX_BUCKET}") '
            f'|> range(start:-1h) '
            f'|> filter(fn:(r)=>r._measurement=="telemetry") '
            f'|> filter(fn:(r)=>r.device=="{args.device}") '
            f'|> filter(fn:(r)=>r._field=="temp_c") '
            f'|> sort(desc:true)'
        )
        text = query_influx_flux(flux)
        rows = list(csv.DictReader(io.StringIO(text)))
    except requests.RequestException:
        rows = []

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["time", "device", "temp_c"])
    w.writeheader()
    for row in rows:
        w.writerow({"time": row.get("_time", ""), "device": row.get("device", args.device),
                    "temp_c": row.get("_value", "")})
    if not rows:
        # CSV de test minimal (pour valider le flux S3 même sans données).
        buf = io.StringIO("time,device,temp_c\n"
                          f"{datetime.now(timezone.utc).isoformat()},{args.device},{args.temp}\n")
    data = buf.getvalue().encode()

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    obj = f"{args.prefix}/{args.device}/test-{ts}.csv"
    url = f"{STORE_URL}/{STORE_BUCKET}/{obj}"

    up = requests.put(url, data=data, headers={"Content-Type": "text/csv"}, timeout=10)
    check("store : upload", up.status_code in (200, 201),
          f"PUT {obj} → HTTP {up.status_code}")
    if up.status_code in (200, 201):
        dl = requests.get(url, timeout=10)
        check("store : download", dl.status_code == 200 and dl.content == data,
              f"GET {len(dl.content)} octets")


# ---------------------------------------------------------------------------
def main() -> int:
    args = parse_args()
    print("=" * 62)
    print(" lab-synthese — vérification complète du stack")
    print(f" broker={BROKER}:{MQTT_PORT}  influx={INFLUX_URL}")
    print(f" loki={LOKI_URL}  store={STORE_URL}")
    print(f" device={args.device}  prefix={args.prefix}  temp={args.temp} °C")
    print("=" * 62)

    step_services()
    step_pub_influx(args)
    step_loki()
    step_store(args)

    # --- Résumé ---
    print("\n" + "=" * 62)
    print(" RÉSUMÉ")
    print("=" * 62)
    n_ok = sum(1 for ok, _ in RESULTS.values() if ok)
    n_ko = sum(1 for ok, _ in RESULTS.values() if not ok)
    for name, (ok, detail) in RESULTS.items():
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name}" + (f"  ({detail})" if detail and not ok else ""))
    print("-" * 62)
    print(f"  {n_ok} OK  ·  {n_ko} KO")

    # Object store KO n'est pas fatal ; le reste l'est.
    fatal_ko = [n for n, (ok, _) in RESULTS.items() if not ok and n != "store : upload"
                and n != "store : download" and n != "minio"]
    return 1 if fatal_ko else 0


if __name__ == "__main__":
    sys.exit(main())
