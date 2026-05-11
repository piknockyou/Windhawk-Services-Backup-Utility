# Windhawk Service Management Utility (Fork)

> 📌 **Based on** [wsbu.py](https://github.com/scorpion421/Windhawk-Services-Backup-Utility) by **scorpion421** (GPL)  
> This fork adds architecture awareness, portable‑first design, smarter auto‑detection, richer UI, and several utility improvements.  
> **Please note:** There are **no compiled releases** – run the Python source directly.

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)
![Version](https://img.shields.io/badge/Version-2.8.8--pyw-green)
![License](https://img.shields.io/badge/License-GPL-yellow)

<img width="922" height="859" alt="image" src="https://github.com/user-attachments/assets/26908127-fc24-4fc7-ba7a-d59641641e86" />

---

## Features

### Core backup / restore
- **One‑click backup** of mod sources, compiled mods, and registry settings into a timestamped ZIP
- **One‑click restore** from any existing archive  
- **Clean Existing State** – dedicated maintenance tool to wipe stale mod state (sources, compiled DLLs, UI caches, registry) while preserving Windhawk's core runtime files, guaranteeing a deterministic restore baseline
- **Smart Stale DLL Exclusion** – automatically detects and drops obsolete compiled mod binaries during backup, preventing historical artifact buildup
- **Service management** – automatically stops Windhawk before file operations and restarts it afterwards, even on errors  
- **Archive integrity check** – every new backup is validated with `zipfile.testzip()`  
- **Backup rotation** – keeps only the last *N* archives (configurable, 0 = unlimited)

### Architecture & hostname awareness
- **Backup filename** now includes **hostname** and **CPU architecture**:  
  `windhawk-backup_DESKTOP-ABC_AMD64_20260115_143022.zip`  
- **Manifest enrichment** – each archive’s `manifest.json` records `arch`, `hostname`, mod list, and creation time  
- **Cross‑architecture warning** – on restore, if the backup was created on a different CPU (e.g. AMD64 → ARM64), a clear warning is logged so you don’t blindly import incompatible binary mods  
- (see *Architecture note* below for more details)

### Portable‑first & smarter paths
- **Config lives next to the script** (not hidden in `%AppData%`) – fully portable  
  – Auto‑migration from the old AppData location on first run  
- **Default backup folder** is the **script’s directory** (with an optional `Windhawk_Backup` subfolder, toggleable in the UI) – keep backups next to the tool  
- **Windhawk root auto‑detection** – probes several known install locations on startup if the configured path is invalid

### UI / UX
- **Help & README** button – opens a tabbed dialog with detailed documentation  
- **Live auto-refresh** – backup list automatically updates in the background without UI flickering
- **Sortable & auto-sizing backup list** – columns automatically fit content; double-click separators to auto-fit
- **Backup preview** – double‑click any archive to see its mod list, metadata, and full manifest  
- **Debounced auto‑save** – settings are saved silently 1 second after any change  
- **Advanced Logging** – features a toggleable Verbose mode (file-level tracking), a Large View window, end-of-operation summaries, and line-by-line color coding
- **Status bar** and **threaded operations** – UI stays responsive during backup/restore

### Other improvements
- **Elevation fix** for `.pyw` files – the `ShellExecuteW` call now correctly separates the interpreter and script paths, so elevation works when launched without a console  
- Transparent documentation – Help tab explicitly lists Windhawk’s registry usage and the exact directories backed up

---

## Requirements

- Windows 10 / 11  
- **Python 3.10+** (standard library only – no extra packages)  
- **Administrator privileges** (for registry and service control)

---

## Usage

### First run
- The tool tries to **auto‑detect** your Windhawk root (checks `%ProgramData%\Windhawk`, `%LocalAppData%\Windhawk`, `C:\Windhawk`, etc.).  
- Backup folder defaults to the script’s directory with a `Windhawk_Backup` subfolder. You can change the **base folder** and toggle the subfolder on/off.  
- Settings are saved in a config file next to the script (e.g. `wsbu.config.json`).

### Backup
1. Verify the **Windhawk Root** and **Backup Base Folder** fields.  
2. Optionally adjust the **number of backups to keep**, or toggle **Exclude stale DLLs**.  
3. Click **Create Backup** – the service is stopped, files staged, obsolete DLLs dropped, registry exported, ZIP created, and the service restarted.

### Restore & Maintenance
1. **Highly Recommended:** Click **Clean Existing State** first. This safely wipes old compiled DLLs and out-of-sync UI caches, ensuring the destination is perfectly clean.
2. Select a backup from the list (double‑click to preview its contents first).  
3. Click **Restore Selected** and confirm the overwrite prompt.  
4. If the backup’s CPU architecture differs from your machine, a warning will appear in the log – **pay attention to it** (see *Architecture note* below).

### Portable installations
- Tick **Portable installation** to skip all registry steps, or click **Auto‑Detect** to let the tool decide (it checks for the registry key).

### Help
- Click the **Help & README** button to open a tabbed reference explaining what is backed up, the registry source, restore caveats, and the auto‑detect candidate list.

---

## Architecture note 🧩

Windhawk compiles mods into **native DLLs** located in `Engine\Mods`. These are **CPU‑specific** – an AMD64 DLL will not run on ARM64, and vice versa.

- **Backups** now record the machine’s architecture (`AMD64` or `ARM64`) in both the file name and the manifest.
- During **restore**, the tool compares the backup’s architecture with your current PC. If they differ, it logs a prominent warning:
  > *“Architecture mismatch — backup was created on AMD64, this machine is ARM64. Compiled mods may not work.”*

The mod *source* (`ModsSource`) and registry settings are architecture‑agnostic and can be transferred freely – only the compiled DLLs are affected. When moving between architectures, you should **re‑download the mods** after restoring the source and registry.

---

## Default paths

| Item | Path / Pattern |
|------|----------------|
| Windhawk root (fallback) | `%ProgramData%\Windhawk` |
| Backup **base** folder | Script directory |
| Backup subfolder (on by default) | `.\Windhawk_Backup\` |
| Config file | `wsbu.config.json` (next to the script) |
| Archive naming | `windhawk-backup_{hostname}_{arch}_{timestamp}.zip` |

---

## Acknowledgments

- Original idea and UI‑first PowerShell script by [@lokize](https://github.com/lokize) ([comment](https://github.com/ramensoftware/windhawk/issues/195#issuecomment-3184189085))
- Solid foundational Python tool by [@scorpion421](https://github.com/scorpion421) – this fork is built on that excellent work.

---

## License

GPL License – see [LICENSE](LICENSE) for details.
