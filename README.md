# WhatsApp Live Archive

Lokales Archiv für WhatsApp-Ereignisse eines verknüpften Geräts. Die Bridge übernimmt verfügbare Nachrichten, Kontakte und Medien automatisch; der Viewer aktualisiert sich nach verarbeiteten Live-Ereignissen.

## Voraussetzungen und Start

Einmalig Docker Desktop (macOS/Windows) oder Docker Engine mit Docker Compose v2 (Linux) installieren und starten. Die Skripte installieren keine Host-Software.

```sh
./start.sh
```

Der erste Start baut das Anwendungsimage und legt `data/` für Datenbank, Live-Events, Medien und die verknüpfte Sitzung an. Der Viewer ist standardmäßig nur unter `http://127.0.0.1:8000` erreichbar. Ein anderer Port kann in `.env` als `VIEWER_PORT=8010` oder direkt in der Umgebung gesetzt werden. Bei belegtem Port wird kein fremder Prozess beendet.

Unter Linux `./start.sh` als normaler Benutzer ausführen, nicht mit `sudo`. Die Container schreiben mit der Benutzer-ID des aufrufenden Benutzers in `data/`. Wurde der Ordner zuvor von Docker oder durch einen Start mit `sudo` als `root` angelegt, kann die Bridge wegen fehlender Schreibrechte als „unhealthy“ ausfallen. Eigentümer einmalig korrigieren und erneut starten:

```sh
sudo chown -R "$(id -u):$(id -g)" data
./start.sh
```

Dabei bleiben die Archivdateien erhalten.

Beenden, ohne Archivdaten zu löschen:

```sh
./stop.sh
```

## WhatsApp verknüpfen

Beim Start läuft zusätzlich ein interner Bridge-Dienst auf Basis der inoffiziellen Bibliothek Baileys. Sobald der QR-Code im lokalen Viewer erscheint, kann das Telefon unter **WhatsApp → Verknüpfte Geräte** gekoppelt werden. Der QR-Code ist ein Anmeldeschlüssel: nur selbst im lokalen Viewer scannen und niemals teilen. Es werden keine Zugangsdaten abgefragt und der Bridge-Dienst bietet keine Funktion zum Senden von Nachrichten.

Nach der Kopplung fordert die Bridge mit `syncFullHistory` die von WhatsApp verfügbare Chat-Historie an und übernimmt danach neue Nachrichten. Bearbeitungen und Löschungen, die als WhatsApp-Ereignis eintreffen, werden als frühere Fassung beziehungsweise Löschmarkierung erhalten. Medien bis 200 MiB werden lokal gespeichert; Nachrichten, Medien und Rohdaten liegen unter `data/`. Die lokale Bridge-Sitzung liegt unter `data/whatsapp-auth/` und erlaubt die Wiederverbindung ohne erneutes Scannen. `data/` vollständig schützen und sichern.

Wenn ein Bild, Video, Audio, Dokument oder Sticker beim ersten Empfang nicht lokal gespeichert werden konnte, kann die Datei in der Chatansicht gezielt erneut angefordert werden. Dies umfasst auch runde Videonachrichten, animierte Sticker, separat übermittelte Albumelemente und verfügbare Link-Vorschaubilder. Zuerst wird die gespeicherte Downloadadresse versucht; nur bei einem entsprechenden WhatsApp-Downloadfehler wird ein Reupload vom Telefon angefordert. Bei Gruppennachrichten wird dabei auch die außerhalb des Schlüssels gespeicherte Teilnehmerkennung berücksichtigt. Das Hauptgerät muss dafür eingeschaltet und online sein. Das funktioniert nur, solange WhatsApp oder das Telefon die Datei für die verknüpfte Sitzung noch bereitstellen. Fehler werden direkt beim Medium angezeigt. Bereits vorhandene Archivdateien werden nicht überschrieben. Fehlende Downloads werden nicht automatisch gesammelt erneut angefordert.

Beim Wechseln zwischen Chats bleibt die Scrollposition der eigenständig scrollbar angezeigten Chatliste erhalten; der Hauptbereich beginnt beim neuen Chat wieder oben. Antworten auf einen WhatsApp-Status werden im Gespräch als solche gekennzeichnet; verfügbarer zitierter Status-Text und bereits archivierte Status-Bilder werden direkt am Antwortbeitrag angezeigt.

Neue Mediendownloads werden vor dem Speichern mit der übermittelten WhatsApp-SHA-256-Prüfsumme und Dateigröße verglichen, sofern diese Metadaten vorhanden sind. Abweichungen werden als Fehler behandelt, nicht als erfolgreiche Sicherung.

