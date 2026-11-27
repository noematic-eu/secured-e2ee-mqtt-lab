# lab-synthese — état des lieux (27/11/2026)

## Contexte

Lab de synthèse du module M2 DFS qui assemble :
- MQTT Secure v3 (mTLS, ACL, enrollment)
- InfluxDB + Grafana (télémétrie + supervision)
- Expo Go (app mobile)
- **E2EE** (ECDH + AES-256-GCM) — en cours
- **Supervision** (CPU, mem, msg, connectivité) — OK
- **Résilience** (broker down → HTTP InfluxDB) — à faire
- **Loki** (logs) — OK
- **Object store** (fichiers de mesures, SeaweedFS) — ✅

Dossier **formateur** pour l'instant — la fiche TP étudiant viendra après
que le stack tourne (cf. AGENTS.md : voix étudiants, pas de notes formateur
dans `etudiant/`).

## État du stack (vérifié 26-27/11)

| Service   | Port host | État  |
|-----------|-----------|-------|
| Mosquitto | **11883** | ✅ up, pub/sub OK |
| InfluxDB  | **18086** | ✅ up, org+bucket+token OK, écriture/lecture OK |
| Grafana   | **13000** | ✅ up, datasources provisionnées (dashboards non vérifiés) |
| Loki      | **13100** | ✅ up, logs queryables |
| Promtail  | —         | ✅ collecte docker → Loki OK |
| ObjectStore | 19000     | ✅ up (SeaweedFS, PUT/GET OK) |

**Flux validés de bout en bout :**
- `pub-test.py --once 25.3` → `bridge.py` → InfluxDB (telemetry + supervision) ✅
- logs conteneurs → promtail → Loki (requête LogQL OK) ✅
- `test-all.py` : **11 OK / 0 KO** ✅

> ⚠️ **Ports** : lab-synthese utilise un schéma **+10000** (11883, 18086,
> 13000, 13100, 19000, 19001) car le lab `lab-flux` (toujours en cours)
> occupe les ports standards 1883/3000/8086. Les deux labs coexistent.
> Ports codés dans `docker-compose.yml` + defaults des scripts.

## Object store (résolu)

MinIO a retiré ses images de Docker Hub (sept. 2026). Remplacé par
**SeaweedFS** (`chrislusf/seaweedfs:latest`) — même contrat HTTP (PUT/GET),
port 19000 (filer 8888).

## Bugs corrigés cette session (ne pas re-diagnostiquer)

| # | Bug | Cause racine | Correctif |
|---|-----|--------------|-----------|
| 1 | `port 1883 already allocated` | lab-flux en cours occupe les ports standards | Ports +10000 pour lab-synthese (compose + scripts) |
| 2 | Messages MQTT non livrés (même topic hors ACL) | ACL sans section défaut = **default-deny total pour l'anonyme** (Mosquitto 2.x) | Section défaut `topic read/write promo/#` dans `mosquitto/acl` + `chmod 600` (avertissement world-readable) |
| 3 | `'Client' object has no attribute 'write'` | `on_message` passe le **client MQTT** à `write_telemetry()`, pas le write_api InfluxDB | `write_api` global, fonctions signées `write_telemetry(payload)` |
| 4 | `'Point' object has no attribute 'fields'` | `Point` n'expose pas `.fields` | Dictionnaire `added` pour tracer les champs écrits |
| 5 | Promtail tailait `/var/log` (job `varlogs`) | L'entrypoint de l'image charge **sa propre** `config.yml`, pas la nôtre | `command: -config.file=/etc/promtail/promtail-config.yml` dans le compose |
| 6 | `invalid labels stage config: expected a map, got string` | `- labels:\n  stream` = string ; il faut une map | `- labels:\n  stream:` (avec deux-points) |
| 7 | Promtail ne voyait que 2 fichiers logs (conteneurs anciens) | **Daemon Docker Desktop macOS : log driver par défaut = `local`** (logs dans `local-logs/`, invisibles en `*-json.log`) | Ancre `x-logging: &default-logging {driver: json-file}` appliquée à tous les services, conteneurs recréés |
| 8 | `test-all.py` : « InfluxDB pas trouvé » | Ma détection cherchait une ligne `,n,` qui n'existe pas dans le CSV InfluxDB | Détection = en-tête + ≥1 ligne de données |
| 9 | `test-all.py` : Loki 400 « parse error » | **Double-encodage** : `quote()` + `requests.params` | LogQL brute dans `params`, requests encode |

Note InfluxDB : le setup wrapper docker (org `m2dfs`, bucket `m2dfs`,
token `m2dfs-token`) fonctionne sur volume neuf — vérifié. (Le log
« skipping setup wrapper » est un artifact de l'entrypoint, le résultat
est OK.)

## Phase E2EE — ✅ FAIT (27/11, durcie 27/11)

