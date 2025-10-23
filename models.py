"""Data model definitions for the Einsatzplaner.

Dieses Modul definiert die Datenbanktabellen für den
Mitarbeiter‑ und Einsatzplaner. Die Tabellen basieren auf
SQLAlchemy und stellen die Grundlage für die gesamte Anwendung dar.
"""

from datetime import date, datetime
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text
from sqlalchemy.exc import NoSuchTableError, OperationalError, ProgrammingError

# Die SQLAlchemy‑Instanz wird in app.py initialisiert und hier importiert.
db = SQLAlchemy()


class Department(db.Model):
    """Abteilungen oder Bereiche, in denen Mitarbeiter eingeplant werden.

    Jede Abteilung hat einen Namen, eine frei wählbare Farbe und einen
    Bereichsbezeichner. Die Farbe kann in der Oberfläche zur
    optischen Unterscheidung verschiedener Bereiche verwendet werden.
    """

    __tablename__ = "department"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    color = db.Column(db.String(20), nullable=True)
    area = db.Column(db.String(120), nullable=True)

    employees = db.relationship("Employee", backref="department", lazy=True)

    def __repr__(self) -> str:
        return f"<Department {self.name}>"


class Employee(db.Model):
    """Mitarbeiter, die in den Dienstplan eingetragen werden.

    Neben dem Namen und einer optionalen Personalnummer können auch
    monatlich verfügbare Stunden hinterlegt werden. Diese Angabe
    dient zur Kontrolle der Arbeitsbelastung. Jeder Mitarbeiter
    gehört einer Abteilung an.
    """

    __tablename__ = "employee"

    id = db.Column(db.Integer, primary_key=True)
    employee_number = db.Column(db.String(50), unique=True, nullable=True)
    name = db.Column(db.String(120), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"), nullable=True)
    monthly_hours = db.Column(db.Float, nullable=True)

    # Neues Feld für ein individuelles Mitarbeiterkürzel. Dieses Kürzel
    # dient dazu, im Dienstplan eine kurze Bezeichnung des Mitarbeiters
    # anzeigen zu können (z.B. Initialen). Das Feld ist optional.
    short_code = db.Column(db.String(20), nullable=True)

    # Felder für die Benutzerverwaltung: Jeder Mitarbeiter kann sich
    # anmelden. Dafür wird ein eindeutiger Benutzername sowie ein
    # gehashter Passwortstring gespeichert. Die Rolle "is_admin"
    # kennzeichnet Administratoren, die erweiterte Rechte besitzen.
    username = db.Column(db.String(120), unique=True, nullable=True)
    password_hash = db.Column(db.String(200), nullable=True)
    is_admin = db.Column(db.Boolean, default=False)

    preferred_schedule_view = db.Column(db.String(20), nullable=False, default="month")

    # Zusätzliche Profildaten
    email = db.Column(db.String(120), nullable=True)
    phone = db.Column(db.String(40), nullable=True)
    position = db.Column(db.String(120), nullable=True)

    # Standard-Arbeitszeiten für automatische Schichtenerstellung
    default_daily_hours = db.Column(db.Float, nullable=True)
    default_work_days = db.Column(db.String(20), nullable=True)  # e.g., "0,1,2,3,4" for Mon-Fri

    shifts = db.relationship("Shift", backref="employee", lazy=True)
    leaves = db.relationship("Leave", backref="employee", lazy=True)
    notifications = db.relationship(
        "Notification",
        backref="recipient",
        lazy=True,
        cascade="all, delete-orphan",
    )

    def set_password(self, password: str) -> None:
        """Speichert das Passwort sicher als Hash.

        Diese Methode nutzt die Werkzeug‑Funktion generate_password_hash,
        um das Passwort vor dem Ablegen in der Datenbank zu verschlüsseln.
        """
        from werkzeug.security import generate_password_hash
        if password:
            self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        """Überprüft, ob das gegebene Passwort zum gespeicherten Hash passt."""
        from werkzeug.security import check_password_hash
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    def __repr__(self) -> str:
        return f"<Employee {self.name}>"


class Shift(db.Model):
    """Ein geplanter Arbeitseinsatz an einem bestimmten Tag.

    Ein Einsatz besteht aus dem Datum, der Anzahl der Stunden
    und einer Bezeichnung (z.B. Frühschicht, Spätschicht, etc.).
    """

    __tablename__ = "shift"

    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    date = db.Column(db.Date, nullable=False)
    hours = db.Column(db.Float, nullable=False)
    shift_type = db.Column(db.String(50), nullable=True)

    # Schichten können durch einen Administrator genehmigt werden. Solange
    # diese Flagge False ist, gilt der Einsatz als nicht freigegeben und wird
    # normalen Mitarbeitern nicht im Dienstplan angerechnet.
    approved = db.Column(db.Boolean, default=False)

    def __repr__(self) -> str:
        return f"<Shift {self.date} {self.hours}h>"


class Leave(db.Model):
    """Abwesenheiten wie Urlaub, Krankheit oder Fortbildungen.

    Für Urlaub kann der Genehmigungsstatus gespeichert werden.
    """

    __tablename__ = "leave"

    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    leave_type = db.Column(db.String(50), nullable=False)  # z.B. Urlaub, Krank, ÜSA
    approved = db.Column(db.Boolean, default=False)
    notes = db.Column(db.Text, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<Leave {self.leave_type} {self.start_date}–{self.end_date} "
            f"{('approved' if self.approved else 'pending')}>"
        )


class ProductivitySettings(db.Model):
    """Produktivitätseinstellungen für die Berechnung der Teile.

    Diese Tabelle speichert die konfigurierbaren Produktivitätswerte,
    die zur Berechnung der erwarteten Teileanzahl verwendet werden.
    Formel: Teile = Gesamt Stunden × Produktivität
    """

    __tablename__ = "productivity_settings"

    id = db.Column(db.Integer, primary_key=True)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"), nullable=True)
    productivity_value = db.Column(db.Float, nullable=False, default=40.0)
    created_date = db.Column(db.Date, nullable=False, default=lambda: date.today())
    is_active = db.Column(db.Boolean, default=True)
    notes = db.Column(db.Text, nullable=True)

    def __repr__(self) -> str:
        return f"<ProductivitySettings {self.productivity_value}>"


class WorkClass(db.Model):
    """Beschreibt eine Arbeitszeit-Klassifikation wie Vollzeit oder Teilzeit."""

    __tablename__ = "work_class"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    hours_per_week = db.Column(db.Float, nullable=True)
    hours_per_month = db.Column(db.Float, nullable=True)
    description = db.Column(db.Text, nullable=True)
    color = db.Column(db.String(20), nullable=True)
    is_default = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    def __repr__(self) -> str:
        return f"<WorkClass {self.name}>"


class BlockedDay(db.Model):
    """Gesperrte Tage wie Feiertage oder Betriebsferien.

    Diese Tabelle speichert Tage, an denen keine Schichten eingeplant
    werden sollen. Dies können Feiertage, Betriebsferien oder andere
    besondere Tage sein. Gesperrte Tage werden im Dienstplan
    entsprechend markiert und berücksichtigt.
    """

    __tablename__ = "blocked_day"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, unique=True)
    name = db.Column(db.String(120), nullable=False)  # z.B. "Weihnachten", "Betriebsferien"
    description = db.Column(db.Text, nullable=True)
    block_type = db.Column(db.String(50), nullable=False, default="Feiertag")  # Feiertag, Betriebsferien, Sonstiges
    created_by = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=True)
    created_date = db.Column(db.Date, nullable=False, default=lambda: date.today())

    def __repr__(self) -> str:
        return f"<BlockedDay {self.date} {self.name}>"


