# Vestel / Telefunken SmartCenter für Home Assistant

Ersatz für die eingestellte App **SmartCenter** (`com.telefunken.smartcenter`, Vestel-Plattform MB100/MB110+): Steuerung, Senderliste, Timer/Aufnahmen und **FollowTV als Stream für VLC** – auch für AES-verschlüsselte Sender.
Protokolldetails: [PROTOCOL.md](PROTOCOL.md).

> Stand: aus der APK rekonstruiert und gegen einen Mock-TV getestet. Am echten TV noch nicht verifiziert → bitte zuerst mit dem CLI testen (unten).

## 1. Erst testen (CLI, PC im selben Netz)
```bash
pip install aiohttp cryptography
python tools/vestel_cli.py discover                  # TV suchen
python tools/vestel_cli.py <IP> info                 # dial_version 2.0 = neues Protokoll
python tools/vestel_cli.py <IP> channels
python tools/vestel_cli.py <IP> key VOL_UP
python tools/vestel_cli.py <IP> timers
python tools/vestel_cli.py <IP> followtv             # zeigt Stream-URL + VLC-Befehl
python tools/vestel_cli.py <IP> followtv --serve 8090 --vlc   # Proxy (entschlüsselt) + VLC
python tools/vestel_cli.py <IP> events               # Live-Events vom TV
```
Antwortet der TV nicht auf SSDP: `--location http://<IP>:<port>/dd.xml` (Port aus `discover` eines anderen Geräts / Router).
Bei Problemen `-v` anhängen und Log schicken.

## 2. Home Assistant
1. HACS → ⋮ → *Benutzerdefinierte Repositories* → `https://github.com/regepower/vestel-smartcenter` (Typ *Integration*) → installieren. Alternativ Ordner `custom_components/vestel_smartcenter` nach `/config/custom_components/` kopieren.
2. HA neu starten → *Integration hinzufügen* → „Vestel / Telefunken SmartCenter“ → IP eintragen oder leer lassen (Suche). TV muss dabei an sein.

Entitäten:
- `media_player.*` – Ein (WOL)/Aus, Lautstärke, Mute (Toggle), Senderwahl über `source`, P+/P−, Play/Pause/Stop, `play_media` (Sendername, Nummer oder URL)
- `remote.*` – beliebige Tasten: `POWER HOME MENU OK UP DOWN LEFT RIGHT BACK EXIT VOL_UP VOL_DOWN MUTE PROG_UP PROG_DOWN SOURCE EPG INFO TEXT RED GREEN YELLOW BLUE PLAY PAUSE STOP RECORD 0-9 …`
- `switch.*_followtv` – **Freigabe für FollowTV**, nach jedem HA-Start *aus*. Nur wenn an, dürfen Kamera/VLC FollowTV am TV starten. Schutz: max. 1 Start pro 30 s und 3 Starts pro 10 min, danach schaltet er sich selbst ab (zu viele Starts lassen manche TVs hängen/neu starten).
- `camera.*_followtv` – FollowTV-Livebild im Dashboard (Karte „Bild-Entität“ / „Picture Entity“, Kameraansicht *Live*). Bei eingeschaltetem Schalter: startet FollowTV beim Öffnen, stoppt ca. 30–40 s nach dem Schließen. Ton wird per ffmpeg nach AAC gewandelt (HA-Stream verwirft MP2/AC3 der Sender); im Player Lautsprecher-Symbol antippen. Attribut `vlc_url` = fertiger Link für VLC. Benötigt `stream` + `ffmpeg` (in `default_config` enthalten).
- `calendar.*_tv_timer` – Erinnerungen und geplante Aufnahmen als Kalender (inkl. täglich/wöchentlich)
- `button.*` – Tasten der Fernbedienung (weitere in der Entitätsliste aktivierbar) und **„FollowTV beenden“**
- `sensor.*_sender`, `*_erinnerungen`, `*_aufnahme_timer` (Liste in Attribut `items`), `*_tv_status`, `*_senderliste` (deaktiviert)

