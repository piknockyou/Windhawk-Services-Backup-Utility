# =============================================================================
#  Windhawk Service Management Utility
#  Based on wsbu.py by scorpion421 (GPL)
# =============================================================================

from __future__ import annotations

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
import traceback
import winreg
import zipfile
from tkinter import filedialog, messagebox, scrolledtext
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

# pyright: reportAny=false
# pyright: reportExplicitAny=false
# pyright: reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false
# pyright: reportUnknownArgumentType=false
# pyright: reportUnknownLambdaType=false
# pyright: reportUnknownParameterType=false
# pyright: reportMissingParameterType=false
# pyright: reportUninitializedInstanceVariable=false
# pyright: reportUnannotatedClassAttribute=false
# pyright: reportMissingTypeStubs=false
# pyright: reportRedeclaration=false
# pyright: reportImplicitStringConcatenation=false
# pyright: reportDeprecated=false
# pyright: reportUnnecessaryTypeIgnoreComment=false
# pyright: reportPrivateUsage=false
# pyright: reportUnusedCallResult=false
# pyright: reportMissingTypeArgument=false
# pyright: reportAttributeAccessIssue=false
# pyright: reportArgumentType=false
# pyright: reportCallIssue=false
# pyright: reportOperatorIssue=false
# pyright: reportGeneralTypeIssues=false

# ---------------------------------------------------------------------------
# Application constants
# ---------------------------------------------------------------------------
APP_VERSION = "2.8.10-pyw"
APP_TITLE = f"Windhawk Service Management Utility v{APP_VERSION}"

WINDHAWK_REGISTRY_KEY = r"SOFTWARE\Windhawk"
WINDHAWK_SERVICE_NAME = "Windhawk"
WINDHAWK_ROOT_SENTINELS = ("ModsSource", os.path.join("Engine", "Mods"), "windhawk.exe")

DEFAULT_WINDHAWK_ROOT = os.path.expandvars(r"%programdata%\Windhawk")
_SCRIPT_DIR = (
    os.path.dirname(os.path.abspath(sys.argv[0]))
    if sys.argv and sys.argv[0]
    else os.path.expanduser("~")
)
DEFAULT_BACKUP_FOLDER = _SCRIPT_DIR
DEFAULT_MAX_BACKUPS = 10

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


# ---------------------------------------------------------------------------
# Tooltip helper
# ---------------------------------------------------------------------------


