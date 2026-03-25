"""
DTF Monitor Agent — runs on each printer PC.
Watches a folder for PNG/TIFF files, reports to the central server,
and provides a local GUI for operators to mark files as printing.
"""

import os
import sys
import json
import time
import uuid
import re
import threading
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import filedialog, messagebox
from pathlib import Path
from html.parser import HTMLParser

from PIL import Image
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import requests

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "config.json")
SUPPORTED_EXTENSIONS = {".png", ".tiff", ".tif"}
DEFAULT_DPI = 300

# Allow large DTF print files without Pillow warnings
Image.MAX_IMAGE_PIXELS = None

# ── Colors (matching dashboard theme) ──
C = {
    "bg":       "#0A0A0B",
    "surface":  "#111113",
    "surface2": "#19191D",
    "surface3": "#222228",
    "border":   "#2A2A30",
    "border2":  "#3A3A42",
    "text":     "#F0EFE9",
    "text2":    "#9B9A94",
    "text3":    "#5A5956",
    "accent":   "#E8FF47",
    "accent2":  "#B5CC30",
    "green":    "#3DCF82",
    "green_bg": "#0D1F14",
    "green_bd": "#1A3D28",
    "red":      "#FF4D4D",
    "blue":     "#4A9EFF",
    "blue_bg":  "#0D1525",
    "orange":   "#FF7A35",
    "purple":   "#A78BFA",
    "purple_bg": "#1A1528",
    "purple_bd": "#2D2545",
}


# ── RIPLOG Parser ──

class RIPLogParser:
    """Parses FlexiPrint RIPLOG.HTML to extract RIP jobs and nest info."""

    # Regex to strip &nbsp; sequences
    _NBSP = re.compile(r'&nbsp;')
    # Regex to strip HTML tags
    _TAGS = re.compile(r'<[^>]+>')

    @staticmethod
    def _clean(text):
        """Remove HTML tags and &nbsp; from a value string."""
        text = RIPLogParser._NBSP.sub(' ', text)
        text = RIPLogParser._TAGS.sub('', text)
        return text.strip()

    @staticmethod
    def parse_file(filepath):
        """
        Parse RIPLOG.HTML and return all blocks in order.
        Each entry is a dict with a 'block_type' field: 'rip' or 'print'.

        "Start RIP Job" = RIP processing only (has full file path).
        "Start Printing" = actual file output (has filename or "Nest (x jobs)").
        """
        try:
            with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
        except Exception as e:
            print(f"Error reading RIPLOG: {e}")
            return []

        blocks = re.split(r'<BR><BR>', content)
        entries = []

        for block in blocks:
            if 'Start RIP Job' in block:
                job = RIPLogParser._parse_block(block, is_rip=True)
                if job:
                    job['block_type'] = 'rip'
                    entries.append(job)
            elif 'Start Printing' in block:
                job = RIPLogParser._parse_block(block, is_rip=False)
                if job:
                    job['block_type'] = 'print'
                    entries.append(job)

        return entries

    @staticmethod
    def _parse_block(block, is_rip=False):
        """Parse a single RIP Job or Printing table block into a dict."""
        job = {}

        # Extract all TH -> TD pairs
        pairs = re.findall(
            r'<TH[^>]*>(.*?)</TH>\s*<TD[^>]*>(.*?)</TD>',
            block,
            re.DOTALL | re.IGNORECASE
        )

        for th_raw, td_raw in pairs:
            key = RIPLogParser._clean(th_raw).rstrip(':')
            val = RIPLogParser._clean(td_raw)
            if not key:
                continue

            if key == 'File':
                job['file'] = val
            elif key == 'Device name':
                job['device'] = val
            elif key == 'File Size':
                job['file_size'] = val
            elif key == 'Sender':
                job['sender'] = val
            elif key == 'Job Type':
                job['job_type'] = val
            elif key == 'Dimensions':
                job['dimensions'] = val
            elif key == 'Resolution':
                job['resolution'] = val
            elif key == 'RIP Start Date and Time':
                job['rip_start'] = val
            elif key == 'RIP End Date and Time':
                job['rip_end'] = val
            elif key == 'RIP Duration':
                job['rip_duration'] = val
            elif key == 'Output Start Date And Time':
                job['output_start'] = val
            elif key == 'Output End Date And Time':
                job['output_end'] = val
            elif key == 'Info':
                if 'Job successfully done' in val:
                    job['success'] = True

        if not job.get('file'):
            return None

        # Parse nest info — file field looks like "Nest (2 jobs)"
        nest_match = re.match(r'^Nest\s*\((\d+)\s*jobs?\)', job['file'])
        if nest_match:
            job['is_nest'] = True
            job['nest_count'] = int(nest_match.group(1))
            job['file'] = f"Nest ({job['nest_count']} jobs)"
        else:
            job['is_nest'] = False
            job['nest_count'] = 0

        # Parse dimensions -> width_in, height_in
        dim_match = re.match(r'([\d.]+)\s*x\s*([\d.]+)\s*in', job.get('dimensions', ''))
        if dim_match:
            job['width_in'] = float(dim_match.group(1))
            job['height_in'] = float(dim_match.group(2))

        # Parse resolution
        res_match = re.match(r'([\d.]+)\s*x?\s*([\d.]+)', job.get('resolution', ''))
        if res_match:
            job['res_x'] = float(res_match.group(1))
            job['res_y'] = float(res_match.group(2))

        return job

    @staticmethod
    def _parse_timestamp(ts_str):
        """Parse RIPLOG timestamp like '5:20:35 PM 9/24/2025' into datetime."""
        if not ts_str:
            return None
        formats = [
            "%I:%M:%S %p %m/%d/%Y",   # 5:20:35 PM 9/24/2025
            "%m/%d/%Y %I:%M %p",       # 9/25/2025 4:18 AM
        ]
        for fmt in formats:
            try:
                return datetime.strptime(ts_str.strip(), fmt)
            except ValueError:
                continue
        return None

    @staticmethod
    def filter_recent(jobs):
        """Filter jobs to only those from today (since midnight 00:00)."""
        cutoff = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        recent = []
        for job in jobs:
            # Try output_end, output_start, rip_end, rip_start
            ts = (RIPLogParser._parse_timestamp(job.get('output_end'))
                  or RIPLogParser._parse_timestamp(job.get('output_start'))
                  or RIPLogParser._parse_timestamp(job.get('rip_end'))
                  or RIPLogParser._parse_timestamp(job.get('rip_start')))
            if ts and ts >= cutoff:
                recent.append(job)
            elif not ts:
                # If we can't parse the timestamp, include it (safe fallback)
                recent.append(job)
        return recent

    @staticmethod
    def build_file_list(entries):
        """
        Convert parsed RIPLOG entries into file info dicts.
        Only "Start Printing" entries produce files.
        For nests, look back at preceding "Start RIP Job" entries for filenames.
        Only includes entries from today (resets at midnight).

        Returns list of dicts with: filename, filepath, width_px, height_px,
        dpi_x, dpi_y, print_inches, nest_group (optional).
        """
        entries = RIPLogParser.filter_recent(entries)

        file_list = []
        # Track which RIP entries have been consumed by nests
        consumed_rip = set()

        # First pass: find all nests and mark their RIP members
        for i, entry in enumerate(entries):
            if entry.get('block_type') != 'print' or not entry.get('is_nest'):
                continue
            nest_count = entry['nest_count']
            # Look backwards for unconsumed RIP entries
            members = []
            for j in range(i - 1, -1, -1):
                if j in consumed_rip:
                    continue
                if entries[j].get('block_type') != 'rip':
                    continue
                if entries[j].get('is_nest'):
                    continue
                members.append(j)
                if len(members) == nest_count:
                    break
            for idx in members:
                consumed_rip.add(idx)

        # Second pass: build file list from "Start Printing" entries only
        for i, entry in enumerate(entries):
            if entry.get('block_type') != 'print':
                continue

            if entry.get('is_nest'):
                # Nest: find the RIP members for filenames
                nest_count = entry['nest_count']
                ts = entry.get('output_start', entry.get('output_end', str(i)))
                nest_group = f"riplog_nest_{ts}"
                nest_group = re.sub(r'[^a-zA-Z0-9_]', '_', nest_group)

                # Look backwards for the RIP entries that belong to this nest
                members = []
                for j in range(i - 1, -1, -1):
                    if entries[j].get('block_type') != 'rip':
                        continue
                    if entries[j].get('is_nest'):
                        continue
                    # Only take entries consumed by this nest
                    # (they were marked in pass 1)
                    members.append(j)
                    if len(members) == nest_count:
                        break

                for idx in reversed(members):
                    info = RIPLogParser._job_to_file_info(entries[idx])
                    if info:
                        info['nest_group'] = nest_group
                        file_list.append(info)
            else:
                # Individual file — find matching RIP entry for full path
                rip_entry = None
                for j in range(i - 1, -1, -1):
                    if entries[j].get('block_type') != 'rip':
                        continue
                    if entries[j].get('is_nest'):
                        continue
                    if j in consumed_rip:
                        continue
                    # Match by filename
                    rip_file = os.path.basename(entries[j].get('file', ''))
                    if rip_file == entry.get('file', ''):
                        rip_entry = entries[j]
                        consumed_rip.add(j)
                        break
                # Use RIP entry (has full path) if found, otherwise Printing entry
                source = rip_entry if rip_entry else entry
                info = RIPLogParser._job_to_file_info(source)
                if info:
                    file_list.append(info)

        return file_list

    @staticmethod
    def _job_to_file_info(job):
        """Convert a single parsed job to the agent file info format."""
        filepath = job.get('file', '')
        if not filepath:
            return None

        filename = os.path.basename(filepath)
        width_in = job.get('width_in', 0)
        height_in = job.get('height_in', 0)
        res_x = job.get('res_x', 300)
        res_y = job.get('res_y', 300)

        width_px = int(width_in * res_x) if width_in and res_x else 0
        height_px = int(height_in * res_y) if height_in and res_y else 0

        return {
            'filename': filename,
            'filepath': filepath,
            'width_px': width_px,
            'height_px': height_px,
            'dpi_x': res_x,
            'dpi_y': res_y,
            'print_inches': height_in,
            'copies': parse_copies(filename),
            'nest_group': None,
            'source': 'riplog',
        }


