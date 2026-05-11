# =============================================================================
#  Windhawk Service Management Utility
#  Based on wsbu.py by scorpion421 (GPL)
# =============================================================================

import ctypes
import datetime
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import winreg
import zipfile
from tkinter import filedialog, messagebox, scrolledtext
import tkinter as tk
from tkinter import ttk

# ---------------------------------------------------------------------------
# Application constants
# ---------------------------------------------------------------------------
APP_VERSION   = "2.5.5-pyw"
APP_TITLE     = f"Windhawk Service Management Utility v{APP_VERSION}"

WINDHAWK_REGISTRY_KEY   = r"SOFTWARE\Windhawk"
WINDHAWK_SERVICE_NAME   = "Windhawk"
WINDHAWK_ROOT_SENTINELS = ("ModsSource", os.path.join("Engine", "Mods"), "windhawk.exe")

DEFAULT_WINDHAWK_ROOT = os.path.expandvars(r"%programdata%\Windhawk")
_SCRIPT_DIR           = (os.path.dirname(os.path.abspath(sys.argv[0]))
                         if sys.argv and sys.argv[0]
                         else os.path.expanduser("~"))
DEFAULT_BACKUP_FOLDER = _SCRIPT_DIR
BACKUP_SUBFOLDER_NAME = "Windhawk_Backup"
DEFAULT_MAX_BACKUPS   = 10

# Candidate paths probed in order when auto-detecting the Windhawk root.
WINDHAWK_ROOT_CANDIDATES = [
    os.path.expandvars(r"%programdata%\Windhawk"),
    os.path.expandvars(r"%localappdata%\Windhawk"),
    r"C:\Windhawk",
    r"C:\Program Files\Windhawk",
    r"C:\Program Files (x86)\Windhawk",
    os.path.join(_SCRIPT_DIR, "Windhawk"),
]

# Config file lives next to the script and mirrors the script’s basename
CONFIG_FILE = os.path.join(
    _SCRIPT_DIR,
    f"{os.path.splitext(os.path.basename(sys.argv[0]))[0]}.config.json",
)

PAD = 8  # Universal spacing unit used throughout the UI

# =============================================================================
#                            CORE LOGIC (BACKEND)
# =============================================================================

# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """
    Load settings from the JSON config file next to the script.
    Falls back to the old AppData location (v2.5.4 and earlier) if no
    local config exists, then migrates it to the new location.
    """
    defaults = {
        "windhawk_root": DEFAULT_WINDHAWK_ROOT,
        "backup_folder": DEFAULT_BACKUP_FOLDER,
        "portable":      False,
        "max_backups":   DEFAULT_MAX_BACKUPS,
        "use_subfolder": True,
    }

    # 1) Try the new location first (next to the script)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            stored = json.load(fh)
        defaults.update(stored)
        return defaults
    except (OSError, json.JSONDecodeError):
        pass

    # 2) New config missing – check the legacy AppData location
    legacy_dir  = os.path.expandvars(r"%appdata%\Windhawk_Backup_Utility")
    legacy_file = os.path.join(legacy_dir, "config.json")
    try:
        with open(legacy_file, "r", encoding="utf-8") as fh:
            stored = json.load(fh)
        defaults.update(stored)
        # Migrate to the new location (best effort – failure is non‑fatal)
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
                json.dump(defaults, fh, indent=2)
        except OSError:
            pass
    except (OSError, json.JSONDecodeError):
        pass

    return defaults


def save_config(cfg: dict) -> None:
    """Persists settings to the JSON config file next to the script. Failure is non-fatal."""
    try:
        # The directory of CONFIG_FILE is the script directory, which already exists.
        # We still call makedirs just in case the script was placed in a different spot.
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Backup catalogue helpers
# ---------------------------------------------------------------------------

def _format_size(size_bytes: int) -> str:
    """Formats a byte count as a human-readable string (B / KB / MB / GB)."""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes //= 1024
    return f"{size_bytes:.1f} TB"


def list_backups(backup_folder: str) -> list[dict]:
    """
    Scans the backup folder for archives and returns metadata for each,
    newest first. Reads manifest.json from inside each ZIP if available.
    """
    results: list[dict] = []
    if not os.path.isdir(backup_folder):
        return results

    names = sorted(
        (n for n in os.listdir(backup_folder)
         if n.startswith("windhawk-backup_") and n.endswith(".zip")),
        reverse=True,
    )
    for name in names:
        full_path = os.path.join(backup_folder, name)
        try:
            size  = os.path.getsize(full_path)
            mtime = os.path.getmtime(full_path)
            dt    = datetime.datetime.fromtimestamp(mtime)

            manifest:  dict       = {}
            mod_count: int | None = None
            try:
                with zipfile.ZipFile(full_path, "r") as zf:
                    znames = zf.namelist()
                    if "manifest.json" in znames:
                        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
                    else:
                        # Legacy archive: normalise separators (Windows shutil
                        # make_archive can use backslashes in ZIP entries).
                        normalized = [n.replace("\\", "/") for n in znames]
                        mod_count = sum(
                            1 for n in normalized
                            if n.startswith("ModsSource/") and n.endswith(".wh.cpp")
                        )
            except Exception:
                pass

            mods_display = str(
                manifest.get("mod_count", mod_count)
                if "mod_count" in manifest or mod_count is not None
                else "-"
            )

            results.append({
                "name":  name,
                "path":  full_path,
                "date":  dt.strftime("%Y-%m-%d  %H:%M:%S"),
                "size":  _format_size(size),
                "kind":  "Portable" if manifest.get("portable") else "Standard",
                "mods":  mods_display,
            })
        except OSError:
            continue
    return results


def create_manifest(windhawk_root: str, portable: bool, hostname: str = "") -> dict:
    """Builds a metadata dict to be stored as manifest.json inside the archive."""
    mods: list[str] = []
    mods_dir = os.path.join(windhawk_root, "ModsSource")
    if os.path.isdir(mods_dir):
        mods = [f for f in os.listdir(mods_dir) if f.endswith(".wh.cpp")]
    mod_names = [f[:-7] for f in mods]  # strip .wh.cpp suffix
    manifest = {
        "app_version":   APP_VERSION,
        "created":       datetime.datetime.now().isoformat(timespec="seconds"),
        "windhawk_root": windhawk_root,
        "portable":      portable,
        "arch":          platform.machine(),
        "mods":          mod_names,
        "mod_count":     len(mod_names),
    }
    if hostname:
        manifest["hostname"] = hostname
    return manifest


def rotate_backups(backup_folder: str, max_backups: int) -> list[str]:
    """
    Deletes the oldest backup archives when the total exceeds max_backups.
    A value of 0 disables rotation entirely. Returns deleted filenames.
    """
    if max_backups <= 0 or not os.path.isdir(backup_folder):
        return []
    archives = sorted(
        f for f in os.listdir(backup_folder)
        if f.startswith("windhawk-backup_") and f.endswith(".zip")
    )
    to_delete = archives[:-max_backups] if len(archives) > max_backups else []
    deleted: list[str] = []
    for name in to_delete:
        try:
            os.remove(os.path.join(backup_folder, name))
            deleted.append(name)
        except OSError:
            pass
    return deleted


# ---------------------------------------------------------------------------
# System helpers
# ---------------------------------------------------------------------------

def registry_key_exists(key_path: str) -> bool:
    """Returns True if the given HKLM registry key exists, False otherwise."""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path):
            return True
    except OSError:
        return False


def is_admin() -> bool:
    """Returns True if the process is running with administrator privileges."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run_as_admin() -> bool:
    """
    Re-launches this script with elevated privileges via ShellExecute.

    FIX vs 2.5.0: when running as .pyw, sys.executable is pythonw.exe.
    ShellExecuteW takes (program, parameters) separately, so we pass
    sys.executable as the program and build parameters as:
        "<script_path>" [extra args...]
    This avoids the script path being double-quoted inside a single args
    string that already starts with it.
    """
    try:
        script = os.path.abspath(sys.argv[0])
        extra  = sys.argv[1:]
        params = " ".join([f'"{script}"'] + [f'"{a}"' for a in extra])
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, None, 1
        )
        return int(result) > 32
    except Exception:
        return False


