"""Konstanten für die ABUS Secvest Integration.

Endpoints verifiziert am 07.07.2026 per HAR-Mitschnitt und Probe-Skript
gegen eine Secvest FUAA50000 (Web-UI mit sec_*.cgi/.cgx-Struktur):

  GET /system/partitions/          -> JSON-Array aller Teilbereiche
  PUT /system/partitions-{n}/      -> {"state": "set"|"unset"} schaltet
  GET /faults/                     -> JSON-Array Störungen (prevents-set!)
  GET /sec_global_status.cgx       -> XML-artig, enthält offene Zonen
  GET /logs/                       -> JSON Ereignisprotokoll

Auth: HTTP Basic (Benutzer Code + Web-Passwort). Fallback: Form-Login
POST /sec_login.cgi mit usr=<code>&pwd=<passwort> (Session-Cookie).
"""

DOMAIN = "secvest"

CONF_VERIFY_SSL = "verify_ssl"
# Options-Flow
CONF_SCAN_INTERVAL = "scan_interval"
CONF_PARTITIONS = "partitions"  # Liste ausgewählter Teilbereich-IDs (str)
# Optionaler HA-eigener Code fürs Unscharfschalten (nicht der Secvest-Code).
# Leer = kein Code nötig. Scharfschalten bleibt immer ohne Code.
CONF_DISARM_CODE = "disarm_code"
# HA-Benutzer (IDs), die OHNE Code unscharf schalten dürfen – gedacht für
# Skripte/Apple Watch, die keinen Code abfragen können. Geprüft wird die
# user_id im Kontext des Service-Aufrufs (bleibt durch Skripte erhalten).
CONF_TRUSTED_USERS = "trusted_users"
# Nach dem Scharfschalten so oft/lange nachprüfen, ob die Anlage den
# Teilbereich wirklich scharf meldet (manche Firmwares antworten 2xx und
# brechen intern ab, z.B. bei offener Zone).
ARM_VERIFY_ATTEMPTS = 4
ARM_VERIFY_INTERVAL = 2.5  # Sekunden
# Die Anlage beantwortet unter Last gelegentlich eine Abfrage nicht.
# So viele Verbindungsfehler in Folge werden mit den letzten Daten
# überbrückt, bevor die Entities 'nicht verfügbar' werden.
MAX_TOLERATED_FAILURES = 2
MIN_SCAN_INTERVAL = 10  # Anlage bricht unter Last ab -> nie darunter
MAX_SCAN_INTERVAL = 120

DEFAULT_PORT = 4433
DEFAULT_SCAN_INTERVAL = 15  # Sekunden; Web-UI selbst pollt ~2 s
DEFAULT_VERIFY_SSL = False  # selbstsigniertes Zertifikat
# /logs/ liefert ~600 Einträge und ignoriert ?limit -> client-seitig
# auf die neuesten begrenzen.
DEFAULT_LOG_LIMIT = 10
# Ereignisprotokoll nur so oft abrufen (Sekunden); der Abruf kostet ~6 s.
LOG_REFRESH_INTERVAL = 300

ENDPOINT_PARTITIONS = "/system/partitions/"
ENDPOINT_PARTITION_SET = "/system/partitions-{p}/"
# Melder je Teilbereich: JSON-Array mit name + state ("open"/"closed"),
# Basic-tauglich. Liefert den Offen-Status auch im unscharfen Zustand –
# auf manchen Anlagen/Firmwares erscheint dort KEINE type-5000-Störung.
ENDPOINT_PARTITION_ZONES = "/system/partitions-{p}/zones/"
ZONE_STATE_OPEN = "open"
ENDPOINT_FAULTS = "/faults/"
# sec_global_status.cgx liefert offene Zonen nur einer eingeloggten
# Web-UI-Session (Basic: <authorised> leer). Daher NICHT vom Client
# genutzt – offene Zonen kommen aus /faults/ type 5000. Nur zur Doku.
ENDPOINT_GLOBAL_STATUS = "/sec_global_status.cgx"
ENDPOINT_LOGS = "/logs/"
ENDPOINT_FORM_LOGIN = "/sec_login.cgi"

# /faults/-Störungstyp für eine offene Zone (belegt) – Basis der
# Offen/Zu-Erkennung der Zonen-Binary-Sensors.
FAULT_TYPE_OPEN_ZONE = "5000"

# Bekannte /faults/-Typ-Codes (Klartext). Weitere beim Auftreten
# ergänzen; unbekannte Codes zeigt der Wartungssensor generisch an.
FAULT_TYPES = {
    FAULT_TYPE_OPEN_ZONE: "Zone offen / belegt",
}

# Zustände laut API: "set" | "unset" | "partset" (alle verifiziert).
# partset (intern scharf) am 07.07.2026 auf TB1 (Haustür) bestätigt:
# PUT liefert state "partset", GET /system/partitions/ ebenso. ACHTUNG:
# partset ist partitionsabhängig – nur intern-scharf-fähige Teilbereiche
# akzeptieren es, andere antworten mit HTTP 409 (z.B. TB3/OG).
STATE_SET = "set"
STATE_UNSET = "unset"
STATE_PARTSET = "partset"

# Alarm-Zustände am 07.07.2026 per Testalarm auf TB2 (EG) belegt
# (tools/watch_status.py). Der Alarm erscheint NUR im state-Feld von
# GET /system/partitions/ – /faults/ und global_status blieben leer.
# Ablauf: set -> set-alarm (Vollalarm) -> acknowledged (quittiert,
# nicht zurückgesetzt) -> unset.
STATE_ALARM = "set-alarm"
STATE_ACKNOWLEDGED = "acknowledged"