**Conçue et arrêtée** (cf. décisions ETAT antérieur) :
- ECDH **P-256** (SECP256R1) ; privé DER PKCS8, public **DER SubjectPublicKeyInfo**
- Clé AES-256 = **HKDF-SHA256**(shared_secret, info=`m2dfs-e2ee|{id_a}|{id_b}|aes-256-gcm|v1`)
  (IDs triés → dérivation symétrique ; contexte lié au couple et à l'usage)
- **AES-256-GCM**, nonce 96 bits aléatoire
- **AAD** = `v1|{topic}|{sender_id}|{seq_hex}` → lie le message au topic,
  à l'identité de l'émetteur, et au seq (anti-redirection, anti-rejeu, anti-spoof)
- Fil réseau = `seq(4) ‖ nonce(12) ‖ ciphertext+tag(16)`
  (le `ciphertext` de `AESGCM.encrypt` inclut déjà le tag GCM de 16 o.)
- L'enrôlement = **agent d'identité** : clés publiques uniquement, jamais
  les clés de session (modèle keyserver PGP, pas CA — il ne signe rien)
- Clés privées locales : `lab-synthese/keys/{id}.priv` (créées au 1er run, persistées)
- Enrôlement en **process local Flask** (comme bridge.py), pas dans le compose

**Durcissement (27/11, revue de sécurité) :**
- [x] HKDF-SHA256 avec contexte (remplace SHA-256 brut)
- [x] AAD = topic + émetteur + seq (remplace AAD=None)
- [x] Wire format versionné : `seq(4)` en tête (permettra de migrer)
- [x] Enrôlement : 409 si déjà présent + détection de substitution de clé
- [x] Client : même clé → OK ; clé différente → erreur explicite

**Vérifié de bout en bout :**
- [x] `scripts/e2ee.py` — helpers crypto (generate, HKDF, encrypt/decrypt+AAD, register, fetch)
- [x] `enrollment_service/app.py` — Flask 8443, persistance state.json, 409 anti-substitution
- [x] `scripts/pub-test.py --e2ee` — chiffre avant publish (passe le topic)
- [x] `scripts/sub-decrypt.py` — subscribe + déchiffrement + vérification AAD
- [x] Raw payload = binaire illisible (130 o., hexdump vérifié)
- [x] Redirection de topic → REFUSÉ (AAD invalide)
- [x] Émetteur inconnu → REFUSÉ (clé inconnue / AAD invalide)
- [x] Substitution de clé sur l'annuaire → 409 + alerte client

**Option B (tranchée) :** `bridge.py` déchiffre avant InfluxDB → **non implémenté**.
Le bridge reste agnostique du chiffrement ; l'app déchiffre. Pour le lab,
`sub-decrypt.py` suffit à montrer le flux.

**Limites connues (sujets de TP) :**
- Clés statiques : pas de forward secrecy (compromission d'une clé privée → tout l'historique exposé)
- Rejeu détectable (AAD + seq) mais pas empêché sans état côté receveur
- Enrôlement sans TLS : un MITM peut substituer une clé au 1er contact (TOFU non implémenté)
- Pas d'authentification de l'émetteur (pas de signature) : l'app ne peut pas prouver que le message vient du device
- Métadonnées visibles : topics, tailles, fréquence, horaires (l'E2EE ne les protège pas)

## Ce qui reste

- [ ] Grafana : vérifier dashboards alimentés (telemetrie.json, supervision.json)
- [ ] Panneau « broker down » (supervision absente depuis X min → rouge)
- [ ] Phase Résilience (app Expo : fallback HTTP, cache local)
- [ ] Fiche TP étudiant (une fois le stack vert)
- [ ] Syllabus : `fiche-cdc-projet.md`, `08-cours-s7-securite.md` §3,
      `11-cours-s9-edge.md`, déroulés S9/S10 (E2EE/InfluxDB « ne faites pas » → « imposé »)

### TP sécurité offensive/défensive (nouveau, issu de la revue 27/11)

- [ ] **Attaque rejeu** : `mosquitto_sub` capture un message, `mosquitto_pub` le réinjecte
      → l'app l'accepte (le seq est dans l'AAD mais sans état, on ne peut pas dire « déjà vu »).
      Correction : `seen_seqs` (LRU cache) côté `sub-decrypt.py`.
- [ ] **Attaque substitution de clé** : un 3e génère sa paire, tente `POST /devices/app-1/pubkey`
      → 409 (bloqué). Sans le 409 (ancienne version) : il déchiffre tout.
      Démontrer les deux versions.
- [ ] **Attaque redirection** : capturer un message du topic `telemetry`, réinjecter sur `supervision`
      → AAD invalide, refusé. Démontrer.
- [ ] (Option avancé) **HPKE / ECDH éphémère** : forward secrecy. Un ECDH par message ou par session,
      pas une clé statique. Changement plus important, bon TP de fin de module.

## Dépôt — quoi committer

Décision du 27/11 : le dépôt décrit le lab. Il ne contient pas les artefacts d'un run. Détail et commandes de régénération dans `README.md` § Dépôt. `.gitignore` exclut les quatre premières lignes du tableau « hors git ».

**Dans git** (sources) : `docker-compose.yml`, `requirements.txt`, `README.md`, `ETAT.md`, `mosquitto/`, `loki/`, `promtail/`, `grafana/` (provisioning + `dashboards/*.json`), `enrollment_service/app.py`, `scripts/*.py`.

**Hors git, à régénérer :**

| Artefact | Produit par | Régénération |
|---|---|---|
| `.venv/` | venv local | `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` |
| `__pycache__/` | import | rien à conserver |
| `keys/{id}.priv` | `e2ee.generate_keypair` au premier `pub-test.py --e2ee` (`--device`) ou `sub-decrypt.py` (`--app-id`) | supprimer `keys/*.priv`, relancer les deux processus. Deux `--app-id` distincts → deux fichiers (`app-1.priv` et `app1.priv` aujourd'hui : le second vient d'un id sans tiret) |
| `enrollment_service/state.json` | `POST /devices/<id>/pubkey` | supprimer le fichier, relancer `app.py`, puis les deux scripts : ils réenregistrent les publiques. L'agent démarre sans ce fichier |
| volumes `influxdb-data`, `grafana-data`, `loki-data`, `objectstore-data` | `docker compose up` | `docker compose down -v && docker compose up -d`. Mesures, logs Loki, CSV SeaweedFS et base Grafana partent. Influx ne réapplique org / bucket / token / rétention que sur volume **vide** |

Les JSON de `grafana/dashboards/` sont la source. L'UI peut diverger dans `grafana-data` (`allowUiUpdates: true`) : un changement à garder s'exporte dans ces JSON avant un `down -v`.

Mosquitto : `persistence false`, rien à archiver. Pas de certificats TLS dans ce lab (anonyme + ACL).

Les trois `.priv` et `state.json` présents sur la machine au 27/11 sont un run de démo. Les effacer ne casse pas le dépôt ; le prochain `--e2ee` en refait d'autres, incompatibles avec d'éventuels messages chiffrés encore dans le broker.

## Fichiers

```
lab-synthese/
├── .gitignore
├── docker-compose.yml          # 6 services, ports +10000, json-file
├── requirements.txt            # paho, influx, requests, cryptography, flask
├── README.md
├── ETAT.md                     # ← ce fichier
├── mosquitto/{config.conf,acl} # ACL : section défaut (anonyme) + rules user
├── loki/loki-config.yml
├── promtail/promtail-config.yml
├── grafana/                    # provisioning + dashboards (sources)
├── enrollment_service/app.py   # ✅ Flask 8443
└── scripts/
    ├── bridge.py               # ✅ MQTT JSON → InfluxDB
    ├── pub-test.py             # ✅ pub plain + --e2ee
    ├── export-store.py         # ✅ InfluxDB → CSV → SeaweedFS
    ├── test-all.py             # ✅ 11 OK / 0 KO
    ├── e2ee.py                 # ✅ ECDH P-256 + HKDF + AES-256-GCM + AAD
    └── sub-decrypt.py          # ✅ subscriber + déchiffrement

hors git : .venv/  keys/*.priv  enrollment_service/state.json
           volumes Compose (hors arbre)
```

## Commandes utiles (ports à jour)

```bash
cd lab-synthese
docker compose up -d                          # 6 services
docker compose ps
.venv/bin/python enrollment_service/app.py &  # enrôlement (port 8443)
.venv/bin/python scripts/pub-test.py --once 25.3           # pub plain
.venv/bin/python scripts/pub-test.py --e2ee --once 25.3    # pub chiffrée
.venv/bin/python scripts/sub-decrypt.py --app-id app-1 --device sensor-1
.venv/bin/python scripts/bridge.py                   # bridge MQTT → InfluxDB
.venv/bin/python scripts/test-all.py                 # vérification complète
.venv/bin/python scripts/export-store.py             # InfluxDB → CSV → SeaweedFS
curl -s http://localhost:13100/loki/api/v1/labels    # Loki ?
curl -s http://localhost:18086/ping                   # InfluxDB ?
curl -s http://localhost:19000/                       # Object store ?
```

## Prochaine session — prompt

> On continue le `lab-synthese`. L'état est dans `lab-synthese/ETAT.md`.
>
> **Priorité 1** : ✅ Stack vert (11 OK / 0 KO). Object store = SeaweedFS (19000).
>
> **Priorité 2** : ✅ E2EE (ECDH + AES-GCM, enrôlement, `pub-test.py --e2ee`, `sub-decrypt.py`). Clés et `state.json` hors git — voir § Dépôt.
>
> **Priorité 3** : ✅ E2EE durcie (HKDF + AAD + 409 enrôlement). Vérifié : redirection, spoof, substitution bloqués.
>
> **Priorité 4** : TP sécurité offensive/défensive (rejeu, substitution de clé, redirection).
> Voir § « TP sécurité offensive/défensive » ci-dessus.
>
> **Priorité 5** : vérifier les dashboards Grafana (13000) alimentés.
>
> **Contrainte** : voix étudiants (AGENTS.md) — lab-synthese reste formateur pour l'instant ; la fiche TP viendra après que le stack soit vert.
