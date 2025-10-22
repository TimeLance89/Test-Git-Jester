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
import secrets
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Tuple
import pandas as pd
from sqlalchemy.exc import IntegrityError
from sqlalchemy import or_, and_, func
from sqlalchemy.orm import joinedload

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    session,
    jsonify,
    Response,
)

from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash

from models import (
    db,
    init_db,
    Department,
    Employee,
    Shift,
    Leave,
    ProductivitySettings,
    BlockedDay,
    Notification,
)
from auto_schedule import create_default_shifts_for_month, create_default_shifts_for_employee_position

LEAVE_TYPES_EXCLUDED_FROM_PRODUCTIVITY = {"Urlaub", "Krank"}

def calculate_productivity_for_dates(dates: List[date], department_id: int | None = None) -> Dict[date, Dict[str, float]]:
    """Berechnet Produktivitätskennzahlen für eine beliebige Liste an Tagen."""

    if not dates:
        return {}

    relevant_days = sorted(set(dates))
    start_date = relevant_days[0]
    end_date = relevant_days[-1]

    shifts_query = Shift.query.filter(
        Shift.date >= start_date,
        Shift.date <= end_date,
        Shift.approved == True
    )

    if department_id:
        shifts_query = shifts_query.join(Employee).filter(Employee.department_id == department_id)

    shifts = shifts_query.all()

    leaves_query = Leave.query.filter(
        Leave.start_date <= end_date,
        Leave.end_date >= start_date,
        Leave.approved == True
    ).all()

    blocked_days = BlockedDay.query.filter(
        BlockedDay.date >= start_date,
        BlockedDay.date <= end_date,
    ).all()
    blocked_dates = {blocked.date for blocked in blocked_days}

    productivity_settings = {}
    all_settings = ProductivitySettings.query.filter_by(is_active=True).all()

    for setting in all_settings:
        if setting.department_id:
            productivity_settings[setting.department_id] = setting.productivity_value
        else:
            productivity_settings['global'] = setting.productivity_value

    default_productivity = productivity_settings.get('global', 40.0)

    shifts_by_day: Dict[date, List[Shift]] = {day: [] for day in relevant_days}
    for shift in shifts:
        if shift.date in blocked_dates:
            continue
        if shift.date in shifts_by_day:
            shifts_by_day[shift.date].append(shift)

    leaves_by_day: Dict[date, List[Leave]] = {day: [] for day in relevant_days}
    for leave in leaves_query:
        current = max(leave.start_date, start_date)
        last = min(leave.end_date, end_date)
        while current <= last:
            if current in leaves_by_day:
                leaves_by_day[current].append(leave)
            current += timedelta(days=1)

    daily_data: Dict[date, Dict[str, float]] = {}

    for day in relevant_days:
        if day in blocked_dates:
            daily_data[day] = {
                "aushilfen_za_std": 0.0,
                "feste_std": 0.0,
                "gesamt_std": 0.0,
                "produktivitaet": 0.0,
                "teile": 0.0,
                "department_breakdown": {},
                "is_blocked": True,
            }
            continue

        daily_shifts = shifts_by_day.get(day, [])
        daily_leaves = leaves_by_day.get(day, [])

        department_hours: Dict[int, Dict[str, float]] = {}
        aushilfen_hours = 0.0
        feste_hours = 0.0

        for shift in daily_shifts:
            is_on_leave = any(
                leave.employee_id == shift.employee_id and leave.leave_type in LEAVE_TYPES_EXCLUDED_FROM_PRODUCTIVITY
                for leave in daily_leaves
            )

            if is_on_leave:
                continue

            dept_id = shift.employee.department_id
            dept_productivity = productivity_settings.get(dept_id, default_productivity)

            if dept_id not in department_hours:
                department_hours[dept_id] = {
                    'hours': 0.0,
                    'productivity': dept_productivity,
                    'teile': 0.0,
                }

            department_hours[dept_id]['hours'] += shift.hours

            if shift.employee.monthly_hours and shift.employee.monthly_hours >= 160:
                feste_hours += shift.hours
            else:
                aushilfen_hours += shift.hours

        gesamt_hours = aushilfen_hours + feste_hours
        total_teile = 0.0

        distinct_productivities = {dept['productivity'] for dept in department_hours.values()}
        if len(distinct_productivities) <= 1:
            used_productivity = next(iter(distinct_productivities), default_productivity)
            total_teile = gesamt_hours * used_productivity
        else:
            total_weighted_productivity = 0.0
            for dept_data in department_hours.values():
                total_teile += dept_data['hours'] * dept_data['productivity']
                total_weighted_productivity += dept_data['hours'] * dept_data['productivity']

            used_productivity = (
                total_weighted_productivity / gesamt_hours if gesamt_hours > 0 else default_productivity
            )

        daily_data[day] = {
            "aushilfen_za_std": aushilfen_hours,
            "feste_std": feste_hours,
            "gesamt_std": gesamt_hours,
            "produktivitaet": round(used_productivity, 1),
            "teile": round(total_teile, 0),
            "department_breakdown": department_hours,
        }

    return daily_data


