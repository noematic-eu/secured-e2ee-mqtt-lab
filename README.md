# lab-synthese — bus, historique, logs, chiffrement

Le live reste MQTT. InfluxDB, Grafana, Loki et le dépôt de fichiers sont à côté du bus. L'écran qui lit le broker ne passe pas par la base.

Les ports sont décalés de 10000 pour laisser `lab-flux` sur 1883, 3000 et 8086. Les deux labs tournent en même temps. Le broker de salle (`lab/mosquitto`, conteneur `m2dfs-mosquitto`) est un troisième processus : ici le conteneur s'appelle `m2dfs-synthese-mosquitto`.

## Architecture

```
pub-test.py  →  Mosquitto :11883  →  bridge.py  →  InfluxDB :18086  →  Grafana :13000
                      │                                      ↓
                      │                                 export-store.py
                      │                                      ↓
                      │                              SeaweedFS :19000
                      ↓
               sub-decrypt.py          (si --e2ee : le bridge ne déchiffre pas)

conteneurs Docker  →  Promtail  →  Loki :13100  →  Grafana
```

Topics, préfixe `promo/equipe-a` et device `sensor-1` par défaut :

- `promo/equipe-a/devices/sensor-1/telemetry` — `{ ts, device_id, temp_c, alert }`
- `promo/equipe-a/devices/sensor-1/supervision` — `{ ts, device_id, msg_count, uptime_s, cpu_pct, mem_mb, broker_up }`

Mosquitto est anonyme. L'ACL autorise `promo/#` en lecture et en écriture. Le seuil d'alerte du bridge est 28 °C.

## Ports et comptes

| Service | URL | Accès |
|---|---|---|
| Mosquitto | `localhost:11883` | anonyme, topics `promo/#` |
| InfluxDB | http://localhost:18086 | org `m2dfs`, bucket `m2dfs`, token `m2dfs-token`, admin / `m2dfs2026` |
| Grafana | http://localhost:13000 | `admin` / `admin` |
| Loki | http://localhost:13100 | — |
| SeaweedFS | http://localhost:19000 | fichiers sous `/mesures/…` |
| Enrôlement | http://localhost:8443 | process local, hors Compose |

L'enrôlement écoute 8443, comme `lab-secured-v3`. Un seul des deux processus peut tenir ce port.

## Lancer

```bash
cd lab-synthese
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
docker compose up -d
```

Compose démarre Mosquitto, InfluxDB, Grafana, Loki, Promtail et SeaweedFS. Le bridge, le simulateur et l'enrôlement restent sur la machine, pour être lus et relancés à la main.

Les logs des conteneurs sont en driver `json-file`. Promtail lit ces fichiers. Sans ce driver, Docker Desktop (macOS) les range ailleurs et Loki reste vide.

Arrêt : `docker compose down` dans `lab-synthese`. Les volumes (InfluxDB, Grafana, Loki, fichiers) restent. `docker compose down -v` les efface.

## Flux clair — courbe Grafana

Trois terminaux, depuis `lab-synthese` :

```bash
.venv/bin/python scripts/bridge.py --prefix promo/equipe-a --device sensor-1
.venv/bin/python scripts/pub-test.py --prefix promo/equipe-a --device sensor-1
.venv/bin/python scripts/pub-test.py --once 29.1
```

La publication continue envoie une température toutes les 2 s, et une supervision tous les 5 messages. `--once` envoie une télémétrie et une supervision, puis quitte.

Le bridge écrit trois measurements :

- `telemetry` — champ `temp_c`, tags `device` et `prefix`
- `events` — si `alert` est vrai ou si `temp_c` ≥ 28
- `supervision` — `cpu_pct`, `mem_mb`, `msg_count`, `uptime_s`, `broker_up` (0 ou 1)

Grafana (`http://localhost:13000`) charge deux tableaux : **M2 DFS — Télémétrie** et **M2 DFS — Supervision**. Le panneau Broker lit le dernier `broker_up` sur une heure : 1 vert (UP), 0 rouge (DOWN). Sans point dans l'heure, le panneau reste rouge.

Sur macOS, `cpu_pct` et `mem_mb` sont des valeurs simulées : `/proc` n'existe pas.

## Flux chiffré — le broker ne lit pas le JSON

Même préfixe, même device, **même** `--app-id` des deux côtés. Le défaut est `app-1`. `app1` et `app-1` sont deux identités, deux clés : le déchiffrement échoue.

```bash
.venv/bin/python enrollment_service/app.py
```

```bash
.venv/bin/python scripts/sub-decrypt.py --app-id app-1 --device sensor-1
.venv/bin/python scripts/pub-test.py --e2ee --app-id app-1 --device sensor-1
```

L'enrôlement ne stocke que les clés publiques (`enrollment_service/state.json`). Les clés privées restent dans `keys/{id}.priv`. Ni l'un ni l'autre ne va dans le dépôt : voir [Dépôt](#dépôt).

Le fil MQTT est `nonce (12 o.) ‖ AES-256-GCM`. La clé AES est `SHA-256` du secret ECDH P-256. Un `mosquitto_sub` sur le topic de télémétrie affiche un binaire. `sub-decrypt.py` affiche le JSON.

