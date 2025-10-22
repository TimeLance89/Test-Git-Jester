from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REQUIREMENTS_FILE = BASE_DIR / "requirements.txt"
MIN_PYTHON = (3, 9)


def check_python_version() -> None:
    """Stellt sicher, dass eine unterstützte Python-Version verwendet wird."""
    if sys.version_info < MIN_PYTHON:
        version = ".".join(map(str, sys.version_info[:3]))
        required = ".".join(map(str, MIN_PYTHON))
        print(
            f"❌ Python {version} wird nicht unterstützt. Bitte verwenden Sie mindestens Version {required}."
        )
        sys.exit(1)


def ensure_requirements_file() -> None:
    """Prüft, ob die requirements.txt verfügbar ist."""
    if not REQUIREMENTS_FILE.exists():
        print(
            f"❌ Die Datei '{REQUIREMENTS_FILE}' wurde nicht gefunden. "
            "Bitte stellen Sie sicher, dass das Script im Projektverzeichnis ausgeführt wird."
        )
        sys.exit(1)


def run_step(description: str, command: list[str]) -> None:
    """Führt einen Shell-Befehl aus und beendet das Script bei Fehlern."""
    print(f"\n➡️  {description}")
    print(f"   Befehl: {' '.join(command)}")
    try:
        subprocess.run(command, cwd=BASE_DIR, check=True)
        print("   ✅ Erfolgreich abgeschlossen")
    except subprocess.CalledProcessError as exc:
        print(
            f"   ❌ Fehler beim Ausführen des Befehls (Exitcode {exc.returncode})."
        )
        sys.exit(exc.returncode or 1)


def install_python_packages() -> None:
    """Installiert alle Python-Abhängigkeiten."""
    python_executable = sys.executable

    run_step(
        "Aktualisiere pip, setuptools und wheel",
        [python_executable, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"],
    )

    run_step(
        "Installiere Projekt-Abhängigkeiten",
        [python_executable, "-m", "pip", "install", "-r", str(REQUIREMENTS_FILE)],
    )


def initialize_database() -> None:
    """Initialisiert die Datenbank und führt notwendige Migrationen aus."""
    python_executable = sys.executable

    run_step(
        "Initialisiere Datenbank (Standardwerte & Admin-Benutzer)",
        [python_executable, "init_db.py"],
    )

    migrate_script = BASE_DIR / "migrate_db.py"
    if migrate_script.exists():
        run_step(
            "Führe optionale Datenbank-Migrationen aus",
            [python_executable, str(migrate_script)],
        )


def main() -> None:
    print("🏗️  Starte vollständige Installation des Employee Planner Projekts")
    check_python_version()
    ensure_requirements_file()
    install_python_packages()
    initialize_database()

    print("\n🎉 Installation abgeschlossen!")
    print("Sie können den Server jetzt direkt mit folgendem Befehl starten:")
    print(f"   {sys.executable} start_server.py")
    print("\nAlternativ können Sie den Server auch direkt mit 'python app.py' starten.")


if __name__ == "__main__":
    main()
