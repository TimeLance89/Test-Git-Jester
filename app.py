"""Webanwendung für den Mitarbeiter‑ und Einsatzplaner.

Diese Flask‑Applikation stellt eine einfache Oberfläche zum Anlegen
von Mitarbeitern und Abteilungen, zur Erfassung von Einsätzen und
Abwesenheiten sowie zur Anzeige eines monatlichen Dienstplans bereit.

Alle Bezeichnungen und Beschriftungen sind in deutscher Sprache
gehalten, um die Benutzung für deutschsprachige Anwender zu
vereinfachen.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from typing import Dict, List, Tuple
import pandas as pd
from sqlalchemy.exc import IntegrityError
from sqlalchemy import or_, and_

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    session,
)

from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash

from models import db, init_db, Department, Employee, Shift, Leave, ProductivitySettings, BlockedDay
from auto_schedule import create_default_shifts_for_month, create_default_shifts_for_employee_position

def get_productivity_data(year: int, month: int, department_id: int = None):
    """Berechnet die Produktivitätsdaten basierend auf geplanten Stunden und Produktivitätseinstellungen."""
    import calendar
    from datetime import date, timedelta
    
    # Berechne die Tage des Monats
    num_days = calendar.monthrange(year, month)[1]
    month_days = [date(year, month, day) for day in range(1, num_days + 1)]
    
    # Hole alle Schichten für den Monat
    shifts_query = Shift.query.filter(
        Shift.date.between(month_days[0], month_days[-1]),
        Shift.approved == True
    )
    
    if department_id:
        shifts_query = shifts_query.join(Employee).filter(Employee.department_id == department_id)
    
    shifts = shifts_query.all()

    # Hole alle genehmigten Abwesenheiten für den Monat
    leaves = Leave.query.filter(
        Leave.start_date <= month_days[-1],
        Leave.end_date >= month_days[0],
        Leave.approved == True
    ).all()
    
    # Hole alle Produktivitätseinstellungen
    productivity_settings = {}
    all_settings = ProductivitySettings.query.filter_by(is_active=True).all()
    
    for setting in all_settings:
        if setting.department_id:
            productivity_settings[setting.department_id] = setting.productivity_value
        else:
            productivity_settings['global'] = setting.productivity_value
    
    # Fallback auf Standard-Produktivität
    default_productivity = productivity_settings.get('global', 40.0)
    
    # Gruppiere Schichten nach Datum
    daily_data = {}
    for day in month_days:
        daily_shifts = [s for s in shifts if s.date == day]
        
        # Berechne Stunden nach Abteilungen
        department_hours = {}
        aushilfen_hours = 0
        feste_hours = 0
        
        # Hole alle genehmigten Abwesenheiten für den aktuellen Tag
        daily_leaves = [l for l in leaves if l.start_date <= day <= l.end_date]
        
        for shift in daily_shifts:
            # Prüfe, ob der Mitarbeiter an diesem Tag Urlaub hat
            is_on_leave = any(leave.employee_id == shift.employee_id and leave.leave_type == 'Urlaub' for leave in daily_leaves)
            
            if is_on_leave:
                continue # Überspringe Schichten von Mitarbeitern im Urlaub

            dept_id = shift.employee.department_id
            
            # Hole die Produktivität für diese Abteilung
            dept_productivity = productivity_settings.get(dept_id, default_productivity)
            
            if dept_id not in department_hours:
                department_hours[dept_id] = {
                    'hours': 0,
                    'productivity': dept_productivity,
                    'teile': 0
                }
            
            department_hours[dept_id]['hours'] += shift.hours
            
            # Unterscheide zwischen festen Mitarbeitern und Aushilfen
            if shift.employee.monthly_hours and shift.employee.monthly_hours >= 160:
                feste_hours += shift.hours
            else:
                aushilfen_hours += shift.hours
        
        # Berechne Gesamtteile basierend auf abteilungsspezifischen Produktivitäten
        total_teile = 0
        gesamt_hours = aushilfen_hours + feste_hours
        
        # Wenn nur eine Abteilung arbeitet oder alle die gleiche Produktivität haben
        if len(set(dept['productivity'] for dept in department_hours.values())) <= 1:
            # Verwende eine einheitliche Produktivität
            used_productivity = list(department_hours.values())[0]['productivity'] if department_hours else default_productivity
            total_teile = gesamt_hours * used_productivity
        else:
            # Berechne gewichteten Durchschnitt der Produktivitäten
            total_weighted_productivity = 0
            for dept_data in department_hours.values():
                total_teile += dept_data['hours'] * dept_data['productivity']
                total_weighted_productivity += dept_data['hours'] * dept_data['productivity']
            
            used_productivity = total_weighted_productivity / gesamt_hours if gesamt_hours > 0 else default_productivity
        
        daily_data[day] = {
            "aushilfen_za_std": aushilfen_hours,
            "feste_std": feste_hours,
            "gesamt_std": gesamt_hours,
            "produktivitaet": round(used_productivity, 1),
            "teile": round(total_teile, 0),
            "department_breakdown": department_hours
        }
    
    # Berechne Gesamtsummen
    totals = {
        "aushilfen_za_std_total": sum(data["aushilfen_za_std"] for data in daily_data.values()),
        "feste_std_total": sum(data["feste_std"] for data in daily_data.values()),
        "gesamt_std_total": sum(data["gesamt_std"] for data in daily_data.values()),
        "teile_total": sum(data["teile"] for data in daily_data.values()),
    }
    
    return daily_data, totals

def calculate_employee_hours_summary(employee_id: int, year: int = None, month: int = None):
    """Berechnet eine Zusammenfassung der Arbeitsstunden für einen Mitarbeiter.
    
    Berücksichtigt nur vergangene Tage für geleistete Stunden, um realistische
    Reststunden-Berechnungen zu ermöglichen.
    
    Args:
        employee_id: ID des Mitarbeiters
        year: Jahr für die Berechnung (Standard: aktuelles Jahr)
        month: Monat für die Berechnung (Standard: aktueller Monat)
    
    Returns:
        Dict mit Stunden-Zusammenfassung
    """
    from datetime import date, datetime
    import calendar
    
    if year is None or month is None:
        today = date.today()
        year = year or today.year
        month = month or today.month
    
    # Berechne Zeitraum für den aktuellen Monat
    start_date = date(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    end_date = date(year, month, last_day)
    
    # Aktuelles Datum für Vergangenheits-Check
    today = date.today()

    # Hole alle genehmigten Abwesenheiten für den Zeitraum (alle Typen)
    all_leaves = Leave.query.filter(
        Leave.employee_id == employee_id,
        Leave.start_date <= end_date,
        Leave.end_date >= start_date,
        Leave.approved == True
    ).all()

    # Hole alle genehmigten Schichten für den Zeitraum
    # Berücksichtige nur Schichten bis zum heutigen Tag (inklusive)
    shifts = Shift.query.filter(
        Shift.employee_id == employee_id,
        Shift.date >= start_date,
        Shift.date <= min(end_date, today),  # Nur vergangene/heutige Tage
        Shift.approved == True
    ).all()
    
    # Berechne geleistete Stunden (nur vergangene Tage)
    worked_hours = sum(shift.hours for shift in shifts)
    
    # Hole Mitarbeiter-Daten
    employee = Employee.query.get(employee_id)
    target_hours = employee.monthly_hours or 0
    
    # Berechne anteilige Soll-Stunden basierend auf vergangenen Arbeitstagen
    if year == today.year and month == today.month:
        # Für den aktuellen Monat: Berechne anteilige Soll-Stunden
        days_in_month = calendar.monthrange(year, month)[1]
        days_passed = min(today.day, days_in_month)
        
        # Berechne Arbeitstage (Mo-Fr) die bereits vergangen sind
        workdays_passed = 0
        total_workdays = 0
        
        # Filter all_leaves for 'Urlaub' type within the current month for workday calculation
        vacation_leaves = [l for l in all_leaves if l.leave_type == 'Urlaub']

        for day in range(1, days_in_month + 1):
            current_date = date(year, month, day)
            
            # Prüfen, ob der Mitarbeiter an diesem Tag Urlaub hat
            is_on_vacation = any(l.start_date <= current_date <= l.end_date for l in vacation_leaves)

            # Montag = 0, Sonntag = 6, also Mo-Fr = 0-4
            if current_date.weekday() < 5:  # Montag bis Freitag
                if not is_on_vacation: # Nur zählen, wenn kein Urlaub
                    total_workdays += 1
                    if day <= today.day:
                        workdays_passed += 1
        
        # Anteilige Soll-Stunden basierend auf vergangenen Arbeitstagen
        if total_workdays > 0:
            proportional_target = (target_hours * workdays_passed) / total_workdays
        else:
            proportional_target = 0
            
        # Für Reststunden: Verwende die vollen Monatsstunden minus bereits geleistete
        remaining_hours = max(0, target_hours - worked_hours)
        if employee.monthly_hours and employee.monthly_hours >= 160:
            overtime_hours = 0
        else:
            overtime_hours = max(0, worked_hours - proportional_target)
        
        # Fortschritt basierend auf anteiligen Soll-Stunden
        completion_percentage = (worked_hours / proportional_target * 100) if proportional_target > 0 else 0
        


        return {
            'employee_id': employee_id,
            'target_hours': target_hours,
            'proportional_target': proportional_target,
            'worked_hours': worked_hours,
            'remaining_hours': remaining_hours,
            'overtime_hours': overtime_hours,
            'completion_percentage': completion_percentage,
            'shift_count': len(shifts),
            'days_passed': days_passed,
            'workdays_passed': workdays_passed,
            'total_workdays': total_workdays,
            'is_current_month': True,
            'shifts_detail': shifts,
            'leaves_detail': all_leaves
        }
    else:
        # Für vergangene/zukünftige Monate: Normale Berechnung
        remaining_hours = max(0, target_hours - worked_hours)
        overtime_hours = max(0, worked_hours - target_hours)
        completion_percentage = (worked_hours / target_hours * 100) if target_hours > 0 else 0

        return {
            'employee_id': employee_id,
            'target_hours': target_hours,
            'proportional_target': target_hours,
            'worked_hours': worked_hours,
            'remaining_hours': remaining_hours,
            'overtime_hours': overtime_hours,
            'completion_percentage': completion_percentage,
            'shift_count': len(shifts),
            'is_current_month': False,
            'shifts_detail': shifts,
            'leaves_detail': all_leaves
        }

def get_all_employees_hours_summary(year: int = None, month: int = None, department_id: int = None):
    """Berechnet die Stunden-Zusammenfassung für alle Mitarbeiter.
    
    Args:
        year: Jahr für die Berechnung (Standard: aktuelles Jahr)
        month: Monat für die Berechnung (Standard: aktueller Monat)
        department_id: Abteilungs-ID für Filterung (None = alle Abteilungen)
    
    Returns:
        Dict mit employee_id als Schlüssel und Stunden-Zusammenfassung als Wert
    """
    if department_id:
        employees = Employee.query.filter_by(department_id=department_id).all()
    else:
        employees = Employee.query.all()
    
    summary = {}
    
    for employee in employees:
        summary[employee.id] = calculate_employee_hours_summary(employee.id, year, month)
    
    return summary

def get_planning_insights(year: int = None, month: int = None, department_id: int = None):
    """Generiert smarte Planungshilfen für Administratoren.
    
    Args:
        year: Jahr für die Analyse (Standard: aktuelles Jahr)
        month: Monat für die Analyse (Standard: aktueller Monat)
        department_id: Abteilungs-ID für Filterung (None = alle Abteilungen)
    
    Returns:
        Dict mit Planungshilfen und Insights
    """
    from datetime import date
    
    if year is None or month is None:
        today = date.today()
        year = year or today.year
        month = month or today.month
    
    hours_summary = get_all_employees_hours_summary(year, month, department_id)
    
    # Kategorisiere Mitarbeiter
    underutilized = []  # Mitarbeiter mit vielen Reststunden
    overutilized = []   # Mitarbeiter mit Überstunden
    balanced = []       # Mitarbeiter im Zielbereich
    
    total_remaining_hours = 0
    total_overtime_hours = 0
    
    for emp_id, data in hours_summary.items():
        employee = Employee.query.get(emp_id)
        if not employee:
            continue
        
        # Nur Aushilfen in den Kapazitätsberechnungen berücksichtigen
        if employee.position != 'Aushilfe':
            continue
            
        data['employee_name'] = employee.name
        data['department'] = employee.department.name if employee.department else 'Keine Abteilung'
        
        total_remaining_hours += data['remaining_hours']
        total_overtime_hours += data['overtime_hours']
        
        if data['remaining_hours'] > 20:  # Mehr als 20 Stunden übrig
            underutilized.append(data)
        elif data['overtime_hours'] > 10:  # Mehr als 10 Überstunden
            overutilized.append(data)
        else:
            balanced.append(data)
    
    # Sortiere Listen
    underutilized.sort(key=lambda x: x['remaining_hours'], reverse=True)
    overutilized.sort(key=lambda x: x['overtime_hours'], reverse=True)
    
    return {
        'underutilized': underutilized,
        'overutilized': overutilized,
        'balanced': balanced,
        'total_remaining_hours': total_remaining_hours,
        'total_overtime_hours': total_overtime_hours,
        'total_employees': len(hours_summary),
        'month': month,
        'year': year
    }

# ---------------------------------------------------------------------------
# Authentifizierungs‑Decorator
# ---------------------------------------------------------------------------
def login_required(view):
    """Decorator, der sicherstellt, dass ein Benutzer angemeldet ist.

    Wenn kein Benutzer angemeldet ist, wird zur Login-Seite umgeleitet.
    Das ursprüngliche Ziel wird dabei über den "next"-Parameter übermittelt.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped

