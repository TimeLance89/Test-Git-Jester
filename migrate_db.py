"""Datenbank-Migrationsskript für neue EmployeeGroupOrder Tabelle."""

import sqlite3
from datetime import date

def migrate_database():
    """Fügt die neue employee_group_order Tabelle hinzu."""
    db_file = "planner.db"
    
    try:
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
        
        # Prüfe ob Tabelle bereits existiert
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='employee_group_order'
        """)
        
        if cursor.fetchone():
            print("✓ Tabelle 'employee_group_order' existiert bereits.")
        else:
            # Erstelle neue Tabelle
            cursor.execute("""
                CREATE TABLE employee_group_order (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_name VARCHAR(50) NOT NULL UNIQUE,
                    order_position INTEGER NOT NULL,
                    created_date DATE NOT NULL,
                    updated_date DATE NOT NULL
                )
            """)
            
            # Füge Standard-Reihenfolge ein
            today = date.today().isoformat()
            default_order = [
                ('Vollzeit', 0, today, today),
                ('Teilzeit', 1, today, today),
                ('Aushilfe', 2, today, today)
            ]
            
            cursor.executemany("""
                INSERT INTO employee_group_order 
                (group_name, order_position, created_date, updated_date)
                VALUES (?, ?, ?, ?)
            """, default_order)
            
            conn.commit()
            print("✓ Tabelle 'employee_group_order' erfolgreich erstellt.")
            print("✓ Standard-Reihenfolge (Vollzeit, Teilzeit, Aushilfe) eingetragen.")
        
        conn.close()
        print("\n✅ Datenbank-Migration erfolgreich abgeschlossen!")
        
    except Exception as e:
        print(f"❌ Fehler bei der Migration: {str(e)}")
        if 'conn' in locals():
            conn.close()

if __name__ == "__main__":
    migrate_database()
