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
import csv
import secrets
import threading
import time as time_module
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from io import StringIO
from pathlib import Path
from typing import Dict, List, Tuple
import pandas as pd
from sqlalchemy.exc import IntegrityError
from sqlalchemy import or_, and_, func, case
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
    current_app,
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
    WorkClass,
    BlockedDay,
    Notification,
    ApprovalAutomation,
)
from auto_schedule import create_default_shifts_for_month, create_default_shifts_for_employee_position

LEAVE_TYPES_EXCLUDED_FROM_PRODUCTIVITY = {"Urlaub", "Krank"}

DEFAULT_GROUP_ICONS = {
    "Vollzeit": "👔",
    "Teilzeit": "⏰",
    "Aushilfe": "🤝",
}

UNASSIGNED_WORK_CLASS_LABEL = "Ohne Arbeitsklasse"

_COLOR_PALETTE = [
    "#2563eb",
    "#0ea5e9",
    "#10b981",
    "#f97316",
    "#8b5cf6",
    "#f43f5e",
    "#22c55e",
    "#14b8a6",
    "#facc15",
    "#6366f1",
]


def _format_file_size(num_bytes: int | None) -> str:
    """Wandelt eine Dateigröße in ein gut lesbares Format um."""

    if num_bytes is None or num_bytes < 0:
        return "–"

    thresholds = [
        (1024 ** 4, "TB"),
        (1024 ** 3, "GB"),
        (1024 ** 2, "MB"),
        (1024, "KB"),
    ]

    for threshold, suffix in thresholds:
        if num_bytes >= threshold:
            value = num_bytes / threshold
            return f"{value:.1f} {suffix}"

    return f"{num_bytes} B"


def _create_default_admin_account() -> None:
    """Stellt sicher, dass ein Standard-Administrator existiert."""

    existing_admin = Employee.query.filter_by(username="admin").first()
    if existing_admin:
        return

    department = Department.query.order_by(Department.id.asc()).first()
    if not department:
        department = Department(
            name="Administration",
            color="#2563eb",
            area="Verwaltung",
        )
        db.session.add(department)
        db.session.flush()

    admin_user = Employee(
        name="Administrator",
        username="admin",
        is_admin=True,
        department_id=department.id,
        monthly_hours=160,
    )
    admin_user.set_password("admin")
    db.session.add(admin_user)
    db.session.commit()


def _normalize_hex_color(value: str | None) -> str | None:
    """Normalisiert einen Hex-Farbwert in die Form #rrggbb."""

    if not value:
        return None

    color = value.strip()
    if not color:
        return None

    if color.startswith("#"):
        color = color[1:]

    if len(color) == 3:
        color = "".join(component * 2 for component in color)

    if len(color) != 6:
        return None

    try:
        int(color, 16)
    except ValueError:
        return None

    return f"#{color.lower()}"


def _hex_to_rgb(color: str | None) -> Tuple[int, int, int] | None:
    """Wandelt einen Hex-Farbwert in RGB um."""

    normalized = _normalize_hex_color(color)
    if not normalized:
        return None

    raw = normalized.lstrip("#")
    try:
        return tuple(int(raw[index : index + 2], 16) for index in (0, 2, 4))
    except ValueError:
        return None


def _lighten_hex(color: str, factor: float) -> str:
    """Erzeugt eine hellere Variante des gegebenen Farbtons."""

    rgb = _hex_to_rgb(color)
    if not rgb:
        return "#f1f5f9"

    factor = max(0.0, min(1.0, factor))

    def _lighten_component(component: int) -> int:
        return int(component + (255 - component) * factor)

    r, g, b = rgb
    return f"#{_lighten_component(r):02x}{_lighten_component(g):02x}{_lighten_component(b):02x}"


def _get_contrast_text_color(color: str) -> str:
    """Ermittelt eine gut lesbare Textfarbe für den angegebenen Hintergrund."""

    rgb = _hex_to_rgb(color)
    if not rgb:
        return "#111827"

    r, g, b = (component / 255 for component in rgb)
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#111827" if luminance > 0.6 else "#ffffff"


def _color_from_name(name: str | None) -> str:
    """Wählt einen konsistenten Farbwert anhand eines Namens."""

    if not name:
        return "#64748b"

    index = abs(hash(name)) % len(_COLOR_PALETTE)
    return _COLOR_PALETTE[index]


def _build_group_meta(name: str, base_color: str | None = None) -> Dict[str, str]:
    """Erstellt Farbinformationen und Symbole für Dienstplan-Gruppen."""

    normalized = _normalize_hex_color(base_color) or _normalize_hex_color(_color_from_name(name))
    if not normalized:
        normalized = "#2563eb"

    header_color = normalized
    row_bg_color = _lighten_hex(header_color, 0.88)
    total_bg_color = _lighten_hex(header_color, 0.75)
    border_color = _lighten_hex(header_color, 0.6)

    icon = DEFAULT_GROUP_ICONS.get(name, "🏷️")
    if name == UNASSIGNED_WORK_CLASS_LABEL:
        icon = "🗂️"

    return {
        "icon": icon,
        "header_color": header_color,
        "header_text_color": _get_contrast_text_color(header_color),
        "row_bg_color": row_bg_color,
        "total_bg_color": total_bg_color,
        "accent_color": header_color,
        "border_color": border_color,
    }


