# Employee Planner (Node.js & MySQL)

Ein webbasiertes Dienstplan-Management-System auf Basis von Node.js, Express und MySQL. Das System ermöglicht die Verwaltung von Abteilungen, Mitarbeitern und geplanten Schichten vollständig über den Browser.

## Features

- **Dashboard** mit den wichtigsten Kennzahlen
- **Abteilungsverwaltung** mit Absicherung gegen versehentliches Löschen
- **Mitarbeiterverwaltung** inkl. Beschäftigungsart und Sollstunden/Monat
- **Dienstplanung** mit Monatsübersicht und Formular zur Schichterstellung
- **MySQL-Datenbank** als persistente Grundlage

## Voraussetzungen

- Node.js (>= 18)
- npm
- MySQL 8.x Server mit einem angelegten Schema (z. B. `employee_planner`)

## Installation

1. **MySQL Schema anlegen**

   ```sql
   CREATE DATABASE employee_planner CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
   ```

2. **Tabellen erstellen**

   ```bash
   mysql -u <USER> -p employee_planner < node_app/db/schema.sql
   ```

3. **Abhängigkeiten installieren**

   ```bash
   cd node_app
   npm install
   ```

4. **Konfigurationsdatei anlegen**

   ```bash
   cp .env.example .env
   # DB_HOST, DB_USER, DB_PASSWORD und DB_NAME entsprechend anpassen
   ```

5. **Entwicklungsserver starten**

   ```bash
   npm start
   ```

6. **Im Browser öffnen**

   ```
   http://localhost:3000
   ```

## Projektstruktur

```
node_app/
├── db/
│   └── schema.sql          # SQL-Schema für MySQL
├── public/
│   └── css/styles.css      # Statische Assets
├── server.js               # Express-Einstiegspunkt
├── package.json            # Node.js Abhängigkeiten
├── src/
│   ├── db/pool.js          # MySQL Connection Pool
│   ├── repositories/       # Datenbankabfragen
│   ├── routes/             # Express Router
│   └── views/              # EJS-Templates
└── .env.example            # Beispielkonfiguration
```

## Weiterentwicklung

- Erweiterung um Urlaubsverwaltung und Feiertagsplanung
- Benutzer- und Rechteverwaltung
- Automatisierte Schichtvorschläge auf Basis von Verfügbarkeiten

## Lizenz

Dieses Projekt ist für den internen Gebrauch entwickelt.