class Notification(db.Model):
    """Benachrichtigung für einen Benutzer der Anwendung."""

    __tablename__ = "notification"

    id = db.Column(db.Integer, primary_key=True)
    recipient_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    message = db.Column(db.String(255), nullable=False)
    link = db.Column(db.String(255), nullable=True)
    is_read = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self) -> str:
        return f"<Notification to={self.recipient_id} read={self.is_read}>"


class ApprovalAutomation(db.Model):
    """Zeitgesteuerte Automatisierungen für Genehmigungsprozesse."""

    __tablename__ = "approval_automation"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    automation_type = db.Column(db.String(50), nullable=False)
    schedule_type = db.Column(db.String(20), nullable=False, default="daily")
    run_time = db.Column(db.Time, nullable=True)
    days_of_week = db.Column(db.String(50), nullable=True)
    target_position = db.Column(db.String(120), nullable=True)
    next_run = db.Column(db.DateTime, nullable=True)
    last_run = db.Column(db.DateTime, nullable=True)
    last_run_summary = db.Column(db.String(255), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<ApprovalAutomation {self.name} active={self.is_active}>"

def _upgrade_schema_if_needed() -> None:
    """Erweitert ältere SQLite-Datenbanken um neue Spalten.

    Die Anwendung wurde ursprünglich ohne einige optionale Felder
    ausgeliefert. Nutzer, die bereits eine bestehende ``planner.db``
    einsetzen, erhalten beim Zugriff auf neue Attribute (wie die
    bevorzugte Dienstplanansicht) sonst einen ``OperationalError``
    von SQLite. Diese Routine ergänzt fehlende Spalten mit geeigneten
    Defaults, ohne dass ein separates Migrationsskript manuell
    ausgeführt werden muss.
    """

    engine = db.engine

    # Die automatische Migration wird aktuell nur für SQLite benötigt.
    if engine.dialect.name != "sqlite":
        return

    inspector = inspect(engine)

    try:
        employee_columns = {col["name"] for col in inspector.get_columns("employee")}
    except (NoSuchTableError, OperationalError):
        # Tabelle existiert (noch) nicht – nichts zu tun.
        return

    try:
        automation_columns = {
            col["name"] for col in inspector.get_columns("approval_automation")
        }
    except (NoSuchTableError, OperationalError):
        automation_columns = set()

    column_statements = {
        "short_code": ["ALTER TABLE employee ADD COLUMN short_code VARCHAR(20)"],
        "username": ["ALTER TABLE employee ADD COLUMN username VARCHAR(120)"],
        "password_hash": ["ALTER TABLE employee ADD COLUMN password_hash VARCHAR(200)"],
        "is_admin": ["ALTER TABLE employee ADD COLUMN is_admin BOOLEAN DEFAULT 0"],
        "preferred_schedule_view": [
            "ALTER TABLE employee ADD COLUMN preferred_schedule_view VARCHAR(20) NOT NULL DEFAULT 'month'",
            "UPDATE employee SET preferred_schedule_view = 'month' WHERE preferred_schedule_view IS NULL OR TRIM(preferred_schedule_view) = ''",
        ],
    }

    automation_column_statements = {
        "target_position": [
            "ALTER TABLE approval_automation ADD COLUMN target_position VARCHAR(120)"
        ]
    }

    missing_columns = [
        stmts for column, stmts in column_statements.items() if column not in employee_columns
    ]

    missing_automation_columns = [
        stmts
        for column, stmts in automation_column_statements.items()
        if column not in automation_columns
    ]

    if not missing_columns and not missing_automation_columns:
        return

    with engine.begin() as connection:
        for statements in missing_columns + missing_automation_columns:
            for statement in statements:
                try:
                    connection.execute(text(statement))
                except (OperationalError, ProgrammingError):
                    # Wenn das Statement doch nicht kompatibel ist (z.B. durch
                    # parallele Migrationen), wird der Start der Anwendung nicht blockiert.
                    continue


def init_db(app):
    """Initialisiert die Datenbank.

    Diese Funktion sollte beim Start der Anwendung einmalig
    aufgerufen werden. Sie erstellt alle Tabellen, falls sie
    noch nicht existieren.
    """
    db.init_app(app)
    with app.app_context():
        db.create_all()
        _upgrade_schema_if_needed()



class EmployeeGroupOrder(db.Model):
    """Speichert die Reihenfolge der Benutzergruppen im Dienstplan.

    Diese Tabelle ermöglicht es Systemadmins, die Reihenfolge der
    Benutzergruppen (Vollzeit, Teilzeit, Aushilfe) im Dienstplan
    per Drag & Drop anzupassen. Die Reihenfolge wird für alle Benutzer
    übernommen.
    """

    __tablename__ = "employee_group_order"

    id = db.Column(db.Integer, primary_key=True)
    group_name = db.Column(db.String(50), nullable=False, unique=True)  # 'Vollzeit', 'Teilzeit', 'Aushilfe'
    order_position = db.Column(db.Integer, nullable=False)
    created_date = db.Column(db.Date, nullable=False, default=lambda: date.today())
    updated_date = db.Column(db.Date, nullable=False, default=lambda: date.today())

    def __repr__(self) -> str:
        return f"<EmployeeGroupOrder {self.group_name} pos={self.order_position}>"