class ToolTip:
    """Lightweight hover tooltip for any widget."""

    def __init__(self, widget: tk.Widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self.tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)
        widget.bind("<ButtonPress>", self._hide)

    def _show(self, _event=None) -> None:
        if self.tip or not self.text:
            return
        try:
            wx = self.widget.winfo_rootx()
            wy = self.widget.winfo_rooty()
            wh = self.widget.winfo_height()
        except Exception:
            return

        self.tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)

        lbl = tk.Label(
            tw,
            text=self.text,
            justify="left",
            background="#ffffe0",
            foreground="black",
            relief="solid",
            borderwidth=1,
            font=("Segoe UI", 9),
        )
        lbl.pack(ipadx=4, ipady=2)

        tw.update_idletasks()
        h = tw.winfo_reqheight()

        x = wx + 10
        y = wy - h - 4
        if y < 0:
            y = wy + wh + 4

        tw.wm_geometry(f"+{x}+{y}")

    def _hide(self, _event=None) -> None:
        if self.tip:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


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
        "portable": False,
        "max_backups": DEFAULT_MAX_BACKUPS,
        "verbose_logging": False,
        "auto_refresh": True,
        "exclude_stale_dlls": True,
        "geometry": "820x680",
        "window_state": "normal",
        "tree_column_widths": {},
        "log_window_geometry": "1100x700",
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
    legacy_dir = os.path.expandvars(r"%appdata%\Windhawk_Backup_Utility")
    legacy_file = os.path.join(legacy_dir, "config.json")
    try:
        with open(legacy_file, "r", encoding="utf-8") as fh:
            stored = json.load(fh)
        defaults.update(stored)
        # Migrate to the new location (best effort – failure is non‑fatal)
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
                json.dump(defaults, fh, indent=2)

            # Invalidate the legacy config after successful migration so
            # deleted local configs don't resurrect old settings forever.
            try:
                migrated_path = legacy_file + ".migrated"
                if os.path.exists(migrated_path):
                    os.remove(migrated_path)

                os.replace(legacy_file, migrated_path)
            except OSError:
                pass

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
        (
            n
            for n in os.listdir(backup_folder)
            if n.startswith("windhawk-backup_") and n.endswith(".zip")
        ),
        reverse=True,
    )
    for name in names:
        full_path = os.path.join(backup_folder, name)
        try:
            size = os.path.getsize(full_path)
            mtime = os.path.getmtime(full_path)
            dt = datetime.datetime.fromtimestamp(mtime)

            manifest: dict = {}
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
                            1
                            for n in normalized
                            if n.startswith("ModsSource/") and n.endswith(".wh.cpp")
                        )
            except Exception:
                pass

            mods_display = str(
                manifest.get("mod_count", mod_count)
                if "mod_count" in manifest or mod_count is not None
                else "-"
            )

            results.append(
                {
                    "name": name,
                    "path": full_path,
                    "date": dt.strftime("%Y-%m-%d  %H:%M:%S"),
                    "size": _format_size(size),
                    "kind": "Portable" if manifest.get("portable") else "Standard",
                    "mods": mods_display,
                }
            )
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
        "app_version": APP_VERSION,
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "windhawk_root": windhawk_root,
        "portable": portable,
        "arch": platform.machine(),
        "mods": mod_names,
        "mod_count": len(mod_names),
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
        f
        for f in os.listdir(backup_folder)
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
        extra = sys.argv[1:]
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
            capture_output=True,
            text=True,
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
    portable: bool = False,
    max_backups: int = DEFAULT_MAX_BACKUPS,
    verbose: bool = False,
    exclude_stale_dlls: bool = True,
    live_log_callback=None,
) -> tuple[bool, list[tuple[str, str]]]:
    """
    Backs up Windhawk mod sources, compiled mods, a manifest.json, and
    (unless portable) the registry key into a timestamped ZIP archive.

    Service is stopped before file access and restarted afterwards via
    try/finally. Archive is validated with zipfile.testzip(). Old backups
    are rotated if max_backups > 0.
    """
    log: list[tuple[str, str]] = []

    def emit(level: str, message: str) -> None:
        log.append((level, message))

        if live_log_callback:
            try:
                # callback signature is: log(message, level)
                live_log_callback(message, level)
            except Exception:
                pass

    backed_up_sources = 0
    backed_up_mod_dlls = 0
    backed_up_runtime_files = 0
    stale_groups = 0
    stale_dlls_excluded = 0

    if not validate_windhawk_root(windhawk_root):
        msg = (
            f"ERROR: Not a valid Windhawk installation:\n{windhawk_root}\n"
            f"Expected at least one of: {', '.join(WINDHAWK_ROOT_SENTINELS)}"
        )

        emit("error", msg)
        return False, log

    try:
        os.makedirs(backup_folder, exist_ok=True)
    except OSError as exc:
        emit("error", f"ERROR: Could not create backup folder: {exc}")
        return False, log

    if os.path.commonpath(
        [os.path.abspath(windhawk_root), os.path.abspath(backup_folder)]
    ) == os.path.abspath(windhawk_root):
        emit(
            "warning",
            "Warning: Backup folder is INSIDE the Windhawk directory.\n"
            "This can cause extremely slow backups or recursive archive growth.\n"
            "Recommended: choose a folder outside the Windhawk installation.",
        )

    arch = platform.machine()
    hostname_raw = platform.node() or socket.gethostname() or "unknown"
    hostname = re.sub(r"[^A-Za-z0-9_-]+", "_", hostname_raw).strip("_")[:32]
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_base = os.path.join(
        backup_folder, f"windhawk-backup_{hostname}_{arch}_{timestamp}"
    )

    if not portable:
        emit("info", "Info: Stopping Windhawk service...")
        ok, msg = stop_windhawk_service()
        emit("success" if ok else "warning", msg)

        if not ok:
            emit(
                "warning",
                "Warning: Proceeding despite service stop issue. Files may be locked.",
            )

    try:
        with tempfile.TemporaryDirectory() as stage_dir:
            emit("header", "=== BACKUP STARTED ===")
            emit("header", "--- Staging Mod Directories ---")

            # Step 1 – Stage mod directories
            for rel, src in {
                "ModsSource": os.path.join(windhawk_root, "ModsSource"),
                os.path.join("Engine", "Mods"): os.path.join(
                    windhawk_root, "Engine", "Mods"
                ),
            }.items():
                dst = os.path.join(stage_dir, rel)
                if os.path.isdir(src):
                    try:
                        emit("info", f"Info: Copying '{rel}' ...")
                        shutil.copytree(src, dst)
                        emit("success", f"Status: '{rel}' staged.")

                        if verbose or exclude_stale_dlls:
                            dll_versions: dict[
                                tuple[str, str], list[tuple[str, str]]
                            ] = {}

                            for root, _dirs, files in os.walk(dst):
                                for file in files:
                                    rel_file = os.path.relpath(
                                        os.path.join(root, file),
                                        stage_dir,
                                    )
                                    if rel.startswith("ModsSource"):
                                        backed_up_sources += 1

                                        if verbose:
                                            log.append(
                                                (
                                                    "verbose",
                                                    f"Verbose: Backed up '{rel_file}'",
                                                )
                                            )

                                    else:
                                        m = re.match(
                                            r"(.+?)_(\d+(?:\.\d+)*)_(\d+)\.dll$",
                                            file,
                                            re.IGNORECASE,
                                        )

                                        if m:
                                            mod_id = m.group(1)
                                            version = m.group(2)

                                            arch_dir = os.path.basename(
                                                os.path.dirname(
                                                    os.path.join(root, file)
                                                )
                                            )

                                            dll_versions.setdefault(
                                                (arch_dir, mod_id), []
                                            ).append((version, file))

                                            backed_up_mod_dlls += 1
                                        else:
                                            backed_up_runtime_files += 1

                                        if verbose:
                                            log.append(
                                                (
                                                    "verbose",
                                                    f"Verbose: Backed up '{rel_file}'",
                                                )
                                            )

                            stale_to_remove: list[str] = []

                            for (_arch, mod_id), entries in dll_versions.items():
                                if len(entries) > 1:
                                    entries_sorted = sorted(entries)

                                    for _ver, old_file in entries_sorted[:-1]:
                                        stale_to_remove.append(old_file)

                            if stale_to_remove:
                                log.append(("header", "--- Stale DLL Analysis ---"))

                            grouped_stale: dict[str, list[str]] = {}

                            for stale_name in stale_to_remove:
                                m = re.match(
                                    r"(.+?)_(\d+(?:\.\d+)*)_(\d+)\.dll$",
                                    stale_name,
                                    re.IGNORECASE,
                                )

                                if not m:
                                    continue

                                mod_id = m.group(1)

                                grouped_stale.setdefault(mod_id, []).append(stale_name)

                            for mod_id, dlls in grouped_stale.items():
                                stale_groups += 1

                                log.append(
                                    (
                                        "warning",
                                        f"Warning: Multiple compiled DLL versions detected for '{mod_id}': "
                                        f"{', '.join(sorted(dlls))}",
                                    )
                                )

                            if exclude_stale_dlls:
                                for root, _dirs, files in os.walk(dst):
                                    for file in files:
                                        if file in stale_to_remove:
                                            try:
                                                os.remove(os.path.join(root, file))
                                                stale_dlls_excluded += 1

                                                log.append(
                                                    (
                                                        "info",
                                                        f"Info: Excluded stale DLL from backup: {file}",
                                                    )
                                                )

                                                backed_up_mod_dlls -= 1

                                            except OSError as exc:
                                                log.append(
                                                    (
                                                        "warning",
                                                        f"Warning: Could not exclude stale DLL '{file}': {exc}",
                                                    )
                                                )
                    except OSError as exc:
                        log.append(
                            ("warning", f"Warning: Could not stage '{rel}': {exc}")
                        )
                else:
                    log.append(("warning", f"Warning: Not found, skipping: {src}"))

            emit("info", "Info: Entering manifest stage ...")

            # Step 2 – Write manifest
            try:
                emit("info", "Info: Writing manifest.json ...")

                manifest_path = os.path.join(stage_dir, "manifest.json")
                with open(manifest_path, "w", encoding="utf-8") as fh:
                    json.dump(
                        create_manifest(windhawk_root, portable, hostname), fh, indent=2
                    )

                emit("success", "Status: Manifest written.")
            except OSError as exc:
                log.append(("warning", f"Warning: Could not write manifest: {exc}"))

            emit("info", "Info: Entering registry export stage ...")

            # Step 3 – Export registry key
            if portable:
                log.append(("info", "Info: Portable mode - registry export skipped."))
            else:
                emit("info", "Info: Exporting registry ...")

                reg_file = os.path.join(stage_dir, "Windhawk.reg")
                try:
                    subprocess.run(
                        [
                            "reg",
                            "export",
                            f"HKLM\\{WINDHAWK_REGISTRY_KEY}",
                            reg_file,
                            "/y",
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=30,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )

                    emit("success", "Status: Registry exported.")
                except subprocess.CalledProcessError as exc:
                    log.append(
                        (
                            "error",
                            f"ERROR: Registry export failed: {exc.stderr.strip()}",
                        )
                    )
                    return False, log

            emit("info", "Info: Entering archive creation stage ...")

            # Step 4 – Create archive
            try:
                emit("info", "Info: Creating ZIP archive ...")
                emit("verbose", f"Verbose: Stage dir = {stage_dir}")
                emit("verbose", f"Verbose: Archive base = {archive_base}")

                archive_result = shutil.make_archive(archive_base, "zip", stage_dir)

                emit("success", f"Status: ZIP archive created: {archive_result}")

                emit("info", "Info: Checking whether ZIP file exists ...")

                if not os.path.isfile(f"{archive_base}.zip"):
                    emit(
                        "error",
                        "ERROR: ZIP creation reported success but file does not exist.",
                    )

                    emit("error", f"ERROR: Expected file:\n{archive_base}.zip")

                    return False, log

            except Exception as exc:
                emit("error", f"ERROR: Archive creation failed: {exc}")
                emit("error", traceback.format_exc())
                return False, log

            emit("info", "Info: Entering archive verification stage ...")

            # Step 5 – Validate archive integrity
            archive_path = f"{archive_base}.zip"

            emit("info", "Info: Verifying archive integrity ...")

            try:
                with zipfile.ZipFile(archive_path, "r") as zf:
                    bad = zf.testzip()
                if bad is not None:
                    log.append(("error", f"ERROR: Archive corrupt - bad entry: {bad}"))
                    return False, log
                log.append(("success", "Status: Archive integrity verified."))
            except zipfile.BadZipFile as exc:
                log.append(("error", f"ERROR: Archive is not a valid ZIP: {exc}"))
                return False, log

            emit("header", "--- Backup Summary ---")
            emit("summary", f"Summary: Source files backed up: {backed_up_sources}")
            emit("summary", f"Summary: Mod DLLs backed up: {backed_up_mod_dlls}")
            emit(
                "summary",
                f"Summary: Runtime files backed up: {backed_up_runtime_files}",
            )
            emit("summary", f"Summary: Stale DLL groups detected: {stale_groups}")
            emit("summary", f"Summary: Stale DLLs excluded: {stale_dlls_excluded}")

            emit("success", f"Operation Complete: Archive created at:\n{archive_path}")

    except Exception as exc:
        emit("error", f"ERROR: Backup exception: {exc}")
        emit("error", traceback.format_exc())
        return False, log

    finally:
        if not portable:
            emit("info", "Info: Restarting Windhawk service ...")
            _, msg = start_windhawk_service()
            emit("success", msg)

    # Step 6 – Rotate old backups
    deleted = rotate_backups(backup_folder, max_backups)
    for name in deleted:
        log.append(("info", f"Info: Rotation - deleted old backup: {name}"))

    return True, log


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


def cleanup_windhawk_mod_state(
    windhawk_root: str,
    portable: bool = False,
    verbose: bool = False,
) -> tuple[bool, list[tuple[str, str]]]:
    """
    Removes existing Windhawk mod state so restores can start from a
    deterministic clean baseline instead of merging into stale files.
    """
    log: list[tuple[str, str]] = []

    if not validate_windhawk_root(windhawk_root):
        return False, [
            (
                "error",
                f"ERROR: Not a valid Windhawk installation:\n{windhawk_root}\n"
                f"Expected at least one of: {', '.join(WINDHAWK_ROOT_SENTINELS)}",
            )
        ]

    # Windhawk compiled mod DLL naming convention:
    #   mod-name_<version>_<hash>.dll
    #
    # Examples:
    #   taskbar-button-click_1.0.9_998577.dll
    #   disable-rounded-corners_1.0.1_354776.dll
    #
    # We intentionally remove ONLY files matching this pattern instead of
    # maintaining a hardcoded runtime keep-list. This is more future-proof
    # because Windhawk runtime/compiler support files may change over time.
    mod_binary_pattern = re.compile(
        r".+?_\d+(?:\.\d+)*_\d+\.(?:dll|whl)$",
        re.IGNORECASE,
    )

    if not portable:
        ok, msg = stop_windhawk_service()
        log.append(("success" if ok else "warning", msg))
        if not ok:
            log.append(("warning", "Warning: Proceeding despite service stop issue."))

    try:
        # Remove source files
        mods_source = os.path.join(windhawk_root, "ModsSource")
        if os.path.isdir(mods_source):
            for name in os.listdir(mods_source):
                path = os.path.join(mods_source, name)
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                        if verbose:
                            log.append(
                                ("verbose", f"Verbose: Removed source file '{path}'")
                            )
                except OSError as exc:
                    log.append(
                        (
                            "warning",
                            f"Warning: Could not remove source file '{name}': {exc}",
                        )
                    )

        # Remove compiled mod DLLs but preserve Windhawk runtime files
        for arch in ("32", "64"):
            mods_dir = os.path.join(windhawk_root, "Engine", "Mods", arch)
            if not os.path.isdir(mods_dir):
                continue

            for name in os.listdir(mods_dir):
                if mod_binary_pattern.match(name):
                    path = os.path.join(mods_dir, name)
                    try:
                        os.remove(path)
                        if verbose:
                            log.append(
                                ("verbose", f"Verbose: Removed mod binary '{path}'")
                            )
                    except OSError as exc:
                        log.append(
                            ("warning", f"Warning: Could not remove '{name}': {exc}")
                        )

        # Remove userprofile.json
        userprofile = os.path.join(windhawk_root, "userprofile.json")
        try:
            if os.path.isfile(userprofile):
                os.remove(userprofile)
        except OSError as exc:
            log.append(
                ("warning", f"Warning: Could not remove userprofile.json: {exc}")
            )

        # Remove registry mod keys
        if not portable:
            try:
                subprocess.run(
                    [
                        "reg",
                        "delete",
                        r"HKLM\SOFTWARE\Windhawk\Engine\Mods",
                        "/f",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                log.append(("success", "Status: Removed existing registry mod state."))
            except OSError as exc:
                log.append(
                    ("warning", f"Warning: Could not clean registry mod state: {exc}")
                )

    finally:
        if not portable:
            _, msg = start_windhawk_service()
            log.append(("success", msg))

    log.append(("success", "Operation Complete: Cleanup finished successfully."))
    return True, log


def execute_restore_operation(
    windhawk_root: str,
    archive_path: str,
    portable: bool = False,
    verbose: bool = False,
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
            log.append(
                "Warning: Proceeding despite service stop issue. Files may be locked."
            )

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

                        if verbose:
                            for root, _dirs, files in os.walk(real_src):
                                for file in files:
                                    rel_file = os.path.relpath(
                                        os.path.join(root, file),
                                        stage_dir,
                                    )
                                    log.append(f"Verbose: Restored '{rel_file}'")
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
                            check=True,
                            capture_output=True,
                            text=True,
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                        log.append("Status: Registry imported.")
                    except subprocess.CalledProcessError as exc:
                        log.append(
                            f"ERROR: Registry import failed: {exc.stderr.strip()}"
                        )
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
        "info": "RoyalBlue",
        "success": "ForestGreen",
        "warning": "DarkOrange",
        "error": "Crimson",
        "verbose": "#666666",
        "header": "#202020",
        "summary": "#006400",
    }

    # Treeview column definitions: heading text, pixel width, anchor
    TV_COLUMNS: dict[str, tuple[str, int, str]] = {
        "date": ("Date / Time", 172, "w"),
        "size": ("Size", 68, "e"),
        "kind": ("Type", 74, "center"),
        "mods": ("Mods", 48, "center"),
        "name": ("Archive Name", 300, "w"),
    }

    LOG_PREFIX_LEVELS: dict[str, str] = {
        "Verbose:": "verbose",
        "Status:": "success",
        "Info:": "info",
        "Warning:": "warning",
        "ERROR:": "error",
        "===": "header",
        "---": "header",
        "Summary:": "summary",
    }

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)

        self._cfg = load_config()

        self.root.geometry(self._cfg.get("geometry", "820x680"))
        self.root.minsize(920, 580)

        self._last_normal_geometry: str | None = None
        self._log_window: tk.Toplevel | None = None

        self._apply_style()

        # Sort state: col -> bool (True = ascending)
        self._sort_ascending: dict[str, bool] = {c: True for c in self.TV_COLUMNS}

        # Debounced auto-save
        self._save_timer_id: str | None = None

        self._known_backup_snapshot: set[tuple[str, float]] = set()

        outer = ttk.Frame(root, padding=PAD)
        outer.pack(fill=tk.BOTH, expand=True)

        # Top bar with Help & README button
        top_bar = ttk.Frame(outer)
        top_bar.pack(fill=tk.X, pady=(0, PAD))
        ttk.Label(
            top_bar, text="Windhawk Backup Utility", font=("Segoe UI", 10, "bold")
        ).pack(side=tk.LEFT)
        ttk.Button(
            top_bar, text=" Help & README ", width=16, command=self._show_help_readme
        ).pack(side=tk.RIGHT)

        self._build_config_section(outer)
        self._build_archive_section(outer)
        self._build_log_section(outer)
        self._build_status_bar(root)

        self._configure_log_tags()
        self._apply_config()
        self._refresh_backup_list()

        self.root.after(3000, self._auto_refresh_backups)

        self.root.bind("<Configure>", self._on_window_configure)
        self.tree.bind("<Delete>", self._on_delete_key)
        self.tree.bind("<BackSpace>", self._on_delete_key)
        self.tree.bind("<Button-3>", self._on_tree_right_click)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._setup_variable_traces()

        self.root.after(50, self._restore_window_state)

    # ------------------------------------------------------------------
    # Styling
    # ------------------------------------------------------------------

    def _apply_style(self) -> None:
        s = ttk.Style()
        s.theme_use("vista")

        s.configure("Treeview", rowheight=23, font=("Segoe UI", 9))
        s.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
        s.map(
            "Treeview",
            background=[("selected", "#CCE4F7")],
            foreground=[("selected", "#000000")],
        )

        s.configure(
            "Accent.Horizontal.TProgressbar",
            troughcolor="#E4E4E4",
            background="#3A9BD5",
            thickness=5,
        )

        s.configure(
            "Status.TLabel",
            font=("Segoe UI", 8),
            foreground="#555555",
            background="#F0F0F0",
        )
        s.configure("StatusBar.TFrame", background="#F0F0F0", relief="sunken")

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
        wh_label = ttk.Label(frame, text="Windhawk Root:")
        wh_label.grid(row=0, column=0, **lbl)  # ty: ignore[invalid-argument-type]
        ToolTip(
            wh_label,
            "Path to the Windhawk installation directory.\n"
            "Must contain ModsSource and/or Engine\\Mods.\n"
            "Auto-detected from common locations on first run.",
        )

        wh_entry = ttk.Entry(frame, textvariable=self.windhawk_path_var)
        wh_entry.grid(row=0, column=1, **ent)  # ty: ignore[invalid-argument-type]
        ToolTip(
            wh_entry, "Supports environment variables, e.g. %ProgramData%\\Windhawk"
        )

        wh_browse = ttk.Button(
            frame, text="Browse...", width=10, command=self._select_windhawk_path
        )
        wh_browse.grid(row=0, column=2, padx=(PAD, 0), pady=4)
        ToolTip(wh_browse, "Browse for the Windhawk installation directory.")

        # Backup folder
        self.backup_path_var = tk.StringVar()
        bk_label = ttk.Label(frame, text="Backup Base Folder:")
        bk_label.grid(row=1, column=0, **lbl)  # ty: ignore[invalid-argument-type]
        ToolTip(
            bk_label,
            "Folder where backup .zip archives are saved AND scanned from.\n"
            "This is both the save location and the read location.",
        )

        bk_entry = ttk.Entry(frame, textvariable=self.backup_path_var)
        bk_entry.grid(row=1, column=1, **ent)  # ty: ignore[invalid-argument-type]
        ToolTip(
            bk_entry,
            "Supports environment variables for portability:\n"
            "  %USERPROFILE%\\Desktop\n"
            "  %APPDATA%\\Backups\n"
            "Variables are expanded at runtime.",
        )

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=1, column=2, padx=(PAD, 0), pady=4, sticky="w")

        bk_browse = ttk.Button(
            btn_frame,
            text="Browse...",
            width=10,
            command=self._select_backup_path,
        )
        bk_browse.pack(side=tk.LEFT)
        ToolTip(bk_browse, "Browse for a backup destination folder.")

        bk_script = ttk.Button(
            btn_frame,
            text="Script",
            width=8,
            command=self._set_backup_to_script,
        )
        bk_script.pack(side=tk.LEFT, padx=(4, 0))
        ToolTip(
            bk_script,
            "Set backup folder to the directory containing this script.\n"
            f"({_SCRIPT_DIR})",
        )

        bk_desktop = ttk.Button(
            btn_frame,
            text="Desktop",
            width=8,
            command=self._set_backup_to_desktop,
        )
        bk_desktop.pack(side=tk.LEFT, padx=(4, 0))
        ToolTip(
            bk_desktop,
            "Set backup folder to your Desktop.\n"
            "Stored as %USERPROFILE%\\Desktop for portability.",
        )

        # Options row
        opts = ttk.Frame(frame)
        opts.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(4, 0))

        ttk.Label(opts, text="Keep last").pack(side=tk.LEFT)

        self.max_backups_var = tk.IntVar(value=DEFAULT_MAX_BACKUPS)
        keep_spin = ttk.Spinbox(
            opts,
            from_=0,
            to=99,
            width=4,
            textvariable=self.max_backups_var,
            validate="focusout",
            validatecommand=(parent.register(self._validate_max_backups), "%P"),
        )
        keep_spin.pack(side=tk.LEFT, padx=(4, 4))
        ToolTip(
            keep_spin,
            "Maximum number of backup archives to keep.\n"
            "Oldest archives are deleted automatically after each backup.\n"
            "Set to 0 to disable rotation (unlimited backups).",
        )

        ttk.Label(opts, text="backups  (0 = unlimited)").pack(
            side=tk.LEFT, padx=(0, PAD * 2)
        )

        self.exclude_stale_dlls_var = tk.BooleanVar(value=True)
        stale_cb = ttk.Checkbutton(
            opts,
            text="Exclude stale DLLs",
            variable=self.exclude_stale_dlls_var,
        )
        stale_cb.pack(side=tk.LEFT, padx=(0, PAD))
        ToolTip(
            stale_cb,
            "When enabled, only the newest compiled DLL of each mod is\n"
            "included in the backup. Older versions are dropped.\n\n"
            "Prevents historical binaries from accumulating in restores.",
        )

        self.portable_var = tk.BooleanVar(value=False)
        portable_cb = ttk.Checkbutton(
            opts,
            text="Portable installation",
            variable=self.portable_var,
            command=self._on_portable_toggled,
        )
        portable_cb.pack(side=tk.LEFT, padx=(0, PAD))
        ToolTip(
            portable_cb,
            "Enable for portable Windhawk installations (no registry).\n"
            "Skips registry export/import and service stop/start.\n"
            "Only mod folders inside the Windhawk root are processed.",
        )

        autodetect_btn = ttk.Button(
            opts, text="Auto-Detect", width=11, command=self._auto_detect_portable
        )
        autodetect_btn.pack(side=tk.LEFT)
        ToolTip(
            autodetect_btn,
            "Detect installation type by checking the registry.\n"
            "If HKLM\\SOFTWARE\\Windhawk exists -> Standard.\n"
            "Otherwise -> Portable.",
        )

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
            tv_frame,
            columns=cols,
            show="headings",
            selectmode="browse",
            height=8,
        )
        for col, (heading, width, anchor) in self.TV_COLUMNS.items():
            self.tree.heading(
                col, text=heading, command=lambda c=col: self._sort_tree(c)
            )
            self.tree.column(
                col,
                width=width,
                minwidth=40,
                anchor=anchor,  # ty: ignore[invalid-argument-type]
                stretch=False,
            )

        vsb = ttk.Scrollbar(tv_frame, orient=tk.VERTICAL, command=self.tree.yview)

        hsb = ttk.Scrollbar(tv_frame, orient=tk.HORIZONTAL, command=self.tree.xview)

        self.tree.configure(
            yscrollcommand=vsb.set,
            xscrollcommand=hsb.set,
        )

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        self.tree.tag_configure("even", background="#FFFFFF")
        self.tree.tag_configure("odd", background="#F2F6FA")
        self.tree.bind("<Double-1>", self._on_tree_double_click)

        # Action buttons
        btn = ttk.Frame(frame)
        btn.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(PAD, 0))

        self.backup_button = ttk.Button(
            btn, text="Create Backup", width=16, command=self._run_backup
        )
        self.backup_button.pack(side=tk.LEFT)
        ToolTip(
            self.backup_button,
            "Create a new timestamped backup archive.\n"
            "Stops the Windhawk service, archives mods + registry,\n"
            "then restarts the service.",
        )

        self.restore_button = ttk.Button(
            btn, text="Restore Selected", width=16, command=self._restore_selected
        )
        self.restore_button.pack(side=tk.LEFT, padx=(PAD, 0))
        ToolTip(
            self.restore_button,
            "Restore the selected backup over the current Windhawk install.\n"
            "TIP: Run 'Clean Existing State' first for a deterministic restore.",
        )

        self.details_button = ttk.Button(
            btn, text="View Details", width=16, command=self._show_preview
        )
        self.details_button.pack(side=tk.LEFT, padx=(PAD, 0))
        ToolTip(
            self.details_button,
            "Show metadata and mod list of the selected backup.\n"
            "Same as double-clicking the row.",
        )

        self.delete_button = ttk.Button(
            btn, text="Delete Selected", width=16, command=self._delete_selected
        )
        self.delete_button.pack(side=tk.LEFT, padx=(PAD, 0))
        ToolTip(
            self.delete_button,
            "Permanently delete the selected backup archive.\nShortcut: Delete key.",
        )

        refresh_btn = ttk.Button(
            btn, text="Refresh", width=9, command=self._refresh_backup_list
        )
        refresh_btn.pack(side=tk.RIGHT)
        ToolTip(
            refresh_btn, "Re-scan the backup folder.\n(Auto-refreshes every 3 seconds.)"
        )

    def _build_log_section(self, parent: ttk.Frame) -> None:
        maint = ttk.LabelFrame(parent, text="Maintenance", padding=PAD)
        maint.pack(fill=tk.X, pady=(0, PAD))

        self.clean_button = ttk.Button(
            maint,
            text="Clean Existing State",
            width=22,
            command=self._clean_existing_state,
        )
        self.clean_button.pack(side=tk.LEFT)
        ToolTip(
            self.clean_button,
            "Remove all installed Windhawk mod state:\n"
            "  - Mod source files (ModsSource\\*.wh.cpp)\n"
            "  - Compiled mod DLLs in Engine\\Mods\\32 and \\64\n"
            "  - userprofile.json\n"
            "  - Registry key HKLM\\SOFTWARE\\Windhawk\\Engine\\Mods\n\n"
            "Windhawk runtime files are PRESERVED.\n"
            "Use before restore for a deterministic clean baseline.",
        )

        ttk.Label(
            maint,
            text="Removes installed mod state before restore operations.",
        ).pack(side=tk.LEFT, padx=(PAD, 0))

        frame = ttk.LabelFrame(parent, text="Operation Log", padding=PAD)
        frame.pack(fill=tk.X, pady=(0, PAD))
        frame.columnconfigure(0, weight=1)

        hdr = ttk.Frame(frame)
        hdr.grid(row=0, column=0, sticky="ew", pady=(0, 4))

        export_btn = ttk.Button(
            hdr, text="Export Log...", width=12, command=self._export_log
        )
        export_btn.pack(side=tk.RIGHT)
        ToolTip(export_btn, "Save the entire log to a .txt file.")

        copy_btn = ttk.Button(
            hdr,
            text="Copy Log",
            width=10,
            command=self._copy_log,
        )
        copy_btn.pack(side=tk.RIGHT, padx=(0, 6))
        ToolTip(copy_btn, "Copy the entire log to the clipboard.")

        clear_btn = ttk.Button(
            hdr,
            text="Clear Log",
            width=10,
            command=self._clear_log,
        )
        clear_btn.pack(side=tk.RIGHT, padx=(0, 6))
        ToolTip(clear_btn, "Clear the current log output window.")

        large_btn = ttk.Button(
            hdr, text="Toggle Large Log", width=17, command=self._open_log_window
        )
        large_btn.pack(side=tk.RIGHT, padx=(0, 6))
        ToolTip(large_btn, "Open the log in a larger window.\nClick again to close it.")

        self.verbose_logging_var = tk.BooleanVar(value=False)

        verbose_cb = ttk.Checkbutton(
            hdr,
            text="Verbose logging",
            variable=self.verbose_logging_var,
        )
        verbose_cb.pack(side=tk.LEFT)
        ToolTip(
            verbose_cb,
            "Log every individual file processed during backup,\n"
            "restore, and cleanup. Useful for diagnostics but very noisy.",
        )

        self.log_widget = scrolledtext.ScrolledText(
            frame,
            height=7,
            wrap=tk.WORD,
            state=tk.DISABLED,
            font=("Consolas", 9),
            relief="flat",
            background="#FAFAFA",
            borderwidth=1,
        )
        self.log_widget.grid(row=1, column=0, sticky="ew")

        self.progressbar = ttk.Progressbar(
            frame,
            mode="indeterminate",
            length=200,
            style="Accent.Horizontal.TProgressbar",
        )
        self.progressbar.grid(row=2, column=0, sticky="ew", pady=(6, 0))

    def _build_status_bar(self, parent: tk.Tk) -> None:
        bar = ttk.Frame(parent, style="StatusBar.TFrame", height=22)
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(bar, textvariable=self.status_var, style="Status.TLabel").pack(
            side=tk.LEFT, padx=(PAD, 0), pady=2
        )

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
            self.verbose_logging_var,
            self.exclude_stale_dlls_var,
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

        raw_backup = self._cfg.get("backup_folder", DEFAULT_BACKUP_FOLDER)

        self.backup_path_var.set(os.path.expandvars(raw_backup))

        self.portable_var.set(self._cfg.get("portable", False))
        self.max_backups_var.set(self._cfg.get("max_backups", DEFAULT_MAX_BACKUPS))
        self.verbose_logging_var.set(self._cfg.get("verbose_logging", False))

        self.exclude_stale_dlls_var.set(self._cfg.get("exclude_stale_dlls", True))

        self.log("Info: Configuration loaded.", "info")

    def _collect_config(self) -> dict:
        geometry = self._last_normal_geometry or self.root.geometry()

        widths: dict[str, int] = {}
        try:
            for col in self.TV_COLUMNS:
                widths[col] = int(self.tree.column(col, "width"))
        except Exception:
            pass

        backup_folder = self.backup_path_var.get().strip()

        try:
            userprofile = os.environ.get("USERPROFILE", "")
            if userprofile:
                normalized_home = os.path.normcase(os.path.normpath(userprofile))

                normalized_backup = os.path.normcase(os.path.normpath(backup_folder))

                if normalized_backup.startswith(normalized_home):
                    rel = os.path.relpath(backup_folder, userprofile)

                    backup_folder = os.path.join("%USERPROFILE%", rel)
        except Exception:
            pass

        return {
            "windhawk_root": self.windhawk_path_var.get().strip(),
            "backup_folder": backup_folder,
            "portable": self.portable_var.get(),
            "max_backups": self._safe_max_backups(),
            "verbose_logging": self.verbose_logging_var.get(),
            "exclude_stale_dlls": self.exclude_stale_dlls_var.get(),
            "geometry": geometry,
            "window_state": self.root.state(),
            "tree_column_widths": widths,
            "log_window_geometry": (
                self._log_window.geometry()
                if self._log_window and self._log_window.winfo_exists()
                else self._cfg.get("log_window_geometry", "1100x700")
            ),
        }

    def _safe_max_backups(self) -> int:
        """Reads max_backups spinbox, clamping non-integer input to default."""
        try:
            return max(0, int(self.max_backups_var.get()))
        except (tk.TclError, ValueError):
            return DEFAULT_MAX_BACKUPS

    def _get_effective_backup_folder(self) -> str:
        return os.path.expandvars(self.backup_path_var.get().strip() or _SCRIPT_DIR)

    def _validate_max_backups(self, value: str) -> bool:
        """Spinbox validatecommand: clamp bad input to DEFAULT on focus-out."""
        try:
            int(value)
            return True
        except ValueError:
            self.max_backups_var.set(DEFAULT_MAX_BACKUPS)
            return False

    def _on_window_configure(self, _event=None) -> None:
        try:
            if self.root.state() == "normal":
                if self.root.winfo_width() > 1 and self.root.winfo_height() > 1:
                    self._last_normal_geometry = self.root.geometry()
        except Exception:
            pass

    def _restore_window_state(self) -> None:
        try:
            state = self._cfg.get("window_state", "normal")

            if state == "zoomed":
                self.root.state("zoomed")
        except Exception:
            pass

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

    def _capture_backup_snapshot(self) -> set[tuple[str, float]]:
        folder = self._get_effective_backup_folder()
        if not os.path.isdir(folder):
            return set()

        result: set[tuple[str, float]] = set()

        for name in os.listdir(folder):
            if name.startswith("windhawk-backup_") and name.endswith(".zip"):
                path = os.path.join(folder, name)
                try:
                    result.add((name, os.path.getmtime(path)))
                except OSError:
                    pass

        return result

    def _auto_refresh_backups(self) -> None:
        try:
            current = self._capture_backup_snapshot()
            if current != self._known_backup_snapshot:
                self._known_backup_snapshot = current
                self._refresh_backup_list()
        finally:
            if self._cfg.get("auto_refresh", True):
                self.root.after(3000, self._auto_refresh_backups)

    def _refresh_backup_list(self) -> None:
        self.tree.delete(*self.tree.get_children())
        backups = list_backups(self._get_effective_backup_folder())
        for i, b in enumerate(backups):
            self.tree.insert(
                "",
                tk.END,
                iid=b["path"],
                values=(b["date"], b["size"], b["kind"], b["mods"], b["name"]),
                tags=("even" if i % 2 == 0 else "odd",),
            )
        self._known_backup_snapshot = self._capture_backup_snapshot()

        saved_widths = self._cfg.get("tree_column_widths", {})

        for col in self.TV_COLUMNS:
            saved = saved_widths.get(col)

            if isinstance(saved, int) and saved > 0:
                self.tree.column(col, width=saved)
            else:
                self._autosize_tree_column(col)

        count = len(backups)
        self._set_status(
            f"{count} backup{'s' if count != 1 else ''} found."
            if count
            else "No backups found in the selected folder."
        )

    def _sort_tree(self, col: str) -> None:
        """
        Sorts treeview rows by the clicked column.
        Toggles ascending/descending on repeated clicks.
        """
        ascending = self._sort_ascending.get(col, True)
        items = [(self.tree.set(iid, col), iid) for iid in self.tree.get_children()]
        items.sort(key=lambda x: x[0], reverse=not ascending)
        for i, (_val, iid) in enumerate(items):
            self.tree.move(iid, "", i)
            self.tree.item(iid, tags=("even" if i % 2 == 0 else "odd",))
        # Flip for next click; reset all others to ascending
        for c in self._sort_ascending:
            self._sort_ascending[c] = True
        self._sort_ascending[col] = not ascending

    def _autosize_tree_column(self, col: str) -> None:
        """Fits a treeview column to the widest visible content using actual font metrics."""
        heading = self.TV_COLUMNS[col][0]
        try:
            font = tkfont.nametofont(self.tree.cget("font"))
        except Exception:
            font = tkfont.nametofont("TkDefaultFont")

        max_px = font.measure(heading)

        for iid in self.tree.get_children():
            value = str(self.tree.set(iid, col))
            max_px = max(max_px, font.measure(value))

        # Add padding for cell borders, focus ring, and scrollbar safety
        width = min(max(60, max_px + 24), 700)
        self.tree.column(col, width=width)

    def _on_tree_double_click(self, event: tk.Event) -> None:
        region = self.tree.identify_region(event.x, event.y)

        if region == "separator":
            col_id = self.tree.identify_column(event.x)
            try:
                idx = int(col_id.replace("#", "")) - 1
                col = list(self.TV_COLUMNS.keys())[idx]
                self._autosize_tree_column(col)
            except Exception:
                pass
            return

        self._show_preview()

    def _selected_archive_path(self) -> str | None:
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _on_delete_key(self, _event=None) -> None:
        widget = self.root.focus_get()

        if widget is not None:
            # Do not hijack Delete key from text-entry widgets
            if isinstance(widget, (tk.Entry, tk.Text, tk.Listbox)):
                return

            widget_class = ""
            try:
                widget_class = widget.winfo_class()
            except Exception:
                pass

            if widget_class in (
                "TEntry",
                "Text",
                "Spinbox",
                "TSpinbox",
                "Entry",
            ):
                return

        self._delete_selected()

    def _on_tree_right_click(self, event) -> None:
        item = self.tree.identify_row(event.y)
        if not item:
            return

        self.tree.selection_set(item)

        menu = tk.Menu(
            self.root,
            tearoff=0,
        )

        menu.add_command(
            label="View Details",
            command=self._show_preview,
        )

        menu.add_command(
            label="Restore Selected",
            command=self._restore_selected,
        )

        menu.add_command(
            label="Delete Selected",
            command=self._delete_selected,
        )
        menu.add_separator()
        menu.add_command(
            label="Open Backup Folder",
            command=lambda: os.startfile(self._get_effective_backup_folder()),
        )

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

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

    def _set_backup_to_script(self) -> None:
        """Set backup folder to the script directory."""
        self.backup_path_var.set(_SCRIPT_DIR)
        self._refresh_backup_list()

        self.log(
            "Info: Backup folder set to script directory.",
            "info",
        )

    def _set_backup_to_desktop(self) -> None:
        """Set backup folder to the user's desktop."""
        desktop = os.path.join(
            os.path.expanduser("~"),
            "Desktop",
        )

        if not os.path.isdir(desktop):
            desktop = os.path.expanduser("~")

        self.backup_path_var.set(desktop)
        self._refresh_backup_list()

        self.log(
            f"Info: Backup folder set to: {desktop}",
            "info",
        )

    # ------------------------------------------------------------------
    # Portable / auto-detect
    # ------------------------------------------------------------------

    def _auto_detect_portable(self) -> None:
        if registry_key_exists(WINDHAWK_REGISTRY_KEY):
            self.portable_var.set(False)
            self.log(
                "Info: Registry key found - standard installation detected. "
                "Portable mode disabled.",
                "info",
            )
        else:
            self.portable_var.set(True)
            self.log(
                "Info: Registry key not found - portable installation assumed. "
                "Portable mode enabled.",
                "warning",
            )

    def _on_portable_toggled(self) -> None:
        if self.portable_var.get():
            self.log(
                "Info: Portable mode enabled - registry steps will be skipped.",
                "warning",
            )
        else:
            self.log(
                "Info: Portable mode disabled - registry steps will be included.",
                "info",
            )

    # ------------------------------------------------------------------
    # Logging and status
    # ------------------------------------------------------------------

    def _configure_log_tags(self) -> None:
        for tag, colour in self.LOG_COLOURS.items():
            self.log_widget.tag_config(tag, foreground=colour)

    def _open_log_window(self) -> None:
        """Toggle large log window."""
        if self._log_window and self._log_window.winfo_exists():
            try:
                focused = self._log_window.focus_get()
                if focused and focused.winfo_toplevel() == self._log_window:
                    self._log_window.destroy()
                    self._log_window = None
                    return

                self._log_window.deiconify()
                self._log_window.lift()
                self._log_window.focus_force()
                return
            except Exception:
                self._log_window = None

        win = tk.Toplevel(self.root)
        self._log_window = win

        win.title("Operation Log - Large View")
        win.geometry(self._cfg.get("log_window_geometry", "1100x700"))
        win.minsize(700, 400)

        toolbar = ttk.Frame(win)
        toolbar.pack(fill=tk.X, padx=PAD, pady=(PAD, 0))

        def _copy_large_log():
            try:
                content = txt.get("1.0", tk.END)

                self.root.clipboard_clear()
                self.root.clipboard_append(content)
                self.root.update()

                self.log("Info: Large log copied to clipboard.", "info")

            except Exception as exc:
                messagebox.showerror(
                    "Copy Failed",
                    str(exc),
                )

        ttk.Button(
            toolbar,
            text="Copy Log",
            width=12,
            command=_copy_large_log,
        ).pack(side=tk.RIGHT)

        txt = scrolledtext.ScrolledText(
            win,
            wrap=tk.NONE,
            font=("Consolas", 10),
        )
        txt.pack(fill=tk.BOTH, expand=True, padx=PAD, pady=PAD)

        txt.insert("1.0", self.log_widget.get("1.0", tk.END))
        txt.config(state=tk.DISABLED)

        def _close():
            try:
                self._cfg["log_window_geometry"] = win.geometry()
            except Exception:
                pass
            self._log_window = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", _close)
        win.focus_force()

    def log(self, message: str, level: str = "info") -> None:
        """Appends a timestamped message to the log widget (thread-safe)."""
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        text = f"[{ts}]  {message}"

        for prefix, inferred_level in self.LOG_PREFIX_LEVELS.items():
            if message.startswith(prefix):
                level = inferred_level
                break

        def _write() -> None:
            self.log_widget.config(state=tk.NORMAL)
            self.log_widget.insert(tk.END, text + "\n", (level,))
            self.log_widget.see(tk.END)
            self.log_widget.config(state=tk.DISABLED)

        self.root.after(0, _write)

    def _set_status(self, text: str) -> None:
        self.root.after(0, lambda: self.status_var.set(text))

    def _copy_log(self) -> None:
        try:
            content = self.log_widget.get("1.0", tk.END)

            self.root.clipboard_clear()
            self.root.clipboard_append(content)
            self.root.update()

            self.log("Info: Log copied to clipboard.", "info")

        except Exception as exc:
            messagebox.showerror(
                "Copy Failed",
                str(exc),
            )

    def _clear_log(self) -> None:
        self.log_widget.config(state=tk.NORMAL)
        self.log_widget.delete("1.0", tk.END)
        self.log_widget.config(state=tk.DISABLED)

        self.log("Info: Log cleared.", "info")

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
                    manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except Exception as exc:
            messagebox.showerror("Preview Failed", f"Could not read archive:\n{exc}")
            return

        win = tk.Toplevel(self.root)
        win.title(f"Backup Details  -  {os.path.basename(archive)}")
        win.geometry("480x540")
        win.minsize(400, 460)
        win.resizable(True, True)
        win.grab_set()

        outer = ttk.Frame(win, padding=PAD)
        outer.pack(fill=tk.BOTH, expand=True)

        def _row(parent: tk.Widget, label: str, value: str, row: int) -> None:
            ttk.Label(
                parent, text=label, font=("Segoe UI", 9, "bold"), anchor="w"
            ).grid(row=row, column=0, sticky="w", padx=(0, PAD), pady=3)
            ttk.Label(parent, text=value, anchor="w").grid(
                row=row, column=1, sticky="ew", pady=3
            )

        meta = ttk.LabelFrame(outer, text="Archive Information", padding=PAD)
        meta.pack(fill=tk.X, pady=(0, PAD))
        meta.columnconfigure(1, weight=1)

        size_bytes = os.path.getsize(archive)

        if manifest:
            _row(meta, "Created:", manifest.get("created", "-"), 0)
            _row(meta, "Utility Version:", manifest.get("app_version", "-"), 1)
            _row(meta, "Architecture:", manifest.get("arch", "Unknown"), 2)
            _row(meta, "Machine:", manifest.get("hostname", "-"), 3)
            _row(
                meta,
                "Installation:",
                "Portable" if manifest.get("portable") else "Standard",
                4,
            )
            _row(meta, "Mod Count:", str(manifest.get("mod_count", "-")), 5)
            _row(meta, "Archive Size:", _format_size(size_bytes), 6)
        else:
            _row(meta, "Archive:", os.path.basename(archive), 0)
            _row(meta, "Size:", _format_size(size_bytes), 1)
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
                lb_frame,
                font=("Consolas", 9),
                selectmode=tk.BROWSE,
                relief="flat",
                borderwidth=0,
                background="#FAFAFA",
                activestyle="none",
                highlightthickness=1,
                highlightcolor="#CCE4F7",
                highlightbackground="#DDDDDD",
            )
            sb = ttk.Scrollbar(lb_frame, orient=tk.VERTICAL, command=lb.yview)
            lb.configure(yscrollcommand=sb.set)
            lb.grid(row=0, column=0, sticky="nsew")
            sb.grid(row=0, column=1, sticky="ns")

            for i, mod in enumerate(sorted(mods)):
                display = mod[:-7] if mod.endswith(".wh.cpp") else mod
                lb.insert(tk.END, f"  {display}")
                lb.itemconfig(i, background="#FFFFFF" if i % 2 == 0 else "#F2F6FA")
        else:
            ttk.Label(
                mod_frame,
                text="No mod list available (legacy backup).",
                foreground="DarkOrange",
            ).grid(sticky="w")

        ttk.Button(outer, text="Close", width=10, command=win.destroy).pack(anchor="e")

    # ------------------------------------------------------------------
    # About / Info
    # ------------------------------------------------------------------

    def _show_help_readme(self) -> None:
        """Opens the Help & README dialog with tabbed documentation."""
        candidates_text = "\n".join(f"    {c}" for c in WINDHAWK_ROOT_CANDIDATES)
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
            tab1,
            wrap=tk.WORD,
            font=("Segoe UI", 9),
            relief="flat",
            background="#FAFAFA",
            state=tk.NORMAL,
        )
        txt1.pack(fill=tk.BOTH, expand=True)
        txt1.insert(
            tk.END,
            (
                "WHAT THIS TOOL DOES\n"
                "\n"
                "Backs up and restores your Windhawk configuration by\n"
                "stopping the Windhawk service, copying mod sources,\n"
                "compiled mods, registry data, and related metadata into\n"
                "a timestamped ZIP, then restarting the service.\n"
                "\n"
                "The utility can automatically exclude stale compiled mod\n"
                "DLLs from backups to avoid carrying historical binaries\n"
                "forward into future restores.\n"
                "\n"
                "The utility also supports CLEANING existing Windhawk\n"
                "mod state before restore operations. This prevents stale\n"
                "compiled DLLs and historical mod remnants from causing\n"
                "incorrect update states or duplicated binaries.\n"
                "\n"
                "Backup filename pattern:\n"
                "  windhawk-backup_{hostname}_{arch}_{timestamp}.zip\n"
                "  Example: windhawk-backup_DESKTOP-ABC123_AMD64_20260115_143022.zip\n"
                "\n"
                "BACKUP FOLDER BEHAVIOR\n"
                "  The selected backup folder is BOTH:\n"
                "    \u2022 where new backups are saved\n"
                "    \u2022 where existing backups are scanned/restored from\n"
                "\n"
                "  There is NO hidden or automatic subfolder.\n"
                "\n"
                "DETAILS / PREVIEW\n"
                "  Backup details can be opened using:\n"
                "    \u2022 View Details button\n"
                "    \u2022 Double-click on a backup row\n"
                "    \u2022 Right-click -> View Details\n"
                "\n"
                "Manifest inside each archive contains:\n"
                "  \u2022 hostname, architecture, mod list, portable flag,\n"
                "    creation time, and the utility version used.\n"
            ),
        )
        txt1.config(state=tk.DISABLED)

        # ---- Tab 2: What is backed up ----
        tab2 = ttk.Frame(notebook)
        notebook.add(tab2, text="Backed up files")
        txt2 = scrolledtext.ScrolledText(
            tab2,
            wrap=tk.WORD,
            font=("Segoe UI", 9),
            relief="flat",
            background="#FAFAFA",
            state=tk.NORMAL,
        )
        txt2.pack(fill=tk.BOTH, expand=True)
        txt2.insert(
            tk.END,
            (
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
                "VERBOSE LOGGING\n"
                "  Verbose logging records every file restored, removed,\n"
                "  staged, skipped, and processed. Useful for diagnostics.\n"
                "\n"
                "PORTABLE INSTALLATIONS\n"
                "  Registry is NOT included. Only the mod folders\n"
                "  inside the Windhawk root are archived.\n"
                "\n"
                "CONFIG PORTABILITY\n"
                "  Backup paths are stored using environment variables\n"
                "  when possible.\n"
                "\n"
                "  Example:\n"
                "    %USERPROFILE%\\Desktop\n"
                "\n"
                "  This allows the script + config to be shared between\n"
                "  machines/users (Dropbox, USB drives, etc.) without\n"
                "  hardcoded usernames becoming a failure point.\n"
            ),
        )
        txt2.config(state=tk.DISABLED)

        # ---- Tab 3: Registry source & backup location ----
        tab3 = ttk.Frame(notebook)
        notebook.add(tab3, text="Registry source")
        txt3 = scrolledtext.ScrolledText(
            tab3,
            wrap=tk.WORD,
            font=("Segoe UI", 9),
            relief="flat",
            background="#FAFAFA",
            state=tk.NORMAL,
        )
        txt3.pack(fill=tk.BOTH, expand=True)
        txt3.insert(
            tk.END,
            (
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
                '  GitHub issue #639 \u2014 "Stop using the registry as a way\n'
                '  to store settings for mods" is open. If Windhawk migrates\n'
                "  mod settings to plain files, the registry step will no longer\n"
                "  be needed. This utility will adapt when that happens.\n"
            ),
        )
        txt3.config(state=tk.DISABLED)

        # ---- Tab 4: Restore notes ----
        tab4 = ttk.Frame(notebook)
        notebook.add(tab4, text="Restore notes")
        txt4 = scrolledtext.ScrolledText(
            tab4,
            wrap=tk.WORD,
            font=("Segoe UI", 9),
            relief="flat",
            background="#FAFAFA",
            state=tk.NORMAL,
        )
        txt4.pack(fill=tk.BOTH, expand=True)
        txt4.insert(
            tk.END,
            (
                "CLEAN RESTORE RECOMMENDATION\n"
                "  Windhawk accumulates stale compiled mod DLLs over time.\n"
                "  Using 'Clean Existing State' before restoring ensures\n"
                "  the backup is restored into a deterministic clean baseline\n"
                "  instead of merging into historical leftovers.\n"
                "\n"
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
                "AUTO-REFRESH\n"
                "  The backup list automatically refreshes every 3 seconds.\n"
                "  Manual refresh is also available.\n"
                "\n"
                "ROOT AUTO-DETECT CANDIDATES (probed in order)\n"
                f"{candidates_text}\n"
            ),
        )
        txt4.config(state=tk.DISABLED)

        ttk.Button(win, text="Close", width=10, command=win.destroy).pack(pady=(0, PAD))

    # ------------------------------------------------------------------
    # Operation control
    # ------------------------------------------------------------------

    def _set_controls_enabled(self, enabled: bool) -> None:
        state = tk.NORMAL if enabled else tk.DISABLED
        for btn in (
            self.backup_button,
            self.restore_button,
            self.details_button,
            self.delete_button,
            self.clean_button,
        ):
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
            try:
                success, messages = execute_backup_operation(
                    cfg["windhawk_root"],
                    effective_folder,
                    portable=cfg["portable"],
                    max_backups=cfg["max_backups"],
                    verbose=cfg["verbose_logging"],
                    exclude_stale_dlls=cfg["exclude_stale_dlls"],
                    live_log_callback=self.log,
                )

            except Exception:
                tb = traceback.format_exc()

                success = False
                messages = [
                    ("error", "ERROR: Unhandled exception during backup."),
                    ("error", tb),
                ]

            self.root.after(0, lambda: self._on_backup_done(success, messages))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_backup_done(self, success: bool, _messages: list[tuple[str, str]]) -> None:
        self._set_controls_enabled(True)

        self._set_status("Backup completed." if success else "Backup failed - see log.")
        self._refresh_backup_list()
        if success:
            messagebox.showinfo(
                "Backup Succeeded", "The backup completed successfully."
            )
        else:
            messagebox.showerror(
                "Backup Failed", "An error occurred. Please review the log."
            )

    # ------------------------------------------------------------------
    # Restore
    # ------------------------------------------------------------------

    def _restore_selected(self) -> None:
        archive = self._selected_archive_path()
        if not archive:
            messagebox.showinfo(
                "No Selection", "Please select a backup from the list to restore."
            )
            return

        wh_path = self.windhawk_path_var.get().strip()
        if not wh_path:
            messagebox.showwarning(
                "Configuration Incomplete", "Please specify the Windhawk root path."
            )
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
                wh_path,
                archive,
                portable,
                self.verbose_logging_var.get(),
            )
            self.root.after(0, lambda: self._on_restore_done(success, message))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_restore_done(self, success: bool, message: str) -> None:
        self._set_controls_enabled(True)
        self.log(message, "success" if success else "error")
        self._set_status(
            "Restore completed." if success else "Restore failed - see log."
        )
        if success:
            messagebox.showinfo(
                "Restore Succeeded", "The restore completed successfully."
            )
        else:
            messagebox.showerror(
                "Restore Failed", "An error occurred. Please review the log."
            )

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def _clean_existing_state(self) -> None:
        wh_path = self.windhawk_path_var.get().strip()
        if not wh_path:
            messagebox.showwarning(
                "Configuration Incomplete",
                "Please specify the Windhawk root path.",
            )
            return

        if not messagebox.askyesno(
            "Confirm Cleanup",
            "This will remove all currently installed Windhawk mods,\n"
            "compiled mod DLLs, registry mod state, and userprofile.json.\n\n"
            "Windhawk runtime files will be preserved.\n\n"
            "Continue?",
            icon="warning",
        ):
            return

        self.log("\n--- Cleanup started ---", "warning")
        self._set_status("Cleanup in progress...")
        self._set_controls_enabled(False)

        portable = self.portable_var.get()

        def _worker() -> None:
            success, messages = cleanup_windhawk_mod_state(
                wh_path,
                portable,
                self.verbose_logging_var.get(),
            )
            self.root.after(0, lambda: self._on_cleanup_done(success, messages))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_cleanup_done(self, success: bool, messages: list[tuple[str, str]]) -> None:
        self._set_controls_enabled(True)

        for level, message in messages:
            self.log(message, level)
        self._set_status(
            "Cleanup completed." if success else "Cleanup failed - see log."
        )

        if success:
            messagebox.showinfo(
                "Cleanup Succeeded",
                "Existing Windhawk mod state was removed successfully.",
            )
        else:
            messagebox.showerror(
                "Cleanup Failed",
                "An error occurred. Please review the log.",
            )

    def _delete_selected(self) -> None:
        archive = self._selected_archive_path()
        if not archive:
            messagebox.showinfo(
                "No Selection", "Please select a backup from the list to delete."
            )
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
