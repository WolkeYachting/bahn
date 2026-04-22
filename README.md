# Bahn-Verspätungstracker Paderborn

Dokumentiert Verspätungen ≥60 min und Ausfälle aller Nah- und Regionalverkehrszüge ab Paderborn Hbf — als Entscheidungsgrundlage für Fahrgastrechte-Anträge.

**Architektur:**
- Flask-Backend auf Render.com
- cron-job.org pingt alle 30 Minuten den `/poll`-Endpunkt
- Daten werden im GitHub-Repo gespeichert (Commits via Contents-API)
- Tägliche Auswertung um 09:00 Europe/Berlin via `/analyze`

## Wie funktioniert's

**Logischer Tag:** Ein Tag X spannt sich von 03:30 Europe/Berlin bis 03:30 des Folgetags. Damit landen nächtliche Trips eindeutig im richtigen Tagesordner.

**Status-Maschine pro Trip:**
- `scheduled` — bekannt aus Plandaten, aber noch nie in Live-Daten gesehen
- `active` — beim letzten Poll in den Departures gesehen, Live-Daten verfügbar
- `finished` — beim letzten Poll **nicht** mehr in den Departures, planmäßige Endankunft mind. 30 min vorbei

**Pollvorgang (alle 30 min):**
1. Alle Abfahrten 24h voraus von Paderborn Hbf abrufen (gefiltert auf RE/RB/S-Bahn)
2. Pro Trip die volle Fahrt holen (alle Zwischenhalte)
3. Trip im richtigen Tageslog ablegen oder aktualisieren
4. Bekannte aktive Trips, die diesmal nicht in den Departures waren und deren Plan-Ankunft +30 min vorbei ist → `finished`

**Auswertung (täglich 09:00):**
1. Vortags-Log laden
2. Noch aktive Trips zwangsweise auf `finished` setzen (`force_finished: true`)
3. Jeden `finished` Trip klassifizieren:
   - **Verspätung:** mindestens ein Halt nach Paderborn hat eine **bestätigte** Ist-Ankunftsverspätung ≥60 min (Zielbahnhof = Halt mit der höchsten Verspätung)
   - **Teilausfall:** Trip startete, aber späterer Halt ist `cancelled`
   - **Vollausfall:** Trip startete gar nicht
4. Trips, die nie aus `scheduled` rauskamen (kein Live-Sichtkontakt) → Diagnose-Liste
5. Excel neu generieren

**Wichtig — „bestätigt" vs. „prognostiziert":** Wir werten nur Halte aus, bei denen die Bahn-API ein tatsächliches `arrival` zurückliefert. Reine Prognosen (z. B. „+70 min", die sich später auf „+45 min" aufholt) zählen nicht als Vorfall.

## Schritt-für-Schritt-Einrichtung

### Schritt 1 — Zwei GitHub-Repos anlegen

Du brauchst **zwei** Repos:

1. **Code-Repo** (z. B. `wolkeyachting/bahn-pb`): Der Code aus diesem ZIP. Dieses Repo wird zu Render verbunden.
2. **Daten-Repo** (z. B. `wolkeyachting/bahn-pb-data`): Hier landen die Logs (`logs/*.json`), `incidents.json` und das Excel (`exports/`). Kann auch das gleiche Repo sein wie das Code-Repo, aber getrennt ist sauberer (Commits stören dich nicht beim Coden).

Beide am besten **öffentlich**, damit du das Excel bequem über die GitHub-Webseite herunterladen kannst.

### Schritt 2 — GitHub Personal Access Token erstellen

1. GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate
2. Name: `bahn-pb`
3. Repository access: **Only select repositories** → das Daten-Repo wählen
4. Permissions → Repository → **Contents: Read and write**
5. Token kopieren (wird nur einmal angezeigt)

### Schritt 3 — Code-Repo befüllen

ZIP entpacken, alle Dateien (außer `logs/`, `exports/`, `incidents.json` falls vorhanden) ins Code-Repo committen.

### Schritt 4 — Auf Render.com deployen

1. render.com → New → **Blueprint**
2. Code-Repo auswählen → Render erkennt `render.yaml` automatisch
3. Beim Setup Environment Variables setzen:
   - `CRON_TOKEN` = ein selbst gewähltes Geheimnis (z. B. `xK9mPq2vN8`) — schützt deine Endpoints
   - `ADMIN_TOKEN` = ein anderes Geheimnis (für `/reset`)
   - `GITHUB_TOKEN` = der Token aus Schritt 2
   - `GITHUB_REPO` = `wolkeyachting/bahn-pb-data` (dein Daten-Repo)
4. Create Web Service → ein paar Minuten warten

Render gibt dir eine URL wie `https://bahn-pb.onrender.com`.

### Schritt 5 — Render testen

Im Browser öffnen:
- `https://bahn-pb.onrender.com/` — sollte die Übersichtsseite zeigen
- `https://bahn-pb.onrender.com/health` — sollte `"github_enabled": true` zeigen
- `https://bahn-pb.onrender.com/poll?token=DEIN_CRON_TOKEN` — sollte einen ersten Poll auslösen (dauert ~30s)

