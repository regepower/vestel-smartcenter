# Vestel / Telefunken SmartCenter – Protokoll

Quelle: `com.telefunken.smartcenter` 5.10.6 (Paket `com.vestel.supertvcommunicator`, nativer Teil `libvlcjni.so`), dekompiliert mit jadx 1.5.1, AES-Teil aus der x86-Variante von `libvlcjni.so` disassembliert.

Legende: ✅ = im Code eindeutig belegt · ⚠️ = Annahme/abgeleitet, am echten TV noch nicht verifiziert

## 1. TV-Generationen

| Klasse | Erkennung | Transport |
|---|---|---|
| **MB90** (alt) | UDP-Broadcast `vr_query_tv_version_782` → Port 4950, Antwort `vr_tv_query_rsp <port> <ver> <mac> <statusport> …` | TCP-Textprotokoll (`GETINFO ACTIVECHANNELLIST`, `VOLUME n`, `SETCHANNEL n` …) – **nicht implementiert** |
| **MB100 / DIAL 1.x** | SSDP | HTTP `GET/POST {Application-URL}vr/<cmd>`, Events über Websocket aus `GET vr/getwebsocketlink` |
| **MB100 / DIAL 2.0** (MB110/MB120/MB130 …) | SSDP, `<dial_version>2.0` | `POST {Application-URL}SmartCenter`, **Antworten + Events über `ws://<tv>:7681/`** |

## 2. Discovery ✅

```
M-SEARCH * HTTP/1.1
HOST: 239.255.255.250:1900
MAN: "ssdp:discover"
MX: 10
ST: urn:dial-multiscreen-org:service:dial:1
```
Antwort `LOCATION:` → `GET` liefert `dd.xml`; Header **`Application-URL`** = Basis-URL für alle Befehle.
In `dd.xml` liegt unter `<root><locale>`: `name, tv_version (mb100/mb97…), software_version, mac, dial_version, browser_type, is_3D_active, alexa_enabled, verificationKey, quiUI`.

Keine Authentifizierung. `verificationKey`/`smartpindisplay` zeigt nur eine PIN am TV an; die App prüft nichts gegen den TV.

## 3. Befehle

v1: `{AppURL}vr/<pfad>` · v2: `POST {AppURL}SmartCenter` mit Body; Abfragen in v2 als `<command>NAME</command>`, Antwort kommt als nächste Nicht-Event-Nachricht auf dem Websocket (App wartet bis 45 s).
XML-Deklaration: v1 `<?xml version="1.0" ?>`, v2 `<?xml version='1.0' ?>`.

| Funktion | v1 | v2 Body | Antwort |
|---|---|---|---|
| Senderliste | GET `vr/activechannellist` | `<command>activechannellist</command>` | `<active_list network='..'><service rsn name handle onid tsid sid type/>…` oder `channel_list_size = 0` |
| aktueller Sender | GET `vr/program` | `<command>program</command>` | `<service …/>` (nur Sender, **keine** Sendungsdaten) |
| Lautstärke lesen | GET `vr/volume` | `<command>volume</command>` | Zahl nach letztem `=` |
| UI-Status | GET `vr/tvstate` | `<command>tvstate</command>` | `<tv_state value='EPG|MEDIA_BROWSER|BACKLIGHT_OFF|STANDBY|OTHER'/>` |
| Erinnerungen | GET `vr/timerlist` | `<command>timerlist</command>` | `<timer id event_name start end repeat …/>` |
| Aufnahmen | GET `vr/recordlist` | `<command>recordlist</command>` | `<record …/>` |
| Portal-Apps | GET `vr/specialportalapps` | `<command>specialportalapps</command>` | `Netflix:1|PVR:1|…` |
| Taste | POST `vr/remote` | `<remote><key code='1012'/></remote>` | – |
| Lautstärke setzen | POST `vr/setvolume` | `<setvolume><volume level='NN' /></setvolume>` | – |
| Sender setzen | POST `vr/setcurrentchannel` | `<setcurrentchannel><channel rsn='N'/></setcurrentchannel>` | – |
| Browser-URL | POST `vr/browserseturl` | `<browserseturl><load url='…'/></browserseturl>` | Status |
| Tastatur | POST `vr/keyboard` | `<keyboard><key value='<Unicode-Codepoint dezimal>'/></keyboard>` | – |
| Maus/Touchpad | POST `vr/mouseevent` | `<mouseevent><event_data dx dy button/></mouseevent>` | – |
| Backlight | POST `vr/setdimmode` | `<setdimmode><dim_mode dim_value='low|medium|high|auto|off|eco'/></setdimmode>` | – |
| Sender umbenennen | POST `vr/changechannelinfo` | `<changechannelinfo><old_service rsn channel_name/><new_service …/></changechannelinfo>` | – |
| Timer anlegen | POST `vr/addtimer` / `vr/addrecord` | `<addtimer>`/`<addrecord>` s. u. | `…'XXXXXXXX'…` (ID) |
| Timer löschen | POST `vr/deletetimer` / `vr/deleterecord` | `<deletetimer><timer id='N'/></deletetimer>` | – |
| FollowTV start | GET `vr/startfollowtv` | `<command>startfollowtv</command>` | s. 5 |
| FollowTV stop | GET `vr/stopfollowtv` | ⚠️ App sendet in v2 nichts (Bug); wir senden `<command>stopfollowtv</command>` | – |
| Portal-URL | – | `<command>getportalurl</command>` | URL |
| PIN anzeigen | – | `<smartpindisplay><key timeout='N'/></smartpindisplay>` | – |

