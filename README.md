# Virtual Organ – Armbian Image (Rpi4B)

## Was wurde vorbereitet
Das Image enthält unter `/opt/virtualorgan/` bereits die komplette Web-Anwendung
(Flask + FluidSynth-Engine) und die mitgelieferte `Organ.sf2`. Da dieses Image
nicht auf einem echten Raspberry Pi läuft (kein Netzwerk/chroot in dieser
Umgebung möglich), installiert sich das System **beim allerersten echten Boot**
selbst fertig (Paketinstallation via `virtualorgan-firstboot.service`):
fluidsynth, Python-Venv, Chromium, Cage (Kiosk-Compositor), Bluetooth/PulseAudio.
Das dauert je nach Internetverbindung 2–5 Minuten; danach startet der Webserver
automatisch und bleibt dauerhaft aktiv.

**Voraussetzung:** Der Pi braucht beim ersten Boot einen Internetzugang
(LAN-Kabel empfohlen, WLAN kann vorher über Armbian's `armbian-config`/
`/boot/armbian_first_run.txt` konfiguriert werden).

## Zugriff
- Weboberfläche: `http://virtualorgan.local:5000` (Hostname ist gesetzt)
- Standard-Login: `admin` / `virtualorgan` (bitte nach dem ersten Login ändern –
  ein eigenes Profil-Passwort-Änderungsformular kann leicht ergänzt werden;
  aktuell über „Einstellungen → Benutzer" neu anlegen und alten Admin löschen)
- Login ist standardmäßig **deaktiviert** (freier Zugriff); in
  „Einstellungen" aktivierbar.

## Features
- Bibliothek: MIDI/.kar/MusicXML hochladen, in Ordnern organisieren, gemeinsam
  oder persönlich (pro Benutzer)
- Wiedergabe über FluidSynth mit der Organ.sf2, Ausgabe wählbar: Web-Browser
  (Vorhören), Klinke (3.5mm), HDMI, Bluetooth, Netzwerk (PulseAudio)
- Mehrere externe MIDI-Controller gleichzeitig anschließbar, frei einzelnen
  Kanälen/Stimmen zuordenbar (`/midi`)
- Pro Stück speicherbare Stimmen-Einstellungen (Kanal→Programm), als Kopie
  sicherbar; globale Profile ebenfalls speicher-/ladbar; neu geladene Stimme
  fällt automatisch auf Standardwerte zurück
- Mehrbenutzerfähig mit Rollen (admin/user), Login optional erzwingbar
- HDMI-Kiosk-Modus (Cage + Chromium Vollbild) an/abschaltbar
- Boot-Zeit-Optimierungen: Bluetooth per Socket-Aktivierung, deaktivierte
  Wartezeiten (NetworkManager-wait-online, apt-daily-Timer), leiser Bootscreen

## Struktur
```
/opt/virtualorgan/app/          Flask-App (app.py, synth_engine.py, ...)
/opt/virtualorgan/soundfonts/   Organ.sf2
/opt/virtualorgan/library/      hochgeladene Dateien (shared/, users/<name>/)
/opt/virtualorgan/data/         SQLite-DB, Kiosk-Flag
/etc/systemd/system/            virtualorgan*.service Units
/usr/local/sbin/virtualorgan-firstboot.sh
```

## Erweiterungsideen (nicht enthalten, aber vorbereitet)
- Weitere Soundfonts hochladbar/wechselbar über „Einstellungen"
- Passwort-Änderung im eigenen Profil (aktuell nur durch Admin über „Einstellungen → Benutzer" setzbar)
- Web-MIDI-Keyboard direkt im Browser als zusätzlicher Eingang

## Änderungen (Bugfixes & Verbesserungen)
- **Fix:** „+ Neuer Ordner" verursachte einen Serverfehler (fehlende `library.mkdir()`-Funktion)
- **Fix:** Checkboxen in „Einstellungen" (Login erforderlich, Kiosk-Modus, Hall, Chorus) ließen sich
  nicht deaktivieren – ein Häkchen entfernen hatte keine Wirkung
- **Fix:** Der „Kiosk-Modus"-Schalter hatte keine Wirkung auf den tatsächlichen Kiosk-Dienst
  (DB-Einstellung und System-Flag-Datei waren nicht verknüpft); jetzt inkl. sofortigem Start/Stop
  über zwei eng begrenzte sudo-Rechte für den Webserver-Dienst (`/etc/sudoers.d/virtualorgan`)
- **Fix:** Robustere Pfad-Validierung in der Bibliothek (Ordner/Datei-Namen)
- **Fix:** Nicht erreichbares Ausgabegerät (z.B. Bluetooth aus) führte zu einer 500-Fehlerseite
  statt einer verständlichen Meldung
- **Fix:** Datei-Endungs-Prüfung beim Hochladen wird jetzt tatsächlich durchgesetzt
- **Fix (Registrierung/Stimmen):** MIDI-Dateien, deren Bank-Select-Werte nicht zum geladenen
  Soundfont passen (z.B. für ein anderes Instrument exportierte Dateien wie manche
  Hauptwerk/Kirchenorgel-MIDIs), landeten bisher lautlos auf Bank 0/Programm 0 ("Montre 8"),
  weil `fluidsynth.program_select()` bei unbekannter Bank einfach nichts tut. Es gibt jetzt einen
  Fallback: existiert die exakte Bank/Programm-Kombination nicht im Soundfont, wird automatisch
  dieselbe Programmnummer in Bank 0 versucht – das trifft in der Praxis sehr oft die richtige,
  eigentlich gemeinte Registrierung
- **Fix (Wiedergabe-Ruckler):** Web-Browser-Vorhören nutzte bisher aufeinanderfolgende
  `<audio>`-Elemente pro Segment, was beim Segmentwechsel zu einem hörbaren Klick/Ruckler führte.
  Wiedergabe läuft jetzt über die Web Audio API mit sample-genauer Segment-Verkettung (gapless)
- **Fix (Wiedergabe stoppt beim Seitenwechsel):** Bibliothek und Virtuelle Orgel wechseln jetzt per
  Soft-Navigation (Inhalt wird per JS ausgetauscht statt die Seite neu zu laden), wodurch die
  Wiedergabe beim Wechseln zwischen den beiden Seiten nicht mehr abbricht
- **Neu:** Echtes Play/Pause (nicht nur Stopp) für Bibliothek und Virtuelle Orgel, als
  gemeinsame, immer sichtbare Transportleiste unter der Navigation
- **Neu:** Admin kann Benutzerpasswörter direkt in „Einstellungen → Benutzer" setzen
- **Neu:** Schutz gegen versehentliches Löschen des eigenen Accounts oder des letzten Admins
- **Neu:** Sichtbare Fehlermeldungen (Toasts) statt lautlos fehlschlagender Aktionen im Browser
- **UI:** Dunkles Theme jetzt auch für Checkboxen/Regler, Tabellen scrollen auf schmalen Bildschirmen,
  Ausgabe-Auswahl gibt es nur noch einmal (statt getrennt auf Bibliothek/Orgel-Seite)
- **Fix (Visualizer zeigte überall "Montre 8"):** Der Registrierungs-Status wurde bei Web-Vorhören
  (Browser-Wiedergabe) nie an `/status` weitergegeben, nur bei Hardware-Wiedergabe – der Visualizer
  zeigte deshalb während des Vorhörens für alle Kanäle dieselbe (falsche) Registrierung
- **Neu (Manuale/Pedal folgen der Wiedergabe):** Die 3 Manual/Pedal-Bedienfelder auf der Orgel-Seite
  zeigen während einer laufenden Wiedergabe automatisch die tatsächlich aktiven MIDI-Kanäle inkl.
  Tastenanschlag-Visualisierung – synchron zum tatsächlich Hörbaren (auch beim Web-Vorhören, trotz
  Segment-Vorabpufferung). Zuordnung ist "klebrig" (kein Herumspringen) und die tiefste gerade aktive
  Stimme bekommt bevorzugt das Pedal. Spielen mehr als 3 Stimmen gleichzeitig (selten, aber möglich),
  werden die überzähligen als kompakte Liste unterhalb der Manuale angezeigt statt einfach zu fehlen.
  Ohne laufende Wiedergabe bleiben die Manuale wie gewohnt frei bedienbar
- **Neu (Dateimanagement wie ein echter Filemanager):** echte verschachtelte Ordner-Navigation mit
  Breadcrumbs (vorher nur eine flache Ebene), Mehrfachauswahl mit Sammel-Löschen/-Verschieben,
  Umbenennen, Drag&Drop-Upload direkt in den aktuellen Ordner, Sortierung nach Name/Datum/Größe
- **Fix (Manuale wirkten träge / Stimmen sprangen willkürlich):** Zwei Ursachen gefunden und behoben:
  1) Die Tastenanzeige aktualisierte sich nur alle 400ms zusammen mit der Server-Abfrage; läuft jetzt
     über eine eigene, von der Server-Abfrage entkoppelte 60fps-Anzeigeschleife.
  2) Viele Orgel-MIDI-Exporte (bestätigt an beiden Testdateien) duplizieren eine Stimme auf zwei
     MIDI-Kanäle (identische Noten/Zeitpunkte, z.B. für eine gekoppelte Zweitregistrierung). Diese
     Duplikate wurden bisher als zwei unabhängige Stimmen behandelt und haben zwei der drei
     Manual/Pedal-Plätze für eine einzige echte Stimme belegt – dadurch stritten sich die
     verbleibenden echten Stimmen ständig um den letzten freien Platz. Duplikate werden jetzt anhand
     eines Vergleichs der kompletten Notenzeitlinie erkannt und wie eine einzige Stimme behandelt
     (BWV552: 8 echte Stimmen statt 12 Kanäle; BWV565: 3 statt 7). Zusätzlich verhindert eine kurze
     Karenzzeit (1s), dass ein Platz bei einer kurzen Pause sofort neu vergeben wird
- **Neu (feste Kanal-/Register-Zuordnung pro Manual/Pedal):** Jedes Manual/Pedal hat jetzt eine
  „Automatisch"-Checkbox (Standard: an). Deaktiviert man sie, lässt sich dem Manual/Pedal eine feste
  Zuordnung zu einem oder mehreren MIDI-Kanälen geben (Mehrfachauswahl = Koppelfunktion, auch beim
  Live-Spiel: ein Tastendruck geht dann an alle zugeordneten Kanäle). Eine solche feste Zuordnung wird
  von der Automatik nie wieder übersteuert – behebt den Fall, dass ein Manual, das lange allein mit
  einem Register spielte, später plötzlich zu einem völlig anderen Kanal wechselt. Hinweis zur
  Reihenfolge: es gibt keine universelle Regel, dass „das obere Manual zuerst/hauptsächlich gespielt
  wird" – das hängt vom Stück und der Registriertradition ab; die Automatik ordnet daher nach Tonhöhe
  (tiefste aktive Stimme → Pedal), nicht nach einer angenommenen Manual-Hierarchie