class RIPLogWatcher:
    """Watches RIPLOG.HTML for changes by polling file modification time."""

    def __init__(self, riplog_path, callback):
        self.riplog_path = riplog_path
        self.callback = callback
        self._last_mtime = 0
        self._last_size = 0
        self._last_job_count = 0
        self.running = True

        # Initial parse
        self._check_for_changes()

        # Start polling thread
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def _poll_loop(self):
        while self.running:
            time.sleep(3)  # Check every 3 seconds
            self._check_for_changes()

    def _check_for_changes(self):
        try:
            stat = os.stat(self.riplog_path)
            mtime = stat.st_mtime
            size = stat.st_size

            if mtime != self._last_mtime or size != self._last_size:
                self._last_mtime = mtime
                self._last_size = size

                # Parse and check if job count changed
                jobs = RIPLogParser.parse_file(self.riplog_path)
                if len(jobs) != self._last_job_count:
                    self._last_job_count = len(jobs)
                    self.callback(jobs)
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"RIPLog watcher error: {e}")

    def stop(self):
        self.running = False


# ── Configuration ──

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return None


def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def run_setup():
    """First-run setup: ask for machine name, watched folder, RIPLOG path, and server URL."""
    root = tk.Tk()
    root.title("DTF Monitor - Setup")
    root.geometry("540x540")
    root.resizable(False, False)
    root.configure(bg=C["bg"])

    config = {}

    def find_riplog():
        """Auto-detect RIPLOG.HTML by searching common FlexiPrint install paths."""
        import glob
        search_roots = [
            os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"), "SAi"),
            os.path.join(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"), "SAi"),
            os.path.join(os.environ.get("ProgramW6432", "C:\\Program Files"), "SAi"),
        ]
        for root_dir in search_roots:
            if not os.path.isdir(root_dir):
                continue
            # Search for RIPLOG.HTML inside any FlexiPRINT edition folder
            for match in glob.glob(os.path.join(root_dir, "Flexi*", "Jobs and Settings", "RIPLOG.HTML")):
                if os.path.isfile(match):
                    return match
        return ""

    def select_folder():
        folder = filedialog.askdirectory(title="Select the folder where print files are downloaded")
        if folder:
            folder_var.set(folder)

    def select_riplog():
        filepath = filedialog.askopenfilename(
            title="Select FlexiPrint RIPLOG.HTML file",
            filetypes=[("HTML files", "*.html *.htm"), ("All files", "*.*")],
            initialdir="C:/Program Files/SAi"
        )
        if filepath:
            riplog_var.set(filepath)

    def finish_setup():
        name = name_var.get().strip()
        folder = folder_var.get().strip()
        server = server_var.get().strip()
        riplog = riplog_var.get().strip()

        if not name:
            messagebox.showerror("Error", "Enter a machine name")
            return
        if not folder or not os.path.isdir(folder):
            messagebox.showerror("Error", "Select a valid folder")
            return
        if not server:
            messagebox.showerror("Error", "Enter server address")
            return
        if riplog and not os.path.isfile(riplog):
            messagebox.showerror("Error", "RIPLOG file not found. Leave blank to skip.")
            return

        config["machine_id"] = str(uuid.uuid4())[:8]
        config["machine_name"] = name
        config["watched_folder"] = folder
        config["server_url"] = server.rstrip("/")
        config["riplog_path"] = riplog if riplog else None
        save_config(config)
        root.destroy()

    # UI
    frame = tk.Frame(root, bg=C["bg"], padx=36, pady=28)
    frame.pack(fill="both", expand=True)

    # Logo
    logo_frame = tk.Frame(frame, bg=C["bg"])
    logo_frame.pack(pady=(0, 24))
    logo_mark = tk.Label(logo_frame, text="D", font=("Segoe UI", 12, "bold"),
                         fg="#000", bg=C["accent"], width=2, height=1)
    logo_mark.pack(side="left", padx=(0, 8))
    tk.Label(logo_frame, text="DTF · Warehouse Setup", font=("Segoe UI", 16, "bold"),
             fg=C["text"], bg=C["bg"]).pack(side="left")

    # Machine Name
    tk.Label(frame, text="MACHINE NAME", font=("Consolas", 9, "bold"),
             fg=C["text2"], bg=C["bg"], anchor="w").pack(fill="x")
    name_var = tk.StringVar()
    name_entry = tk.Entry(frame, textvariable=name_var, font=("Segoe UI", 12),
                          bg=C["surface2"], fg=C["text"], insertbackground=C["text"],
                          relief="flat", bd=0, highlightthickness=1,
                          highlightbackground=C["border"], highlightcolor=C["accent"])
    name_entry.pack(fill="x", pady=(4, 14), ipady=6)

    # Folder
    tk.Label(frame, text="PRINT FILES FOLDER", font=("Consolas", 9, "bold"),
             fg=C["text2"], bg=C["bg"], anchor="w").pack(fill="x")
    folder_frame = tk.Frame(frame, bg=C["bg"])
    folder_frame.pack(fill="x", pady=(4, 14))
    folder_var = tk.StringVar()
    tk.Entry(folder_frame, textvariable=folder_var, font=("Segoe UI", 12),
             bg=C["surface2"], fg=C["text"], insertbackground=C["text"],
             relief="flat", bd=0, highlightthickness=1,
             highlightbackground=C["border"], highlightcolor=C["accent"]).pack(side="left", fill="x", expand=True, ipady=6)
    tk.Button(folder_frame, text="Browse...", command=select_folder,
              font=("Segoe UI", 10, "bold"), bg=C["surface3"], fg=C["text2"],
              relief="flat", padx=12, pady=4, cursor="hand2",
              activebackground=C["border2"], activeforeground=C["text"]).pack(side="right", padx=(8, 0))

    # RIPLOG Path (optional)
    tk.Label(frame, text="FLEXIPRINT RIPLOG FILE (OPTIONAL)", font=("Consolas", 9, "bold"),
             fg=C["text2"], bg=C["bg"], anchor="w").pack(fill="x")
    riplog_frame = tk.Frame(frame, bg=C["bg"])
    riplog_frame.pack(fill="x", pady=(4, 2))
    riplog_var = tk.StringVar(value=find_riplog())
    tk.Entry(riplog_frame, textvariable=riplog_var, font=("Segoe UI", 12),
             bg=C["surface2"], fg=C["text"], insertbackground=C["text"],
             relief="flat", bd=0, highlightthickness=1,
             highlightbackground=C["border"], highlightcolor=C["accent"]).pack(side="left", fill="x", expand=True, ipady=6)
    tk.Button(riplog_frame, text="Browse...", command=select_riplog,
              font=("Segoe UI", 10, "bold"), bg=C["surface3"], fg=C["text2"],
              relief="flat", padx=12, pady=4, cursor="hand2",
              activebackground=C["border2"], activeforeground=C["text"]).pack(side="right", padx=(8, 0))
    tk.Label(frame, text="Auto-detect RIP files and nests from FlexiPrint log",
             font=("Consolas", 8), fg=C["text3"], bg=C["bg"], anchor="w").pack(fill="x", pady=(0, 14))

    # Server
    tk.Label(frame, text="SERVER ADDRESS", font=("Consolas", 9, "bold"),
             fg=C["text2"], bg=C["bg"], anchor="w").pack(fill="x")
    server_var = tk.StringVar(value="http://")
    tk.Entry(frame, textvariable=server_var, font=("Segoe UI", 12),
             bg=C["surface2"], fg=C["text"], insertbackground=C["text"],
             relief="flat", bd=0, highlightthickness=1,
             highlightbackground=C["border"], highlightcolor=C["accent"]).pack(fill="x", pady=(4, 24), ipady=6)

    # Save button
    tk.Button(frame, text="Save & Start", command=finish_setup,
              font=("Segoe UI", 13, "bold"), bg=C["accent"], fg="#000",
              relief="flat", padx=24, pady=8, cursor="hand2",
              activebackground=C["accent2"], activeforeground="#000").pack()

    root.mainloop()
    return load_config()


# ── Copies parsing ──

def parse_copies(filename):
    """Extract copies count from filename pattern like '(2 x)'. Returns 1 if not found."""
    m = re.search(r'\((\d+)\s*x\)', filename)
    return int(m.group(1)) if m else 1


# ── File scanning ──

def get_image_info(filepath):
    """Read image dimensions and DPI from file metadata."""
    try:
        with Image.open(filepath) as img:
            width, height = img.size
            dpi = img.info.get("dpi", (DEFAULT_DPI, DEFAULT_DPI))
            dpi_x = float(dpi[0]) if dpi[0] else DEFAULT_DPI
            dpi_y = float(dpi[1]) if dpi[1] else DEFAULT_DPI
            print_inches = height / dpi_y if dpi_y > 0 else 0

            filename = os.path.basename(filepath)
            copies = parse_copies(filename)
            return {
                "filename": filename,
                "filepath": filepath,
                "width_px": width,
                "height_px": height,
                "dpi_x": round(dpi_x, 1),
                "dpi_y": round(dpi_y, 1),
                "print_inches": round(print_inches, 2),
                "copies": copies,
            }
    except Exception as e:
        print(f"Error reading {filepath}: {e}")
        return None


def scan_folder(folder):
    """Scan folder for supported image files."""
    files = []
    if not os.path.isdir(folder):
        return files
    for entry in os.scandir(folder):
        if entry.is_file() and Path(entry.name).suffix.lower() in SUPPORTED_EXTENSIONS:
            info = get_image_info(entry.path)
            if info:
                files.append(info)
    return files


# ── Folder watcher ──

class FolderHandler(FileSystemEventHandler):
    def __init__(self, callback):
        self.callback = callback

    def on_created(self, event):
        if not event.is_directory:
            ext = Path(event.src_path).suffix.lower()
            if ext in SUPPORTED_EXTENSIONS:
                time.sleep(1)
                self.callback()

    def on_deleted(self, event):
        if not event.is_directory:
            self.callback()

    def on_moved(self, event):
        self.callback()


# ── Main Agent Application ──

class AgentApp:
    def __init__(self, config):
        self.config = config
        self.server_url = config["server_url"]
        self.machine_id = config["machine_id"]
        self.machine_name = config["machine_name"]
        self.watched_folder = config["watched_folder"]
        self.riplog_path = config.get("riplog_path")
        self.jobs = []
        self.local_files = []
        self.riplog_files = []  # Files from RIPLOG
        self.history_jobs = []  # Completed jobs from server
        self.connected = False
        self.running = True
        self.selected_job_ids = set()  # For nest selection
        self.active_tab = "queue"  # "queue" or "history"
        self.search_query = ""  # Search filter

        # Folder watcher disabled — using RIPLOG only
        self.observer = None

        # Start RIPLOG watcher if configured
        self.riplog_watcher = None
        if self.riplog_path and os.path.isfile(self.riplog_path):
            self.riplog_watcher = RIPLogWatcher(self.riplog_path, self.on_riplog_change)

        # Start heartbeat thread
        self.heartbeat_thread = threading.Thread(target=self.heartbeat_loop, daemon=True)
        self.heartbeat_thread.start()

        # Build and run GUI
        self.build_gui()

    def _on_operator_change(self, *args):
        """Save operator name to config when changed."""
        name = self.operator_var.get().strip()
        self.config["operator"] = name
        save_config(self.config)

    def on_folder_change(self):
        self.send_heartbeat()
        self.root.after(100, self.refresh_ui)

    def on_riplog_change(self, parsed_jobs):
        """Called by RIPLogWatcher when new RIP jobs are detected."""
        self.riplog_files = RIPLogParser.build_file_list(parsed_jobs)
        self.send_heartbeat()
        try:
            self.root.after(0, self.refresh_ui)
        except Exception:
            pass

    def send_heartbeat(self):
        # Only use RIPLOG files (folder scan disabled)
        all_files = list(self.riplog_files)

        try:
            operator = ""
            try:
                operator = self.operator_var.get().strip()
            except Exception:
                operator = self.config.get("operator", "")
            payload = {
                "machine_id": self.machine_id,
                "machine_name": self.machine_name,
                "watched_folder": self.watched_folder,
                "operator": operator,
                "files": all_files,
            }
            resp = requests.post(f"{self.server_url}/api/heartbeat", json=payload, timeout=5)
            if resp.status_code != 200:
                print(f"Server error {resp.status_code}: {resp.text[:200]}")
                self.connected = False
                return False
            data = resp.json()
            self.jobs = data.get("jobs", [])
            self.connected = True
            return True
        except Exception as e:
            print(f"Heartbeat error: {e}")
            self.connected = False
            self.jobs = [{
                "id": None,
                "filename": f["filename"],
                "filepath": f["filepath"],
                "width_px": f["width_px"],
                "height_px": f["height_px"],
                "dpi_x": f["dpi_x"],
                "dpi_y": f["dpi_y"],
                "print_inches": f["print_inches"],
                "copies": f.get("copies", 1),
                "status": "queued",
                "nest_group": None,
            } for f in self.local_files]
            return False

    def fetch_history(self):
        """Fetch completed jobs for this machine from server."""
        try:
            resp = requests.get(
                f"{self.server_url}/api/history",
                params={"limit": 50},
                timeout=5,
            )
            if resp.ok:
                all_history = resp.json()
                # Filter to only this machine's jobs
                self.history_jobs = [
                    j for j in all_history
                    if j.get("machine_id") == self.machine_id
                ]
        except Exception as e:
            print(f"History fetch error: {e}")

    def heartbeat_loop(self):
        while self.running:
            success = self.send_heartbeat()
            self.fetch_history()
            try:
                self.root.after(0, self.refresh_ui)
                self.root.after(0, self.update_status, success)
            except Exception:
                pass
            time.sleep(8)

    def update_status(self, connected):
        if connected:
            self.status_dot.config(bg=C["green"])
            self.status_label.config(text="LIVE", fg=C["green"])
        else:
            self.status_dot.config(bg=C["red"])
            self.status_label.config(text="OFFLINE", fg=C["red"])

    def mark_printing(self, job_id):
        try:
            resp = requests.post(
                f"{self.server_url}/api/jobs/{job_id}/start",
                json={"machine_id": self.machine_id},
                timeout=5,
            )
            if resp.ok:
                self.send_heartbeat()
                self.refresh_ui()
        except Exception as e:
            print(f"Error marking printing: {e}")

    def mark_done(self, job_id):
        try:
            resp = requests.post(
                f"{self.server_url}/api/jobs/{job_id}/complete",
                timeout=5,
            )
            if resp.ok:
                self.send_heartbeat()
                self.refresh_ui()
        except Exception as e:
            print(f"Error marking done: {e}")

    def do_nest(self):
        """Nest selected queued files together."""
        ids = list(self.selected_job_ids)
        if len(ids) < 2:
            return
        try:
            resp = requests.post(
                f"{self.server_url}/api/nest",
                json={"machine_id": self.machine_id, "job_ids": ids},
                timeout=5,
            )
            if resp.ok:
                data = resp.json()
                self.jobs = data.get("jobs", self.jobs)
                self.selected_job_ids.clear()
                self.refresh_ui()
        except Exception as e:
            print(f"Error creating nest: {e}")

    def do_unnest(self, nest_group):
        """Break apart a nest group."""
        try:
            resp = requests.post(
                f"{self.server_url}/api/unnest",
                json={"machine_id": self.machine_id, "nest_group": nest_group},
                timeout=5,
            )
            if resp.ok:
                data = resp.json()
                self.jobs = data.get("jobs", self.jobs)
                self.refresh_ui()
        except Exception as e:
            print(f"Error unnesting: {e}")

    def toggle_select(self, job_id):
        if job_id in self.selected_job_ids:
            self.selected_job_ids.discard(job_id)
        else:
            self.selected_job_ids.add(job_id)
        self.refresh_ui()

    def build_gui(self):
        self.root = tk.Tk()
        self.root.title(f"DTF Monitor — {self.machine_name}")
        self.root.geometry("620x720")
        self.root.configure(bg=C["bg"])
        self.root.protocol("WM_DELETE_WINDOW", self.on_minimize)

        # ── Header ──
        header = tk.Frame(self.root, bg=C["surface"], pady=12, padx=20)
        header.pack(fill="x")

        # Logo
        logo_mark = tk.Label(header, text="D", font=("Segoe UI", 10, "bold"),
                             fg="#000", bg=C["accent"], width=2)
        logo_mark.pack(side="left", padx=(0, 10))

        tk.Label(header, text=self.machine_name, font=("Segoe UI", 15, "bold"),
                 fg=C["text"], bg=C["surface"]).pack(side="left")

        # Status indicator (right side)
        status_frame = tk.Frame(header, bg=C["surface"])
        status_frame.pack(side="right")

        self.status_dot = tk.Frame(status_frame, bg=C["green"], width=7, height=7)
        self.status_dot.pack(side="left", padx=(0, 6))
        self.status_dot.pack_propagate(False)

        self.status_label = tk.Label(status_frame, text="LIVE",
                                     font=("Consolas", 10, "bold"),
                                     fg=C["green"], bg=C["surface"])
        self.status_label.pack(side="left")

        # ── Operator Name Bar ──
        operator_bar = tk.Frame(self.root, bg=C["surface2"], pady=8, padx=20)
        operator_bar.pack(fill="x")

        tk.Label(operator_bar, text="OPERATOR:", font=("Consolas", 10, "bold"),
                 fg=C["text2"], bg=C["surface2"]).pack(side="left", padx=(0, 8))

        self.operator_var = tk.StringVar(value=self.config.get("operator", ""))
        self.operator_entry = tk.Entry(operator_bar, textvariable=self.operator_var,
                                        font=("Segoe UI", 12), width=20,
                                        bg=C["surface3"], fg=C["accent"], insertbackground=C["accent"],
                                        relief="flat", bd=0, highlightthickness=1,
                                        highlightbackground=C["border"], highlightcolor=C["accent"])
        self.operator_entry.pack(side="left", fill="x", expand=True, ipady=4)

        # Save operator name when changed
        self.operator_var.trace_add("write", self._on_operator_change)

        # ── Stats Bar ──
        stats_bar = tk.Frame(self.root, bg=C["surface2"], pady=10, padx=20)
        stats_bar.pack(fill="x")

        self.stat_files = self._make_stat(stats_bar, "FILES", "0")
        self.stat_inches = self._make_stat(stats_bar, "TOTAL INCHES", "0")
        self.stat_printing = self._make_stat(stats_bar, "PRINTING", "—")

        # Folder path + RIPLOG status
        folder_bar = tk.Frame(self.root, bg=C["bg"], padx=20, pady=6)
        folder_bar.pack(fill="x")
        tk.Label(folder_bar, text=f"FOLDER: {self.watched_folder}",
                 font=("Consolas", 9), fg=C["text3"], bg=C["bg"], anchor="w").pack(fill="x")
        if self.riplog_path:
            riplog_status = "RIPLOG: Active" if self.riplog_watcher else "RIPLOG: File not found"
            riplog_color = C["green"] if self.riplog_watcher else C["red"]
            tk.Label(folder_bar, text=riplog_status,
                     font=("Consolas", 9), fg=riplog_color, bg=C["bg"], anchor="w").pack(fill="x")

        # ── Separator ──
        tk.Frame(self.root, bg=C["border"], height=1).pack(fill="x")

        # ── Tab bar (Queue / History) ──
        tab_bar = tk.Frame(self.root, bg=C["surface"], padx=20, pady=8)
        tab_bar.pack(fill="x")

        self.tab_queue_btn = tk.Label(tab_bar, text="Queue", font=("Segoe UI", 11, "bold"),
                                       fg=C["accent"], bg=C["surface"], padx=12, pady=4, cursor="hand2")
        self.tab_queue_btn.pack(side="left")
        self.tab_queue_btn.bind("<Button-1>", lambda e: self.switch_tab("queue"))

        self.tab_history_btn = tk.Label(tab_bar, text="History", font=("Segoe UI", 11),
                                         fg=C["text3"], bg=C["surface"], padx=12, pady=4, cursor="hand2")
        self.tab_history_btn.pack(side="left")
        self.tab_history_btn.bind("<Button-1>", lambda e: self.switch_tab("history"))

        # Search bar
        search_frame = tk.Frame(tab_bar, bg=C["surface"])
        search_frame.pack(side="right")

        tk.Label(search_frame, text="Search:", font=("Consolas", 9),
                 fg=C["text3"], bg=C["surface"]).pack(side="left", padx=(0, 6))

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", self._on_search_change)
        self.search_entry = tk.Entry(search_frame, textvariable=self.search_var,
                                font=("Segoe UI", 11), width=18,
                                bg=C["surface2"], fg=C["text"], insertbackground=C["text"],
                                relief="flat", bd=0, highlightthickness=1,
                                highlightbackground=C["border"], highlightcolor=C["accent"])
        self.search_entry.pack(side="left", ipady=3)

        # ── Nest action bar (hidden until 2+ selected) ──
        self.nest_bar = tk.Frame(self.root, bg=C["purple_bg"], padx=20, pady=8)
        # Not packed initially — shown when needed

        self.nest_label = tk.Label(self.nest_bar, text="0 files selected",
                                   font=("Consolas", 10), fg=C["purple"], bg=C["purple_bg"])
        self.nest_label.pack(side="left")

        tk.Button(self.nest_bar, text="Nest Selected", font=("Segoe UI", 10, "bold"),
                  bg=C["purple"], fg="white", relief="flat", padx=12, pady=3,
                  cursor="hand2", activebackground="#8B6FE0", activeforeground="white",
                  command=self.do_nest).pack(side="right")

        tk.Button(self.nest_bar, text="Clear", font=("Segoe UI", 10),
                  bg=C["surface3"], fg=C["text3"], relief="flat", padx=8, pady=3,
                  cursor="hand2", command=self._clear_selection).pack(side="right", padx=(0, 8))

        # ── Scrollable file list ──
        self.list_frame_container = tk.Frame(self.root, bg=C["bg"])
        self.list_frame_container.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(self.list_frame_container, bg=C["bg"],
                                highlightthickness=0, bd=0)
        scrollbar = tk.Scrollbar(self.list_frame_container, orient="vertical",
                                 command=self.canvas.yview,
                                 bg=C["surface3"], troughcolor=C["bg"],
                                 width=6, relief="flat")
        self.list_frame = tk.Frame(self.canvas, bg=C["bg"])

        self.list_frame.bind("<Configure>",
                             lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas_window = self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)

        # Make list_frame fill canvas width
        self.canvas.bind("<Configure>", self._on_canvas_resize)

        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Mouse wheel scrolling
        self.canvas.bind_all("<MouseWheel>",
                             lambda e: self.canvas.yview_scroll(int(-1*(e.delta/120)), "units"))

        # Initial data
        self.send_heartbeat()
        self.refresh_ui()

        self.root.mainloop()

    def _on_canvas_resize(self, event):
        self.canvas.itemconfig(self.canvas_window, width=event.width)

    def switch_tab(self, tab):
        self.active_tab = tab
        if tab == "queue":
            self.tab_queue_btn.config(fg=C["accent"], font=("Segoe UI", 11, "bold"))
            self.tab_history_btn.config(fg=C["text3"], font=("Segoe UI", 11))
        else:
            self.tab_queue_btn.config(fg=C["text3"], font=("Segoe UI", 11))
            self.tab_history_btn.config(fg=C["accent"], font=("Segoe UI", 11, "bold"))
        self.refresh_ui()

    def _on_search_change(self, *args):
        self.search_query = self.search_var.get().strip().lower()
        self.refresh_ui()
        self.search_entry.focus_set()

    def _clear_selection(self):
        self.selected_job_ids.clear()
        self.refresh_ui()

    def _make_stat(self, parent, label, value):
        frame = tk.Frame(parent, bg=C["surface2"])
        frame.pack(side="left", expand=True, fill="x")
        tk.Label(frame, text=label, font=("Consolas", 9, "bold"),
                 fg=C["text3"], bg=C["surface2"]).pack()
        val_label = tk.Label(frame, text=value, font=("Segoe UI", 16, "bold"),
                             fg=C["text"], bg=C["surface2"])
        val_label.pack()
        return val_label

    def refresh_ui(self):
        for widget in self.list_frame.winfo_children():
            widget.destroy()

        printing = [j for j in self.jobs if j["status"] == "printing"]
        queued = [j for j in self.jobs if j["status"] == "queued"]

        # Update stats bar
        total_files = len(printing) + len(queued)
        total_inches = sum(j.get("print_inches", 0) * j.get("copies", 1) for j in printing + queued)
        self.stat_files.config(text=str(total_files))
        self.stat_inches.config(text=f"{total_inches:.1f}")

        if printing:
            nest_groups = set(j.get("nest_group") for j in printing if j.get("nest_group"))
            if nest_groups:
                label = f"NEST ({len(printing)} files)"
            else:
                fname = printing[0]["filename"]
                label = fname[:20] + "..." if len(fname) > 20 else fname
            self.stat_printing.config(text=label, fg=C["green"])
        else:
            self.stat_printing.config(text="—", fg=C["text3"])

        # History tab
        if self.active_tab == "history":
            self.nest_bar.pack_forget()
            self._render_history_tab()
            return

        # Show/hide nest action bar
        if len(self.selected_job_ids) >= 2:
            self.nest_label.config(text=f"{len(self.selected_job_ids)} files selected")
            self.nest_bar.pack(fill="x", before=self.list_frame_container)
        else:
            self.nest_bar.pack_forget()

        # Apply search filter
        if self.search_query:
            printing = [j for j in printing if self.search_query in j.get("filename", "").lower()]
            queued = [j for j in queued if self.search_query in j.get("filename", "").lower()]

        if not printing and not queued:
            empty_frame = tk.Frame(self.list_frame, bg=C["bg"], pady=40)
            empty_frame.pack(fill="x")
            msg = "No matching files" if self.search_query else "No print files found"
            tk.Label(empty_frame, text=msg,
                     font=("Segoe UI", 12), fg=C["text3"], bg=C["bg"]).pack()
            if not self.search_query:
                tk.Label(empty_frame, text="Waiting for RIPLOG data...",
                         font=("Consolas", 10), fg=C["text3"], bg=C["bg"]).pack(pady=(4, 0))
            return

        # ── Currently printing ──
        if printing:
            # Group by nest_group
            nest_groups = {}
            solo_printing = []
            for j in printing:
                ng = j.get("nest_group")
                if ng:
                    nest_groups.setdefault(ng, []).append(j)
                else:
                    solo_printing.append(j)

            self._section_label("CURRENTLY PRINTING", C["green"])

            for ng, nest_jobs in nest_groups.items():
                self._build_nest_printing_block(ng, nest_jobs)

            for job in solo_printing:
                self._build_printing_row(job)

        # ── Queue ──
        if queued:
            # Group queued by nest_group
            nest_groups = {}
            solo_queued = []
            for j in queued:
                ng = j.get("nest_group")
                if ng:
                    nest_groups.setdefault(ng, []).append(j)
                else:
                    solo_queued.append(j)

            self._section_label(f"QUEUE  ({len(queued)} files)", C["text2"])

            num = 1
            for ng, nest_jobs in nest_groups.items():
                self._build_nest_queued_block(ng, nest_jobs, num)
                num += len(nest_jobs)

            for job in solo_queued:
                self._build_queue_row(job, num)
                num += 1

    def _section_label(self, text, color):
        frame = tk.Frame(self.list_frame, bg=C["bg"], padx=20, pady=8)
        frame.pack(fill="x")

        # Dot
        if color == C["green"]:
            dot = tk.Frame(frame, bg=color, width=6, height=6)
            dot.pack(side="left", padx=(0, 8))
            dot.pack_propagate(False)

        tk.Label(frame, text=text, font=("Consolas", 10, "bold"),
                 fg=color, bg=C["bg"]).pack(side="left")

    def _build_printing_row(self, job):
        row = tk.Frame(self.list_frame, bg=C["green_bg"], padx=20, pady=12,
                       highlightbackground=C["green_bd"], highlightthickness=1)
        row.pack(fill="x", padx=16, pady=(4, 2))

        # Left info
        info = tk.Frame(row, bg=C["green_bg"])
        info.pack(side="left", fill="x", expand=True)

        tk.Label(info, text=job["filename"],
                 font=("Segoe UI", 13, "bold"),
                 fg=C["accent"], bg=C["green_bg"],
                 anchor="w", wraplength=320).pack(anchor="w")

        copies = job.get("copies", 1)
        inch_str = f"{job['print_inches']:.1f} in" if copies <= 1 else f"{job['print_inches']:.1f} in x{copies} = {job['print_inches'] * copies:.1f} in"
        meta = f"{job['width_px']}x{job['height_px']} px  |  DPI: {job['dpi_x']}x{job['dpi_y']}  |  {inch_str}"
        tk.Label(info, text=meta, font=("Consolas", 9),
                 fg=C["text2"], bg=C["green_bg"], anchor="w").pack(anchor="w", pady=(2, 0))

        # Done button
        if job.get("id") is not None:
            tk.Button(row, text="Done", font=("Segoe UI", 11, "bold"),
                      bg=C["green"], fg="#000",
                      activebackground=C["accent2"], activeforeground="#000",
                      padx=14, pady=4, relief="flat", cursor="hand2",
                      command=lambda jid=job["id"]: self.mark_done(jid)).pack(side="right", padx=(8, 0))

        # Status badge
        badge = tk.Label(row, text="PRINTING", font=("Consolas", 10, "bold"),
                         fg=C["green"], bg=C["green_bd"],
                         padx=10, pady=4)
        badge.pack(side="right")

    def _build_nest_printing_block(self, nest_group, jobs):
        """Render a nest group that's currently printing."""
        total_inches = sum(j.get("print_inches", 0) * j.get("copies", 1) for j in jobs)

        # Nest header
        nest_frame = tk.Frame(self.list_frame, bg=C["green_bg"], padx=20, pady=10,
                              highlightbackground=C["green_bd"], highlightthickness=1)
        nest_frame.pack(fill="x", padx=16, pady=(4, 2))

        header_row = tk.Frame(nest_frame, bg=C["green_bg"])
        header_row.pack(fill="x")

        tk.Label(header_row, text=f"NEST  ({len(jobs)} files · {total_inches:.1f} in)",
                 font=("Segoe UI", 13, "bold"), fg=C["accent"], bg=C["green_bg"]).pack(side="left")

        # Done button for the whole nest
        first_id = jobs[0].get("id")
        if first_id is not None:
            tk.Button(header_row, text="Done", font=("Segoe UI", 11, "bold"),
                      bg=C["green"], fg="#000",
                      activebackground=C["accent2"], activeforeground="#000",
                      padx=14, pady=4, relief="flat", cursor="hand2",
                      command=lambda jid=first_id: self.mark_done(jid)).pack(side="right", padx=(8, 0))

        tk.Label(header_row, text="PRINTING", font=("Consolas", 10, "bold"),
                 fg=C["green"], bg=C["green_bd"], padx=10, pady=4).pack(side="right")

        # List files inside nest
        for j in jobs:
            file_row = tk.Frame(nest_frame, bg=C["green_bg"], padx=20)
            file_row.pack(fill="x", pady=(4, 0))
            tk.Label(file_row, text="·", font=("Consolas", 10),
                     fg=C["green"], bg=C["green_bg"]).pack(side="left", padx=(0, 6))
            tk.Label(file_row, text=j["filename"], font=("Segoe UI", 11),
                     fg=C["text"], bg=C["green_bg"], anchor="w").pack(side="left")
            c = j.get("copies", 1)
            inch_text = f"{j['print_inches']:.1f} in" if c <= 1 else f"{j['print_inches']:.1f} in x{c}"
            tk.Label(file_row, text=inch_text,
                     font=("Consolas", 9), fg=C["text2"], bg=C["green_bg"]).pack(side="right")

    def _build_nest_queued_block(self, nest_group, jobs, start_num):
        """Render a queued nest group."""
        total_inches = sum(j.get("print_inches", 0) * j.get("copies", 1) for j in jobs)

        nest_frame = tk.Frame(self.list_frame, bg=C["purple_bg"], padx=16, pady=10,
                              highlightbackground=C["purple_bd"], highlightthickness=1)
        nest_frame.pack(fill="x", padx=16, pady=(2, 2))

        header_row = tk.Frame(nest_frame, bg=C["purple_bg"])
        header_row.pack(fill="x")

        tk.Label(header_row, text=f"NEST  ({len(jobs)} files · {total_inches:.1f} in)",
                 font=("Segoe UI", 12, "bold"), fg=C["purple"], bg=C["purple_bg"]).pack(side="left")

        # Buttons
        btn_frame = tk.Frame(header_row, bg=C["purple_bg"])
        btn_frame.pack(side="right")

        # Print button for the nest (starts the whole nest)
        first_id = jobs[0].get("id")
        if first_id is not None:
            tk.Button(btn_frame, text="Print", font=("Segoe UI", 11, "bold"),
                      bg=C["blue"], fg="white",
                      activebackground="#3580D4", activeforeground="white",
                      padx=14, pady=4, relief="flat", cursor="hand2",
                      command=lambda jid=first_id: self.mark_printing(jid)).pack(side="left", padx=(0, 6))

        # Unnest button
        tk.Button(btn_frame, text="Unnest", font=("Segoe UI", 10),
                  bg=C["surface3"], fg=C["text3"],
                  activebackground=C["border2"], activeforeground=C["text"],
                  padx=8, pady=3, relief="flat", cursor="hand2",
                  command=lambda ng=nest_group: self.do_unnest(ng)).pack(side="left")

        # List files inside nest
        for j in jobs:
            file_row = tk.Frame(nest_frame, bg=C["purple_bg"], padx=20)
            file_row.pack(fill="x", pady=(4, 0))
            tk.Label(file_row, text="·", font=("Consolas", 10),
                     fg=C["purple"], bg=C["purple_bg"]).pack(side="left", padx=(0, 6))
            tk.Label(file_row, text=j["filename"], font=("Segoe UI", 11),
                     fg=C["text"], bg=C["purple_bg"], anchor="w").pack(side="left")
            c = j.get("copies", 1)
            inch_text = f"{j['print_inches']:.1f} in" if c <= 1 else f"{j['print_inches']:.1f} in x{c}"
            tk.Label(file_row, text=inch_text,
                     font=("Consolas", 9), fg=C["text2"], bg=C["purple_bg"]).pack(side="right")

    def _build_queue_row(self, job, num):
        is_selected = job.get("id") in self.selected_job_ids
        bg = C["blue_bg"] if is_selected else C["surface"]
        border_color = C["blue"] if is_selected else C["border"]

        row = tk.Frame(self.list_frame, bg=bg, padx=16, pady=10,
                       highlightbackground=border_color, highlightthickness=1)
        row.pack(fill="x", padx=16, pady=(2, 2))

        # Checkbox for nest selection
        if job.get("id") is not None:
            cb_text = "☑" if is_selected else "☐"
            cb_color = C["blue"] if is_selected else C["text3"]
            cb = tk.Label(row, text=cb_text, font=("Segoe UI", 14),
                          fg=cb_color, bg=bg, cursor="hand2")
            cb.pack(side="left", padx=(0, 8))
            cb.bind("<Button-1>", lambda e, jid=job["id"]: self.toggle_select(jid))

        # Number
        tk.Label(row, text=f"{num:02d}", font=("Consolas", 10),
                 fg=C["text3"], bg=bg, width=3).pack(side="left", padx=(0, 10))

        # Info
        info = tk.Frame(row, bg=bg)
        info.pack(side="left", fill="x", expand=True)

        tk.Label(info, text=job["filename"],
                 font=("Segoe UI", 12),
                 fg=C["text"], bg=bg,
                 anchor="w", wraplength=260).pack(anchor="w")

        copies = job.get("copies", 1)
        inch_str = f"{job['print_inches']:.1f} in" if copies <= 1 else f"{job['print_inches']:.1f} in x{copies} = {job['print_inches'] * copies:.1f} in"
        meta = f"{job['width_px']}x{job['height_px']} px  |  DPI: {job['dpi_x']}x{job['dpi_y']}  |  {inch_str}"
        tk.Label(info, text=meta, font=("Consolas", 9),
                 fg=C["text3"], bg=bg, anchor="w").pack(anchor="w", pady=(2, 0))

        # Print button
        if job.get("id") is not None:
            btn = tk.Button(row, text="Print", font=("Segoe UI", 11, "bold"),
                            bg=C["blue"], fg="white",
                            activebackground="#3580D4", activeforeground="white",
                            padx=14, pady=4, relief="flat", cursor="hand2",
                            command=lambda jid=job["id"]: self.mark_printing(jid))
        else:
            btn = tk.Button(row, text="Print", font=("Segoe UI", 11, "bold"),
                            bg=C["surface3"], fg=C["text3"],
                            padx=14, pady=4, relief="flat", state="disabled")
        btn.pack(side="right", padx=(8, 0))

    def _render_history_tab(self):
        """Render completed jobs in the history tab."""
        jobs = self.history_jobs

        # Apply search filter
        if self.search_query:
            jobs = [j for j in jobs if self.search_query in j.get("filename", "").lower()]

        if not jobs:
            empty_frame = tk.Frame(self.list_frame, bg=C["bg"], pady=40)
            empty_frame.pack(fill="x")
            msg = "No matching jobs" if self.search_query else "No completed jobs yet"
            tk.Label(empty_frame, text=msg,
                     font=("Segoe UI", 12), fg=C["text3"], bg=C["bg"]).pack()
            return

        self._section_label(f"COMPLETED  ({len(jobs)} jobs)", C["text2"])

        for i, job in enumerate(jobs):
            bg = C["surface"] if i % 2 == 0 else C["surface2"]
            row = tk.Frame(self.list_frame, bg=bg, padx=16, pady=8,
                           highlightbackground=C["border"], highlightthickness=1)
            row.pack(fill="x", padx=16, pady=(1, 1))

            # Left info
            info = tk.Frame(row, bg=bg)
            info.pack(side="left", fill="x", expand=True)

            tk.Label(info, text=job.get("filename", ""),
                     font=("Segoe UI", 11), fg=C["text"], bg=bg,
                     anchor="w", wraplength=320).pack(anchor="w")

            # Time and duration
            completed_at = job.get("completed_at", "")
            started_at = job.get("started_at", "")
            duration = ""
            time_str = ""
            if completed_at:
                try:
                    from datetime import datetime as dt
                    ct = dt.fromisoformat(completed_at)
                    time_str = ct.strftime("%H:%M")
                except Exception:
                    time_str = completed_at[:16]
            if started_at and completed_at:
                try:
                    from datetime import datetime as dt
                    st = dt.fromisoformat(started_at)
                    ct = dt.fromisoformat(completed_at)
                    secs = int((ct - st).total_seconds())
                    if secs < 60:
                        duration = f"{secs}s"
                    elif secs < 3600:
                        duration = f"{secs // 60}m {secs % 60}s"
                    else:
                        duration = f"{secs // 3600}h {(secs % 3600) // 60}m"
                except Exception:
                    pass

            copies = job.get("copies", 1)
            inch_str = f"{job.get('print_inches', 0):.1f} in"
            if copies > 1:
                inch_str += f" x{copies}"
            meta_parts = [inch_str]
            if time_str:
                meta_parts.append(time_str)
            if duration:
                meta_parts.append(duration)

            tk.Label(info, text="  |  ".join(meta_parts),
                     font=("Consolas", 9), fg=C["text3"], bg=bg,
                     anchor="w").pack(anchor="w", pady=(2, 0))

            # Done badge
            tk.Label(row, text="DONE", font=("Consolas", 9, "bold"),
                     fg=C["green"], bg=C["green_bg"],
                     padx=8, pady=3).pack(side="right")

    def on_minimize(self):
        self.root.iconify()

    def cleanup(self):
        self.running = False
        if self.observer:
            self.observer.stop()
            self.observer.join()
        if self.riplog_watcher:
            self.riplog_watcher.stop()


# ── Entry point ──

def main():
    config = load_config()
    if config is None:
        config = run_setup()
    if config is None:
        print("Setup cancelled.")
        sys.exit(0)

    app = AgentApp(config)
    app.cleanup()


if __name__ == "__main__":
    main()