Für eine vollständige Medienprüfung müssen auch Downloadverweise in zitierten Nachrichten und Link-Favicons berücksichtigt werden. Der lokale Retry-Endpunkt akzeptiert dazu einen `part`-Queryparameter mit dem aus den archivierten Rohdaten ermittelten Feldpfad. Zitatdateien werden mit ihrer ursprünglichen Nachrichtenkennung angefordert, aber als zusätzliche Datei an der zitierenden Archivnachricht gespeichert; es werden keine ursprünglichen Nachrichten oder Zeitstempel erfunden. Bereits archivierte Dateien mit derselben Prüfsumme müssen nicht erneut geladen werden. Ein Album enthält selbst keine Datei: Seine angekündigten Elemente müssen als separate Nachrichten vorliegen.

Der Reupload-Fallback berücksichtigt HTTP 403, 404 und 410 sowie das von Baileys verwendete Fehlerfeld `output.statusCode`. Pro Downloadversuch wird höchstens ein Reupload angefordert; andere Fehler werden nicht durch wiederholte Reuploads verdeckt.

Die Antwort auf eine Reupload-Anforderung wird höchstens 60 Sekunden abgewartet. Bei Zeitüberschreitung oder Verbindungsabbruch werden die zugehörigen Ereignislistener entfernt und der Versuch freigegeben. Eine ausbleibende Telefonantwort blockiert dadurch keine weiteren Versuche. Auch ein auf dem Telefon abspielbares Video ist damit noch nicht garantiert für das verknüpfte Gerät abrufbar.

Baileys `7.0.0-rc14` ist nicht offiziell mit WhatsApp verbunden oder von Meta unterstützt. Die Bibliothek kann nach Änderungen am WhatsApp-Web-Protokoll ausfallen; WhatsApp garantiert keine vollständige Synchronisierung der gesamten Vergangenheit. Der erste History-Sync ist daher best effort. Die Session nur für das eigene Konto verwenden; das lokale Konto kann durch WhatsApp abgemeldet oder eingeschränkt werden. Für einen isolierten Verbindungstest kann `WHATSAPP_BRIDGE_MODE=mock` gesetzt werden; damit wird kein QR erzeugt und kein Konto kontaktiert.

## Grenzen und Datenschutz

Nur Ereignisse, die WhatsApp an das verknüpfte Gerät liefert, werden archiviert. Bereits vorhandene Historie hängt davon ab, was WhatsApp beim Verknüpfen synchronisiert; vollständige rückwirkende Sicherung kann nicht garantiert werden. Fehlende Ereignisse werden nicht als Löschungen gedeutet. Das Archiv kann nicht zurück in WhatsApp eingespielt werden.

Absender von Gruppennachrichten werden auch dann aus der Teilnehmerkennung ermittelt, wenn diese im History-Sync außerhalb des Nachrichtenschlüssels liegt. Bei älteren Archivdatensätzen korrigiert der Viewer die Anzeige aus den erhaltenen Rohdaten, ohne die gespeicherten Nachrichtenidentitäten zu ändern. Ein vorhandener Einzelchat derselben Person liefert den Anzeigenamen auch ohne Telefonnummernzuordnung.

Manuell vergebene Anzeigenamen werden als lokale Aliase getrennt vom WhatsApp-Namen gespeichert und haben in Chatliste, Überschrift und Absenderanzeige Vorrang. WhatsApp-Ereignisse überschreiben sie nicht. Während der Alias-Eingabe oder des Speicherns wird der automatische Nachrichten-Refresh ausgesetzt, damit ungespeicherte Eingaben erhalten bleiben. Vor dieser Trennung gespeicherte Namen sind nicht zuverlässig als manuell oder von WhatsApp vergeben unterscheidbar; gewünschte Aliase einmal erneut speichern.

Der Profilname einer selbst gesendeten Nachricht wird nicht als Name des Gesprächspartners übernommen. Ebenso wird der Profilname eines Gruppenteilnehmers nicht zum Gruppennamen. Bereits falsch übernommene Namen werden nicht anhand einer Vermutung automatisch umbenannt.

Archivdaten einschließlich Chattexten und WhatsApp-Rohdaten liegen lokal und unverschlüsselt unter `data/`. Die Anwendung hat standardmäßig keine Anmeldung und darf nicht ungeschützt ins öffentliche Netzwerk gestellt werden. `data/` enthält persönliche Daten und ist vom Git- sowie Docker-Build-Kontext ausgeschlossen. Für eine konsistente Dateikopie den Stack zuerst mit `./stop.sh` beenden; `data/` anschließend vollständig sichern.

## Archivschutz beim Hauptgerätewechsel

Das Archiv ist unabhängig vom Chatbestand des Telefons. Auch eine leere Historie auf einem neuen Hauptgerät entfernt keine bereits archivierten Chats. Historien-Replays ergänzen fehlende Nachrichten und Medien, ersetzen aber keine bereits gespeicherten Nachrichteninhalte. Änderungen aus Live-Ereignissen erhalten vorherige Inhalte automatisch als frühere Fassungen; WhatsApp-Löschungen bleiben Markierungen. Frühere Fassungen sind auch durchsuchbar. Alle Nachrichten eines Chats sind über **Ältere Nachrichten** erreichbar, auch ohne aktive WhatsApp-Verbindung.

