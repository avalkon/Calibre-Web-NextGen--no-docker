#!/usr/bin/env python3
# /srv/calibre-ingest/watched_ingest.py
# CWA-Compatible Ingest Watcher with Full Status File Support
# Prevents files in the retry queue from being processed by the normal scanner.
# Moves successful source/conversion files to processed/.
# Moves permanently failed files to failed/.
# Honors retry_queue_file from dirs.json.
# Creates the configured temporary conversion directory.
# Handles filesystem errors without killing the watcher.
# Uses atomic-ish retry queue writes to reduce corruption risk.
# Avoids leaving converted files in the ingest directory after successful import.
# FIXED: Adds BOTH original and converted formats, timeout wraps full pipeline, 
#     max-retry files are permanently ignored after moving to failed/.

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path


# Configuration files
DIRS_JSON = Path("/opt/calibre-web-nextgen/dirs.json")
CWA_DB = Path("/opt/calibre-web-nextgen/cwa.db")

# Status tracking files (matches CWA format exactly)
STATUS_FILE = Path("/opt/calibre-web-nextgen/config/cwa_ingest_status")
RETRY_QUEUE_FILE = Path("/opt/calibre-web-nextgen/config/cwa_ingest_retry_queue")

# Metadata change logs directory (triggers cover_enforcer.py)
METADATA_CHANGE_LOGS_DIR = Path("/opt/calibre-web-nextgen/config/metadata_change_logs")

# Set by watch_directory() from dirs.json.
CALIBRE_LIBRARY = ""


def load_dirs_config():
    """Load paths from CWA's dirs.json."""
    default_config = {
        "ingest_folder": "/srv/calibre-ingest",
        "calibre_library_dir": "/home/ava/Dusty Bookshelf",
        "tmp_conversion_dir": "/tmp/cwa_conversions",
        "processed_folder": "/srv/calibre-ingest/processed",
        "failed_folder": "/srv/calibre-ingest/failed",
        "retry_queue_file": str(RETRY_QUEUE_FILE),
        "max_retry_attempts": 3,
        "retry_interval_seconds": 300,
    }

    if not DIRS_JSON.exists():
        return default_config

    try:
        with open(DIRS_JSON, "r", encoding="utf-8") as f:
            config = json.load(f)

        if not isinstance(config, dict):
            raise ValueError("dirs.json root must be an object")

        # Preserve the original behavior of accepting quoted string values.
        for key, value in list(config.items()):
            if isinstance(value, str):
                value = value.strip()
                if len(value) >= 2 and value.startswith("'") and value.endswith("'"):
                    value = value[1:-1]
                config[key] = value

        return {**default_config, **config}

    except (OSError, json.JSONDecodeError, ValueError) as e:
        print(f"[CONFIG] Error reading dirs.json: {e}")
        return default_config


def load_cwa_settings():
    """Read settings from cwa.db cwa_settings table."""
    default_settings = {
        "auto_convert": 1,
        "auto_convert_target_format": "epub",
        "auto_ingest_automerge": 0,
        "auto_ingest_ignored_formats": "",
        "auto_convert_ignored_formats": "",
        "auto_convert_retained_formats": "",
        "ingest_timeout_minutes": 15,
        "ingest_stale_temp_minutes": 120,
        "ingest_stale_temp_interval": 600,
    }

    if not CWA_DB.exists():
        return default_settings

    conn = None
    try:
        conn = sqlite3.connect(CWA_DB)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM cwa_settings LIMIT 1")
        row = cursor.fetchone()

        if row:
            columns = [desc[0] for desc in cursor.description]
            return {**default_settings, **dict(zip(columns, row))}

        return default_settings

    except sqlite3.Error as e:
        print(f"[SETTINGS] Error reading cwa.db: {e}")
        return default_settings

    finally:
        if conn is not None:
            conn.close()


# ============================================================
# METADATA CHANGE LOG FUNCTIONS (NEW)
# ============================================================