def validate_windhawk_root(path: str) -> bool:
    """
    Returns True if at least one known Windhawk sentinel exists inside the
    given root path, preventing operations on obviously wrong directories.
    """
    return any(os.path.exists(os.path.join(path, s)) for s in WINDHAWK_ROOT_SENTINELS)


def detect_windhawk_root() -> str | None:
    """
    Probes WINDHAWK_ROOT_CANDIDATES in order and returns the first path
    that passes validate_windhawk_root(), or None if nothing is found.
    """
    for candidate in WINDHAWK_ROOT_CANDIDATES:
        if validate_windhawk_root(candidate):
            return candidate
    return None


def _run_sc(action: str) -> tuple[bool, str]:
    """Runs 'sc <action> <service>' and returns (success, combined_output)."""
    try:
        r = subprocess.run(
            ["sc", action, WINDHAWK_SERVICE_NAME],
            capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except OSError as exc:
        return False, str(exc)


def stop_windhawk_service() -> tuple[bool, str]:
    """Stops the Windhawk Windows service. Returns (success, message)."""
    ok, out = _run_sc("stop")
    if ok:
        return True, "Status: Windhawk service stopped."
    if "1062" in out or "not started" in out.lower():
        return True, "Info: Windhawk service was not running - no action needed."
    return False, f"Warning: Could not stop Windhawk service: {out}"


def start_windhawk_service() -> tuple[bool, str]:
    """Starts the Windhawk Windows service. Returns (success, message)."""
    ok, out = _run_sc("start")
    if ok:
        return True, "Status: Windhawk service restarted."
    return False, f"Warning: Could not restart Windhawk service: {out}"


# ---------------------------------------------------------------------------
# Backup / Restore operations
# ---------------------------------------------------------------------------

def execute_backup_operation(
    windhawk_root: str,
    backup_folder: str,
    portable:      bool = False,
    max_backups:   int  = DEFAULT_MAX_BACKUPS,
) -> tuple[bool, str]:
    """
    Backs up Windhawk mod sources, compiled mods, a manifest.json, and
    (unless portable) the registry key into a timestamped ZIP archive.

    Service is stopped before file access and restarted afterwards via
    try/finally. Archive is validated with zipfile.testzip(). Old backups
    are rotated if max_backups > 0.
    """
    log: list[str] = []

    if not validate_windhawk_root(windhawk_root):
        return False, (
            f"ERROR: Not a valid Windhawk installation:\n{windhawk_root}\n"
            f"Expected at least one of: {', '.join(WINDHAWK_ROOT_SENTINELS)}"
        )

    try:
        os.makedirs(backup_folder, exist_ok=True)
    except OSError as exc:
        return False, f"ERROR: Could not create backup folder: {exc}"

    arch         = platform.machine()
    hostname_raw = platform.node() or socket.gethostname() or "unknown"
    hostname     = re.sub(r'[^A-Za-z0-9_-]+', '_', hostname_raw).strip('_')[:32]
    timestamp    = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_base = os.path.join(backup_folder,
                                f"windhawk-backup_{hostname}_{arch}_{timestamp}")

    if not portable:
        ok, msg = stop_windhawk_service()
        log.append(msg)
        if not ok:
            log.append("Warning: Proceeding despite service stop issue. Files may be locked.")

    try:
        with tempfile.TemporaryDirectory() as stage_dir:

            # Step 1 – Stage mod directories
            for rel, src in {
                "ModsSource":                   os.path.join(windhawk_root, "ModsSource"),
                os.path.join("Engine", "Mods"): os.path.join(windhawk_root, "Engine", "Mods"),
            }.items():
                dst = os.path.join(stage_dir, rel)
                if os.path.isdir(src):
                    try:
                        shutil.copytree(src, dst)
                        log.append(f"Status: '{rel}' staged.")
                    except OSError as exc:
                        log.append(f"Warning: Could not stage '{rel}': {exc}")
                else:
                    log.append(f"Warning: Not found, skipping: {src}")

            # Step 2 – Write manifest
            try:
                manifest_path = os.path.join(stage_dir, "manifest.json")
                with open(manifest_path, "w", encoding="utf-8") as fh:
                    json.dump(create_manifest(windhawk_root, portable, hostname), fh, indent=2)
                log.append("Status: Manifest written.")
            except OSError as exc:
                log.append(f"Warning: Could not write manifest: {exc}")

            # Step 3 – Export registry key
            if portable:
                log.append("Info: Portable mode - registry export skipped.")
            else:
                reg_file = os.path.join(stage_dir, "Windhawk.reg")
                try:
                    subprocess.run(
                        ["reg", "export",
                         f"HKLM\\{WINDHAWK_REGISTRY_KEY}", reg_file, "/y"],
                        check=True, capture_output=True, text=True,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    log.append("Status: Registry exported.")
                except subprocess.CalledProcessError as exc:
                    log.append(f"ERROR: Registry export failed: {exc.stderr.strip()}")
                    return False, "\n".join(log)

            # Step 4 – Create archive
            try:
                shutil.make_archive(archive_base, "zip", stage_dir)
            except OSError as exc:
                log.append(f"ERROR: Archive creation failed: {exc}")
                return False, "\n".join(log)

            # Step 5 – Validate archive integrity
            archive_path = f"{archive_base}.zip"
            try:
                with zipfile.ZipFile(archive_path, "r") as zf:
                    bad = zf.testzip()
                if bad is not None:
                    log.append(f"ERROR: Archive corrupt - bad entry: {bad}")
                    return False, "\n".join(log)
                log.append("Status: Archive integrity verified.")
            except zipfile.BadZipFile as exc:
                log.append(f"ERROR: Archive is not a valid ZIP: {exc}")
                return False, "\n".join(log)

            log.append(f"\nOperation Complete: Archive created at:\n{archive_path}")

    except OSError as exc:
        log.append(f"ERROR: Staging directory error: {exc}")
        return False, "\n".join(log)

    finally:
        if not portable:
            _, msg = start_windhawk_service()
            log.append(msg)

    # Step 6 – Rotate old backups
    deleted = rotate_backups(backup_folder, max_backups)
    for name in deleted:
        log.append(f"Info: Rotation - deleted old backup: {name}")

    return True, "\n".join(log)


def _resolve_nested_source(path: str) -> tuple[str, bool]:
    """
    Detects and resolves one level of same-name nesting inside a directory.
    If 'path' contains a direct subdirectory whose name matches the last
    component of 'path', that subdirectory is returned as the real source.
    """
    folder_name = os.path.basename(path)
    nested = os.path.join(path, folder_name)
    if os.path.isdir(nested):
        return nested, True
    return path, False


def execute_restore_operation(
    windhawk_root: str,
    archive_path:  str,
    portable:      bool = False,
) -> tuple[bool, str]:
    """
    Restores mod sources, compiled mods, and (unless portable) registry
    settings from a previously created ZIP archive.
    Service is stopped before file access and restarted afterwards.
    """
    log: list[str] = []

    if not validate_windhawk_root(windhawk_root):
        return False, (
            f"ERROR: Not a valid Windhawk installation:\n{windhawk_root}\n"
            f"Expected at least one of: {', '.join(WINDHAWK_ROOT_SENTINELS)}"
        )

    if not portable:
        ok, msg = stop_windhawk_service()
        log.append(msg)
        if not ok:
            log.append("Warning: Proceeding despite service stop issue. Files may be locked.")

    try:
        with tempfile.TemporaryDirectory() as stage_dir:

            # Step 1 – Extract archive
            try:
                shutil.unpack_archive(archive_path, stage_dir)
                log.append(f"Status: '{os.path.basename(archive_path)}' extracted.")
            except Exception as exc:
                log.append(f"ERROR: Extraction failed: {exc}")
                return False, "\n".join(log)

            # Arch mismatch check
            try:
                mf_path = os.path.join(stage_dir, "manifest.json")
                if os.path.isfile(mf_path):
                    with open(mf_path, "r", encoding="utf-8") as fh:
                        _mf = json.load(fh)
                    arch_bak = _mf.get("arch", "")
                    arch_cur = platform.machine()
                    if arch_bak and arch_cur and arch_bak != arch_cur:
                        log.append(
                            f"Warning: Architecture mismatch — backup was created on "
                            f"{arch_bak}, this machine is {arch_cur}. "
                            f"Compiled mods may not work."
                        )
            except Exception:
                pass

            # Step 2 – Restore mod directories
            for label, (src, dst) in {
                "ModsSource": (
                    os.path.join(stage_dir, "ModsSource"),
                    os.path.join(windhawk_root, "ModsSource"),
                ),
                os.path.join("Engine", "Mods"): (
                    os.path.join(stage_dir, "Engine", "Mods"),
                    os.path.join(windhawk_root, "Engine", "Mods"),
                ),
            }.items():
                if os.path.isdir(src):
                    real_src, was_nested = _resolve_nested_source(src)
                    if was_nested:
                        log.append(
                            f"Info: Nested structure detected in '{label}' - "
                            f"using inner folder to prevent duplication."
                        )
                    try:
                        shutil.copytree(real_src, dst, dirs_exist_ok=True)
                        log.append(f"Status: '{label}' restored.")
                    except OSError as exc:
                        log.append(f"Warning: Could not restore '{label}': {exc}")
                else:
                    log.append(f"Warning: '{label}' not found in archive, skipping.")

            # Step 3 – Import registry key
            if portable:
                log.append("Info: Portable mode - registry import skipped.")
            else:
                reg_file = os.path.join(stage_dir, "Windhawk.reg")
                if os.path.isfile(reg_file):
                    try:
                        subprocess.run(
                            ["reg", "import", reg_file],
                            check=True, capture_output=True, text=True,
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                        log.append("Status: Registry imported.")
                    except subprocess.CalledProcessError as exc:
                        log.append(f"ERROR: Registry import failed: {exc.stderr.strip()}")
                        return False, "\n".join(log)
                else:
                    log.append("Warning: Registry file not found in archive, skipping.")

    except OSError as exc:
        log.append(f"ERROR: Staging directory error: {exc}")
        return False, "\n".join(log)

    finally:
        if not portable:
            _, msg = start_windhawk_service()
            log.append(msg)

    log.append("\nOperation Complete: Restore finished successfully.")
    return True, "\n".join(log)


# =============================================================================
#                       GRAPHICAL USER INTERFACE
# =============================================================================

class WindhawkManagerApp:
    """Main application window."""

    LOG_COLOURS: dict[str, str] = {
        "info":    "RoyalBlue",
        "success": "ForestGreen",
        "warning": "DarkOrange",
        "error":   "Crimson",
    }

    # Treeview column definitions: heading text, pixel width, anchor
    TV_COLUMNS: dict[str, tuple[str, int, str]] = {
        "date": ("Date / Time",  172, "w"),
        "size": ("Size",          68, "e"),
        "kind": ("Type",          74, "center"),
        "mods": ("Mods",          48, "center"),
        "name": ("Archive Name", 300, "w"),
    }

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("820x680")
        self.root.minsize(740, 580)

        self._apply_style()

        self._cfg = load_config()

        # Sort state: col -> bool (True = ascending)
        self._sort_ascending: dict[str, bool] = {c: True for c in self.TV_COLUMNS}

        # Debounced auto-save
        self._save_timer_id: str | None = None

        outer = ttk.Frame(root, padding=PAD)
        outer.pack(fill=tk.BOTH, expand=True)

        # Top bar with Help & README button
        top_bar = ttk.Frame(outer)
        top_bar.pack(fill=tk.X, pady=(0, PAD))
        ttk.Label(top_bar, text="Windhawk Backup Utility",
                  font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        ttk.Button(top_bar, text=" Help & README ", width=16,
                   command=self._show_help_readme).pack(side=tk.RIGHT)

        self._build_config_section(outer)
        self._build_archive_section(outer)
        self._build_log_section(outer)
        self._build_status_bar(root)

        self._configure_log_tags()
        self._apply_config()
        self._refresh_backup_list()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._setup_variable_traces()

    # ------------------------------------------------------------------
    # Styling
    # ------------------------------------------------------------------

    def _apply_style(self) -> None:
        s = ttk.Style()
        s.theme_use("vista")

        s.configure("Treeview", rowheight=23, font=("Segoe UI", 9))
        s.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
        s.map("Treeview",
              background=[("selected", "#CCE4F7")],
              foreground=[("selected", "#000000")])

        s.configure("Accent.Horizontal.TProgressbar",
                    troughcolor="#E4E4E4", background="#3A9BD5", thickness=5)

        s.configure("Status.TLabel",
                    font=("Segoe UI", 8), foreground="#555555",
                    background="#F0F0F0")
        s.configure("StatusBar.TFrame",
                    background="#F0F0F0", relief="sunken")

    # ------------------------------------------------------------------
    # UI builders
    # ------------------------------------------------------------------

    def _build_config_section(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Configuration", padding=PAD)
        frame.pack(fill=tk.X, pady=(0, PAD))
        frame.columnconfigure(1, weight=1)

        lbl = {"sticky": "w", "padx": (0, PAD), "pady": 4}
        ent = {"sticky": "ew", "pady": 4}

        # Windhawk root
        self.windhawk_path_var = tk.StringVar()
        ttk.Label(frame, text="Windhawk Root:").grid(row=0, column=0, **lbl)
        ttk.Entry(frame, textvariable=self.windhawk_path_var).grid(
            row=0, column=1, **ent)
        ttk.Button(frame, text="Browse...", width=10,
                   command=self._select_windhawk_path).grid(
            row=0, column=2, padx=(PAD, 0), pady=4)

        # Backup folder
        self.backup_path_var = tk.StringVar()
        ttk.Label(frame, text="Backup Base Folder:").grid(row=1, column=0, **lbl)
        ttk.Entry(frame, textvariable=self.backup_path_var).grid(
            row=1, column=1, **ent)
        ttk.Button(frame, text="Browse...", width=10,
                   command=self._select_backup_path).grid(
            row=1, column=2, padx=(PAD, 0), pady=4)

        # Options row
        opts = ttk.Frame(frame)
        opts.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(4, 0))

        ttk.Label(opts, text="Keep last").pack(side=tk.LEFT)

        self.max_backups_var = tk.IntVar(value=DEFAULT_MAX_BACKUPS)
        ttk.Spinbox(
            opts, from_=0, to=99, width=4,
            textvariable=self.max_backups_var,
            validate="focusout",
            validatecommand=(
                parent.register(self._validate_max_backups), "%P"
            ),
        ).pack(side=tk.LEFT, padx=(4, 4))

        ttk.Label(opts, text="backups  (0 = unlimited)").pack(
            side=tk.LEFT, padx=(0, PAD * 2))

        self.use_subfolder_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opts, text="Use 'Windhawk_Backup' subfolder",
            variable=self.use_subfolder_var,
            command=self._on_use_subfolder_toggled,
        ).pack(side=tk.LEFT, padx=(0, PAD))

        self.portable_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opts, text="Portable installation",
            variable=self.portable_var,
            command=self._on_portable_toggled,
        ).pack(side=tk.LEFT, padx=(0, PAD))

        ttk.Button(opts, text="Auto-Detect", width=11,
                   command=self._auto_detect_portable).pack(side=tk.LEFT)

    def _build_archive_section(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Backup Archives", padding=PAD)
        frame.pack(fill=tk.BOTH, expand=True, pady=(0, PAD))
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        # Treeview with scrollbar
        tv_frame = ttk.Frame(frame)
        tv_frame.grid(row=0, column=0, columnspan=2, sticky="nsew")
        tv_frame.columnconfigure(0, weight=1)
        tv_frame.rowconfigure(0, weight=1)

        cols = list(self.TV_COLUMNS.keys())
        self.tree = ttk.Treeview(
            tv_frame, columns=cols, show="headings",
            selectmode="browse", height=8,
        )
        for col, (heading, width, anchor) in self.TV_COLUMNS.items():
            self.tree.heading(col, text=heading,
                              command=lambda c=col: self._sort_tree(c))
            self.tree.column(col, width=width, minwidth=40, anchor=anchor)

        vsb = ttk.Scrollbar(tv_frame, orient=tk.VERTICAL,
                            command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        self.tree.tag_configure("even", background="#FFFFFF")
        self.tree.tag_configure("odd",  background="#F2F6FA")
        self.tree.bind("<Double-1>", lambda _e: self._show_preview())

        # Action buttons
        btn = ttk.Frame(frame)
        btn.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(PAD, 0))

        self.backup_button = ttk.Button(
            btn, text="Create Backup", width=15, command=self._run_backup)
        self.backup_button.pack(side=tk.LEFT)

        self.restore_button = ttk.Button(
            btn, text="Restore Selected", width=15,
            command=self._restore_selected)
        self.restore_button.pack(side=tk.LEFT, padx=(PAD, 0))

        self.delete_button = ttk.Button(
            btn, text="Delete Selected", width=15,
            command=self._delete_selected)
        self.delete_button.pack(side=tk.LEFT, padx=(PAD, 0))

        ttk.Button(btn, text="Refresh", width=9,
                   command=self._refresh_backup_list).pack(side=tk.RIGHT)

    def _build_log_section(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Operation Log", padding=PAD)
        frame.pack(fill=tk.X, pady=(0, PAD))
        frame.columnconfigure(0, weight=1)

        hdr = ttk.Frame(frame)
        hdr.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Button(hdr, text="Export Log...", width=12,
                   command=self._export_log).pack(side=tk.RIGHT)

        self.log_widget = scrolledtext.ScrolledText(
            frame, height=7, wrap=tk.WORD, state=tk.DISABLED,
            font=("Consolas", 9), relief="flat",
            background="#FAFAFA", borderwidth=1,
        )
        self.log_widget.grid(row=1, column=0, sticky="ew")

        self.progressbar = ttk.Progressbar(
            frame, mode="indeterminate", length=200,
            style="Accent.Horizontal.TProgressbar",
        )
        self.progressbar.grid(row=2, column=0, sticky="ew", pady=(6, 0))

    def _build_status_bar(self, parent: tk.Tk) -> None:
        bar = ttk.Frame(parent, style="StatusBar.TFrame", height=22)
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(bar, textvariable=self.status_var,
                  style="Status.TLabel").pack(
            side=tk.LEFT, padx=(PAD, 0), pady=2)

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _setup_variable_traces(self) -> None:
        """Watch config variables and trigger a debounced save on any change."""
        for var in (
            self.windhawk_path_var,
            self.backup_path_var,
            self.portable_var,
            self.max_backups_var,
            self.use_subfolder_var,
        ):
            if isinstance(var, tk.BooleanVar):
                var.trace_add("write", lambda *_: self._schedule_save())
            else:
                var.trace_add("write", lambda *_, **__: self._schedule_save())

    def _schedule_save(self) -> None:
        """Reset the debounce timer; save will occur 1 second after the last change."""
        if self._save_timer_id is not None:
            self.root.after_cancel(self._save_timer_id)
        self._save_timer_id = self.root.after(1000, self._save_config_quietly)

    def _save_config_quietly(self) -> None:
        """Persist current settings to disk without logging."""
        self._save_timer_id = None
        save_config(self._collect_config())

    def _apply_config(self) -> None:
        wh_root = self._cfg.get("windhawk_root", DEFAULT_WINDHAWK_ROOT)
        if not validate_windhawk_root(wh_root):
            detected = detect_windhawk_root()
            if detected:
                wh_root = detected
                self.log(f"Info: Auto-detected Windhawk root: {wh_root}", "info")
            else:
                self.log(
                    "Warning: Windhawk root not found — could not auto-detect.",
                    "warning",
                )
        self.windhawk_path_var.set(wh_root)

        # Backward-compatible migration: strip subfolder from stored path
        raw_backup = self._cfg.get("backup_folder", DEFAULT_BACKUP_FOLDER)
        use_sub = self._cfg.get("use_subfolder", True)
        if use_sub and raw_backup and os.path.basename(os.path.normpath(raw_backup)) == BACKUP_SUBFOLDER_NAME:
            raw_backup = os.path.dirname(raw_backup) or raw_backup   # strip the subfolder
        self.backup_path_var.set(raw_backup)

        self.portable_var.set(self._cfg.get("portable", False))
        self.max_backups_var.set(
            self._cfg.get("max_backups", DEFAULT_MAX_BACKUPS))
        self.use_subfolder_var.set(
            self._cfg.get("use_subfolder", True))
        self.use_subfolder_var.set(use_sub)
        self.log("Info: Configuration loaded.", "info")

    def _collect_config(self) -> dict:
        return {
            "windhawk_root": self.windhawk_path_var.get().strip(),
            "backup_folder": self.backup_path_var.get().strip(),
            "portable":      self.portable_var.get(),
            "max_backups":   self._safe_max_backups(),
            "use_subfolder": self.use_subfolder_var.get(),
        }

    def _safe_max_backups(self) -> int:
        """Reads max_backups spinbox, clamping non-integer input to default."""
        try:
            return max(0, int(self.max_backups_var.get()))
        except (tk.TclError, ValueError):
            return DEFAULT_MAX_BACKUPS

    def _get_effective_backup_folder(self) -> str:
        base = self.backup_path_var.get().strip() or _SCRIPT_DIR
        if self.use_subfolder_var.get():
            return os.path.join(base, BACKUP_SUBFOLDER_NAME)
        return base

    def _on_use_subfolder_toggled(self) -> None:
        # When disabling subfolder usage, strip the subfolder name from the
        # displayed path so that the user sees the real base directory.
        if not self.use_subfolder_var.get():
            current = self.backup_path_var.get().strip()
            if current and os.path.basename(os.path.normpath(current)) == BACKUP_SUBFOLDER_NAME:
                new_base = os.path.dirname(current) or current
                self.backup_path_var.set(new_base)
        self._refresh_backup_list()

    def _validate_max_backups(self, value: str) -> bool:
        """Spinbox validatecommand: clamp bad input to DEFAULT on focus-out."""
        try:
            int(value)
            return True
        except ValueError:
            self.max_backups_var.set(DEFAULT_MAX_BACKUPS)
            return False

    def _on_close(self) -> None:
        # Cancel any pending debounced save and persist immediately
        if self._save_timer_id is not None:
            self.root.after_cancel(self._save_timer_id)
            self._save_timer_id = None
        save_config(self._collect_config())
        self.root.destroy()

    # ------------------------------------------------------------------
    # Treeview helpers
    # ------------------------------------------------------------------

    def _refresh_backup_list(self) -> None:
        self.tree.delete(*self.tree.get_children())
        backups = list_backups(self._get_effective_backup_folder())
        for i, b in enumerate(backups):
            self.tree.insert(
                "", tk.END, iid=b["path"],
                values=(b["date"], b["size"], b["kind"],
                        b["mods"], b["name"]),
                tags=("even" if i % 2 == 0 else "odd",),
            )
        count = len(backups)
        self._set_status(
            f"{count} backup{'s' if count != 1 else ''} found."
            if count else "No backups found in the selected folder."
        )

    def _sort_tree(self, col: str) -> None:
        """
        Sorts treeview rows by the clicked column.
        Toggles ascending/descending on repeated clicks.
        """
        ascending = self._sort_ascending.get(col, True)
        items = [
            (self.tree.set(iid, col), iid)
            for iid in self.tree.get_children()
        ]
        items.sort(key=lambda x: x[0], reverse=not ascending)
        for i, (_val, iid) in enumerate(items):
            self.tree.move(iid, "", i)
            self.tree.item(iid, tags=("even" if i % 2 == 0 else "odd",))
        # Flip for next click; reset all others to ascending
        for c in self._sort_ascending:
            self._sort_ascending[c] = True
        self._sort_ascending[col] = not ascending

    def _selected_archive_path(self) -> str | None:
        sel = self.tree.selection()
        return sel[0] if sel else None

    # ------------------------------------------------------------------
    # Browse dialogs
    # ------------------------------------------------------------------

    def _safe_initial_dir(self, path: str) -> str:
        return path if os.path.isdir(path) else os.path.expanduser("~")

    def _select_windhawk_path(self) -> None:
        path = filedialog.askdirectory(
            title="Select Windhawk Installation Directory",
            initialdir=self._safe_initial_dir(self.windhawk_path_var.get()),
        )
        if path:
            self.windhawk_path_var.set(os.path.normpath(path))

    def _select_backup_path(self) -> None:
        path = filedialog.askdirectory(
            title="Select Backup Destination Folder",
            initialdir=self._safe_initial_dir(self.backup_path_var.get()),
        )
        if path:
            self.backup_path_var.set(os.path.normpath(path))
            self._refresh_backup_list()

    # ------------------------------------------------------------------
    # Portable / auto-detect
    # ------------------------------------------------------------------

    def _auto_detect_portable(self) -> None:
        if registry_key_exists(WINDHAWK_REGISTRY_KEY):
            self.portable_var.set(False)
            self.log(
                "Info: Registry key found - standard installation detected. "
                "Portable mode disabled.", "info")
        else:
            self.portable_var.set(True)
            self.log(
                "Info: Registry key not found - portable installation assumed. "
                "Portable mode enabled.", "warning")

    def _on_portable_toggled(self) -> None:
        if self.portable_var.get():
            self.log(
                "Info: Portable mode enabled - registry steps will be skipped.",
                "warning")
        else:
            self.log(
                "Info: Portable mode disabled - registry steps will be included.",
                "info")

    # ------------------------------------------------------------------
    # Logging and status
    # ------------------------------------------------------------------

    def _configure_log_tags(self) -> None:
        for tag, colour in self.LOG_COLOURS.items():
            self.log_widget.tag_config(tag, foreground=colour)

    def log(self, message: str, level: str = "info") -> None:
        """Appends a timestamped message to the log widget (thread-safe)."""
        ts   = datetime.datetime.now().strftime("%H:%M:%S")
        text = f"[{ts}]  {message}"

        def _write() -> None:
            self.log_widget.config(state=tk.NORMAL)
            self.log_widget.insert(tk.END, text + "\n", (level,))
            self.log_widget.see(tk.END)
            self.log_widget.config(state=tk.DISABLED)

        self.root.after(0, _write)

    def _set_status(self, text: str) -> None:
        self.root.after(0, lambda: self.status_var.set(text))

    def _export_log(self) -> None:
        default_name = (
            f"wsbu-log-{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        )
        path = filedialog.asksaveasfilename(
            title="Export Operation Log",
            defaultextension=".txt",
            filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")],
            initialfile=default_name,
        )
        if not path:
            return
        content = self.log_widget.get("1.0", tk.END)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            self.log(f"Info: Log exported to: {path}", "info")
        except OSError as exc:
            messagebox.showerror("Export Failed", str(exc))

    # ------------------------------------------------------------------
    # Backup preview
    # ------------------------------------------------------------------

    def _show_preview(self) -> None:
        archive = self._selected_archive_path()
        if not archive:
            return

        manifest: dict = {}
        try:
            with zipfile.ZipFile(archive, "r") as zf:
                if "manifest.json" in zf.namelist():
                    manifest = json.loads(
                        zf.read("manifest.json").decode("utf-8"))
        except Exception as exc:
            messagebox.showerror("Preview Failed",
                                 f"Could not read archive:\n{exc}")
            return

        win = tk.Toplevel(self.root)
        win.title(f"Backup Details  -  {os.path.basename(archive)}")
        win.geometry("480x540")
        win.minsize(400, 460)
        win.resizable(True, True)
        win.grab_set()

        outer = ttk.Frame(win, padding=PAD)
        outer.pack(fill=tk.BOTH, expand=True)

        def _row(parent: ttk.Frame, label: str, value: str, row: int) -> None:
            ttk.Label(parent, text=label,
                      font=("Segoe UI", 9, "bold"), anchor="w").grid(
                row=row, column=0, sticky="w", padx=(0, PAD), pady=3)
            ttk.Label(parent, text=value, anchor="w").grid(
                row=row, column=1, sticky="ew", pady=3)

        meta = ttk.LabelFrame(outer, text="Archive Information", padding=PAD)
        meta.pack(fill=tk.X, pady=(0, PAD))
        meta.columnconfigure(1, weight=1)

        size_bytes = os.path.getsize(archive)

        if manifest:
            _row(meta, "Created:",         manifest.get("created",     "-"), 0)
            _row(meta, "Utility Version:", manifest.get("app_version", "-"), 1)
            _row(meta, "Architecture:",    manifest.get("arch", "Unknown"), 2)
            _row(meta, "Machine:",         manifest.get("hostname", "-"), 3)
            _row(meta, "Installation:",    "Portable" if manifest.get("portable") else "Standard", 4)
            _row(meta, "Mod Count:",       str(manifest.get("mod_count", "-")), 5)
            _row(meta, "Archive Size:",    _format_size(size_bytes), 6)
        else:
            _row(meta, "Archive:", os.path.basename(archive), 0)
            _row(meta, "Size:",    _format_size(size_bytes),  1)
            ttk.Label(
                meta,
                text="No manifest.json found in this archive (legacy backup).",
                foreground="DarkOrange",
            ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(PAD, 0))

        mods: list[str] = manifest.get("mods", [])
        mod_frame = ttk.LabelFrame(
            outer,
            text=f"Installed Mods  ({len(mods)})" if mods else "Installed Mods",
            padding=PAD,
        )
        mod_frame.pack(fill=tk.BOTH, expand=True, pady=(0, PAD))
        mod_frame.rowconfigure(0, weight=1)
        mod_frame.columnconfigure(0, weight=1)

        if mods:
            lb_frame = ttk.Frame(mod_frame)
            lb_frame.grid(sticky="nsew")
            lb_frame.rowconfigure(0, weight=1)
            lb_frame.columnconfigure(0, weight=1)

            lb = tk.Listbox(
                lb_frame, font=("Consolas", 9),
                selectmode=tk.BROWSE, relief="flat", borderwidth=0,
                background="#FAFAFA", activestyle="none",
                highlightthickness=1, highlightcolor="#CCE4F7",
                highlightbackground="#DDDDDD",
            )
            sb = ttk.Scrollbar(lb_frame, orient=tk.VERTICAL,
                               command=lb.yview)
            lb.configure(yscrollcommand=sb.set)
            lb.grid(row=0, column=0, sticky="nsew")
            sb.grid(row=0, column=1, sticky="ns")

            for i, mod in enumerate(sorted(mods)):
                display = mod[:-7] if mod.endswith(".wh.cpp") else mod
                lb.insert(tk.END, f"  {display}")
                lb.itemconfig(i,
                              background="#FFFFFF" if i % 2 == 0
                              else "#F2F6FA")
        else:
            ttk.Label(
                mod_frame,
                text="No mod list available (legacy backup).",
                foreground="DarkOrange",
            ).grid(sticky="w")

        ttk.Button(outer, text="Close", width=10,
                   command=win.destroy).pack(anchor="e")

    # ------------------------------------------------------------------
    # About / Info
    # ------------------------------------------------------------------

    def _show_help_readme(self) -> None:
        """Opens the Help & README dialog with tabbed documentation."""
        candidates_text = "\n".join(
            f"    {c}" for c in WINDHAWK_ROOT_CANDIDATES
        )
        win = tk.Toplevel(self.root)
        win.title("Help & README - Windhawk Backup Utility")
        win.geometry("600x500")
        win.minsize(520, 420)
        win.resizable(True, True)
        win.grab_set()

        notebook = ttk.Notebook(win)
        notebook.pack(fill=tk.BOTH, expand=True, padx=PAD, pady=PAD)

        # ---- Tab 1: Overview ----
        tab1 = ttk.Frame(notebook)
        notebook.add(tab1, text="Overview")
        txt1 = scrolledtext.ScrolledText(
            tab1, wrap=tk.WORD, font=("Segoe UI", 9),
            relief="flat", background="#FAFAFA", state=tk.NORMAL,
        )
        txt1.pack(fill=tk.BOTH, expand=True)
        txt1.insert(tk.END, (
            "WHAT THIS TOOL DOES\n"
            "\n"
            "Backs up and restores your Windhawk configuration by\n"
            "stopping the Windhawk service, copying mod sources,\n"
            "compiled mods, and registry data into a timestamped ZIP,\n"
            "then restarting the service.\n"
            "\n"
            "Backup filename pattern:\n"
            "  windhawk-backup_{hostname}_{arch}_{timestamp}.zip\n"
            "  Example: windhawk-backup_DESKTOP-ABC123_AMD64_20260115_143022.zip\n"
            "\n"
            "Manifest inside each archive contains:\n"
            "  \u2022 hostname, architecture, mod list, portable flag,\n"
            "    creation time, and the utility version used.\n"
        ))
        txt1.config(state=tk.DISABLED)

        # ---- Tab 2: What is backed up ----
        tab2 = ttk.Frame(notebook)
        notebook.add(tab2, text="Backed up files")
        txt2 = scrolledtext.ScrolledText(
            tab2, wrap=tk.WORD, font=("Segoe UI", 9),
            relief="flat", background="#FAFAFA", state=tk.NORMAL,
        )
        txt2.pack(fill=tk.BOTH, expand=True)
        txt2.insert(tk.END, (
            "FILES BACKED UP\n"
            "  %ProgramData%\\Windhawk\\ModsSource\\\n"
            "      Mod source code (.wh.cpp)\n"
            "  %ProgramData%\\Windhawk\\Engine\\Mods\\\n"
            "      Compiled mod DLLs  \u2190 architecture-specific!\n"
            "\n"
            "REGISTRY (standard install)\n"
            "  HKLM\\SOFTWARE\\Windhawk  (full key tree)\n"
            "  Covers: Engine\\Mods (mod settings / enabled states),\n"
            "          Engine\\ModsWritable, Settings (exclusion list)\n"
            "\n"
            "PORTABLE INSTALLATIONS\n"
            "  Registry is NOT included. Only the mod folders\n"
            "  inside the Windhawk root are archived.\n"
        ))
        txt2.config(state=tk.DISABLED)

        # ---- Tab 3: Registry source & backup location ----
        tab3 = ttk.Frame(notebook)
        notebook.add(tab3, text="Registry source")
        txt3 = scrolledtext.ScrolledText(
            tab3, wrap=tk.WORD, font=("Segoe UI", 9),
            relief="flat", background="#FAFAFA", state=tk.NORMAL,
        )
        txt3.pack(fill=tk.BOTH, expand=True)
        txt3.insert(tk.END, (
            "REGISTRY SOURCE KEY\n"
            "  HKLM\\SOFTWARE\\Windhawk\n"
            "\n"
            "How it's backed up:\n"
            "  During archive creation, we run:\n"
            "    reg export HKLM\\SOFTWARE\\Windhawk Windhawk.reg /y\n"
            "  The resulting .reg file is stored at the root of the ZIP.\n"
            "\n"
            "How it's restored:\n"
            "  When you restore a backup, we run:\n"
            "    reg import Windhawk.reg\n"
            "  This writes all values back into the exact same registry path.\n"
            "\n"
            "NOTE:\n"
            "  GitHub issue #639 \u2014 \"Stop using the registry as a way\n"
            "  to store settings for mods\" is open. If Windhawk migrates\n"
            "  mod settings to plain files, the registry step will no longer\n"
            "  be needed. This utility will adapt when that happens.\n"
        ))
        txt3.config(state=tk.DISABLED)

        # ---- Tab 4: Restore notes ----
        tab4 = ttk.Frame(notebook)
        notebook.add(tab4, text="Restore notes")
        txt4 = scrolledtext.ScrolledText(
            tab4, wrap=tk.WORD, font=("Segoe UI", 9),
            relief="flat", background="#FAFAFA", state=tk.NORMAL,
        )
        txt4.pack(fill=tk.BOTH, expand=True)
        txt4.insert(tk.END, (
            "ARCHITECTURE NOTE\n"
            "  Compiled DLLs in Engine\\Mods are CPU-specific.\n"
            "  AMD64 backups will NOT work on ARM64 and vice versa.\n"
            "  Architecture is recorded in each backup's manifest.json\n"
            "  and checked automatically on restore. A warning is\n"
            "  shown if there is a mismatch.\n"
            "\n"
            "PORTABLE vs STANDARD\n"
            "  Restoring a portable backup onto a standard install\n"
            "  will NOT touch the registry.\n"
            "\n"
            "VERSION COMPATIBILITY\n"
            "  Written and tested against:\n"
            "    Windhawk v1.7.3  |  Dec 8, 2025  |  commit b59b38c\n"
            "\n"
            "ROOT AUTO-DETECT CANDIDATES (probed in order)\n"
            f"{candidates_text}\n"
        ))
        txt4.config(state=tk.DISABLED)

        ttk.Button(win, text="Close", width=10,
                   command=win.destroy).pack(pady=(0, PAD))

    # ------------------------------------------------------------------
    # Operation control
    # ------------------------------------------------------------------

    def _set_controls_enabled(self, enabled: bool) -> None:
        state = tk.NORMAL if enabled else tk.DISABLED
        for btn in (self.backup_button, self.restore_button,
                    self.delete_button):
            btn.config(state=state)
        if enabled:
            self.progressbar.stop()
            self.progressbar.config(value=0)
        else:
            self.progressbar.start(12)

    # ------------------------------------------------------------------
    # Backup
    # ------------------------------------------------------------------

    def _run_backup(self) -> None:
        cfg = self._collect_config()
        effective_folder = self._get_effective_backup_folder()
        if not cfg["windhawk_root"] or not self.backup_path_var.get().strip():
            messagebox.showwarning(
                "Configuration Incomplete",
                "Please specify both the Windhawk root and a backup base folder.",
            )
            return

        self.log("\n--- Backup started ---", "info")
        self._set_status("Backup in progress...")
        self._set_controls_enabled(False)

        def _worker() -> None:
            success, message = execute_backup_operation(
                cfg["windhawk_root"],
                effective_folder,
                portable=cfg["portable"],
                max_backups=cfg["max_backups"],
            )
            self.root.after(0,
                lambda: self._on_backup_done(success, message))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_backup_done(self, success: bool, message: str) -> None:
        self._set_controls_enabled(True)
        self.log(message, "success" if success else "error")
        self._set_status(
            "Backup completed." if success else "Backup failed - see log.")
        self._refresh_backup_list()
        if success:
            messagebox.showinfo("Backup Succeeded",
                                "The backup completed successfully.")
        else:
            messagebox.showerror("Backup Failed",
                                 "An error occurred. Please review the log.")

    # ------------------------------------------------------------------
    # Restore
    # ------------------------------------------------------------------

    def _restore_selected(self) -> None:
        archive = self._selected_archive_path()
        if not archive:
            messagebox.showinfo("No Selection",
                                "Please select a backup from the list to restore.")
            return

        wh_path = self.windhawk_path_var.get().strip()
        if not wh_path:
            messagebox.showwarning("Configuration Incomplete",
                                   "Please specify the Windhawk root path.")
            return

        name = os.path.basename(archive)
        if not messagebox.askyesno(
            "Confirm Restore",
            f"Restore from:\n{name}\n\n"
            f"This will overwrite existing mod files. Continue?",
        ):
            return

        self.log(f"\n--- Restore started: {name} ---", "info")
        self._set_status("Restore in progress...")
        self._set_controls_enabled(False)

        portable = self.portable_var.get()

        def _worker() -> None:
            success, message = execute_restore_operation(
                wh_path, archive, portable)
            self.root.after(0,
                lambda: self._on_restore_done(success, message))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_restore_done(self, success: bool, message: str) -> None:
        self._set_controls_enabled(True)
        self.log(message, "success" if success else "error")
        self._set_status(
            "Restore completed." if success else "Restore failed - see log.")
        if success:
            messagebox.showinfo("Restore Succeeded",
                                "The restore completed successfully.")
        else:
            messagebox.showerror("Restore Failed",
                                 "An error occurred. Please review the log.")

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def _delete_selected(self) -> None:
        archive = self._selected_archive_path()
        if not archive:
            messagebox.showinfo("No Selection",
                                "Please select a backup from the list to delete.")
            return

        name = os.path.basename(archive)
        if not messagebox.askyesno(
            "Confirm Delete",
            f"Permanently delete:\n{name}\n\nThis cannot be undone.",
            icon="warning",
        ):
            return

        try:
            os.remove(archive)
            self.log(f"Info: Deleted backup: {name}", "info")
        except OSError as exc:
            messagebox.showerror("Delete Failed", str(exc))
            return

        self._refresh_backup_list()
        self._set_status(f"Deleted: {name}")


# =============================================================================
#                          APPLICATION ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    if not is_admin():
        if not run_as_admin():
            # messagebox requires a root window if tk hasn't started yet
            _tmp = tk.Tk()
            _tmp.withdraw()
            messagebox.showerror(
                "Elevation Failed",
                "This application requires administrator privileges and "
                "could not elevate.\n"
                "Please re-run it manually as Administrator.",
            )
            _tmp.destroy()
        sys.exit()

    root = tk.Tk()
    WindhawkManagerApp(root)
    root.mainloop()