Aktionen (`vestel_smartcenter.*`): `add_timer`, `delete_timer`, `open_url`, `send_text`, `set_eco_mode`, `start_followtv`, `stop_followtv`, `refresh_channels` (Ziel: Vestel-Mediaplayer) sowie `send_raw` (Debug, ohne Ziel):
```yaml
action: vestel_smartcenter.send_raw
data:
  path: SmartCenter
  body: "<command>tvstate</command>"
```

Diagnose: *Integration → ⋮ → Diagnose herunterladen* enthält die letzten 60 Anfragen/Antworten des TVs.

## Blueprint: Strom für Aufnahmen bei Abwesenheit
Schaltet die TV-Steckdose 10 min vor einer geplanten Aufnahme ein, wenn du nicht zu Hause bist, und 10 min nach dem Ende wieder aus (nur wenn sie vorher aus war, du immer noch weg bist und keine weitere Aufnahme ansteht). Meldet beides aufs Handy.

Import: *Einstellungen → Automationen → Blueprints → Blueprint importieren* →
`https://github.com/regepower/vestel-smartcenter/blob/main/blueprints/automation/vestel_smartcenter/recording_power.yaml`

Timer und Aufnahmen werden in HA gespeichert und bleiben auch bei ausgeschaltetem TV/Strom und nach Neustarts erhalten (Stand der letzten Verbindung zum TV).

## 3. FollowTV in VLC
```yaml
action: vestel_smartcenter.start_followtv
target:
  entity_id: media_player.telefunken
response_variable: ft
```
`start_followtv` schaltet die Freigabe ein (startet selbst nichts am TV). Antwort `proxy_url` (oder Attribut `vlc_url` der FollowTV-Kamera) → in VLC öffnen (*Medien → Netzwerkstream*):
`http://<HA>:8123/api/vestel_smartcenter/followtv/<entry_id>.ts?token=…`
- HA holt den Stream vom TV (RTSP über HTTP, Port 8554), entschlüsselt ggf. AES und liefert normales MPEG-TS. Mehrere Clients gleichzeitig möglich; 5 s nach dem letzten Client wird FollowTV beendet (sofort: Button „FollowTV beenden“).
- Die `rtsp://…:8554/followTVlive|…|…|…|…|`-Adresse des TVs ist AES-verschlüsselt (die Hex-Blöcke sind der Schlüssel) → in VLC nicht abspielbar, immer `proxy_url` nehmen. Nur unverschlüsselt (`direct_url` gesetzt) geht direkt: `vlc --rtsp-http --rtsp-http-port=8554 "<direct_url>"`.
- Token schützt die URL (VLC kann keinen HA-Login). Nicht nach außen freigeben.
- Optionen der Integration: RTSP-über-HTTP-Port (0 = reines RTSP).

## 4. Grenzen
- **EPG**: kommt in der App nicht vom TV, sondern von Gracenote (Cloud, vermutlich abgeschaltet). Vom TV: nur aktueller Sender, Timer, Aufnahmen.
- Mute-Status und Ein-Zustand liefert der TV nicht direkt (Ein = DIAL antwortet und Status ≠ STANDBY).
- Alte MB90-Geräte (TCP-Protokoll) nicht unterstützt. Getestet: Telefunken MB230 (DIAL 2.0).
- Der SmartCenter-Dienst mancher TVs hängt sich bei vielen schnellen Anfragen auf (nimmt POSTs an, antwortet nicht mehr) – hilft nur Strom trennen. Die Integration begrenzt deshalb Anfragen und baut die Verbindung bei fehlenden Antworten neu auf. Bei Hängern: Diagnose herunterladen und im Issue anhängen.
- Original-App unter Android 14/15: `adb install --bypass-low-target-sdk-block com-telefunken-smartcenter.apk`; FollowTV in der App geht auf reinen 64-Bit-Geräten trotzdem nicht (keine arm64-Libs).

## Entwicklung
```bash
pip install aiohttp cryptography pytest pytest-asyncio ruff mypy
ruff check . && mypy custom_components tools tests && pytest           # Bibliothek + Mock-TV
pip install pytest-homeassistant-custom-component
pytest tests_ha -o asyncio_mode=auto -p no:cacheprovider                # HA-Integration
```