def _get_available_group_names(include_unassigned: bool = True) -> List[str]:
    """Bestimmt alle bekannten Positions- bzw. Arbeitsklassen-Gruppen."""

    work_classes = (
        WorkClass.query.order_by(WorkClass.is_default.desc(), WorkClass.name.asc()).all()
    )
    names: List[str] = []
    seen: set[str] = set()

    for work_class in work_classes:
        if work_class.name and work_class.name not in seen:
            names.append(work_class.name)
            seen.add(work_class.name)

    existing_positions = [
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

    for value in existing_positions:
        if value and value not in seen:
            names.append(value)
            seen.add(value)

    if include_unassigned:
        has_unassigned = (
            db.session.query(Employee.id)
            .filter(or_(Employee.position.is_(None), Employee.position == ""))
            .first()
            is not None
        )
        if has_unassigned and UNASSIGNED_WORK_CLASS_LABEL not in seen:
            names.append(UNASSIGNED_WORK_CLASS_LABEL)

    return names

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


def _parse_days_of_week(days: str | None) -> list[int]:
    """Wandelt eine kommaseparierte Liste von Wochentagen in Integer um."""

    if not days:
        return []

    parsed: set[int] = set()
    for entry in days.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            day = int(entry)
        except ValueError:
            continue
        if 0 <= day <= 6:
            parsed.add(day)
    return sorted(parsed)


def _calculate_next_run(
    schedule_type: str,
    run_time_value,
    days: str | None,
    *,
    reference: datetime | None = None,
) -> datetime | None:
    """Berechnet den nächsten Ausführungstermin für eine Automatisierung."""

    if schedule_type == "once":
        return None

    if reference is None:
        reference = datetime.now()

    if not run_time_value:
        return None

    run_time = run_time_value

    if schedule_type == "daily":
        candidate = datetime.combine(reference.date(), run_time)
        if candidate <= reference:
            candidate += timedelta(days=1)
        return candidate

    if schedule_type == "weekly":
        allowed_days = _parse_days_of_week(days)
        if not allowed_days:
            allowed_days = list(range(7))

        for delta in range(0, 8):
            candidate_date = reference.date() + timedelta(days=delta)
            candidate_dt = datetime.combine(candidate_date, run_time)
            if candidate_dt <= reference:
                continue
            if candidate_date.weekday() in allowed_days:
                return candidate_dt

        # Fallback: nächsten passenden Wochentag bestimmen.
        weekday = reference.weekday()
        offsets = [((day - weekday) % 7) or 7 for day in allowed_days]
        candidate_date = reference.date() + timedelta(days=min(offsets))
        return datetime.combine(candidate_date, run_time)

    return None


def _forecast_automation_runs(
    automation: ApprovalAutomation,
    *,
    limit: int = 3,
) -> List[datetime]:
    """Ermittelt die nächsten geplanten Ausführungszeitpunkte einer Automation."""

    occurrences: List[datetime] = []

    if not automation.next_run:
        return occurrences

    current = automation.next_run
    occurrences.append(current)

    for _ in range(1, max(1, limit)):
        next_occurrence = _calculate_next_run(
            automation.schedule_type,
            automation.run_time,
            automation.days_of_week,
            reference=current + timedelta(seconds=1),
        )
        if not next_occurrence:
            break
        occurrences.append(next_occurrence)
        current = next_occurrence

    return occurrences[:limit]


def _execute_automation(automation: ApprovalAutomation) -> str:
    """Führt eine Automatisierung aus und gibt eine Zusammenfassung zurück."""

    approved_shifts = 0
    approved_leaves = 0
    created_schedule_shifts = 0
    skipped_schedule_shifts = 0
    schedule_month_label: str | None = None

    shift_link = url_for("shift_requests_overview")
    leave_link = url_for("leave_requests")

    if automation.automation_type in {"approve_shifts", "approve_all"}:
        pending_shifts = Shift.query.filter_by(approved=False).all()
        for shift in pending_shifts:
            shift.approved = True
            request_message = _build_shift_request_message(shift.employee, shift.date)
            _clear_request_notifications(request_message, shift_link)
            notify_employee(
                shift.employee_id,
                f"Dein Einsatz am {shift.date.strftime('%d.%m.%Y')} wurde automatisch genehmigt.",
                url_for("schedule", month=shift.date.month, year=shift.date.year),
            )
            approved_shifts += 1

    if automation.automation_type in {"approve_leaves", "approve_all"}:
        pending_leaves = Leave.query.filter_by(approved=False).all()
        for leave in pending_leaves:
            leave.approved = True
            request_message = _build_leave_request_message(
                leave.employee,
                leave.leave_type,
                leave.start_date,
                leave.end_date,
            )
            _clear_request_notifications(request_message, leave_link)
            if leave.start_date == leave.end_date:
                date_range = leave.start_date.strftime('%d.%m.%Y')
            else:
                date_range = (
                    f"{leave.start_date.strftime('%d.%m.%Y')} bis {leave.end_date.strftime('%d.%m.%Y')}"
                )
            notify_employee(
                leave.employee_id,
                f"Dein {leave.leave_type}-Antrag für {date_range} wurde automatisch genehmigt.",
                url_for("leave_form"),
            )
            approved_leaves += 1

    if automation.automation_type == "auto_schedule_position":
        if not automation.target_position:
            return "Keine Zielgruppe für die Auto-Schicht-Automatisierung hinterlegt."

        reference_dt = automation.next_run or datetime.now()
        target_year = reference_dt.year
        target_month = reference_dt.month

        result = create_default_shifts_for_employee_position(
            automation.target_position,
            target_year,
            target_month,
        )

        created_schedule_shifts = result.get("total_created", 0)
        skipped_schedule_shifts = result.get("total_skipped", 0)
        schedule_month_label = datetime(target_year, target_month, 1).strftime("%m.%Y")

    summary_parts = []
    if approved_shifts:
        summary_parts.append(f"{approved_shifts} Einsätze freigegeben")
    if approved_leaves:
        summary_parts.append(f"{approved_leaves} Abwesenheiten genehmigt")

    if schedule_month_label is not None:
        schedule_part = f"{created_schedule_shifts} Schichten erstellt"
        if skipped_schedule_shifts:
            schedule_part += f", {skipped_schedule_shifts} übersprungen"
        if not created_schedule_shifts and not skipped_schedule_shifts:
            schedule_part = "Keine Schichten erstellt"
        schedule_part += f" ({automation.target_position}, Monat {schedule_month_label})"
        summary_parts.append(schedule_part)

    if summary_parts:
        summary_text = " · ".join(summary_parts)
        admin_message = f"Automation '{automation.name}' hat {summary_text}."
        admins = Employee.query.filter(Employee.is_admin.is_(True)).all()
        for admin in admins:
            _create_notification(admin.id, admin_message, url_for("system_settings"))
    else:
        summary_text = "Keine offenen Vorgänge gefunden."

    return summary_text


def _run_and_schedule_automation(automation: ApprovalAutomation) -> str:
    """Hilfsfunktion, die eine Automation ausführt und den nächsten Termin setzt."""

    summary = _execute_automation(automation)
    now = datetime.now()
    automation.last_run = now
    automation.last_run_summary = summary

    if automation.schedule_type == "once":
        automation.is_active = False
        automation.next_run = None
    else:
        automation.next_run = _calculate_next_run(
            automation.schedule_type,
            automation.run_time,
            automation.days_of_week,
            reference=now + timedelta(seconds=1),
        )

    automation.updated_at = datetime.now()
    db.session.commit()
    return summary


def _process_due_automations() -> None:
    """Prüft fällige Automatisierungen und führt sie aus."""

    now = datetime.now()
    due_automations = ApprovalAutomation.query.filter(
        ApprovalAutomation.is_active.is_(True),
        ApprovalAutomation.next_run.isnot(None),
        ApprovalAutomation.next_run <= now,
    ).all()

    for automation in due_automations:
        try:
            _run_and_schedule_automation(automation)
        except Exception as exc:  # pragma: no cover - Schutz vor unerwarteten Fehlern
            db.session.rollback()
            print(f"Fehler bei Automatisierung '{automation.name}': {exc}")


def _start_automation_worker(app: Flask) -> None:
    """Startet einen Hintergrund-Thread zur Verarbeitung der Automatisierungen."""

    if getattr(app, "_automation_worker_started", False):
        return

    def _worker() -> None:
        while True:
            with app.app_context():
                _process_due_automations()
            time_module.sleep(60)

    thread = threading.Thread(target=_worker, name="automation-runner", daemon=True)
    thread.start()
    app._automation_worker_started = True


AUTOMATION_TYPE_CHOICES = [
    ("approve_shifts", "Einsätze automatisch freigeben"),
    ("approve_leaves", "Abwesenheiten automatisch genehmigen"),
    ("approve_all", "Einsätze & Abwesenheiten gemeinsam freigeben"),
    ("auto_schedule_position", "Schichten für Mitarbeitergruppe automatisch planen"),
]

SCHEDULE_CHOICES = [
    ("daily", "Täglich zur angegebenen Uhrzeit"),
    ("weekly", "Wöchentlich an ausgewählten Tagen"),
    ("once", "Einmalig zum festgelegten Zeitpunkt"),
]

WEEKDAY_LABELS = [
    ("0", "Montag"),
    ("1", "Dienstag"),
    ("2", "Mittwoch"),
    ("3", "Donnerstag"),
    ("4", "Freitag"),
    ("5", "Samstag"),
    ("6", "Sonntag"),
]

def create_app() -> Flask:

    """Erzeugt und konfiguriert die Flask‑Anwendung."""
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///planner.db"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SECRET_KEY"] = secrets.token_hex(32)

    init_db(app)

    @app.template_filter("round_half_up")
    def round_half_up_filter(value, digits: int = 0):
        """Rundet numerische Werte im kaufmännischen Sinn (0.5 -> 1)."""

        if value is None:
            return ""

        try:
            digits = int(digits)
        except (TypeError, ValueError):
            digits = 0

        try:
            decimal_value = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return value

        exponent = Decimal("1").scaleb(-digits)
        try:
            rounded = decimal_value.quantize(exponent, rounding=ROUND_HALF_UP)
        except InvalidOperation:
            return value

        if digits <= 0:
            try:
                return int(rounded)
            except (ValueError, OverflowError):
                return float(rounded)

        return format(rounded, f".{digits}f")

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
        raw_position_filter = (request.args.get("position") or "").strip()

        work_classes = (
            WorkClass.query.order_by(WorkClass.is_default.desc(), WorkClass.name.asc()).all()
        )
        active_work_classes = [wc for wc in work_classes if wc.is_active]

        position_options: List[str] = []
        seen_positions: set[str] = set()

        for work_class in active_work_classes:
            if work_class.name not in seen_positions:
                position_options.append(work_class.name)
                seen_positions.add(work_class.name)

        existing_positions = [
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
        for value in existing_positions:
            if value and value not in seen_positions:
                position_options.append(value)
                seen_positions.add(value)

        has_unassigned_employees = (
            db.session.query(Employee.id)
            .filter(or_(Employee.position.is_(None), Employee.position == ""))
            .first()
            is not None
        )

        position_filter = raw_position_filter
        valid_positions = set(position_options)
        include_unassigned = False
        if raw_position_filter == "__UNASSIGNED__":
            include_unassigned = True
        elif raw_position_filter and raw_position_filter not in valid_positions:
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

        if include_unassigned:
            employee_query = employee_query.filter(
                or_(Employee.position.is_(None), Employee.position == "")
            )
        elif position_filter:
            employee_query = employee_query.filter(Employee.position == position_filter)

        employees = employee_query.order_by(Employee.name).all()

        # Berechne Reststunden für alle Mitarbeiter (abteilungsbasiert)
        user_dept_id = current_user.department_id if current_user else None

        hours_summary = get_all_employees_hours_summary(year, month, user_dept_id)

        # Generiere Planungshilfen (abteilungsbasiert)
        planning_insights = get_planning_insights(year, month, user_dept_id)

        position_counter = Counter(emp.position for emp in employees if emp.position)
        hero_position_summary: List[Dict[str, object]] = []
        summary_names: set[str] = set()
        for work_class in active_work_classes:
            hero_position_summary.append(
                {
                    "name": work_class.name,
                    "count": position_counter.get(work_class.name, 0),
                    "color": _normalize_hex_color(work_class.color)
                    or _color_from_name(work_class.name),
                }
            )
            summary_names.add(work_class.name)

        for value in existing_positions:
            if value not in summary_names:
                hero_position_summary.append(
                    {
                        "name": value,
                        "count": position_counter.get(value, 0),
                        "color": _color_from_name(value),
                    }
                )
                summary_names.add(value)

        unassigned_count = sum(1 for emp in employees if not (emp.position and emp.position.strip()))
        if unassigned_count:
            hero_position_summary.append(
                {
                    "name": UNASSIGNED_WORK_CLASS_LABEL,
                    "count": unassigned_count,
                    "color": _color_from_name(UNASSIGNED_WORK_CLASS_LABEL),
                }
            )

        non_zero_entries = [entry for entry in hero_position_summary if entry["count"] > 0]
        if non_zero_entries:
            top_position_summary = sorted(
                non_zero_entries, key=lambda item: item["count"], reverse=True
            )[:3]
        else:
            top_position_summary = hero_position_summary[:3]

        position_filter_options = [
            {"value": option, "label": option} for option in position_options
        ]
        if has_unassigned_employees:
            position_filter_options.append(
                {"value": "__UNASSIGNED__", "label": UNASSIGNED_WORK_CLASS_LABEL}
            )

        selected_position_value = "__UNASSIGNED__" if include_unassigned else position_filter

        return render_template(
            "employees.html",
            employees=employees,
            departments=departments,
            hours_summary=hours_summary,
            planning_insights=planning_insights,
            current_month=month,
            current_year=year,
            search_query=search_query,
            selected_position=selected_position_value,
            work_classes=active_work_classes,
            position_filter_options=position_filter_options,
            hero_position_summary=top_position_summary,
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

        work_classes = (
            WorkClass.query.order_by(WorkClass.is_default.desc(), WorkClass.name.asc()).all()
        )
        active_work_classes = [wc for wc in work_classes if wc.is_active]
        valid_positions = {wc.name for wc in active_work_classes}

        if active_work_classes and not position:
            flash("Bitte wähle eine Arbeitsklasse für den Mitarbeiter aus.", "warning")
            return redirect(url_for("employees"))

        if position and position not in valid_positions:
            flash("Bitte wähle eine gültige Arbeitsklasse.", "danger")
            return redirect(url_for("employees"))

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
        all_work_classes = (
            WorkClass.query.order_by(WorkClass.is_default.desc(), WorkClass.name.asc()).all()
        )
        active_work_classes = [wc for wc in all_work_classes if wc.is_active]
        valid_positions = {wc.name for wc in active_work_classes}
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
            if position and position not in valid_positions and position != emp.position:
                flash("Bitte wähle eine gültige Arbeitsklasse.", "danger")
                return redirect(url_for("edit_employee", emp_id=emp.id))
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
        legacy_positions: List[str] = []
        if emp.position and emp.position not in valid_positions:
            legacy_positions.append(emp.position)
        return render_template(
            "employee_edit.html",
            emp=emp,
            departments=departments,
            work_classes=active_work_classes,
            legacy_positions=legacy_positions,
        )

    @app.route("/abteilungen")
    @admin_required
    def departments() -> str:
        """Liste der Abteilungen mit Formular zum Hinzufügen neuer Abteilungen."""
        # Abteilungsbasierte Einschränkung
        current_user = get_current_user()
        is_department_admin = bool(current_user and current_user.department_id)
        is_super_admin = bool(current_user and current_user.is_admin and not current_user.department_id)

        if is_department_admin:
            # Abteilungsadmin sieht nur seine eigene Abteilung
            departments = Department.query.filter_by(id=current_user.department_id).all()
        else:
            # Super-Admin ohne Abteilung sieht alle
            departments = Department.query.order_by(Department.name).all()

        total_employees = sum(len(dept.employees) for dept in departments)
        areas = sorted(
            {
                dept.area.strip()
                for dept in departments
                if dept.area and dept.area.strip()
            }
        )
        metrics = {
            "total_departments": len(departments),
            "total_employees": total_employees,
            "unique_areas": len(areas),
            "unassigned_employees": Employee.query.filter_by(department_id=None).count()
            if is_super_admin
            else None,
        }

        department_visuals = {}
        top_department = None
        for index, dept in enumerate(departments):
            base_color = _normalize_hex_color(dept.color) or _COLOR_PALETTE[index % len(_COLOR_PALETTE)]
            department_visuals[dept.id] = {
                "base": base_color,
                "surface": _lighten_hex(base_color, 0.85),
                "accent": _lighten_hex(base_color, 0.6),
                "text": _get_contrast_text_color(base_color),
            }
            if top_department is None or len(dept.employees) > len(top_department.employees):
                top_department = dept

        return render_template(
            "departments.html",
            departments=departments,
            department_visuals=department_visuals,
            metrics=metrics,
            areas=areas,
            top_department=top_department,
            is_department_admin=is_department_admin,
            is_super_admin=is_super_admin,
        )

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
        color = _normalize_hex_color(request.form.get("color"))
        area = request.form.get("area", "").strip() or None
        dept = Department(name=name, color=color, area=area)
        db.session.add(dept)
        db.session.commit()
        flash(f"Abteilung {name} wurde gespeichert.", "success")
        return redirect(url_for("departments"))

    @app.route("/abteilungen/aktualisieren/<int:dept_id>", methods=["POST"])
    @admin_required
    def update_department(dept_id: int) -> str:
        """Aktualisiert eine bestehende Abteilung."""
        current_user = get_current_user()
        dept = Department.query.get_or_404(dept_id)

        if current_user and current_user.department_id and current_user.department_id != dept_id:
            flash("Sie können nur Ihre eigene Abteilung bearbeiten.", "warning")
            return redirect(url_for("departments"))

        name = request.form.get("name", "").strip()
        if not name:
            flash("Bitte geben Sie einen Namen an.", "warning")
            return redirect(url_for("departments"))

        color = _normalize_hex_color(request.form.get("color"))
        area = request.form.get("area", "").strip() or None

        dept.name = name
        dept.color = color
        dept.area = area
        db.session.commit()
        flash(f"Abteilung {name} wurde aktualisiert.", "success")
        return redirect(url_for("departments"))

    @app.route("/abteilungen/loeschen/<int:dept_id>", methods=["POST"])
    @admin_required
    def delete_department(dept_id: int) -> str:
        """Löscht eine Abteilung."""
        current_user = get_current_user()
        if current_user and current_user.department_id:
            flash("Abteilungsadministratoren können keine Abteilungen löschen.", "warning")
            return redirect(url_for("departments"))

        dept = Department.query.get_or_404(dept_id)
        if dept.employees:
            flash(
                "Die Abteilung kann nicht gelöscht werden, solange Mitarbeiter zugeordnet sind.",
                "danger",
            )
            return redirect(url_for("departments"))

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

        all_work_classes = (
            WorkClass.query.order_by(WorkClass.is_default.desc(), WorkClass.name.asc()).all()
        )
        work_class_by_name = {wc.name: wc for wc in all_work_classes}

        employee_groups: Dict[str, List[Employee]] = {
            wc.name: [] for wc in all_work_classes
        }

        employees = all_employees
        for emp in employees:
            position_name = (emp.position or "").strip()
            if position_name:
                if position_name not in employee_groups:
                    employee_groups[position_name] = []
                employee_groups[position_name].append(emp)
            else:
                employee_groups.setdefault(UNASSIGNED_WORK_CLASS_LABEL, []).append(emp)

        employee_groups.setdefault(UNASSIGNED_WORK_CLASS_LABEL, employee_groups.get(UNASSIGNED_WORK_CLASS_LABEL, []))

        from models import EmployeeGroupOrder

        group_order_entries = EmployeeGroupOrder.query.order_by(EmployeeGroupOrder.order_position).all()
        saved_order = {entry.group_name: entry.order_position for entry in group_order_entries}
        base_order_mapping = {wc.name: index for index, wc in enumerate(all_work_classes)}

        def _group_sort_key(name: str) -> Tuple[int, int, str]:
            if name in saved_order:
                return (0, saved_order[name], name.lower())
            base_rank = base_order_mapping.get(name, len(base_order_mapping))
            if name == UNASSIGNED_WORK_CLASS_LABEL:
                base_rank += len(base_order_mapping)
            return (1, base_rank, name.lower())

        ordered_group_names = sorted(employee_groups.keys(), key=_group_sort_key)

        group_meta: Dict[str, Dict[str, str]] = {}
        for group_name in employee_groups.keys():
            work_class = work_class_by_name.get(group_name)
            base_color = work_class.color if work_class else None
            group_meta[group_name] = _build_group_meta(group_name, base_color)

        # Für Rückwärtskompatibilität
        vollzeit_employees = list(employee_groups.get("Vollzeit", []))
        teilzeit_employees = list(employee_groups.get("Teilzeit", []))
        aushilfe_employees = list(employee_groups.get("Aushilfe", []))

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
            group_meta=group_meta,
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

        work_classes = (
            WorkClass.query.order_by(WorkClass.is_default.desc(), WorkClass.name.asc()).all()
        )
        active_work_classes = [wc for wc in work_classes if wc.is_active]
        inactive_work_classes = [wc for wc in work_classes if not wc.is_active]

        suggested_work_classes = [
            {
                "name": "Vollzeit",
                "hours_per_week": 40,
                "hours_per_month": 160,
                "description": "Standardmodell für Mitarbeitende mit regulärem Pensum.",
                "color": "#1d4ed8",
            },
            {
                "name": "Teilzeit",
                "hours_per_week": 25,
                "hours_per_month": 100,
                "description": "Flexible Arbeitszeitmodelle für geteilte Rollen.",
                "color": "#10b981",
            },
            {
                "name": "Minijob",
                "hours_per_week": 10,
                "hours_per_month": 40,
                "description": "Geringfügige Beschäftigungen für Stoßzeiten.",
                "color": "#f97316",
            },
        ]

        existing_names = {wc.name.lower() for wc in work_classes}
        recommended_work_classes = [
            suggestion
            for suggestion in suggested_work_classes
            if suggestion["name"].lower() not in existing_names
        ]

        quick_actions = [
            {
                "id": "work-classes",
                "icon": "🧭",
                "title": "Arbeitsklassen verwalten",
                "description": "Standardmodelle wie Vollzeit, Teilzeit oder Minijob pflegen.",
                "href": "#work-class-manager",
                "state": "Verfügbar",
            },
            {
                "id": "backup-mode",
                "icon": "🛡️",
                "title": "Backup-Modus öffnen",
                "description": "Notfallmaßnahmen & Wartung für System-Administratoren.",
                "href": url_for("backup_mode"),
                "state": "Verfügbar",
            },
            {
                "id": "sync-policies",
                "icon": "🛡️",
                "title": "Richtlinien synchronisieren",
                "description": "Berechtigungen für neue Mandanten mit einem Klick ausrollen.",
                "state": "In Planung",
            },
            {
                "id": "refresh-cache",
                "icon": "🧹",
                "title": "Systemcache bereinigen",
                "description": "Hält Integrationen und Hintergrundprozesse performant.",
                "state": "Demnächst",
            },
        ]

        focus_areas = [
            {
                "icon": "🏢",
                "title": "Mandantenverwaltung",
                "description": "Strukturen für unterschiedliche Standorte oder Firmenbereiche abbilden.",
                "badge": "Live",
            },
            {
                "icon": "📡",
                "title": "Netzwerkintegration",
                "description": "Anbindung an bestehende Verzeichnisdienste und VPN-fähige Nutzung.",
                "badge": "Pilot",
            },
            {
                "icon": "📊",
                "title": "Auswertungen",
                "description": "Berichte für Geschäftsführung, HR und Betriebsrat vorbereiten.",
                "badge": "Roadmap",
            },
        ]

        maintenance_notes = [
            {
                "icon": "🗄️",
                "title": "Datenbank-Optimierung",
                "window": "Jeden Sonntag · 02:00 – 03:00 Uhr",
                "impact": "Kurzzeitige Warteschlangen bei Schreibzugriffen möglich",
            },
            {
                "icon": "🔐",
                "title": "Sicherheitsupdates",
                "window": "Mittwochs · 22:00 Uhr",
                "impact": "Dienste werden nacheinander neu gestartet",
            },
            {
                "icon": "☁️",
                "title": "Backup-Replikation",
                "window": "Stündlich",
                "impact": "Kollokation in sekundäres Rechenzentrum",
            },
        ]

        roadmap = [
            {
                "icon": "⚙️",
                "title": "Active Directory Sync",
                "description": "Synchronisiert Benutzer direkt aus dem Unternehmensverzeichnis.",
                "quarter": "Q3 2024",
            },
            {
                "icon": "🧾",
                "title": "Zeiterfassungs-Export",
                "description": "Standardisierte Formate für Lohnbuchhaltung und ERP.",
                "quarter": "Q4 2024",
            },
            {
                "icon": "📱",
                "title": "Self-Service App",
                "description": "Mitarbeitende passen Benachrichtigungen und Profile selbst an.",
                "quarter": "Q1 2025",
            },
        ]

        audit_notes = [
            "Revisionssichere Archivierung der Audit-Logs für mindestens 24 Monate.",
            "Export als CSV und PDF inklusive digitaler Signatur in Vorbereitung.",
            "Alarmierung bei ungewöhnlichen Anmeldeversuchen über E-Mail und Webhooks.",
        ]

        stats = [
            {"label": "Aktive Arbeitsklassen", "value": len(active_work_classes)},
            {"label": "Geplante Integrationen", "value": len(roadmap)},
            {"label": "Direktaktionen", "value": len(quick_actions)},
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
            work_classes=work_classes,
            active_work_classes=active_work_classes,
            inactive_work_classes=inactive_work_classes,
            recommended_work_classes=recommended_work_classes,
        )

    @app.route("/settings/backup-modus")
    @super_admin_required
    def backup_mode() -> str:
        """Spezialbereich für Backups und Notfallmaßnahmen."""

        employee_count = Employee.query.count()
        department_count = Department.query.count()
        shift_count = Shift.query.count()
        leave_count = Leave.query.count()

        backup_stats = [
            {"label": "Gespeicherte Mitarbeitende", "value": employee_count},
            {"label": "Aktive Abteilungen", "value": department_count},
            {"label": "Einsatzdatensätze", "value": shift_count},
            {"label": "Abwesenheiten", "value": leave_count},
        ]

        database_uri = current_app.config.get("SQLALCHEMY_DATABASE_URI", "")
        db_file_path: Path | None = None
        db_display_path = "Unbekannt"
        db_file_size = "Nicht verfügbar"

        if database_uri.startswith("sqlite:///"):
            raw_path = database_uri.replace("sqlite:///", "", 1)
            db_file_path = Path(raw_path)
            if not db_file_path.is_absolute():
                db_file_path = (Path(current_app.instance_path) / raw_path).resolve()

            db_display_path = str(db_file_path)
            if db_file_path.exists():
                db_file_size = _format_file_size(db_file_path.stat().st_size)
            else:
                db_file_size = "Datei nicht gefunden"

        return render_template(
            "settings_backup.html",
            backup_stats=backup_stats,
            db_file_path=db_display_path,
            db_file_size=db_file_size,
            is_sqlite_database=database_uri.startswith("sqlite:///"),
            last_checked=datetime.now(),
        )

    @app.route("/settings/backup-modus/datenbank-zuruecksetzen", methods=["POST"])
    @super_admin_required
    def reset_database() -> str:
        """Leert die Datenbank und spielt Standardwerte erneut ein."""

        confirmation = (request.form.get("confirmation") or "").strip().lower()
        acknowledge = request.form.get("acknowledge") == "on"

        if confirmation not in {"löschen", "loeschen"} or not acknowledge:
            flash(
                "Sicherheitsabfrage fehlgeschlagen. Bitte bestätigen Sie mit 'LÖSCHEN' und aktivierter Warnung.",
                "danger",
            )
            return redirect(url_for("backup_mode"))

        try:
            db.session.remove()
            db.drop_all()
            db.create_all()
            _create_default_admin_account()
        except Exception as exc:  # pragma: no cover - sicherheitsrelevante Fehlerbehandlung
            db.session.rollback()
            current_app.logger.exception("Fehler beim Zurücksetzen der Datenbank", exc_info=exc)
            flash("Zurücksetzen fehlgeschlagen. Details finden Sie im Log.", "danger")
            return redirect(url_for("backup_mode"))

        flash(
            "Datenbank wurde gelöscht und mit Standardwerten initialisiert. Bitte melden Sie sich erneut mit admin/admin an.",
            "success",
        )
        session.pop("user_id", None)
        session.pop("is_admin", None)
        session.pop("department_id", None)
        return redirect(url_for("login"))

    def _parse_hours_value(raw_value: str | None) -> float | None:
        if raw_value is None:
            return None
        value = raw_value.replace(",", ".").strip()
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            raise ValueError("invalid")

    @app.route("/settings/work-classes/anlegen", methods=["POST"])
    @super_admin_required
    def create_work_class() -> str:
        name = (request.form.get("name") or "").strip()
        hours_per_week_raw = request.form.get("hours_per_week")
        hours_per_month_raw = request.form.get("hours_per_month")
        description = (request.form.get("description") or "").strip()
        color = (request.form.get("color") or "").strip() or None
        set_default = request.form.get("is_default") == "on"

        errors: List[str] = []

        if not name:
            errors.append("Bitte geben Sie einen Namen für die Arbeitsklasse an.")
        else:
            existing = WorkClass.query.filter(
                func.lower(WorkClass.name) == name.lower()
            ).first()
            if existing:
                errors.append("Es existiert bereits eine Arbeitsklasse mit diesem Namen.")

        hours_per_week = None
        if hours_per_week_raw is not None:
            try:
                parsed = _parse_hours_value(hours_per_week_raw)
                if parsed is not None and parsed < 0:
                    errors.append("Wochenstunden dürfen nicht negativ sein.")
                else:
                    hours_per_week = parsed
            except ValueError:
                errors.append("Wochenstunden konnten nicht interpretiert werden.")

        hours_per_month = None
        if hours_per_month_raw is not None:
            try:
                parsed = _parse_hours_value(hours_per_month_raw)
                if parsed is not None and parsed < 0:
                    errors.append("Monatsstunden dürfen nicht negativ sein.")
                else:
                    hours_per_month = parsed
            except ValueError:
                errors.append("Monatsstunden konnten nicht interpretiert werden.")

        if errors:
            for message in errors:
                flash(message, "danger")
            return redirect(url_for("system_settings"))

        new_work_class = WorkClass(
            name=name,
            hours_per_week=hours_per_week,
            hours_per_month=hours_per_month,
            description=description or None,
            color=color,
            is_active=True,
        )

        if set_default:
            for existing_default in WorkClass.query.filter_by(is_default=True).all():
                existing_default.is_default = False
            new_work_class.is_default = True

        try:
            db.session.add(new_work_class)
            db.session.commit()
            flash(f"Arbeitsklasse '{new_work_class.name}' wurde angelegt.", "success")
        except IntegrityError:
            db.session.rollback()
            flash("Die Arbeitsklasse konnte nicht gespeichert werden.", "danger")

        return redirect(url_for("system_settings"))

    @app.route("/settings/work-classes/<int:class_id>/aktualisieren", methods=["POST"])
    @super_admin_required
    def update_work_class(class_id: int) -> str:
        work_class = WorkClass.query.get_or_404(class_id)

        name = (request.form.get("name") or "").strip()
        hours_per_week_raw = request.form.get("hours_per_week")
        hours_per_month_raw = request.form.get("hours_per_month")
        description = (request.form.get("description") or "").strip()
        color = (request.form.get("color") or "").strip() or None
        set_default = request.form.get("is_default") == "on"

        errors: List[str] = []

        if name:
            existing = WorkClass.query.filter(
                func.lower(WorkClass.name) == name.lower(),
                WorkClass.id != work_class.id,
            ).first()
            if existing:
                errors.append("Eine andere Arbeitsklasse verwendet bereits diesen Namen.")
        else:
            errors.append("Der Name darf nicht leer sein.")

        try:
            parsed_week = _parse_hours_value(hours_per_week_raw)
            if parsed_week is not None and parsed_week < 0:
                errors.append("Wochenstunden dürfen nicht negativ sein.")
        except ValueError:
            errors.append("Wochenstunden konnten nicht interpretiert werden.")
            parsed_week = None

        try:
            parsed_month = _parse_hours_value(hours_per_month_raw)
            if parsed_month is not None and parsed_month < 0:
                errors.append("Monatsstunden dürfen nicht negativ sein.")
        except ValueError:
            errors.append("Monatsstunden konnten nicht interpretiert werden.")
            parsed_month = None

        if errors:
            for message in errors:
                flash(message, "danger")
            return redirect(url_for("system_settings"))

        work_class.name = name
        work_class.hours_per_week = parsed_week
        work_class.hours_per_month = parsed_month
        work_class.description = description or None
        work_class.color = color

        if set_default:
            for existing_default in WorkClass.query.filter_by(is_default=True).all():
                if existing_default.id != work_class.id:
                    existing_default.is_default = False
            work_class.is_default = True
            work_class.is_active = True

        try:
            db.session.commit()
            flash(f"Arbeitsklasse '{work_class.name}' wurde aktualisiert.", "success")
        except IntegrityError:
            db.session.rollback()
            flash("Die Änderungen konnten nicht gespeichert werden.", "danger")

        return redirect(url_for("system_settings"))

    @app.route("/settings/work-classes/<int:class_id>/umschalten", methods=["POST"])
    @super_admin_required
    def toggle_work_class(class_id: int) -> str:
        work_class = WorkClass.query.get_or_404(class_id)
        work_class.is_active = not work_class.is_active
        if not work_class.is_active and work_class.is_default:
            work_class.is_default = False

        db.session.commit()

        status = "reaktiviert" if work_class.is_active else "deaktiviert"
        flash(f"Arbeitsklasse '{work_class.name}' wurde {status}.", "success")
        return redirect(url_for("system_settings"))

    @app.route("/settings/work-classes/<int:class_id>/standard", methods=["POST"])
    @super_admin_required
    def set_default_work_class(class_id: int) -> str:
        work_class = WorkClass.query.get_or_404(class_id)

        for existing_default in WorkClass.query.filter_by(is_default=True).all():
            if existing_default.id != work_class.id:
                existing_default.is_default = False

        work_class.is_default = True
        work_class.is_active = True
        db.session.commit()

        flash(f"'{work_class.name}' ist jetzt die Standard-Arbeitsklasse.", "success")
        return redirect(url_for("system_settings"))

    @app.route("/settings/work-classes/<int:class_id>/loeschen", methods=["POST"])
    @super_admin_required
    def delete_work_class(class_id: int) -> str:
        work_class = WorkClass.query.get_or_404(class_id)

        if work_class.is_default:
            flash("Die Standard-Arbeitsklasse kann nicht gelöscht werden.", "warning")
            return redirect(url_for("system_settings"))

        db.session.delete(work_class)
        db.session.commit()

        flash(f"Arbeitsklasse '{work_class.name}' wurde entfernt.", "success")
        return redirect(url_for("system_settings"))

    @app.route("/settings/automatisierte-freigaben")
    @super_admin_required
    def automated_approvals() -> str:
        """Verwaltet zeitgesteuerte Automatisierungen für Freigaben."""

        _process_due_automations()

        automations = ApprovalAutomation.query.order_by(ApprovalAutomation.created_at.desc()).all()

        position_rows = (
            Employee.query.with_entities(Employee.position)
            .filter(Employee.position.isnot(None), Employee.position != "")
            .distinct()
            .order_by(Employee.position.asc())
            .all()
        )
        positions = []
        seen_positions = set()
        for row in position_rows:
            value = (row[0] or "").strip()
            if value and value not in seen_positions:
                positions.append(value)
                seen_positions.add(value)

        now = datetime.now()
        active_count = sum(1 for automation in automations if automation.is_active)
        inactive_count = sum(1 for automation in automations if not automation.is_active)
        next_runs = [automation.next_run for automation in automations if automation.next_run]
        upcoming_run = min(next_runs) if next_runs else None
        overdue_automations = [
            automation
            for automation in automations
            if automation.next_run and automation.next_run <= now
        ]
        runs_last_24h = sum(
            1
            for automation in automations
            if automation.last_run and now - automation.last_run <= timedelta(hours=24)
        )
        recent_runs = sorted(
            [automation for automation in automations if automation.last_run],
            key=lambda automation: automation.last_run or datetime.min,
            reverse=True,
        )[:5]
        type_statistics = [
            {
                "label": label,
                "count": sum(
                    1 for automation in automations if automation.automation_type == value
                ),
            }
            for value, label in AUTOMATION_TYPE_CHOICES
        ]
        timeline_entries: List[dict] = []
        for automation in automations:
            for index, occurrence in enumerate(_forecast_automation_runs(automation, limit=3)):
                timeline_entries.append(
                    {
                        "automation": automation,
                        "scheduled_time": occurrence,
                        "is_primary": index == 0,
                        "is_overdue": occurrence <= now,
                    }
                )
        timeline_entries.sort(key=lambda entry: (entry["scheduled_time"], entry["automation"].id))
        timeline_entries = timeline_entries[:10]

        type_labels = dict(AUTOMATION_TYPE_CHOICES)
        schedule_labels = dict(SCHEDULE_CHOICES)
        weekday_map = {code: label for code, label in WEEKDAY_LABELS}

        return render_template(
            "automated_approvals.html",
            automations=automations,
            automation_types=AUTOMATION_TYPE_CHOICES,
            schedule_choices=SCHEDULE_CHOICES,
            weekday_labels=WEEKDAY_LABELS,
            active_count=active_count,
            upcoming_run=upcoming_run,
            overdue_count=len(overdue_automations),
            runs_last_24h=runs_last_24h,
            inactive_count=inactive_count,
            timeline_entries=timeline_entries,
            recent_runs=recent_runs,
            type_statistics=type_statistics,
            type_labels=type_labels,
            schedule_labels=schedule_labels,
            weekday_map=weekday_map,
            positions=positions,
        )

    @app.route("/settings/automatisierte-freigaben/anlegen", methods=["POST"])
    @super_admin_required
    def create_automated_approval() -> str:
        """Erstellt eine neue Automatisierung für Freigaben."""

        name = (request.form.get("name") or "").strip()
        automation_type = request.form.get("automation_type")
        schedule_type = request.form.get("schedule_type")
        run_time_raw = request.form.get("run_time")
        once_date_raw = request.form.get("once_date")
        selected_days = request.form.getlist("days_of_week")
        target_position = (request.form.get("target_position") or "").strip()

        if not name:
            flash("Bitte vergeben Sie einen Namen für die Automatisierung.", "danger")
            return redirect(url_for("automated_approvals"))

        valid_types = {choice[0] for choice in AUTOMATION_TYPE_CHOICES}
        if automation_type not in valid_types:
            flash("Der ausgewählte Automatisierungstyp ist ungültig.", "danger")
            return redirect(url_for("automated_approvals"))

        valid_schedules = {choice[0] for choice in SCHEDULE_CHOICES}
        if schedule_type not in valid_schedules:
            flash("Der ausgewählte Zeitplan ist ungültig.", "danger")
            return redirect(url_for("automated_approvals"))

        run_time_value = None
        if run_time_raw:
            try:
                run_time_value = datetime.strptime(run_time_raw, "%H:%M").time()
            except ValueError:
                flash("Bitte geben Sie eine gültige Uhrzeit im Format HH:MM an.", "danger")
                return redirect(url_for("automated_approvals"))

        next_run = None
        days_value = ",".join(sorted(set(selected_days))) if selected_days else None

        if schedule_type == "weekly" and not days_value:
            flash("Bitte wählen Sie mindestens einen Wochentag für den wöchentlichen Ablauf.", "danger")
            return redirect(url_for("automated_approvals"))

        if schedule_type == "once":
            if not once_date_raw or not run_time_value:
                flash("Für eine einmalige Automatisierung sind Datum und Uhrzeit erforderlich.", "danger")
                return redirect(url_for("automated_approvals"))
            try:
                run_date = datetime.strptime(once_date_raw, "%Y-%m-%d").date()
            except ValueError:
                flash("Bitte geben Sie ein gültiges Datum an.", "danger")
                return redirect(url_for("automated_approvals"))
            next_run = datetime.combine(run_date, run_time_value)
            if next_run <= datetime.now():
                flash("Der Ausführungszeitpunkt muss in der Zukunft liegen.", "danger")
                return redirect(url_for("automated_approvals"))
        else:
            if not run_time_value:
                flash("Bitte legen Sie eine Uhrzeit fest, zu der die Automatisierung ausgeführt werden soll.", "danger")
                return redirect(url_for("automated_approvals"))
            next_run = _calculate_next_run(
                schedule_type,
                run_time_value,
                days_value,
                reference=datetime.now() + timedelta(seconds=1),
            )
            if not next_run:
                flash("Der nächste Ausführungstermin konnte nicht berechnet werden.", "danger")
                return redirect(url_for("automated_approvals"))

        if automation_type == "auto_schedule_position":
            if not target_position:
                flash("Bitte wählen Sie die gewünschte Mitarbeitergruppe aus.", "danger")
                return redirect(url_for("automated_approvals"))

            position_rows = (
                Employee.query.with_entities(Employee.position)
                .filter(Employee.position.isnot(None), Employee.position != "")
                .distinct()
                .all()
            )
            valid_positions = {
                (row[0] or "").strip()
                for row in position_rows
                if (row[0] or "").strip()
            }
            if target_position not in valid_positions:
                flash("Die ausgewählte Mitarbeitergruppe ist ungültig.", "danger")
                return redirect(url_for("automated_approvals"))
        else:
            target_position = None

        automation = ApprovalAutomation(
            name=name,
            automation_type=automation_type,
            schedule_type=schedule_type,
            run_time=run_time_value,
            days_of_week=days_value,
            next_run=next_run,
            is_active=True,
            target_position=target_position,
        )

        db.session.add(automation)
        db.session.commit()

        flash(f"Automatisierung '{name}' wurde angelegt.", "success")
        return redirect(url_for("automated_approvals"))

    @app.route("/settings/automatisierte-freigaben/<int:automation_id>/umschalten", methods=["POST"])
    @super_admin_required
    def toggle_automated_approval(automation_id: int) -> str:
        """Aktiviert oder deaktiviert eine bestehende Automatisierung."""

        automation = ApprovalAutomation.query.get_or_404(automation_id)
        automation.is_active = not automation.is_active

        if automation.is_active:
            if automation.schedule_type == "once" and not automation.next_run:
                flash(
                    "Einmalige Automatisierungen können nach ihrer Ausführung nicht erneut aktiviert werden.",
                    "warning",
                )
                automation.is_active = False
            else:
                if not automation.next_run:
                    automation.next_run = _calculate_next_run(
                        automation.schedule_type,
                        automation.run_time,
                        automation.days_of_week,
                        reference=datetime.now() + timedelta(seconds=1),
                    )
        db.session.commit()

        status = "aktiviert" if automation.is_active else "deaktiviert"
        flash(f"Automatisierung '{automation.name}' wurde {status}.", "success")
        return redirect(url_for("automated_approvals"))

    @app.route("/settings/automatisierte-freigaben/<int:automation_id>/loeschen", methods=["POST"])
    @super_admin_required
    def delete_automated_approval(automation_id: int) -> str:
        """Löscht eine Automatisierung dauerhaft."""

        automation = ApprovalAutomation.query.get_or_404(automation_id)
        db.session.delete(automation)
        db.session.commit()
        flash(f"Automatisierung '{automation.name}' wurde entfernt.", "info")
        return redirect(url_for("automated_approvals"))

    @app.route("/settings/automatisierte-freigaben/<int:automation_id>/ausfuehren", methods=["POST"])
    @super_admin_required
    def run_automated_approval(automation_id: int) -> str:
        """Führt eine Automatisierung sofort aus."""

        automation = ApprovalAutomation.query.get_or_404(automation_id)
        summary = _run_and_schedule_automation(automation)
        flash(f"Automatisierung '{automation.name}' ausgeführt: {summary}", "success")
        return redirect(url_for("automated_approvals"))

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

    ROLE_META = {
        "super_admin": {"label": "Super-Admin", "icon": "🔥", "accent": "danger"},
        "department_admin": {"label": "Abteilungs-Admin", "icon": "🏢", "accent": "primary"},
        "employee": {"label": "Mitarbeiter", "icon": "👤", "accent": "muted"},
    }

    ROLE_LABELS = {key: value["label"] for key, value in ROLE_META.items()}

    ROLE_CARD_DESCRIPTIONS = {
        "super_admin": "Vollzugriff auf Einstellungen und alle Abteilungen.",
        "department_admin": "Verantwortlich für Planung und Freigaben der eigenen Abteilung.",
        "employee": "Hat Zugriff auf persönliche Daten, Abwesenheiten und Dienstpläne.",
    }

    def _resolve_user_role(user: Employee) -> str:
        """Gibt die logische Rolle eines Benutzers zurück."""

        if user.is_admin and not user.department_id:
            return "super_admin"
        if user.is_admin and user.department_id:
            return "department_admin"
        return "employee"

    def _calculate_role_counts(user_list: List[Employee]) -> Dict[str, int]:
        """Zählt, wie viele Benutzer je Rolle vorhanden sind."""

        counts = {
            "total": len(user_list),
            "super_admin": 0,
            "department_admin": 0,
            "employee": 0,
        }

        for user in user_list:
            role_key = _resolve_user_role(user)
            counts[role_key] += 1

        return counts

    def _apply_user_filters(
        query,
        *,
        search_query: str = "",
        role: str = "all",
        department: str = "all",
        contact: str = "all",
    ):
        """Wendet Such- und Filterparameter auf die Benutzerabfrage an."""

        if search_query:
            like_pattern = f"%{search_query.lower()}%"
            query = query.filter(
                or_(
                    func.lower(Employee.name).like(like_pattern),
                    func.lower(Employee.username).like(like_pattern),
                    func.lower(Employee.email).like(like_pattern),
                )
            )

        if role == "super_admin":
            query = query.filter(Employee.is_admin.is_(True), Employee.department_id.is_(None))
        elif role == "department_admin":
            query = query.filter(Employee.is_admin.is_(True), Employee.department_id.isnot(None))
        elif role == "employee":
            query = query.filter(or_(Employee.is_admin.is_(False), Employee.is_admin.is_(None)))

        if department == "none":
            query = query.filter(Employee.department_id.is_(None))
        else:
            try:
                department_id = int(department)
            except (TypeError, ValueError):
                department_id = None

            if department_id:
                query = query.filter(Employee.department_id == department_id)

        trimmed_email = func.trim(func.coalesce(Employee.email, ""))
        trimmed_phone = func.trim(func.coalesce(Employee.phone, ""))

        if contact == "complete":
            query = query.filter(
                func.length(trimmed_email) > 0,
                func.length(trimmed_phone) > 0,
            )
        elif contact == "missing_email":
            query = query.filter(func.length(trimmed_email) == 0)
        elif contact == "missing_phone":
            query = query.filter(func.length(trimmed_phone) == 0)
        elif contact == "incomplete":
            query = query.filter(
                or_(
                    func.length(trimmed_email) == 0,
                    func.length(trimmed_phone) == 0,
                )
            )

        return query

    def _apply_user_sort(query, sort_option: str):
        """Sortiert die Benutzerliste entsprechend der Auswahl."""

        sort_option = sort_option or "name_asc"

        if sort_option == "name_desc":
            return query.order_by(func.lower(Employee.name).desc())
        if sort_option == "newest":
            return query.order_by(Employee.id.desc())
        if sort_option == "oldest":
            return query.order_by(Employee.id.asc())
        if sort_option == "role":
            role_case = case(
                (
                    and_(Employee.is_admin.is_(True), Employee.department_id.is_(None)),
                    0,
                ),
                (
                    and_(Employee.is_admin.is_(True), Employee.department_id.isnot(None)),
                    1,
                ),
                else_=2,
            )
            return query.order_by(role_case, func.lower(Employee.name).asc())
        if sort_option == "department":
            return query.outerjoin(Department).order_by(
                func.lower(Department.name).asc(),
                func.lower(Employee.name).asc(),
            )

        # Standard: alphabetisch nach Name
        return query.order_by(func.lower(Employee.name).asc())

    @app.route("/system/benutzer")
    @admin_required
    def user_management() -> str:
        """Benutzerverwaltung - nur für Administratoren."""
        current_user = get_current_user()

        base_query = Employee.query.filter(Employee.username.isnot(None))

        # Abteilungsadministratoren sehen nur ihre eigene Abteilung
        if current_user and current_user.department_id:
            base_query = base_query.filter(Employee.department_id == current_user.department_id)

        departments = Department.query.order_by(Department.name).all()
        department_ids = {str(department.id) for department in departments}

        search_query = request.args.get("q", "").strip()
        role_filter = request.args.get("role", "all")
        department_filter = request.args.get("department", "all")
        sort_option = request.args.get("sort", "name_asc")
        contact_filter = request.args.get("contact", "all")
        view_mode = request.args.get("view", "table")

        if role_filter not in {"all", "super_admin", "department_admin", "employee"}:
            role_filter = "all"

        if department_filter not in department_ids | {"all", "none"}:
            department_filter = "all"

        if sort_option not in {"name_asc", "name_desc", "newest", "oldest", "role", "department"}:
            sort_option = "name_asc"

        if contact_filter not in {"all", "complete", "missing_email", "missing_phone", "incomplete"}:
            contact_filter = "all"

        if view_mode not in {"table", "cards"}:
            view_mode = "table"

        scoped_users = base_query.options(joinedload(Employee.department)).all()

        filtered_query = _apply_user_filters(
            base_query,
            search_query=search_query,
            role=role_filter,
            department=department_filter,
            contact=contact_filter,
        )
        sorted_query = _apply_user_sort(filtered_query, sort_option)
        users = sorted_query.options(joinedload(Employee.department)).all()

        role_counts_total = _calculate_role_counts(scoped_users)
        role_counts_visible = _calculate_role_counts(users)

        contact_counts: Dict[str, int] = {
            "complete": 0,
            "missing_email": 0,
            "missing_phone": 0,
        }

        for user in scoped_users:
            has_email = bool(user.email and user.email.strip())
            has_phone = bool(user.phone and user.phone.strip())

            if has_email and has_phone:
                contact_counts["complete"] += 1
            else:
                if not has_email:
                    contact_counts["missing_email"] += 1
                if not has_phone:
                    contact_counts["missing_phone"] += 1

        contact_counts["incomplete"] = role_counts_total["total"] - contact_counts["complete"]
        contact_counts["all"] = role_counts_total["total"]

        department_counts: Dict[str, Dict[str, int]] = defaultdict(lambda: {"total": 0, "admins": 0})
        for user in scoped_users:
            key = str(user.department_id) if user.department_id is not None else "none"
            department_counts[key]["total"] += 1
            if user.is_admin:
                department_counts[key]["admins"] += 1

        department_overview = []
        for department in departments:
            stats = department_counts.get(str(department.id))
            if not stats:
                continue
            department_overview.append(
                {
                    "id": str(department.id),
                    "name": department.name,
                    "color": department.color,
                    "total": stats["total"],
                    "admins": stats["admins"],
                }
            )

        system_stats = department_counts.get("none")
        if system_stats:
            department_overview.insert(
                0,
                {
                    "id": "none",
                    "name": "Systemweit / ohne Abteilung",
                    "color": None,
                    "total": system_stats["total"],
                    "admins": system_stats["admins"],
                },
            )

        department_lookup = {item["id"]: item for item in department_overview}

        stats = {
            "total": role_counts_total["total"],
            "visible": role_counts_visible["total"],
        }

        filter_values = {
            "q": search_query,
            "role": role_filter,
            "department": department_filter,
            "sort": sort_option,
            "contact": contact_filter,
            "view": view_mode,
        }

        base_link_params = {}
        if search_query:
            base_link_params["q"] = search_query
        if department_filter not in {"all"}:
            base_link_params["department"] = department_filter
        if sort_option and sort_option != "name_asc":
            base_link_params["sort"] = sort_option
        if contact_filter != "all":
            base_link_params["contact"] = contact_filter
        if view_mode != "table":
            base_link_params["view"] = view_mode

        sort_options = [
            ("name_asc", "Name (A-Z)"),
            ("name_desc", "Name (Z-A)"),
            ("role", "Rollenpriorität"),
            ("department", "Abteilung"),
            ("newest", "Neueste zuerst"),
            ("oldest", "Älteste zuerst"),
        ]

        contact_filter_options = [
            {
                "value": "all",
                "label": "Alle Kontaktzustände",
                "description": "Keine Einschränkung",
                "count_key": "all",
            },
            {
                "value": "complete",
                "label": "Kontakt vollständig",
                "description": "E-Mail und Telefon vorhanden",
                "count_key": "complete",
            },
            {
                "value": "incomplete",
                "label": "Kontakt offen",
                "description": "Mindestens eine Angabe fehlt",
                "count_key": "incomplete",
            },
            {
                "value": "missing_email",
                "label": "Ohne E-Mail",
                "description": "E-Mail-Adresse fehlt",
                "count_key": "missing_email",
            },
            {
                "value": "missing_phone",
                "label": "Ohne Telefonnummer",
                "description": "Telefonnummer fehlt",
                "count_key": "missing_phone",
            },
        ]

        role_cards = [
            {
                "id": "all",
                "label": "Alle Benutzer",
                "icon": "👥",
                "description": "Systemweiter Überblick über alle aktiven Benutzer.",
                "total": role_counts_total["total"],
                "visible": role_counts_visible["total"],
                "url": url_for("user_management", **base_link_params),
                "active": role_filter == "all",
                "accent": "primary",
                "progress": (
                    (role_counts_visible["total"] / role_counts_total["total"]) * 100
                    if role_counts_total["total"]
                    else 0
                ),
                "missing": role_counts_total["total"] - role_counts_visible["total"],
            }
        ]

        for role_key in ("super_admin", "department_admin", "employee"):
            params = dict(base_link_params)
            params["role"] = role_key
            total_for_role = role_counts_total[role_key]
            visible_for_role = role_counts_visible[role_key]
            role_cards.append(
                {
                    "id": role_key,
                    "label": ROLE_META[role_key]["label"],
                    "icon": ROLE_META[role_key]["icon"],
                    "description": ROLE_CARD_DESCRIPTIONS[role_key],
                    "total": role_counts_total[role_key],
                    "visible": role_counts_visible[role_key],
                    "url": url_for("user_management", **params),
                    "active": role_filter == role_key,
                    "accent": ROLE_META[role_key]["accent"],
                    "progress": (
                        (visible_for_role / total_for_role) * 100
                        if total_for_role
                        else 0
                    ),
                    "missing": total_for_role - visible_for_role,
                }
            )

        department_link_base = dict(base_link_params)
        department_link_base.pop("department", None)
        if role_filter != "all":
            department_link_base["role"] = role_filter
        for item in department_overview:
            params = dict(department_link_base)
            params["department"] = item["id"]
            item["url"] = url_for("user_management", **params)

        total_users = stats["total"]
        admin_total = role_counts_total["super_admin"] + role_counts_total["department_admin"]
        contact_completion_rate = (
            round((contact_counts["complete"] / total_users) * 100, 1)
            if total_users
            else 0.0
        )
        admin_ratio = (
            round((admin_total / total_users) * 100, 1)
            if total_users
            else 0.0
        )

        insights = [
            {
                "icon": "🔎",
                "label": "Aktuelle Ansicht",
                "value": stats["visible"],
                "subtext": f"von {total_users} Benutzer:innen gesamt" if total_users else "Keine Benutzer vorhanden",
            },
            {
                "icon": "✅",
                "label": "Kontakt vollständig",
                "value": contact_counts["complete"],
                "subtext": (
                    f"{contact_completion_rate:.0f}% mit E-Mail & Telefon"
                    if total_users
                    else "Noch keine Kontaktdaten hinterlegt"
                ),
            },
            {
                "icon": "🛡️",
                "label": "Admins gesamt",
                "value": admin_total,
                "subtext": (
                    f"{admin_ratio:.0f}% Anteil am Team"
                    if total_users
                    else "Noch keine Administratoren ernannt"
                ),
            },
            {
                "icon": "📭",
                "label": "Kontakt offen",
                "value": contact_counts["incomplete"],
                "subtext": (
                    "Keine offenen Kontaktdaten"
                    if contact_counts["incomplete"] == 0
                    else f"{contact_counts['missing_email']} ohne E-Mail · {contact_counts['missing_phone']} ohne Telefon"
                ),
            },
        ]

        top_departments = [
            {
                **item,
                "share": (
                    round((item["total"] / total_users) * 100, 1)
                    if total_users
                    else 0.0
                ),
            }
            for item in department_overview
            if item["id"] != "none"
        ]
        top_departments.sort(key=lambda entry: entry["total"], reverse=True)
        top_departments = top_departments[:3]

        def build_filter_url(exclude: str | None = None) -> str:
            params = {}
            if search_query and exclude != "q":
                params["q"] = search_query
            if role_filter != "all" and exclude != "role":
                params["role"] = role_filter
            if department_filter not in {"all"} and exclude != "department":
                params["department"] = department_filter
            if sort_option and sort_option != "name_asc" and exclude != "sort":
                params["sort"] = sort_option
            if contact_filter != "all" and exclude != "contact":
                params["contact"] = contact_filter
            if view_mode != "table":
                params["view"] = view_mode
            return url_for("user_management", **params)

        active_filters = []
        if search_query:
            active_filters.append(
                {
                    "label": "Suche",
                    "value": f"„{search_query}“",
                    "remove_url": build_filter_url("q"),
                }
            )
        if role_filter != "all":
            active_filters.append(
                {
                    "label": "Rolle",
                    "value": ROLE_LABELS.get(role_filter, role_filter),
                    "remove_url": build_filter_url("role"),
                }
            )
        if department_filter not in {"all"}:
            if department_filter == "none":
                department_label = "Ohne Abteilung"
            else:
                department_label = next(
                    (
                        department.name
                        for department in departments
                        if str(department.id) == department_filter
                    ),
                    "Abteilung",
                )
            active_filters.append(
                {
                    "label": "Abteilung",
                    "value": department_label,
                    "remove_url": build_filter_url("department"),
                }
            )
        if contact_filter != "all":
            contact_label = next(
                (
                    option["label"]
                    for option in contact_filter_options
                    if option["value"] == contact_filter
                ),
                "Kontaktstatus",
            )
            active_filters.append(
                {
                    "label": "Kontakt",
                    "value": contact_label,
                    "remove_url": build_filter_url("contact"),
                }
            )

        clear_params = {}
        if sort_option != "name_asc":
            clear_params["sort"] = sort_option
        if view_mode != "table":
            clear_params["view"] = view_mode
        clear_filters_url = url_for("user_management", **clear_params)

        export_params = {}
        if search_query:
            export_params["q"] = search_query
        if role_filter != "all":
            export_params["role"] = role_filter
        if department_filter not in {"all"}:
            export_params["department"] = department_filter
        if contact_filter != "all":
            export_params["contact"] = contact_filter
        export_params["sort"] = sort_option

        export_url = url_for("export_users", **export_params)

        view_toggle_params = dict(base_link_params)
        view_toggle_params.pop("view", None)
        view_modes = []
        for view_id, label, icon in (
            ("table", "Tabellenansicht", "📋"),
            ("cards", "Kartenansicht", "🗂️"),
        ):
            params = dict(view_toggle_params)
            if view_id != "table":
                params["view"] = view_id
            view_modes.append(
                {
                    "id": view_id,
                    "label": label,
                    "icon": icon,
                    "active": view_mode == view_id,
                    "url": url_for("user_management", **params),
                }
            )

        missing_contact_params = dict(base_link_params)
        if "contact" in missing_contact_params:
            missing_contact_params.pop("contact")
        missing_contact_params["contact"] = "incomplete"
        missing_contact_url = url_for("user_management", **missing_contact_params)

        role_lookup = {user.id: _resolve_user_role(user) for user in users}

        return render_template(
            "user_management.html",
            users=users,
            departments=departments,
            stats=stats,
            role_cards=role_cards,
            role_counts_total=role_counts_total,
            role_counts_visible=role_counts_visible,
            filter_values=filter_values,
            active_filters=active_filters,
            clear_filters_url=clear_filters_url,
            export_url=export_url,
            department_overview=department_overview,
            department_lookup=department_lookup,
            role_meta=ROLE_META,
            role_lookup=role_lookup,
            sort_options=sort_options,
            contact_filter_options=contact_filter_options,
            contact_counts=contact_counts,
            insights=insights,
            contact_completion_rate=contact_completion_rate,
            admin_ratio=admin_ratio,
            admin_total=admin_total,
            top_departments=top_departments,
            view_mode=view_mode,
            view_modes=view_modes,
            missing_contact_url=missing_contact_url,
        )

    @app.route("/system/benutzer/export")
    @admin_required
    def export_users() -> Response:
        """Exportiert die aktuelle Benutzerliste als CSV."""

        current_user = get_current_user()

        base_query = Employee.query.filter(Employee.username.isnot(None))
        if current_user and current_user.department_id:
            base_query = base_query.filter(Employee.department_id == current_user.department_id)

        search_query = request.args.get("q", "").strip()
        role_filter = request.args.get("role", "all")
        department_filter = request.args.get("department", "all")
        sort_option = request.args.get("sort", "name_asc")
        contact_filter = request.args.get("contact", "all")

        if contact_filter not in {"all", "complete", "missing_email", "missing_phone", "incomplete"}:
            contact_filter = "all"

        filtered_query = _apply_user_filters(
            base_query,
            search_query=search_query,
            role=role_filter,
            department=department_filter,
            contact=contact_filter,
        )
        sorted_query = _apply_user_sort(filtered_query, sort_option)
        users = sorted_query.options(joinedload(Employee.department)).all()

        output = StringIO()
        writer = csv.writer(output, delimiter=";")
        writer.writerow(["Name", "Benutzername", "E-Mail", "Telefon", "Rolle", "Abteilung"])

        for user in users:
            role_key = _resolve_user_role(user)
            role_label = ROLE_LABELS.get(role_key, "Unbekannt")
            department_label = (
                user.department.name
                if user.department
                else ("Alle Abteilungen" if user.is_admin else "Keine Zuordnung")
            )

            writer.writerow(
                [
                    user.name,
                    user.username or "",
                    user.email or "",
                    user.phone or "",
                    role_label,
                    department_label,
                ]
            )

        csv_data = output.getvalue()
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"benutzer-{timestamp}.csv"

        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

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

        group_order_entries = EmployeeGroupOrder.query.order_by(EmployeeGroupOrder.order_position).all()
        available_groups = _get_available_group_names()

        order_mapping = {entry.group_name: entry.order_position for entry in group_order_entries}

        for entry in group_order_entries:
            if entry.group_name not in available_groups:
                available_groups.append(entry.group_name)

        sorted_groups = sorted(
            available_groups,
            key=lambda name: (order_mapping.get(name, len(order_mapping)), name.lower()),
        )

        return jsonify(
            {
                "groups": [
                    {"name": name, "position": index}
                    for index, name in enumerate(sorted_groups)
                ]
            }
        )

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
            data = request.get_json() or {}
            groups = data.get('groups', [])

            available_groups = _get_available_group_names()
            if not available_groups:
                return jsonify({'error': 'Keine Gruppen verfügbar.'}), 400

            sanitized_groups: List[str] = []
            seen: set[str] = set()

            for group in groups:
                name = (group or {}).get('name')
                if not name:
                    continue
                if name in available_groups and name not in seen:
                    sanitized_groups.append(name)
                    seen.add(name)

            for name in available_groups:
                if name not in seen:
                    sanitized_groups.append(name)

            if not sanitized_groups:
                sanitized_groups = available_groups

            # Lösche alte Reihenfolge
            EmployeeGroupOrder.query.delete()

            # Speichere neue Reihenfolge
            for index, name in enumerate(sanitized_groups):
                group_order = EmployeeGroupOrder(
                    group_name=name,
                    order_position=index,
                )
                db.session.add(group_order)
            
            db.session.commit()
            
            return jsonify({'success': True, 'message': 'Reihenfolge erfolgreich aktualisiert.'})
        
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': f'Fehler beim Aktualisieren: {str(e)}'}), 500

    _start_automation_worker(app)

    return app

if __name__ == "__main__":
    app = create_app()
    app.run(debug=True, host='0.0.0.0', port=5001)