```bash
mosquitto_sub -h localhost -p 11883 -t 'promo/equipe-a/devices/sensor-1/telemetry'
```

`--e2ee` chiffre la télémétrie et la supervision. Le bridge attend du JSON : ces messages ne remplissent pas InfluxDB. Pour la courbe, republiez sans `--e2ee`.

## Export des mesures

Après un flux clair (le CSV vient d'InfluxDB) :

```bash
.venv/bin/python scripts/export-store.py --device sensor-1 --prefix promo/equipe-a --range 1h
```

Le fichier est `http://localhost:19000/mesures/promo/equipe-a/sensor-1/<horodatage>.csv`.

## Vérification d'un coup

`test-all.py` démarre le bridge, publie une température, interroge InfluxDB et Loki, exporte un CSV, puis arrête le bridge.

```bash
.venv/bin/python scripts/test-all.py
```

## Dépôt

À committer : tout ce qui décrit le lab et que `git status` montre, sauf les chemins de `.gitignore`.

| Versionné | Rôle |
|---|---|
| `docker-compose.yml`, `mosquitto/`, `loki/`, `promtail/` | Stack et config broker / logs |
| `grafana/provisioning/`, `grafana/dashboards/*.json` | Sources Grafana. Le provisioning relit les JSON toutes les 30 s |
| `enrollment_service/app.py`, `scripts/*.py` | Agent d'identité et outils |
| `requirements.txt`, `README.md`, `ETAT.md` | Dépendances et doc formateur |

Les comptes du lab (`m2dfs-token`, `admin` / `admin`, `admin` / `m2dfs2026`) sont des fixtures écrites dans le compose et dans `grafana/provisioning/datasources/datasources.yml`. Ils ne sont pas tirés au hasard.

Ne pas committer, et recréer si le dossier est neuf ou si on veut repartir de zéro :

| Absent du dépôt | Quand il apparaît | Comment le refaire |
|---|---|---|
| `.venv/` | `python3 -m venv .venv` | `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` |
| `__pycache__/` | import Python | disparaît avec le venv ; inutile de le garder |
| `keys/{id}.priv` | premier `generate_keypair` | lancer `sub-decrypt.py` (clé de `--app-id`) et `pub-test.py --e2ee` (clé de `--device`). Fichier DER PKCS8, mode `0600`. `app-1` et `app1` sont deux fichiers |
| `enrollment_service/state.json` | premier `POST /devices/<id>/pubkey` | le processus Flask le réécrit quand les deux côtés s'enrôlent. Démarrer l'agent avant les scripts |
| volumes Docker `influxdb-data`, `grafana-data`, `loki-data`, `objectstore-data` | `docker compose up` | `docker compose down -v && docker compose up -d` |

`keys/` n'existe pas tant qu'aucun script E2EE n'a tourné : `e2ee.py` crée le dossier.

Une clé privée ne déchiffre que les messages faits pour la publique enrôlée avec elle. Pour recommencer l'E2EE, effacer les deux ensemble, puis relancer l'agent, l'abonné et l'émetteur :

```bash
rm -f keys/*.priv enrollment_service/state.json
.venv/bin/python enrollment_service/app.py
.venv/bin/python scripts/sub-decrypt.py --app-id app-1 --device sensor-1
.venv/bin/python scripts/pub-test.py --e2ee --app-id app-1 --device sensor-1
```

`docker compose down -v` efface les mesures InfluxDB, les logs Loki, les CSV SeaweedFS et la base Grafana. Au `up` suivant, InfluxDB rejoue le setup du compose (org `m2dfs`, bucket `m2dfs`, token `m2dfs-token`, rétention 72 h) **seulement si le volume est vide**. Un volume déjà initialisé ignore ces variables. Grafana recharge admin depuis l'environnement sur volume neuf, et les deux tableaux depuis `grafana/dashboards/`. Une modification faite dans l'interface ( `allowUiUpdates: true` ) reste dans le volume : pour la garder, exporter le JSON dans `grafana/dashboards/`.

Mosquitto a `persistence false` : pas de base broker à versionner.

## Fichiers

```
lab-synthese/
├── .gitignore                   # .venv, __pycache__, keys/*.priv, state.json
├── docker-compose.yml
├── requirements.txt
├── README.md
├── ETAT.md                      # notes formateur, pas la fiche étudiant
├── enrollment_service/app.py    # clés publiques, port 8443
├── mosquitto/config.conf
├── mosquitto/acl
├── loki/loki-config.yml
├── promtail/promtail-config.yml
├── grafana/                     # datasources + deux dashboards (sources)
└── scripts/
    ├── bridge.py                # MQTT JSON → InfluxDB
    ├── pub-test.py              # device simulé, option --e2ee
    ├── sub-decrypt.py           # abonné qui déchiffre
    ├── e2ee.py                  # ECDH P-256 + AES-256-GCM
    ├── export-store.py          # InfluxDB → CSV → SeaweedFS
    └── test-all.py

# produits au run, ignorés par git :
#   .venv/
#   keys/{id}.priv
#   enrollment_service/state.json
#   volumes Compose (hors arbre) : influxdb-data, grafana-data, loki-data, objectstore-data
```
