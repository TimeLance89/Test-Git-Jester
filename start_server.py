#!/usr/bin/env python3
"""
Employee Planner - Quick Start Script
Einfacher Start-Script für den Employee Planner Server
"""

import os
import sys
import subprocess
import time

def main():
    """Hauptfunktion für den Server-Start"""
    
    print("🏢 Employee Planner - Quick Start")
    print("=" * 40)
    
    # Prüfen ob wir im richtigen Verzeichnis sind
    if not os.path.exists("app.py"):
        print("❌ Fehler: app.py nicht gefunden!")
        print("   Bitte führen Sie dieses Script im Employee Planner Verzeichnis aus.")
        return 1
    
    # Prüfen ob server_gui.py existiert
    if os.path.exists("server_gui.py"):
        print("🖥️  Server-GUI verfügbar!")
        choice = input("Möchten Sie die grafische Oberfläche verwenden? (j/n): ").lower().strip()
        
        if choice in ['j', 'ja', 'y', 'yes', '']:
            print("🚀 Starte Server-GUI...")
            try:
                subprocess.run([sys.executable, "server_gui.py"])
                return 0
            except KeyboardInterrupt:
                print("\n👋 Server-GUI beendet.")
                return 0
            except Exception as e:
                print(f"❌ Fehler beim Starten der GUI: {e}")
                print("🔄 Fallback auf Kommandozeilen-Modus...")
    
    # Kommandozeilen-Modus
    print("💻 Starte Server im Kommandozeilen-Modus...")
    print(f"🌐 Server wird verfügbar sein unter: http://localhost:5001")
    
    # Lokale IP ermitteln
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        print(f"🌍 Netzwerk-Zugriff unter: http://{local_ip}:5001")
    except:
        pass
    
    print("\n🔧 Zum Beenden: Strg+C drücken")
    print("-" * 40)
    
    try:
        # Server starten
        subprocess.run([sys.executable, "app.py"])
    except KeyboardInterrupt:
        print("\n👋 Server beendet.")
        return 0
    except Exception as e:
        print(f"❌ Fehler beim Starten des Servers: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
