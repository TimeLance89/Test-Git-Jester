#!/usr/bin/env python3
"""
Employee Planner Server GUI
Moderne TKINTER-Oberfläche zur Verwaltung des Employee Planner Servers
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import subprocess
import threading
import time
import requests
import socket
import sys
import os
from datetime import datetime
import webbrowser

class EmployeePlannerServerGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Employee Planner Server Manager")
        self.root.geometry("800x900")
        self.root.minsize(700, 500)

        # Grundfarben & Layout
        self.colors = {
            'background': '#eef2ff',
            'surface': '#ffffff',
            'primary': '#2563eb',
            'primary_hover': '#1d4ed8',
            'primary_active': '#1e40af',
            'success': '#16a34a',
            'success_hover': '#15803d',
            'success_active': '#166534',
            'danger': '#dc2626',
            'danger_hover': '#b91c1c',
            'danger_active': '#991b1b',
            'warning': '#d97706',
            'warning_hover': '#b45309',
            'warning_active': '#92400e',
            'secondary': '#475569',
            'secondary_hover': '#334155',
            'secondary_active': '#1e293b',
            'text': '#0f172a',
            'text_muted': '#64748b',
            'border': '#e2e8f0',
            'disabled_bg': '#e2e8f0',
            'disabled_fg': '#94a3b8',
            'log_bg': '#0f172a',
            'log_fg': '#e2e8f0'
        }

        self.root.configure(bg=self.colors['background'])

        # Server-Prozess
        self.server_process = None
        self.server_running = False
        self.server_url = "http://localhost:5001"

        self.setup_styles()
        self.create_widgets()
        self.update_status()
        
        # Automatische Status-Updates
        self.status_update_job()
        
        # Beim Schließen Server beenden
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
    
    def setup_styles(self):
        """Konfiguriert moderne Styles für ttk Widgets"""
        style = ttk.Style()

        # Modernes Theme nutzen
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass

        style.configure('.', font=('Segoe UI', 10))
        style.configure('Background.TFrame', background=self.colors['background'])
        style.configure('Surface.TFrame', background=self.colors['surface'], borderwidth=0)
        style.configure('Card.TFrame', background=self.colors['surface'])
        try:
            style.configure('Card.TLabelframe',
                            background=self.colors['surface'],
                            borderwidth=1,
                            relief='solid',
                            bordercolor=self.colors['border'],
                            darkcolor=self.colors['border'],
                            lightcolor=self.colors['border'])
        except tk.TclError:
            style.configure('Card.TLabelframe',
                            background=self.colors['surface'],
                            borderwidth=1,
                            relief='solid')
        style.configure('Card.TLabelframe.Label',
                        background=self.colors['surface'],
                        foreground=self.colors['text_muted'],
                        font=('Segoe UI', 10, 'bold'))
        style.configure('TLabel',
                        background=self.colors['surface'],
                        foreground=self.colors['text'])
        style.configure('Muted.TLabel',
                        background=self.colors['surface'],
                        foreground=self.colors['text_muted'])
        style.configure('Background.TLabel',
                        background=self.colors['background'],
                        foreground=self.colors['text'])
        style.configure('Footer.TLabel',
                        background=self.colors['surface'],
                        foreground=self.colors['text_muted'])
        style.configure('Title.TLabel',
                        background=self.colors['surface'],
                        foreground=self.colors['text'])
        style.configure('Link.TLabel',
                        background=self.colors['surface'],
                        foreground=self.colors['primary'])

        # Moderne Button-Styles
        button_padding = (20, 10)
        button_font = ('Segoe UI', 10, 'bold')

        style.configure('Primary.TButton',
                        background=self.colors['primary'],
                        foreground='white',
                        padding=button_padding,
                        font=button_font)
        style.configure('Success.TButton',
                        background=self.colors['success'],
                        foreground='white',
                        padding=button_padding,
                        font=button_font)
        style.configure('Danger.TButton',
                        background=self.colors['danger'],
                        foreground='white',
                        padding=button_padding,
                        font=button_font)
        style.configure('Warning.TButton',
                        background=self.colors['warning'],
                        foreground='white',
                        padding=button_padding,
                        font=button_font)
        style.configure('Secondary.TButton',
                        background=self.colors['secondary'],
                        foreground='white',
                        padding=button_padding,
                        font=button_font)

        # Konsistente Farben für verschiedene Button-Zustände
        style.map('Primary.TButton',
                  background=[('disabled', self.colors['disabled_bg']),
                              ('pressed', self.colors['primary_active']),
                              ('active', self.colors['primary_hover'])],
                  foreground=[('disabled', self.colors['disabled_fg'])])
        style.map('Success.TButton',
                  background=[('disabled', self.colors['disabled_bg']),
                              ('pressed', self.colors['success_active']),
                              ('active', self.colors['success_hover'])],
                  foreground=[('disabled', self.colors['disabled_fg'])])
        style.map('Danger.TButton',
                  background=[('disabled', self.colors['disabled_bg']),
                              ('pressed', self.colors['danger_active']),
                              ('active', self.colors['danger_hover'])],
                  foreground=[('disabled', self.colors['disabled_fg'])])
        style.map('Warning.TButton',
                  background=[('disabled', self.colors['disabled_bg']),
                              ('pressed', self.colors['warning_active']),
                              ('active', self.colors['warning_hover'])],
                  foreground=[('disabled', self.colors['disabled_fg'])])
        style.map('Secondary.TButton',
                  background=[('disabled', self.colors['disabled_bg']),
                              ('pressed', self.colors['secondary_active']),
                              ('active', self.colors['secondary_hover'])],
                  foreground=[('disabled', self.colors['disabled_fg'])])

    def create_widgets(self):
        """Erstellt die Benutzeroberfläche"""

        # Hauptcontainer mit Padding
        main_frame = ttk.Frame(self.root, padding="20", style='Surface.TFrame')
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        # Header
        self.create_header(main_frame)
        
        # Status-Bereich
        self.create_status_section(main_frame)
        
        # Control-Buttons
        self.create_control_section(main_frame)
        
        # Server-Info
        self.create_info_section(main_frame)
        
        # Log-Bereich
        self.create_log_section(main_frame)
        
        # Footer
        self.create_footer(main_frame)
        
        # Grid-Konfiguration
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(4, weight=1)  # Log-Bereich soll expandieren
    
    def create_header(self, parent):
        """Erstellt den Header-Bereich"""
        header_frame = ttk.Frame(parent, style='Surface.TFrame')
        header_frame.grid(row=0, column=0, sticky=(tk.W, tk.E), pady=(0, 20))

        # Titel
        title_label = ttk.Label(header_frame,
                                text="🏢 Employee Planner Server Manager",
                                style='Title.TLabel',
                                font=('Segoe UI', 18, 'bold'))
        title_label.grid(row=0, column=0, sticky=tk.W)

        # Untertitel
        subtitle_label = ttk.Label(header_frame,
                                  text="Moderne Verwaltung für Ihren Dienstplan-Server",
                                  font=('Segoe UI', 10),
                                  style='Muted.TLabel')
        subtitle_label.grid(row=1, column=0, sticky=tk.W, pady=(5, 0))

    def create_status_section(self, parent):
        """Erstellt den Status-Bereich"""
        status_frame = ttk.LabelFrame(parent,
                                      text="📊 Server Status",
                                      padding="15",
                                      style='Card.TLabelframe')
        status_frame.grid(row=1, column=0, sticky=(tk.W, tk.E), pady=(0, 15))

        # Status-Indikator
        status_container = ttk.Frame(status_frame, style='Card.TFrame')
        status_container.grid(row=0, column=0, sticky=(tk.W, tk.E))

        self.status_indicator = tk.Label(status_container,
                                         text="●",
                                         font=('Segoe UI', 20),
                                         fg=self.colors['danger'],
                                         bg=self.colors['surface'])
        self.status_indicator.grid(row=0, column=0, padx=(0, 10))
        
        self.status_label = ttk.Label(status_container,
                                     text="Server gestoppt",
                                     font=('Segoe UI', 12, 'bold'))
        self.status_label.grid(row=0, column=1)

        # Server-URL
        self.url_label = ttk.Label(status_frame,
                                  text=f"URL: {self.server_url}",
                                  font=('Segoe UI', 10),
                                  style='Muted.TLabel')
        self.url_label.grid(row=1, column=0, sticky=tk.W, pady=(10, 0))

        # Netzwerk-Info
        self.network_label = ttk.Label(status_frame,
                                      text="Netzwerk: Nicht verfügbar",
                                      font=('Segoe UI', 10),
                                      style='Muted.TLabel')
        self.network_label.grid(row=2, column=0, sticky=tk.W, pady=(5, 0))

        status_frame.columnconfigure(0, weight=1)

    def create_control_section(self, parent):
        """Erstellt die Control-Buttons"""
        control_frame = ttk.LabelFrame(parent,
                                       text="🎮 Server Steuerung",
                                       padding="15",
                                       style='Card.TLabelframe')
        control_frame.grid(row=2, column=0, sticky=(tk.W, tk.E), pady=(0, 15))

        # Button-Container
        button_frame = ttk.Frame(control_frame, style='Card.TFrame')
        button_frame.grid(row=0, column=0, sticky=(tk.W, tk.E))

        # Start Button
        self.start_button = ttk.Button(button_frame,
                                      text="🚀 Server starten",
                                      style='Success.TButton',
                                      command=self.start_server)
        self.start_button.grid(row=0, column=0, padx=(0, 10))
        
        # Neustart Button
        self.restart_button = ttk.Button(button_frame,
                                        text="🔄 Neustart",
                                        style='Warning.TButton',
                                        command=self.restart_server,
                                        state='disabled')
        self.restart_button.grid(row=0, column=1, padx=(0, 10))
        
        # Stop Button
        self.stop_button = ttk.Button(button_frame,
                                     text="⏹️ Server stoppen",
                                     style='Danger.TButton',
                                     command=self.stop_server,
                                     state='disabled')
        self.stop_button.grid(row=0, column=2, padx=(0, 10))
        
        # Browser öffnen Button
        self.browser_button = ttk.Button(button_frame,
                                        text="🌐 Im Browser öffnen",
                                        style='Primary.TButton',
                                        command=self.open_browser,
                                        state='disabled')
        self.browser_button.grid(row=0, column=3)
        
        button_frame.columnconfigure((0, 1, 2, 3), weight=1)
        control_frame.columnconfigure(0, weight=1)

    def create_info_section(self, parent):
        """Erstellt den Info-Bereich"""
        info_frame = ttk.LabelFrame(parent,
                                    text="ℹ️ Server Information",
                                    padding="15",
                                    style='Card.TLabelframe')
        info_frame.grid(row=3, column=0, sticky=(tk.W, tk.E), pady=(0, 15))

        # Info-Grid
        info_container = ttk.Frame(info_frame, style='Card.TFrame')
        info_container.grid(row=0, column=0, sticky=(tk.W, tk.E))

        # Port
        ttk.Label(info_container, text="Port:", font=('Segoe UI', 10, 'bold')).grid(row=0, column=0, sticky=tk.W)
        ttk.Label(info_container, text="5001", font=('Segoe UI', 10)).grid(row=0, column=1, sticky=tk.W, padx=(10, 0))
        
        # Host
        ttk.Label(info_container, text="Host:", font=('Segoe UI', 10, 'bold')).grid(row=0, column=2, sticky=tk.W, padx=(30, 0))
        ttk.Label(info_container, text="0.0.0.0 (Alle Netzwerke)", font=('Segoe UI', 10)).grid(row=0, column=3, sticky=tk.W, padx=(10, 0))
        
        # Lokale IP
        local_ip = self.get_local_ip()
        ttk.Label(info_container, text="Lokale IP:", font=('Segoe UI', 10, 'bold')).grid(row=1, column=0, sticky=tk.W, pady=(10, 0))
        ttk.Label(info_container, text=local_ip, font=('Segoe UI', 10)).grid(row=1, column=1, sticky=tk.W, padx=(10, 0), pady=(10, 0))
        
        # Netzwerk-URL
        network_url = f"http://{local_ip}:5001"
        ttk.Label(info_container, text="Netzwerk-URL:", font=('Segoe UI', 10, 'bold')).grid(row=1, column=2, sticky=tk.W, padx=(30, 0), pady=(10, 0))
        self.network_url_label = ttk.Label(info_container,
                                           text=network_url,
                                           font=('Segoe UI', 10),
                                           style='Link.TLabel')
        self.network_url_label.grid(row=1, column=3, sticky=tk.W, padx=(10, 0), pady=(10, 0))

        info_frame.columnconfigure(0, weight=1)

    def create_log_section(self, parent):
        """Erstellt den Log-Bereich"""
        log_frame = ttk.LabelFrame(parent,
                                   text="📋 Server Logs",
                                   padding="15",
                                   style='Card.TLabelframe')
        log_frame.grid(row=4, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), pady=(0, 15))

        # Log-Text mit Scrollbar
        self.log_text = scrolledtext.ScrolledText(log_frame,
                                                 height=12,
                                                 font=('Consolas', 9),
                                                 bg=self.colors['log_bg'],
                                                 fg=self.colors['log_fg'],
                                                 insertbackground=self.colors['log_fg'],
                                                 borderwidth=0,
                                                 relief='flat')
        self.log_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        # Clear Log Button
        clear_button = ttk.Button(log_frame,
                                  text="🗑️ Logs löschen",
                                  command=self.clear_logs,
                                  style='Secondary.TButton')
        clear_button.grid(row=1, column=0, sticky=tk.E, pady=(10, 0))

        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        # Willkommensnachricht
        self.log("🎉 Employee Planner Server Manager gestartet")
        self.log(f"📍 Arbeitsverzeichnis: {os.getcwd()}")
    
    def create_footer(self, parent):
        """Erstellt den Footer"""
        footer_frame = ttk.Frame(parent, style='Surface.TFrame')
        footer_frame.grid(row=5, column=0, sticky=(tk.W, tk.E))

        footer_label = ttk.Label(footer_frame,
                                text="Employee Planner Server Manager v1.0 | (C) Steffen Ruh",
                                font=('Segoe UI', 8),
                                style='Footer.TLabel')
        footer_label.grid(row=0, column=0)

        # Zeit-Label
        self.time_label = ttk.Label(footer_frame,
                                   text="",
                                   font=('Segoe UI', 8),
                                   style='Footer.TLabel')
        self.time_label.grid(row=0, column=1, sticky=tk.E)

        footer_frame.columnconfigure(1, weight=1)

        # Zeit aktualisieren
        self.update_time()
    
    def get_local_ip(self):
        """Ermittelt die lokale IP-Adresse"""
        try:
            # Verbindung zu Google DNS um lokale IP zu ermitteln
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "127.0.0.1"
    
    def log(self, message):
        """Fügt eine Nachricht zum Log hinzu"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_message = f"[{timestamp}] {message}"
        
        self.log_text.insert(tk.END, log_message + "\n")
        self.log_text.see(tk.END)
        
        # Automatisches Scrollen
        self.root.update_idletasks()
    
    def clear_logs(self):
        """Löscht alle Log-Einträge"""
        self.log_text.delete(1.0, tk.END)
        self.log("🗑️ Logs gelöscht")
    
    def start_server(self):
        """Startet den Flask-Server"""
        if self.server_running:
            return

        try:
            self.log("🚀 Starte Employee Planner Server...")
            self.start_button.config(state='disabled')
            self.restart_button.config(state='disabled')
            self.stop_button.config(state='disabled')
            self.browser_button.config(state='disabled')

            # Server in separatem Thread starten
            def run_server():
                try:
                    # CREATE_NO_WINDOW Flag für Windows um Konsole zu verstecken
                    startupinfo = None
                    if sys.platform == 'win32':
                        startupinfo = subprocess.STARTUPINFO()
                        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                        startupinfo.wShowWindow = subprocess.SW_HIDE
                    
                    self.server_process = subprocess.Popen(
                        [sys.executable, "app.py"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        universal_newlines=True,
                        bufsize=1,
                        startupinfo=startupinfo,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                    )
                    
                    # Server-Output in Log anzeigen
                    for line in iter(self.server_process.stdout.readline, ''):
                        if line.strip():
                            self.root.after(0, lambda l=line.strip(): self.log(f"📡 {l}"))
                    
                except Exception as e:
                    self.root.after(0, lambda: self.log(f"❌ Server-Fehler: {e}"))
            
            server_thread = threading.Thread(target=run_server, daemon=True)
            server_thread.start()
            
            # Warten bis Server bereit ist
            self.root.after(2000, self.check_server_ready)
            
        except Exception as e:
            self.log(f"❌ Fehler beim Starten: {e}")
            messagebox.showerror("Fehler", f"Server konnte nicht gestartet werden:\n{e}")
            self.update_button_states()
    
    def check_server_ready(self):
        """Überprüft ob der Server bereit ist"""
        try:
            response = requests.get(self.server_url, timeout=2)
            if response.status_code == 200:
                self.server_running = True
                self.log("✅ Server erfolgreich gestartet und bereit!")
                self.log(f"🌐 Erreichbar unter: {self.server_url}")
                local_ip = self.get_local_ip()
                self.log(f"🌍 Netzwerk-Zugriff: http://{local_ip}:5001")
                self.update_button_states()
            else:
                self.root.after(1000, self.check_server_ready)
        except:
            self.root.after(1000, self.check_server_ready)
    
    def stop_server(self):
        """Stoppt den Flask-Server"""
        if not self.server_running and not self.server_process:
            return

        try:
            self.log("⏹️ Stoppe Employee Planner Server...")

            process = self.server_process
            if process:
                # Versuche zuerst graceful shutdown
                process.terminate()

                try:
                    # Warte auf Prozess-Ende
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # Falls terminate nicht funktioniert, kill verwenden
                    self.log("⚠️ Server reagiert nicht, erzwinge Beendigung...")
                    process.kill()
                    process.wait()

                # Auf Windows: Töte auch alle Kind-Prozesse (Flask Reloader)
                if sys.platform == 'win32':
                    try:
                        import psutil
                        parent = psutil.Process(process.pid)
                        for child in parent.children(recursive=True):
                            child.kill()
                    except:
                        pass  # psutil nicht verfügbar oder Prozess bereits beendet

                if process.stdout:
                    try:
                        process.stdout.close()
                    except Exception:
                        pass

                self.server_process = None

            self.server_running = False
            self.log("✅ Server erfolgreich gestoppt")
            self.update_button_states()

        except Exception as e:
            self.log(f"❌ Fehler beim Stoppen: {e}")
            messagebox.showerror("Fehler", f"Server konnte nicht gestoppt werden:\n{e}")

    def restart_server(self):
        """Startet den Server neu"""
        self.log("🔄 Starte Server neu...")
        self.stop_server()
        self.root.after(2000, self.start_server)
    
    def open_browser(self):
        """Öffnet den Server im Browser"""
        if self.server_running:
            self.log("🌐 Öffne Employee Planner im Browser...")
            webbrowser.open(self.server_url)
        else:
            messagebox.showwarning("Server nicht aktiv", "Der Server muss zuerst gestartet werden.")
    
    def update_button_states(self):
        """Aktualisiert den Zustand der Buttons"""
        if self.server_running:
            self.start_button.config(state='disabled')
            self.restart_button.config(state='normal')
            self.stop_button.config(state='normal')
            self.browser_button.config(state='normal')
        else:
            self.start_button.config(state='normal')
            self.restart_button.config(state='disabled')
            self.stop_button.config(state='disabled')
            self.browser_button.config(state='disabled')
    
    def update_status(self):
        """Aktualisiert die Status-Anzeige"""
        if self.server_running:
            self.status_indicator.config(fg=self.colors['success'])
            self.status_label.config(text="Server läuft")
            local_ip = self.get_local_ip()
            self.network_label.config(text=f"Netzwerk: http://{local_ip}:5001")
        else:
            self.status_indicator.config(fg=self.colors['danger'])
            self.status_label.config(text="Server gestoppt")
            self.network_label.config(text="Netzwerk: Nicht verfügbar")
    
    def status_update_job(self):
        """Regelmäßige Status-Updates"""
        self.update_status()
        self.root.after(5000, self.status_update_job)  # Alle 5 Sekunden
    
    def update_time(self):
        """Aktualisiert die Zeitanzeige"""
        current_time = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        self.time_label.config(text=current_time)
        self.root.after(1000, self.update_time)  # Jede Sekunde
    
    def on_closing(self):
        """Wird beim Schließen des Fensters aufgerufen"""
        if self.server_running or self.server_process:
            # Server automatisch stoppen ohne Nachfrage
            self.log("🔴 GUI wird geschlossen, stoppe Server...")
            self.stop_server()
            # Kurz warten damit Server sauber beendet wird
            self.root.after(500, self.root.destroy)
        else:
            self.root.destroy()
    
    def run(self):
        """Startet die GUI"""
        self.root.mainloop()

def main():
    """Hauptfunktion"""
    # Prüfen ob app.py existiert
    if not os.path.exists("app.py"):
        messagebox.showerror("Fehler", 
                           "app.py nicht gefunden!\n\n"
                           "Bitte starten Sie den Server Manager im Employee Planner Verzeichnis.")
        return
    
    # GUI starten
    app = EmployeePlannerServerGUI()
    app.run()

if __name__ == "__main__":
    main()
