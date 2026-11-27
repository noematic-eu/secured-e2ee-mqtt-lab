#!/usr/bin/env python3
"""
export-store.py — Exporte les mesures InfluxDB → CSV → object store (SeaweedFS)

Usage :
    python3 export-store.py --device sensor-1 --prefix promo/equipe-a
    python3 export-store.py --device sensor-1 --prefix promo/equipe-a --range 1h
"""

import argparse
import csv
import io
import json
import sys
from datetime import datetime, timezone

import requests

# --- InfluxDB ---
INFLUX_URL = "http://localhost:18086"
INFLUX_TOKEN = "m2dfs-token"
INFLUX_ORG = "m2dfs"

# --- Object store (SeaweedFS) ---
STORE_URL = "http://localhost:19000"
STORE_BUCKET = "mesures"


def parse_args():
    p = argparse.ArgumentParser(description="InfluxDB → CSV → object store")
    p.add_argument("--device", default="sensor-1", help="Nom du device")
    p.add_argument("--prefix", default="promo/equipe-a", help="Préfixe d'équipe")
    p.add_argument("--range", default="1h", help="Période (1h, 24h, 7d)")
    return p.parse_args()


def query_influx(device: str, prefix: str, time_range: str) -> list[dict]:
    """Requêter InfluxDB (Flux) et retourner les mesures."""
    flux = f'''
    from(bucket: "m2dfs")
      |> range(start: -{time_range})
      |> filter(fn: (r) => r._measurement == "telemetry")
      |> filter(fn: (r) => r.device == "{device}")
      |> filter(fn: (r) => r._field == "temp_c")
      |> sort(desc: true)
    '''
    headers = {
        "Authorization": f"Token {INFLUX_TOKEN}",
        "Content-Type": "application/vnd.flux",
        "Accept": "application/csv",
    }
    resp = requests.post(
        f"{INFLUX_URL}/api/v2/query",
        params={"org": INFLUX_ORG},
        data=flux,
        headers=headers,
        timeout=10,
    )
    resp.raise_for_status()

    # Parser CSV
    lines = resp.text.strip().split("\n")
    if len(lines) < 2:
        return []

    reader = csv.DictReader(io.StringIO(resp.text))
    results = []
    for row in reader:
        results.append({
            "time": row.get("_time", ""),
            "device": row.get("device", device),
            "temp_c": row.get("_value", ""),
        })
    return results


def csv_to_bytes(measures: list[dict]) -> bytes:
    """Convertir les mesures en CSV."""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["time", "device", "temp_c"])
    writer.writeheader()
    for m in measures:
        writer.writerow(m)
    return output.getvalue().encode("utf-8")


def upload_store(object_name: str, data: bytes) -> bool:
    """Uploader un fichier dans l'object store (HTTP PUT)."""
    url = f"{STORE_URL}/{STORE_BUCKET}/{object_name}"
    resp = requests.put(url, data=data, headers={"Content-Type": "text/csv"}, timeout=10)
    return resp.status_code in (200, 201)


def download_store(object_name: str) -> bytes | None:
    """Télécharger un fichier depuis l'object store (HTTP GET)."""
    url = f"{STORE_URL}/{STORE_BUCKET}/{object_name}"
    resp = requests.get(url, timeout=10)
    if resp.status_code == 200:
        return resp.content
    return None


def main():
    args = parse_args()

    print(f"1. Query InfluxDB : device={args.device}, range={args.range}")
    measures = query_influx(args.device, args.prefix, args.range)
    print(f"   → {len(measures)} mesures trouvées")

    if not measures:
        print("   (pas de données — lancer pub-test.py d'abord)")
        sys.exit(0)

    print(f"   Première : {measures[0]['time']}  temp={measures[0]['temp_c']} °C")
    print(f"   Dernière : {measures[-1]['time']}  temp={measures[-1]['temp_c']} °C")

    # --- Export CSV ---
    csv_data = csv_to_bytes(measures)
    print(f"2. CSV généré : {len(csv_data)} octets")

    # --- Upload MinIO ---
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    object_name = f"{args.prefix}/{args.device}/{ts}.csv"

    print(f"3. Upload object store : {object_name}")
    if upload_store(object_name, csv_data):
        print(f"   ✓ Upload OK")

        # --- Vérification : download ---
        downloaded = download_store(object_name)
        if downloaded:
            print(f"   ✓ Download OK : {len(downloaded)} octets")
            lines = downloaded.decode().strip().split("\n")
            print(f"   ✓ {len(lines) - 1} lignes de données")
        else:
            print("   ✗ Download échoué")
    else:
        print("   ✗ Upload échoué — vérifier que l'object store tourne")
        sys.exit(1)

    print("\n✓ Export terminé.")


if __name__ == "__main__":
    main()