def admin_required(view):
    """Decorator, der sicherstellt, dass der aktuelle Benutzer Administrator ist.

    Nicht angemeldete Benutzer werden zur Login-Seite geleitet. Angemeldete
    Benutzer ohne Adminrechte werden zur Startseite umgeleitet und erhalten
    eine Fehlermeldung.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        if not session.get("is_admin"):
            flash("Sie besitzen keine Berechtigung für diese Aktion.", "danger")
            return redirect(url_for("index"))
        return view(*args, **kwargs)
    return wrapped

def get_current_user():
    """Gibt den aktuell angemeldeten Benutzer zurück."""
    user_id = session.get("user_id")
    if user_id:
        return Employee.query.get(user_id)
    return None

def get_user_department_employees():
    """Gibt alle Mitarbeiter der Abteilung des aktuellen Benutzers zurück."""
    current_user = get_current_user()
    if not current_user or not current_user.department_id:
        return Employee.query.all()  # Fallback: alle Mitarbeiter wenn keine Abteilung
    
    return Employee.query.filter_by(department_id=current_user.department_id).all()

def department_required(view):
    """Decorator, der sicherstellt, dass der Benutzer einer Abteilung zugeordnet ist."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        
        current_user = get_current_user()
        if not current_user or not current_user.department_id:
            flash("Sie müssen einer Abteilung zugeordnet sein, um diese Funktion zu nutzen.", "warning")
            return redirect(url_for("index"))
        
        return view(*args, **kwargs)
    return wrapped

