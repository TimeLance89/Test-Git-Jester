# Employee Planner - Dienstplan Management System

Ein webbasiertes System zur Verwaltung von Mitarbeitern, Dienstplänen und Arbeitszeiten.

## Features

- **Mitarbeiterverwaltung**: Vollzeit- und Teilzeitmitarbeiter mit individuellen Arbeitszeiten
- **Dienstplanung**: Monatliche Schichtplanung mit visueller Übersicht
- **Abteilungsverwaltung**: Organisation von Mitarbeitern in Abteilungen
- **Urlaubsverwaltung**: Antragstellung und Genehmigung von Urlaubszeiten
- **Gesperrte Tage**: Verwaltung von Feiertagen und besonderen Ereignissen
- **Responsive Design**: Optimiert für Desktop, Tablet und Mobile

## Installation

1. **Abhängigkeiten installieren:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Datenbank initialisieren:**
   ```bash
   python init_db.py
   ```

3. **Anwendung starten:**
   ```bash
   python app.py
   ```

4. **Im Browser öffnen:**
   ```
   http://localhost:5001
   ```

## Projektstruktur

```
employee_planner/
├── app.py                 # Haupt-Flask-Anwendung
├── models.py              # Datenbankmodelle
├── auto_schedule.py       # Automatische Schichtplanung
├── init_db.py            # Datenbank-Initialisierung
├── requirements.txt       # Python-Abhängigkeiten
├── install.sh            # Installations-Script
├── static/               # CSS und statische Dateien
├── templates/            # HTML-Templates
└── instance/             # Datenbank-Dateien
```

## Vollzeit vs. Teilzeit Mitarbeiter

Das System unterscheidet automatisch zwischen Vollzeit- und Teilzeitmitarbeitern:

- **Vollzeit**: Mitarbeiter mit ≥160 Stunden/Monat
  - Vereinfachte Stundenanzeige im Dienstplan
  - Keine Soll-/Reststunden-Anzeige in der Mitarbeiterliste

- **Teilzeit**: Mitarbeiter mit <160 Stunden/Monat
  - Detaillierte Stundenübersicht
  - Soll-/Reststunden-Tracking

## Technologie

- **Backend**: Flask (Python)
- **Database**: SQLite
- **Frontend**: Bootstrap 5, HTML5, CSS3
- **Template Engine**: Jinja2

## Lizenz

Dieses Projekt ist für den internen Gebrauch entwickelt.