Die SQLite-Datenbank blockiert physische Löschungen von Archivdatensätzen sowie Änderungen an Nachrichtenidentitäten, Medienreferenzen und früheren Fassungen. Die Datei `data/archive.db.identity` gehört zur Datenbank: Fehlt bei vorhandenem Identitätsnachweis die Datenbank oder stimmt ihre Identität nicht überein, wird der Start abgebrochen, statt ein leeres Archiv anzulegen. Beim ersten Start mit dieser Version wird eine bestehende Datenbank ohne diesen Nachweis übernommen. Kontakt-/Telefonnummernzuordnungen werden dauerhaft im Archiv gespeichert, sobald sie bekannt werden; fehlende frühere Zuordnungen können nicht nachträglich garantiert rekonstruiert werden.

**Vor dem Handywechsel im Viewer „Archiv absichern“ ausführen und den Erfolg abwarten.** Der Archiver kann einen bevorstehenden Telefonwechsel nicht automatisch erkennen. Die Funktion:

- erstellt über die SQLite-Backup-API einen konsistenten Datenbankstand;
- kopiert alle darin referenzierten Medien und die zu Beginn vorhandenen, noch nicht importierten Ereignisse;
- prüft Datenbankintegrität, Fremdschlüssel, Archividentität, Datensatzinventar und SHA-256-Prüfsummen;
- veröffentlicht erst danach eine neue Sicherung unter `data/backups/<Sicherungsname>/`;
- überschreibt oder löscht keine vorherige erfolgreiche Sicherung.

Die Ereignisverarbeitung wartet während der Sicherung; die Bridge kann weitere Ereignisse in die Warteschlange schreiben. Später eintreffende Ereignisse gehören zum nächsten Sicherungsstand. Die Anwendung ist für einen einzelnen Viewer-Prozess mit einem Ereignis-Importer ausgelegt; mehrere Worker/Importer auf demselben Datenverzeichnis werden nicht unterstützt.

**Neue lokale Kopplung** erstellt automatisch eine frische geprüfte Sicherung. Bei fehlenden/beschädigten referenzierten Medien, unzureichendem Speicherplatz oder anderen Sicherungsfehlern wird die Neukopplung blockiert und ein Fehler angezeigt. Die Bridge prüft den Sicherungsnachweis zusätzlich vor dem Entfernen ihrer Authentifizierungsdaten. Sicherungen enthalten keine WhatsApp-Anmeldeschlüssel; eine Wiederherstellung des Archivs benötigt keine funktionierende Sitzung.

**Wichtig:** Eine Sicherung auf derselben Festplatte schützt nicht vor deren Ausfall. Den vollständigen Sicherungsordner zusätzlich auf einen unabhängigen, vorzugsweise verschlüsselten Datenträger kopieren und dort prüfen. Sicherungen sind unverschlüsselt und werden nicht automatisch ausgedünnt; ausreichend Speicherplatz bereithalten. Die Anzeige nennt den letzten erfolgreich geprüften Sicherungszeitpunkt, nicht eine Garantie für den aktuellen Zustand aller Dateien.

### Sicherung prüfen und isoliert wiederherstellen

Auf einem Rechner mit den Python-Projektabhängigkeiten können Sicherungen über die Kommandozeile geprüft werden:

```sh
python -m whatsapp_archive.protection verify /pfad/zur/kopierten/sicherung
python -m whatsapp_archive.protection restore /pfad/zur/kopierten/sicherung --destination /pfad/zum/neuen/archiv
```

Die Wiederherstellung prüft Quelle und wiederhergestellte Kopie und erlaubt ausschließlich ein **noch nicht vorhandenes Zielverzeichnis**. Das bestehende oder laufende Archiv wird niemals überschrieben. Das Ergebnis enthält Datenbank, Identitätsdatei, Medien und ausstehende Ereignisse. Eine verlorene Anmeldung ist für den Zugriff auf gespeicherte Inhalte nicht erforderlich.

Diese Absicherung gilt für bereits gespeicherte Inhalte mit der standardmäßig verwendeten, dateibasierten SQLite-Datenbank. Sie ersetzt weder eine unabhängige Datenträgersicherung noch garantiert sie Nachrichten oder Medien, die WhatsApp dem Archiver nie geliefert hat. Betriebssystemzugriffe, manuelles Löschen kompletter Datenverzeichnisse und Hardwareausfälle können durch Datenbankregeln allein nicht verhindert werden.

## Entwicklung

```sh
python -m pip install -e . pytest
PYTHONPATH=src python -m pytest tests -q
npm test --prefix bridge
```