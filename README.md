# WhatsApp Live Archive

Lokales Archiv für WhatsApp-Ereignisse eines verknüpften Geräts. Die Bridge übernimmt verfügbare Nachrichten, Kontakte und Medien automatisch; der Viewer aktualisiert sich nach verarbeiteten Live-Ereignissen.

## Voraussetzungen und Start

Einmalig Docker Desktop (macOS/Windows) oder Docker Engine mit Docker Compose v2 (Linux) installieren und starten. Die Skripte installieren keine Host-Software.

```sh
./start.sh
```

Der erste Start baut das Anwendungsimage und legt `data/` für Datenbank, Live-Events, Medien und die verknüpfte Sitzung an. Der Viewer ist standardmäßig nur unter `http://127.0.0.1:8000` erreichbar. Ein anderer Port kann in `.env` als `VIEWER_PORT=8010` oder direkt in der Umgebung gesetzt werden. Bei belegtem Port wird kein fremder Prozess beendet.

Beenden, ohne Archivdaten zu löschen:

```sh
./stop.sh
```

## WhatsApp verknüpfen

Beim Start läuft zusätzlich ein interner Bridge-Dienst auf Basis der inoffiziellen Bibliothek Baileys. Sobald der QR-Code im lokalen Viewer erscheint, kann das Telefon unter **WhatsApp → Verknüpfte Geräte** gekoppelt werden. Der QR-Code ist ein Anmeldeschlüssel: nur selbst im lokalen Viewer scannen und niemals teilen. Es werden keine Zugangsdaten abgefragt und der Bridge-Dienst bietet keine Funktion zum Senden von Nachrichten.

Nach der Kopplung fordert die Bridge mit `syncFullHistory` die von WhatsApp verfügbare Chat-Historie an und übernimmt danach neue Nachrichten. Bearbeitungen und Löschungen, die als WhatsApp-Ereignis eintreffen, werden als frühere Fassung beziehungsweise Löschmarkierung erhalten. Medien bis 200 MiB werden lokal gespeichert; Nachrichten, Medien und Rohdaten liegen unter `data/`. Die lokale Bridge-Sitzung liegt unter `data/whatsapp-auth/` und erlaubt die Wiederverbindung ohne erneutes Scannen. `data/` vollständig schützen und sichern.

Wenn eine Videodatei beim ersten Empfang nicht lokal gespeichert werden konnte, kann sie in der Chatansicht gezielt erneut angefordert werden. Das funktioniert nur, solange WhatsApp die Datei für die verknüpfte Sitzung noch bereitstellt.

Baileys `7.0.0-rc14` ist nicht offiziell mit WhatsApp verbunden oder von Meta unterstützt. Die Bibliothek kann nach Änderungen am WhatsApp-Web-Protokoll ausfallen; WhatsApp garantiert keine vollständige Synchronisierung der gesamten Vergangenheit. Der erste History-Sync ist daher best effort. Die Session nur für das eigene Konto verwenden; das lokale Konto kann durch WhatsApp abgemeldet oder eingeschränkt werden. Für einen isolierten Verbindungstest kann `WHATSAPP_BRIDGE_MODE=mock` gesetzt werden; damit wird kein QR erzeugt und kein Konto kontaktiert.

## Grenzen und Datenschutz

Nur Ereignisse, die WhatsApp an das verknüpfte Gerät liefert, werden archiviert. Bereits vorhandene Historie hängt davon ab, was WhatsApp beim Verknüpfen synchronisiert; vollständige rückwirkende Sicherung kann nicht garantiert werden. Fehlende Ereignisse werden nicht als Löschungen gedeutet. Das Archiv kann nicht zurück in WhatsApp eingespielt werden.

Archivdaten einschließlich Chattexten und WhatsApp-Rohdaten liegen lokal und unverschlüsselt unter `data/`. Die Anwendung hat standardmäßig keine Anmeldung und darf nicht ungeschützt ins öffentliche Netzwerk gestellt werden. `data/` enthält persönliche Daten und ist vom Git- sowie Docker-Build-Kontext ausgeschlossen. Für eine konsistente Dateikopie den Stack zuerst mit `./stop.sh` beenden; `data/` anschließend vollständig sichern.

## Entwicklung

```sh
python -m pip install -e .
PYTHONPATH=src python -m unittest discover -s tests -v
```