def write_metadata_change_log(book_id, title, authors=None, **extra_fields):
    """
    Write a metadata change log that triggers cover_enforcer.py.
    
    The log filename format is: {YYYYMMDDHHMMSS}-{book_id}.json
    The metadata-change-detector watches for these files and runs:
      cover_enforcer.py --log <filename>
    """
    try:
        METADATA_CHANGE_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[METADATA] Error creating metadata log directory: {e}")
        return None

    payload = {
        "book_id": str(book_id),
        "title": title or "",
        "authors": authors or [],
    }
    payload.update(extra_fields)

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    filename = f"{timestamp}-{book_id}.json"
    path = METADATA_CHANGE_LOGS_DIR / filename

    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"[METADATA] Wrote change log: {filename}")
        return path
    except OSError as e:
        print(f"[METADATA] Error writing change log: {e}")
        return None


# ============================================================
# STATUS FILE FUNCTIONS (CWA Compatible Format)
# ============================================================

def write_ingest_status(state, filename="", detail=""):
    """
    Write status file in CWA format:
    state:filename:YYYY-MM-DD HH:MM:SS:detail

    Possible states: idle, processing, failed, stopped
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status_line = f"{state}:{filename}:{timestamp}:{detail}"

    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            f.write(status_line)
        print(f"[STATUS] {state.upper()}: {filename or 'idle'}")
    except OSError as e:
        print(f"[STATUS] Error writing status file: {e}")


def clear_ingest_status():
    """Clear status file when idle."""
    try:
        if STATUS_FILE.exists():
            STATUS_FILE.unlink()
    except OSError:
        pass


# ============================================================
# RETRY QUEUE FUNCTIONS
# ============================================================

def get_retry_queue_file(config=None):
    """Return the configured retry queue path."""
    if config:
        configured = config.get("retry_queue_file")
        if configured:
            return Path(configured)
    return RETRY_QUEUE_FILE


def load_retry_queue(config=None):
    """Load retry queue from JSON file."""
    retry_file = get_retry_queue_file(config)

    if not retry_file.exists():
        return []

    try:
        with open(retry_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, list):
            print(f"[RETRY] Invalid queue format in {retry_file}; resetting.")
            return []

        return data

    except (json.JSONDecodeError, OSError) as e:
        print(f"[RETRY] Error reading queue: {e}")
        return []


def save_retry_queue(queue_data, config=None):
    """Save retry queue to JSON file using a temporary file then replace."""
    retry_file = get_retry_queue_file(config)

    try:
        retry_file.parent.mkdir(parents=True, exist_ok=True)
        temp_file = retry_file.with_name(retry_file.name + ".tmp")

        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(queue_data, f, indent=2)
            f.write("\n")

        os.replace(temp_file, retry_file)

    except OSError as e:
        print(f"[RETRY] Error saving queue: {e}")


def retry_queue_contains(filename, config=None):
    """Return True if filename is already represented in the retry queue."""
    return any(
        entry.get("filename") == filename
        for entry in load_retry_queue(config)
        if isinstance(entry, dict)
    )


def add_to_retry_queue(filepath, error_msg, config):
    """Add or update a file in the retry queue."""
    queue_data = load_retry_queue(config)

    existing = None
    for entry in queue_data:
        if isinstance(entry, dict) and entry.get("filename") == filepath.name:
            existing = entry
            break

    if existing is not None:
        # Keep the current attempt count when an existing retry is re-queued.
        existing["original_path"] = str(filepath)
        existing["last_attempt"] = datetime.now().isoformat()
        existing["error"] = str(error_msg)[:500]
        print(
            f"[RETRY] Updated queue entry: {filepath.name} "
            f"(attempt {existing.get('attempts', 1)}/{existing.get('max_attempts', 3)})"
        )
    else:
        retry_entry = {
            "filename": filepath.name,
            "original_path": str(filepath),
            "attempts": 1,
            "max_attempts": int(config.get("max_retry_attempts", 3)),
            "last_attempt": datetime.now().isoformat(),
            "error": str(error_msg)[:500],
        }
        queue_data.append(retry_entry)
        print(
            f"[RETRY] Added to queue: {filepath.name} "
            f"(attempt 1/{retry_entry['max_attempts']})"
        )

    save_retry_queue(queue_data, config)


# ============================================================
# SHUTDOWN HANDLER
# ============================================================

def signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    print("\n[SHUTDOWN] Received termination signal...")
    write_ingest_status("stopped", "", "shutdown requested")
    sys.exit(0)


signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)


# ============================================================
# PARSING & VALIDATION FUNCTIONS
# ============================================================

def parse_format_list(formats_str):
    """Parse comma-separated format list."""
    if not formats_str:
        return set()

    result = set()
    for fmt in str(formats_str).split(","):
        fmt = fmt.strip().lower()
        if not fmt:
            continue
        result.add(fmt if fmt.startswith(".") else f".{fmt}")

    return result


def get_processing_timeout(settings):
    """Get timeout for processing in seconds."""
    try:
        timeout_min = int(settings.get("ingest_timeout_minutes", 15))
    except (TypeError, ValueError):
        timeout_min = 15

    return max(1, timeout_min) * 60


# ============================================================
# CLEANUP FUNCTIONS
# ============================================================

def cleanup_stale_temps(tmp_dir, stale_minutes, settings=None):
    """Remove temp files older than threshold."""
    if not tmp_dir:
        return 0

    tmp_path = Path(tmp_dir)

    if not tmp_path.exists():
        return 0

    try:
        stale_minutes = max(1, int(stale_minutes))
    except (TypeError, ValueError):
        stale_minutes = 120

    cutoff_time = datetime.now() - timedelta(minutes=stale_minutes)
    cleaned = 0

    try:
        files = list(tmp_path.rglob("*"))
    except OSError as e:
        print(f"[CLEANUP] Error scanning {tmp_path}: {e}")
        return 0

    for file in files:
        if not file.is_file():
            continue

        try:
            mtime = datetime.fromtimestamp(file.stat().st_mtime)
            if mtime < cutoff_time:
                file.unlink()
                cleaned += 1
                print(f"[CLEANUP] Removed stale temp: {file.relative_to(tmp_path)}")
        except (OSError, FileNotFoundError) as e:
            print(f"[CLEANUP] Error cleaning {file}: {e}")

    return cleaned


# ============================================================
# FILESYSTEM HELPERS
# ============================================================

def ensure_directory(path):
    """Create a directory if needed."""
    Path(path).mkdir(parents=True, exist_ok=True)


def move_to_folder(filepath, folder, overwrite=False):
    """
    Move filepath into folder.

    Returns the destination Path on success, otherwise None.
    By default, never silently overwrites an existing file.
    """
    filepath = Path(filepath)
    folder = Path(folder)

    try:
        ensure_directory(folder)
        destination = folder / filepath.name

        if destination.exists():
            if overwrite:
                destination.unlink()
            else:
                # Preserve both files rather than crashing the watcher.
                stem = destination.stem
                suffix = destination.suffix
                timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                destination = folder / f"{stem}.{timestamp}{suffix}"

        filepath.rename(destination)
        return destination

    except (OSError, FileNotFoundError) as e:
        print(f"[FILES] Error moving {filepath} -> {folder}: {e}")
        return None


def move_failed_file(filepath, config):
    """Move a permanently failed source file to failed_folder."""
    destination = move_to_folder(filepath, config.get("failed_folder"))

    if destination:
        print(f"[FAILED] Moved to: {destination}")
        return True

    return False


def remove_retry_entry(filename, config):
    """Remove all retry entries for filename."""
    queue_data = load_retry_queue(config)
    new_queue = [
        entry
        for entry in queue_data
        if not isinstance(entry, dict) or entry.get("filename") != filename
    ]

    if len(new_queue) != len(queue_data):
        save_retry_queue(new_queue, config)


# ============================================================
# CONVERSION & LIBRARY FUNCTIONS
# ============================================================

def convert_file(source_path, target_format, tmp_dir=None, timeout_sec=1800):
    """Convert using ebook-convert with timeout."""
    source_path = Path(source_path)

    if not target_format:
        print(f"[CONVERT] No target format specified for {source_path.name}")
        return None

    target_format = str(target_format).lstrip(".").lower()
    output_path = source_path.with_suffix(f".{target_format}")

    if tmp_dir:
        try:
            ensure_directory(tmp_dir)
        except OSError as e:
            print(f"[CONVERT] Cannot create temp directory {tmp_dir}: {e}")
            write_ingest_status(
                "failed",
                source_path.name,
                f"temp directory error: {str(e)[:100]}",
            )
            return None

    env = os.environ.copy()

    if tmp_dir:
        env["TEMP"] = str(tmp_dir)
        env["TMP"] = str(tmp_dir)
        env["TMPDIR"] = str(tmp_dir)

    cmd = ["ebook-convert", str(source_path), str(output_path)]

    try:
        write_ingest_status(
            "processing",
            source_path.name,
            "conversion started",
        )

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_sec,
        )

        if result.returncode == 0 and output_path.exists():
            print(f"[CONVERT] ✓ {source_path.name} → {output_path.name}")
            write_ingest_status(
                "processing",
                source_path.name,
                "conversion complete",
            )
            return output_path

        error = (result.stderr or result.stdout or "ebook-convert failed").strip()
        print(f"[CONVERT] ✗ {source_path.name}: {error[:500]}")
        write_ingest_status(
            "failed",
            source_path.name,
            f"conversion error: {error[:100]}",
        )
        return None

    except subprocess.TimeoutExpired:
        print(
            f"[CONVERT] ⏱️ TIMEOUT after {timeout_sec}s "
            f"for {source_path.name}"
        )
        write_ingest_status(
            "failed",
            source_path.name,
            "conversion timeout",
        )
        return None
    except OSError as e:
        print(f"[CONVERT] ✗ {source_path.name}: {e}")
        write_ingest_status(
            "failed",
            source_path.name,
            str(e)[:100],
        )
        return None
    except Exception as e:
        print(f"[CONVERT] ✗ {source_path.name}: {e}")
        write_ingest_status(
            "failed",
            source_path.name,
            str(e)[:100],
        )
        return None


def add_to_library(filepath, automerge=False, settings=None):
    """
    Add file to Calibre library using calibredb.
    Returns (success: bool, book_id: int or None)
    """
    if not CALIBRE_LIBRARY:
        print("[LIBRARY] ✗ Calibre library path is not configured")
        return False, None
    
    cmd = [
        "calibredb",
        "add",
        "--library-path",
        CALIBRE_LIBRARY,
        str(filepath),
    ]
    if automerge:
        cmd.append("--automergeresult")
    
    timeout_sec = get_processing_timeout(settings or load_cwa_settings())
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        if result.returncode == 0:
            # Try to extract book_id from output
            # calibredb outputs: "Added book ids: 42"
            book_id = None
            try:
                for line in result.stdout.split('\n'):
                    if 'Added book ids:' in line:
                        parts = line.split(':')
                        if len(parts) > 1:
                            book_id = int(parts[1].strip())
                            break
            except (ValueError, IndexError):
                pass
            return True, book_id
        error = (result.stderr or result.stdout or "calibredb failed").strip()
        print(f"[LIBRARY] ✗ {filepath.name}: {error[:500]}")
        return False, None
    except subprocess.TimeoutExpired:
        print(f"[LIBRARY] ⏱️ TIMEOUT adding {filepath.name}")
        return False, None
    except OSError as e:
        print(f"[LIBRARY] ✗ {filepath.name}: {e}")
        return False, None
    except Exception as e:
        print(f"[LIBRARY] ✗ {filepath.name}: {e}")
        return False, None


# ============================================================
# BOOK PROCESSING PIPELINE
# ============================================================

def should_ingest(filepath, settings):
    """Check if file should be ingested (filter ignored formats)."""
    ignored = parse_format_list(
        settings.get("auto_ingest_ignored_formats", "")
    )
    if filepath.suffix.lower() in ignored:
        return False, f"format {filepath.suffix} is in ingest ignore list"
    return True, "allowed"


def should_convert(source_ext, settings):
    """Determine if file should be converted."""
    try:
        auto_convert = int(settings.get("auto_convert", 1))
    except (TypeError, ValueError):
        auto_convert = 1
    if not auto_convert:
        return False, None, "auto_convert disabled"
    target_format = str(
        settings.get("auto_convert_target_format", "epub")
    ).lower().replace(".", "")
    target_ext = f".{target_format}"
    retained = parse_format_list(
        settings.get("auto_convert_retained_formats", "")
    )
    ignored_conv = parse_format_list(
        settings.get("auto_convert_ignored_formats", "")
    )
    if source_ext in retained:
        return False, source_ext, "format is retained"
    if source_ext in ignored_conv:
        return False, source_ext, "format is ignored for conversion"
    if source_ext == target_ext:
        return False, source_ext, "already target format"
    return True, target_ext, "convert needed"


def process_book(filepath, settings, config):
    """
    Full processing pipeline with status tracking.
    
    When conversion is needed:
    1. Add ORIGINAL format to library
    2. Convert to target format
    3. Add CONVERTED format to library
    4. Write metadata change log for the original (to trigger cover_enforcer)
    5. Move both files to processed/
    
    The entire operation from start to file move must complete within timeout.
    """
    filepath = Path(filepath)
    if not filepath.exists():
        return False

    original_filepath = filepath
    converted_filepath = None
    original_book_id = None
    converted_book_id = None
    
    # Get the timeout that covers the ENTIRE operation
    timeout_sec = get_processing_timeout(settings)
    operation_start = time.time()
    
    def time_remaining():
        elapsed = time.time() - operation_start
        return max(1, timeout_sec - elapsed)
    
    # Check if should ingest.
    allowed, reason = should_ingest(filepath, settings)
    if not allowed:
        print(f"[INGEST] Skip {filepath.name}: {reason}")
        return False

    source_ext = filepath.suffix.lower()
    # Check if should convert.
    needs_conversion, target_or_source, reason = should_convert(
        source_ext,
        settings,
    )
    
    if needs_conversion:
        # FIX #1: Add ORIGINAL format FIRST before conversion
        print(f"[INGEST] Adding original format: {original_filepath.name}")
        try:
            automerge = bool(
                int(settings.get("auto_ingest_automerge", 0))
            )
        except (TypeError, ValueError):
            automerge = False
        
        success, original_book_id = add_to_library(
            original_filepath,
            automerge,
            settings,
        )
        if not success:
            print(f"[INGEST] ✗ Failed to add original format {original_filepath.name}")
            add_to_retry_queue(
                original_filepath,
                "Failed to add original format",
                config,
            )
            write_ingest_status(
                "idle",
                "",
                "file queued for retry",
            )
            return False
        
        print(f"[INGEST] ✓ Original added (book_id={original_book_id})")
        
        # Now convert (with remaining timeout)
        tmp_dir = config.get("tmp_conversion_dir")
        remaining = time_remaining()
        
        converted_filepath = convert_file(
            original_filepath,
            target_or_source.replace(".", ""),
            tmp_dir,
            int(remaining),
        )
        if not converted_filepath:
            print(f"[INGEST] ✗ Conversion failed for {original_filepath.name}")
            add_to_retry_queue(
                original_filepath,
                "Conversion failed",
                config,
            )
            write_ingest_status(
                "idle",
                "",
                "file queued for retry",
            )
            return False
        
        # FIX #1: Add CONVERTED format as well
        print(f"[INGEST] Adding converted format: {converted_filepath.name}")
        remaining = time_remaining()
        success, converted_book_id = add_to_library(
            converted_filepath,
            automerge,
            settings,
        )
        if not success:
            print(f"[INGEST] ✗ Failed to add converted format {converted_filepath.name}")
            print(f"[INGEST] Note: Original {original_filepath.name} was already added as book_id={original_book_id}")
            add_to_retry_queue(
                original_filepath,
                "Failed to add converted format",
                config,
            )
            write_ingest_status(
                "idle",
                "",
                "file queued for retry",
            )
            return False
        
        print(f"[INGEST] ✓ Converted added (book_id={converted_book_id})")
        
        # Write metadata log for the ORIGINAL (not converted)
        if original_book_id:
            title = original_filepath.stem.split(' - ')[0] if ' - ' in original_filepath.stem else original_filepath.stem
            write_metadata_change_log(
                book_id=original_book_id,
                title=title,
                authors=[],
                source="auto-ingest",
            )
        
        # Move both to processed
        processed_folder = Path(config.get("processed_folder"))
        
        converted_destination = move_to_folder(
            converted_filepath,
            processed_folder,
        )
        if converted_destination is None:
            print(
                f"[INGEST] ✓ Both formats imported, "
                f"but could not move converted file to processed/"
            )
            write_ingest_status(
                "failed",
                original_filepath.name,
                "import succeeded; processed move failed",
            )
            return True
        
        original_destination = move_to_folder(
            original_filepath,
            processed_folder,
        )
        if original_destination is None:
            print(
                f"[INGEST] ✓ Both formats imported, "
                f"but original source could not be moved to processed/"
            )
            write_ingest_status(
                "failed",
                original_filepath.name,
                "import succeeded; original move failed",
            )
            return True
        
        print(
            f"[INGEST] ✓ Imported both formats: "
            f"{original_destination.name} + {converted_destination.name}"
        )
        
    else:
        # No conversion needed: just add original and move it
        try:
            automerge = bool(
                int(settings.get("auto_ingest_automerge", 0))
            )
        except (TypeError, ValueError):
            automerge = False

        success, original_book_id = add_to_library(
            filepath,
            automerge,
            settings,
        )
        if not success:
            add_to_retry_queue(
                original_filepath,
                "Library add failed",
                config,
            )
            write_ingest_status(
                "idle",
                "",
                "file queued for retry",
            )
            return False
        
        # Write metadata log
        if original_book_id:
            title = original_filepath.stem.split(' - ')[0] if ' - ' in original_filepath.stem else original_filepath.stem
            write_metadata_change_log(
                book_id=original_book_id,
                title=title,
                authors=[],
                source="auto-ingest",
            )
        
        # Move to processed
        processed_folder = Path(config.get("processed_folder"))
        destination = move_to_folder(
            filepath,
            processed_folder,
        )
        if destination is None:
            print(
                f"[INGEST] ✓ Imported {filepath.name}, "
                f"but could not move it to processed/"
            )
            write_ingest_status(
                "failed",
                filepath.name,
                "import succeeded; processed move failed",
            )
            return True

        print(f"[INGEST] ✓ Imported: {destination.name}")

    # The file was successfully imported. Remove any stale retry entry.
    remove_retry_entry(original_filepath.name, config)
    write_ingest_status(
        "idle",
        "",
        "processing complete",
    )
    return True


# ============================================================
# RETRY PROCESSING
# ============================================================

def process_from_retry_queue(settings, config):
    """Process files from retry queue that are ready."""
    try:
        max_interval = max(
            1,
            int(config.get("retry_interval_seconds", 300)),
        )
    except (TypeError, ValueError):
        max_interval = 300

    queue_data = load_retry_queue(config)
    processed = []
    changed = False
    for entry in queue_data[:]:
        if not isinstance(entry, dict):
            queue_data.remove(entry)
            changed = True
            continue
        try:
            last_attempt = datetime.fromisoformat(entry["last_attempt"])
        except (KeyError, ValueError, TypeError):
            print(
                f"[RETRY] Invalid last_attempt for "
                f"{entry.get('filename', '?')}; removing entry."
            )
            queue_data.remove(entry)
            changed = True
            continue
        if datetime.now() - last_attempt < timedelta(seconds=max_interval):
            continue
        try:
            attempts = int(entry.get("attempts", 1))
            max_attempts = int(entry.get("max_attempts", 3))
        except (TypeError, ValueError):
            attempts = 1
            max_attempts = 3

        # Once the recorded number of attempts has been exhausted,
        # permanently fail the source file.
        # FIX #3: After moving to failed, also ignore it in future scans
        if attempts >= max_attempts:
            filename = entry.get("filename", "unknown")
            filepath = Path(entry.get("original_path", ""))
            print(
                f"[RETRY] Max attempts ({max_attempts}) reached "
                f"for {filename}; moving to failed folder and blacklisting"
            )
            if filepath.exists():
                move_failed_file(filepath, config)
            
            # Remove from retry queue so it's never retried again
            queue_data.remove(entry)
            changed = True
            continue
        
        filepath = Path(entry.get("original_path", ""))
        if not filepath.exists():
            print(
                f"[RETRY] File no longer exists: "
                f"{entry.get('filename', '?')}"
            )
            queue_data.remove(entry)
            changed = True
            continue
        next_attempt = attempts + 1
        print(
            f"[RETRY] Attempt {next_attempt}/{max_attempts} "
            f"for {filepath.name}"
        )
        write_ingest_status(
            "processing",
            filepath.name,
            "retry started",
        )

        # process_book() may update the queue itself if it fails.
        if process_book(filepath, settings, config):
            # It was successfully imported and process_book() removes any
            # stale retry entry.
            queue_data = load_retry_queue(config)
            processed.append(filepath.name)
            changed = True
        else:
            # Re-read the queue because process_book() may have updated it.
            queue_data = load_retry_queue(config)

            # Find the entry again and update its attempt timestamp/count.
            updated = False
            for current in queue_data:
                if (
                    isinstance(current, dict)
                    and current.get("filename") == filepath.name
                ):
                    current["attempts"] = next_attempt
                    current["last_attempt"] = datetime.now().isoformat()
                    current["error"] = current.get(
                        "error",
                        "retry failed",
                    )
                    updated = True
                    changed = True
                    break
            if not updated:
                # Defensive fallback: recreate the entry.
                queue_data.append(
                    {
                        "filename": filepath.name,
                        "original_path": str(filepath),
                        "attempts": next_attempt,
                        "max_attempts": max_attempts,
                        "last_attempt": datetime.now().isoformat(),
                        "error": "retry failed",
                    }
                )
                changed = True
    if changed:
        save_retry_queue(queue_data, config)

    return processed


# ============================================================
# MAIN WATCH LOOP
# ============================================================

def watch_directory():
    """Main polling loop with status file updates."""
    global CALIBRE_LIBRARY

    # Load paths from dirs.json at startup.
    config = load_dirs_config()
    ingest_folder = Path(config.get("ingest_folder"))
    processed_folder = Path(config.get("processed_folder"))
    failed_folder = Path(config.get("failed_folder"))
    tmp_conversion_dir = config.get("tmp_conversion_dir")
    CALIBRE_LIBRARY = config.get("calibre_library_dir", "")

    # Create all necessary directories.
    for directory in [
        ingest_folder,
        processed_folder,
        failed_folder,
        STATUS_FILE.parent,
        METADATA_CHANGE_LOGS_DIR,
    ]:
        ensure_directory(directory)

    if tmp_conversion_dir:
        ensure_directory(tmp_conversion_dir)

    retry_file = get_retry_queue_file(config)
    retry_file.parent.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("[WATCHER] Calibre-Web NextGen CWA-Compatible Ingest Watcher")
    print("=" * 60)
    print(f"Ingest Directory:       {ingest_folder}")
    print(f"Status File:            {STATUS_FILE}")
    print(f"Retry Queue:            {retry_file}")
    print(f"Metadata Change Logs:   {METADATA_CHANGE_LOGS_DIR}")
    print(f"Calibre Library:        {CALIBRE_LIBRARY}")
    print(f"CWA Settings DB:        {CWA_DB}")
    print("Poll Interval:          10 seconds")
    print("=" * 60)
    print("")

    # Initialize status file - show we're running/idle.
    write_ingest_status("idle", "", "watcher active")
    scan_iteration = 0
    last_cleanup = datetime.now()

    while True:
        scan_iteration += 1
        try:
            # Reload settings each scan so admin panel changes are reflected.
            settings = load_cwa_settings()
            timeout_sec = get_processing_timeout(settings)

            try:
                stale_temp_min = int(
                    settings.get("ingest_stale_temp_minutes", 120)
                )
            except (TypeError, ValueError):
                stale_temp_min = 120

            try:
                stale_interval = max(
                    1,
                    int(settings.get("ingest_stale_temp_interval", 600)),
                )
            except (TypeError, ValueError):
                stale_interval = 600

            # Log settings every 10 scans.
            if scan_iteration % 10 == 0:
                print(
                    f"[SETTINGS] auto_convert="
                    f"{settings.get('auto_convert', '?')}, "
                    f"target_format="
                    f"{settings.get('auto_convert_target_format', '?')}, "
                    f"timeout={timeout_sec // 60}min"
                )

            # Stale temp cleanup.
            if (
                datetime.now() - last_cleanup
            ).total_seconds() >= stale_interval:
                cleaned = cleanup_stale_temps(
                    tmp_conversion_dir,
                    stale_temp_min,
                    settings,
                )
                if cleaned > 0:
                    print(
                        f"[CLEANUP] Removed {cleaned} stale temp file(s)"
                    )
                last_cleanup = datetime.now()

            # Process retry queue before the normal scan every 5 scans.
            if scan_iteration % 5 == 0:
                queue = load_retry_queue(config)
                if queue:
                    retried = process_from_retry_queue(
                        settings,
                        config,
                    )
                    if retried:
                        print(
                            f"[RETRY] Successfully re-processed: "
                            f"{retried}"
                        )

            # Load retry queue once. Files represented there must NOT be
            # processed by the normal scanner, otherwise the retry interval
            # is bypassed.
            retry_queue = load_retry_queue(config)
            retry_filenames = {
                entry.get("filename")
                for entry in retry_queue
                if isinstance(entry, dict)
            }

            try:
                files = [
                    f
                    for f in ingest_folder.iterdir()
                    if f.is_file() and not f.name.startswith(".")
                ]
            except OSError as e:
                print(
                    f"[SCAN] Error reading ingest directory "
                    f"{ingest_folder}: {e}"
                )
                files = []

            processed_count = 0
            for filepath in files:
                if filepath.name in retry_filenames:
                    print(
                        f"[SCAN] Skipping queued retry: "
                        f"{filepath.name}"
                    )
                    continue

                print(f"[SCAN] Processing {filepath.name}...")
                if process_book(filepath, settings, config):
                    processed_count += 1

            status = (
                f"Scan #{scan_iteration}: "
                f"{processed_count} new file(s)"
            )

            queue = load_retry_queue(config)
            if queue:
                status += f" | {len(queue)} in retry queue"
            print(f"[WATCHER] {status}")

        except Exception as e:
            # A single unexpected file/config/database problem must not
            # terminate the long-running watcher.
            print(f"[WATCHER] Unexpected error in scan loop: {e}")
            write_ingest_status(
                "failed",
                "",
                f"watcher error: {str(e)[:100]}",
            )
        time.sleep(10)


if __name__ == "__main__":
    watch_directory()