Timer-Body (Zeiten in **UTC**, Stunde ohne führende Null, `repeat` o/d/w, `change='a' playback='p'`):
```xml
<addtimer><service handle='12' rsn='2' event_name='Titel' onid='1' tsid='1011' sid='11110'/>
<start y='2026' m='10' d='18' h='18' min='15'/><end y='2026' m='10' d='18' h='19' min='45'/>
<pref repeat='o' change='a' playback='p'/></addtimer>
```
Zeitformat in Listen: `yyyy/MM/dd-HH:mm` (UTC).

**Einschalten:** Wake-on-LAN, App sendet Magic Packet unicast an `<tv-ip>:50`. **Ausschalten:** Taste 1012.

### Tastencodes ✅
POWER 1012 · HOME 1046 · MENU 1048 · QMENU 1043 · BACK 1010 · EXIT 1037 · OK 1053 · UP 1020 · DOWN 1019 · LEFT 1021 · RIGHT 1022 · VOL+ 1016 · VOL− 1017 · MUTE 1013 · P+ 1032 · P− 1033 · SOURCE 1056 · MMEDIA 1057 · INFO 1018 · EPG 1047 · TEXT 1255 · SUBTITLE 1031 · LANG 1015 · SCREEN 1011 · PRESETS 1014 · FAV 1040 · SLEEP 1042 · ROT 1055 · GRÜN 1054 · GELB 1050 · BLAU 1052 · PLAY 1025 · PAUSE 1049 · STOP 1024 · REC 1051 · REW 1027 · FF 1028 · SWAP/PREV 1034 · CHAN 1045 · 0–9 = 1000–1009

## 4. Events (Websocket) ✅
v2 in `<event>…</event>` verpackt, v1 roh:
`<tv_state value='…'/>`, `tv_status:0` (TV aus), `<channel_list_changed/>`, `<timer id=…>` / `<record id=…>` (Listen geändert), `keyboard_open_status:0|1|2`, `<open_browser_status value='0|1'/>`, `tv_portal_status:0|1`, `timeshift_started_status:0` (FollowTV beendet).

## 5. FollowTV

Antwort auf `startfollowtv`: `…_url:<URL>` (alles nach dem letzten `_url:`; MB230 real: `timeshift_url:rtsp://<tv>:8554/followTVlive|k1|k2|k3|k4|` mit abschließendem `|`) ✅ oder Fehler `followtv_started_status:N`:
3 Start fehlgeschlagen · 4 nicht erlaubt · 5 Aufnahme verschlüsselt · 6 Sender verschlüsselt · 7 Browser läuft · 8 Mediaplayer läuft · 9 Quelle nicht unterstützt · 10 FollowTV deaktiviert · 12 RTSP-HD-Inhalt

Wiedergabe in der App (libVLC):
* MB100/MB97: `:rtsp-http :rtsp-http-port=8554` → RTSP über HTTP-Tunnel (QuickTime-Stil) auf Port **8554** ✅
* MB90: HTTP-Stream ✅

**Verschlüsselung** ✅ (aus `FollowmeTVFragment` + gepatchtem `ts.c` in `libvlcjni.so`):
* URL hat dann die Form `rtsp://…|k1|k2|k3|k4` (je 8 Hex-Zeichen).
* `key = ror2(k4) ‖ ror2(k2) ‖ ror2(k3) ‖ ror2(k1)` – jedes Wort 32 Bit Big-Endian, um 2 Bit nach rechts rotiert → **AES-128**.
* App übergibt `--ts-csa-ck=<32 Hex>/decrypt` (MB100) bzw. ohne `/decrypt` (MB90). Der gepatchte TS-Demuxer liest 16 Bytes per `%2hhx`, und bei `/decrypt` wird AES-**Entschlüsselung**, sonst AES-**Verschlüsselung** angewendet.
* Paketgröße im gepatchten Demuxer fest **192 Byte**; jedes Paket wird in 12 Blöcken AES-ECB verarbeitet, das Ergebnis um 4 Byte nach vorne geschoben → 188-Byte-TS-Paket mit Sync 0x47.
* Stock-VLC kann das nicht (dessen `ts-csa-ck` ist DVB-CSA mit 8-Byte-Key) → Proxy nötig (in dieser Integration enthalten).
* ⚠️ Am echten TV noch nicht verifiziert: RTP-Payload-Grenzen = Paketgrenzen (Proxy resynct sonst automatisch), genaues HTTP-Tunnel-Verhalten des TV-Servers, Stop-Befehl in v2.

## 6. EPG
Die App holt das EPG **nicht vom TV**, sondern von Gracenote (`c13566208.ipg.web.cddbp.net/webapi/xml/1.0/`) – Dienst vermutlich abgeschaltet. Vom TV gibt es nur den aktuellen Sender (`program`), Timer- und Aufnahmelisten. Für ein EPG in Home Assistant eine externe Quelle (XMLTV o. ä.) nutzen.

## 7. Warum die App unter Android 15 nicht mehr läuft
* `targetSdkVersion 17` → Android 14 sperrt targetSdk < 23, Android 15 < 24. Umgehung per ADB: `adb install --bypass-low-target-sdk-block com-telefunken-smartcenter.apk` (Android 14+).
* Native Libs nur `armeabi`, `armeabi-v7a`, `x86` – **kein `arm64-v8a`**. Auf reinen 64-Bit-Geräten (z. B. Pixel 7 und neuer) lädt libVLC nicht → FollowTV geht dort grundsätzlich nicht.