def same_department_required(view):
    """Decorator für Aktionen, die nur innerhalb der eigenen Abteilung erlaubt sind."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        
        # Für Super-Admins ohne Abteilung: Vollzugriff
        current_user = get_current_user()
        if current_user and current_user.is_admin and not current_user.department_id:
            return view(*args, **kwargs)
        
        return view(*args, **kwargs)
    return wrapped

def get_pending_requests_count():
    """Zählt die Anzahl der ausstehenden Schicht- und Abwesenheitsanträge."""
    current_user = get_current_user()
    
    # Nur Anträge der eigenen Abteilung zählen
    if current_user and current_user.department_id:
        # Schichten der eigenen Abteilung
        pending_shifts = db.session.query(Shift).join(Employee).filter(
            Shift.approved == False,
            Employee.department_id == current_user.department_id
        ).count()
        
        # Abwesenheiten der eigenen Abteilung  
        pending_leaves = db.session.query(Leave).join(Employee).filter(
            Leave.approved == False,
            Employee.department_id == current_user.department_id
        ).count()
    else:
        # Super-Admin ohne Abteilung sieht alle
        pending_shifts = Shift.query.filter_by(approved=False).count()
        pending_leaves = Leave.query.filter_by(approved=False).count()
    
    return pending_shifts, pending_leaves

def create_app() -> Flask:

    """Erzeugt und konfiguriert die Flask‑Anwendung."""
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///planner.db"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SECRET_KEY"] = "dienstplan-geheim"

    init_db(app)

    @app.context_processor
    def inject_pending_counts():
        if session.get("is_admin"):
            pending_shifts, pending_leaves = get_pending_requests_count()
            return dict(pending_shifts_count=pending_shifts, pending_leaves_count=pending_leaves)
        return dict(pending_shifts_count=0, pending_leaves_count=0)

    def _upgrade_db() -> None:

        """Fügt fehlende Spalten zur Tabelle employee hinzu (SQLite)."""
        import sqlite3
        db_uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
        if db_uri.startswith("sqlite"):
            db_file = db_uri.split("///")[-1]
            try:
                conn = sqlite3.connect(db_file)
                cursor = conn.cursor()
                cursor.execute("PRAGMA table_info(employee);")
                cols = [row[1] for row in cursor.fetchall()]
                upgrades = {
                    "short_code": "ALTER TABLE employee ADD COLUMN short_code VARCHAR(20)",
                    "username": "ALTER TABLE employee ADD COLUMN username VARCHAR(120)",
                    "password_hash": "ALTER TABLE employee ADD COLUMN password_hash VARCHAR(200)",
                    "is_admin": "ALTER TABLE employee ADD COLUMN is_admin BOOLEAN DEFAULT 0",
                }
                for col, stmt in upgrades.items():
                    if col not in cols:
                        try:
                            cursor.execute(stmt)
                        except Exception:
                            pass
                conn.commit()
                try:
                    cursor.execute("PRAGMA table_info(shift);")
                    shift_cols = [row[1] for row in cursor.fetchall()]
                    if "approved" not in shift_cols:
                        cursor.execute("ALTER TABLE shift ADD COLUMN approved BOOLEAN DEFAULT 0")
                        conn.commit()
                except Exception:
                    pass
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    with app.app_context():
        _upgrade_db()
        try:
            if not Employee.query.filter_by(is_admin=True).first():
                admin = Employee(
                    name="Administrator",
                    short_code="ADM",
                    username="admin",
                    is_admin=True,
                )
                admin.set_password("admin")
                db.session.add(admin)
                db.session.commit()
        except Exception:
            pass

    @app.route("/")
    @login_required
    def index() -> str:
        """Startseite: einfache Übersicht über die vorhandenen Daten."""
        current_user = get_current_user()
        
        if current_user and current_user.department_id:
            # Abteilungsadmin sieht nur Statistiken seiner Abteilung
            employee_count = Employee.query.filter_by(department_id=current_user.department_id).count()
            department_count = 1  # Nur die eigene Abteilung
            
            # Nur Abwesenheitsanträge der eigenen Abteilung
            pending_leaves = db.session.query(Leave).join(Employee).filter(
                Leave.approved == False,
                Employee.department_id == current_user.department_id
            ).count()
        else:
            # Super-Admin sieht alle Statistiken
            employee_count = Employee.query.count()
            department_count = Department.query.count()
            pending_leaves = Leave.query.filter_by(approved=False).count()
            
        return render_template(
            "index.html",
            employee_count=employee_count,
            department_count=department_count,
            pending_leaves=pending_leaves,
        )

    @app.route("/login", methods=["GET", "POST"])
    def login() -> str:
        """Anmeldeseite für Benutzer."""
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            user = None
            if username:
                user = Employee.query.filter_by(username=username).first()
            if user and user.check_password(password):
                session.clear()
                session["user_id"] = user.id
                session["is_admin"] = bool(user.is_admin)
                session["department_id"] = user.department_id  # Abteilungs-ID für Zugriffskontrolle
                next_url = request.args.get("next") or url_for("index")
                flash(f"Willkommen zurück, {user.name}! Sie wurden erfolgreich angemeldet.", "success")
                return redirect(next_url)
            else:
                flash("❌ Anmeldung fehlgeschlagen. Bitte überprüfen Sie Ihren Benutzernamen und Ihr Passwort.", "danger")
        return render_template("login.html")

    @app.route("/logout")
    def logout() -> str:
        """Meldet den aktuellen Benutzer ab."""
        session.clear()
        flash("Sie wurden erfolgreich abgemeldet. Auf Wiedersehen!", "info")
        return redirect(url_for("login"))

    @app.route("/mitarbeiter")
    @admin_required
    def employees() -> str:
        """Liste der Mitarbeiter mit Formular zum Hinzufügen neuer Mitarbeiter."""
        from datetime import date

        # Hole aktuelle Monat/Jahr Parameter oder verwende aktuelle Werte
        today = date.today()
        month = request.args.get('month', type=int) or today.month
        year = request.args.get('year', type=int) or today.year
        
        # Abteilungsbasierte Filterung
        current_user = get_current_user()
        if current_user and current_user.department_id:
            # Nur Mitarbeiter der eigenen Abteilung anzeigen
            employees = Employee.query.filter_by(department_id=current_user.department_id).order_by(Employee.name).all()
            departments = Department.query.filter_by(id=current_user.department_id).all()
        else:
            # Super-Admin ohne Abteilung sieht alle
            employees = Employee.query.order_by(Employee.name).all()
            departments = Department.query.order_by(Department.name).all()
        
        # Berechne Reststunden für alle Mitarbeiter (abteilungsbasiert)
        current_user = get_current_user()
        user_dept_id = current_user.department_id if current_user else None
        
        hours_summary = get_all_employees_hours_summary(year, month, user_dept_id)
        
        # Generiere Planungshilfen (abteilungsbasiert)
        planning_insights = get_planning_insights(year, month, user_dept_id)
        
        return render_template(
            "employees.html",
            employees=employees,
            departments=departments,
            hours_summary=hours_summary,
            planning_insights=planning_insights,
            current_month=month,
            current_year=year,
        )

    @app.route("/berichte/monat")
    @admin_required
    def monthly_report() -> str:
        """Zeigt einen monatlichen Bericht über Stunden und Abwesenheiten."""
        today = date.today()
        month = request.args.get("month", type=int) or today.month
        year = request.args.get("year", type=int) or today.year

        if month < 1 or month > 12:
            month = today.month

        start_date = date(year, month, 1)
        last_day = calendar.monthrange(year, month)[1]
        end_date = date(year, month, last_day)

        current_user = get_current_user()
        is_super_admin = bool(current_user and current_user.is_admin and not current_user.department_id)

        selected_department_id = None
        available_departments: List[Department] = []

        if current_user and current_user.department_id:
            selected_department_id = current_user.department_id
        else:
            selected_department_id = request.args.get("department_id", type=int)
            available_departments = Department.query.order_by(Department.name).all()
            if selected_department_id and not any(d.id == selected_department_id for d in available_departments):
                selected_department_id = None

        employee_query = Employee.query
        if selected_department_id:
            employee_query = employee_query.filter_by(department_id=selected_department_id)
        employees = employee_query.order_by(Employee.name).all()

        summary_department = selected_department_id if selected_department_id else None
        hours_summary = get_all_employees_hours_summary(year, month, summary_department)

        leaves_query = Leave.query.join(Employee).filter(
            Leave.approved == True,
            Leave.start_date <= end_date,
            Leave.end_date >= start_date,
        )
        if selected_department_id:
            leaves_query = leaves_query.filter(Employee.department_id == selected_department_id)

        leaves = leaves_query.all()

        sick_days_by_employee: Dict[int, int] = {}
        usa_days_by_employee: Dict[int, int] = {}

        for leave in leaves:
            overlap_start = max(leave.start_date, start_date)
            overlap_end = min(leave.end_date, end_date)
            if overlap_start > overlap_end:
                continue
            leave_days = (overlap_end - overlap_start).days + 1

            if leave.leave_type == "Krank":
                sick_days_by_employee[leave.employee_id] = sick_days_by_employee.get(leave.employee_id, 0) + leave_days
            elif leave.leave_type == "ÜSA":
                usa_days_by_employee[leave.employee_id] = usa_days_by_employee.get(leave.employee_id, 0) + leave_days

        report_rows = []
        totals = {
            "total_hours": 0.0,
            "total_overtime": 0.0,
            "total_sick_days": 0,
            "total_usa_days": 0,
        }

        for employee in employees:
            summary = hours_summary.get(employee.id, {})
            worked_hours = float(summary.get("worked_hours", 0))
            overtime_hours = float(summary.get("overtime_hours", 0))
            target_hours = float(summary.get("target_hours", 0) or 0)
            proportional_target = float(summary.get("proportional_target", target_hours))
            remaining_hours = float(summary.get("remaining_hours", 0))
            sick_days = sick_days_by_employee.get(employee.id, 0)
            usa_days = usa_days_by_employee.get(employee.id, 0)

            totals["total_hours"] += worked_hours
            totals["total_overtime"] += overtime_hours
            totals["total_sick_days"] += sick_days
            totals["total_usa_days"] += usa_days

            report_rows.append(
                {
                    "employee": employee,
                    "department_name": employee.department.name if employee.department else "Keine Abteilung",
                    "worked_hours": worked_hours,
                    "overtime_hours": overtime_hours,
                    "target_hours": target_hours,
                    "proportional_target": proportional_target,
                    "remaining_hours": remaining_hours,
                    "sick_days": sick_days,
                    "usa_days": usa_days,
                    "is_current_month": bool(summary.get("is_current_month")),
                }
            )

        employee_count = len(report_rows)
        totals["average_hours"] = totals["total_hours"] / employee_count if employee_count else 0
        totals["average_overtime"] = totals["total_overtime"] / employee_count if employee_count else 0
        totals["average_sick_days"] = totals["total_sick_days"] / employee_count if employee_count else 0
        totals["average_usa_days"] = totals["total_usa_days"] / employee_count if employee_count else 0

        department_overview = []
        if is_super_admin and not selected_department_id:
            department_totals: Dict[str, Dict[str, float]] = {}
            for row in report_rows:
                dept_name = row["department_name"]
                if dept_name not in department_totals:
                    department_totals[dept_name] = {
                        "hours": 0.0,
                        "overtime": 0.0,
                        "sick_days": 0,
                        "usa_days": 0,
                        "employees": 0,
                    }
                department_totals[dept_name]["hours"] += row["worked_hours"]
                department_totals[dept_name]["overtime"] += row["overtime_hours"]
                department_totals[dept_name]["sick_days"] += row["sick_days"]
                department_totals[dept_name]["usa_days"] += row["usa_days"]
                department_totals[dept_name]["employees"] += 1

            department_overview = [
                {
                    "name": name,
                    "hours": data["hours"],
                    "overtime": data["overtime"],
                    "sick_days": data["sick_days"],
                    "usa_days": data["usa_days"],
                    "employees": data["employees"],
                }
                for name, data in sorted(department_totals.items(), key=lambda item: item[0].lower())
            ]

        month_label = f"{calendar.month_name[month]} {year}"
        prev_month_date = start_date - timedelta(days=1)
        next_month_date = end_date + timedelta(days=1)

        prev_params = {"month": prev_month_date.month, "year": prev_month_date.year}
        next_params = {"month": next_month_date.month, "year": next_month_date.year}
        if selected_department_id:
            prev_params["department_id"] = selected_department_id
            next_params["department_id"] = selected_department_id

        month_choices = [(i, calendar.month_name[i]) for i in range(1, 13)]
        year_choices = list(range(today.year - 2, today.year + 3))

        return render_template(
            "monthly_report.html",
            month=month,
            year=year,
            month_label=month_label,
            month_choices=month_choices,
            year_choices=year_choices,
            selected_department_id=selected_department_id,
            available_departments=available_departments,
            is_super_admin=is_super_admin,
            report_rows=report_rows,
            totals=totals,
            employee_count=employee_count,
            department_overview=department_overview,
            prev_params=prev_params,
            next_params=next_params,
        )

    @app.route("/mitarbeiter/hinzufuegen", methods=["POST"])
    @admin_required
    def add_employee() -> str:
        """Legt einen neuen Mitarbeiter an."""
        name = request.form.get("name", "").strip()
        if not name:
            flash("Bitte geben Sie einen Namen an.", "warning")
            return redirect(url_for("employees"))
        emp_number = request.form.get("employee_number", "").strip() or None
        dept_id = request.form.get("department_id") or None
        dept_id = int(dept_id) if dept_id else None
        
        # Abteilungsbasierte Einschränkung
        current_user = get_current_user()
        if current_user and current_user.department_id:
            # Abteilungsadmin kann nur Mitarbeiter in seiner eigenen Abteilung anlegen
            dept_id = current_user.department_id
        monthly_hours = request.form.get("monthly_hours") or None
        monthly_hours = float(monthly_hours) if monthly_hours else None
        email = request.form.get("email", "").strip() or None
        phone = request.form.get("phone", "").strip() or None
        position = request.form.get("position", "").strip() or None
        short_code = request.form.get("short_code", "").strip() or None
        username = request.form.get("username", "").strip() or None
        password = request.form.get("password", "")
        is_admin_flag = bool(request.form.get("is_admin"))
        
        # Standard-Arbeitszeiten verarbeiten
        default_daily_hours = request.form.get("default_daily_hours") or None
        default_daily_hours = float(default_daily_hours) if default_daily_hours else None
        work_days = request.form.getlist("work_days")
        default_work_days = ",".join(work_days) if work_days else None
        
        employee = Employee(
            name=name,
            employee_number=emp_number,
            department_id=dept_id,
            monthly_hours=monthly_hours,
            email=email,
            phone=phone,
            position=position,
            short_code=short_code,
            username=username,
            is_admin=is_admin_flag,
            default_daily_hours=default_daily_hours,
            default_work_days=default_work_days,
        )
        if password:
            employee.set_password(password)
        try:
            db.session.add(employee)
            db.session.commit()
            flash(f"Mitarbeiter {name} wurde gespeichert.", "success")
            return redirect(url_for("employees"))
        except IntegrityError:
            db.session.rollback()
            flash("Ein Mitarbeiter mit diesem Benutzernamen oder dieser Personalnummer existiert bereits.", "danger")
            return redirect(url_for("employees"))

    @app.route("/mitarbeiter/loeschen/<int:emp_id>")
    @admin_required
    def delete_employee(emp_id: int) -> str:
        """Löscht einen Mitarbeiter und alle zugehörigen Einträge."""
        employee = Employee.query.get_or_404(emp_id)
        db.session.delete(employee)
        db.session.commit()
        flash(f"Mitarbeiter {employee.name} wurde gelöscht.", "info")
        return redirect(url_for("employees"))

    @app.route("/mitarbeiter/<int:emp_id>")
    @login_required
    def employee_profile(emp_id: int) -> str:
        """Zeigt die Detailansicht eines Mitarbeiters an."""
        emp = Employee.query.get_or_404(emp_id)
        if not session.get("is_admin") and session.get("user_id") != emp_id:
            flash("Sie können nur Ihr eigenes Profil anzeigen.", "danger")
            return redirect(url_for("index"))
        
        # Nur Schichten der aktuellen Woche anzeigen (Montag bis Sonntag)
        from datetime import datetime, timedelta
        today = datetime.now().date()
        # Montag der aktuellen Woche berechnen (0 = Montag, 6 = Sonntag)
        start_of_week = today - timedelta(days=today.weekday())
        end_of_week = start_of_week + timedelta(days=6)
        
        shifts = Shift.query.filter(
            Shift.employee_id == emp.id,
            Shift.date >= start_of_week,
            Shift.date <= end_of_week
        ).order_by(Shift.date.asc()).all()
        
        leaves_list = Leave.query.filter_by(employee_id=emp.id).order_by(Leave.start_date.desc()).all()
        return render_template(
            "employee_profile.html",
            emp=emp,
            shifts=shifts,
            leaves=leaves_list,
        )

    @app.route("/mitarbeiter/bearbeiten/<int:emp_id>", methods=["GET", "POST"])
    @login_required
    def edit_employee(emp_id: int) -> str:
        """Bearbeitet die Daten eines Mitarbeiters."""
        emp = Employee.query.get_or_404(emp_id)
        if not session.get("is_admin") and session.get("user_id") != emp_id:
            flash("Sie können nur Ihre eigenen Daten bearbeiten.", "danger")
            return redirect(url_for("index"))
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            emp_number = request.form.get("employee_number", "").strip() or None
            dept_id = request.form.get("department_id") or None
            dept_id = int(dept_id) if dept_id else None
            monthly_hours = request.form.get("monthly_hours") or None
            monthly_hours = float(monthly_hours) if monthly_hours else None
            email = request.form.get("email", "").strip() or None
            phone = request.form.get("phone", "").strip() or None
            position = request.form.get("position", "").strip() or None
            short_code = request.form.get("short_code", "").strip() or None
            username = request.form.get("username", "").strip() or None
            password = request.form.get("password", "")
            is_admin_flag = bool(request.form.get("is_admin"))
            emp.name = name or emp.name
            emp.employee_number = emp_number
            emp.department_id = dept_id
            emp.monthly_hours = monthly_hours
            emp.email = email
            emp.phone = phone
            emp.position = position
            emp.short_code = short_code
            
            # Standard-Arbeitszeiten verarbeiten
            default_daily_hours = request.form.get("default_daily_hours") or None
            default_daily_hours = float(default_daily_hours) if default_daily_hours else None
            emp.default_daily_hours = default_daily_hours
            
            work_days = request.form.getlist("work_days")
            emp.default_work_days = ",".join(work_days) if work_days else None
            
            if session.get("is_admin"):
                emp.is_admin = is_admin_flag
            if username:
                emp.username = username
            if password:
                emp.set_password(password)
            db.session.commit()
            flash("Mitarbeiter wurde aktualisiert.", "success")
            return redirect(url_for("employee_profile", emp_id=emp.id))
        departments = Department.query.order_by(Department.name).all()
        return render_template(
            "employee_edit.html",
            emp=emp,
            departments=departments,
        )

    @app.route("/abteilungen")
    @admin_required
    def departments() -> str:
        """Liste der Abteilungen mit Formular zum Hinzufügen neuer Abteilungen."""
        # Abteilungsbasierte Einschränkung
        current_user = get_current_user()
        if current_user and current_user.department_id:
            # Abteilungsadmin sieht nur seine eigene Abteilung
            departments = Department.query.filter_by(id=current_user.department_id).all()
        else:
            # Super-Admin ohne Abteilung sieht alle
            departments = Department.query.order_by(Department.name).all()
        return render_template("departments.html", departments=departments)

    @app.route("/abteilungen/hinzufuegen", methods=["POST"])
    @admin_required
    def add_department() -> str:
        """Fügt eine neue Abteilung hinzu."""
        # Abteilungsadministratoren können keine neuen Abteilungen erstellen
        current_user = get_current_user()
        if current_user and current_user.department_id:
            flash("Sie können keine neuen Abteilungen erstellen. Wenden Sie sich an den Super-Administrator.", "warning")
            return redirect(url_for("departments"))
            
        name = request.form.get("name", "").strip()
        if not name:
            flash("Bitte geben Sie einen Namen an.", "warning")
            return redirect(url_for("departments"))
        color = request.form.get("color", "").strip() or None
        area = request.form.get("area", "").strip() or None
        dept = Department(name=name, color=color, area=area)
        db.session.add(dept)
        db.session.commit()
        flash(f"Abteilung {name} wurde gespeichert.", "success")
        return redirect(url_for("departments"))

    @app.route("/abteilungen/loeschen/<int:dept_id>")
    @admin_required
    def delete_department(dept_id: int) -> str:
        """Löscht eine Abteilung."""
        dept = Department.query.get_or_404(dept_id)
        db.session.delete(dept)
        db.session.commit()
        flash(f"Abteilung {dept.name} wurde gelöscht.", "info")
        return redirect(url_for("departments"))

    @app.route("/dienstplan")
    @login_required
    def schedule() -> str:
        """Monatliche Übersicht über Einsätze und Abwesenheiten."""
        month = request.args.get("month", type=int)
        year = request.args.get("year", type=int)
        if not month or not year:
            today = date.today()
            month = today.month
            year = today.year
        # Abteilungsbasierte Filterung erzwingen
        current_user = get_current_user()
        if current_user and current_user.department_id:
            # Nur Mitarbeiter der eigenen Abteilung anzeigen
            department_id = current_user.department_id
        else:
            # Super-Admin ohne Abteilung kann Abteilung wählen
            department_id = request.args.get("department", type=int)
        
        cal = calendar.Calendar(firstweekday=0)
        month_days = [d for d in cal.itermonthdates(year, month) if d.month == month]
        
        if department_id:
            all_employees = (
                Employee.query
                .filter_by(department_id=department_id)
                .filter(or_(Employee.is_admin == False, and_(Employee.is_admin == True, Employee.position == 'Vollzeit')))
                .order_by(Employee.name)
                .all()
            )
        else:
            # Nur für Super-Admins ohne Abteilung
            all_employees = (
                Employee.query
                .filter(or_(Employee.is_admin == False, and_(Employee.is_admin == True, Employee.position == 'Vollzeit')))
                .order_by(Employee.name)
                .all()
            )
        
        # Gruppiere Mitarbeiter nach Arbeitszeit-Kategorien
        vollzeit_employees = [emp for emp in all_employees if emp.position == 'Vollzeit']
        teilzeit_employees = [emp for emp in all_employees if emp.position == 'Teilzeit']
        aushilfe_employees = [emp for emp in all_employees if emp.position == 'Aushilfe']
        
        # Hole die Reihenfolge der Benutzergruppen aus der Datenbank
        from models import EmployeeGroupOrder
        group_order = EmployeeGroupOrder.query.order_by(EmployeeGroupOrder.order_position).all()
        
        # Erstelle ein Dictionary für die Gruppen
        employee_groups = {
            'Vollzeit': vollzeit_employees,
            'Teilzeit': teilzeit_employees,
            'Aushilfe': aushilfe_employees
        }
        
        # Wenn keine Reihenfolge definiert ist, verwende Standard-Reihenfolge
        if not group_order:
            ordered_group_names = ['Vollzeit', 'Teilzeit', 'Aushilfe']
        else:
            ordered_group_names = [g.group_name for g in group_order]
        
        # Für Rückwärtskompatibilität
        employees = all_employees

        shifts_query = Shift.query.filter(
            Shift.date.between(month_days[0], month_days[-1])
        ).all()
        shifts = {(s.employee_id, s.date): s for s in shifts_query}
        leaves_query = Leave.query.filter(
            and_(
                Leave.start_date <= month_days[-1],
                Leave.end_date >= month_days[0],
                Leave.approved == True
            )
        ).all()
        leaves: Dict[Tuple[int, date], Leave] = {}
        for leave in leaves_query:
            current_date = leave.start_date
            while current_date <= leave.end_date:
                if current_date.month == month:
                    leaves[(leave.employee_id, current_date)] = leave
                current_date += timedelta(days=1)
        employee_totals = {
            emp.id: sum(
                s.hours for (eid, _), s in shifts.items() if eid == emp.id and s.approved
            )
            for emp in employees
        }
        departments = Department.query.order_by(Department.name).all()
        current_user = Employee.query.get(session.get("user_id"))

        productivity_data, productivity_data_totals = get_productivity_data(year, month, department_id)

        # Gesperrte Tage für den Monat abrufen
        blocked_days_query = BlockedDay.query.filter(
            BlockedDay.date.between(month_days[0], month_days[-1])
        ).all()
        blocked_days = {bd.date: bd for bd in blocked_days_query}

        return render_template(
            "schedule.html",
            month=month,
            year=year,
            month_days=month_days,
            employees=employees,
            vollzeit_employees=vollzeit_employees,
            teilzeit_employees=teilzeit_employees,
            aushilfe_employees=aushilfe_employees,
            employee_groups=employee_groups,
            ordered_group_names=ordered_group_names,
            shifts=shifts,
            leaves=leaves,
            blocked_days=blocked_days,
            employee_totals=employee_totals,
            departments=departments,
            selected_department=department_id,
            calendar=calendar,
            current_user=current_user,
            productivity_data=productivity_data,
            productivity_data_totals=productivity_data_totals,
        )

    @app.route("/einsatz/hinzufuegen", methods=["POST"])
    @login_required
    def add_shift() -> str:
        """Fügt einen neuen Einsatz hinzu."""
        emp_id = request.form.get("employee_id", type=int)
        date_str = request.form.get("date", "")
        hours = request.form.get("hours", type=float)
        shift_type = request.form.get("shift_type", "").strip() or None
        
        # Berechtigungsprüfung: Normale Mitarbeiter können nur für sich selbst Schichten eintragen
        if not session.get("is_admin", False):
            current_user_id = session.get("user_id")
            if emp_id != current_user_id:
                flash("Sie können nur Ihre eigenen Schichten eintragen.", "error")
                return redirect(url_for("schedule"))
        
        if not emp_id or not date_str or not hours:
            flash("Bitte füllen Sie alle Pflichtfelder aus.", "warning")
            return redirect(url_for("schedule"))
        shift_date = datetime.strptime(date_str, "%Y-%m-%d").date()

        # Prüfen, ob das Datum ein gesperrter Tag ist
        if BlockedDay.query.filter_by(date=shift_date).first():
            flash(f"An diesem Tag ({shift_date.strftime("%d.%m.%Y")}) können keine Schichten hinzugefügt werden, da er gesperrt ist.", "danger")
            return redirect(url_for("schedule", month=shift_date.month, year=shift_date.year))
        new_shift = Shift(
            employee_id=emp_id,
            date=shift_date,
            hours=hours,
            shift_type=shift_type,
            approved=session.get("is_admin", False),
        )
        db.session.add(new_shift)
        db.session.commit()
        flash("Einsatz wurde hinzugefügt.", "success")
        return redirect(url_for("schedule", month=shift_date.month, year=shift_date.year))

    @app.route("/einsatz/loeschen/<int:shift_id>", methods=["GET", "POST"])
    @login_required
    def delete_shift(shift_id: int) -> str:
        """Löscht einen Einsatz."""
        shift = Shift.query.get_or_404(shift_id)
        
        # Berechtigungsprüfung
        if not session.get("is_admin") and session.get("user_id") != shift.employee_id:
            flash("Sie können nur Ihre eigenen Einsätze löschen.", "danger")
            return redirect(url_for("schedule"))
        
        shift_date = shift.date
        db.session.delete(shift)
        db.session.commit()
        flash("Einsatz wurde gelöscht.", "info")
        return redirect(url_for("schedule", month=shift_date.month, year=shift_date.year))

    @app.route("/abwesenheit/loeschen/<int:leave_id>", methods=["GET", "POST"])
    @login_required
    def delete_leave(leave_id: int) -> str:
        """Löscht eine Abwesenheit."""
        leave = Leave.query.get_or_404(leave_id)
        if not session.get("is_admin") and session.get("user_id") != leave.employee_id:
            flash("Sie können nur Ihre eigenen Abwesenheiten löschen.", "danger")
            return redirect(url_for("schedule"))
        leave_date = leave.start_date
        db.session.delete(leave)
        db.session.commit()
        flash("Abwesenheit wurde gelöscht.", "info")
        return redirect(url_for("schedule", month=leave_date.month, year=leave_date.year))

    @app.route("/einsatz/uebersicht")
    @admin_required
    def shift_requests_overview() -> str:
        """Liste der offenen Einsatzanträge."""
        pending_shifts = Shift.query.filter_by(approved=False).order_by(Shift.date).all()
        
        # Hole auch genehmigte Schichten für den Kalkulator
        approved_shifts = Shift.query.filter_by(approved=True).order_by(Shift.date).all()
        
        # Gruppiere Schichten nach Datum für den Kalkulator
        from collections import defaultdict
        shifts_by_date_raw = defaultdict(list)
        approved_by_date_raw = defaultdict(list)
        
        for shift in pending_shifts:
            shifts_by_date_raw[shift.date].append(shift)
        
        for shift in approved_shifts:
            approved_by_date_raw[shift.date].append(shift)
        
        # Sortiere Daten
        sorted_dates = sorted(shifts_by_date_raw.keys())
        
        # Hole Produktivitätseinstellungen
        productivity_settings = {}
        all_settings = ProductivitySettings.query.filter_by(is_active=True).all()
        for setting in all_settings:
            if setting.department_id:
                # Konvertiere zu String für JSON
                productivity_settings[str(setting.department_id)] = setting.productivity_value
            else:
                productivity_settings['global'] = setting.productivity_value
        
        default_productivity = productivity_settings.get('global', 40.0)
        
        # Konvertiere für JSON: date objects zu strings, shift objects zu dicts
        shifts_by_date_json = {}
        approved_by_date_json = {}
        
        # Alle Daten sammeln (pending + approved)
        all_dates = set(list(shifts_by_date_raw.keys()) + list(approved_by_date_raw.keys()))
        
        for date_obj in all_dates:
            date_str = date_obj.strftime('%Y-%m-%d')
            
            # Ausstehende Schichten
            if date_obj in shifts_by_date_raw:
                shifts_by_date_json[date_str] = [
                    {
                        'id': s.id,
                        'hours': s.hours,
                        'shift_type': s.shift_type,
                        'approved': False,
                        'employee': {
                            'id': s.employee.id,
                            'name': s.employee.name,
                            'position': s.employee.position,
                            'department_id': s.employee.department_id,
                            'department': {
                                'id': s.employee.department.id,
                                'name': s.employee.department.name
                            } if s.employee.department else None
                        }
                    }
                    for s in shifts_by_date_raw[date_obj]
                ]
            
            # Genehmigte Schichten
            if date_obj in approved_by_date_raw:
                approved_by_date_json[date_str] = [
                    {
                        'id': s.id,
                        'hours': s.hours,
                        'shift_type': s.shift_type,
                        'approved': True,
                        'employee': {
                            'id': s.employee.id,
                            'name': s.employee.name,
                            'position': s.employee.position,
                            'department_id': s.employee.department_id,
                            'department': {
                                'id': s.employee.department.id,
                                'name': s.employee.department.name
                            } if s.employee.department else None
                        }
                    }
                    for s in approved_by_date_raw[date_obj]
                ]
        
        return render_template(
            "shift_requests.html",
            shifts=pending_shifts,
            shifts_by_date=dict(shifts_by_date_raw),
            shifts_by_date_json=shifts_by_date_json,
            approved_by_date_json=approved_by_date_json,
            sorted_dates=sorted_dates,
            productivity_settings=productivity_settings,
            default_productivity=default_productivity
        )

    @app.route("/einsatz/genehmigen/<int:shift_id>")
    @admin_required
    def approve_shift(shift_id: int) -> str:
        """Genehmigt einen Einsatz."""
        shift = Shift.query.get_or_404(shift_id)
        shift.approved = True
        db.session.commit()
        flash("Einsatz wurde genehmigt.", "success")
        return redirect(url_for("schedule", month=shift.date.month, year=shift.date.year))

    @app.route("/einsatz/ablehnen/<int:shift_id>")
    @admin_required
    def decline_shift(shift_id: int) -> str:
        """Lehnt einen Einsatz ab (löscht ihn)."""
        shift = Shift.query.get_or_404(shift_id)
        shift_date = shift.date
        db.session.delete(shift)
        db.session.commit()
        flash("Einsatz wurde abgelehnt und gelöscht.", "info")
        return redirect(url_for("schedule", month=shift_date.month, year=shift_date.year))

    @app.route("/meine-stunden")
    @login_required
    def employee_hours_overview() -> str:
        """Zeigt die monatliche Stundenübersicht für den angemeldeten Mitarbeiter an.
        Geplant, geleistet und offene Stunden.
        """
        employee_id = session.get("user_id")
        if not employee_id:
            flash("Sie sind nicht angemeldet.", "danger")
            return redirect(url_for("login"))
        
        # Hole das Employee-Objekt
        employee = Employee.query.get_or_404(employee_id)

        current_year = date.today().year
        current_month = date.today().month

        # Hole die Stundenübersicht für den aktuellen Monat
        hours_summary = calculate_employee_hours_summary(employee_id, current_year, current_month)
        
        # Hole die Stundenübersicht für die letzten 12 Monate für Diagramme
        monthly_data = []
        for i in range(12):
            month = current_month - i
            year = current_year
            if month <= 0:
                month += 12
                year -= 1
            summary = calculate_employee_hours_summary(employee_id, year, month)
            monthly_data.append({
                'month_year': f"{month}/{year}",
                'worked_hours': summary.get('worked_hours', 0),
                'target_hours': summary.get('target_hours', 0),
                'proportional_target': summary.get('proportional_target', 0),
                'remaining_hours': summary.get('remaining_hours', 0),
                'overtime_hours': summary.get('overtime_hours', 0),
            })
        monthly_data.reverse() # Älteste zuerst

        # Wochentags-Analyse
        weekday_hours = {i: 0 for i in range(7)}
        for shift in hours_summary.get('shifts_detail', []):
            weekday_hours[shift.date.weekday()] += shift.hours

        # Schichtarten-Analyse
        shift_type_hours = {}
        for shift in hours_summary.get('shifts_detail', []):
            shift_type = shift.shift_type or "Unbekannt"
            shift_type_hours[shift_type] = shift_type_hours.get(shift_type, 0) + shift.hours

        return render_template(
            "employee_hours_overview.html",
            employee=employee,
            hours_summary=hours_summary,
            monthly_data=monthly_data,
            weekday_hours=weekday_hours,
            shift_type_hours=shift_type_hours,
            current_month=current_month,
            current_year=current_year
        )

    @app.route("/abwesenheit/antrag", methods=["GET", "POST"])
    @login_required
    def leave_form() -> str:
        """Formular zur Beantragung von Abwesenheiten."""
        # Abteilungsbasierte Filterung für Mitarbeiterauswahl
        current_user = get_current_user()
        if current_user and current_user.department_id:
            # Nur Mitarbeiter der eigenen Abteilung
            employees = Employee.query.filter_by(department_id=current_user.department_id).all()
        else:
            # Super-Admin sieht alle Mitarbeiter
            employees = Employee.query.all()
        
        if request.method == "POST":
            # Wenn der Benutzer ein Admin ist, kann er einen Mitarbeiter auswählen.
            # Andernfalls wird die employee_id des angemeldeten Benutzers verwendet.
            if session.get("is_admin"):
                emp_id = request.form.get("employee_id", type=int)
            else:
                emp_id = session.get("user_id")

            if not emp_id:
                flash("Mitarbeiter-ID konnte nicht ermittelt werden.", "danger")
                return redirect(url_for("index"))
            start_date_str = request.form.get("start_date", "")
            end_date_str = request.form.get("end_date", "")
            leave_type = request.form.get("leave_type", "")
            notes = request.form.get("notes", "").strip() or None
            if not all([start_date_str, end_date_str, leave_type]):
                flash("Bitte füllen Sie alle Pflichtfelder aus.", "warning")
                # current_employee wird am Ende der Funktion gesetzt
                return redirect(url_for("leave_form"))
            
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
            
            # Abwesenheiten sind standardmäßig nicht genehmigt, außer bei 'Krank', die automatisch genehmigt werden.
            is_approved = True if leave_type == 'Krank' else False
            new_leave = Leave(
                employee_id=emp_id,
                start_date=start_date,
                end_date=end_date,
                leave_type=leave_type,
                notes=notes,
                approved=is_approved,
            )


            db.session.add(new_leave)
            db.session.commit()
            flash("Ihr Antrag wurde eingereicht.", "success")
            return redirect(url_for("index"))
        
        current_employee = Employee.query.get(session.get("user_id"))
        return render_template("leave_form.html", employees=employees, current_employee=current_employee)





    @app.route("/abwesenheit/antraege")
    @admin_required
    def leave_requests() -> str:
        """Liste der offenen Abwesenheitsanträge."""
        from datetime import datetime
        from sqlalchemy import func, extract
        
        # Abteilungsbasierte Filterung
        current_user = get_current_user()
        
        if current_user and current_user.department_id:
            # Nur Anträge der eigenen Abteilung
            # Ausstehende Anträge (ohne Krankheit)
            pending_leaves = db.session.query(Leave).join(Employee).filter(
                Leave.approved == False,
                Leave.leave_type != 'Krank',
                Employee.department_id == current_user.department_id
            ).order_by(Leave.start_date).all()
            
            # Krankheitsanträge (ausstehend und genehmigt)
            sick_leaves = db.session.query(Leave).join(Employee).filter(
                Leave.leave_type == 'Krank',
                Employee.department_id == current_user.department_id
            ).order_by(Leave.start_date.desc()).all()
            
            # Genehmigte Abwesenheiten (ohne Krankheit)
            approved_leaves = db.session.query(Leave).join(Employee).filter(
                Leave.approved == True,
                Leave.leave_type != 'Krank',
                Employee.department_id == current_user.department_id
            ).order_by(Leave.start_date).all()
            
            # Chart-Daten für die eigene Abteilung (Krankheitstage pro Mitarbeiter im aktuellen Monat)
            current_month = datetime.now().month
            current_year = datetime.now().year
            
            chart_data_raw = db.session.query(
                Employee.name,
                func.sum(
                    func.julianday(Leave.end_date) - func.julianday(Leave.start_date) + 1
                ).label('total_days')
            ).join(Leave).filter(
                Leave.leave_type == 'Krank',
                Employee.department_id == current_user.department_id,
                extract('year', Leave.start_date) == current_year,
                extract('month', Leave.start_date) == current_month
            ).group_by(Employee.id, Employee.name).all()
            
            # Convert Row objects to list of lists for JSON serialization
            chart_data = [[row[0], float(row[1])] for row in chart_data_raw]
            
            departments = [current_user.department]
            selected_department_id = current_user.department_id
            
        else:
            # Super-Admin ohne Abteilung sieht alle
            # Ausstehende Anträge (ohne Krankheit)
            pending_leaves = Leave.query.filter(
                Leave.approved == False,
                Leave.leave_type != 'Krank'
            ).order_by(Leave.start_date).all()
            
            # Krankheitsanträge (ausstehend und genehmigt)
            sick_leaves = Leave.query.filter(
                Leave.leave_type == 'Krank'
            ).order_by(Leave.start_date.desc()).all()
            
            # Genehmigte Abwesenheiten (ohne Krankheit)
            approved_leaves = Leave.query.filter(
                Leave.approved == True,
                Leave.leave_type != 'Krank'
            ).order_by(Leave.start_date).all()
            
            # Chart-Daten für alle Abteilungen oder ausgewählte Abteilung
            selected_department_id = request.args.get('department_id', type=int)
            current_month = datetime.now().month
            current_year = datetime.now().year
            
            if selected_department_id:
                # Spezifische Abteilung
                chart_data_raw = db.session.query(
                    Employee.name,
                    func.sum(
                        func.julianday(Leave.end_date) - func.julianday(Leave.start_date) + 1
                    ).label('total_days')
                ).join(Leave).filter(
                    Leave.leave_type == 'Krank',
                    Employee.department_id == selected_department_id,
                    extract('year', Leave.start_date) == current_year,
                    extract('month', Leave.start_date) == current_month
                ).group_by(Employee.id, Employee.name).all()
                
                # Convert Row objects to list of lists for JSON serialization
                chart_data = [[row[0], float(row[1])] for row in chart_data_raw]
            else:
                # Alle Abteilungen zusammengefasst
                chart_data_raw = db.session.query(
                    Employee.name,
                    func.sum(
                        func.julianday(Leave.end_date) - func.julianday(Leave.start_date) + 1
                    ).label('total_days')
                ).join(Leave).filter(
                    Leave.leave_type == 'Krank',
                    extract('year', Leave.start_date) == current_year,
                    extract('month', Leave.start_date) == current_month
                ).group_by(Employee.id, Employee.name).all()
                
                # Convert Row objects to list of lists for JSON serialization
                chart_data = [[row[0], float(row[1])] for row in chart_data_raw]
            
            departments = Department.query.all()
            
        return render_template(
            "leave_requests.html",
            pending_leaves=pending_leaves,
            sick_leaves=sick_leaves,
            approved_leaves=approved_leaves,
            chart_data=chart_data,
            departments=departments,
            selected_department_id=selected_department_id
        )


    @app.route("/abwesenheit/genehmigen/<int:leave_id>")
    @admin_required
    def approve_leave(leave_id: int) -> str:
        """Genehmigt einen Abwesenheitsantrag."""
        leave = Leave.query.get_or_404(leave_id)
        leave.approved = True
        db.session.commit()
        flash("Antrag genehmigt.", "success")
        return redirect(url_for("leave_requests"))

    @app.route("/abwesenheit/ablehnen/<int:leave_id>")
    @admin_required
    def decline_leave(leave_id: int) -> str:
        """Lehnt einen Abwesenheitsantrag ab (löscht ihn)."""
        leave = Leave.query.get_or_404(leave_id)
        db.session.delete(leave)
        db.session.commit()
        flash("Antrag abgelehnt und gelöscht.", "info")
        return redirect(url_for("leave_requests"))

    @app.route("/produktivitaet")
    @admin_required
    def productivity_settings() -> str:
        """Zeigt die Produktivitätseinstellungen an."""
        departments = Department.query.order_by(Department.name).all()
        settings = ProductivitySettings.query.filter_by(is_active=True).all()
        
        # Erstelle ein Dictionary für einfachen Zugriff
        settings_dict = {}
        for setting in settings:
            key = setting.department_id if setting.department_id else 'global'
            settings_dict[key] = setting
        
        return render_template(
            "productivity_settings.html",
            departments=departments,
            settings_dict=settings_dict,
        )

    @app.route("/produktivitaet/speichern", methods=["POST"])
    @admin_required
    def save_productivity_settings() -> str:
        """Speichert die Produktivitätseinstellungen."""
        try:
            # Globale Einstellung
            global_value = request.form.get("global_productivity", type=float)
            if global_value:
                # Deaktiviere alte globale Einstellungen
                old_global = ProductivitySettings.query.filter_by(
                    department_id=None, is_active=True
                ).all()
                for setting in old_global:
                    setting.is_active = False
                
                # Erstelle neue globale Einstellung
                new_global = ProductivitySettings(
                    department_id=None,
                    productivity_value=global_value,
                    is_active=True
                )
                db.session.add(new_global)
            
            # Abteilungsspezifische Einstellungen
            departments = Department.query.all()
            for dept in departments:
                dept_value = request.form.get(f"dept_{dept.id}_productivity", type=float)
                if dept_value:
                    # Deaktiviere alte Einstellungen für diese Abteilung
                    old_dept = ProductivitySettings.query.filter_by(
                        department_id=dept.id, is_active=True
                    ).all()
                    for setting in old_dept:
                        setting.is_active = False
                    
                    # Erstelle neue Einstellung für diese Abteilung
                    new_dept = ProductivitySettings(
                        department_id=dept.id,
                        productivity_value=dept_value,
                        is_active=True
                    )
                    db.session.add(new_dept)
            
            db.session.commit()
            flash("Produktivitätseinstellungen wurden gespeichert.", "success")
        except Exception as e:
            db.session.rollback()
            flash(f"Fehler beim Speichern: {str(e)}", "danger")
        
        return redirect(url_for("productivity_settings"))

    @app.route("/auto-schedule")
    @admin_required
    def auto_schedule_form() -> str:
        """Formular für die automatische Schichtenerstellung."""
        from datetime import date
        today = date.today()
        current_user = get_current_user()
        employee_query = Employee.query.order_by(Employee.name.asc())
        if current_user and current_user.department_id:
            employee_query = employee_query.filter_by(department_id=current_user.department_id)

        employees = employee_query.all()
        positions = sorted({emp.position for emp in employees if emp.position})

        return render_template(
            "auto_schedule.html",
            current_month=today.month,
            current_year=today.year,
            employees=employees,
            positions=positions,
            is_department_admin=bool(current_user and current_user.department_id),
        )

    @app.route("/auto-schedule/create", methods=["POST"])
    @admin_required
    def create_auto_schedule() -> str:
        """Erstellt automatisch Schichten basierend auf Standard-Arbeitszeiten."""
        year = request.form.get("year", type=int)
        month = request.form.get("month", type=int)
        mode = request.form.get("mode", "all")  # "all", "position", "employee"
        dry_run = bool(request.form.get("dry_run"))
        current_user = get_current_user()
        restricted_department_id = (
            current_user.department_id
            if current_user and current_user.department_id
            else None
        )

        if not year or not month:
            flash("Bitte geben Sie Jahr und Monat an.", "warning")
            return redirect(url_for("auto_schedule_form"))

        try:
            if mode == "position":
                position = request.form.get("position", "").strip()
                if not position:
                    flash("Bitte geben Sie eine Position an.", "warning")
                    return redirect(url_for("auto_schedule_form"))

                if restricted_department_id:
                    allowed_positions = {
                        emp.position
                        for emp in Employee.query.filter_by(
                            department_id=restricted_department_id
                        )
                        if emp.position
                    }
                    if position not in allowed_positions:
                        flash(
                            "Sie können nur Positionen aus Ihrer Abteilung auswählen.",
                            "danger",
                        )
                        return redirect(url_for("auto_schedule_form"))

                result = create_default_shifts_for_employee_position(
                    position,
                    year,
                    month,
                    dry_run=dry_run,
                    department_id=restricted_department_id,
                )

                if dry_run:
                    flash(f"Vorschau: {result['total_created']} Schichten würden erstellt, {result['total_skipped']} übersprungen (Position: {position}).", "info")
                else:
                    flash(f"{result['total_created']} Schichten für Position '{position}' erstellt, {result['total_skipped']} übersprungen.", "success")

            elif mode == "employee":
                employee_id = request.form.get("employee_id", type=int)
                if not employee_id:
                    flash("Bitte wählen Sie einen Mitarbeiter aus.", "warning")
                    return redirect(url_for("auto_schedule_form"))

                employee = Employee.query.get(employee_id)

                if not employee or (
                    restricted_department_id
                    and employee.department_id != restricted_department_id
                ):
                    flash(
                        "Sie können nur Mitarbeiter aus Ihrer Abteilung auswählen.",
                        "danger",
                    )
                    return redirect(url_for("auto_schedule_form"))

                result = create_default_shifts_for_month(
                    year,
                    month,
                    employee_id=employee_id,
                    dry_run=dry_run,
                    department_id=restricted_department_id,
                )

                if dry_run:
                    flash(f"Vorschau: {result['total_created']} Schichten würden für {employee.name} erstellt, {result['total_skipped']} übersprungen.", "info")
                else:
                    flash(f"{result['total_created']} Schichten für {employee.name} erstellt, {result['total_skipped']} übersprungen.", "success")

            else:  # mode == "all"
                result = create_default_shifts_for_month(
                    year,
                    month,
                    employee_id=None,
                    dry_run=dry_run,
                    department_id=restricted_department_id,
                )

                if dry_run:
                    flash(f"Vorschau: {result['total_created']} Schichten würden erstellt, {result['total_skipped']} übersprungen.", "info")
                else:
                    flash(f"{result['total_created']} Schichten erstellt, {result['total_skipped']} übersprungen.", "success")
            
        except Exception as e:
            flash(f"Fehler beim Erstellen der Schichten: {str(e)}", "danger")
        
        return redirect(url_for("schedule", month=month, year=year))

    @app.route("/gesperrte-tage")
    @admin_required
    def blocked_days() -> str:
        """Zeigt die Übersicht der gesperrten Tage an."""
        blocked_days_list = BlockedDay.query.order_by(BlockedDay.date.asc()).all()
        return render_template("blocked_days.html", blocked_days=blocked_days_list)

    @app.route("/gesperrte-tage/hinzufuegen", methods=["GET", "POST"])
    @admin_required
    def add_blocked_day() -> str:
        """Fügt einen neuen gesperrten Tag hinzu."""
        if request.method == "POST":
            date_str = request.form.get("date", "").strip()
            name = request.form.get("name", "").strip()
            description = request.form.get("description", "").strip()
            block_type = request.form.get("block_type", "Feiertag").strip()
            
            if not date_str or not name:
                flash("Datum und Name sind erforderlich.", "warning")
                today_str = date.today().strftime('%Y-%m-%d')
                return render_template("add_blocked_day.html", date=date, today=today_str)
            
            try:
                blocked_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                
                # Prüfen, ob das Datum bereits gesperrt ist
                existing = BlockedDay.query.filter_by(date=blocked_date).first()
                if existing:
                    flash(f"Das Datum {blocked_date.strftime('%d.%m.%Y')} ist bereits als '{existing.name}' gesperrt.", "warning")
                    today_str = date.today().strftime('%Y-%m-%d')
                    return render_template("add_blocked_day.html", date=date, today=today_str)
                
                blocked_day = BlockedDay(
                    date=blocked_date,
                    name=name,
                    description=description,
                    block_type=block_type,
                    created_by=session.get("user_id")
                )
                
                db.session.add(blocked_day)
                db.session.commit()
                flash(f"Gesperrter Tag '{name}' am {blocked_date.strftime('%d.%m.%Y')} wurde hinzugefügt.", "success")
                return redirect(url_for("blocked_days"))
                
            except ValueError:
                flash("Ungültiges Datumsformat. Bitte verwenden Sie YYYY-MM-DD.", "danger")
                today_str = date.today().strftime('%Y-%m-%d')
                return render_template("add_blocked_day.html", date=date, today=today_str)
            except Exception as e:
                flash(f"Fehler beim Hinzufügen: {str(e)}", "danger")
                today_str = date.today().strftime('%Y-%m-%d')
                return render_template("add_blocked_day.html", date=date, today=today_str)
        
        today_str = date.today().strftime('%Y-%m-%d')
        return render_template("add_blocked_day.html", date=date, today=today_str)

    @app.route("/gesperrte-tage/loeschen/<int:blocked_day_id>")
    @admin_required
    def delete_blocked_day(blocked_day_id: int) -> str:
        """Löscht einen gesperrten Tag."""
        blocked_day = BlockedDay.query.get_or_404(blocked_day_id)
        name = blocked_day.name
        date_str = blocked_day.date.strftime('%d.%m.%Y')
        
        db.session.delete(blocked_day)
        db.session.commit()
        flash(f"Gesperrter Tag '{name}' vom {date_str} wurde gelöscht.", "info")
        return redirect(url_for("blocked_days"))

    @app.route("/system/benutzer")
    @admin_required
    def user_management() -> str:
        """Benutzerverwaltung - nur für Administratoren."""
        current_user = get_current_user()
        
        # Nur Super-Admins können alle Benutzer verwalten
        if current_user and current_user.department_id:
            # Abteilungsadmin sieht nur Benutzer seiner Abteilung
            users = Employee.query.filter(
                Employee.username.isnot(None),
                Employee.department_id == current_user.department_id
            ).order_by(Employee.name).all()
        else:
            # Super-Admin sieht alle Benutzer
            users = Employee.query.filter(Employee.username.isnot(None)).order_by(Employee.name).all()
        
        departments = Department.query.order_by(Department.name).all()
        return render_template("user_management.html", users=users, departments=departments)

    @app.route("/system/benutzer/<int:user_id>/super-admin", methods=["POST"])
    @admin_required
    def make_user_super_admin(user_id: int) -> str:
        """Macht einen Benutzer zum Super-Administrator."""
        current_user = get_current_user()
        
        # Nur Super-Admins können andere zu Super-Admins machen
        if current_user and current_user.department_id:
            flash("Nur Super-Administratoren können andere Benutzer zu Super-Administratoren machen.", "danger")
            return redirect(url_for("user_management"))
        
        user = Employee.query.get_or_404(user_id)
        
        # Status vor der Änderung
        old_status = "Super-Admin" if (user.is_admin and not user.department_id) else \
                     "Abteilungs-Admin" if user.is_admin else "Mitarbeiter"
        
        user.is_admin = True
        user.department_id = None  # Vollzugriff auf alle Abteilungen
        
        try:
            db.session.commit()
            flash(f"✅ {user.name} ist jetzt Super-Administrator mit Vollzugriff auf alle Abteilungen.", "success")
        except Exception as e:
            db.session.rollback()
            flash(f"Fehler beim Upgrade: {str(e)}", "danger")
        
        return redirect(url_for("user_management"))

    @app.route("/system/benutzer/<int:user_id>/department-admin", methods=["POST"])
    @admin_required
    def make_user_department_admin(user_id: int) -> str:
        """Macht einen Benutzer zum Abteilungsadministrator."""
        current_user = get_current_user()
        
        # Nur Super-Admins können Abteilungsadmins ernennen
        if current_user and current_user.department_id:
            flash("Nur Super-Administratoren können Abteilungsadministratoren ernennen.", "danger")
            return redirect(url_for("user_management"))
        
        user = Employee.query.get_or_404(user_id)
        department_id = request.form.get("department_id", type=int)
        
        if not department_id:
            flash("Bitte wählen Sie eine Abteilung aus.", "warning")
            return redirect(url_for("user_management"))
        
        department = Department.query.get_or_404(department_id)
        
        user.is_admin = True
        user.department_id = department_id
        
        try:
            db.session.commit()
            flash(f"✅ {user.name} ist jetzt Administrator der Abteilung '{department.name}'.", "success")
        except Exception as e:
            db.session.rollback()
            flash(f"Fehler beim Upgrade: {str(e)}", "danger")
        
        return redirect(url_for("user_management"))

    @app.route("/system/benutzer/<int:user_id>/remove-admin", methods=["POST"])
    @admin_required
    def remove_user_admin(user_id: int) -> str:
        """Entfernt Administrator-Rechte von einem Benutzer."""
        current_user = get_current_user()
        
        # Nur Super-Admins können Admin-Rechte entziehen
        if current_user and current_user.department_id:
            flash("Nur Super-Administratoren können Administrator-Rechte entziehen.", "danger")
            return redirect(url_for("user_management"))
        
        user = Employee.query.get_or_404(user_id)
        
        # Verhindere, dass sich der letzte Super-Admin selbst degradiert
        if user.id == current_user.id:
            super_admins = Employee.query.filter_by(is_admin=True, department_id=None).count()
            if super_admins <= 1:
                flash("Sie können sich nicht selbst degradieren, da Sie der einzige Super-Administrator sind.", "warning")
                return redirect(url_for("user_management"))
        
        old_status = "Super-Admin" if (user.is_admin and not user.department_id) else \
                     "Abteilungs-Admin" if user.is_admin else "Mitarbeiter"
        
        user.is_admin = False
        # department_id bleibt erhalten für normale Mitarbeiter
        
        try:
            db.session.commit()
            flash(f"✅ Administrator-Rechte von {user.name} wurden entfernt.", "info")
        except Exception as e:
            db.session.rollback()
            flash(f"Fehler beim Entfernen der Rechte: {str(e)}", "danger")
        
        return redirect(url_for("user_management"))

    @app.route("/api/employee-group-order", methods=["GET"])
    @login_required
    def get_employee_group_order():
        """Gibt die aktuelle Reihenfolge der Benutzergruppen zurück."""
        from models import EmployeeGroupOrder
        from flask import jsonify
        
        group_order = EmployeeGroupOrder.query.order_by(EmployeeGroupOrder.order_position).all()
        
        if not group_order:
            # Standard-Reihenfolge zurückgeben
            return jsonify({
                'groups': [
                    {'name': 'Vollzeit', 'position': 0},
                    {'name': 'Teilzeit', 'position': 1},
                    {'name': 'Aushilfe', 'position': 2}
                ]
            })
        
        return jsonify({
            'groups': [
                {'name': g.group_name, 'position': g.order_position}
                for g in group_order
            ]
        })

    @app.route("/api/employee-group-order", methods=["POST"])
    @admin_required
    def update_employee_group_order():
        """Aktualisiert die Reihenfolge der Benutzergruppen (nur für Systemadmins)."""
        from models import EmployeeGroupOrder
        from flask import jsonify
        
        current_user = get_current_user()
        
        # Nur Super-Admins (Systemadmins) ohne Abteilung dürfen die Reihenfolge ändern
        if current_user and current_user.department_id:
            return jsonify({'error': 'Nur Systemadministratoren können die Reihenfolge ändern.'}), 403
        
        try:
            data = request.get_json()
            groups = data.get('groups', [])
            
            if not groups:
                return jsonify({'error': 'Keine Gruppen angegeben.'}), 400
            
            # Lösche alte Reihenfolge
            EmployeeGroupOrder.query.delete()
            
            # Speichere neue Reihenfolge
            for group in groups:
                group_order = EmployeeGroupOrder(
                    group_name=group['name'],
                    order_position=group['position']
                )
                db.session.add(group_order)
            
            db.session.commit()
            
            return jsonify({'success': True, 'message': 'Reihenfolge erfolgreich aktualisiert.'})
        
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': f'Fehler beim Aktualisieren: {str(e)}'}), 500

    return app

if __name__ == "__main__":
    app = create_app()
    app.run(debug=True, host='0.0.0.0', port=5001)