def get_productivity_data(year: int, month: int, department_id: int = None):
    """Berechnet die Produktivitätsdaten basierend auf geplanten Stunden und Produktivitätseinstellungen."""
    import calendar
    from datetime import date

    num_days = calendar.monthrange(year, month)[1]
    month_days = [date(year, month, day) for day in range(1, num_days + 1)]

    daily_data = calculate_productivity_for_dates(month_days, department_id)

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
    tracks_overtime = ((employee.position or "").lower() == "aushilfe") if employee else False
    
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
        if tracks_overtime:
            overtime_hours = max(0, worked_hours - proportional_target)
        else:
            overtime_hours = 0
        
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
        overtime_hours = max(0, worked_hours - target_hours) if tracks_overtime else 0
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
    total_target_hours = 0
    total_worked_hours = 0
    assistant_count = 0
    assistants_without_hours = 0
    
    for emp_id, data in hours_summary.items():
        employee = Employee.query.get(emp_id)
        if not employee:
            continue
        
        # Nur Aushilfen in den Kapazitätsberechnungen berücksichtigen
        if employee.position != 'Aushilfe':
            continue
            
        data['employee_name'] = employee.name
        data['department'] = employee.department.name if employee.department else 'Keine Abteilung'
        
        remaining_hours = data.get('remaining_hours', 0) or 0
        overtime_hours = data.get('overtime_hours', 0) or 0
        target_hours = data.get('target_hours', 0) or 0
        worked_hours = data.get('worked_hours', 0) or 0

        total_remaining_hours += remaining_hours
        total_overtime_hours += overtime_hours
        total_target_hours += target_hours
        total_worked_hours += worked_hours
        assistant_count += 1
        if worked_hours == 0:
            assistants_without_hours += 1

        data['remaining_hours'] = remaining_hours
        data['overtime_hours'] = overtime_hours
        data['target_hours'] = target_hours
        data['worked_hours'] = worked_hours

        if remaining_hours > 20:  # Mehr als 20 Stunden übrig
            underutilized.append(data)
        elif overtime_hours > 10:  # Mehr als 10 Überstunden
            overutilized.append(data)
        else:
            balanced.append(data)

    # Sortiere Listen
    underutilized.sort(key=lambda x: x['remaining_hours'], reverse=True)
    overutilized.sort(key=lambda x: x['overtime_hours'], reverse=True)

    avg_remaining_hours = (total_remaining_hours / assistant_count) if assistant_count else 0
    avg_overtime_hours = (total_overtime_hours / assistant_count) if assistant_count else 0
    coverage_rate = (total_worked_hours / total_target_hours * 100) if total_target_hours else 0
    net_available_hours = total_remaining_hours - total_overtime_hours

    return {
        'underutilized': underutilized,
        'overutilized': overutilized,
        'balanced': balanced,
        'total_remaining_hours': total_remaining_hours,
        'total_overtime_hours': total_overtime_hours,
        'total_target_hours': total_target_hours,
        'total_worked_hours': total_worked_hours,
        'assistant_count': assistant_count,
        'assistants_without_hours': assistants_without_hours,
        'underutilized_count': len(underutilized),
        'overutilized_count': len(overutilized),
        'balanced_count': len(balanced),
        'avg_remaining_hours': avg_remaining_hours,
        'avg_overtime_hours': avg_overtime_hours,
        'coverage_rate': coverage_rate,
        'net_available_hours': net_available_hours,
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


def super_admin_required(view):
    """Stellt sicher, dass nur Systemadministratoren Zugriff erhalten."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))

        current_user = get_current_user()
        is_super_admin = bool(
            current_user and current_user.is_admin and not current_user.department_id
        )

        if not is_super_admin:
            flash("Nur Systemadministratoren können diesen Bereich öffnen.", "danger")
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

def _create_notification(recipient_id: int, message: str, link: str | None = None) -> None:
    """Erzeugt eine Benachrichtigung für einen bestimmten Empfänger."""

    if not recipient_id or not message:
        return

    trimmed_message = message[:255]
    trimmed_link = link[:255] if link else None

    notification = Notification(
        recipient_id=recipient_id,
        message=trimmed_message,
        link=trimmed_link,
    )
    db.session.add(notification)


def _build_shift_request_message(employee: Employee, shift_date: date) -> str:
    """Erstellt eine konsistente Meldung für neue Einsatzanträge."""

    display_date = shift_date.strftime("%d.%m.%Y")
    return f"{employee.name} hat einen Einsatz am {display_date} eingereicht."


def _build_leave_request_message(
    employee: Employee,
    leave_type: str,
    start_date: date,
    end_date: date,
) -> str:
    """Erstellt eine konsistente Meldung für neue Abwesenheitsanträge."""

    if start_date == end_date:
        date_range = start_date.strftime("%d.%m.%Y")
    else:
        date_range = (
            f"{start_date.strftime('%d.%m.%Y')} bis {end_date.strftime('%d.%m.%Y')}"
        )
    return f"{employee.name} hat {leave_type} für {date_range} beantragt."


def _clear_request_notifications(message: str, link: str | None = None) -> None:
    """Entfernt Benachrichtigungen zu erledigten Vorgängen für andere Leitungen."""

    if not message:
        return

    query = Notification.query.filter(Notification.message == message)

    if link is not None:
        query = query.filter(Notification.link == link)

    query.delete(synchronize_session=False)


def notify_admins_of_request(employee: Employee, message: str, link: str | None = None) -> None:
    """Informiert alle relevanten Administratoren über einen neuen Antrag."""

    if not employee:
        return

    admin_query = Employee.query.filter(Employee.is_admin.is_(True))

    if employee.department_id:
        admin_query = admin_query.filter(
            or_(
                Employee.department_id == employee.department_id,
                Employee.department_id.is_(None),
            )
        )

    for admin in admin_query.all():
        _create_notification(admin.id, message, link)


def notify_employee(employee_id: int, message: str, link: str | None = None) -> None:
    """Erzeugt eine Benachrichtigung für einen einzelnen Mitarbeiter."""

    if not employee_id:
        return

    _create_notification(employee_id, message, link)


def create_app() -> Flask:

    """Erzeugt und konfiguriert die Flask‑Anwendung."""
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///planner.db"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SECRET_KEY"] = secrets.token_hex(32)

    init_db(app)

    @app.context_processor
    def inject_pending_counts():
        context = dict(
            pending_shifts_count=0,
            pending_leaves_count=0,
            notifications=[],
            unread_notifications_count=0,
        )

        user_id = session.get("user_id")
        if user_id:
            current_user = get_current_user()
            if current_user:
                notifications_query = Notification.query.filter_by(
                    recipient_id=current_user.id
                ).order_by(Notification.created_at.desc())

                context["notifications"] = notifications_query.limit(10).all()
                context["unread_notifications_count"] = Notification.query.filter_by(
                    recipient_id=current_user.id,
                    is_read=False,
                ).count()

                if session.get("is_admin"):
                    pending_shifts, pending_leaves = get_pending_requests_count()
                    context["pending_shifts_count"] = pending_shifts
                    context["pending_leaves_count"] = pending_leaves
        elif session.get("is_admin"):
            pending_shifts, pending_leaves = get_pending_requests_count()
            context["pending_shifts_count"] = pending_shifts
            context["pending_leaves_count"] = pending_leaves

        return context

    @app.route("/notifications/mark-read", methods=["POST"])
    @login_required
    def mark_notifications_read():
        user_id = session.get("user_id")
        if not user_id:
            return {"status": "unauthorized"}, 401

        notifications = Notification.query.filter_by(
            recipient_id=user_id,
            is_read=False,
        ).all()

        for notification in notifications:
            notification.is_read = True

        db.session.commit()

        return {"status": "ok"}

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
                    "preferred_schedule_view": (
                        "ALTER TABLE employee "
                        "ADD COLUMN preferred_schedule_view VARCHAR(20) NOT NULL DEFAULT 'month'"
                    ),
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
        """Startseite mit interaktiver Übersicht über echte Teamdaten."""
        current_user = get_current_user()
        is_admin = bool(session.get("is_admin"))

        department_id = current_user.department_id if current_user and current_user.department_id else None

        if is_admin:
            if department_id:
                # Abteilungsadmins erhalten Kennzahlen für ihren Verantwortungsbereich
                employee_count = Employee.query.filter_by(department_id=department_id).count()
                department_count = 1
                pending_leaves = (
                    db.session.query(Leave)
                    .join(Employee)
                    .filter(Leave.approved == False, Employee.department_id == department_id)
                    .count()
                )
            else:
                # Systemadmins erhalten die globale Sicht
                employee_count = Employee.query.count()
                department_count = Department.query.count()
                pending_leaves = Leave.query.filter_by(approved=False).count()
        else:
            # Mitarbeitende erhalten nur persönliche Kennzahlen
            if department_id:
                employee_count = Employee.query.filter_by(department_id=department_id).count()
                department_count = 1
            else:
                employee_count = Employee.query.count()
                department_count = Department.query.count()

            pending_leaves = (
                Leave.query.filter(
                    Leave.employee_id == current_user.id,
                    Leave.approved == False,
                ).count()
                if current_user
                else 0
            )

        today = date.today()
        week_dates = [today + timedelta(days=offset) for offset in range(7)]
        week_start, week_end = week_dates[0], week_dates[-1]
        weekday_short_names = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

        shifts_query = Shift.query.filter(Shift.date >= week_start, Shift.date <= week_end)
        leaves_query = Leave.query.filter(Leave.end_date >= week_start, Leave.start_date <= week_end)

        if not is_admin and current_user:
            shifts_query = shifts_query.filter(Shift.employee_id == current_user.id)
            leaves_query = leaves_query.filter(Leave.employee_id == current_user.id)
        elif department_id:
            shifts_query = shifts_query.join(Employee).filter(Employee.department_id == department_id)
            leaves_query = leaves_query.join(Employee).filter(Employee.department_id == department_id)

        shifts = (
            shifts_query.options(joinedload(Shift.employee).joinedload(Employee.department)).all()
        )
        leaves = (
            leaves_query.options(joinedload(Leave.employee).joinedload(Employee.department)).all()
        )

        approved_shifts = [shift for shift in shifts if shift.approved]

        hours_by_day = {day: 0.0 for day in week_dates}
        employees_by_day: Dict[date, set[int]] = {day: set() for day in week_dates}
        scheduled_employee_hours: Dict[int, float] = defaultdict(float)
        unique_employees_scheduled: set[int] = set()

        for shift in approved_shifts:
            hours_by_day[shift.date] = hours_by_day.get(shift.date, 0.0) + shift.hours
            employees_by_day.setdefault(shift.date, set()).add(shift.employee_id)
            unique_employees_scheduled.add(shift.employee_id)
            scheduled_employee_hours[shift.employee_id] += shift.hours

        employees_on_leave_by_day: Dict[date, set[int]] = {day: set() for day in week_dates}
        leave_type_counter: Counter[str] = Counter()
        leave_status_counter: Counter[str] = Counter()

        for leave in leaves:
            leave_type_counter[leave.leave_type] += 1
            leave_status_counter["approved" if leave.approved else "pending"] += 1

            current = max(leave.start_date, week_start)
            last = min(leave.end_date, week_end)
            while current <= last:
                employees_on_leave_by_day.setdefault(current, set()).add(leave.employee_id)
                current += timedelta(days=1)

        team_capacity = []
        total_hours = 0.0
        for day in week_dates:
            scheduled_count = len(employees_by_day.get(day, set()))
            on_leave_count = len(employees_on_leave_by_day.get(day, set()))
            available_count = max(employee_count - on_leave_count, 0)
            hours = round(hours_by_day.get(day, 0.0), 2)
            coverage = round((scheduled_count / available_count) * 100, 1) if available_count else 0.0

            total_hours += hours

            team_capacity.append(
                {
                    "date_iso": day.isoformat(),
                    "date_label": f"{weekday_short_names[day.weekday()]} {day.strftime('%d.%m.')}",
                    "hours": hours,
                    "scheduled": scheduled_count,
                    "on_leave": on_leave_count,
                    "available": available_count,
                    "coverage": coverage,
                }
            )

        coverage_average = (
            round(
                sum(entry["coverage"] for entry in team_capacity) / len(team_capacity),
                1,
            )
            if team_capacity
            else 0.0
        )

        coverage_today = next(
            (entry for entry in team_capacity if entry["date_iso"] == today.isoformat()),
            None,
        )

        if coverage_today:
            if coverage_today["available"] == 0 and coverage_today["scheduled"] == 0:
                coverage_message = "Heute liegen keine Einsätze für das Team vor."
            elif coverage_today["coverage"] >= 90:
                coverage_message = "Starke Besetzung – alle Schichten sind nahezu vollständig gedeckt."
            elif coverage_today["coverage"] >= 70:
                coverage_message = "Gute Abdeckung. Behalte verbleibende Lücken im Blick."
            else:
                coverage_message = "Plane zusätzliche Einsätze, um offene Schichten zu schließen."
        else:
            coverage_message = "Für die aktuelle Woche wurden noch keine Einsätze geplant."

        capacity_highlights = [
            {
                "date_label": entry["date_label"],
                "coverage": entry["coverage"],
                "scheduled": entry["scheduled"],
                "available": entry["available"],
            }
            for entry in sorted(team_capacity, key=lambda item: item["coverage"])[:3]
        ]

        week_chart = {
            "labels": [entry["date_label"] for entry in team_capacity],
            "hours": [entry["hours"] for entry in team_capacity],
            "scheduled": [entry["scheduled"] for entry in team_capacity],
            "on_leave": [entry["on_leave"] for entry in team_capacity],
        }

        leave_status_overview = {
            "approved": leave_status_counter.get("approved", 0),
            "pending": leave_status_counter.get("pending", 0),
        }

        leave_type_breakdown = [
            {"type": leave_type, "count": count}
            for leave_type, count in sorted(
                leave_type_counter.items(), key=lambda item: item[1], reverse=True
            )
        ]

        department_distribution = []
        if department_id:
            department = Department.query.get(department_id)
            department_distribution.append(
                {
                    "name": department.name if department else "Abteilung",
                    "count": employee_count,
                }
            )
        else:
            department_counts = (
                db.session.query(Department.name, func.count(Employee.id))
                .outerjoin(Employee)
                .group_by(Department.id)
                .order_by(Department.name)
                .all()
            )
            department_distribution = [
                {"name": name or "Ohne Zuordnung", "count": count}
                for name, count in department_counts
            ]

        employee_lookup = {}
        for shift in approved_shifts:
            if shift.employee:
                employee_lookup[shift.employee_id] = shift.employee.name

        top_contributors = [
            {"name": employee_lookup.get(emp_id, "Mitarbeiter"), "hours": round(hours, 2)}
            for emp_id, hours in sorted(
                scheduled_employee_hours.items(), key=lambda item: item[1], reverse=True
            )[:5]
        ]

        personal_week_overview = {"hours": 0.0, "shift_count": 0, "leave_days": 0, "pending_leaves": 0}
        if current_user:
            personal_week_shifts = [
                shift for shift in approved_shifts if shift.employee_id == current_user.id
            ]
            personal_week_overview["hours"] = round(
                sum(shift.hours for shift in personal_week_shifts),
                1,
            )
            personal_week_overview["shift_count"] = len(personal_week_shifts)
            personal_week_overview["leave_days"] = sum(
                1 for employees in employees_on_leave_by_day.values() if current_user.id in employees
            )
            personal_week_overview["pending_leaves"] = sum(
                1 for leave in leaves if leave.employee_id == current_user.id and not leave.approved
            )

        upcoming_events = []
        for shift in approved_shifts:
            event_title = shift.shift_type or "Einsatz"
            event_employee = (
                shift.employee.short_code
                or shift.employee.name
                if shift.employee
                else "Unbekannt"
            )
            event_department = (
                shift.employee.department.name
                if shift.employee and shift.employee.department
                else None
            )
            upcoming_events.append(
                (
                    shift.date,
                    {
                        "type": "shift",
                        "title": event_title,
                        "employee": event_employee,
                        "employee_id": shift.employee_id,
                        "hours": round(shift.hours, 2),
                        "department": event_department,
                    },
                )
            )

        for leave in leaves:
            event_department = (
                leave.employee.department.name
                if leave.employee and leave.employee.department
                else None
            )
            upcoming_events.append(
                (
                    leave.start_date,
                    {
                        "type": "leave",
                        "title": leave.leave_type,
                        "employee": leave.employee.name if leave.employee else "Unbekannt",
                        "employee_id": leave.employee_id,
                        "approved": bool(leave.approved),
                        "start": leave.start_date.strftime("%d.%m."),
                        "end": leave.end_date.strftime("%d.%m."),
                        "department": event_department,
                    },
                )
            )

        if not is_admin and current_user:
            upcoming_events = [
                (event_date, data)
                for event_date, data in upcoming_events
                if data.get("employee_id") == current_user.id
            ]

        upcoming_events = [
            {
                **data,
                "date": event_date.isoformat(),
                "date_label": event_date.strftime("%d.%m.%Y"),
            }
            for event_date, data in sorted(upcoming_events, key=lambda item: item[0])[:6]
        ]

        next_shift_query = Shift.query.filter(Shift.date >= today, Shift.approved == True)
        if current_user and not is_admin:
            next_shift_query = next_shift_query.filter(Shift.employee_id == current_user.id)
        elif department_id:
            next_shift_query = next_shift_query.join(Employee).filter(Employee.department_id == department_id)

        next_shift = next_shift_query.order_by(Shift.date.asc(), Shift.id.asc()).first()
        next_shift_info = None
        if next_shift:
            next_shift_info = {
                "title": next_shift.shift_type or "Einsatz",
                "hours": round(next_shift.hours, 2),
                "date_label": next_shift.date.strftime("%d.%m.%Y"),
                "employee": next_shift.employee.name if next_shift.employee else None,
                "department": (
                    next_shift.employee.department.name
                    if next_shift.employee and next_shift.employee.department
                    else None
                ),
                "is_personal": current_user is not None and next_shift.employee_id == current_user.id,
            }

        next_pending_leave_query = Leave.query.filter_by(approved=False)
        if not is_admin and current_user:
            next_pending_leave_query = next_pending_leave_query.filter(
                Leave.employee_id == current_user.id
            )
        elif department_id:
            next_pending_leave_query = next_pending_leave_query.join(Employee).filter(
                Employee.department_id == department_id
            )
        next_pending_leave = next_pending_leave_query.order_by(Leave.start_date.asc()).first()
        next_pending_leave_info = None
        if next_pending_leave:
            next_pending_leave_info = {
                "employee": next_pending_leave.employee.name if next_pending_leave.employee else None,
                "date_range": (
                    f"{next_pending_leave.start_date.strftime('%d.%m.%Y')} – "
                    f"{next_pending_leave.end_date.strftime('%d.%m.%Y')}"
                ),
                "type": next_pending_leave.leave_type,
            }

        approval_window_start = today - timedelta(days=30)
        approval_window_end = today + timedelta(days=30)
        approval_query = Leave.query.filter(
            Leave.start_date >= approval_window_start,
            Leave.start_date <= approval_window_end,
        )
        if not is_admin and current_user:
            approval_query = approval_query.filter(Leave.employee_id == current_user.id)
        elif department_id:
            approval_query = approval_query.join(Employee).filter(Employee.department_id == department_id)
        approval_leaves = approval_query.all()
        approval_rate = None
        if approval_leaves:
            approved_count = sum(1 for leave in approval_leaves if leave.approved)
            approval_rate = round((approved_count / len(approval_leaves)) * 100, 1)

        week_window_label = f"{week_start.strftime('%d.%m.%Y')} – {week_end.strftime('%d.%m.%Y')}"

        personal_day_overview = []
        if current_user and not is_admin:
            for entry in team_capacity:
                status = "free"
                description = "Keine Einsätze geplant."
                if entry["scheduled"] > 0:
                    status = "shift"
                    description = f"{entry['hours']} Std eingeplant."
                elif entry["on_leave"] > 0:
                    status = "leave"
                    description = "Als abwesend markiert."

                personal_day_overview.append(
                    {
                        "date_label": entry["date_label"],
                        "status": status,
                        "description": description,
                    }
                )

        return render_template(
            "index.html",
            employee_count=employee_count,
            department_count=department_count,
            pending_leaves=pending_leaves,
            current_user=current_user,
            team_capacity=team_capacity,
            week_chart=week_chart,
            leave_status_overview=leave_status_overview,
            leave_type_breakdown=leave_type_breakdown,
            department_distribution=department_distribution,
            coverage_average=coverage_average,
            coverage_today=coverage_today,
            coverage_message=coverage_message,
            capacity_highlights=capacity_highlights,
            total_hours=round(total_hours, 1),
            unique_employees=len(unique_employees_scheduled),
            personal_week_overview=personal_week_overview,
            top_contributors=top_contributors,
            upcoming_events=upcoming_events,
            next_shift_info=next_shift_info,
            next_pending_leave=next_pending_leave_info,
            approval_rate=approval_rate,
            week_window_label=week_window_label,
            personal_day_overview=personal_day_overview,
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
        
        search_query = (request.args.get("q") or "").strip()
        position_filter = (request.args.get("position") or "").strip()
        valid_positions = {"Vollzeit", "Teilzeit", "Aushilfe"}
        if position_filter not in valid_positions:
            position_filter = ""

        # Abteilungsbasierte Filterung
        current_user = get_current_user()
        if current_user and current_user.department_id:
            # Nur Mitarbeiter der eigenen Abteilung anzeigen
            employee_query = Employee.query.filter_by(department_id=current_user.department_id)
            departments = Department.query.filter_by(id=current_user.department_id).all()
        else:
            # Super-Admin ohne Abteilung sieht alle
            employee_query = Employee.query
            departments = Department.query.order_by(Department.name).all()

        if search_query:
            like_pattern = f"%{search_query}%"
            employee_query = employee_query.filter(
                or_(
                    Employee.name.ilike(like_pattern),
                    Employee.email.ilike(like_pattern),
                    Employee.short_code.ilike(like_pattern),
                )
            )

        if position_filter:
            employee_query = employee_query.filter(Employee.position == position_filter)

        employees = employee_query.order_by(Employee.name).all()

        # Berechne Reststunden für alle Mitarbeiter (abteilungsbasiert)
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
            search_query=search_query,
            selected_position=position_filter,
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

        base_employee_count = employee_query.count()

        search_term = request.args.get("search", "").strip()
        if search_term:
            employee_query = employee_query.filter(Employee.name.ilike(f"%{search_term}%"))

        available_positions = [
            value
            for (value,) in (
                db.session.query(Employee.position)
                .filter(Employee.position.isnot(None))
                .filter(Employee.position != "")
                .distinct()
                .order_by(Employee.position)
                .all()
            )
        ]
        has_unassigned_positions = bool(
            employee_query.filter(
                or_(Employee.position.is_(None), Employee.position == "")
            ).count()
        )

        position_filter_options: List[Dict[str, str]] = [
            {"value": value, "label": value} for value in available_positions
        ]
        if has_unassigned_positions:
            position_filter_options.append({"value": "__NONE__", "label": "Ohne Gruppe"})

        has_position_filter = request.args.get("positions_filter") == "1"
        requested_positions = request.args.getlist("position")
        selected_named_positions = [
            position for position in requested_positions if position in available_positions
        ]
        include_unassigned_selected = "__NONE__" in requested_positions and has_unassigned_positions

        if has_position_filter:
            position_filters = []
            if selected_named_positions:
                position_filters.append(Employee.position.in_(selected_named_positions))
            if include_unassigned_selected:
                position_filters.append(or_(Employee.position.is_(None), Employee.position == ""))

            if position_filters:
                employee_query = employee_query.filter(or_(*position_filters))
                employees = employee_query.order_by(Employee.name).all()
            else:
                employees = []
        else:
            employees = employee_query.order_by(Employee.name).all()

        applied_position_filters: List[str] = []
        if has_position_filter:
            applied_position_filters = list(selected_named_positions)
            if include_unassigned_selected:
                applied_position_filters.append("Ohne Gruppe")

        selected_position_values = []
        if has_position_filter:
            selected_position_values = list(selected_named_positions)
            if include_unassigned_selected:
                selected_position_values.append("__NONE__")
        else:
            selected_position_values = [option["value"] for option in position_filter_options]

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
            "total_target_hours": 0.0,
            "total_proportional_target": 0.0,
            "total_remaining_hours": 0.0,
        }

        restrict_overtime_to_aushilfe = bool(
            current_user
            and current_user.department_id
            and not is_super_admin
        )

        for employee in employees:
            summary = hours_summary.get(employee.id, {})
            worked_hours = float(summary.get("worked_hours", 0))
            overtime_hours = float(summary.get("overtime_hours", 0))
            if restrict_overtime_to_aushilfe and employee.position != "Aushilfe":
                overtime_hours = 0.0
            target_hours = float(summary.get("target_hours", 0) or 0)
            proportional_target = float(summary.get("proportional_target", target_hours))
            remaining_hours = float(summary.get("remaining_hours", 0))
            sick_days = sick_days_by_employee.get(employee.id, 0)
            usa_days = usa_days_by_employee.get(employee.id, 0)

            totals["total_hours"] += worked_hours
            totals["total_overtime"] += overtime_hours
            totals["total_sick_days"] += sick_days
            totals["total_usa_days"] += usa_days
            totals["total_target_hours"] += target_hours
            totals["total_proportional_target"] += proportional_target
            totals["total_remaining_hours"] += remaining_hours

            progress_to_date = 0.0
            if proportional_target:
                progress_to_date = (worked_hours / proportional_target) * 100

            monthly_completion = 0.0
            if target_hours:
                monthly_completion = (worked_hours / target_hours) * 100
            progress_to_date_clamped = min(progress_to_date, 100.0)

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
                    "progress_to_date": progress_to_date,
                    "progress_to_date_clamped": progress_to_date_clamped,
                    "monthly_completion": monthly_completion,
                }
            )

        employee_count = len(report_rows)
        totals["average_hours"] = totals["total_hours"] / employee_count if employee_count else 0
        totals["average_overtime"] = totals["total_overtime"] / employee_count if employee_count else 0
        totals["average_sick_days"] = totals["total_sick_days"] / employee_count if employee_count else 0
        totals["average_usa_days"] = totals["total_usa_days"] / employee_count if employee_count else 0
        totals["total_absence_days"] = totals["total_sick_days"] + totals["total_usa_days"]

        totals["progress_rate"] = (
            (totals["total_hours"] / totals["total_proportional_target"]) * 100
            if totals["total_proportional_target"]
            else 0
        )
        totals["target_coverage"] = (
            (totals["total_hours"] / totals["total_target_hours"]) * 100
            if totals["total_target_hours"]
            else 0
        )
        totals["remaining_ratio"] = (
            (totals["total_remaining_hours"] / totals["total_target_hours"]) * 100
            if totals["total_target_hours"]
            else 0
        )

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

        overtime_hotspots = sorted(
            [row for row in report_rows if row["overtime_hours"] > 0],
            key=lambda entry: entry["overtime_hours"],
            reverse=True,
        )[:3]

        remaining_focus = sorted(
            [row for row in report_rows if row["remaining_hours"] > 0],
            key=lambda entry: entry["remaining_hours"],
            reverse=True,
        )[:3]

        absence_hotspots = sorted(
            [
                row
                for row in report_rows
                if (row["sick_days"] or row["usa_days"])
            ],
            key=lambda entry: entry["sick_days"] + entry["usa_days"],
            reverse=True,
        )[:3]

        overtime_employee_count = sum(1 for row in report_rows if row["overtime_hours"] > 0)
        remaining_employee_count = sum(1 for row in report_rows if row["remaining_hours"] > 0)
        absence_employee_count = sum(1 for row in report_rows if (row["sick_days"] or row["usa_days"]))

        month_label = f"{calendar.month_name[month]} {year}"
        prev_month_date = start_date - timedelta(days=1)
        next_month_date = end_date + timedelta(days=1)

        prev_params = {"month": prev_month_date.month, "year": prev_month_date.year}
        next_params = {"month": next_month_date.month, "year": next_month_date.year}
        if selected_department_id:
            prev_params["department_id"] = selected_department_id
            next_params["department_id"] = selected_department_id
        if search_term:
            prev_params["search"] = search_term
            next_params["search"] = search_term
        if has_position_filter:
            prev_params["positions_filter"] = "1"
            next_params["positions_filter"] = "1"
            position_params: List[str] = list(selected_named_positions)
            if include_unassigned_selected:
                position_params.append("__NONE__")
            if position_params:
                prev_params["position"] = position_params
                next_params["position"] = position_params

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
            base_employee_count=base_employee_count,
            position_filter_options=position_filter_options,
            selected_positions=selected_position_values,
            applied_position_filters=applied_position_filters,
            positions_filter_active=has_position_filter,
            search_term=search_term,
            report_rows=report_rows,
            totals=totals,
            employee_count=employee_count,
            department_overview=department_overview,
            overtime_hotspots=overtime_hotspots,
            remaining_focus=remaining_focus,
            absence_hotspots=absence_hotspots,
            overtime_employee_count=overtime_employee_count,
            remaining_employee_count=remaining_employee_count,
            absence_employee_count=absence_employee_count,
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
        today = date.today()
        if not month or not year:
            month = today.month
            year = today.year

        if today.year == year and today.month == month:
            reference_date = today
        else:
            reference_date = date(year, month, 1)

        week_start_param = request.args.get("week_start")
        parsed_week_start: date | None = None
        if week_start_param:
            try:
                parsed_week_start = datetime.strptime(week_start_param, "%Y-%m-%d").date()
            except ValueError:
                parsed_week_start = None

        if parsed_week_start:
            week_start = parsed_week_start - timedelta(days=parsed_week_start.weekday())
        else:
            week_start = reference_date - timedelta(days=reference_date.weekday())
        week_end = week_start + timedelta(days=6)
        week_days = [week_start + timedelta(days=offset) for offset in range(7)]
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
        schedule_start = min(month_days[0], week_start)
        schedule_end = max(month_days[-1], week_end)

        month_first_day = date(year, month, 1)
        month_last_day = date(year, month, calendar.monthrange(year, month)[1])
        prev_week_start = week_start - timedelta(days=7)
        prev_week_end = prev_week_start + timedelta(days=6)
        next_week_start = week_start + timedelta(days=7)

        has_prev_week = prev_week_end >= month_first_day
        has_next_week = next_week_start <= month_last_day

        base_week_params = {"month": month, "year": year}
        if department_id:
            base_week_params["department"] = department_id

        prev_week_url = (
            url_for("schedule", **{**base_week_params, "week_start": prev_week_start.isoformat()})
            if has_prev_week
            else None
        )
        next_week_url = (
            url_for("schedule", **{**base_week_params, "week_start": next_week_start.isoformat()})
            if has_next_week
            else None
        )
        
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
            Shift.date.between(schedule_start, schedule_end)
        ).all()
        shifts = {(s.employee_id, s.date): s for s in shifts_query}
        leaves_query = Leave.query.filter(
            and_(
                Leave.start_date <= schedule_end,
                Leave.end_date >= schedule_start,
                Leave.approved == True
            )
        ).all()
        leaves: Dict[Tuple[int, date], Leave] = {}
        for leave in leaves_query:
            current_date = max(leave.start_date, schedule_start)
            last_relevant_day = min(leave.end_date, schedule_end)
            while current_date <= last_relevant_day:
                leaves[(leave.employee_id, current_date)] = leave
                current_date += timedelta(days=1)
        blocked_days_query = BlockedDay.query.filter(
            BlockedDay.date.between(schedule_start, schedule_end)
        ).all()
        blocked_days = {bd.date: bd for bd in blocked_days_query}
        blocked_dates = set(blocked_days.keys())

        employee_totals = {
            emp.id: sum(
                s.hours
                for (eid, day), s in shifts.items()
                if (
                    eid == emp.id
                    and s.approved
                    and day.month == month
                    and day.year == year
                    and day not in blocked_dates
                )
            )
            for emp in employees
        }
        week_employee_totals = {
            emp.id: sum(
                s.hours
                for (eid, day), s in shifts.items()
                if (
                    eid == emp.id
                    and s.approved
                    and week_start <= day <= week_end
                    and day not in blocked_dates
                )
            )
            for emp in employees
        }
        total_week_hours = sum(week_employee_totals.values())
        employees_with_shifts = sum(1 for hours in week_employee_totals.values() if hours > 0)
        departments = Department.query.order_by(Department.name).all()
        current_user = Employee.query.get(session.get("user_id"))
        active_schedule_view = "month"
        if current_user and current_user.preferred_schedule_view in {"month", "week"}:
            active_schedule_view = current_user.preferred_schedule_view

        productivity_data, productivity_data_totals = get_productivity_data(year, month, department_id)
        week_productivity_data = calculate_productivity_for_dates(week_days, department_id)

        # Gesperrte Tage stehen bereits als Dictionary zur Verfügung

        week_assignments: Dict[str, List[Dict[str, object]]] = {}
        for day in week_days:
            if day in blocked_dates:
                week_assignments[day.isoformat()] = []
                continue

            assignments: List[Dict[str, object]] = []
            for emp in employees:
                shift = shifts.get((emp.id, day))
                if not shift:
                    continue

                leave = leaves.get((emp.id, day))
                if leave and leave.leave_type in LEAVE_TYPES_EXCLUDED_FROM_PRODUCTIVITY:
                    continue

                assignments.append(
                    {
                        "employeeId": emp.id,
                        "employeeName": emp.name,
                        "hours": float(shift.hours or 0),
                        "approved": bool(shift.approved),
                        "shiftType": shift.shift_type or "",
                        "position": emp.position or "",
                    }
                )

            assignments.sort(key=lambda entry: entry["employeeName"].lower())
            week_assignments[day.isoformat()] = assignments

        return render_template(
            "schedule.html",
            month=month,
            year=year,
            month_days=month_days,
            week_days=week_days,
            week_start=week_start,
            week_end=week_end,
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
            week_employee_totals=week_employee_totals,
            total_week_hours=total_week_hours,
            employees_with_shifts=employees_with_shifts,
            departments=departments,
            selected_department=department_id,
            calendar=calendar,
            current_user=current_user,
            today=today,
            productivity_data=productivity_data,
            productivity_data_totals=productivity_data_totals,
            week_productivity_data=week_productivity_data,
            active_schedule_view=active_schedule_view,
            week_assignments=week_assignments,
            week_start_iso=week_start.isoformat(),
            prev_week_url=prev_week_url,
            next_week_url=next_week_url,
            has_prev_week=has_prev_week,
            has_next_week=has_next_week,
        )

    @app.route("/dienstplan/ansicht", methods=["POST"])
    @login_required
    def update_schedule_view() -> Response:
        """Speichert die zuletzt verwendete Dienstplan-Ansicht im Benutzerkonto."""

        payload = request.get_json(silent=True) or {}
        requested_view = payload.get("view")

        if requested_view not in {"month", "week"}:
            return jsonify({"success": False, "message": "Ungültige Ansicht."}), 400

        current_user = get_current_user()
        if not current_user:
            return jsonify({"success": False, "message": "Benutzer nicht angemeldet."}), 401

        current_user.preferred_schedule_view = requested_view
        db.session.commit()

        return jsonify({"success": True, "view": requested_view})

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

        employee = Employee.query.get_or_404(emp_id)
        shift_date = datetime.strptime(date_str, "%Y-%m-%d").date()

        # Prüfen, ob das Datum ein gesperrter Tag ist
        if BlockedDay.query.filter_by(date=shift_date).first():
            flash(f"An diesem Tag ({shift_date.strftime("%d.%m.%Y")}) können keine Schichten hinzugefügt werden, da er gesperrt ist.", "danger")
            return redirect(url_for("schedule", month=shift_date.month, year=shift_date.year))
        new_shift = Shift(
            employee_id=employee.id,
            date=shift_date,
            hours=hours,
            shift_type=shift_type,
            approved=session.get("is_admin", False),
        )
        db.session.add(new_shift)

        if not new_shift.approved:
            message = _build_shift_request_message(employee, shift_date)
            notify_admins_of_request(
                employee,
                message,
                url_for("shift_requests_overview"),
            )

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
        request_message = _build_shift_request_message(shift.employee, shift.date)
        request_link = url_for("shift_requests_overview")
        _clear_request_notifications(request_message, request_link)
        message = f"Dein Einsatz am {shift.date.strftime('%d.%m.%Y')} wurde genehmigt."
        notify_employee(
            shift.employee_id,
            message,
            url_for("schedule", month=shift.date.month, year=shift.date.year),
        )
        db.session.commit()
        flash("Einsatz wurde genehmigt.", "success")
        return redirect(url_for("schedule", month=shift.date.month, year=shift.date.year))

    @app.route("/einsatz/ablehnen/<int:shift_id>")
    @admin_required
    def decline_shift(shift_id: int) -> str:
        """Lehnt einen Einsatz ab (löscht ihn)."""
        shift = Shift.query.get_or_404(shift_id)
        request_message = _build_shift_request_message(shift.employee, shift.date)
        request_link = url_for("shift_requests_overview")
        _clear_request_notifications(request_message, request_link)
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
        month_names = [
            "Januar", "Februar", "März", "April", "Mai", "Juni",
            "Juli", "August", "September", "Oktober", "November", "Dezember"
        ]

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
                'label': f"{month_names[month - 1]} {year}",
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

        total_weekday_hours = sum(weekday_hours.values())
        weekday_labels = [
            "Montag", "Dienstag", "Mittwoch", "Donnerstag",
            "Freitag", "Samstag", "Sonntag"
        ]
        weekday_breakdown = []
        for index, label in enumerate(weekday_labels):
            value = weekday_hours.get(index, 0)
            percentage = (value / total_weekday_hours * 100) if total_weekday_hours else 0
            weekday_breakdown.append({
                'label': label,
                'hours': value,
                'percentage': percentage
            })
        top_weekday = max(weekday_breakdown, key=lambda item: item['hours']) if total_weekday_hours else None
        calm_weekday = min(
            (item for item in weekday_breakdown if item['hours'] > 0),
            key=lambda item: item['hours'],
            default=None
        )

        total_shift_type_hours = sum(shift_type_hours.values())
        shift_breakdown = []
        for shift_type, value in sorted(shift_type_hours.items(), key=lambda item: item[1], reverse=True):
            percentage = (value / total_shift_type_hours * 100) if total_shift_type_hours else 0
            shift_breakdown.append({
                'type': shift_type,
                'hours': value,
                'percentage': percentage
            })

        recent_months = monthly_data[-4:] if monthly_data else []
        monthly_trend = None
        if len(monthly_data) >= 2:
            last_month = monthly_data[-1]
            previous_month = monthly_data[-2]
            difference = last_month['worked_hours'] - previous_month['worked_hours']
            monthly_trend = {
                'current': last_month,
                'previous': previous_month,
                'difference': difference,
                'direction': 'up' if difference >= 0 else 'down'
            }

        completion_percentage = hours_summary.get('completion_percentage', 0)
        progress_percentage = max(0, min(completion_percentage, 100))

        remaining_hours = hours_summary.get('remaining_hours', 0)
        proportional_target = hours_summary.get('proportional_target', 0)
        worked_hours = hours_summary.get('worked_hours', 0)
        overtime_hours = hours_summary.get('overtime_hours', 0)
        shift_count = hours_summary.get('shift_count', 0)

        recommendations = []
        if remaining_hours > 0:
            recommendations.append(
                f"Du liegst {remaining_hours:.1f} Stunden unter dem Monatsziel. Plane zusätzliche Einsätze oder prüfe offene Schichten."
            )
        else:
            recommendations.append(
                "Du hast dein Monatsziel erreicht – nutze die Zeit für Ausgleich oder Weiterbildung."
            )

        if proportional_target and worked_hours < proportional_target:
            deficit = proportional_target - worked_hours
            recommendations.append(
                f"Bis heute fehlen {deficit:.1f} Stunden zu den anteiligen Soll-Stunden. Kleine zusätzliche Einsätze gleichen das aus."
            )
        elif proportional_target:
            surplus = worked_hours - proportional_target
            recommendations.append(
                f"Du liegst {surplus:.1f} Stunden vor dem anteiligen Soll – behalte deine Erholung im Blick."
            )

        if overtime_hours > 0:
            recommendations.append(
                f"Aktuell stehen {overtime_hours:.1f} Überstunden an. Prüfe Möglichkeiten zum Ausgleich oder zur Freigabe."
            )
        elif shift_count == 0:
            recommendations.append(
                "Es wurden noch keine genehmigten Schichten erfasst. Bitte reiche deine Zeiten zeitnah ein."
            )

        if len(recommendations) > 3:
            recommendations = recommendations[:3]

        return render_template(
            "employee_hours_overview.html",
            employee=employee,
            hours_summary=hours_summary,
            monthly_data=monthly_data,
            recent_months=recent_months,
            monthly_trend=monthly_trend,
            weekday_hours=weekday_hours,
            weekday_breakdown=weekday_breakdown,
            top_weekday=top_weekday,
            calm_weekday=calm_weekday,
            shift_type_hours=shift_type_hours,
            shift_breakdown=shift_breakdown,
            recommendations=recommendations,
            progress_percentage=progress_percentage,
            completion_percentage=completion_percentage,
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
            employee = Employee.query.get_or_404(emp_id)
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

            if not is_approved:
                message = _build_leave_request_message(
                    employee,
                    leave_type,
                    start_date,
                    end_date,
                )
                notify_admins_of_request(
                    employee,
                    message,
                    url_for("leave_requests"),
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
        request_message = _build_leave_request_message(
            leave.employee,
            leave.leave_type,
            leave.start_date,
            leave.end_date,
        )
        request_link = url_for("leave_requests")
        _clear_request_notifications(request_message, request_link)
        if leave.start_date == leave.end_date:
            date_range = leave.start_date.strftime('%d.%m.%Y')
        else:
            date_range = (
                f"{leave.start_date.strftime('%d.%m.%Y')} bis {leave.end_date.strftime('%d.%m.%Y')}"
            )
        message = f"Dein {leave.leave_type}-Antrag für {date_range} wurde genehmigt."
        notify_employee(
            leave.employee_id,
            message,
            url_for("leave_form"),
        )
        db.session.commit()
        flash("Antrag genehmigt.", "success")
        return redirect(url_for("leave_requests"))

    @app.route("/abwesenheit/ablehnen/<int:leave_id>")
    @admin_required
    def decline_leave(leave_id: int) -> str:
        """Lehnt einen Abwesenheitsantrag ab (löscht ihn)."""
        leave = Leave.query.get_or_404(leave_id)
        request_message = _build_leave_request_message(
            leave.employee,
            leave.leave_type,
            leave.start_date,
            leave.end_date,
        )
        request_link = url_for("leave_requests")
        _clear_request_notifications(request_message, request_link)
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

    @app.route("/settings")
    @super_admin_required
    def system_settings() -> str:
        """Übersichtsseite für künftige globale Einstellungen."""

        quick_actions = [
            {
                "id": "sync-policies",
                "icon": "🔄",
                "title": "Richtlinien synchronisieren",
                "description": "Aktualisiert Berechtigungen systemweit in wenigen Sekunden.",
            },
            {
                "id": "refresh-cache",
                "icon": "🧹",
                "title": "Systemcache bereinigen",
                "description": "Löscht temporäre Daten und startet Hintergrunddienste sanft neu.",
            },
            {
                "id": "export-audit",
                "icon": "📄",
                "title": "Änderungsprotokoll exportieren",
                "description": "Bereitet einen vollständigen Audit-Report für das Compliance-Team vor.",
            },
        ]

        focus_areas = [
            {
                "icon": "🛡️",
                "title": "Sicherheitsrichtlinien",
                "description": "Zugriffs- und Rollenmodelle verwalten sowie Mehrfaktorauthentifizierung steuern.",
                "badge": "Stabil",
            },
            {
                "icon": "🔔",
                "title": "Benachrichtigungen",
                "description": "Globale Eskalationspfade und Zustelloptionen für kritische Hinweise konfigurieren.",
                "badge": "Beta",
            },
            {
                "icon": "📦",
                "title": "Integrationen",
                "description": "Schnittstellen zu HR- und Zeiterfassungssystemen verwalten und testen.",
                "badge": "In Planung",
            },
        ]

        maintenance_notes = [
            {
                "icon": "🗄️",
                "title": "Datenbank-Optimierung",
                "window": "Jeden Sonntag · 02:00 – 03:00 Uhr",
                "impact": "Kurzzeitige Leseunterbrechungen möglich",
            },
            {
                "icon": "☁️",
                "title": "Cloud-Sicherung",
                "window": "Täglich · 01:30 Uhr",
                "impact": "Automatische Sicherung aller Kernmodule",
            },
            {
                "icon": "🧪",
                "title": "Funktions-Sandbox",
                "window": "Mittwochs · 21:00 – 22:00 Uhr",
                "impact": "Neue Features werden ohne Produktivdaten getestet",
            },
        ]

        roadmap = [
            {
                "icon": "🧭",
                "title": "Self-Service Portale",
                "description": "Ermöglicht Mitarbeitenden eigene Einstellungen wie Sprache und Benachrichtigungen.",
                "quarter": "Q3 2024",
            },
            {
                "icon": "🤖",
                "title": "Automatisierte Freigaben",
                "description": "Genehmigungsprozesse für wiederkehrende Abläufe beschleunigen.",
                "quarter": "Q4 2024",
            },
            {
                "icon": "📊",
                "title": "Erweiterte Auswertungen",
                "description": "Konsolidierte Reports für Vorstand und Betriebsrat vorbereiten.",
                "quarter": "Q1 2025",
            },
        ]

        audit_notes = [
            "Tägliche Sicherung der Audit-Logs im revisionssicheren Speicher.",
            "Export als CSV und PDF vorbereitet, Freigabe in Kürze verfügbar.",
            "Benachrichtigungen bei ungewöhnlichen Anmeldeversuchen werden ausgebaut.",
        ]

        stats = [
            {"label": "Aktive Module", "value": len(focus_areas)},
            {"label": "Geplante Erweiterungen", "value": len(roadmap)},
            {"label": "Automatisierungen", "value": len(quick_actions)},
        ]

        last_updated = datetime.now()

        return render_template(
            "settings.html",
            quick_actions=quick_actions,
            focus_areas=focus_areas,
            maintenance_notes=maintenance_notes,
            roadmap=roadmap,
            audit_notes=audit_notes,
            stats=stats,
            last_updated=last_updated,
        )

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

