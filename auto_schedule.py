"""Automatische Schichtenerstellung basierend auf Standard-Arbeitszeiten.

Dieses Modul stellt Funktionen zur Verfügung, um automatisch Schichten
für Mitarbeiter basierend auf ihren hinterlegten Standard-Arbeitszeiten
zu erstellen.
"""

import calendar
from datetime import date, timedelta
from typing import Optional

from models import db, Employee, Shift


def create_default_shifts_for_month(
    year: int,
    month: int,
    employee_id: Optional[int] = None,
    dry_run: bool = False,
    department_id: Optional[int] = None,
):
    """Erstellt Standard-Schichten für einen Monat basierend auf den Mitarbeiter-Einstellungen.
    
    Args:
        year: Jahr für die Schichtenerstellung
        month: Monat für die Schichtenerstellung
        employee_id: Optional - nur für einen bestimmten Mitarbeiter, sonst für alle
        dry_run: Wenn True, werden keine Änderungen in der Datenbank vorgenommen
    
    Returns:
        Dict mit Informationen über erstellte Schichten
    """
    # Berechne die Tage des Monats
    num_days = calendar.monthrange(year, month)[1]
    month_start = date(year, month, 1)
    month_end = date(year, month, num_days)
    
    # Hole Mitarbeiter
    employee_query = Employee.query

    if employee_id:
        employee_query = employee_query.filter(Employee.id == employee_id)
    else:
        employee_query = employee_query.filter(
            Employee.default_daily_hours.isnot(None),
            Employee.default_work_days.isnot(None)
        )

    if department_id:
        employee_query = employee_query.filter(Employee.department_id == department_id)

    employees = employee_query.all()
    
    created_shifts = []
    skipped_shifts = []
    
    for employee in employees:
        if not employee.default_daily_hours or not employee.default_work_days:
            continue
            
        # Parse Arbeitstage (0=Montag, 6=Sonntag)
        work_days = [int(day) for day in employee.default_work_days.split(',') if day.strip()]
        
        # Durchlaufe alle Tage des Monats
        current_date = month_start
        while current_date <= month_end:
            # Prüfe ob es ein Arbeitstag ist (0=Montag, 6=Sonntag)
            if current_date.weekday() in work_days:
                # Prüfe ob bereits eine Schicht für diesen Tag existiert
                existing_shift = Shift.query.filter_by(
                    employee_id=employee.id,
                    date=current_date
                ).first()
                
                if not existing_shift:
                    shift_data = {
                        'employee_id': employee.id,
                        'employee_name': employee.name,
                        'date': current_date,
                        'hours': employee.default_daily_hours,
                        'shift_type': 'Standard'
                    }
                    
                    if not dry_run:
                        new_shift = Shift(
                            employee_id=employee.id,
                            date=current_date,
                            hours=employee.default_daily_hours,
                            shift_type='Standard',
                            approved=True  # Automatisch genehmigte Standard-Schichten
                        )
                        db.session.add(new_shift)
                        created_shifts.append(shift_data)
                    else:
                        created_shifts.append(shift_data)
                else:
                    skipped_shifts.append({
                        'employee_id': employee.id,
                        'employee_name': employee.name,
                        'date': current_date,
                        'reason': 'Schicht bereits vorhanden'
                    })
            
            current_date += timedelta(days=1)
    
    if not dry_run:
        db.session.commit()
    
    return {
        'created_shifts': created_shifts,
        'skipped_shifts': skipped_shifts,
        'total_created': len(created_shifts),
        'total_skipped': len(skipped_shifts)
    }


def create_default_shifts_for_employee_position(
    position: str,
    year: int,
    month: int,
    dry_run: bool = False,
    department_id: Optional[int] = None,
):
    """Erstellt Standard-Schichten für alle Mitarbeiter einer bestimmten Position.
    
    Args:
        position: Position der Mitarbeiter (z.B. "Vollzeit")
        year: Jahr für die Schichtenerstellung
        month: Monat für die Schichtenerstellung
        dry_run: Wenn True, werden keine Änderungen in der Datenbank vorgenommen
    
    Returns:
        Dict mit Informationen über erstellte Schichten
    """
    # Berechne die Tage des Monats
    num_days = calendar.monthrange(year, month)[1]
    month_start = date(year, month, 1)
    month_end = date(year, month, num_days)
    
    # Hole Mitarbeiter mit der angegebenen Position
    employees_query = Employee.query.filter_by(position=position)

    if department_id:
        employees_query = employees_query.filter(Employee.department_id == department_id)

    employees = employees_query.all()
    
    created_shifts = []
    skipped_shifts = []
    
    for employee in employees:
        # Für Vollzeit-Mitarbeiter: Standard 8 Stunden, Montag bis Freitag
        if position == "Vollzeit":
            default_hours = 8.0
            work_days = [0, 1, 2, 3, 4]  # Montag bis Freitag
        else:
            # Für andere Positionen: Verwende hinterlegte Standard-Arbeitszeiten
            if not employee.default_daily_hours or not employee.default_work_days:
                continue
            default_hours = employee.default_daily_hours
            work_days = [int(day) for day in employee.default_work_days.split(',') if day.strip()]
        
        # Durchlaufe alle Tage des Monats
        current_date = month_start
        while current_date <= month_end:
            # Prüfe ob es ein Arbeitstag ist (0=Montag, 6=Sonntag)
            if current_date.weekday() in work_days:
                # Prüfe ob bereits eine Schicht für diesen Tag existiert
                existing_shift = Shift.query.filter_by(
                    employee_id=employee.id,
                    date=current_date
                ).first()
                
                if not existing_shift:
                    shift_data = {
                        'employee_id': employee.id,
                        'employee_name': employee.name,
                        'date': current_date,
                        'hours': default_hours,
                        'shift_type': f'Standard ({position})'
                    }
                    
                    if not dry_run:
                        new_shift = Shift(
                            employee_id=employee.id,
                            date=current_date,
                            hours=default_hours,
                            shift_type=f'Standard ({position})',
                            approved=True  # Automatisch genehmigte Standard-Schichten
                        )
                        db.session.add(new_shift)
                        created_shifts.append(shift_data)
                    else:
                        created_shifts.append(shift_data)
                else:
                    skipped_shifts.append({
                        'employee_id': employee.id,
                        'employee_name': employee.name,
                        'date': current_date,
                        'reason': 'Schicht bereits vorhanden'
                    })
            
            current_date += timedelta(days=1)
    
    if not dry_run:
        db.session.commit()
    
    return {
        'created_shifts': created_shifts,
        'skipped_shifts': skipped_shifts,
        'total_created': len(created_shifts),
        'total_skipped': len(skipped_shifts)
    }
