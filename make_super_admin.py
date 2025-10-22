#!/usr/bin/env python3
"""
Super-Administrator Upgrade Tool
Dieses Script macht einen Benutzer zum Super-Administrator mit Vollzugriff auf alle Abteilungen.
"""

import sys
import os
from models import db, Employee

def create_app():
    """Erstellt Flask-App für Datenbankzugriff."""
    from flask import Flask
    import os
    
    app = Flask(__name__)
    
    # Absoluter Pfad zur Datenbank
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "planner.db")
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app

def list_users():
    """Zeigt alle Benutzer mit Admin-Status an."""
    users = Employee.query.filter(Employee.username.isnot(None)).all()
    
    print("\n📋 Alle Benutzer im System:")
    print("-" * 80)
    print(f"{'ID':<4} {'Name':<20} {'Benutzername':<15} {'Admin':<8} {'Abteilung':<12} {'Status'}")
    print("-" * 80)
    
    for user in users:
        admin_status = "✅ Ja" if user.is_admin else "❌ Nein"
        dept_name = user.department.name if user.department else "Alle"
        
        if user.is_admin and not user.department_id:
            status = "🔥 Super-Admin"
        elif user.is_admin and user.department_id:
            status = "🏢 Abteilungs-Admin"
        else:
            status = "👤 Mitarbeiter"
            
        print(f"{user.id:<4} {user.name:<20} {user.username:<15} {admin_status:<8} {dept_name:<12} {status}")
    
    print("-" * 80)

def make_super_admin(identifier, by_type="username"):
    """Macht einen Benutzer zum Super-Administrator."""
    
    # Benutzer finden
    if by_type == "username":
        user = Employee.query.filter_by(username=identifier).first()
    elif by_type == "id":
        user = Employee.query.get(int(identifier))
    elif by_type == "email":
        user = Employee.query.filter_by(email=identifier).first()
    else:
        print("❌ Ungültiger Suchtyp. Verwenden Sie: username, id, oder email")
        return False
    
    if not user:
        print(f"❌ Benutzer '{identifier}' nicht gefunden.")
        return False
    
    # Status vor der Änderung
    old_status = "Super-Admin" if (user.is_admin and not user.department_id) else \
                 "Abteilungs-Admin" if user.is_admin else "Mitarbeiter"
    
    # Zum Super-Admin machen
    user.is_admin = True
    user.department_id = None  # Vollzugriff auf alle Abteilungen
    
    try:
        db.session.commit()
        print(f"✅ Erfolgreich! {user.name} ({user.username}) ist jetzt Super-Administrator.")
        print(f"   Status geändert: {old_status} → Super-Admin")
        print(f"   Vollzugriff auf alle Abteilungen gewährt.")
        return True
    except Exception as e:
        db.session.rollback()
        print(f"❌ Fehler beim Speichern: {e}")
        return False

def main():
    """Hauptfunktion des Scripts."""
    app = create_app()
    
    with app.app_context():
        print("🔧 Super-Administrator Upgrade Tool")
        print("=" * 50)
        
        if len(sys.argv) == 1:
            # Keine Argumente - interaktiver Modus
            list_users()
            print("\n🎯 Benutzer zum Super-Administrator machen:")
            print("Geben Sie den Benutzernamen, die ID oder E-Mail ein:")
            
            identifier = input("Eingabe: ").strip()
            if not identifier:
                print("❌ Keine Eingabe erhalten.")
                return
            
            # Automatisch erkennen ob ID, E-Mail oder Benutzername
            if identifier.isdigit():
                success = make_super_admin(identifier, "id")
            elif "@" in identifier:
                success = make_super_admin(identifier, "email")
            else:
                success = make_super_admin(identifier, "username")
            
            if success:
                print("\n🎉 Upgrade abgeschlossen! Sie können sich jetzt mit Vollzugriff anmelden.")
        
        elif len(sys.argv) == 2:
            # Ein Argument - direkter Modus
            identifier = sys.argv[1]
            
            if identifier == "--list":
                list_users()
            else:
                # Automatisch erkennen
                if identifier.isdigit():
                    make_super_admin(identifier, "id")
                elif "@" in identifier:
                    make_super_admin(identifier, "email")
                else:
                    make_super_admin(identifier, "username")
        
        else:
            print("❌ Zu viele Argumente.")
            print("Verwendung:")
            print("  python3 make_super_admin.py                    # Interaktiver Modus")
            print("  python3 make_super_admin.py --list             # Alle Benutzer anzeigen")
            print("  python3 make_super_admin.py BENUTZERNAME       # Direkter Upgrade")
            print("  python3 make_super_admin.py user@domain.de     # Upgrade über E-Mail")
            print("  python3 make_super_admin.py 1                  # Upgrade über ID")

if __name__ == "__main__":
    main()
