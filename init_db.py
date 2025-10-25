#!/usr/bin/env python3

from app import create_app
from sqlalchemy import func

from models import db, Employee, Department

def init_database():
    app = create_app()
    
    with app.app_context():
        # Create all tables
        db.create_all()
        
        if Department.query.count() == 0:
            default_department = Department(name='Administration', color='#2563eb', area='Verwaltung')
            db.session.add(default_department)
            db.session.commit()

        user_exists = (
            db.session.query(Employee.id)
            .filter(Employee.username.isnot(None))
            .filter(func.length(func.trim(Employee.username)) > 0)
            .first()
        )

        if user_exists:
            print("✓ Mindestens ein Benutzerkonto vorhanden – Setup kann übersprungen werden.")
        else:
            print(
                "ℹ️ Es wurde noch kein Benutzer angelegt. Starten Sie die Anwendung und rufen Sie "
                "die Setup-Seite unter /setup auf, um das erste Administrationskonto zu erstellen."
            )

        print("✓ Database initialized successfully")

if __name__ == "__main__":
    init_database()