Nach dem ersten Poll: ins Daten-Repo schauen → unter `logs/` sollte ein `YYYY-MM-DD.json` aufgetaucht sein.

### Schritt 6 — cron-job.org einrichten

1. cron-job.org Account anlegen
2. **Job 1: Polling**
   - URL: `https://bahn-pb.onrender.com/poll?token=DEIN_CRON_TOKEN`
   - Schedule: alle 30 Minuten
   - Timezone: Europe/Berlin
3. **Job 2: Analyse**
   - URL: `https://bahn-pb.onrender.com/analyze?token=DEIN_CRON_TOKEN`
   - Schedule: täglich um 09:00
   - Timezone: Europe/Berlin

Beide Jobs aktivieren. Fertig.

### Schritt 7 — Excel abholen

Zwei Wege:
- Direkt aus dem Daten-Repo: `exports/bahn_verspaetungen.xlsx` → „Download raw file"
- Über die App: `https://bahn-pb.onrender.com/excel`

## Erwartetes Datenvolumen

Pro Tag etwa 30-50 Trips × ~30 Stops × eine Aktualisierung pro 30 min Poll = **~200–500 KB pro Tagesfile**. Über ein Jahr also rund **100 MB im Daten-Repo**. Das passt locker.

## Reset

Im Browser/curl:
```
curl -X POST -H "X-Admin-Token: DEIN_ADMIN_TOKEN" \
     "https://bahn-pb.onrender.com/reset?confirm=yes"
```

Setzt `incidents.json` auf leer zurück. Die Log-Dateien bleiben erhalten — du kannst Tage später jederzeit per `?date=YYYY-MM-DD` einzeln neu auswerten:
```
curl "https://bahn-pb.onrender.com/analyze?token=DEIN_CRON_TOKEN&date=2026-04-21"
```

## Was, wenn Render schläft?

Render Free schläft nach 15 min Inaktivität. cron-job.org pingt aber alle 30 min `/poll` — das weckt Render zuverlässig auf. Der erste Aufruf einer schlafenden Instanz dauert etwa 30-60 s, aber das ist für unseren Use-Case egal: cron-job.org wartet bis zu 30 s auf Antwort, der Poll selbst dauert dann 30-60 s. Falls cron-job.org einen Timeout macht, läuft der Poll auf Render trotzdem weiter und committet das Ergebnis.

## Lokale Entwicklung

```powershell
python -m pip install -r requirements.txt
python poll_day.py            # einmal pollen (lokale Daten)
python analyze_day.py 2026-04-21  # einen Tag analysieren
python generate_excel.py      # Excel bauen
python app.py                 # Flask lokal auf :5000
python test_pipeline.py       # alle Tests laufen lassen
```

Ohne `GITHUB_TOKEN` läuft alles im Local-Only-Modus. Daten landen direkt im Projektordner.

## Dateistruktur

```
bahn-pb/
├── app.py                  ← Flask-Backend
├── poll_day.py             ← Polling + Status-Maschine
├── analyze_day.py          ← tägliche Auswertung + Force-Finish
├── generate_excel.py       ← Excel-Export
├── storage.py              ← GitHub Contents API + lokaler Cache
├── timeutil.py             ← Logischer-Tag-Berechnung
├── config.json             ← Station, Produktfilter, Schwelle, Tagesgrenze
├── render.yaml             ← Render-Deployment-Config
├── requirements.txt
├── test_pipeline.py        ← Tests (10 Stück, alle grün)
├── logs/
│   └── YYYY-MM-DD.json    ← ein File pro logischem Tag
├── exports/
│   └── bahn_verspaetungen.xlsx  ← Auswertung zum Download
└── incidents.json          ← kumulative Vorfallsliste + Diagnose
```

## Excel-Spalten

**Sheet „Vorfälle":**
| Spalte | Bedeutung |
|---|---|
| Datum | logischer Tag (03:30-Grenze) |
| Zug | Linie (z. B. RE 11) |
| Richtung | Endziel laut Fahrplan |
| Startbahnhof | immer Paderborn Hbf |
| Soll-Abfahrt | Plan-Abfahrt in Paderborn |
| Zielbahnhof | bei Verspätung: Halt mit höchster bestätigter Verspätung; bei Teilausfall: letzter erreichter Halt |
| Soll-Ankunft / Ist-Ankunft | am Zielbahnhof |
| Verspätung (min) | bestätigte Ankunftsverspätung am Zielbahnhof |
| Kategorie | Verspätung / Teilausfall / Vollausfall |
| Ausfall ab | (nur bei Ausfällen) erster ausgefallener Halt |
| Erzwungen abgeschlossen | „ja" wenn der Trip beim Force-Finish noch `active` war (Datenqualität ggf. niedriger) |
| Hinweis | erläuternder Text |

**Sheet „Diagnose":** Trips, die zwar im Plan standen, aber nie Live-Daten lieferten. Meist Wochenend-/Feiertagsausfälle. Nicht als Vorfall, aber zur Kontrolle festgehalten.
